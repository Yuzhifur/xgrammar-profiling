import json
import tempfile
import unittest
from pathlib import Path

from xgrammar_profile.validation import (
    assert_signature_expected,
    RecordValidationError,
    compare_signatures,
    read_jsonl,
    validate_result_record,
)


def record(**updates):
    result = {
        "schema_version": 1,
        "record_type": "sample",
        "experiment": "repetition",
        "status": "success",
        "case_id": "regex-range-n128",
        "block_id": 0,
        "measured": True,
        "compile_time_ns": 100,
    }
    result.update(updates)
    return result


class ValidationTests(unittest.TestCase):
    def test_valid_record(self):
        validate_result_record(record())

    def test_success_requires_time(self):
        with self.assertRaises(RecordValidationError):
            validate_result_record(record(compile_time_ns=None))

    def test_jsonl_reports_line(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.jsonl"
            path.write_text(json.dumps(record()) + "\nnot json\n", encoding="utf-8")
            with self.assertRaisesRegex(RecordValidationError, ":2:"):
                read_jsonl(path)

    def test_signature_mismatch_stops_validation(self):
        compare_signatures({"a": {"mask": "x"}, "b": {"mask": "x"}}, context="ok")
        with self.assertRaises(RecordValidationError):
            compare_signatures({"a": {"mask": "x"}, "b": {"mask": "y"}}, context="bad")

    def test_string_and_token_oracles_must_both_match(self):
        accepted = {
            "string_accepted": True,
            "string_terminated": True,
            "tokens": {"accepted": [True, True], "terminated": True},
        }
        assert_signature_expected(accepted, expected=True, context="accepted")
        rejected = {
            "string_accepted": False,
            "string_terminated": False,
            "tokens": {"accepted": [True, False], "terminated": False},
        }
        assert_signature_expected(rejected, expected=False, context="rejected")
        with self.assertRaises(RecordValidationError):
            assert_signature_expected(
                {
                    "string_accepted": True,
                    "string_terminated": True,
                    "tokens": {"accepted": [True, False], "terminated": False},
                },
                expected=True,
                context="token mismatch",
            )


if __name__ == "__main__":
    unittest.main()
