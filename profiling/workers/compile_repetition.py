#!/usr/bin/env python3
"""One isolated repetition-compression benchmark worker."""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict

from xgrammar_profile.replay import (
    baseline_handshake,
    current_rss_bytes,
    matcher_signature,
    measurement_end_handshake,
    profiling_snapshot,
    worker_stdout_guard,
)
from xgrammar_profile.tokenizer_snapshot import load_tokenizer_info


def base(job: Dict[str, Any], status: str = "success") -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "record_type": "sample",
        "experiment": "repetition",
        "status": status,
        "case_id": job["case_id"],
        "block_id": int(job["block_id"]),
        "measured": bool(job["measured"]),
        "variant": job["variant"],
        "arm": job["arm"],
        "workload_fingerprint": job["workload_fingerprint"],
        "family": job["family"],
        "bound": int(job["bound"]),
    }


def run(job: Dict[str, Any]) -> Dict[str, Any]:
    import xgrammar as xgr

    tokenizer_info = load_tokenizer_info(Path(job["tokenizer_snapshot"]))
    compiler = xgr.GrammarCompiler(tokenizer_info, max_threads=1, cache_enabled=False)
    baseline_rss = current_rss_bytes()
    baseline_handshake(job, baseline_rss)
    try:
        from xgrammar.testing import get_profiling_build_config

        detailed_stats = bool(get_profiling_build_config().get("XGRAMMAR_ENABLE_PROFILING_STATS"))
    except (ImportError, AttributeError, RuntimeError):
        detailed_stats = False
    cpu_start = time.process_time_ns()
    wall_start = time.perf_counter_ns()
    if job["compile_kind"] == "json-schema":
        compiled = compiler.compile_json_schema(job["source"])
    elif job["compile_kind"] == "regex":
        compiled = compiler.compile_regex(job["source"])
    else:
        raise ValueError(f"unknown compile_kind: {job['compile_kind']}")
    wall = time.perf_counter_ns() - wall_start
    cpu = time.process_time_ns() - cpu_start
    signatures = {}
    if job.get("validate_semantics"):
        token_ids = job.get("validation_token_ids", {})
        for name, text in job["acceptance_examples"].items():
            signatures[name] = matcher_signature(compiled, text, token_ids.get(name))
    record = base(job)
    record.update(
        {
            "family": job["family"],
            "bound": int(job["bound"]),
            "expected_acceptance": job.get("expected_acceptance"),
            "wall_time_ns": wall,
            "cpu_time_ns": cpu,
            "compile_time_ns": wall,
            "peak_rss_bytes": None,
            "baseline_rss_bytes": baseline_rss,
            "compiled_grammar_bytes": int(compiled.memory_size_bytes),
            "semantic_signature": signatures or None,
            "diagnostics": profiling_snapshot(
                compiler,
                compiled,
                detailed=detailed_stats or bool(job.get("force_structure_snapshot")),
            ),
        }
    )
    measurement_end_handshake(job)
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True, type=Path)
    args = parser.parse_args()
    job = json.loads(args.job.read_text(encoding="utf-8"))
    record: Dict[str, Any] | None = None
    failure: Dict[str, Any] | None = None
    with worker_stdout_guard():
        try:
            record = run(job)
        except Exception as exc:
            failure = {
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc()[-8000:],
            }
    if failure is not None or record is None:
        record = base(job, "program_error")
        record.update(
            {
                "wall_time_ns": None,
                "cpu_time_ns": None,
                "compile_time_ns": None,
                "peak_rss_bytes": None,
                "baseline_rss_bytes": current_rss_bytes(),
                "compiled_grammar_bytes": None,
                **(failure or {}),
            }
        )
        print(json.dumps(record, sort_keys=True, separators=(",", ":")), flush=True)
        return 1
    print(json.dumps(record, sort_keys=True, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
