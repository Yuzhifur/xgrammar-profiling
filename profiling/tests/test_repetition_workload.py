import unittest

from xgrammar_profile.repetition_workload import cases_from_config, make_case


class RepetitionWorkloadTests(unittest.TestCase):
    def test_boundary_cases_preserve_requested_bound(self):
        for bound in (127, 128, 129):
            case = make_case("regex-range", bound)
            self.assertEqual(case.source, f"[a]{{0,{bound}}}")
            self.assertEqual(case.bound, bound)

    def test_json_array_minmax(self):
        case = make_case("json-array-minmax", 129)
        self.assertEqual(case.source["minItems"], 64)
        self.assertEqual(case.source["maxItems"], 129)
        self.assertIn("min_minus_one", case.acceptance_examples)
        self.assertFalse(case.expected_acceptance["max_plus_one"])

    def test_boundary_oracles_include_valid_and_over_bound_examples(self):
        for family in (
            "json-array-object",
            "json-array-minmax",
            "regex-exact",
            "regex-nonzero-min",
        ):
            case = make_case(family, 128)
            valid_boundary = "exact" if family == "regex-exact" else "max"
            self.assertIn(valid_boundary, case.acceptance_examples)
            self.assertTrue(case.expected_acceptance[valid_boundary])
            self.assertIn("max_plus_one", case.acceptance_examples)
            self.assertFalse(case.expected_acceptance["max_plus_one"])

    def test_config_cases_are_deduplicated(self):
        config = {
            "repetition": {
                "families": ["json-string"],
                "bounds": [128],
                "focused_families": ["json-string"],
                "focused_bounds": [128],
            }
        }
        self.assertEqual(len(cases_from_config(config)), 1)

    def test_unknown_family_rejected(self):
        with self.assertRaises(ValueError):
            make_case("unknown", 10)


if __name__ == "__main__":
    unittest.main()
