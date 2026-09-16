/*!
 *  Copyright (c) 2024 by Contributors
 * \file xgrammar/testing.h
 * \brief The header testing utilities.
 */
#ifndef XGRAMMAR_TESTING_H_
#define XGRAMMAR_TESTING_H_

#include <dlpack/dlpack.h>
#include <xgrammar/xgrammar.h>

#include <cstdint>
#include <string>
#include <vector>

#include "profiling.h"

namespace xgrammar {

std::string PrintTokenByIds(
    const std::vector<int32_t>& token_ids, const TokenizerInfo& tokenizer_info, int max_print_num
);

Grammar _EBNFToGrammarNoNormalization(
    const std::string& ebnf_string, const std::string& root_rule_name
);

std::string _PrintGrammarFSMs(const Grammar& grammar);

/*! \brief Return the profiling CMake configuration as JSON. Always available for discovery. */
std::string Testing_GetProfilingBuildConfigJSON();

#if XGRAMMAR_ENABLE_PROFILING_API
/*! \brief Clear only the rule-level cache, preserving the exact whole-grammar LRU. */
void Testing_ClearRuleLevelCache(GrammarCompiler* compiler);

int64_t Testing_GetRuleLevelCacheSizeBytes(const GrammarCompiler& compiler);

int64_t Testing_GetGrammarLevelCacheSizeBytes(const GrammarCompiler& compiler);

/*! \brief Return cache state and diagnostic counters as JSON. */
std::string Testing_GetProfilingStatsJSON(GrammarCompiler* compiler);

void Testing_ResetProfilingStats(GrammarCompiler* compiler);

/*! \brief Return compiled grammar structure counts as JSON. */
std::string Testing_GetCompiledGrammarStatsJSON(const CompiledGrammar& compiled_grammar);
#endif

}  // namespace xgrammar

#endif  // XGRAMMAR_TESTING_H_
