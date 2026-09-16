import unittest

from xgrammar_profile.cache_workload import (
    exact_repeat_stream,
    generate_stream,
    generate_tool,
    stream_fingerprint,
    validation_text,
)


class CacheWorkloadTests(unittest.TestCase):
    def test_stream_is_deterministic_and_request_zero_is_new(self):
        first = generate_stream(tools_per_request=10, seen_before_fraction=0.5, requests=4, seed=12)
        second = generate_stream(
            tools_per_request=10, seen_before_fraction=0.5, requests=4, seed=12
        )
        self.assertEqual(stream_fingerprint(first), stream_fingerprint(second))
        self.assertEqual(first[0].realized_seen_before_fraction, 0.0)
        self.assertEqual(first[0].target_seen_before_fraction, 0.0)
        self.assertEqual(first[1].realized_seen_before_fraction, 0.5)
        self.assertEqual(len(set(first[1].tool_ids)), 10)

    def test_zero_reuse_never_repeats_identity(self):
        stream = generate_stream(tools_per_request=7, seen_before_fraction=0, requests=5, seed=3)
        ids = [tool_id for request in stream for tool_id in request.tool_ids]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(request.realized_seen_before_fraction == 0 for request in stream))

    def test_tools_vary_shapes_and_have_strict_openai_shape(self):
        tools = [generate_tool(tool_id) for tool_id in range(12)]
        self.assertEqual(len({tool["function"]["name"] for tool in tools}), 12)
        self.assertTrue(all(tool["type"] == "function" for tool in tools))
        self.assertTrue(all(tool["function"]["parameters"]["type"] == "object" for tool in tools))

    def test_exact_repeat_control_is_byte_identical(self):
        stream = exact_repeat_stream(tools_per_request=5, requests=5, seed=8)
        self.assertEqual([request.tools for request in stream[1:]], [stream[0].tools] * 4)
        self.assertEqual(
            [request.realized_seen_before_fraction for request in stream], [0.0, 1.0, 1.0, 1.0, 1.0]
        )

    def test_qwen3_validation_literal_preserves_required_outer_spaces(self):
        text = validation_text(7)
        self.assertIn('{"name": "profile_tool_00000007", "arguments": ', text)
        self.assertTrue(text.startswith("<tool_call>\n"))
        self.assertTrue(text.endswith("\n</tool_call>"))


if __name__ == "__main__":
    unittest.main()
