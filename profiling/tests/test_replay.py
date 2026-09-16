import unittest

from xgrammar_profile.replay import profiling_snapshot


class FakeCompiler:
    def get_rule_cache_size_bytes(self):
        return 11

    def get_grammar_cache_size_bytes(self):
        return 22

    def get_profiling_stats(self):
        return {"hits": 3}


class FakeCompiled:
    def get_compiled_grammar_stats(self):
        return {"rules": 4}


class ReplayTests(unittest.TestCase):
    def test_timing_snapshot_is_lightweight(self):
        result = profiling_snapshot(FakeCompiler(), FakeCompiled(), detailed=False)
        self.assertEqual(result, {"rule_cache_size_bytes": 11, "grammar_cache_size_bytes": 22})

    def test_diagnostic_snapshot_includes_structure_walk(self):
        result = profiling_snapshot(FakeCompiler(), FakeCompiled(), detailed=True)
        self.assertEqual(result["profiling_stats"], {"hits": 3})
        self.assertEqual(result["compiled_grammar_stats"], {"rules": 4})


if __name__ == "__main__":
    unittest.main()
