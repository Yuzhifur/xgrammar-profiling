import tempfile
import unittest
from pathlib import Path

from xgrammar_profile.variants import package_tree_sha256


class VariantTests(unittest.TestCase):
    def test_package_tree_hash_detects_wrapper_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory)
            package = site / "xgrammar"
            package.mkdir()
            wrapper = package / "testing.py"
            wrapper.write_text("VALUE = 1\n", encoding="utf-8")
            first = package_tree_sha256([site])
            wrapper.write_text("VALUE = 2\n", encoding="utf-8")
            second = package_tree_sha256([site])
            wrapper.write_text("VALUE = 1\n", encoding="utf-8")
            before_injection = package_tree_sha256([site])
            (site / "sitecustomize.py").write_text("INJECTED = True\n", encoding="utf-8")
            after_injection = package_tree_sha256([site])
        self.assertNotEqual(first, second)
        self.assertNotEqual(before_injection, after_injection)


if __name__ == "__main__":
    unittest.main()
