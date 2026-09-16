"""Focused tests for the private profiling controls and diagnostics."""

import pytest

import xgrammar as xgr

BUILD_CONFIG = xgr.testing.get_profiling_build_config()
HAS_PROFILING_API = BUILD_CONFIG["XGRAMMAR_ENABLE_PROFILING_API"]
HAS_PROFILING_STATS = BUILD_CONFIG["XGRAMMAR_ENABLE_PROFILING_STATS"]
RULE_CACHE_DISABLED = BUILD_CONFIG["XGRAMMAR_PROFILE_DISABLE_RULE_LEVEL_CACHE"]
REPEAT_COMPRESSION_DISABLED = BUILD_CONFIG["XGRAMMAR_PROFILE_DISABLE_REPETITION_COMPRESSION"]


def test_profiling_build_config_is_self_consistent():
    assert set(BUILD_CONFIG) == {
        "XGRAMMAR_PROFILE_DISABLE_RULE_LEVEL_CACHE",
        "XGRAMMAR_PROFILE_DISABLE_REPETITION_COMPRESSION",
        "XGRAMMAR_ENABLE_PROFILING_API",
        "XGRAMMAR_ENABLE_PROFILING_STATS",
    }
    assert all(type(value) is bool for value in BUILD_CONFIG.values())
    assert not HAS_PROFILING_STATS or HAS_PROFILING_API


@pytest.mark.skipif(not HAS_PROFILING_API, reason="private profiling API is not built")
def test_rule_only_clear_preserves_exact_grammar_cache():
    tokenizer_info = xgr.TokenizerInfo(["a", "b", "ab", "ba"])
    compiler = xgr.GrammarCompiler(
        tokenizer_info, max_threads=1, cache_enabled=True, cache_limit_bytes=4 * 1024 * 1024
    )
    grammar = 'root ::= item item\nitem ::= "a" | "b"'
    compiler.compile_grammar(grammar)

    grammar_bytes = xgr.testing.get_grammar_cache_size_bytes(compiler)
    rule_bytes = xgr.testing.get_rule_cache_size_bytes(compiler)
    assert grammar_bytes > 0
    assert compiler.get_cache_size_bytes() == grammar_bytes + rule_bytes
    if RULE_CACHE_DISABLED:
        assert rule_bytes == 0
    else:
        assert rule_bytes > 0

    xgr.testing.clear_rule_level_cache(compiler)
    assert xgr.testing.get_rule_cache_size_bytes(compiler) == 0
    assert xgr.testing.get_grammar_cache_size_bytes(compiler) == grammar_bytes

    # This is an exact-LRU hit, so it must not repopulate the rule cache.
    compiler.compile_grammar(grammar)
    assert xgr.testing.get_grammar_cache_size_bytes(compiler) == grammar_bytes
    assert xgr.testing.get_rule_cache_size_bytes(compiler) == 0


@pytest.mark.skipif(not HAS_PROFILING_API, reason="private profiling API is not built")
def test_compiled_structure_stats_report_repetition_ablation():
    tokenizer_info = xgr.TokenizerInfo(["a", "aa"])
    compiler = xgr.GrammarCompiler(tokenizer_info, max_threads=1, cache_enabled=False)
    compiled = compiler.compile_grammar('root ::= "a"{0, 129}')
    stats = xgr.testing.get_compiled_grammar_stats(compiled)

    assert stats["rule_count"] > 0
    assert stats["grammar_expression_count"] > 0
    assert stats["complete_fsm_state_count"] > 0
    assert stats["complete_fsm_edge_count"] > 0
    assert stats["adaptive_mask_entry_count"] == stats["scannable_state_count"]
    assert stats["memory_size_bytes"] == compiled.memory_size_bytes
    assert stats["compact_repeat_expression_count"] == (0 if REPEAT_COMPRESSION_DISABLED else 1)

    matcher = xgr.GrammarMatcher(compiled, terminate_without_stop_token=True)
    assert matcher.accept_string("a" * 129)
    assert matcher.is_terminated()

    # JSON arrays lower the first item separately, so their user-facing compression
    # transition is maxItems 129 -> 130 rather than 128 -> 129.
    array_129 = compiler.compile_json_schema(
        {"type": "array", "items": {"type": "integer"}, "maxItems": 129}
    )
    array_130 = compiler.compile_json_schema(
        {"type": "array", "items": {"type": "integer"}, "maxItems": 130}
    )
    count_129 = xgr.testing.get_compiled_grammar_stats(array_129)["compact_repeat_expression_count"]
    count_130 = xgr.testing.get_compiled_grammar_stats(array_130)["compact_repeat_expression_count"]
    if REPEAT_COMPRESSION_DISABLED:
        assert (count_129, count_130) == (0, 0)
    else:
        assert (count_129, count_130) == (0, 1)


