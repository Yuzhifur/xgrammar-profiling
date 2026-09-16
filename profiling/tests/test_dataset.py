import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from xgrammar_profile.config import sha256_file, write_json_atomic
from xgrammar_profile.dataset import (
    BFCL_MANIFEST_SCHEMA_VERSION,
    BFCL_NORMALIZATION_REFERENCE_BLOBS,
    BFCL_NORMALIZATION_REFERENCE_PATHS,
    BFCL_NORMALIZATION_REFERENCE_REVISION,
    BFCL_NORMALIZATION_REFERENCE_SHA256,
    DatasetError,
    GORILLA_TO_OPENAPI_TYPES,
    _empty_normalization_counts,
    _normalization_contract,
    _normalization_provenance,
    _normalize_bfcl_with_metadata,
    _normalize_function_with_metadata,
    _trace_specs_from_accepted,
    bfcl_trace_specs,
    normalize_bfcl,
    prepare_bfcl,
    validate_revision,
    verify_bfcl_snapshot,
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

    def test_gorilla_dialect_is_converted_recursively_without_touching_data_values(self):
        source = {
            "name": "weather.lookup.v2",
            "description": "Look up weather.",
            "response": {"type": "float"},
            "parameters": {
                "type": "dict",
                "properties": {
                    "temperature": {"type": "float", "description": "Temperature."},
                    "flags": {"type": "list", "items": {"type": "bool"}},
                    "readings": {
                        "type": "list",
                        "items": {"type": "float", "description": "Array item."},
                    },
                    "matrix": {
                        "type": "list",
                        "items": {"type": "list", "items": {"type": "long"}},
                    },
                    "records": {
                        "type": "list",
                        "items": {"type": "dict", "properties": {"active": {"type": "bool"}}},
                    },
                    "payload": {
                        "type": "dict",
                        "properties": {
                            "count": {"type": "long"},
                            "nested_temperature": {
                                "type": "float",
                                "description": "Nested temperature.",
                            },
                            "implicit": {"description": "BFCL defaults this to a string."},
                            "choice": {"anyOf": [{"type": "String"}, {"type": "null"}]},
                        },
                    },
                    "metadata": {"type": "HashMap", "additionalProperties": {"type": "Any"}},
                    "literal": {
                        "type": "string",
                        "default": {"type": "dict"},
                        "const": {"type": "list"},
                        "enum": [{"type": "float"}],
                        "examples": [{"type": "tuple"}],
                    },
                },
            },
        }
        original = copy.deepcopy(source)

        normalized, reason, counts = _normalize_function_with_metadata(source)
        repeated, repeated_reason, repeated_counts = _normalize_function_with_metadata(source)

        self.assertIsNone(reason)
        self.assertIsNone(repeated_reason)
        self.assertEqual(repeated, normalized)
        self.assertEqual(repeated_counts, counts)
        self.assertEqual(source, original)
        function = normalized["function"]
        self.assertEqual(function["name"], "weather_lookup_v2")
        self.assertNotIn("response", function)
        parameters = function["parameters"]
        self.assertEqual(parameters["type"], "object")
        temperature = parameters["properties"]["temperature"]
        self.assertEqual(temperature["type"], "number")
        self.assertEqual(temperature["format"], "float")
        self.assertEqual(temperature["description"], "Temperature. This is a float type value.")
        self.assertEqual(parameters["properties"]["flags"]["type"], "array")
        self.assertEqual(parameters["properties"]["flags"]["items"]["type"], "boolean")
        reading_items = parameters["properties"]["readings"]["items"]
        self.assertEqual(reading_items["type"], "number")
        self.assertNotIn("format", reading_items)
        self.assertEqual(reading_items["description"], "Array item.")
        matrix_items = parameters["properties"]["matrix"]["items"]
        self.assertEqual(matrix_items["type"], "array")
        self.assertEqual(matrix_items["items"]["type"], "integer")
        record_items = parameters["properties"]["records"]["items"]
        self.assertEqual(record_items["type"], "object")
        self.assertEqual(record_items["properties"]["active"]["type"], "boolean")
        payload_properties = parameters["properties"]["payload"]["properties"]
        self.assertEqual(payload_properties["count"]["type"], "integer")
        self.assertEqual(payload_properties["nested_temperature"]["type"], "number")
        self.assertEqual(payload_properties["nested_temperature"]["format"], "float")
        self.assertEqual(payload_properties["implicit"]["type"], "string")
        self.assertEqual(payload_properties["choice"]["type"], "string")
        self.assertEqual(
            [item["type"] for item in payload_properties["choice"]["anyOf"]], ["String", "null"]
        )
        metadata = parameters["properties"]["metadata"]
        self.assertEqual(metadata["type"], "object")
        self.assertEqual(metadata["additionalProperties"]["type"], "Any")
        literal = parameters["properties"]["literal"]
        self.assertEqual(literal["default"], {"type": "dict"})
        self.assertEqual(literal["const"], {"type": "list"})
        self.assertEqual(literal["enum"], [{"type": "float"}])
        self.assertEqual(literal["examples"], [{"type": "tuple"}])
        self.assertEqual(counts["normalized_source_function_occurrences"], 1)
        self.assertEqual(counts["gorilla_dialect_source_occurrences"], 1)
        self.assertEqual(counts["renamed_source_function_occurrences"], 1)
        self.assertEqual(counts["function_name_dot_replacement_character_count"], 2)
        self.assertEqual(counts["projected_non_openai_field_source_occurrences"], 1)
        self.assertEqual(counts["projected_response_field_source_occurrences"], 1)
        self.assertEqual(counts["missing_property_type_default_occurrences"], 2)
        self.assertEqual(counts["float_format_annotation_occurrences"], 2)
        self.assertEqual(counts["type_rewrite_occurrences"]["dict->object"], 3)

    def test_already_valid_json_schema_is_preserved(self):
        parameters = {
            "type": "object",
            "properties": {
                "nullable": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "default": {"type": "dict"},
                },
                "value": {"type": ["integer", "null"]},
            },
        }
        source = {"name": "valid_name", "parameters": parameters}

        normalized, reason, counts = _normalize_function_with_metadata(source)

        self.assertIsNone(reason)
        self.assertEqual(normalized["function"]["parameters"], parameters)
        self.assertNotIn("type", normalized["function"]["parameters"]["properties"]["nullable"])
        self.assertEqual(counts["gorilla_dialect_source_occurrences"], 0)
        self.assertEqual(counts["missing_property_type_default_occurrences"], 0)
        self.assertEqual(counts["type_rewrite_occurrences"], {})

    def test_dotted_name_collision_is_deduplicated_and_counted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parameters = {"type": "dict", "properties": {}}
            (root / "data.json").write_text(
                json.dumps(
                    {
                        "tools": [
                            {"name": "math.factorial", "parameters": parameters},
                            {"name": "math_factorial", "parameters": parameters},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            accepted, rejected, counts = _normalize_bfcl_with_metadata(root)

        self.assertEqual(rejected, [])
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["tool"]["function"]["name"], "math_factorial")
        self.assertEqual(counts["normalized_source_function_occurrences"], 2)
        self.assertEqual(counts["renamed_source_function_occurrences"], 1)
        self.assertEqual(counts["function_name_dot_replacement_character_count"], 1)
        self.assertEqual(counts["source_unique_function_name_count"], 2)
        self.assertEqual(counts["normalized_unique_function_name_count"], 1)
        self.assertEqual(counts["normalized_name_collision_count"], 1)

    def test_normalization_contract_pins_upstream_mapping_and_source(self):
        contract = _normalization_contract()
        self.assertEqual(
            BFCL_NORMALIZATION_REFERENCE_REVISION, "6ea57973c7a6097fd7c5915698c54c17c5b1b6c8"
        )
        self.assertEqual(
            BFCL_NORMALIZATION_REFERENCE_PATHS,
            (
                "berkeley-function-call-leaderboard/bfcl_eval/model_handler/utils.py",
                "berkeley-function-call-leaderboard/bfcl_eval/constants/type_mappings.py",
            ),
        )
        self.assertEqual(
            GORILLA_TO_OPENAPI_TYPES,
            {
                "integer": "integer",
                "number": "number",
                "float": "number",
                "string": "string",
                "boolean": "boolean",
                "bool": "boolean",
                "array": "array",
                "list": "array",
                "dict": "object",
                "object": "object",
                "tuple": "array",
                "any": "string",
                "byte": "integer",
                "short": "integer",
                "long": "integer",
                "double": "number",
                "char": "string",
                "ArrayList": "array",
                "Array": "array",
                "HashMap": "object",
                "Hashtable": "object",
                "Queue": "array",
                "Stack": "array",
                "Any": "string",
                "String": "string",
                "Bigint": "integer",
            },
        )
        self.assertEqual(contract["reference_revision"], BFCL_NORMALIZATION_REFERENCE_REVISION)
        self.assertEqual(contract["reference_paths"], list(BFCL_NORMALIZATION_REFERENCE_PATHS))
        self.assertEqual(contract["reference_git_blobs"], BFCL_NORMALIZATION_REFERENCE_BLOBS)
        self.assertEqual(contract["reference_sha256"], BFCL_NORMALIZATION_REFERENCE_SHA256)
        self.assertEqual(
            contract["adapter_call"],
            "convert_to_tool(functions, GORILLA_TO_OPENAPI, ModelStyle.OSSMODEL)",
        )
        self.assertEqual(contract["type_mapping"], GORILLA_TO_OPENAPI_TYPES)

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
            normalization_counts = _empty_normalization_counts()
            normalization_counts["deduplicated_candidate_count"] = len(accepted)
            normalization_counts["normalized_source_function_occurrences"] = len(accepted)
            normalization_counts["source_unique_function_name_count"] = len(accepted)
            normalization_counts["normalized_unique_function_name_count"] = len(accepted)
            write_json_atomic(
                root / "manifest.json",
                {
                    "schema_version": BFCL_MANIFEST_SCHEMA_VERSION,
                    "revision": "a" * 40,
                    "normalization": _normalization_provenance(normalization_counts),
                    "accepted_count": len(accepted),
                    "accepted_unique_name_count": len(accepted),
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
                            "supported_unique_name_count": len(accepted),
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
            verified = verify_bfcl_snapshot(output)
        self.assertEqual(manifest["accepted_count"], 150)
        self.assertTrue(manifest["validation"]["passed"])
        self.assertEqual(manifest["schema_version"], BFCL_MANIFEST_SCHEMA_VERSION)
        self.assertEqual(verified["normalization"], manifest["normalization"])
        applied = manifest["normalization"]["applied"]
        self.assertEqual(applied["deduplicated_candidate_count"], 150)
        self.assertEqual(applied["normalized_source_function_occurrences"], 150)
        self.assertEqual(applied["gorilla_dialect_source_occurrences"], 0)
        self.assertEqual(applied["source_unique_function_name_count"], 150)
        self.assertEqual(applied["normalized_unique_function_name_count"], 150)
        self.assertEqual(applied["normalized_name_collision_count"], 0)

    def test_verification_rejects_inconsistent_normalization_counters(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            values = [
                {"name": f"tool_{index}", "parameters": {"type": "dict", "properties": {}}}
                for index in range(150)
            ]
            (source / "data.json").write_text(json.dumps({"tools": values}), encoding="utf-8")
            output = root / "output"
            build_config = {"XGRAMMAR_ENABLE_PROFILING_API": True}

            def support(candidates):
                return (candidates, [], {"passed": True, "runtime_build_config": build_config})

            def traces(items):
                return {"passed": True, "runtime_build_config": build_config}

            prepare_bfcl(
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
            manifest_path = output / "manifest.json"
            tampered = json.loads(manifest_path.read_text(encoding="utf-8"))
            tampered["normalization"]["applied"]["deduplicated_candidate_count"] = 149
            write_json_atomic(manifest_path, tampered)
            with self.assertRaisesRegex(DatasetError, "normalization counters are inconsistent"):
                verify_bfcl_snapshot(output)


if __name__ == "__main__":
    unittest.main()
