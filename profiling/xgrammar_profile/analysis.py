"""Deterministic summaries and paired bootstrap intervals from raw JSONL."""

from __future__ import annotations

import csv
import json
import math
import random
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from .cache_workload import exact_repeat_stream, generate_stream, stream_fingerprint
from .config import canonical_json, load_config, resolve_path, sha256_file, write_json_atomic
from .dataset import bfcl_trace_specs
from .repetition_workload import case_fingerprint, cases_from_config, make_case
from .validation import assert_signature_expected, compare_signatures, read_jsonl


class AnalysisError(RuntimeError):
    pass


def compact_repeat_expected(family: str, bound: int) -> bool:
    """The empirically qualified v0.2.7 normalizer thresholds."""
    return bound >= (130 if family.startswith("json-array") else 129)


def _compiled_grammar_size(record: Mapping[str, Any]) -> int:
    """Return the current entry size used to bound the exact-cache soft overshoot."""
    value = record.get("compiled_grammar_bytes")
    if value is None:
        value = (
            record.get("diagnostics", {}).get("compiled_grammar_stats", {}).get("memory_size_bytes")
        )
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise AnalysisError("cache diagnostic lacks a valid compiled-grammar size")
    return value


def _validate_cache_stats(
    record: Mapping[str, Any], arm: str, *, largest_seen_grammar_bytes: int | None = None
) -> None:
    """Validate the diagnostic counters before they can support mechanism claims."""
    stats = record["diagnostics"]["profiling_stats"]
    rule = stats["rule_level_cache"]
    grammar = stats["grammar_level_cache"]
    budget = int(record["cache_limit_bytes"])
    grammar_share = (budget // 3) * 2
    # v0.2.7 assigns the integer-division remainder to RuleLevelCache.
    rule_share = budget - grammar_share
    for cache_name, cache in (("rule", rule), ("grammar", grammar)):
        if not all(
            isinstance(cache.get(key), int) and not isinstance(cache.get(key), bool)
            for key in ("bytes", "entries", "max_bytes")
        ):
            raise AnalysisError(f"{cache_name} cache sizes are missing for {arm}")
        if cache["bytes"] < 0 or cache["entries"] < 0 or cache["max_bytes"] < 0:
            raise AnalysisError(f"{cache_name} cache sizes are invalid for {arm}")
    if rule["bytes"] > rule["max_bytes"]:
        raise AnalysisError(f"rule cache exceeds its hard capacity for {arm}")
    if grammar.get("enabled") is not True:
        raise AnalysisError(f"grammar-level cache is disabled in {arm}")
    if grammar["max_bytes"] != grammar_share:
        raise AnalysisError(f"grammar-cache share differs from v0.2.7 for {arm}")
    current_grammar_bytes = _compiled_grammar_size(record)
    if largest_seen_grammar_bytes is None:
        largest_seen_grammar_bytes = current_grammar_bytes
    if (
        not isinstance(largest_seen_grammar_bytes, int)
        or isinstance(largest_seen_grammar_bytes, bool)
        or largest_seen_grammar_bytes < current_grammar_bytes
    ):
        raise AnalysisError(f"grammar-cache entry-size bound is invalid for {arm}")
    # ThreadSafeLRUCache evicts before computing/inserting a miss.  The exact-
    # grammar cache can therefore end a call above its target by at most one
    # inserted entry.  A later exact hit can retain that state, so validate
    # against the largest entry observed so far in this stream rather than the
    # current request alone.
    if grammar["bytes"] > grammar["max_bytes"] + largest_seen_grammar_bytes:
        raise AnalysisError(f"grammar cache exceeds its soft capacity bound for {arm}")
    for key in ("lookups", "misses", "hits", "evictions"):
        value = grammar.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise AnalysisError(f"grammar-cache {key} counter is invalid for {arm}")
    if grammar["hits"] != grammar["lookups"] - grammar["misses"]:
        raise AnalysisError(f"grammar-cache counter arithmetic failed for {arm}")

    if arm == "rule-off":
        if (
            rule.get("enabled") is not False
            or rule["bytes"] != 0
            or rule["entries"] != 0
            or rule["max_bytes"] != 0
            or rule.get("shards") != []
        ):
            raise AnalysisError("rule-off diagnostic retained a rule-level cache")
        return

    if (
        not isinstance(stats.get("fsm_hash_time_ns"), int)
        or not isinstance(stats.get("adaptive_mask_resolution_time_ns"), int)
        or stats["fsm_hash_time_ns"] <= 0
        or stats["adaptive_mask_resolution_time_ns"] <= 0
    ):
        raise AnalysisError(
            f"cache timing counters did not exercise the enabled mechanism for {arm}"
        )

    if rule.get("enabled") is not True or rule["max_bytes"] != rule_share:
        raise AnalysisError(f"rule-level cache controls differ from v0.2.7 in {arm}")
    required_rule = (
        "lookups",
        "misses",
        "hits",
        "perfect_hits",
        "basic_hits",
        "same_compile_hits",
        "prior_compile_hits",
        "successful_insertions",
        "duplicate_insert_attempts",
        "oversized_rejected_entries",
        "evictions",
    )
    if any(
        not isinstance(rule.get(key), int) or isinstance(rule.get(key), bool) or rule[key] < 0
        for key in required_rule
    ):
        raise AnalysisError(f"rule-cache counters are missing or invalid for {arm}")
    if not (
        rule["hits"]
        == rule["lookups"] - rule["misses"]
        == rule["perfect_hits"] + rule["basic_hits"]
        == rule["same_compile_hits"] + rule["prior_compile_hits"]
    ):
        raise AnalysisError(f"rule-cache counter arithmetic failed for {arm}")
    if arm == "intra-only" and (rule["same_compile_hits"] <= 0 or rule["prior_compile_hits"] != 0):
        raise AnalysisError("intra-only diagnostic did not isolate positive same-compile reuse")
    shards = rule.get("shards")
    if not isinstance(shards, list) or not shards:
        raise AnalysisError(f"rule-cache shard diagnostics are missing for {arm}")
    for index, shard in enumerate(shards):
        if not isinstance(shard, dict) or shard.get("index") != index:
            raise AnalysisError(f"rule-cache shard ordering is invalid for {arm}")
        for key in ("bytes", "entries", "evictions"):
            value = shard.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise AnalysisError(f"rule-cache shard {key} is invalid for {arm}")
    if sum(shard["bytes"] for shard in shards) != rule["bytes"]:
        raise AnalysisError(f"rule-cache shard byte sum differs from total for {arm}")
    if sum(shard["entries"] for shard in shards) != rule["entries"]:
        raise AnalysisError(f"rule-cache shard entry sum differs from total for {arm}")
    if sum(shard["evictions"] for shard in shards) != rule["evictions"]:
        raise AnalysisError(f"rule-cache shard eviction sum differs from total for {arm}")


def percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        raise ValueError("cannot take percentile of empty input")
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return float(ordered[lower] * (1 - weight) + ordered[upper] * weight)


def paired_speedup(
    disabled: Mapping[int, float], optimized: Mapping[int, float], *, resamples: int, seed: int
) -> Dict[str, Any]:
    blocks = sorted(set(disabled) & set(optimized))
    logs = [
        math.log(disabled[block] / optimized[block])
        for block in blocks
        if disabled[block] > 0 and optimized[block] > 0
    ]
    if not logs:
        return {
            "paired_blocks": 0,
            "speedup": None,
            "ci95": [None, None],
            "relative_half_width": None,
        }
    estimate = math.exp(statistics.median(logs))
    rng = random.Random(seed)
    boot = []
    for _ in range(max(1, resamples)):
        sample = [logs[rng.randrange(len(logs))] for _ in logs]
        boot.append(math.exp(statistics.median(sample)))
    lower, upper = percentile(boot, 0.025), percentile(boot, 0.975)
    return {
        "paired_blocks": len(logs),
        "speedup": estimate,
        "ci95": [lower, upper],
        "relative_half_width": (upper - lower) / (2 * estimate),
    }


def _measurement_records(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result = []
    for record in records:
        correct_type = (
            record.get("experiment") == "cache" and record.get("record_type") == "stream-summary"
        ) or (record.get("experiment") == "repetition" and record.get("record_type") == "sample")
        if correct_type and record.get("measured") is True:
            result.append(record)
    return result


def _terminally_censored(records: Sequence[Mapping[str, Any]]) -> bool:
    ordered = sorted(records, key=lambda record: int(record.get("block_id", -1)))
    return len(ordered) >= 3 and all(
        record.get("status") in {"timeout", "rss_limit"}
        and record.get("baseline_ready_observed") is True
        for record in ordered[-3:]
    )


def summarize_records(
    records: Iterable[Dict[str, Any]], *, resamples: int = 10000, seed: int = 270227
) -> Dict[str, Any]:
    records = list(records)
    measurements = _measurement_records(records)
    groups: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    for record in measurements:
        groups[
            (
                record["experiment"],
                record["case_id"],
                record.get("arm") or record.get("variant") or "unknown",
            )
        ].append(record)
    cells: List[Dict[str, Any]] = []
    for (experiment, case_id, arm), group in sorted(groups.items()):
        successes = [record for record in group if record["status"] == "success"]
        times = [
            record["compile_time_ns"] / 1e6
            for record in successes
            if record.get("compile_time_ns") is not None
        ]
        cpu_times = [
            record["cpu_time_ns"] / 1e6
            for record in successes
            if record.get("cpu_time_ns") is not None
        ]
        rss = [
            record["peak_rss_bytes"]
            for record in successes
            if record.get("peak_rss_bytes") is not None
        ]
        rss_delta = [
            record["peak_rss_delta_bytes"]
            for record in successes
            if record.get("peak_rss_delta_bytes") is not None
        ]
        compiled_bytes = [
            record["compiled_grammar_bytes"]
            for record in successes
            if record.get("compiled_grammar_bytes") is not None
        ]
        workload_class = next(
            (record.get("workload_class") for record in group if record.get("workload_class")), None
        )
        terminal_censored = _terminally_censored(group)
        cells.append(
            {
                "experiment": experiment,
                "case_id": case_id,
                "arm": arm,
                "workload_class": workload_class
                or ("repetition-" + str(group[0].get("family", "unknown"))),
                "samples": len(group),
                "successes": len(successes),
                "timeouts": sum(record["status"] == "timeout" for record in group),
                "rss_limits": sum(record["status"] == "rss_limit" for record in group),
                "errors": sum(
                    record["status"] not in {"success", "timeout", "rss_limit", "skipped"}
                    for record in group
                ),
                "steal_flagged_samples": sum(
                    record.get("steal_flagged") is True for record in group
                ),
                "max_steal_fraction": max(
                    (
                        float(record["steal_fraction"])
                        for record in group
                        if isinstance(record.get("steal_fraction"), (int, float))
                    ),
                    default=None,
                ),
                "terminal_censored": terminal_censored,
                "median_compile_ms": statistics.median(times) if times else None,
                "q1_compile_ms": percentile(times, 0.25) if times else None,
                "q3_compile_ms": percentile(times, 0.75) if times else None,
                "p95_compile_ms": percentile(times, 0.95) if len(times) >= 20 else None,
                "median_peak_rss_bytes": statistics.median(rss) if rss else None,
                "q1_peak_rss_bytes": percentile(rss, 0.25) if rss else None,
                "q3_peak_rss_bytes": percentile(rss, 0.75) if rss else None,
                "median_peak_rss_delta_bytes": statistics.median(rss_delta) if rss_delta else None,
                "q1_peak_rss_delta_bytes": percentile(rss_delta, 0.25) if rss_delta else None,
                "q3_peak_rss_delta_bytes": percentile(rss_delta, 0.75) if rss_delta else None,
                "median_cpu_ms": statistics.median(cpu_times) if cpu_times else None,
                "median_compiled_grammar_bytes": (
                    statistics.median(compiled_bytes) if compiled_bytes else None
                ),
                "q1_compiled_grammar_bytes": (
                    percentile(compiled_bytes, 0.25) if compiled_bytes else None
                ),
                "q3_compiled_grammar_bytes": (
                    percentile(compiled_bytes, 0.75) if compiled_bytes else None
                ),
            }
        )

    by_case: Dict[Tuple[str, str], Dict[str, Dict[int, Dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    for record in measurements:
        if record["status"] == "success" and record.get("compile_time_ns"):
            by_case[(record["experiment"], record["case_id"])][
                record.get("arm") or record["variant"]
            ][int(record["block_id"])] = record
    comparisons: List[Dict[str, Any]] = []
    comparison_arms = {
        "cache": [
            ("rule-off", "intra-only", "same-compile-rule-cache"),
            ("intra-only", "full", "cross-request-persistence"),
            ("rule-off", "full", "total-rule-cache"),
        ],
        "repetition": [("no-repeat-compression", "production", "repetition-compression")],
    }
    for (experiment, case_id), arms in sorted(by_case.items()):
        for disabled_name, optimized_name, label in comparison_arms[experiment]:
            if disabled_name not in arms or optimized_name not in arms:
                continue
            metric = (
                "post_cold_compile_time_ns"
                if label == "cross-request-persistence"
                else "compile_time_ns"
            )
            disabled_values = {
                block: float(record[metric])
                for block, record in arms[disabled_name].items()
                if record.get(metric) is not None and record.get(metric) > 0
            }
            optimized_values = {
                block: float(record[metric])
                for block, record in arms[optimized_name].items()
                if record.get(metric) is not None and record.get(metric) > 0
            }
            comparison_censored = any(
                _terminally_censored(groups.get((experiment, case_id, arm_name), []))
                for arm_name in (disabled_name, optimized_name)
            )
            result = (
                {
                    "paired_blocks": len(set(disabled_values) & set(optimized_values)),
                    "speedup": None,
                    "ci95": [None, None],
                    "relative_half_width": None,
                }
                if comparison_censored
                else paired_speedup(
                    disabled_values, optimized_values, resamples=resamples, seed=seed
                )
            )
            representative = next(iter(arms[optimized_name].values()))
            result.update(
                {
                    "experiment": experiment,
                    "case_id": case_id,
                    "comparison": label,
                    "disabled_arm": disabled_name,
                    "optimized_arm": optimized_name,
                    "estimand": (
                        "per-stream aggregate requests 1..N (cold request 0 excluded)"
                        if metric == "post_cold_compile_time_ns"
                        else (
                            "whole-stream aggregate including request 0"
                            if experiment == "cache"
                            else "one isolated compilation"
                        )
                    ),
                    "metric": metric,
                    "workload_class": representative.get("workload_class")
                    or ("repetition-" + str(representative.get("family", "unknown"))),
                    "censored": comparison_censored,
                }
            )
            comparisons.append(result)
    geometric: Dict[str, float] = {}
    strata = sorted({(item["workload_class"], item["comparison"]) for item in comparisons})
    for workload_class, label in strata:
        values = [
            item["speedup"]
            for item in comparisons
            if item["comparison"] == label
            and item["workload_class"] == workload_class
            and item["speedup"]
            and not item.get("censored")
        ]
        if values:
            geometric[f"{workload_class}:{label}"] = math.exp(
                sum(math.log(value) for value in values) / len(values)
            )
    mechanisms: List[Dict[str, Any]] = []
    for record in records:
        if record.get("experiment") != "cache" or record.get("record_type") != "sample":
            continue
        diagnostics = record.get("diagnostics")
        if not isinstance(diagnostics, dict):
            continue
        rule_bytes = diagnostics.get("rule_cache_size_bytes")
        grammar_bytes = diagnostics.get("grammar_cache_size_bytes")
        total = (
            rule_bytes + grammar_bytes
            if isinstance(rule_bytes, int) and isinstance(grammar_bytes, int)
            else None
        )
        budget = record.get("cache_limit_bytes")
        mechanisms.append(
            {
                "case_id": record["case_id"],
                "arm": record.get("arm"),
                "variant": record.get("variant"),
                "block_id": record.get("block_id"),
                "request_index": record.get("request_index"),
                "workload_class": record.get("workload_class"),
                "measured": record.get("measured"),
                "status": record.get("status"),
                "steal_rerun_of_block": record.get("steal_rerun_of_block"),
                "cache_limit_bytes": budget,
                "rule_cache_size_bytes": rule_bytes,
                "grammar_cache_size_bytes": grammar_bytes,
                "total_cache_size_bytes": total,
                "total_budget_utilization": (
                    total / budget
                    if total is not None and isinstance(budget, int) and budget
                    else None
                ),
                "profiling_stats": diagnostics.get("profiling_stats"),
            }
        )
    request_curves: List[Dict[str, Any]] = []
    request_groups: Dict[Tuple[str, str, int], List[Dict[str, Any]]] = defaultdict(list)
    successful_cache_streams = {
        (record.get("case_id"), record.get("arm"), record.get("block_id"))
        for record in records
        if record.get("experiment") == "cache"
        and record.get("record_type") == "stream-summary"
        and record.get("status") == "success"
        and record.get("measured") is True
    }
    for record in records:
        if (
            record.get("experiment") == "cache"
            and record.get("record_type") == "sample"
            and record.get("status") == "success"
            and record.get("measured") is True
            and isinstance(record.get("request_index"), int)
            and (record.get("case_id"), record.get("arm"), record.get("block_id"))
            in successful_cache_streams
        ):
            request_groups[
                (record["case_id"], record.get("arm") or "unknown", record["request_index"])
            ].append(record)
    for (case_id, arm, request_index), group in sorted(request_groups.items()):
        request_curves.append(
            {
                "case_id": case_id,
                "arm": arm,
                "request_index": request_index,
                "cold_request": request_index == 0,
                "workload_class": group[0].get("workload_class"),
                "median_compile_ms": statistics.median(r["compile_time_ns"] / 1e6 for r in group),
                "median_realized_seen_before_fraction": statistics.median(
                    r.get("realized_seen_before_fraction", 0.0) for r in group
                ),
                "median_rule_cache_size_bytes": (
                    statistics.median(
                        r["diagnostics"]["rule_cache_size_bytes"]
                        for r in group
                        if isinstance(r.get("diagnostics", {}).get("rule_cache_size_bytes"), int)
                    )
                    if any(
                        isinstance(r.get("diagnostics", {}).get("rule_cache_size_bytes"), int)
                        for r in group
                    )
                    else None
                ),
                "median_grammar_cache_size_bytes": (
                    statistics.median(
                        r["diagnostics"]["grammar_cache_size_bytes"]
                        for r in group
                        if isinstance(r.get("diagnostics", {}).get("grammar_cache_size_bytes"), int)
                    )
                    if any(
                        isinstance(r.get("diagnostics", {}).get("grammar_cache_size_bytes"), int)
                        for r in group
                    )
                    else None
                ),
            }
        )

    def replay_token_entries(
        value: Any, path: tuple[str, ...] = ()
    ) -> Iterable[tuple[tuple[str, ...], Dict[str, Any]]]:
        if isinstance(value, dict):
            tokens = value.get("tokens")
            if isinstance(tokens, dict):
                yield path, tokens
            for key, item in value.items():
                if key != "tokens":
                    yield from replay_token_entries(item, path + (str(key),))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                yield from replay_token_entries(item, path + (str(index),))

    replay: List[Dict[str, Any]] = []
    for record in records:
        # Cache summaries repeat the signatures already carried by the per-request
        # records.  Keep each replay trace exactly once and preserve its request id.
        if record.get("experiment") == "cache" and record.get("record_type") != "sample":
            continue
        signature = record.get("semantic_signature")
        if not signature:
            continue
        for path, tokens in replay_token_entries(signature):
            if tokens.get("median_time_ns") is None:
                continue
            all_tokens_accepted = bool(
                isinstance(tokens.get("accepted"), list)
                and all(value is True for value in tokens.get("accepted", []))
            )
            terminated = tokens.get("terminated") is True
            trace_name = "/".join(path)
            if record.get("experiment") == "cache":
                oracle_expected = bool(path and path[-1] == "valid")
            else:
                oracle_expected = record.get("expected_acceptance", {}).get(path[0] if path else "")
            replay.append(
                {
                    "experiment": record.get("experiment"),
                    "case_id": record.get("case_id"),
                    "arm": record.get("arm"),
                    "request_or_trace_index": (
                        record.get("request_index")
                        if record.get("experiment") == "cache"
                        else "/".join(path)
                    ),
                    "trace": trace_name,
                    "token_count": len(tokens.get("ids", [])),
                    "all_tokens_accepted": all_tokens_accepted,
                    "terminated": terminated,
                    "oracle_expected": oracle_expected,
                    "performance_eligible": bool(
                        oracle_expected is True and all_tokens_accepted and terminated
                    ),
                    "median_time_ns": tokens.get("median_time_ns"),
                    "p95_time_ns": tokens.get("p95_time_ns"),
                }
            )
    repetition_structure: List[Dict[str, Any]] = []
    for record in records:
        if record.get("experiment") != "repetition" or record.get("block_id") != -2:
            continue
        stats = record.get("diagnostics", {}).get("compiled_grammar_stats")
        if not isinstance(stats, dict):
            continue
        repetition_structure.append(
            {
                "case_id": record.get("case_id"),
                "family": record.get("family"),
                "bound": record.get("bound"),
                "arm": record.get("arm"),
                "variant": record.get("variant"),
                "compiled_grammar_bytes": record.get("compiled_grammar_bytes"),
                **{key: stats.get(key) for key in sorted(stats)},
            }
        )
    return {
        "schema_version": 1,
        "cells": cells,
        "comparisons": comparisons,
        "geometric_mean_speedups": geometric,
        "cache_mechanisms": mechanisms,
        "request_curves": request_curves,
        "replay": replay,
        "repetition_structure": repetition_structure,
    }


def _expected_cases(config: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    cache_cases = {
        f"cache-tools{int(tools)}-seen{int(round(float(reuse) * 100)):02d}"
        for tools in config["cache"]["tools_per_request"]
        for reuse in config["cache"]["seen_before_fractions"]
    }
    if config["cache"].get("include_500_tool_subset"):
        cache_cases.add("cache-tools500-seen90")
    secondary = config["cache"].get("secondary", {})
    if secondary.get("enabled"):
        exact = secondary["exact_repeat"]
        cache_cases.add(f"cache-exact-repeat-tools{int(exact['tools'])}")
        cache_cases.update(
            f"cache-budget{int(budget)}-tools100-seen90"
            for budget in secondary.get("budget_sweep_bytes", [])
        )
        thread_values = secondary.get("resolved_thread_sweep")
        if not isinstance(thread_values, list):
            raise AnalysisError("frozen config lacks resolved_thread_sweep")
        cache_cases.update(
            f"cache-threads{int(threads)}-tools100-seen50" for threads in thread_values
        )
    if config.get("bfcl", {}).get("enabled"):
        cache_cases.update({"bfcl-10-schema-sample", "bfcl-50-medium-reuse", "bfcl-100-high-reuse"})
    repetition_cases = {case.case_id for case in cases_from_config(dict(config))}
    return cache_cases, repetition_cases


def _expected_cache_metadata(config: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    primary_cpu = int(config["execution"]["primary_cpu"])
    physical = [int(cpu) for cpu in config["execution"]["physical_cpu_ids"]]
    default_budget = int(config["cache"]["cache_limit_bytes"])
    default_threads = int(config["cache"]["compiler_threads"])
    requests = int(config["cache"]["requests_per_stream"])
    result: Dict[str, Dict[str, Any]] = {}
    for tools in config["cache"]["tools_per_request"]:
        for reuse in config["cache"]["seen_before_fractions"]:
            case_id = f"cache-tools{int(tools)}-seen{int(round(float(reuse) * 100)):02d}"
            result[case_id] = {
                "kind": "synthetic",
                "tools": int(tools),
                "reuse": float(reuse),
                "requests": requests,
                "workload_class": "controlled-primary",
                "cache_limit_bytes": default_budget,
                "compiler_threads": default_threads,
                "cpu_affinity": [primary_cpu],
            }
    if config["cache"].get("include_500_tool_subset"):
        result["cache-tools500-seen90"] = {
            "kind": "synthetic",
            "tools": 500,
            "reuse": 0.9,
            "requests": 5,
            "workload_class": "controlled-500-tool",
            "cache_limit_bytes": default_budget,
            "compiler_threads": default_threads,
            "cpu_affinity": [primary_cpu],
        }
    secondary = config["cache"].get("secondary", {})
    if secondary.get("enabled"):
        exact = secondary["exact_repeat"]
        result[f"cache-exact-repeat-tools{int(exact['tools'])}"] = {
            "kind": "exact-repeat",
            "tools": int(exact["tools"]),
            "reuse": 1.0,
            "requests": int(exact["requests"]),
            "workload_class": "exact-repeat-control",
            "cache_limit_bytes": default_budget,
            "compiler_threads": 1,
            "cpu_affinity": [primary_cpu],
        }
        for budget in secondary.get("budget_sweep_bytes", []):
            result[f"cache-budget{int(budget)}-tools100-seen90"] = {
                "kind": "synthetic",
                "tools": 100,
                "reuse": 0.9,
                "requests": requests,
                "workload_class": "budget-sweep",
                "cache_limit_bytes": int(budget),
                "compiler_threads": 1,
                "cpu_affinity": [primary_cpu],
            }
        for threads in secondary.get("resolved_thread_sweep", []):
            threads = int(threads)
            result[f"cache-threads{threads}-tools100-seen50"] = {
                "kind": "synthetic",
                "tools": 100,
                "reuse": 0.5,
                "requests": requests,
                "workload_class": "thread-sweep",
                "cache_limit_bytes": default_budget,
                "compiler_threads": threads,
                "cpu_affinity": physical[:threads],
            }
    if config.get("bfcl", {}).get("enabled"):
        traces = bfcl_trace_specs(
            resolve_path(config["bfcl"]["snapshot_dir"]),
            requests=int(config["bfcl"].get("trace_requests", 5)),
        )
        for trace in traces:
            result[trace["label"]] = {
                "kind": "fixed",
                "tools": int(trace["tools_per_request"]),
                "reuse": float(trace["reuse"]),
                "requests": len(trace["requests"]),
                "fixed_requests": trace["requests"],
                "workload_class": "bfcl-validation",
                "cache_limit_bytes": default_budget,
                "compiler_threads": 1,
                "cpu_affinity": [primary_cpu],
            }
    return result


def _verify_workload_contract(
    run_dir: Path,
    config: Mapping[str, Any],
    records: Sequence[Dict[str, Any]],
    measurements: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    cache_metadata = _expected_cache_metadata(config)
    repetition_cases = {case.case_id: case for case in cases_from_config(dict(config))}
    expected_variants = {
        ("cache", "rule-off"): "no-rule-cache",
        ("cache", "intra-only"): "production-profile",
        ("cache", "full"): "production-profile",
        ("repetition", "production"): "production-profile",
        ("repetition", "no-repeat-compression"): "no-repeat-compression",
    }
    measured_jobs: Dict[Tuple[str, str, int, str], Dict[str, Any]] = {}
    for path in sorted((run_dir / "jobs").glob("*.json")):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AnalysisError(f"invalid canonical job {path}: {exc}") from exc
        if job.get("measured") is not True:
            continue
        experiment = "cache" if "requests" in job else "repetition"
        key = (experiment, job["case_id"], int(job["block_id"]), job["arm"])
        if key in measured_jobs:
            raise AnalysisError(f"duplicate canonical measured job: {key}")
        measured_jobs[key] = job
        expected_variant = expected_variants.get((experiment, job["arm"]))
        if job.get("variant") != expected_variant:
            raise AnalysisError(
                f"job {key} uses variant {job.get('variant')}, expected {expected_variant}"
            )
        if experiment == "cache":
            expected = cache_metadata.get(job["case_id"])
            if expected is None:
                raise AnalysisError(f"unexpected measured cache job case: {job['case_id']}")
            for field in (
                "workload_class",
                "cache_limit_bytes",
                "compiler_threads",
                "cpu_affinity",
            ):
                if job.get(field) != expected[field]:
                    raise AnalysisError(
                        f"cache job metadata mismatch for {key}/{field}: "
                        f"{job.get(field)!r} != {expected[field]!r}"
                    )
            if job.get("expected_request_count") != expected["requests"]:
                raise AnalysisError(f"cache job request count mismatch for {key}")
            if len(job.get("requests", [])) != expected["requests"]:
                raise AnalysisError(f"cache job payload count mismatch for {key}")
            actual_fingerprint = stream_fingerprint(job["requests"])
            if job.get("workload_fingerprint") != actual_fingerprint:
                raise AnalysisError(f"cache job fingerprint does not match payload for {key}")
            seed = (
                int(config["cache"]["seed"])
                + expected["tools"] * 100_003
                + int(expected["reuse"] * 1000) * 101
                + max(int(job["block_id"]), -1) * 17
            )
            if expected["kind"] == "exact-repeat":
                expected_stream = exact_repeat_stream(
                    tools_per_request=expected["tools"], requests=expected["requests"], seed=seed
                )
            elif expected["kind"] == "fixed":
                expected_stream = expected["fixed_requests"]
            else:
                expected_stream = generate_stream(
                    tools_per_request=expected["tools"],
                    seen_before_fraction=expected["reuse"],
                    requests=expected["requests"],
                    seed=seed,
                )
            if actual_fingerprint != stream_fingerprint(expected_stream):
                raise AnalysisError(f"cache job differs from frozen generator for {key}")
        else:
            expected_case = repetition_cases.get(job["case_id"])
            if expected_case is None:
                raise AnalysisError(f"unexpected measured repetition job case: {job['case_id']}")
            if (
                job.get("family") != expected_case.family
                or job.get("bound") != expected_case.bound
                or job.get("compile_kind") != expected_case.compile_kind
                or job.get("source") != expected_case.source
                or job.get("workload_fingerprint") != case_fingerprint(expected_case)
            ):
                raise AnalysisError(f"repetition job metadata/fingerprint mismatch for {key}")
            if job.get("cpu_affinity") != [int(config["execution"]["primary_cpu"])]:
                raise AnalysisError(f"repetition job affinity mismatch for {key}")

    canonical_by_key = {
        (
            record["experiment"],
            record["case_id"],
            int(record["block_id"]),
            record.get("arm"),
        ): record
        for record in measurements
    }
    if set(canonical_by_key) != set(measured_jobs):
        raise AnalysisError(
            "measured canonical jobs and outcomes differ; "
            f"missing outcomes={sorted(set(measured_jobs)-set(canonical_by_key))[:5]}, "
            f"missing jobs={sorted(set(canonical_by_key)-set(measured_jobs))[:5]}"
        )
    for key, job in measured_jobs.items():
        outcome = canonical_by_key[key]
        expected_variant = expected_variants[(key[0], key[3])]
        if outcome.get("variant") != expected_variant:
            raise AnalysisError(f"measured outcome variant mismatch for {key}")
        if outcome.get("workload_fingerprint") != job.get("workload_fingerprint"):
            raise AnalysisError(f"measured outcome fingerprint mismatch for {key}")
        related = [
            record
            for record in records
            if record.get("measured") is True
            and record.get("experiment") == key[0]
            and record.get("case_id") == key[1]
            and int(record.get("block_id", -999)) == key[2]
            and record.get("arm") == key[3]
        ]
        if any(
            record.get("variant") != expected_variant
            or record.get("workload_fingerprint") != job.get("workload_fingerprint")
            for record in related
        ):
            raise AnalysisError(f"worker records disagree with canonical job for {key}")
        if key[0] == "cache" and outcome.get("status") == "success":
            request_samples = [
                record
                for record in related
                if record.get("record_type") == "sample"
                and record.get("status") == "success"
                and isinstance(record.get("request_index"), int)
            ]
            expected_indices = list(range(len(job["requests"])))
            if sorted(record["request_index"] for record in request_samples) != expected_indices:
                raise AnalysisError(f"successful cache request indices are incomplete for {key}")
            if outcome.get("request_count") != len(job["requests"]):
                raise AnalysisError(f"cache stream summary request_count mismatch for {key}")
            by_index = {record["request_index"]: record for record in request_samples}
            for request in job["requests"]:
                sample = by_index[int(request["request_index"])]
                if (
                    sample.get("tool_ids") != request["tool_ids"]
                    or sample.get("tool_count") != len(request["tools"])
                    or sample.get("target_seen_before_fraction")
                    != request["target_seen_before_fraction"]
                    or sample.get("realized_seen_before_fraction")
                    != request["realized_seen_before_fraction"]
                ):
                    raise AnalysisError(f"cache request metadata mismatch for {key}")
        elif key[0] == "repetition":
            expected_case = repetition_cases[key[1]]
            if (
                outcome.get("family") != expected_case.family
                or outcome.get("bound") != expected_case.bound
            ):
                raise AnalysisError(f"repetition outcome metadata mismatch for {key}")

    fingerprints: Dict[Tuple[str, str, int], set[str]] = defaultdict(set)
    for (experiment, case_id, block_id, _), job in measured_jobs.items():
        fingerprints[(experiment, case_id, block_id)].add(job["workload_fingerprint"])
    mismatches = [key for key, values in fingerprints.items() if len(values) != 1]
    if mismatches:
        raise AnalysisError(
            f"causal arms received different workload fingerprints: {mismatches[:5]}"
        )
    return {"measured_job_count": len(measured_jobs), "fingerprint_groups": len(fingerprints)}


def verify_run_completeness(
    run_dir: Path, config: Dict[str, Any], records: List[Dict[str, Any]], raw_files: List[Path]
) -> Dict[str, Any]:
    completion_path = run_dir / "run-complete.json"
    if not completion_path.is_file():
        raise AnalysisError("run-complete.json is missing; the authoritative run did not finish")
    completion = json.loads(completion_path.read_text(encoding="utf-8"))
    if completion.get("complete") is not True:
        raise AnalysisError("run completion manifest is not marked complete")
    for field in (
        "config_hash",
        "source_head",
        "tokenizer_manifest_sha256",
        "bfcl_manifest_sha256",
        "bfcl_revision",
        "bfcl_traces_sha256",
        "qualification_canonical_sha256",
        "machine_fingerprint",
        "variant_manifest_hashes",
    ):
        if completion.get(field) != config.get(field):
            raise AnalysisError(f"completion manifest {field} does not match frozen config")
    actual_raw_hashes = {
        path.relative_to(run_dir).as_posix(): sha256_file(path) for path in raw_files
    }
    if completion.get("raw_files") != actual_raw_hashes:
        raise AnalysisError("raw JSONL hashes differ from run-complete.json")
    if completion.get("record_count") != len(records):
        raise AnalysisError("raw record count differs from run-complete.json")
    actual_provenance = {
        name: sha256_file(run_dir / name)
        for name in ("frozen-config.json", "environment.json", "variant-manifests.json")
        if (run_dir / name).is_file()
    }
    if completion.get("provenance_files") != actual_provenance or len(actual_provenance) != 3:
        raise AnalysisError("run provenance files are missing or changed after completion")
    environment = json.loads((run_dir / "environment.json").read_text(encoding="utf-8"))
    if environment.get("stable_machine_fingerprint") != config.get("machine_fingerprint"):
        raise AnalysisError("captured run environment differs from the frozen pilot machine")
    allowed_affinity = environment.get("cpu_affinity")
    if not isinstance(allowed_affinity, list) or not set(
        config["execution"]["physical_cpu_ids"]
    ).issubset(set(allowed_affinity)):
        raise AnalysisError("frozen benchmark CPUs are absent from captured process affinity")
    job_hashes = {
        path.relative_to(run_dir).as_posix(): sha256_file(path)
        for path in sorted((run_dir / "jobs").glob("*.json"))
    }
    job_manifest_sha = __import__("hashlib").sha256(canonical_json(job_hashes)).hexdigest()
    if (
        completion.get("job_count") != len(job_hashes)
        or completion.get("jobs_manifest_sha256") != job_manifest_sha
    ):
        raise AnalysisError("canonical worker jobs are missing or changed after completion")

    expected_config_hash = config["config_hash"]
    expected_variant_hashes = config["variant_manifest_hashes"]
    for index, record in enumerate(records):
        if record.get("config_hash") != expected_config_hash:
            raise AnalysisError(f"record {index} has a foreign/missing config hash")
        variant = record.get("variant")
        if variant not in expected_variant_hashes:
            raise AnalysisError(f"record {index} names unrecognized variant {variant!r}")
        if record.get("variant_manifest_sha256") != expected_variant_hashes[variant]:
            raise AnalysisError(f"record {index} has a mismatched variant manifest hash")
        if record.get("status") in {"program_error", "invalid_output", "kernel_termination"}:
            raise AnalysisError(
                f"fatal worker status remains in completed run: {record.get('status')}"
            )
        if record.get("status") not in {"success", "timeout", "rss_limit", "skipped"}:
            raise AnalysisError(f"unrecognized completed-run status: {record.get('status')}")

    # Detailed traversal/stat counters are diagnostic-only.  Even when recorded after
    # the timer they can perturb the next request and therefore cannot appear between
    # authoritative timing samples.
    for record in records:
        if record.get("measured") is not True or record.get("status") != "success":
            continue
        diagnostics = record.get("diagnostics")
        if isinstance(diagnostics, dict) and (
            "profiling_stats" in diagnostics or "compiled_grammar_stats" in diagnostics
        ):
            raise AnalysisError(
                "authoritative timing record contains a diagnostic structure walk: "
                f"{record.get('experiment')}/{record.get('case_id')}/{record.get('arm')}"
            )

    measurements = _measurement_records(records)
    duplicate_keys: set[tuple[Any, ...]] = set()
    for record in measurements:
        key = (record["experiment"], record["case_id"], record.get("arm"), record["block_id"])
        if key in duplicate_keys:
            raise AnalysisError(f"duplicate measured cell record: {key}")
        duplicate_keys.add(key)
        if record.get("status") == "success" and not record.get("baseline_ready_observed"):
            raise AnalysisError(f"successful measurement lacks RSS baseline handshake: {key}")
        if record.get("status") == "success" and not record.get("measurement_end_observed"):
            raise AnalysisError(f"successful measurement lacks final retained-RSS handshake: {key}")
        if (
            record.get("status") in {"timeout", "rss_limit"}
            and record.get("baseline_ready_observed") is not True
        ):
            raise AnalysisError(
                f"measured censor occurred before the RSS baseline handshake: {key}"
            )

    for record in (item for item in measurements if item.get("steal_flagged") is True):
        canonical_type = "stream-summary" if record["experiment"] == "cache" else "sample"
        reruns = [
            item
            for item in records
            if item.get("record_type") == canonical_type
            and item.get("experiment") == record.get("experiment")
            and item.get("case_id") == record.get("case_id")
            and item.get("arm") == record.get("arm")
            and item.get("measured") is False
            and item.get("steal_rerun_of_block") == record.get("block_id")
        ]
        if len(reruns) != 1 or reruns[0].get("status") != "success":
            raise AnalysisError(
                "steal-flagged measurement lacks one successful retained diagnostic rerun: "
                f"{record['experiment']}/{record['case_id']}/{record.get('arm')}/"
                f"block-{record['block_id']}"
            )
        if reruns[0].get("workload_fingerprint") != record.get("workload_fingerprint"):
            raise AnalysisError("steal diagnostic rerun changed the frozen workload")

    expected_cache, expected_repetition = _expected_cases(config)
    measured_cache = {
        record["case_id"] for record in measurements if record["experiment"] == "cache"
    }
    measured_repetition = {
        record["case_id"] for record in measurements if record["experiment"] == "repetition"
    }
    if measured_cache != expected_cache:
        raise AnalysisError(
            f"cache matrix mismatch; missing={sorted(expected_cache-measured_cache)}, "
            f"unexpected={sorted(measured_cache-expected_cache)}"
        )
    if measured_repetition != expected_repetition:
        raise AnalysisError(
            f"repetition matrix mismatch; missing={sorted(expected_repetition-measured_repetition)}, "
            f"unexpected={sorted(measured_repetition-expected_repetition)}"
        )
    workload_audit = _verify_workload_contract(run_dir, config, records, measurements)

    minimum = int(config["sampling"]["minimum_blocks"])
    maximum = int(config["sampling"]["maximum_blocks"])
    arms_by_experiment = {
        "cache": (expected_cache, {"rule-off", "intra-only", "full"}),
        "repetition": (expected_repetition, {"production", "no-repeat-compression"}),
    }
    for experiment, (cases, expected_arms) in arms_by_experiment.items():
        for case_id in cases:
            for arm in expected_arms:
                group = [
                    record
                    for record in measurements
                    if record["experiment"] == experiment
                    and record["case_id"] == case_id
                    and record.get("arm") == arm
                ]
                if not group:
                    raise AnalysisError(f"missing measured arm {experiment}/{case_id}/{arm}")
                if any(record["block_id"] < 0 or record["block_id"] >= maximum for record in group):
                    raise AnalysisError(f"invalid block id in {experiment}/{case_id}/{arm}")
                successes = [record for record in group if record["status"] == "success"]
                if len(successes) < minimum and not _terminally_censored(group):
                    raise AnalysisError(
                        f"incomplete arm {experiment}/{case_id}/{arm}: "
                        f"{len(successes)} successes, no terminal three-censor sequence"
                    )
                other = [
                    record
                    for record in group
                    if record["status"] not in {"success", "timeout", "rss_limit"}
                ]
                if other:
                    raise AnalysisError(f"unexpected outcomes in {experiment}/{case_id}/{arm}")

            case_records = [
                record
                for record in measurements
                if record["experiment"] == experiment and record["case_id"] == case_id
            ]
            final_block = max(int(record["block_id"]) for record in case_records)
            for arm in expected_arms:
                arm_blocks = sorted(
                    int(record["block_id"]) for record in case_records if record.get("arm") == arm
                )
                if arm_blocks != list(range(len(arm_blocks))):
                    raise AnalysisError(
                        f"non-contiguous blocks in {experiment}/{case_id}/{arm}: {arm_blocks}"
                    )

            def has_terminal_censor(prefix: int) -> bool:
                for arm in expected_arms:
                    statuses = [
                        record["status"]
                        for record in sorted(
                            (
                                item
                                for item in case_records
                                if item.get("arm") == arm and int(item["block_id"]) <= prefix
                            ),
                            key=lambda item: int(item["block_id"]),
                        )
                    ]
                    if len(statuses) >= 3 and all(
                        status in {"timeout", "rss_limit"} for status in statuses[-3:]
                    ):
                        return True
                return False

            def has_active_censor(prefix: int) -> bool:
                for arm in expected_arms:
                    statuses = [
                        record["status"]
                        for record in sorted(
                            (
                                item
                                for item in case_records
                                if item.get("arm") == arm and int(item["block_id"]) <= prefix
                            ),
                            key=lambda item: int(item["block_id"]),
                        )
                    ]
                    trailing = 0
                    for status in reversed(statuses):
                        if status not in {"timeout", "rss_limit"}:
                            break
                        trailing += 1
                    if trailing in (1, 2):
                        return True
                return False

            if final_block + 1 > maximum or (
                final_block + 1 < minimum
                and not all(
                    _terminally_censored(
                        [record for record in case_records if record.get("arm") == arm]
                    )
                    for arm in expected_arms
                )
            ):
                raise AnalysisError(
                    f"invalid adaptive stopping length for {experiment}/{case_id}: "
                    f"{final_block + 1} blocks"
                )

            def precision_at(prefix: int) -> bool:
                subset = [
                    record
                    for record in records
                    if not (
                        record.get("measured") is True
                        and record.get("experiment") == experiment
                        and record.get("case_id") == case_id
                        and int(record.get("block_id", -1)) > prefix
                    )
                ]
                return precision_reached(
                    subset,
                    experiment=experiment,
                    case_id=case_id,
                    minimum_blocks=minimum,
                    relative_half_width=float(config["sampling"]["relative_ci_half_width"]),
                    resamples=int(config["sampling"]["bootstrap_resamples"]),
                    seed=int(config["sampling"]["bootstrap_seed"]),
                )

            # Re-evaluate the immediately preceding precision decision with the exact
            # frozen bootstrap parameters.  This is the decision the runner made on
            # the prior loop iteration and avoids an O(cases * blocks^2 * resamples)
            # audit while still detecting any off-by-one overrun.
            if (
                not has_terminal_censor(final_block)
                and not has_active_censor(final_block - 1)
                and final_block >= minimum
                and precision_at(final_block - 1)
            ):
                raise AnalysisError(
                    f"adaptive run continued after its precision rule at "
                    f"{experiment}/{case_id}/block-{final_block - 1}"
                )
            if final_block + 1 < maximum and not (
                not has_active_censor(final_block)
                and (has_terminal_censor(final_block) or precision_at(final_block))
            ):
                raise AnalysisError(
                    f"adaptive run stopped without precision or censor criterion: "
                    f"{experiment}/{case_id}"
                )

    cache_diag = [
        record
        for record in records
        if record.get("case_id") == "diagnostic-cache-tools100-seen90"
        and record.get("record_type") == "stream-summary"
        and record.get("status") == "success"
    ]
    if {record.get("arm") for record in cache_diag} != {"rule-off", "intra-only", "full"}:
        raise AnalysisError("cache diagnostic representative arms are incomplete")
    expected_cache_diagnostic_variants = {
        "rule-off": "no-rule-cache-diagnostic",
        "intra-only": "production-diagnostic",
        "full": "production-diagnostic",
    }

    cache_replay_signatures: Dict[str, Any] = {}
    for arm in ("rule-off", "intra-only", "full"):
        samples = [
            record
            for record in records
            if record.get("case_id") == "diagnostic-cache-tools100-seen90"
            and record.get("arm") == arm
            and record.get("record_type") == "sample"
            and record.get("status") == "success"
        ]
        if (
            len(samples) != 20
            or sorted(int(record.get("request_index", -1)) for record in samples) != list(range(20))
            or any(
                record.get("variant") != expected_cache_diagnostic_variants[arm]
                for record in samples
            )
        ):
            raise AnalysisError(f"cache diagnostic request matrix is incomplete for {arm}")
        if any(
            not isinstance(record.get("diagnostics", {}).get("profiling_stats"), dict)
            for record in samples
        ):
            raise AnalysisError(f"cache mechanism diagnostics are missing for {arm}")
        largest_seen_grammar_bytes = 0
        for record in sorted(samples, key=lambda item: int(item["request_index"])):
            stats = record["diagnostics"]["profiling_stats"]
            if (
                stats.get("stats_enabled") is not True
                or not isinstance(stats.get("rule_level_cache"), dict)
                or not isinstance(stats.get("grammar_level_cache"), dict)
                or not isinstance(stats.get("fsm_hash_time_ns"), int)
                or not isinstance(stats.get("adaptive_mask_resolution_time_ns"), int)
            ):
                raise AnalysisError(f"cache profiling counter schema is incomplete for {arm}")
            largest_seen_grammar_bytes = max(
                largest_seen_grammar_bytes, _compiled_grammar_size(record)
            )
            _validate_cache_stats(
                record, arm, largest_seen_grammar_bytes=largest_seen_grammar_bytes
            )
        replay_samples = [record for record in samples if record.get("semantic_signature")]
        if len(replay_samples) != 1 or replay_samples[0].get("request_index") != max(
            int(record.get("request_index", -1)) for record in samples
        ):
            raise AnalysisError(f"cache late-request replay diagnostic is incomplete for {arm}")
        cache_replay_signatures[arm] = replay_samples[0]["semantic_signature"]
        if (
            arm == "full"
            and replay_samples[0]["diagnostics"]["profiling_stats"]["rule_level_cache"][
                "prior_compile_hits"
            ]
            <= 0
        ):
            raise AnalysisError("full cache diagnostic did not exercise prior-compile reuse")
        signature = replay_samples[0]["semantic_signature"]
        try:
            assert_signature_expected(
                signature["valid"], expected=True, context=f"cache replay {arm} valid"
            )
            assert_signature_expected(
                signature["invalid"], expected=False, context=f"cache replay {arm} invalid"
            )
        except Exception as exc:
            raise AnalysisError(str(exc)) from exc
    try:
        compare_signatures(cache_replay_signatures, context="cache replay diagnostic")
    except Exception as exc:
        raise AnalysisError(str(exc)) from exc
    for budget in (64 * 1024**2, 256 * 1024**2, 512 * 1024**2):
        case_id = f"diagnostic-cache-budget{budget}-tools100-seen90"
        budget_samples = [
            record
            for record in records
            if record.get("case_id") == case_id
            and record.get("record_type") == "sample"
            and record.get("status") == "success"
        ]
        if (
            len(budget_samples) != 20
            or sorted(int(record.get("request_index", -1)) for record in budget_samples)
            != list(range(20))
            or any(record.get("variant") != "production-diagnostic" for record in budget_samples)
        ):
            raise AnalysisError(
                f"cache budget diagnostic request matrix is incomplete for {budget}"
            )
        if any(
            not isinstance(record.get("diagnostics", {}).get("profiling_stats"), dict)
            for record in budget_samples
        ):
            raise AnalysisError(f"cache budget diagnostics are missing for {budget} bytes")
        if any(
            record["diagnostics"]["profiling_stats"].get("stats_enabled") is not True
            or "adaptive_mask_resolution_time_ns" not in record["diagnostics"]["profiling_stats"]
            for record in budget_samples
        ):
            raise AnalysisError(f"cache budget counter schema is incomplete for {budget}")
        largest_seen_grammar_bytes = 0
        for record in sorted(budget_samples, key=lambda item: int(item["request_index"])):
            largest_seen_grammar_bytes = max(
                largest_seen_grammar_bytes, _compiled_grammar_size(record)
            )
            _validate_cache_stats(
                record, "full", largest_seen_grammar_bytes=largest_seen_grammar_bytes
            )
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
            case_id = f"{family}-n{bound}"
            replay_diagnostic = [
                record
                for record in records
                if record.get("case_id") == case_id
                and record.get("block_id") == -3
                and record.get("record_type") == "sample"
                and record.get("status") == "success"
            ]
            expected_replay_variants = {
                "production": "production-profile",
                "no-repeat-compression": "no-repeat-compression",
            }
            if {(record.get("arm"), record.get("variant")) for record in replay_diagnostic} != set(
                expected_replay_variants.items()
            ):
                raise AnalysisError(f"repetition replay arms are incomplete for {case_id}")
            if any(not record.get("semantic_signature") for record in replay_diagnostic):
                raise AnalysisError(f"repetition replay diagnostics missing for {case_id}")
            if any(
                "profiling_stats" in record.get("diagnostics", {})
                or "compiled_grammar_stats" in record.get("diagnostics", {})
                for record in replay_diagnostic
            ):
                raise AnalysisError(
                    f"repetition replay contaminated by structure diagnostics: {case_id}"
                )
            signatures = {
                record["arm"]: record["semantic_signature"] for record in replay_diagnostic
            }
            try:
                compare_signatures(signatures, context=f"repetition replay {case_id}")
            except Exception as exc:
                raise AnalysisError(str(exc)) from exc
            expected_case = make_case(family, bound)
            for arm, signature in signatures.items():
                for name, expected in expected_case.expected_acceptance.items():
                    try:
                        assert_signature_expected(
                            signature[name],
                            expected=expected,
                            context=f"repetition replay {case_id}/{arm}/{name}",
                        )
                    except Exception as exc:
                        raise AnalysisError(str(exc)) from exc

    structure_families = (
        "json-string",
        "json-array-primitive",
        "json-array-object",
        "json-array-minmax",
        "regex-range",
        "regex-exact",
        "regex-nonzero-min",
    )
    structure_case_ids = {
        make_case(family, bound).case_id
        for family in structure_families
        for bound in (127, 128, 129, 130)
    }
    structure_keys = {
        "rule_count",
        "grammar_expression_count",
        "complete_fsm_state_count",
        "complete_fsm_edge_count",
        "per_rule_fsm_state_count",
        "per_rule_fsm_edge_count",
        "scannable_state_count",
        "adaptive_mask_entry_count",
        "accepted_token_classifications",
        "rejected_token_classifications",
        "uncertain_token_classifications",
        "compact_repeat_expression_count",
        "memory_size_bytes",
    }
    for case_id in sorted(structure_case_ids):
        diagnostic = [
            record
            for record in records
            if record.get("case_id") == case_id
            and record.get("block_id") == -2
            and record.get("record_type") == "sample"
            and record.get("status") == "success"
        ]
        if {(record.get("arm"), record.get("variant")) for record in diagnostic} != {
            ("production", "production-diagnostic"),
            ("no-repeat-compression", "no-repeat-compression"),
        }:
            raise AnalysisError(f"repetition structure arms are incomplete for {case_id}")
        for record in diagnostic:
            stats = record.get("diagnostics", {}).get("compiled_grammar_stats")
            if not isinstance(stats, dict) or not structure_keys.issubset(stats):
                raise AnalysisError(f"repetition structure stats missing for {case_id}")
            if any(not isinstance(stats[key], int) or stats[key] < 0 for key in structure_keys):
                raise AnalysisError(f"invalid repetition structure counter for {case_id}")
            if stats["memory_size_bytes"] != record.get("compiled_grammar_bytes"):
                raise AnalysisError(f"repetition memory counters disagree for {case_id}")
            if record.get("family") != case_id.rsplit("-n", 1)[0]:
                raise AnalysisError(f"repetition diagnostic family mismatch for {case_id}")
            if int(record.get("bound", -1)) != int(case_id.rsplit("-n", 1)[1]):
                raise AnalysisError(f"repetition diagnostic bound mismatch for {case_id}")
            if record["arm"] == "production":
                profiling = record.get("diagnostics", {}).get("profiling_stats")
                if not isinstance(profiling, dict) or profiling.get("stats_enabled") is not True:
                    raise AnalysisError(f"production structure stats build mismatch for {case_id}")
            if record.get("semantic_signature"):
                raise AnalysisError(
                    f"structure diagnostic unexpectedly contains replay for {case_id}"
                )
        by_arm = {
            record["arm"]: record["diagnostics"]["compiled_grammar_stats"] for record in diagnostic
        }
        family, bound_text = case_id.rsplit("-n", 1)
        bound = int(bound_text)
        production_repeats = by_arm["production"]["compact_repeat_expression_count"]
        disabled_repeats = by_arm["no-repeat-compression"]["compact_repeat_expression_count"]
        if disabled_repeats != 0:
            raise AnalysisError(f"disabled arm retained compact repeats for {case_id}")
        expected_compact = compact_repeat_expected(family, bound)
        if (production_repeats > 0) != expected_compact:
            raise AnalysisError(
                f"unexpected compact-repeat threshold for {case_id}: {production_repeats}"
            )
    large_diagnostic = [
        record
        for record in records
        if record.get("case_id") == "regex-range-n65536"
        and record.get("block_id") == -2
        and record.get("record_type") == "sample"
        and record.get("arm") == "production"
        and record.get("status") == "success"
    ]
    if len(large_diagnostic) != 1 or not isinstance(
        large_diagnostic[0].get("diagnostics", {}).get("compiled_grammar_stats"), dict
    ):
        raise AnalysisError("large repetition production diagnostic is missing")
    if large_diagnostic[0].get("variant") != "production-diagnostic":
        raise AnalysisError("large repetition diagnostic used the wrong variant")
    if (
        large_diagnostic[0]["diagnostics"]["compiled_grammar_stats"].get(
            "compact_repeat_expression_count", 0
        )
        <= 0
    ):
        raise AnalysisError("large production diagnostic did not exercise compact repetition")
    return {
        "schema_version": 1,
        "passed": True,
        "record_count": len(records),
        "raw_files": actual_raw_hashes,
        "expected_cache_cases": len(expected_cache),
        "expected_repetition_cases": len(expected_repetition),
        "config_hash": expected_config_hash,
        "workload_audit": workload_audit,
    }


def analyze_run(run_dir: Path) -> Dict[str, Any]:
    raw_files = sorted((run_dir / "raw").glob("*.jsonl"))
    if not raw_files:
        raise AnalysisError(f"no raw JSONL files found under {run_dir / 'raw'}")
    records: List[Dict[str, Any]] = []
    for path in raw_files:
        records.extend(read_jsonl(path))
    config_path = run_dir / "frozen-config.json"
    if not config_path.is_file():
        raise AnalysisError("frozen-config.json is missing")
    config = load_config(config_path, require_frozen=True)
    completeness = verify_run_completeness(run_dir, config, records, raw_files)
    sampling = config.get("sampling", {})
    summary = summarize_records(
        records,
        resamples=int(sampling.get("bootstrap_resamples", 10000)),
        seed=int(sampling.get("bootstrap_seed", 270227)),
    )
    summary["record_count"] = len(records)
    summary["raw_files"] = [path.relative_to(run_dir).as_posix() for path in raw_files]
    summary["completeness"] = completeness
    output = run_dir / "analysis"
    output.mkdir(parents=True, exist_ok=True)
    write_json_atomic(output / "summary.json", summary)
    write_json_atomic(output / "completeness.json", completeness)
    fields = [
        "experiment",
        "case_id",
        "arm",
        "samples",
        "successes",
        "timeouts",
        "rss_limits",
        "errors",
        "steal_flagged_samples",
        "max_steal_fraction",
        "terminal_censored",
        "workload_class",
        "median_compile_ms",
        "q1_compile_ms",
        "q3_compile_ms",
        "p95_compile_ms",
        "median_cpu_ms",
        "median_peak_rss_bytes",
        "q1_peak_rss_bytes",
        "q3_peak_rss_bytes",
        "median_peak_rss_delta_bytes",
        "q1_peak_rss_delta_bytes",
        "q3_peak_rss_delta_bytes",
        "median_compiled_grammar_bytes",
        "q1_compiled_grammar_bytes",
        "q3_compiled_grammar_bytes",
    ]
    with (output / "summary.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows({field: cell.get(field) for field in fields} for cell in summary["cells"])
    comparison_fields = [
        "experiment",
        "case_id",
        "comparison",
        "disabled_arm",
        "optimized_arm",
        "paired_blocks",
        "workload_class",
        "estimand",
        "metric",
        "censored",
        "speedup",
        "ci95_low",
        "ci95_high",
        "relative_half_width",
    ]
    with (output / "comparisons.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=comparison_fields)
        writer.writeheader()
        for item in summary["comparisons"]:
            row = dict(item)
            row["ci95_low"], row["ci95_high"] = item["ci95"]
            writer.writerow({field: row.get(field) for field in comparison_fields})
    mechanism_fields = [
        "case_id",
        "arm",
        "variant",
        "block_id",
        "request_index",
        "workload_class",
        "measured",
        "status",
        "steal_rerun_of_block",
        "cache_limit_bytes",
        "rule_cache_size_bytes",
        "grammar_cache_size_bytes",
        "total_cache_size_bytes",
        "total_budget_utilization",
    ]
    with (output / "cache-mechanisms.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=mechanism_fields)
        writer.writeheader()
        writer.writerows(
            {field: item.get(field) for field in mechanism_fields}
            for item in summary["cache_mechanisms"]
        )
    curve_fields = [
        "case_id",
        "arm",
        "request_index",
        "cold_request",
        "workload_class",
        "median_compile_ms",
        "median_realized_seen_before_fraction",
        "median_rule_cache_size_bytes",
        "median_grammar_cache_size_bytes",
    ]
    with (output / "request-curves.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=curve_fields)
        writer.writeheader()
        writer.writerows(
            {field: item.get(field) for field in curve_fields} for item in summary["request_curves"]
        )
    replay_fields = [
        "experiment",
        "case_id",
        "arm",
        "request_or_trace_index",
        "trace",
        "token_count",
        "all_tokens_accepted",
        "terminated",
        "oracle_expected",
        "performance_eligible",
        "median_time_ns",
        "p95_time_ns",
    ]
    with (output / "replay.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=replay_fields)
        writer.writeheader()
        writer.writerows(
            {field: item.get(field) for field in replay_fields} for item in summary["replay"]
        )
    structure_rows = summary["repetition_structure"]
    structure_fields = sorted({key for row in structure_rows for key in row})
    with (output / "repetition-structure.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=structure_fields)
        writer.writeheader()
        writer.writerows(
            {field: row.get(field) for field in structure_fields} for row in structure_rows
        )
    analysis_manifest = {
        "schema_version": 1,
        "config_hash": config["config_hash"],
        "raw_files": completeness["raw_files"],
        "run_complete_sha256": sha256_file(run_dir / "run-complete.json"),
        "summary_sha256": sha256_file(output / "summary.json"),
        "completeness_sha256": sha256_file(output / "completeness.json"),
    }
    write_json_atomic(output / "analysis-manifest.json", analysis_manifest)
    return summary


def precision_reached(
    records: Iterable[Dict[str, Any]],
    *,
    experiment: str,
    case_id: str,
    minimum_blocks: int,
    relative_half_width: float,
    resamples: int,
    seed: int,
) -> bool:
    relevant = [
        record
        for record in records
        if record.get("experiment") == experiment and record.get("case_id") == case_id
    ]
    summary = summarize_records(relevant, resamples=resamples, seed=seed)
    required = 2 if experiment == "cache" else 1
    comparisons = [
        item
        for item in summary["comparisons"]
        if item["experiment"] == experiment
        and item["case_id"] == case_id
        and item["comparison"] != "total-rule-cache"
    ]
    return len(comparisons) >= required and all(
        item["paired_blocks"] >= minimum_blocks
        and item["relative_half_width"] is not None
        and item["relative_half_width"] <= relative_half_width
        for item in comparisons
    )