@pytest.mark.skipif(not HAS_PROFILING_STATS, reason="diagnostic profiling stats are not built")
def test_diagnostic_cache_counters_and_reset():
    tokenizer_info = xgr.TokenizerInfo(["a", "b", "ab", "ba"])
    compiler = xgr.GrammarCompiler(
        tokenizer_info, max_threads=1, cache_enabled=True, cache_limit_bytes=4 * 1024 * 1024
    )
    grammar = 'root ::= item item\nitem ::= "a" | "b"'
    compiler.compile_grammar(grammar)
    compiler.compile_grammar(grammar)

    stats = xgr.testing.get_profiling_stats(compiler)
    assert stats["stats_enabled"] is True
    assert stats["fsm_hash_time_ns"] >= 0
    assert stats["adaptive_mask_resolution_time_ns"] >= 0
    grammar_stats = stats["grammar_level_cache"]
    assert grammar_stats["lookups"] == 2
    assert grammar_stats["misses"] == 1
    assert grammar_stats["hits"] == 1
    assert grammar_stats["entries"] == 1

    rule_stats = stats["rule_level_cache"]
    if RULE_CACHE_DISABLED:
        assert rule_stats["enabled"] is False
    else:
        assert rule_stats["enabled"] is True
        assert rule_stats["lookups"] > 0
        assert rule_stats["successful_insertions"] > 0
        assert rule_stats["hits"] == (rule_stats["perfect_hits"] + rule_stats["basic_hits"])
        assert rule_stats["hits"] == (
            rule_stats["same_compile_hits"] + rule_stats["prior_compile_hits"]
        )

    xgr.testing.reset_profiling_stats(compiler)
    reset_stats = xgr.testing.get_profiling_stats(compiler)
    assert reset_stats["grammar_level_cache"]["lookups"] == 0
    assert reset_stats["grammar_level_cache"]["misses"] == 0
    assert reset_stats["grammar_level_cache"]["entries"] == 1
    if not RULE_CACHE_DISABLED:
        assert reset_stats["rule_level_cache"]["lookups"] == 0
        assert reset_stats["rule_level_cache"]["entries"] > 0


@pytest.mark.skipif(
    not HAS_PROFILING_STATS or RULE_CACHE_DISABLED,
    reason="rule-cache origin counters are not built",
)
def test_rule_cache_hit_origin_distinguishes_same_and_prior_compile():
    tokenizer_info = xgr.TokenizerInfo(["a", "b", "ab", "ba"])

    same_compile = xgr.GrammarCompiler(tokenizer_info, max_threads=1, cache_enabled=True)
    same_compile.compile_grammar(
        "root ::= one two three\none ::= [ab]+\ntwo ::= [ab]+\nthree ::= [ab]+"
    )
    same_stats = xgr.testing.get_profiling_stats(same_compile)["rule_level_cache"]
    assert same_stats["same_compile_hits"] > 0
    assert same_stats["prior_compile_hits"] == 0

    prior_compile = xgr.GrammarCompiler(tokenizer_info, max_threads=1, cache_enabled=True)
    prior_compile.compile_grammar('root ::= item item\nitem ::= "a" | "b"')
    xgr.testing.reset_profiling_stats(prior_compile)
    prior_compile.compile_grammar('root ::= renamed renamed\nrenamed ::= "a" | "b"')
    prior_stats = xgr.testing.get_profiling_stats(prior_compile)["rule_level_cache"]
    assert prior_stats["prior_compile_hits"] > 0
    assert prior_stats["same_compile_hits"] == 0
