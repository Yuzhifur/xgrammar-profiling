# XGrammar v0.2.7 profiling: Cross-Grammar Cache and Repetition State Compression

> Status: **TBD results template.** Do not publish until every bracketed placeholder is replaced, all
> correctness gates pass, and `verify-reports` succeeds against the authoritative run directory.

## Executive summary

[Summarize what was tested, the two principal latency/memory findings, correctness status, and the
most important limitation in plain language. State explicitly that this is library-level CPU work,
not end-to-end LLM inference.]

## 1. What profiling means in this study

[Explain benchmark versus profile versus ablation versus correctness validation. Describe grammar
compilation and matcher replay without assuming prior profiling knowledge.]

## 2. Scope and terminology

### 2.1 Selected v0.2.7 optimizations

- Cross-Grammar Cache (`RuleLevelCache`)
- Repetition State Compression (`RepetitionRangeExpander`, `kRepeat`)

[Explain why these current XGrammar-2 mechanisms replace the first two labels from the older slide
deck. Identify what is out of scope: model weights, GPU work, inference, and universal speedup
claims.]

### 2.2 Research questions and hypotheses

[Restate the three cache comparisons and two repetition arms. List hypotheses written before the
run and distinguish them from findings.]

## 3. Provenance and reproducibility

### 3.1 Source and build identity

| Item | Recorded value | Evidence |
|---|---|---|
| XGrammar release base | `82505d0d987c36a4209fb3d8571cf6b0f28b5acd` | [manifest path] |
| Profiling implementation commit | [40-hex commit] | [manifest path] |
| Frozen config SHA-256 | [hash] | [path] |
| Production extension SHA-256 | [hash] | [path] |
| No-rule-cache extension SHA-256 | [hash] | [path] |
| No-repeat-compression extension SHA-256 | [hash] | [path] |
| Optional sampling-diagnostic extension SHA-256 | [hash or unavailable] | [path/preflight] |

[Record compiler, linker, CMake, Ninja, Python, dependency snapshot, CMake flags, dirty-patch hash,
and proof that timing builds had stats disabled.]

### 3.2 Data identity

[Record Qwen tokenizer repository/revision/manifest hash, BFCL repository/40-hex revision/manifest
hash, accepted and rejected counts, and confirmation that no model weights were acquired.]

### 3.3 Droplet and operating environment

[Record provider plan/region, dedicated-vCPU claim as provider metadata, kernel/distribution,
virtualization, CPU topology/affinity, memory/swap, disk, mitigations/governor visibility, cgroup
mode, steal-time rule, and network-offline environment. Do not claim control the VM did not expose.]

## 4. Experimental design

### 4.1 Cross-Grammar Cache ablation

| Arm | Exact grammar LRU | Same-compile rule reuse | Earlier-request rule reuse |
|---|---:|---:|---:|
| `rule-off` | on | off | off |
| `intra-only` | on | on | off (cleared before request) |
| `full` | on | on | on |

[Explain why cache on/off alone is confounded. Define seen-before fraction against all prior
requests and distinguish exact tool identity from structural cache hits. State the unit of
replication is a whole stream worker.]

### 4.2 Repetition State Compression ablation

[Describe production versus legacy explicit expansion, retained memoization, the family-specific
boundaries (128/129 for direct regex/string lowering and user-facing 129/130 for JSON arrays),
families, bounds, and fresh cache-disabled compiler per case.]

### 4.3 Workloads and measurements

[List controlled matrix, BFCL traces, exact-repeat/budget/thread controls actually run, repetition
core/focused cases, replay traces, compile timing boundary, sampled-plus-retained-endpoint peak RSS
method, compiled size, and diagnostic counters. Explain that the exact whole-grammar cache uses a
soft target that may overshoot by one inserted grammar. Note any predeclared subset that pilot
resource gates deferred.]

### 4.4 Sampling and statistics

[Record warmup, independent block counts, randomized paired order, paired log-speedup bootstrap,
CI stopping rule, cluster/stream unit, outlier retention, steal reruns, and handling of censored
timeout/RSS outcomes. Define speedup numerator and denominator each time.]

## 5. Correctness qualification

