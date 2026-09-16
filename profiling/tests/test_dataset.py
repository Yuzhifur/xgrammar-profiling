import json
import hashlib
import tempfile
import unittest
from pathlib import Path

from xgrammar_profile.config import sha256_file, write_json_atomic
from xgrammar_profile.dataset import (
    DatasetError,
    _trace_specs_from_accepted,
    bfcl_trace_specs,
    normalize_bfcl,
    prepare_bfcl,
    validate_revision,
)


class DatasetTests(unittest.TestCase):
    def test_revision_is_immutable(self):
        with self.assertRaises(DatasetError):
            validate_revision("main")
        revision = "a" * 40
        self.assertEqual(validate_revision(revision), revision)

    def test_normalization_is_deterministic_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = {
                "tools": [
                    {"name": "weather", "parameters": {"type": "object"}},
                    {"name": "weather", "parameters": {"type": "object"}},
                    {"name": "bad", "parameters": "not-json"},
                ]
            }
            (root / "data.json").write_text(json.dumps(value), encoding="utf-8")
            accepted, rejected = normalize_bfcl(root)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["tool"]["function"]["name"], "weather")
        self.assertEqual(len(rejected), 1)

    def test_jsonl_keeps_valid_lines_and_reports_invalid_line_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "official-style.jsonl"
            first = {"tools": [{"name": "alpha", "parameters": {"type": "object"}}]}
            second = {"functions": [{"name": "beta", "parameters": {"type": "object"}}]}
            path.write_text(
                json.dumps(first) + "\n" + "{not-json}\n" + json.dumps(second) + "\n",
                encoding="utf-8",
            )
            accepted, rejected = normalize_bfcl(root)
        self.assertEqual({item["tool"]["function"]["name"] for item in accepted}, {"alpha", "beta"})
        self.assertEqual(len(rejected), 1)
        self.assertTrue(rejected[0]["origin"].endswith("official-style.jsonl:2"))

    def test_normalized_origins_do_not_depend_on_checkout_path(self):
        outputs = []
        for _ in range(2):
            temporary = tempfile.TemporaryDirectory()
            self.addCleanup(temporary.cleanup)
            root = Path(temporary.name)
            nested = root / "nested"
            nested.mkdir()
            (nested / "data.json").write_text(
                json.dumps({"tools": [{"name": "alpha", "parameters": {"type": "object"}}]}),
                encoding="utf-8",
            )
            outputs.append(normalize_bfcl(root))
        self.assertEqual(outputs[0], outputs[1])

    def test_three_bfcl_traces_have_declared_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            accepted = []
            for index in range(150):
                accepted.append(
                    {
                        "fingerprint": f"{index:064x}",
                        "origin": f"fake[{index}]",
                        "tool": {
                            "type": "function",
                            "function": {
                                "name": f"tool_{index}",
                                "parameters": {"type": "object", "properties": {}},
                            },
                        },
                    }
                )
            write_json_atomic(root / "accepted.json", accepted)
            write_json_atomic(root / "rejected.json", [])
            frozen_traces = _trace_specs_from_accepted(accepted, requests=5, seed=270227)
            write_json_atomic(
                root / "manifest.json",
                {
                    "revision": "a" * 40,
                    "accepted_count": len(accepted),
                    "rejected_count": 0,
                    "normalized_candidate_count": len(accepted),
                    "support_assessed_count": len(accepted),
                    "support_unassessed_count": 0,
                    "traces_sha256": hashlib.sha256(
                        json.dumps(frozen_traces, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest(),
                    "trace_parameters": {"requests": 5, "seed": 270227},
                    "validation": {
                        "passed": True,
                        "production_variant_manifest_sha256": "b" * 64,
                        "tokenizer_manifest_sha256": "c" * 64,
                        "build_config": {},
                        "support": {
                            "passed": True,
                            "assessed_count": len(accepted),
                            "supported_count": len(accepted),
                            "rejected_count": 0,
                            "unassessed_count": 0,
                            "batches": [{"runtime_build_config": {}}],
                        },
                        "trace_smoke": {"passed": True, "runtime_build_config": {}},
                    },
                    "files": {
                        "accepted.json": sha256_file(root / "accepted.json"),
                        "rejected.json": sha256_file(root / "rejected.json"),
                    },
                },
            )
            traces = bfcl_trace_specs(root, requests=5)
        self.assertEqual(
            [trace["label"] for trace in traces],
            ["bfcl-10-schema-sample", "bfcl-50-medium-reuse", "bfcl-100-high-reuse"],
        )
        self.assertEqual(traces[1]["requests"][1]["realized_seen_before_fraction"], 0.5)
        self.assertEqual(traces[2]["requests"][1]["realized_seen_before_fraction"], 0.9)

    def test_prepare_binds_injected_support_and_trace_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            values = [
                {"name": f"tool_{index}", "parameters": {"type": "object", "properties": {}}}
                for index in range(150)
            ]
            (source / "data.json").write_text(json.dumps({"tools": values}), encoding="utf-8")
            output = root / "output"

            build_config = {"XGRAMMAR_ENABLE_PROFILING_API": True}

            def support(candidates):
                return (
                    candidates,
                    [],
                    {
                        "passed": True,
                        "supported_count": len(candidates),
                        "runtime_build_config": build_config,
                    },
                )

            def traces(items):
                return {
                    "passed": True,
                    "labels": [item["label"] for item in items],
                    "runtime_build_config": build_config,
                }

            manifest = prepare_bfcl(
                revision="a" * 40,
                output=output,
                source_dir=source,
                source_revision="a" * 40,
                support_validator=support,
                trace_validator=traces,
                validation_provenance={
                    "production_variant_manifest_sha256": "b" * 64,
                    "tokenizer_manifest_sha256": "c" * 64,
                    "build_config": build_config,
                },
            )
        self.assertEqual(manifest["accepted_count"], 150)
        self.assertTrue(manifest["validation"]["passed"])


if __name__ == "__main__":
    unittest.main()
