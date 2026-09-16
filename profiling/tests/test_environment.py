import unittest

from xgrammar_profile.environment import machine_identity_fingerprint, require_machine_identity


class EnvironmentTests(unittest.TestCase):
    def test_machine_identity_is_canonical_and_mismatch_is_rejected(self):
        first = {"system": "Linux", "lscpu": {"CPU(s)": "4"}, "mem_total_kib": 10}
        reordered = {"mem_total_kib": 10, "lscpu": {"CPU(s)": "4"}, "system": "Linux"}
        self.assertEqual(
            machine_identity_fingerprint(first), machine_identity_fingerprint(reordered)
        )
        require_machine_identity(first, reordered)
        with self.assertRaises(ValueError):
            require_machine_identity(first, {**first, "mem_total_kib": 11})


if __name__ == "__main__":
    unittest.main()