| Gate | Result | Evidence |
|---|---|---|
| Pristine C++ suite | [pass/fail/counts] | [path/query] |
| Applicable Python suite | [pass/fail/counts] | [path/query] |
| Full semantic suite on every timing variant | [pass/expected-disabled/counts] | [path/query] |
| Profiling hooks on five API/diagnostic variants | [pass/fail/counts] | [path/query] |
| Controlled-cache differential bitmasks/replay | [result] | [path/query] |
| BFCL production compile validation | [result] | [path/query] |
| Repetition boundaries/properties | [result] | [path/query] |
| Process memory/timeout guard | [result] | [path/query] |

[List narrowly allowlisted expected-disabled mechanism tests. Describe any mismatch and resolution.
If an unexplained semantic mismatch remains, stop: no performance section is reportable.]

## 6. Cross-Grammar Cache results

### 6.1 Same-compile structural reuse

[Table/figure: `rule-off / intra-only`, latency reduction, uncertainty, peak RSS, compiled size,
measured current-compile hits. Discuss request 0 separately. Cite analysis query and raw files.]

### 6.2 Additional earlier-request reuse

[Table/figure: `intra-only / full`, latency reduction, uncertainty, prior-compile hits, target and
realized reuse, novel-structure overhead/regressions.]

### 6.3 Total effect, cache budget, and saturation

[Report `rule-off / full`, separate rule/exact cache bytes, configured split, evictions, stream
curves, budget sweep, exact-repeat control, and any gated 500-tool result.]

### 6.4 External-validity compilation and controlled matcher replay

[Report BFCL as a pinned compile-success and secondary cross-arm timing check; keep its stratum out
of controlled-workload aggregate claims. BFCL does not supply a matcher oracle or
bitmask-equivalence claim. Separately report controlled-trace bitmask, token-acceptance,
termination, replay-cost, and accepted/rejected/uncertain representation evidence.]

## 7. Repetition State Compression results

### 7.1 Family-specific boundary behavior at 127/128/129/130

[Table/figure for each core family: compile latency, peak RSS, compiled bytes, FSM/rule/repeat
counts, and correctness. Direct string/regex lowerings transition at 128/129; JSON arrays lower the
first item separately and transition at user-facing `maxItems=129/130`.]

### 7.2 Scaling to large upper bounds

[Plot successful cases on appropriate axes. Preserve 6 GiB guard and timeout outcomes as censored
observations. Do not substitute the limit as a measured time or memory value.]

### 7.3 Focused shapes and matcher replay

[Report nested arrays, exact/nonzero-min regex, min/max arrays, and manageable replay lengths.]

## 8. Where costs occur

[Summarize diagnostic counters and, only if preflight succeeded, hardware counters or cpu-clock
sampling. Identify timing and diagnostic builds separately. If unavailable, say so directly.]

## 9. Interpretation

### 9.1 Measured observations

[Facts directly supported by this run.]

### 9.2 Causal interpretation

[Claims justified by the ablation, including possible confounders.]

### 9.3 Relationship to prior XGrammar-2 claims

[Compare terminology/direction only. Never present paper/blog numbers as measurements from this
droplet.]

## 10. Limitations and threats to validity

[At minimum: one release/commit, one tokenizer, synthetic workload construction, limited BFCL
traces, one VM/provider/CPU, virtualization noise, optional PMU availability, CPU-only library
scope, finite cache budget, instrumentation separation, and censored extremes.]

## 11. Conclusions and recommendations

[Answer both research questions, state where each optimization matters, call out regressions or
thresholds, and limit generalization to the tested environment.]

## 12. Exact reproduction commands

[Copy the actual immutable commit, BFCL revision, variant root, qualification/pilot/frozen/run IDs,
and commands from `profiling/DROPLET_RUNBOOK.md`. Include report-verification output.]

## 13. Evidence index

| Claim/table/figure | Analysis query or script | Summary artifact | Raw records |
|---|---|---|---|
| [item] | [query] | [path + SHA-256] | [paths + SHA-256] |

## Appendix A. Full configuration and manifests

[Embed or link frozen config, validation manifest, variant manifests, tokenizer/BFCL manifests, and
machine metadata.]

## Appendix B. Complete result tables

[Include all cells, including null/unavailable/censored outcomes and sample counts.]

## Appendix C. Deviations and incident log

[List pilot-authorized operational changes, steal reruns, interrupted attempts, unavailable
features, and deviations. Write “None” only after checking the retained directories.]
