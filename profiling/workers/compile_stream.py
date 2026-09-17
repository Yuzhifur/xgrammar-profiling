#!/usr/bin/env python3
"""One isolated cache-stream benchmark worker."""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List

from xgrammar_profile.replay import (
    baseline_handshake,
    compiler_hook,
    current_rss_bytes,
    matcher_signature,
    measurement_end_handshake,
    profiling_snapshot,
    serialize_structural_tag,
    worker_stdout_guard,
)
from xgrammar_profile.tokenizer_snapshot import load_tokenizer_info


def record_base(job: Dict[str, Any], record_type: str, status: str = "success") -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "record_type": record_type,
        "experiment": "cache",
        "status": status,
        "case_id": job["case_id"],
        "block_id": int(job["block_id"]),
        "measured": bool(job["measured"]),
        "variant": job["variant"],
        "arm": job["arm"],
        "workload_fingerprint": job["workload_fingerprint"],
        "expected_request_count": int(job["expected_request_count"]),
    }


def run(job: Dict[str, Any]) -> List[Dict[str, Any]]:
    import xgrammar as xgr
    from xgrammar.builtin_structural_tag import get_model_structural_tag

    tokenizer_info = load_tokenizer_info(Path(job["tokenizer_snapshot"]))
    compiler = xgr.GrammarCompiler(
        tokenizer_info,
        max_threads=int(job["compiler_threads"]),
        cache_enabled=True,
        cache_limit_bytes=int(job["cache_limit_bytes"]),
    )
    baseline_rss = current_rss_bytes()
    baseline_handshake(job, baseline_rss)
    try:
        from xgrammar.testing import get_profiling_build_config

        detailed_stats = bool(get_profiling_build_config().get("XGRAMMAR_ENABLE_PROFILING_STATS"))
    except (ImportError, AttributeError, RuntimeError):
        detailed_stats = False
    records: List[Dict[str, Any]] = []
    cumulative_wall = 0
    cumulative_cpu = 0
    stream_started = time.perf_counter_ns()
    requests = job["requests"]
    semantic_signatures = []
    last_compiled = None
    for request in requests:
        tag = get_model_structural_tag(
            "qwen_3",
            tools=request["tools"],
            tool_choice="auto",
            reasoning="disabled",
            parallel_tool_calls=True,
        )
        serialized_tag = serialize_structural_tag(tag)
        if job["arm"] == "intra-only":
            compiler_hook(compiler, "clear_rule_level_cache", required=True)
        compiler_hook(compiler, "reset_profiling_stats")
        cpu_start = time.process_time_ns()
        wall_start = time.perf_counter_ns()
        compiled = compiler.compile_structural_tag(serialized_tag)
        last_compiled = compiled
        wall = time.perf_counter_ns() - wall_start
        cpu = time.process_time_ns() - cpu_start
        cumulative_wall += wall
        cumulative_cpu += cpu
        record = record_base(job, "sample")
        record.update(
            {
                "request_index": int(request["request_index"]),
                "target_seen_before_fraction": float(request["target_seen_before_fraction"]),
                "realized_seen_before_fraction": float(request["realized_seen_before_fraction"]),
                "tool_count": len(request["tools"]),
                "tool_ids": request["tool_ids"],
                "workload_class": job.get("workload_class", "controlled-primary"),
                "cache_limit_bytes": int(job["cache_limit_bytes"]),
                "compiler_threads": int(job["compiler_threads"]),
                "wall_time_ns": wall,
                "cpu_time_ns": cpu,
                "compile_time_ns": wall,
                "peak_rss_bytes": None,
                "baseline_rss_bytes": baseline_rss,
                # A compiled-grammar size walk is deliberately deferred until all
                # stream timings finish so it cannot perturb a later request.
                "compiled_grammar_bytes": None,
                "diagnostics": profiling_snapshot(compiler, compiled, detailed=detailed_stats),
            }
        )
        semantic_indices = job.get("semantic_request_indices")
        validate_this_request = job.get("validate_semantics") and (
            semantic_indices is None or int(request["request_index"]) in semantic_indices
        )
        if validate_this_request:
            signature = {
                "valid": matcher_signature(
                    compiled, request["validation_text"], request.get("validation_token_ids")
                ),
                "invalid": matcher_signature(
                    compiled,
                    request["invalid_validation_text"],
                    request.get("invalid_validation_token_ids"),
                ),
            }
            semantic_signatures.append(signature)
            record["semantic_signature"] = signature
        records.append(record)
    final_compiled_bytes = (
        int(last_compiled.memory_size_bytes) if last_compiled is not None else None
    )
    summary = record_base(job, "stream-summary")
    summary.update(
        {
            "request_count": len(requests),
            "wall_time_ns": time.perf_counter_ns() - stream_started,
            "cpu_time_ns": cumulative_cpu,
            "compile_time_ns": cumulative_wall,
            "peak_rss_bytes": None,
            "baseline_rss_bytes": baseline_rss,
            "compiled_grammar_bytes": final_compiled_bytes,
            "semantic_signature": semantic_signatures or None,
            "post_cold_compile_time_ns": sum(
                int(record["compile_time_ns"]) for record in records[1:]
            ),
            "workload_class": job.get("workload_class", "controlled-primary"),
            "cache_limit_bytes": int(job["cache_limit_bytes"]),
            "compiler_threads": int(job["compiler_threads"]),
        }
    )
    records.append(summary)
    measurement_end_handshake(job)
    return records


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True, type=Path)
    args = parser.parse_args()
    job = json.loads(args.job.read_text(encoding="utf-8"))
    records: List[Dict[str, Any]] = []
    failure: Dict[str, Any] | None = None
    with worker_stdout_guard():
        try:
            records = run(job)
        except Exception as exc:
            failure = {
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc()[-8000:],
            }
    if failure is not None:
        record = record_base(job, "sample", "program_error")
        record.update(
            {
                "wall_time_ns": None,
                "cpu_time_ns": None,
                "compile_time_ns": None,
                "peak_rss_bytes": None,
                "baseline_rss_bytes": current_rss_bytes(),
                "compiled_grammar_bytes": None,
                **failure,
            }
        )
        print(json.dumps(record, sort_keys=True, separators=(",", ":")), flush=True)
        return 1
    for record in records:
        print(json.dumps(record, sort_keys=True, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
