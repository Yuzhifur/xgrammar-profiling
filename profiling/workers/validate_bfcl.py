#!/usr/bin/env python3
"""Offline production-profile compatibility validation for normalized BFCL schemas."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

from xgrammar_profile.replay import serialize_structural_tag
from xgrammar_profile.tokenizer_snapshot import load_tokenizer_info


def _compiler(snapshot: Path) -> Any:
    import xgrammar as xgr

    return xgr.GrammarCompiler(load_tokenizer_info(snapshot), max_threads=1, cache_enabled=False)


def _compile_tools(compiler: Any, tools: list[Dict[str, Any]]) -> None:
    from xgrammar.builtin_structural_tag import get_model_structural_tag

    tag = get_model_structural_tag(
        "qwen_3", tools=tools, tool_choice="auto", reasoning="disabled", parallel_tool_calls=True
    )
    compiler.compile_structural_tag(serialize_structural_tag(tag))


def run(job: Dict[str, Any]) -> Dict[str, Any]:
    from xgrammar.testing import get_profiling_build_config

    compiler = _compiler(Path(job["tokenizer_snapshot"]))
    build_config = get_profiling_build_config()
    if job["mode"] == "support":
        outcomes = []
        for item in job["payload"]:
            try:
                _compile_tools(compiler, [item["tool"]])
            except Exception as exc:  # Candidate-local failures are expected evidence.
                outcomes.append(
                    {
                        "fingerprint": item["fingerprint"],
                        "supported": False,
                        "reason": f"{type(exc).__name__}: {exc}",
                    }
                )
            else:
                outcomes.append({"fingerprint": item["fingerprint"], "supported": True})
        return {"outcomes": outcomes, "runtime_build_config": build_config}
    if job["mode"] == "traces":
        compiled_requests = 0
        labels = []
        for trace in job["payload"]:
            for request in trace["requests"]:
                _compile_tools(compiler, request["tools"])
                compiled_requests += 1
            labels.append(trace["label"])
        return {
            "passed": True,
            "labels": labels,
            "compiled_requests": compiled_requests,
            "runtime_build_config": build_config,
        }
    raise ValueError(f"unknown BFCL validation mode: {job['mode']}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = run(json.loads(args.job.read_text(encoding="utf-8")))
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
