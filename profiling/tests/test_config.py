import unittest

from xgrammar_profile.config import (
    ConfigError,
    EXPECTED_NO_REPEAT_FAILURES,
    EXPECTED_QUALIFICATION_SUITES,
    validate_qualification_structure,
)


class QualificationStructureTests(unittest.TestCase):
    def _qualification(self):
        return {
            "suites": {name: {"status": "passed"} for name in EXPECTED_QUALIFICATION_SUITES},
            "expected_disabled": {
                "expected_node_ids": list(EXPECTED_NO_REPEAT_FAILURES),
                "observed_expected_failures": [
                    {"node_id": node_id, "status": "expected_failure"}
                    for node_id in EXPECTED_NO_REPEAT_FAILURES
                ],
                "unexpected_outcomes": [],
                "zero_unexplained_failures": True,
            },
        }

    def test_exact_reviewed_matrix_and_allowlist_pass(self):
        validate_qualification_structure(self._qualification())

    def test_missing_suite_or_broadened_allowlist_fails(self):
        qualification = self._qualification()
        qualification["suites"].pop("pristine_ctest")
        with self.assertRaises(ConfigError):
            validate_qualification_structure(qualification)

        qualification = self._qualification()
        qualification["expected_disabled"]["expected_node_ids"].append("whole-file::*")
        with self.assertRaises(ConfigError):
            validate_qualification_structure(qualification)


if __name__ == "__main__":
    unittest.main()
