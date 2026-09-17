"""Orchestrate validation, pilot, freezing, and authoritative measurements."""

from __future__ import annotations

import copy
import json
import math
import os
import platform
import random
import shutil
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Tuple

from .analysis import percentile, precision_reached
from .cache_workload import CacheRequest, exact_repeat_stream, generate_stream, stream_fingerprint
from .config import (
    ConfigError,
    canonical_json,
    config_hash,
    load_config,
    load_json,
    profiling_root,
    repo_root,
    resolve_path,
    sha256_file,
    validate_config,
    validate_qualification_structure,
    write_json_atomic,
)
from .dataset import DatasetError, bfcl_trace_specs, verify_bfcl_snapshot
from .environment import (
    available_cpu_ids,
    capture_environment,
    machine_identity_fingerprint,
    perf_preflight,
    physical_cpu_ids,
    require_machine_identity,
    stable_machine_identity,
)
from .measurement import initialize_jsonl, run_worker
from .process_guard import watchdog_self_test
from .repetition_workload import RepetitionCase, case_fingerprint, cases_from_config, make_case
from .tokenizer_snapshot import load_hf_tokenizer, verify_snapshot
from .validation import RecordValidationError, assert_signature_expected, compare_signatures
from .variants import (
    Variant,
    VariantError,
    load_variants,
    manifest_hashes,
    verify_dependency_environment,
    verify_import,
)
from .variants import EXPECTED_BUILD_CONFIG


class SuiteError(RuntimeError):
    pass


