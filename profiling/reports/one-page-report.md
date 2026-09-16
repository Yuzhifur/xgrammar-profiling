# XGrammar v0.2.7 profiling: one-page summary

> **TBD draft template—not a result.** Replace every bracketed field from the verified comprehensive
> report before publication.

This study measured two optimizations in XGrammar v0.2.7: Cross-Grammar Cache and Repetition State
Compression. These are current XGrammar-2 mechanisms, replacing terminology from an older slide
deck that does not map cleanly onto independently removable v0.2.7 features. We measured XGrammar’s
CPU grammar compilation and limited matcher replay. We did not load a language model, use a GPU, or
measure end-to-end serving, so the findings must not be presented as an LLM generation speedup.

The cache experiment used three versions. `rule-off` removed rule-level lookup and reuse while
leaving the exact whole-grammar cache intact. `intra-only` allowed structurally identical rules to
share work inside one compilation, then cleared the rule cache before the next request. `full`
preserved entries across requests. This design separates same-grammar deduplication from genuine
reuse of structures compiled for earlier requests. Controlled streams varied tool count and the
fraction of tools seen in any prior request; frozen BFCL schemas provided a smaller realistic
compile-success check. BFCL supplied no matcher oracle. Whole streams, rather than their dependent
individual requests, were the independent samples.

For same-compile reuse, the paired median speedup was [RATIO]× ([LOW]–[HIGH] 95% interval), or a
[PERCENT]% compile-time reduction, in [WORKLOAD SCOPE]. For additional earlier-request reuse, it was
[RATIO]× ([LOW]–[HIGH]), with benefit [DESCRIBE TREND] as measured prior-compile hits increased.
[STATE ANY NOVEL-STRUCTURE REGRESSION.] Peak resident memory changed by [RESULT], while the rule and
whole-grammar cache portions used [RESULT] and [RESULT]. The pinned BFCL schemas [DID/DID NOT]
compile successfully and [DID/DID NOT] show the same timing direction across arms; this secondary
stratum was kept out of controlled-workload aggregates. On separate controlled traces, external
token bitmasks, token acceptance, and termination behavior matched across every qualified cache
arm.

The repetition experiment compared normal compression with XGrammar’s existing explicit legacy
expansion while keeping repetition memoization and other optimizations unchanged. It tested JSON
strings, JSON arrays, and regular expressions at 127, 128, 129, and 130, then up to 1,000,000.
Direct string/regex lowerings diverged at 129; JSON arrays lower their first item separately and
diverged at user-facing `maxItems=130`.
At [BOUND/WORKLOAD], compression changed compilation time from [VALUE] to [VALUE], peak resident
memory from [VALUE] to [VALUE], and compiled size from [VALUE] to [VALUE]. Explicit expansion
[TIMED OUT/HIT THE 6 GiB GUARD/COMPLETED] for [CASES]; such outcomes are reported as censored rather
than assigned invented measurements. Repetition acceptance and boundary checks matched for every
qualified case.

All authoritative measurements came from exact commit [IMPLEMENTATION COMMIT] on one dedicated-CPU
Ubuntu droplet, using the pinned Qwen3-0.6B tokenizer assets and no model weights. Variants ran in
fresh processes, one worker at a time, in randomized paired order. Ordinary cells had [N]–[N]
independent blocks under a frozen confidence-interval stopping rule. Hardware counters and sampling
were [AVAILABLE/UNAVAILABLE]; diagnostic data never supplied authoritative latency.

The practical conclusion is [ONE-SENTENCE CACHE CONCLUSION]. [ONE-SENTENCE REPETITION CONCLUSION].
Results are specific to this XGrammar commit, tokenizer, workloads, cache budgets, CPU, and cloud
environment. The comprehensive report contains correctness evidence, uncertainty, censored cases,
raw-record links, and exact reproduction commands.
