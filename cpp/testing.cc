/*!
 *  Copyright (c) 2024 by Contributors
 * \file xgrammar/testing.cc
 */
#include "testing.h"

#include <picojson.h>
#include <xgrammar/xgrammar.h>

#include <algorithm>
#include <cstdint>
#include <sstream>
#include <string>
#include <unordered_set>
#include <vector>

#include "compiled_grammar_impl.h"
#include "grammar_impl.h"
#include "grammar_parser.h"
#include "support/encoding.h"

namespace xgrammar {

std::string PrintTokenByIds(
    const std::vector<int32_t>& token_ids, const TokenizerInfo& tokenizer_info, int max_print_num
) {
  std::stringstream ss;
  const auto& sorted_decoded_vocab = tokenizer_info.GetDecodedVocab();
  ss << "[";
  int print_num = std::min(static_cast<int>(token_ids.size()), max_print_num);
  for (int i = 0; i < print_num; ++i) {
    ss << "#" << token_ids[i] << " <" << EscapeString(sorted_decoded_vocab[token_ids[i]]) << ">";
    if (i < print_num - 1) {
      ss << ", ";
    }
  }
  if (static_cast<int>(token_ids.size()) > max_print_num) {
    ss << ", ...";
  }
  ss << "]";
  return ss.str();
}

Grammar _EBNFToGrammarNoNormalization(
    const std::string& ebnf_string, const std::string& root_rule_name
) {
  return ParseEBNF(ebnf_string, root_rule_name);
}

std::string _PrintGrammarFSMs(const Grammar& grammar) {
  XGRAMMAR_CHECK(static_cast<int>(grammar->per_rule_fsms.size()) == grammar->NumRules())
      << "The grammar has no per-rule FSMs; build them first";
  std::string result;
  for (int i = 0; i < grammar->NumRules(); i++) {
    result += "Rule " + std::to_string(i) + ": " + grammar->GetRule(i).name + ", FSM: ";
    if (grammar->per_rule_fsms[i].has_value()) {
      result += grammar->per_rule_fsms[i]->GetFsm().ToString();
    } else {
      result += "None";
    }
    result += "\n";
  }
  return result;
}

std::string Testing_GetProfilingBuildConfigJSON() {
  picojson::object result;
  result["XGRAMMAR_PROFILE_DISABLE_RULE_LEVEL_CACHE"] =
      picojson::value(static_cast<bool>(XGRAMMAR_PROFILE_DISABLE_RULE_LEVEL_CACHE));
  result["XGRAMMAR_PROFILE_DISABLE_REPETITION_COMPRESSION"] =
      picojson::value(static_cast<bool>(XGRAMMAR_PROFILE_DISABLE_REPETITION_COMPRESSION));
  result["XGRAMMAR_ENABLE_PROFILING_API"] =
      picojson::value(static_cast<bool>(XGRAMMAR_ENABLE_PROFILING_API));
  result["XGRAMMAR_ENABLE_PROFILING_STATS"] =
      picojson::value(static_cast<bool>(XGRAMMAR_ENABLE_PROFILING_STATS));
  return picojson::value(std::move(result)).serialize();
}

#if XGRAMMAR_ENABLE_PROFILING_API
std::string Testing_GetCompiledGrammarStatsJSON(const CompiledGrammar& compiled_grammar) {
  const auto* compiled_impl = compiled_grammar.ImplPtr();
  XGRAMMAR_CHECK(compiled_impl != nullptr) << "CompiledGrammar must not be null";
  const auto& grammar = compiled_impl->grammar;
  const auto* grammar_impl = grammar.ImplPtr();
  XGRAMMAR_CHECK(grammar_impl != nullptr) << "Compiled grammar contains a null Grammar";

  int64_t repeat_expression_count = 0;
  for (int32_t expr_id = 0; expr_id < grammar_impl->NumGrammarExprs(); ++expr_id) {
    if (grammar_impl->GetGrammarExpr(expr_id).type == Grammar::Impl::GrammarExprType::kRepeat) {
      ++repeat_expression_count;
    }
  }

  int64_t per_rule_fsm_state_count = 0;
  int64_t per_rule_fsm_edge_count = 0;
  int64_t scannable_state_count = 0;
  for (const auto& optional_fsm : grammar_impl->per_rule_fsms) {
    if (!optional_fsm.has_value()) {
      continue;
    }
    const auto& fsm = optional_fsm.value();
    per_rule_fsm_state_count += fsm.GetNodeNum();
    per_rule_fsm_edge_count += fsm.GetEdgeNum();
    std::unordered_set<int> reachable_states;
    fsm.GetFsm().GetReachableStates(&reachable_states);
    for (int state : reachable_states) {
      if (fsm.GetFsm().IsScanableState(state)) {
        ++scannable_state_count;
      }
    }
  }

  int64_t accepted_token_classifications = 0;
  int64_t rejected_token_classifications = 0;
  int64_t uncertain_token_classifications = 0;
  const int64_t vocab_size = compiled_impl->tokenizer_info.GetVocabSize();
  for (const auto& [state, mask] : compiled_impl->adaptive_token_mask_cache) {
    (void)state;
    const int64_t uncertain = static_cast<int64_t>(mask.uncertain_indices.size());
    int64_t accepted = 0;
    int64_t rejected = 0;
    switch (mask.store_type) {
      case AdaptiveTokenMask::StoreType::kAccepted:
        accepted = static_cast<int64_t>(mask.accepted_indices.size());
        rejected = vocab_size - accepted - uncertain;
        break;
      case AdaptiveTokenMask::StoreType::kRejected:
        rejected = static_cast<int64_t>(mask.rejected_indices.size());
        accepted = vocab_size - rejected - uncertain;
        break;
      case AdaptiveTokenMask::StoreType::kAcceptedBitset:
        accepted = mask.accepted_bitset.Count();
        rejected = vocab_size - accepted - uncertain;
        break;
    }
    XGRAMMAR_DCHECK(accepted >= 0 && rejected >= 0 && uncertain >= 0);
    accepted_token_classifications += accepted;
    rejected_token_classifications += rejected;
    uncertain_token_classifications += uncertain;
  }

  picojson::object result;
  result["rule_count"] = picojson::value(static_cast<int64_t>(grammar_impl->NumRules()));
  result["grammar_expression_count"] =
      picojson::value(static_cast<int64_t>(grammar_impl->NumGrammarExprs()));
  result["complete_fsm_state_count"] =
      picojson::value(static_cast<int64_t>(grammar_impl->complete_fsm.NumStates()));
  result["complete_fsm_edge_count"] =
      picojson::value(static_cast<int64_t>(grammar_impl->complete_fsm.GetNumEdges()));
  result["per_rule_fsm_state_count"] = picojson::value(per_rule_fsm_state_count);
  result["per_rule_fsm_edge_count"] = picojson::value(per_rule_fsm_edge_count);
  result["scannable_state_count"] = picojson::value(scannable_state_count);
  result["adaptive_mask_entry_count"] =
      picojson::value(static_cast<int64_t>(compiled_impl->adaptive_token_mask_cache.size()));
  result["accepted_token_classifications"] = picojson::value(accepted_token_classifications);
  result["rejected_token_classifications"] = picojson::value(rejected_token_classifications);
  result["uncertain_token_classifications"] = picojson::value(uncertain_token_classifications);
  result["compact_repeat_expression_count"] = picojson::value(repeat_expression_count);
  result["memory_size_bytes"] =
      picojson::value(static_cast<int64_t>(compiled_grammar.MemorySizeBytes()));
  return picojson::value(std::move(result)).serialize();
}
#endif

}  // namespace xgrammar