def _log(message: str) -> None:
    """Operator progress line on stderr; never part of any evidence file."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[xgrammar-profile {timestamp}] {message}", file=sys.stderr, flush=True)


CACHE_VARIANTS = {
    "rule-off": "no-rule-cache",
    "intra-only": "production-profile",
    "full": "production-profile",
}
REPETITION_VARIANTS = {
    "production": "production-profile",
    "no-repeat-compression": "no-repeat-compression",
}


def _update_terminal_censor(previous: int, outcome: str | None, *, measured: bool) -> int:
    """Count only consecutive measured timeout/RSS outcomes."""
    if not measured:
        return previous
    return previous + 1 if outcome in {"timeout", "rss_limit"} else 0


def timestamped_dir(results_root: Path, prefix: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = results_root / f"{prefix}-{timestamp}"
    suffix = 1
    while candidate.exists():
        candidate = results_root / f"{prefix}-{timestamp}-{suffix}"
        suffix += 1
    return candidate


def create_run_dir(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise SuiteError(f"refusing existing run directory: {path}") from exc
    (path / "raw").mkdir()
    (path / "jobs").mkdir()


def _tracked_tree_clean() -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=normal"],
        cwd=repo_root(),
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0 and not result.stdout.strip()


def _git_head() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root(), text=True, capture_output=True, check=False
    )
    if result.returncode != 0:
        raise SuiteError(f"cannot identify source revision: {result.stderr.strip()}")
    return result.stdout.strip()


def resolve_assets(
    config: Mapping[str, Any], *, variant_root: Path | None = None, authoritative: bool = False
) -> tuple[Path, Path, Path | None, Path | None, Dict[str, Variant]]:
    snapshot = resolve_path(config["tokenizer"]["snapshot_dir"])
    manifest = verify_snapshot(snapshot, expected_revision=config["tokenizer"]["revision"])
    tokenizer_manifest = snapshot / "manifest.json"
    root = variant_root.resolve() if variant_root else resolve_path(config["variants"]["root"])
    variants = load_variants(
        root,
        config["variants"]["required"],
        authoritative=authoritative,
        expected_source_commit=config.get("source_head") or _git_head(),
    )
    verify_dependency_environment(variants, sys.executable)
    bfcl_dir: Path | None = None
    bfcl_manifest_path: Path | None = None
    if config.get("bfcl", {}).get("enabled"):
        bfcl_dir = resolve_path(config["bfcl"]["snapshot_dir"])
        bfcl_manifest = verify_bfcl_snapshot(bfcl_dir)
        bfcl_manifest_path = bfcl_dir / "manifest.json"
        traces = bfcl_trace_specs(
            bfcl_dir, requests=int(config.get("bfcl", {}).get("trace_requests", 5))
        )
        traces_sha = __import__("hashlib").sha256(canonical_json(traces)).hexdigest()
        bfcl_validation = bfcl_manifest.get("validation", {})
        production_info = verify_import(variants["production-profile"], sys.executable)
        if (
            bfcl_validation.get("production_variant_manifest_sha256")
            != variants["production-profile"].manifest_sha256
            or bfcl_validation.get("tokenizer_manifest_sha256") != sha256_file(tokenizer_manifest)
            or bfcl_validation.get("build_config") != production_info.get("profiling_build_config")
        ):
            raise SuiteError(
                "BFCL support validation provenance differs from the selected "
                "production-profile/tokenizer"
            )
        if authoritative:
            if config.get("bfcl_manifest_sha256") != sha256_file(bfcl_manifest_path):
                raise SuiteError("BFCL manifest differs from the frozen configuration")
            if config.get("bfcl_revision") != bfcl_manifest.get("revision"):
                raise SuiteError("BFCL immutable revision differs from the frozen configuration")
            if config.get("bfcl_traces_sha256") != traces_sha:
                raise SuiteError("prepared BFCL traces differ from the frozen configuration")
    if authoritative:
        expected_tokenizer = config.get("tokenizer_manifest_sha256")
        if expected_tokenizer != sha256_file(tokenizer_manifest):
            raise SuiteError("tokenizer manifest differs from the frozen configuration")
        expected_variants = config.get("variant_manifest_hashes")
        if manifest_hashes(variants) != expected_variants:
            raise SuiteError("one or more build manifests differ from the frozen configuration")
        qualification = config.get("qualification")
        if not isinstance(qualification, dict) or qualification.get("passed") is not True:
            raise SuiteError("frozen config lacks passed correctness qualification evidence")
        if qualification.get("variant_manifest_hashes") != manifest_hashes(variants):
            raise SuiteError("qualification evidence does not match selected variants")
        if qualification.get("tokenizer_manifest_sha256") != sha256_file(tokenizer_manifest):
            raise SuiteError("qualification evidence does not match tokenizer snapshot")
        canonical_qualification_sha = (
            __import__("hashlib").sha256(canonical_json(qualification)).hexdigest()
        )
        if config.get("qualification_canonical_sha256") != canonical_qualification_sha:
            raise SuiteError("embedded qualification evidence hash does not match frozen config")
    del manifest
    return snapshot, tokenizer_manifest, bfcl_dir, bfcl_manifest_path, variants


def _cache_case_id(tool_count: int, reuse: float) -> str:
    return f"cache-tools{tool_count}-seen{int(round(reuse * 100)):02d}"


def _stream_job(
    config: Mapping[str, Any],
    stream: Iterable[CacheRequest],
    *,
    case_id: str,
    block_id: int,
    measured: bool,
    arm: str,
    variant: Variant,
    snapshot: Path,
    validate_semantics: bool = False,
    tokenizer: Any | None = None,
    cache_limit_bytes: int | None = None,
    compiler_threads: int | None = None,
    workload_class: str = "controlled-primary",
    cpu_affinity: List[int] | None = None,
    semantic_request_indices: List[int] | None = None,
) -> Dict[str, Any]:
    requests = [
        request.to_dict() if hasattr(request, "to_dict") else dict(request) for request in stream
    ]
    workload_fingerprint = stream_fingerprint(requests)
    if validate_semantics:
        if tokenizer is None:
            raise SuiteError("semantic cache validation requires the offline HF tokenizer")
        for request in requests:
            function_name = request["tools"][0]["function"]["name"]
            invalid_text = request["validation_text"].replace(
                function_name, "profile_missing_tool", 1
            )
            request["invalid_validation_text"] = invalid_text
            request["validation_token_ids"] = list(
                tokenizer.encode(request["validation_text"], add_special_tokens=False)
            )
            request["invalid_validation_token_ids"] = list(
                tokenizer.encode(invalid_text, add_special_tokens=False)
            )
    return {
        "case_id": case_id,
        "block_id": block_id,
        "measured": measured,
        "variant": variant.name,
        "arm": arm,
        "tokenizer_snapshot": str(snapshot),
        "compiler_threads": (
            compiler_threads
            if compiler_threads is not None
            else config["cache"]["compiler_threads"]
        ),
        "cache_limit_bytes": (
            cache_limit_bytes
            if cache_limit_bytes is not None
            else config["cache"]["cache_limit_bytes"]
        ),
        "requests": requests,
        "workload_fingerprint": workload_fingerprint,
        "expected_request_count": len(requests),
        "validate_semantics": validate_semantics,
        "semantic_request_indices": semantic_request_indices,
        "workload_class": workload_class,
        "cpu_affinity": cpu_affinity,
    }


def _repetition_job(
    case: RepetitionCase,
    *,
    block_id: int,
    measured: bool,
    arm: str,
    variant: Variant,
    snapshot: Path,
    validate_semantics: bool = False,
    tokenizer: Any | None = None,
    force_structure_snapshot: bool = False,
    cpu_affinity: List[int] | None = None,
) -> Dict[str, Any]:
    validation_token_ids: Dict[str, List[int]] = {}
    if validate_semantics:
        if tokenizer is None:
            raise SuiteError("semantic repetition validation requires the offline HF tokenizer")
        validation_token_ids = {
            name: list(tokenizer.encode(text, add_special_tokens=False))
            for name, text in case.acceptance_examples.items()
        }
    return {
        **case.to_dict(),
        "workload_fingerprint": case_fingerprint(case),
        "block_id": block_id,
        "measured": measured,
        "variant": variant.name,
        "arm": arm,
        "tokenizer_snapshot": str(snapshot),
        "validate_semantics": validate_semantics,
        "validation_token_ids": validation_token_ids,
        "force_structure_snapshot": force_structure_snapshot,
        "cpu_affinity": cpu_affinity,
    }


def _metadata(
    job: Mapping[str, Any], config: Mapping[str, Any], variant: Variant
) -> Dict[str, Any]:
    return {
        "experiment": "cache" if "requests" in job else "repetition",
        "case_id": job["case_id"],
        "block_id": job["block_id"],
        "measured": job["measured"],
        "variant": variant.name,
        "arm": job["arm"],
        "config_hash": config.get("config_hash"),
        "variant_manifest_sha256": variant.manifest_sha256,
        "steal_time_threshold": config.get("steal_time_threshold"),
        "cpu_affinity": job.get("cpu_affinity"),
        "workload_class": job.get("workload_class"),
        "cache_limit_bytes": job.get("cache_limit_bytes"),
        "compiler_threads": job.get("compiler_threads"),
        "workload_fingerprint": job.get("workload_fingerprint"),
        "expected_request_count": job.get("expected_request_count"),
        "family": job.get("family"),
        "bound": job.get("bound"),
        "steal_rerun_of_block": job.get("steal_rerun_of_block"),
    }


def _dispatch(
    run_dir: Path,
    raw_path: Path,
    config: Mapping[str, Any],
    variant: Variant,
    job: Dict[str, Any],
    sequence: int,
) -> List[Dict[str, Any]]:
    experiment = "cache" if "requests" in job else "repetition"
    worker = (
        profiling_root()
        / "workers"
        / ("compile_stream.py" if experiment == "cache" else "compile_repetition.py")
    )
    safe_case = job["case_id"].replace("/", "_")
    job_path = run_dir / "jobs" / f"{sequence:06d}-{safe_case}-{job['arm']}.json"
    records = run_worker(
        worker=worker,
        job=job,
        job_path=job_path,
        raw_path=raw_path,
        variant=variant,
        execution=dict(config["execution"]),
        metadata=_metadata(job, config, variant),
    )
    canonical_statuses = [
        record.get("status")
        for record in records
        if record.get("record_type") in {"sample", "stream-summary"}
    ]
    guard = next((record for record in records if record.get("record_type") == "guard"), {})
    _log(
        f"job {sequence:06d} {job['case_id']} arm={job['arm']} block={job['block_id']} "
        f"measured={job['measured']} status={canonical_statuses[-1] if canonical_statuses else None} "
        f"wall={(guard.get('wall_time_ns') or 0) / 1e9:.1f}s "
        f"steal_flagged={bool(guard.get('steal_flagged'))}"
    )
    fatal = [
        record
        for record in records
        if record.get("record_type") in {"sample", "stream-summary"}
        and record.get("status") in {"program_error", "invalid_output", "kernel_termination"}
    ]
    if fatal:
        first = fatal[0]
        raise SuiteError(
            f"fatal worker outcome for {job['case_id']}/{job['arm']}: "
            f"{first['status']} {first.get('error', first.get('parse_error', ''))}"
        )
    prebaseline_censors = [
        record
        for record in records
        if record.get("record_type") in {"sample", "stream-summary"}
        and record.get("measured") is True
        and record.get("status") in {"timeout", "rss_limit"}
        and record.get("baseline_ready_observed") is not True
    ]
    if prebaseline_censors:
        first = prebaseline_censors[0]
        raise SuiteError(
            f"worker {first['status']} before the RSS baseline handshake for "
            f"{job['case_id']}/{job['arm']}; this is an infrastructure failure, "
            "not an algorithm censor"
        )
    missing_end_samples = [
        record
        for record in records
        if record.get("record_type") in {"sample", "stream-summary"}
        and record.get("status") == "success"
        and record.get("measurement_end_observed") is not True
    ]
    if missing_end_samples:
        raise SuiteError(
            f"successful worker lacked the final retained-RSS handshake for "
            f"{job['case_id']}/{job['arm']}"
        )
    return records


def execute_suite(
    config: Dict[str, Any],
    run_dir: Path,
    variants: Dict[str, Variant],
    snapshot: Path,
    *,
    include_warmups: bool = True,
) -> List[Dict[str, Any]]:
    raw_path = run_dir / "raw" / "results.jsonl"
    initialize_jsonl(raw_path)
    all_records: List[Dict[str, Any]] = []
    steal_reruns: List[Tuple[Dict[str, Any], Variant]] = []
    sequence = 0
    sampling = config["sampling"]
    warmups = int(sampling["warmup_blocks"]) if include_warmups else 0
    minimum = int(sampling["minimum_blocks"])
    maximum = int(sampling["maximum_blocks"])
    rng = random.Random(int(config["cache"]["seed"]))
    configured_physical = config.get("execution", {}).get("physical_cpu_ids")
    physical_ids = (
        [int(cpu) for cpu in configured_physical]
        if isinstance(configured_physical, list) and configured_physical
        else physical_cpu_ids(preferred=config.get("execution", {}).get("primary_cpu"))
    )
    primary_cpu = int(config.get("execution", {}).get("primary_cpu", physical_ids[0]))
    if primary_cpu not in physical_ids:
        raise SuiteError(
            f"primary CPU {primary_cpu} is absent from distinct physical CPU list {physical_ids}"
        )

    cache_cells: List[Dict[str, Any]] = []
    for tool_count in config["cache"]["tools_per_request"]:
        for reuse in config["cache"]["seen_before_fractions"]:
            cache_cells.append(
                {
                    "case_id": _cache_case_id(int(tool_count), float(reuse)),
                    "kind": "synthetic",
                    "class": "controlled-primary",
                    "tools": int(tool_count),
                    "reuse": float(reuse),
                    "requests": int(config["cache"]["requests_per_stream"]),
                    "cache_limit_bytes": int(config["cache"]["cache_limit_bytes"]),
                    "compiler_threads": int(config["cache"]["compiler_threads"]),
                    "cpu_affinity": [primary_cpu],
                }
            )
    if config["cache"].get("include_500_tool_subset"):
        cache_cells.append(
            {
                "case_id": "cache-tools500-seen90",
                "kind": "synthetic",
                "class": "controlled-500-tool",
                "tools": 500,
                "reuse": 0.9,
                "requests": 5,
                "cache_limit_bytes": int(config["cache"]["cache_limit_bytes"]),
                "compiler_threads": int(config["cache"]["compiler_threads"]),
                "cpu_affinity": [primary_cpu],
            }
        )
    secondary = config["cache"].get("secondary", {})
    if secondary.get("enabled"):
        exact = secondary["exact_repeat"]
        cache_cells.append(
            {
                "case_id": f"cache-exact-repeat-tools{int(exact['tools'])}",
                "kind": "exact-repeat",
                "class": "exact-repeat-control",
                "tools": int(exact["tools"]),
                "reuse": 1.0,
                "requests": int(exact["requests"]),
                "cache_limit_bytes": int(config["cache"]["cache_limit_bytes"]),
                "compiler_threads": 1,
                "cpu_affinity": [primary_cpu],
            }
        )
        for budget in secondary.get("budget_sweep_bytes", []):
            cache_cells.append(
                {
                    "case_id": f"cache-budget{int(budget)}-tools100-seen90",
                    "kind": "synthetic",
                    "class": "budget-sweep",
                    "tools": 100,
                    "reuse": 0.9,
                    "requests": int(config["cache"]["requests_per_stream"]),
                    "cache_limit_bytes": int(budget),
                    "compiler_threads": 1,
                    "cpu_affinity": [primary_cpu],
                }
            )
        cap = min(len(physical_ids), 8)
        configured_threads = secondary.get(
            "resolved_thread_sweep", secondary.get("thread_sweep", [])
        )
        thread_values = [
            cap if value == "physical-cap-8" else int(value) for value in configured_threads
        ]
        for threads in sorted(set(thread_values)):
            if threads < 1 or threads > len(physical_ids):
                raise SuiteError(
                    f"thread sweep requests {threads} physical CPUs, available distinct cores: {physical_ids}"
                )
            cache_cells.append(
                {
                    "case_id": f"cache-threads{threads}-tools100-seen50",
                    "kind": "synthetic",
                    "class": "thread-sweep",
                    "tools": 100,
                    "reuse": 0.5,
                    "requests": int(config["cache"]["requests_per_stream"]),
                    "cache_limit_bytes": int(config["cache"]["cache_limit_bytes"]),
                    "compiler_threads": threads,
                    "cpu_affinity": physical_ids[:threads],
                }
            )
    if config.get("bfcl", {}).get("enabled"):
        bfcl_dir = resolve_path(config["bfcl"]["snapshot_dir"])
        for trace in bfcl_trace_specs(
            bfcl_dir, requests=int(config["bfcl"].get("trace_requests", 5))
        ):
            cache_cells.append(
                {
                    "case_id": trace["label"],
                    "kind": "fixed",
                    "class": "bfcl-validation",
                    "tools": trace["tools_per_request"],
                    "reuse": trace["reuse"],
                    "requests": len(trace["requests"]),
                    "fixed_requests": trace["requests"],
                    "cache_limit_bytes": int(config["cache"]["cache_limit_bytes"]),
                    "compiler_threads": 1,
                    "cpu_affinity": [primary_cpu],
                }
            )
    _log(
        f"matrix: {len(cache_cells)} cache cells x 3 arms, "
        f"{len(cases_from_config(config))} repetition cases x 2 arms, "
        f"warmup={warmups} min_blocks={minimum} max_blocks={maximum}"
    )
    for cell in cache_cells:
        tool_count = cell["tools"]
        reuse = cell["reuse"]
        request_count = cell["requests"]
        case_id = cell["case_id"]
        terminal: Dict[str, int] = {arm: 0 for arm in CACHE_VARIANTS}
        success_counts: Dict[str, int] = {arm: 0 for arm in CACHE_VARIANTS}
        for position in range(warmups + maximum):
            measured = position >= warmups
            block_id = position - warmups
            seed = (
                int(config["cache"]["seed"])
                + tool_count * 100_003
                + int(reuse * 1000) * 101
                + max(block_id, -1) * 17
            )
            if cell["kind"] == "exact-repeat":
                stream = exact_repeat_stream(
                    tools_per_request=tool_count, requests=request_count, seed=seed
                )
            elif cell["kind"] == "fixed":
                stream = cell["fixed_requests"]
            else:
                stream = generate_stream(
                    tools_per_request=tool_count,
                    seen_before_fraction=reuse,
                    requests=request_count,
                    seed=seed,
                )
            arms = list(CACHE_VARIANTS)
            rng.shuffle(arms)
            for arm in arms:
                if measured and terminal[arm] >= 3:
                    continue
                variant = variants[CACHE_VARIANTS[arm]]
                job = _stream_job(
                    config,
                    stream,
                    case_id=case_id,
                    block_id=block_id,
                    measured=measured,
                    arm=arm,
                    variant=variant,
                    snapshot=snapshot,
                    cache_limit_bytes=cell["cache_limit_bytes"],
                    compiler_threads=cell["compiler_threads"],
                    workload_class=cell["class"],
                    cpu_affinity=cell["cpu_affinity"],
                )
                records = _dispatch(run_dir, raw_path, config, variant, job, sequence)
                sequence += 1
                all_records.extend(records)
                if measured and any(record.get("steal_flagged") for record in records):
                    rerun_job = copy.deepcopy(job)
                    rerun_job["measured"] = False
                    rerun_job["steal_rerun_of_block"] = block_id
                    rerun_job["workload_class"] = (
                        job.get("workload_class", "cache") + "-steal-rerun"
                    )
                    steal_reruns.append((rerun_job, variant))
                outcomes = [
                    record["status"]
                    for record in records
                    if record["record_type"] == "stream-summary"
                ]
                if not outcomes:
                    outcomes = [
                        record["status"] for record in records if record["record_type"] == "sample"
                    ]
                outcome = outcomes[-1] if outcomes else None
                if measured and outcome == "success":
                    success_counts[arm] += 1
                terminal[arm] = _update_terminal_censor(terminal[arm], outcome, measured=measured)
            completed = block_id + 1
            if measured:
                if any(value >= 3 for value in terminal.values()) and all(
                    terminal[arm] >= 3 or success_counts[arm] >= minimum for arm in CACHE_VARIANTS
                ):
                    break
                if any(value > 0 for value in terminal.values()):
                    # Obtain the full three measured confirmations before declaring an
                    # arm censored; do not let precision in the surviving arms stop it.
                    continue
                if completed >= minimum and precision_reached(
                    all_records,
                    experiment="cache",
                    case_id=case_id,
                    minimum_blocks=minimum,
                    relative_half_width=float(sampling["relative_ci_half_width"]),
                    resamples=int(sampling["bootstrap_resamples"]),
                    seed=int(sampling["bootstrap_seed"]),
                ):
                    break
        _log(
            f"cell {case_id} done: measured_blocks={block_id + 1} "
            f"successes={success_counts} terminal_censors={terminal}"
        )

    for case in cases_from_config(config):
        terminal = {arm: 0 for arm in REPETITION_VARIANTS}
        success_counts = {arm: 0 for arm in REPETITION_VARIANTS}
        for position in range(warmups + maximum):
            measured = position >= warmups
            block_id = position - warmups
            arms = list(REPETITION_VARIANTS)
            rng.shuffle(arms)
            for arm in arms:
                if measured and terminal[arm] >= 3:
                    continue
                variant = variants[REPETITION_VARIANTS[arm]]
                job = _repetition_job(
                    case,
                    block_id=block_id,
                    measured=measured,
                    arm=arm,
                    variant=variant,
                    snapshot=snapshot,
                    cpu_affinity=[primary_cpu],
                )
                records = _dispatch(run_dir, raw_path, config, variant, job, sequence)
                sequence += 1
                all_records.extend(records)
                if measured and any(record.get("steal_flagged") for record in records):
                    rerun_job = copy.deepcopy(job)
                    rerun_job["measured"] = False
                    rerun_job["steal_rerun_of_block"] = block_id
                    steal_reruns.append((rerun_job, variant))
                outcomes = [
                    record["status"] for record in records if record["record_type"] == "sample"
                ]
                outcome = outcomes[0] if outcomes else None
                if measured and outcome == "success":
                    success_counts[arm] += 1
                terminal[arm] = _update_terminal_censor(terminal[arm], outcome, measured=measured)
            completed = block_id + 1
            if measured:
                if any(value >= 3 for value in terminal.values()) and all(
                    terminal[arm] >= 3 or success_counts[arm] >= minimum
                    for arm in REPETITION_VARIANTS
                ):
                    break
                if any(value > 0 for value in terminal.values()):
                    continue
                if completed >= minimum and precision_reached(
                    all_records,
                    experiment="repetition",
                    case_id=case.case_id,
                    minimum_blocks=minimum,
                    relative_half_width=float(sampling["relative_ci_half_width"]),
                    resamples=int(sampling["bootstrap_resamples"]),
                    seed=int(sampling["bootstrap_seed"]),
                ):
                    break
        _log(
            f"cell {case.case_id} done: measured_blocks={block_id + 1} "
            f"successes={success_counts} terminal_censors={terminal}"
        )
    _log(f"timing matrix done; steal reruns queued: {len(steal_reruns)}")
    # A noisy sample is retained as measured evidence and rerun exactly once at the end.
    # Reruns are diagnostic (`measured=false`) so they cannot silently replace observations.
    for job, variant in steal_reruns:
        records = _dispatch(run_dir, raw_path, config, variant, job, sequence)
        sequence += 1
        all_records.extend(records)
    return all_records


def execute_diagnostics(
    config: Dict[str, Any], run_dir: Path, variants: Dict[str, Variant], snapshot: Path
) -> List[Dict[str, Any]]:
    """Run stats-enabled representative cases after all authoritative timing work."""
    if "production-diagnostic" not in variants or "no-rule-cache-diagnostic" not in variants:
        raise SuiteError("diagnostic variants are required by the frozen authoritative config")
    raw_path = run_dir / "raw" / "diagnostics.jsonl"
    initialize_jsonl(raw_path)
    records: List[Dict[str, Any]] = []
    sequence = 10_000_000
    _log("diagnostics starting (every diagnostic job must succeed)")
    primary_cpu = int(config["execution"]["primary_cpu"])
    tokenizer = load_hf_tokenizer(snapshot)

    def dispatch_required(variant: Variant, job: Dict[str, Any], *, record_type: str) -> None:
        nonlocal sequence
        produced = _dispatch(run_dir, raw_path, config, variant, job, sequence)
        sequence += 1
        records.extend(produced)
        canonical = [record for record in produced if record.get("record_type") == record_type]
        if len(canonical) != 1 or canonical[0].get("status") != "success":
            statuses = [record.get("status") for record in canonical]
            raise SuiteError(
                f"required diagnostic {job['case_id']}/{job['arm']} did not succeed: {statuses}"
            )

    stream = generate_stream(
        tools_per_request=100,
        seen_before_fraction=0.9,
        requests=20,
        seed=int(config["cache"]["seed"]) + 9900,
    )
    diagnostic_arms = {
        "rule-off": variants["no-rule-cache-diagnostic"],
        "intra-only": variants["production-diagnostic"],
        "full": variants["production-diagnostic"],
    }
    for arm, variant in diagnostic_arms.items():
        job = _stream_job(
            config,
            stream,
            case_id="diagnostic-cache-tools100-seen90",
            block_id=-2,
            measured=False,
            arm=arm,
            variant=variant,
            snapshot=snapshot,
            workload_class="diagnostic-representative",
            cpu_affinity=[primary_cpu],
            validate_semantics=True,
            tokenizer=tokenizer,
            semantic_request_indices=[len(stream) - 1],
        )
        dispatch_required(variant, job, record_type="stream-summary")
    for budget in (64 * 1024**2, 256 * 1024**2, 512 * 1024**2):
        variant = variants["production-diagnostic"]
        job = _stream_job(
            config,
            stream,
            case_id=f"diagnostic-cache-budget{budget}-tools100-seen90",
            block_id=-2,
            measured=False,
            arm="full",
            variant=variant,
            snapshot=snapshot,
            cache_limit_bytes=budget,
            workload_class="diagnostic-budget-sweep",
            cpu_affinity=[primary_cpu],
        )
        dispatch_required(variant, job, record_type="stream-summary")
    # Replay is deliberately isolated from stats-enabled structure inspection.  Both
    # replay arms use the ordinary timing builds, but these jobs remain unmeasured.
    replay_families = (
        "json-string",
        "json-array-primitive",
        "json-array-object",
        "json-array-minmax",
        "regex-range",
        "regex-exact",
        "regex-nonzero-min",
    )
    for family in replay_families:
        for bound in (127, 128, 129, 130):
            case = make_case(family, bound)
            for arm, variant_name in (
                ("production", "production-profile"),
                ("no-repeat-compression", "no-repeat-compression"),
            ):
                variant = variants[variant_name]
                job = _repetition_job(
                    case,
                    block_id=-3,
                    measured=False,
                    arm=arm,
                    variant=variant,
                    snapshot=snapshot,
                    validate_semantics=True,
                    tokenizer=tokenizer,
                    cpu_affinity=[primary_cpu],
                )
                dispatch_required(variant, job, record_type="sample")
    structure_families = (
        "json-string",
        "json-array-primitive",
        "json-array-object",
        "json-array-minmax",
        "regex-range",
        "regex-exact",
        "regex-nonzero-min",
    )
    structure_cases = [
        make_case(family, bound) for family in structure_families for bound in (127, 128, 129, 130)
    ]
    for case in structure_cases:
        for arm, variant_name in (
            ("production", "production-diagnostic"),
            ("no-repeat-compression", "no-repeat-compression"),
        ):
            variant = variants[variant_name]
            job = _repetition_job(
                case,
                block_id=-2,
                measured=False,
                arm=arm,
                variant=variant,
                snapshot=snapshot,
                force_structure_snapshot=True,
                cpu_affinity=[primary_cpu],
            )
            dispatch_required(variant, job, record_type="sample")
    # The very-large structure case is production-only because the uncompressed arm
    # may be intentionally infeasible.
    large_case = make_case("regex-range", 65536)
    diagnostic_variant = variants["production-diagnostic"]
    large_job = _repetition_job(
        large_case,
        block_id=-2,
        measured=False,
        arm="production",
        variant=diagnostic_variant,
        snapshot=snapshot,
        force_structure_snapshot=True,
        cpu_affinity=[primary_cpu],
    )
    dispatch_required(diagnostic_variant, large_job, record_type="sample")
    return records


def validate_variants(
    config: Dict[str, Any],
    output: Path,
    variants: Dict[str, Variant],
    snapshot: Path,
    bfcl_dir: Path | None = None,
) -> Dict[str, Any]:
    create_run_dir(output)
    raw = output / "raw" / "validation.jsonl"
    initialize_jsonl(raw)
    tokenizer = load_hf_tokenizer(snapshot)
    results: Dict[str, Any] = {
        "schema_version": 1,
        "passed": False,
        "source_commit": _git_head(),
        "release_commit": config["source"]["release_commit"],
        "config_hash": config_hash(config),
        "variant_manifest_hashes": manifest_hashes(variants),
        "tokenizer_manifest_sha256": sha256_file(snapshot / "manifest.json"),
        "imports": {},
        "cache_cases": {},
        "repetition_cases": {},
        "bfcl_cases": {},
        "errors": [],
    }
    for name, variant in variants.items():
        results["imports"][name] = verify_import(variant, sys.executable)
    sequence = 0
    try:
        stream = generate_stream(
            tools_per_request=4, seen_before_fraction=0.5, requests=3, seed=90127
        )
        cache_signatures: Dict[str, Any] = {}
        for arm in ("rule-off", "intra-only", "full"):
            variant = variants[CACHE_VARIANTS[arm]]
            job = _stream_job(
                config,
                stream,
                case_id="validation-cache",
                block_id=0,
                measured=False,
                arm=arm,
                variant=variant,
                snapshot=snapshot,
                validate_semantics=True,
                tokenizer=tokenizer,
            )
            records = _dispatch(output, raw, config, variant, job, sequence)
            sequence += 1
            summary = next(
                (
                    r
                    for r in records
                    if r["record_type"] == "stream-summary" and r["status"] == "success"
                ),
                None,
            )
            if summary is None or summary.get("semantic_signature") is None:
                raise RecordValidationError(
                    f"cache validation arm {arm} did not produce a signature"
                )
            cache_signatures[arm] = summary["semantic_signature"]
        for arm, signatures in cache_signatures.items():
            for request_index, signature in enumerate(signatures):
                valid = signature["valid"]
                invalid = signature["invalid"]
                assert_signature_expected(
                    valid, expected=True, context=f"cache {arm} request {request_index} valid"
                )
                assert_signature_expected(
                    invalid, expected=False, context=f"cache {arm} request {request_index} invalid"
                )
        compare_signatures(cache_signatures, context="cache validation stream")
        results["cache_cases"]["validation-cache"] = "passed"

        if config.get("bfcl", {}).get("enabled"):
            if bfcl_dir is None:
                raise RecordValidationError(
                    "BFCL validation is enabled but no verified snapshot was supplied"
                )
            bfcl_manifest = verify_bfcl_snapshot(bfcl_dir)
            bfcl_traces = bfcl_trace_specs(
                bfcl_dir, requests=int(config["bfcl"].get("trace_requests", 5))
            )
            results["bfcl_manifest_sha256"] = sha256_file(bfcl_dir / "manifest.json")
            results["bfcl_revision"] = bfcl_manifest["revision"]
            results["bfcl_traces_sha256"] = (
                __import__("hashlib").sha256(canonical_json(bfcl_traces)).hexdigest()
            )
            results["bfcl_production_variant_manifest_sha256"] = bfcl_manifest["validation"][
                "production_variant_manifest_sha256"
            ]
            results["bfcl_tokenizer_manifest_sha256"] = bfcl_manifest["validation"][
                "tokenizer_manifest_sha256"
            ]
            results["bfcl_validation_build_config"] = bfcl_manifest["validation"]["build_config"]
            for trace in bfcl_traces:
                for arm in ("rule-off", "intra-only", "full"):
                    variant = variants[CACHE_VARIANTS[arm]]
                    job = _stream_job(
                        config,
                        trace["requests"],
                        case_id=f"qualification-{trace['label']}",
                        block_id=0,
                        measured=False,
                        arm=arm,
                        variant=variant,
                        snapshot=snapshot,
                        workload_class="bfcl-qualification",
                    )
                    produced = _dispatch(output, raw, config, variant, job, sequence)
                    sequence += 1
                    summary = next(
                        (
                            record
                            for record in produced
                            if record["record_type"] == "stream-summary"
                            and record["status"] == "success"
                        ),
                        None,
                    )
                    if summary is None:
                        raise RecordValidationError(
                            f"BFCL trace {trace['label']} failed to compile under {arm}"
                        )
                results["bfcl_cases"][trace["label"]] = "passed"

        validation_cases: List[RepetitionCase] = []
        for bound in (127, 128, 129):
            for family in (
                "json-string",
                "json-array-primitive",
                "json-array-object",
                "json-array-minmax",
                "regex-range",
                "regex-exact",
                "regex-nonzero-min",
            ):
                validation_cases.append(make_case(family, bound))
        for family in (
            "json-string",
            "json-array-primitive",
            "json-array-object",
            "json-array-minmax",
            "regex-range",
            "regex-exact",
            "regex-nonzero-min",
        ):
            validation_cases.append(make_case(family, 130))
        # Exhaust every string over {a,c} through max+1 for small `[a]` ranges.
        import itertools

        for bound in (1, 2, 3, 4):
            examples: Dict[str, str] = {}
            expected: Dict[str, bool] = {}
            for length in range(bound + 2):
                for chars in itertools.product("ac", repeat=length):
                    text = "".join(chars)
                    name = f"len{length}-bits{''.join(chars) or 'empty'}"
                    examples[name] = text
                    expected[name] = length <= bound and "c" not in text
            validation_cases.append(
                RepetitionCase(
                    f"property-exhaustive-a-0-{bound}",
                    "regex-property",
                    bound,
                    "regex",
                    f"[a]{{0,{bound}}}",
                    examples,
                    expected,
                )
            )
        property_rng = random.Random(8128)
        for index in range(10):
            upper = property_rng.randint(2, 12)
            lower = property_rng.randint(0, upper)
            body = "[ab]" if index % 2 else "[a]"
            examples = {
                "min_minus_one": "a" * max(0, lower - 1),
                "min": "a" * lower,
                "max": "a" * upper,
                "max_plus_one": "a" * (upper + 1),
                "wrong_body": "c" * max(1, lower),
            }
            expected = {
                "min_minus_one": lower == 0,
                "min": True,
                "max": True,
                "max_plus_one": False,
                "wrong_body": False,
            }
            validation_cases.append(
                RepetitionCase(
                    f"property-random-{index}-{lower}-{upper}",
                    "regex-property",
                    upper,
                    "regex",
                    f"{body}{{{lower},{upper}}}",
                    examples,
                    expected,
                )
            )
        for case in validation_cases:
            signatures: Dict[str, Any] = {}
            for arm in ("production", "no-repeat-compression"):
                variant = variants[REPETITION_VARIANTS[arm]]
                job = _repetition_job(
                    case,
                    block_id=0,
                    measured=False,
                    arm=arm,
                    variant=variant,
                    snapshot=snapshot,
                    validate_semantics=True,
                    tokenizer=tokenizer,
                )
                records = _dispatch(output, raw, config, variant, job, sequence)
                sequence += 1
                sample = next(
                    (
                        r
                        for r in records
                        if r["record_type"] == "sample" and r["status"] == "success"
                    ),
                    None,
                )
                if sample is None or sample.get("semantic_signature") is None:
                    raise RecordValidationError(
                        f"repetition validation arm {arm} failed for {case.case_id}"
                    )
                signatures[arm] = sample["semantic_signature"]
                for name, expected in case.expected_acceptance.items():
                    signature = sample["semantic_signature"][name]
                    assert_signature_expected(
                        signature, expected=expected, context=f"{arm} {case.case_id}/{name}"
                    )
            compare_signatures(signatures, context=case.case_id)
            results["repetition_cases"][case.case_id] = "passed"
        results["passed"] = True
    except (RecordValidationError, VariantError, Exception) as exc:
        results["errors"].append(f"{type(exc).__name__}: {exc}")
        write_json_atomic(output / "validation.json", results)
        raise SuiteError(results["errors"][-1]) from exc
    results["record_count"] = sum(
        1 for line in raw.read_text(encoding="utf-8").splitlines() if line.strip()
    )
    write_json_atomic(output / "validation.json", results)
    return results


def _coefficient_of_variation(values: List[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = statistics.mean(values)
    return statistics.stdev(values) / mean if mean else None


def run_pilot(
    config: Dict[str, Any],
    run_dir: Path,
    variants: Dict[str, Variant],
    snapshot: Path,
    *,
    quick: bool = False,
    qualification_path: Path | None = None,
) -> Dict[str, Any]:
    config = copy.deepcopy(config)
    resolved_variant_root = next(iter(variants.values())).directory.parent
    config["variants"]["root"] = str(resolved_variant_root)
    requested_cpu_text = os.environ.get("XGRAMMAR_PROFILE_CPU")
    if requested_cpu_text is not None:
        try:
            requested_cpu = int(requested_cpu_text)
        except ValueError as exc:
            raise SuiteError("XGRAMMAR_PROFILE_CPU must be an integer CPU id") from exc
    else:
        requested_cpu = available_cpu_ids()[0]
    physical_ids = physical_cpu_ids(preferred=requested_cpu)
    machine_identity = stable_machine_identity()
    config["execution"]["primary_cpu"] = requested_cpu
    config["execution"]["physical_cpu_ids"] = physical_ids
    qualification: Dict[str, Any] | None = None
    qualification_sha256: str | None = None
    authoritative_config: Dict[str, Any] | None = None
    if not quick:
        if platform.system() != "Linux":
            raise SuiteError("full freeze-eligible pilot requires Linux; use --quick on macOS")
        if not _tracked_tree_clean():
            raise SuiteError(
                "full pilot requires a clean tracked and untracked source tree; "
                "use --quick only for development plumbing"
            )
        if qualification_path is None:
            raise SuiteError(
                "full pilot requires --qualification from the completed validation workflow"
            )
        qualification = load_json(qualification_path)
        if qualification.get("passed") is not True:
            raise SuiteError("qualification evidence is not marked passed")
        try:
            validate_qualification_structure(qualification)
        except ConfigError as exc:
            raise SuiteError(str(exc)) from exc
        authoritative_config = load_config(
            resolve_path(config.get("authoritative_config", "profiling/configs/v0.2.7.json"))
        )
        expected_names = set(authoritative_config["variants"]["required"])
        if expected_names != set(EXPECTED_BUILD_CONFIG):
            raise SuiteError("authoritative config must name exactly the six reviewed variants")
        authoritative_variants = load_variants(
            resolved_variant_root,
            authoritative_config["variants"]["required"],
            expected_source_commit=_git_head(),
        )
        verify_dependency_environment(authoritative_variants, sys.executable)
        selected_hashes = manifest_hashes(authoritative_variants)
        qualified_hashes = qualification.get("variant_manifest_hashes")
        if qualified_hashes != selected_hashes:
            raise SuiteError("qualification variant hashes do not match pilot variants")
        if qualification.get("source_commit") != _git_head():
            raise SuiteError("qualification source_commit does not match current HEAD")
        if qualification.get("release_commit") != config["source"]["release_commit"]:
            raise SuiteError("qualification release commit is not reviewed v0.2.7")
        if qualification.get("config_hash") != config_hash(authoritative_config):
            raise SuiteError("qualification was not produced from the authoritative config")
        if authoritative_config.get("bfcl", {}).get("enabled"):
            qualified_bfcl_dir = resolve_path(authoritative_config["bfcl"]["snapshot_dir"])
            qualified_bfcl_manifest = verify_bfcl_snapshot(qualified_bfcl_dir)
            qualified_bfcl_traces = bfcl_trace_specs(
                qualified_bfcl_dir,
                requests=int(authoritative_config["bfcl"].get("trace_requests", 5)),
            )
            qualified_trace_sha = (
                __import__("hashlib").sha256(canonical_json(qualified_bfcl_traces)).hexdigest()
            )
            if (
                qualification.get("bfcl_manifest_sha256")
                != sha256_file(qualified_bfcl_dir / "manifest.json")
                or qualification.get("bfcl_revision") != qualified_bfcl_manifest["revision"]
                or qualification.get("bfcl_traces_sha256") != qualified_trace_sha
                or qualification.get("bfcl_production_variant_manifest_sha256")
                != authoritative_variants["production-profile"].manifest_sha256
                or qualification.get("bfcl_tokenizer_manifest_sha256")
                != sha256_file(snapshot / "manifest.json")
                or qualification.get("bfcl_validation_build_config")
                != verify_import(authoritative_variants["production-profile"], sys.executable).get(
                    "profiling_build_config"
                )
            ):
                raise SuiteError(
                    "qualification BFCL evidence does not match prepared frozen traces"
                )
        tokenizer_hash = sha256_file(snapshot / "manifest.json")
        if qualification.get("tokenizer_manifest_sha256") != tokenizer_hash:
            raise SuiteError("qualification tokenizer hash does not match pilot snapshot")
        qualification_sha256 = sha256_file(qualification_path)
    create_run_dir(run_dir)
    write_json_atomic(run_dir / "resolved-config.json", config)
    write_json_atomic(run_dir / "environment.json", capture_environment())
    if qualification is not None:
        shutil.copyfile(qualification_path, run_dir / "qualification.json")
        if sha256_file(run_dir / "qualification.json") != qualification_sha256:
            raise SuiteError("qualification copy hash changed unexpectedly")
    watchdog = watchdog_self_test(sys.executable, workdir=repo_root())
    perf = perf_preflight()
    # The ordinary small pilot validates the complete orchestration path.
    records = execute_suite(config, run_dir, variants, snapshot)

    pilot_config = config.get("pilot", {})
    # Record a fixed, unmeasured 100-tool/90%-reuse curve at the proposed final stream
    # length.  The decision remains conservative (20) unless a future reviewed pilot
    # policy declares a pre-specified adjustment; the curve makes saturation auditable.
    proposed_stream_length = int((authoritative_config or config)["cache"]["requests_per_stream"])
    saturation_raw = run_dir / "raw" / "saturation.jsonl"
    initialize_jsonl(saturation_raw)
    saturation_stream = generate_stream(
        tools_per_request=100,
        seen_before_fraction=0.9,
        requests=proposed_stream_length,
        seed=779090,
    )
    saturation_variant = variants["production-profile"]
    saturation_job = _stream_job(
        config,
        saturation_stream,
        case_id="pilot-saturation-tools100-seen90",
        block_id=0,
        measured=False,
        arm="full",
        variant=saturation_variant,
        snapshot=snapshot,
        workload_class="pilot-saturation",
        cpu_affinity=[requested_cpu],
    )
    saturation_records = run_worker(
        worker=profiling_root() / "workers" / "compile_stream.py",
        job=saturation_job,
        job_path=run_dir / "jobs" / "saturation.json",
        raw_path=saturation_raw,
        variant=saturation_variant,
        execution=dict(config["execution"]),
        metadata=_metadata(saturation_job, config, saturation_variant),
    )
    saturation_summary = next(
        (record for record in saturation_records if record["record_type"] == "stream-summary"), None
    )
    saturation_samples = sorted(
        (
            record
            for record in saturation_records
            if record["record_type"] == "sample" and record.get("request_index") is not None
        ),
        key=lambda record: int(record["request_index"]),
    )
    saturation_ok = bool(
        saturation_summary
        and saturation_summary.get("status") == "success"
        and len(saturation_samples) == proposed_stream_length
        and all(record.get("status") == "success" for record in saturation_samples)
    )
    if saturation_summary and saturation_summary.get("status") in {
        "program_error",
        "invalid_output",
        "kernel_termination",
    }:
        raise SuiteError(f"pilot saturation worker failed: {saturation_summary.get('status')}")
    saturation_curve = [
        {
            "request_index": record["request_index"],
            "compile_time_ns": record.get("compile_time_ns"),
            "realized_seen_before_fraction": record.get("realized_seen_before_fraction"),
            "rule_cache_size_bytes": record.get("diagnostics", {}).get("rule_cache_size_bytes"),
            "grammar_cache_size_bytes": record.get("diagnostics", {}).get(
                "grammar_cache_size_bytes"
            ),
        }
        for record in saturation_samples
    ]

    # The 500-tool resource gate is deliberately separate from the causal matrix.
    gate_raw = run_dir / "raw" / "resource-gate.jsonl"
    initialize_jsonl(gate_raw)
    stream = generate_stream(
        tools_per_request=500, seen_before_fraction=0.9, requests=1, seed=50090
    )
    variant = variants["no-rule-cache"]
    gate_job = _stream_job(
        config,
        stream,
        case_id="pilot-500-tool-resource-gate",
        block_id=0,
        measured=False,
        arm="rule-off",
        variant=variant,
        snapshot=snapshot,
        cpu_affinity=[requested_cpu],
    )
    gate_execution = dict(config["execution"])
    gate_execution["timeout_seconds"] = float(pilot_config.get("resource_gate_timeout_seconds", 30))
    gate_config = copy.deepcopy(config)
    gate_config["execution"] = gate_execution
    gate_records = run_worker(
        worker=profiling_root() / "workers" / "compile_stream.py",
        job=gate_job,
        job_path=run_dir / "jobs" / "resource-gate.json",
        raw_path=gate_raw,
        variant=variant,
        execution=gate_execution,
        metadata=_metadata(gate_job, gate_config, variant),
    )
    gate_summary = next((r for r in gate_records if r["record_type"] == "stream-summary"), None)
    final_sampling = (authoritative_config or config)["sampling"]
    subset_requests = 5
    estimated_full_subset_seconds = (
        float(gate_summary["compile_time_ns"])
        / 1e9
        * subset_requests
        * len(CACHE_VARIANTS)
        * (int(final_sampling["warmup_blocks"]) + int(final_sampling["maximum_blocks"]))
        * 1.25
        if gate_summary
        and gate_summary.get("status") == "success"
        and gate_summary.get("compile_time_ns") is not None
        else None
    )
    remaining_run_budget_seconds = float(pilot_config.get("remaining_run_budget_seconds", 21600))
    gate_status = gate_summary["status"] if gate_summary else "invalid_output"
    gate_peak_rss_bytes = (
        max(
            int(gate_summary.get("peak_rss_bytes") or 0),
            int(gate_summary.get("whole_worker_peak_rss_bytes") or 0),
        )
        if gate_summary
        else None
    )
    gate_valid = gate_status == "success" or bool(
        gate_summary
        and gate_status in {"timeout", "rss_limit"}
        and gate_summary.get("baseline_ready_observed") is True
    )
    gate_pass = bool(
        gate_summary
        and gate_summary["status"] == "success"
        and gate_summary["compile_time_ns"]
        <= float(pilot_config.get("resource_gate_timeout_seconds", 30)) * 1e9
        and gate_peak_rss_bytes is not None
        and gate_peak_rss_bytes < int(pilot_config.get("resource_gate_rss_bytes", 4 * 1024**3))
        and estimated_full_subset_seconds is not None
        and estimated_full_subset_seconds <= remaining_run_budget_seconds
    )

    sentinel_raw = run_dir / "raw" / "sentinel.jsonl"
    initialize_jsonl(sentinel_raw)
    target_seconds = 0.0 if quick else float(pilot_config.get("sentinel_duration_seconds", 900))
    deadline = time.monotonic() + target_seconds
    sentinel_times: List[float] = []
    steal_fractions: List[float] = []
    sentinel_failures: List[Dict[str, Any]] = []
    count = 0
    started = time.monotonic()
    while count == 0 or time.monotonic() < deadline:
        sentinel_stream = generate_stream(
            tools_per_request=10, seen_before_fraction=0.5, requests=3, seed=770000
        )
        sentinel_variant = variants["production-profile"]
        job = _stream_job(
            config,
            sentinel_stream,
            case_id="pilot-sentinel",
            block_id=count,
            measured=False,
            arm="full",
            variant=sentinel_variant,
            snapshot=snapshot,
            cpu_affinity=[requested_cpu],
        )
        produced = run_worker(
            worker=profiling_root() / "workers" / "compile_stream.py",
            job=job,
            job_path=run_dir / "jobs" / f"sentinel-{count:06d}.json",
            raw_path=sentinel_raw,
            variant=sentinel_variant,
            execution=dict(config["execution"]),
            metadata=_metadata(job, config, sentinel_variant),
        )
        outcomes = [r for r in produced if r["record_type"] == "stream-summary"]
        if len(outcomes) != 1:
            raise SuiteError("sentinel worker did not emit exactly one canonical outcome")
        summary = outcomes[0]
        if summary.get("status") in {"program_error", "invalid_output", "kernel_termination"}:
            raise SuiteError(f"sentinel worker failed: {summary.get('status')}")
        if summary.get("status") == "success":
            sentinel_times.append(summary["compile_time_ns"] / 1e6)
            if summary.get("steal_fraction") is not None:
                steal_fractions.append(float(summary["steal_fraction"]))
        else:
            sentinel_failures.append({"iteration": count, "status": summary.get("status")})
        count += 1
        if quick or sentinel_failures:
            break
    duration = time.monotonic() - started
    cv = _coefficient_of_variation(sentinel_times)
    max_cv = float(pilot_config.get("sentinel_max_coefficient_of_variation", 0.10))
    sentinel_acceptable = not sentinel_failures and (
        len(sentinel_times) == 1
        if quick
        else len(sentinel_times) >= 7 and cv is not None and cv <= max_cv
    )
    steal_threshold = max(0.01, 2 * percentile(steal_fractions, 0.95)) if steal_fractions else 0.01
    freeze_eligible = (
        not quick
        and all(watchdog.values())
        and duration >= target_seconds * 0.95
        and sentinel_acceptable
        and saturation_ok
        and gate_valid
        and not any(
            r["status"] not in {"success", "skipped"}
            for r in records
            if r["record_type"] in {"sample", "stream-summary"}
        )
    )
    summary = {
        "schema_version": 1,
        "quick": quick,
        "freeze_eligible": freeze_eligible,
        "watchdog": watchdog,
        "qualification": qualification,
        "qualification_source_sha256": qualification_sha256,
        "qualification_canonical_sha256": (
            __import__("hashlib").sha256(canonical_json(qualification)).hexdigest()
            if qualification is not None
            else None
        ),
        "perf": perf,
        "resource_gate": {
            "include_500_tool_subset": gate_pass,
            "status": gate_status,
            "compile_time_ns": gate_summary.get("compile_time_ns") if gate_summary else None,
            "peak_rss_bytes": gate_summary.get("peak_rss_bytes") if gate_summary else None,
            "whole_worker_peak_rss_bytes": (
                gate_summary.get("whole_worker_peak_rss_bytes") if gate_summary else None
            ),
            "conservative_gate_peak_rss_bytes": gate_peak_rss_bytes,
            "estimated_full_subset_seconds": estimated_full_subset_seconds,
            "remaining_run_budget_seconds": remaining_run_budget_seconds,
            "estimate_safety_factor": 1.25,
        },
        "saturation": {
            "status": saturation_summary.get("status") if saturation_summary else "invalid_output",
            "proposed_requests_per_stream": proposed_stream_length,
            "selected_requests_per_stream": proposed_stream_length,
            "decision": "retain predeclared length; no post-hoc workload adjustment",
            "curve": saturation_curve,
        },
        "sentinel": {
            "target_seconds": target_seconds,
            "duration_seconds": duration,
            "successful_samples": len(sentinel_times),
            "median_compile_ms": statistics.median(sentinel_times) if sentinel_times else None,
            "coefficient_of_variation": cv,
            "maximum_coefficient_of_variation": max_cv,
            "acceptable": sentinel_acceptable,
            "iterations": count,
            "failures": sentinel_failures,
        },
        "frozen_decisions": {
            "timeout_seconds": (authoritative_config or config)["execution"]["timeout_seconds"],
            "requests_per_stream": proposed_stream_length,
            "include_500_tool_subset": gate_pass,
            "steal_time_threshold": steal_threshold,
            "perf_hardware_available": perf["hardware"].get("returncode") == 0,
            "perf_sampling_available": perf["software_sampling"].get("returncode") == 0,
            "primary_cpu": requested_cpu,
            "physical_cpu_ids": physical_ids,
            "machine_identity": machine_identity,
            "machine_fingerprint": machine_identity_fingerprint(machine_identity),
        },
    }
    write_json_atomic(run_dir / "pilot-summary.json", summary)
    return summary


def freeze_from_pilot(pilot_dir: Path, output: Path | None = None) -> Dict[str, Any]:
    pilot = load_json(pilot_dir / "pilot-summary.json")
    if pilot.get("freeze_eligible") is not True:
        raise SuiteError("pilot is not freeze-eligible; inspect pilot-summary.json")
    pilot_config = load_json(pilot_dir / "resolved-config.json")
    authoritative_path = resolve_path(
        pilot_config.get("authoritative_config", "profiling/configs/v0.2.7.json")
    )
    config = load_config(authoritative_path)
    config["variants"]["root"] = pilot_config["variants"]["root"]
    if not _tracked_tree_clean():
        raise SuiteError(
            "tracked source changes are present; commit the profiling implementation before freezing"
        )
    decisions = pilot["frozen_decisions"]
    config["execution"]["timeout_seconds"] = decisions["timeout_seconds"]
    config["cache"]["requests_per_stream"] = decisions["requests_per_stream"]
    config["cache"]["include_500_tool_subset"] = decisions["include_500_tool_subset"]
    config["execution"]["primary_cpu"] = decisions["primary_cpu"]
    config["execution"]["physical_cpu_ids"] = decisions["physical_cpu_ids"]
    config["machine_identity"] = decisions["machine_identity"]
    config["machine_fingerprint"] = decisions["machine_fingerprint"]
    if machine_identity_fingerprint(config["machine_identity"]) != config["machine_fingerprint"]:
        raise SuiteError("pilot machine identity evidence is internally inconsistent")
    secondary = config["cache"].get("secondary", {})
    if secondary.get("enabled"):
        cap = min(len(decisions["physical_cpu_ids"]), 8)
        secondary["resolved_thread_sweep"] = sorted(
            set(
                cap if value == "physical-cap-8" else int(value)
                for value in secondary.get("thread_sweep", [])
                if value == "physical-cap-8" or int(value) <= len(decisions["physical_cpu_ids"])
            )
        )
    config["steal_time_threshold"] = decisions["steal_time_threshold"]
    config["perf"] = {
        "hardware_available": decisions["perf_hardware_available"],
        "sampling_available": decisions["perf_sampling_available"],
    }
    snapshot, tokenizer_manifest, bfcl_dir, bfcl_manifest_path, variants = resolve_assets(config)
    del snapshot
    qualification = pilot.get("qualification")
    if not isinstance(qualification, dict) or qualification.get("passed") is not True:
        raise SuiteError("pilot lacks bound passed qualification evidence")
    if qualification.get("variant_manifest_hashes") != manifest_hashes(variants):
        raise SuiteError("qualification did not cover every authoritative variant")
    if qualification.get("tokenizer_manifest_sha256") != sha256_file(tokenizer_manifest):
        raise SuiteError("qualification tokenizer hash changed before freeze")
    copied_qualification = pilot_dir / "qualification.json"
    if not copied_qualification.is_file() or sha256_file(copied_qualification) != pilot.get(
        "qualification_source_sha256"
    ):
        raise SuiteError("copied qualification evidence is absent or has been modified")
    canonical_qualification_sha = (
        __import__("hashlib").sha256(canonical_json(qualification)).hexdigest()
    )
    if canonical_qualification_sha != pilot.get("qualification_canonical_sha256"):
        raise SuiteError("embedded qualification evidence has been modified")
    config["frozen"] = True
    config["frozen_at_unix_ns"] = time.time_ns()
    config["pilot_summary_sha256"] = sha256_file(pilot_dir / "pilot-summary.json")
    config["tokenizer_manifest_sha256"] = sha256_file(tokenizer_manifest)
    config["variant_manifest_hashes"] = manifest_hashes(variants)
    config["qualification"] = qualification
    config["qualification_source_sha256"] = pilot.get("qualification_source_sha256")
    config["qualification_canonical_sha256"] = canonical_qualification_sha
    if bfcl_dir is not None and bfcl_manifest_path is not None:
        bfcl_manifest = verify_bfcl_snapshot(bfcl_dir)
        traces = bfcl_trace_specs(bfcl_dir, requests=int(config["bfcl"].get("trace_requests", 5)))
        config["bfcl_manifest_sha256"] = sha256_file(bfcl_manifest_path)
        config["bfcl_revision"] = bfcl_manifest["revision"]
        config["bfcl_traces_sha256"] = (
            __import__("hashlib").sha256(canonical_json(traces)).hexdigest()
        )
        config["bfcl_validation"] = bfcl_manifest["validation"]
        if (
            qualification.get("bfcl_manifest_sha256") != config["bfcl_manifest_sha256"]
            or qualification.get("bfcl_revision") != config["bfcl_revision"]
            or qualification.get("bfcl_traces_sha256") != config["bfcl_traces_sha256"]
            or qualification.get("bfcl_production_variant_manifest_sha256")
            != bfcl_manifest["validation"]["production_variant_manifest_sha256"]
            or qualification.get("bfcl_tokenizer_manifest_sha256")
            != bfcl_manifest["validation"]["tokenizer_manifest_sha256"]
            or qualification.get("bfcl_validation_build_config")
            != bfcl_manifest["validation"]["build_config"]
        ):
            raise SuiteError("qualification BFCL binding changed before freeze")
    config["source_head"] = _git_head()
    config["config_hash"] = config_hash(config)
    validate_config(config, require_frozen=True)
    output = output or (pilot_dir / "frozen-config.json")
    if output.exists():
        raise SuiteError(f"refusing to overwrite frozen config: {output}")
    write_json_atomic(output, config)
    return config


def run_authoritative(
    config: Dict[str, Any], run_dir: Path, variant_root: Path | None = None
) -> List[Dict[str, Any]]:
    if platform.system() != "Linux":
        raise SuiteError("authoritative runs require Linux; Mac results are development-only")
    if not _tracked_tree_clean():
        raise SuiteError("tracked source tree is dirty; authoritative run refused")
    if config.get("source_head") != _git_head():
        raise SuiteError("current source HEAD differs from frozen configuration")
    current_machine = stable_machine_identity()
    try:
        require_machine_identity(config["machine_identity"], current_machine)
    except ValueError as exc:
        raise SuiteError(str(exc)) from exc
    if machine_identity_fingerprint(current_machine) != config.get("machine_fingerprint"):
        raise SuiteError("current machine fingerprint differs from frozen pilot host")
    frozen_physical = [int(cpu) for cpu in config["execution"].get("physical_cpu_ids", [])]
    primary_cpu = int(config["execution"].get("primary_cpu", -1))
    current_physical = physical_cpu_ids(preferred=primary_cpu)
    if not frozen_physical or any(cpu not in current_physical for cpu in frozen_physical):
        raise SuiteError(
            f"frozen distinct physical CPUs {frozen_physical} are unavailable; current {current_physical}"
        )
    snapshot, _, _, _, variants = resolve_assets(
        config, variant_root=variant_root, authoritative=True
    )
    create_run_dir(run_dir)
    write_json_atomic(run_dir / "frozen-config.json", config)
    write_json_atomic(run_dir / "environment.json", capture_environment())
    write_json_atomic(
        run_dir / "variant-manifests.json",
        {name: variant.manifest for name, variant in variants.items()},
    )
    _log(f"authoritative run starting: {run_dir}")
    # Diagnostics are unmeasured mechanism/replay jobs.  They run first so a failure in
    # that stage costs minutes rather than a completed timing matrix; the append-only raw
    # files, sequence numbering, and analysis are order-independent.
    records = execute_diagnostics(config, run_dir, variants, snapshot)
    records.extend(execute_suite(config, run_dir, variants, snapshot))
    _log("all jobs finished; hashing raw files and writing run-complete.json")
    raw_hashes = {
        path.relative_to(run_dir).as_posix(): sha256_file(path)
        for path in sorted((run_dir / "raw").glob("*.jsonl"))
    }
    provenance_hashes = {
        name: sha256_file(run_dir / name)
        for name in ("frozen-config.json", "environment.json", "variant-manifests.json")
    }
    job_hashes = {
        path.relative_to(run_dir).as_posix(): sha256_file(path)
        for path in sorted((run_dir / "jobs").glob("*.json"))
    }
    completion = {
        "schema_version": 1,
        "complete": True,
        "completed_at_unix_ns": time.time_ns(),
        "config_hash": config["config_hash"],
        "source_head": config["source_head"],
        "tokenizer_manifest_sha256": config["tokenizer_manifest_sha256"],
        "bfcl_manifest_sha256": config.get("bfcl_manifest_sha256"),
        "bfcl_revision": config.get("bfcl_revision"),
        "bfcl_traces_sha256": config.get("bfcl_traces_sha256"),
        "qualification_canonical_sha256": config.get("qualification_canonical_sha256"),
        "machine_fingerprint": config.get("machine_fingerprint"),
        "variant_manifest_hashes": config["variant_manifest_hashes"],
        "raw_files": raw_hashes,
        "record_count": len(records),
        "provenance_files": provenance_hashes,
        "job_count": len(job_hashes),
        "jobs_manifest_sha256": __import__("hashlib")
        .sha256(canonical_json(job_hashes))
        .hexdigest(),
    }
    write_json_atomic(run_dir / "run-complete.json", completion)
    _log(f"run complete: {len(records)} records, {len(job_hashes)} jobs")
    return records
