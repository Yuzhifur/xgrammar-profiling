import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xgrammar_profile.analysis import (
    _terminally_censored,
    _validate_cache_stats,
    compact_repeat_expected,
    paired_speedup,
    precision_reached,
    summarize_records,
    verify_run_completeness,
)
from xgrammar_profile.config import write_json_atomic


def measurement(experiment, case_id, arm, block, value):
    return {
        "schema_version": 1,
        "record_type": "stream-summary" if experiment == "cache" else "sample",
        "experiment": experiment,
        "status": "success",
        "case_id": case_id,
        "block_id": block,
        "measured": True,
        "arm": arm,
        "variant": arm,
        "compile_time_ns": value,
        "peak_rss_bytes": 1000,
        "peak_rss_delta_bytes": 100,
        "compiled_grammar_bytes": 500,
        "baseline_ready_observed": True,
    }


class AnalysisTests(unittest.TestCase):
    def test_completion_rejects_changed_or_missing_authority_fingerprints(self):
        config = {
            "config_hash": "a" * 64,
            "source_head": "b" * 40,
            "tokenizer_manifest_sha256": "c" * 64,
            "bfcl_manifest_sha256": "d" * 64,
            "bfcl_revision": "e" * 40,
            "bfcl_traces_sha256": "f" * 64,
            "qualification_canonical_sha256": "1" * 64,
            "machine_fingerprint": "2" * 64,
            "variant_manifest_hashes": {},
        }
        for field, value in (
            ("qualification_canonical_sha256", "3" * 64),
            ("machine_fingerprint", None),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                completion = {"complete": True, **config}
                if value is None:
                    completion.pop(field)
                else:
                    completion[field] = value
                write_json_atomic(root / "run-complete.json", completion)
                with self.assertRaisesRegex(Exception, field):
                    verify_run_completeness(root, config, [], [])

    def test_family_specific_compact_repeat_thresholds(self):
        for family in ("json-string", "regex-range", "regex-exact", "regex-nonzero-min"):
            self.assertFalse(compact_repeat_expected(family, 128))
            self.assertTrue(compact_repeat_expected(family, 129))
        for family in ("json-array-primitive", "json-array-object", "json-array-minmax"):
            self.assertFalse(compact_repeat_expected(family, 129))
            self.assertTrue(compact_repeat_expected(family, 130))

    def test_paired_speedup(self):
        result = paired_speedup({0: 200, 1: 400}, {0: 100, 1: 200}, resamples=100, seed=1)
        self.assertAlmostEqual(result["speedup"], 2.0)
        self.assertEqual(result["paired_blocks"], 2)

    def test_cache_comparisons_have_causal_labels(self):
        records = []
        for block in range(7):
            records.extend(
                [
                    measurement("cache", "cell", "rule-off", block, 300 + block),
                    measurement("cache", "cell", "intra-only", block, 200 + block),
                    measurement("cache", "cell", "full", block, 100 + block),
                ]
            )
        summary = summarize_records(records, resamples=200, seed=2)
        labels = {item["comparison"] for item in summary["comparisons"]}
        self.assertEqual(
            labels, {"same-compile-rule-cache", "cross-request-persistence", "total-rule-cache"}
        )
        total = next(
            item for item in summary["comparisons"] if item["comparison"] == "total-rule-cache"
        )
        self.assertGreater(total["speedup"], 2.5)

    def test_repetition_censored_count(self):
        record = measurement("repetition", "case", "no-repeat-compression", 0, 10)
        record.update(status="timeout", compile_time_ns=None)
        summary = summarize_records([record])
        self.assertEqual(summary["cells"][0]["timeouts"], 1)
        self.assertIsNone(summary["cells"][0]["median_compile_ms"])

    def test_cache_censored_stream_is_not_dropped(self):
        record = measurement("cache", "cell", "rule-off", 0, 10)
        record.update(status="rss_limit", compile_time_ns=None)
        summary = summarize_records([record])
        self.assertEqual(summary["cells"][0]["rss_limits"], 1)

    def test_confirmed_terminal_censor_suppresses_speedup(self):
        records = []
        for block in range(2):
            for arm, value in (("rule-off", 300), ("intra-only", 200), ("full", 100)):
                record = measurement("cache", "cell", arm, block, value)
                record["post_cold_compile_time_ns"] = value - 10
                records.append(record)
        for block in range(2, 5):
            censored = measurement("cache", "cell", "full", block, 100)
            censored.update(status="timeout", compile_time_ns=None)
            records.append(censored)
        summary = summarize_records(records, resamples=20, seed=3)
        affected = [item for item in summary["comparisons"] if item["optimized_arm"] == "full"]
        self.assertTrue(affected)
        self.assertTrue(all(item["censored"] for item in affected))
        self.assertTrue(all(item["speedup"] is None for item in affected))

    def test_transient_censor_does_not_suppress_successful_estimand(self):
        records = []
        for block in range(7):
            for arm, value in (("rule-off", 300), ("intra-only", 200), ("full", 100)):
                record = measurement("cache", "cell", arm, block, value)
                record["post_cold_compile_time_ns"] = value - 10
                records.append(record)
        transient = measurement("cache", "cell", "full", 7, 100)
        transient.update(status="timeout", compile_time_ns=None)
        records.append(transient)
        summary = summarize_records(records, resamples=20, seed=3)
        affected = [item for item in summary["comparisons"] if item["optimized_arm"] == "full"]
        self.assertTrue(all(not item["censored"] for item in affected))
        self.assertTrue(all(item["speedup"] is not None for item in affected))

    def test_only_post_baseline_censors_can_be_terminal(self):
        prebaseline = []
        postbaseline = []
        for block in range(3):
            before = measurement("repetition", "case", "production", block, 1)
            before.update(status="timeout", compile_time_ns=None, baseline_ready_observed=False)
            prebaseline.append(before)
            after = dict(before, baseline_ready_observed=True)
            postbaseline.append(after)
        self.assertFalse(_terminally_censored(prebaseline))
        self.assertTrue(_terminally_censored(postbaseline))

    def test_memory_and_compiled_size_iqr(self):
        records = []
        for block, value in enumerate((100, 200, 300, 400)):
            record = measurement("repetition", "memory", "production", block, 1)
            record.update(
                peak_rss_bytes=value,
                peak_rss_delta_bytes=value // 2,
                compiled_grammar_bytes=value * 2,
            )
            records.append(record)
        cell = summarize_records(records)["cells"][0]
        self.assertEqual(cell["q1_peak_rss_bytes"], 175.0)
        self.assertEqual(cell["q3_peak_rss_bytes"], 325.0)
        self.assertEqual(cell["q1_peak_rss_delta_bytes"], 87.5)
        self.assertEqual(cell["q3_peak_rss_delta_bytes"], 162.5)
        self.assertEqual(cell["q1_compiled_grammar_bytes"], 350.0)
        self.assertEqual(cell["q3_compiled_grammar_bytes"], 650.0)

    def test_cache_stats_use_truncated_v027_budget_shares_and_reject_zero_exercise(self):
        # 64 MiB is not divisible by three, so this catches dropping the
        # one-byte remainder that v0.2.7 assigns to RuleLevelCache.
        budget = 64 * 1024**2
        grammar_share = (budget // 3) * 2
        rule_share = budget - grammar_share
        self.assertEqual(rule_share + grammar_share, budget)
        record = {
            "cache_limit_bytes": budget,
            "diagnostics": {
                "compiled_grammar_stats": {"memory_size_bytes": 100},
                "profiling_stats": {
                    "fsm_hash_time_ns": 1,
                    "adaptive_mask_resolution_time_ns": 1,
                    "rule_level_cache": {
                        "enabled": True,
                        "bytes": 10,
                        "entries": 1,
                        "max_bytes": rule_share,
                        "lookups": 3,
                        "misses": 1,
                        "hits": 2,
                        "perfect_hits": 1,
                        "basic_hits": 1,
                        "same_compile_hits": 2,
                        "prior_compile_hits": 0,
                        "successful_insertions": 1,
                        "duplicate_insert_attempts": 0,
                        "oversized_rejected_entries": 0,
                        "evictions": 0,
                        "shards": [{"index": 0, "bytes": 10, "entries": 1, "evictions": 0}],
                    },
                    "grammar_level_cache": {
                        "enabled": True,
                        "bytes": 20,
                        "entries": 1,
                        "max_bytes": grammar_share,
                        "lookups": 2,
                        "misses": 1,
                        "hits": 1,
                        "evictions": 0,
                    },
                },
            },
        }
        _validate_cache_stats(record, "intra-only")
        grammar = record["diagnostics"]["profiling_stats"]["grammar_level_cache"]
        # v0.2.7 evicts before inserting an exact-grammar miss, so one newly
        # inserted entry may remain above the configured grammar-cache target.
        grammar["bytes"] = grammar_share + 100
        _validate_cache_stats(record, "intra-only", largest_seen_grammar_bytes=100)
        grammar["bytes"] += 1
        with self.assertRaisesRegex(Exception, "soft capacity"):
            _validate_cache_stats(record, "intra-only", largest_seen_grammar_bytes=100)
        grammar["bytes"] = 20
        record["diagnostics"]["profiling_stats"]["adaptive_mask_resolution_time_ns"] = 0
        with self.assertRaisesRegex(Exception, "did not exercise"):
            _validate_cache_stats(record, "intra-only")

    def test_cache_mechanism_does_not_depend_on_semantic_signature(self):
        record = measurement("cache", "diagnostic", "full", -2, 10)
        record.update(
            measured=False,
            record_type="sample",
            request_index=0,
            cache_limit_bytes=100,
            diagnostics={
                "rule_cache_size_bytes": 20,
                "grammar_cache_size_bytes": 30,
                "profiling_stats": {"rule_level_cache": {"hits": 1}},
            },
        )
        summary = summarize_records([record])
        self.assertEqual(len(summary["cache_mechanisms"]), 1)
        self.assertEqual(summary["cache_mechanisms"][0]["total_cache_size_bytes"], 50)

    def test_request_curves_exclude_warmups_and_diagnostic_reruns(self):
        measured = measurement("cache", "curve", "full", 0, 100)
        sample = dict(measured, record_type="sample", request_index=1, measured=True)
        unmeasured = dict(sample, measured=False, compile_time_ns=10_000)
        partial = dict(sample, block_id=1, compile_time_ns=20_000)
        timeout = dict(measured, block_id=1, status="timeout", compile_time_ns=None)
        summary = summarize_records([measured, sample, unmeasured, partial, timeout])
        self.assertEqual(len(summary["request_curves"]), 1)
        self.assertEqual(summary["request_curves"][0]["median_compile_ms"], 0.0001)

    def test_nested_repetition_replay_is_summarized(self):
        record = measurement("repetition", "regex-range-n128", "production", -2, 10)
        record.update(
            measured=False,
            semantic_signature={
                "max": {"tokens": {"ids": [1, 2], "median_time_ns": 7, "p95_time_ns": 9}}
            },
        )
        summary = summarize_records([record])
        self.assertEqual(len(summary["replay"]), 1)
        self.assertEqual(summary["replay"][0]["request_or_trace_index"], "max")

    def test_precision_filters_foreign_cases_before_bootstrap(self):
        captured = []

        def fake(records, **kwargs):
            captured.extend(records)
            return {
                "comparisons": [
                    {
                        "experiment": "repetition",
                        "case_id": "target",
                        "comparison": "repetition-compression",
                        "paired_blocks": 7,
                        "relative_half_width": 0.01,
                    }
                ]
            }

        records = [
            measurement("repetition", "target", "production", 0, 10),
            measurement("repetition", "foreign", "production", 0, 10),
        ]
        with patch("xgrammar_profile.analysis.summarize_records", side_effect=fake):
            self.assertTrue(
                precision_reached(
                    records,
                    experiment="repetition",
                    case_id="target",
                    minimum_blocks=7,
                    relative_half_width=0.05,
                    resamples=10,
                    seed=1,
                )
            )
        self.assertEqual({record["case_id"] for record in captured}, {"target"})


if __name__ == "__main__":
    unittest.main()
