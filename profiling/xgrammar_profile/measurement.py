"""Worker dispatch and append-only result recording."""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

from .config import write_json_atomic
from .environment import steal_ticks
from .process_guard import GuardResult, run_guarded
from .validation import RecordValidationError, parse_worker_jsonl, validate_result_record
from .variants import Variant


class MeasurementError(RuntimeError):
    pass


def initialize_jsonl(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8"):
            pass
    except FileExistsError as exc:
        raise MeasurementError(f"refusing to overwrite append-only results: {path}") from exc


def append_records(path: Path, records: List[Dict[str, Any]]) -> None:
    for record in records:
        validate_result_record(record)
    with path.open("a", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()


def _guard_record(guard: GuardResult, metadata: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "record_type": "guard",
        "experiment": metadata["experiment"],
        "status": guard.status,
        "case_id": metadata["case_id"],
        "block_id": metadata["block_id"],
        "measured": metadata["measured"],
        "variant": metadata.get("variant"),
        "arm": metadata.get("arm"),
        "wall_time_ns": guard.elapsed_ns,
        "cpu_time_ns": None,
        "compile_time_ns": None,
        "peak_rss_bytes": guard.peak_rss_bytes,
        "baseline_rss_bytes": None,
        "compiled_grammar_bytes": None,
        "exit_code": guard.exit_code,
        "signal": guard.signal,
        "ended_by": guard.ended_by,
        "last_rss_bytes": guard.last_rss_bytes,
        "measurement_peak_rss_bytes": guard.measurement_peak_rss_bytes,
        "baseline_ready_observed": guard.baseline_ready_observed,
        "measurement_end_observed": guard.measurement_end_observed,
        "measurement_end_rss_bytes": guard.measurement_end_rss_bytes,
        "measurement_completed_monotonic_ns": guard.measurement_completed_monotonic_ns,
        "rss_poll_interval_seconds": guard.rss_poll_interval_seconds,
        "cgroup_peak_bytes": guard.cgroup_peak_bytes,
        "cgroup_events": guard.cgroup_events,
        "stderr_tail": guard.stderr_tail,
        "config_hash": metadata.get("config_hash"),
        "variant_manifest_sha256": metadata.get("variant_manifest_sha256"),
        "workload_class": metadata.get("workload_class"),
        "cache_limit_bytes": metadata.get("cache_limit_bytes"),
        "compiler_threads": metadata.get("compiler_threads"),
        "workload_fingerprint": metadata.get("workload_fingerprint"),
        "expected_request_count": metadata.get("expected_request_count"),
        "family": metadata.get("family"),
        "bound": metadata.get("bound"),
        "steal_rerun_of_block": metadata.get("steal_rerun_of_block"),
    }


def _censored_record(status: str, metadata: Dict[str, Any], guard: GuardResult) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "record_type": "stream-summary" if metadata["experiment"] == "cache" else "sample",
        "experiment": metadata["experiment"],
        "status": status,
        "case_id": metadata["case_id"],
        "block_id": metadata["block_id"],
        "measured": metadata["measured"],
        "variant": metadata.get("variant"),
        "arm": metadata.get("arm"),
        "wall_time_ns": guard.elapsed_ns,
        "cpu_time_ns": None,
        "compile_time_ns": None,
        "peak_rss_bytes": guard.peak_rss_bytes,
        "baseline_rss_bytes": None,
        "compiled_grammar_bytes": None,
        "config_hash": metadata.get("config_hash"),
        "variant_manifest_sha256": metadata.get("variant_manifest_sha256"),
        "workload_class": metadata.get("workload_class"),
        "cache_limit_bytes": metadata.get("cache_limit_bytes"),
        "compiler_threads": metadata.get("compiler_threads"),
        "workload_fingerprint": metadata.get("workload_fingerprint"),
        "expected_request_count": metadata.get("expected_request_count"),
        "family": metadata.get("family"),
        "bound": metadata.get("bound"),
        "steal_rerun_of_block": metadata.get("steal_rerun_of_block"),
    }


def run_worker(
    *,
    worker: Path,
    job: Dict[str, Any],
    job_path: Path,
    raw_path: Path,
    variant: Variant,
    execution: Dict[str, Any],
    metadata: Dict[str, Any],
) -> List[Dict[str, Any]]:
    canonical_job = dict(job)
    write_json_atomic(job_path, canonical_job)
    job = dict(canonical_job)
    baseline_ready_path = job_path.with_suffix(job_path.suffix + ".baseline.json")
    baseline_ack_path = job_path.with_suffix(job_path.suffix + ".baseline.ack")
    measurement_end_ready_path = job_path.with_suffix(job_path.suffix + ".measurement-end.json")
    measurement_end_ack_path = job_path.with_suffix(job_path.suffix + ".measurement-end.ack")
    measurement_end_temporary_path = measurement_end_ready_path.with_name(
        f".{measurement_end_ready_path.name}.tmp"
    )
    guarded_job_path = job_path.with_suffix(job_path.suffix + ".guarded")
    for stale in (
        baseline_ready_path,
        baseline_ack_path,
        measurement_end_ready_path,
        measurement_end_ack_path,
        measurement_end_temporary_path,
    ):
        try:
            stale.unlink()
        except FileNotFoundError:
            pass
    job["baseline_ready_path"] = str(baseline_ready_path)
    job["baseline_ack_path"] = str(baseline_ack_path)
    job["measurement_end_ready_path"] = str(measurement_end_ready_path)
    job["measurement_end_ack_path"] = str(measurement_end_ack_path)
    write_json_atomic(guarded_job_path, job)
    command = [sys.executable, str(worker), "--job", str(guarded_job_path)]
    affinity = metadata.get("cpu_affinity")
    if affinity is not None:
        if not isinstance(affinity, list) or not affinity or len(set(affinity)) != len(affinity):
            raise MeasurementError(f"invalid worker CPU affinity: {affinity}")
        if hasattr(os, "sched_getaffinity") and not set(affinity).issubset(os.sched_getaffinity(0)):
            raise MeasurementError(
                f"worker affinity {affinity} is outside harness allowance {sorted(os.sched_getaffinity(0))}"
            )
        if platform.system() == "Linux":
            taskset = shutil.which("taskset")
            if taskset is None:
                raise MeasurementError("per-worker CPU affinity requires taskset on Linux")
            command = [taskset, "--cpu-list", ",".join(str(cpu) for cpu in affinity)] + command
    steal_before = steal_ticks(affinity)
    guarded_started = time.monotonic()
    try:
        guard = run_guarded(
            command,
            timeout_seconds=float(execution["timeout_seconds"]),
            rss_limit_bytes=int(execution["rss_limit_bytes"]),
            poll_interval_seconds=float(execution["rss_poll_interval_seconds"]),
            grace_seconds=float(execution["termination_grace_seconds"]),
            cwd=worker.parents[2],
            env=variant.environment(),
            baseline_ready_path=baseline_ready_path,
            baseline_ack_path=baseline_ack_path,
            measurement_end_ready_path=measurement_end_ready_path,
            measurement_end_ack_path=measurement_end_ack_path,
        )
    finally:
        for transient in (
            guarded_job_path,
            baseline_ready_path,
            baseline_ack_path,
            measurement_end_ready_path,
            measurement_end_ack_path,
            measurement_end_temporary_path,
        ):
            try:
                transient.unlink()
            except FileNotFoundError:
                pass
    guarded_elapsed = time.monotonic() - guarded_started
    steal_after = steal_ticks(affinity)
    steal_delta = (
        max(0, steal_after - steal_before)
        if steal_before is not None and steal_after is not None
        else None
    )
    if steal_delta is not None and guarded_elapsed > 0:
        ticks_per_second = os.sysconf("SC_CLK_TCK")
        steal_fraction = steal_delta / (
            ticks_per_second * guarded_elapsed * max(1, len(affinity) if affinity else 1)
        )
    else:
        steal_fraction = None
    threshold = metadata.get("steal_time_threshold")
    steal_flagged = bool(
        steal_fraction is not None
        and isinstance(threshold, (int, float))
        and steal_fraction > threshold
    )
    records: List[Dict[str, Any]] = []
    parse_error: str | None = None
    try:
        records = parse_worker_jsonl(guard.stdout)
    except RecordValidationError as exc:
        parse_error = str(exc)
        records = []
    for record in records:
        if (
            metadata["experiment"] == "cache"
            and record.get("record_type") == "sample"
            and record.get("status") != "success"
            and "request_index" not in record
        ):
            record["record_type"] = "stream-summary"

    def is_canonical(record: Dict[str, Any]) -> bool:
        return (
            metadata["experiment"] == "cache" and record.get("record_type") == "stream-summary"
        ) or (metadata["experiment"] == "repetition" and record.get("record_type") == "sample")

    canonical = [record for record in records if is_canonical(record)]
    canonical_is_usable = (
        len(canonical) == 1
        and not (guard.status != "success" and canonical[0].get("status") == "success")
        and not (canonical[0].get("status") == "success" and not guard.measurement_end_observed)
    )
    if not canonical_is_usable:
        # Retain any successfully parsed per-request evidence, but replace malformed,
        # duplicate, or contradicted canonical outcomes with exactly one supervisor
        # outcome.  This prevents a worker killed after emitting a few cache requests
        # from disappearing from the cache analysis.
        records = [record for record in records if not is_canonical(record)]
        status = guard.status if guard.status != "success" else "invalid_output"
        replacement = _censored_record(status, metadata, guard)
        if parse_error:
            replacement["parse_error"] = parse_error
        elif (
            guard.status == "success"
            and len(canonical) == 1
            and canonical[0].get("status") == "success"
            and not guard.measurement_end_observed
        ):
            replacement["parse_error"] = (
                "successful worker omitted the final retained-RSS handshake"
            )
        elif len(canonical) > 1:
            replacement["parse_error"] = f"worker produced {len(canonical)} canonical outcomes"
        elif guard.status == "success":
            replacement["parse_error"] = "worker produced no canonical outcome"
        records.append(replacement)
    for record in records:
        record["whole_worker_peak_rss_bytes"] = guard.peak_rss_bytes
        record["peak_rss_bytes"] = (
            guard.measurement_peak_rss_bytes
            if guard.measurement_peak_rss_bytes is not None
            else guard.peak_rss_bytes
        )
        baseline = record.get("baseline_rss_bytes")
        record["peak_rss_delta_bytes"] = (
            max(0, record["peak_rss_bytes"] - baseline)
            if isinstance(baseline, int) and guard.baseline_ready_observed
            else None
        )
        record["baseline_ready_observed"] = guard.baseline_ready_observed
        record["measurement_end_observed"] = guard.measurement_end_observed
        record["measurement_end_rss_bytes"] = guard.measurement_end_rss_bytes
        record["measurement_completed_monotonic_ns"] = guard.measurement_completed_monotonic_ns
        record["config_hash"] = metadata.get("config_hash")
        record["variant_manifest_sha256"] = variant.manifest_sha256
        record["steal_ticks"] = steal_delta
        record["steal_fraction"] = steal_fraction
        record["steal_flagged"] = steal_flagged
        record["cpu_affinity"] = affinity
        for field in (
            "workload_fingerprint",
            "expected_request_count",
            "workload_class",
            "cache_limit_bytes",
            "compiler_threads",
            "family",
            "bound",
            "steal_rerun_of_block",
        ):
            if metadata.get(field) is not None:
                record.setdefault(field, metadata[field])
    guard_meta = dict(metadata)
    guard_meta["variant_manifest_sha256"] = variant.manifest_sha256
    records.append(_guard_record(guard, guard_meta))
    records[-1]["steal_ticks"] = steal_delta
    records[-1]["steal_fraction"] = steal_fraction
    records[-1]["steal_flagged"] = steal_flagged
    records[-1]["cpu_affinity"] = affinity
    append_records(raw_path, records)
    return records
