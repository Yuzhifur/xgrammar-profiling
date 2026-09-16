/*!
 * Copyright (c) 2026 by Contributors
 * \file xgrammar/profiling.h
 * \brief Internal data structures for the private profiling API.
 *
 * This header is intentionally not installed.  The profiling surface is a testing-only
 * implementation detail and is not part of XGrammar's supported C++ API.
 */
#ifndef XGRAMMAR_PROFILING_H_
#define XGRAMMAR_PROFILING_H_

#include <array>
#include <cstddef>
#include <cstdint>

#ifndef XGRAMMAR_PROFILE_DISABLE_RULE_LEVEL_CACHE
#define XGRAMMAR_PROFILE_DISABLE_RULE_LEVEL_CACHE 0
#endif

#ifndef XGRAMMAR_PROFILE_DISABLE_REPETITION_COMPRESSION
#define XGRAMMAR_PROFILE_DISABLE_REPETITION_COMPRESSION 0
#endif

#ifndef XGRAMMAR_ENABLE_PROFILING_API
#define XGRAMMAR_ENABLE_PROFILING_API 0
#endif

#ifndef XGRAMMAR_ENABLE_PROFILING_STATS
#define XGRAMMAR_ENABLE_PROFILING_STATS 0
#endif

namespace xgrammar {

inline constexpr std::size_t kRuleLevelCacheShardCount = 16;

/*! \brief Point-in-time rule-level cache state and optional diagnostic counters. */
struct RuleLevelCacheProfilingSnapshot {
  std::size_t max_bytes = 0;
  std::size_t bytes = 0;
  std::size_t entries = 0;
  std::array<std::size_t, kRuleLevelCacheShardCount> shard_bytes{};
  std::array<std::size_t, kRuleLevelCacheShardCount> shard_entries{};

  // These values remain zero in builds where XGRAMMAR_ENABLE_PROFILING_STATS is disabled.
  std::uint64_t lookups = 0;
  std::uint64_t misses = 0;
  std::uint64_t perfect_hits = 0;
  std::uint64_t basic_hits = 0;
  std::uint64_t same_compile_hits = 0;
  std::uint64_t prior_compile_hits = 0;
  std::uint64_t insertions = 0;
  std::uint64_t duplicate_insertions = 0;
  std::uint64_t oversized_rejections = 0;
  std::uint64_t evictions = 0;
  std::array<std::uint64_t, kRuleLevelCacheShardCount> shard_evictions{};
};

}  // namespace xgrammar

#endif  // XGRAMMAR_PROFILING_H_
