# XGrammar v0.2.7 profiling plan, revision 2

Status: implementation-ready after one review iteration  
Target source: XGrammar v0.2.7 release commit `82505d0d987c36a4209fb3d8571cf6b0f28b5acd`  
Primary execution host: dedicated-CPU Ubuntu droplet, 8 GiB RAM, 25 GiB disk  
Development host: this Mac; development and correctness results only

## 1. Decision summary

This study will profile two optimizations that are present and independently ablatable in the
v0.2.7 code:

1. **Cross-Grammar Cache**, called `RuleLevelCache` or "crossing cache" in parts of the source.
2. **Repetition State Compression**, implemented by `RepetitionRangeExpander`, `kRepeat`, and
   repeat-aware FSM/Earley logic.

The older slide terms—Adaptive Token Mask Cache, Context Expansion, Persistent Execution Stack,
and Optimization Passes—do not map to four clean switches in v0.2.7. Adaptive token masks and the
Earley parser are now foundational; disabling them would require inventing a different engine.
Context expansion and persistent-stack terminology describes the older architecture, while the
optimizer combines several passes whose effects cannot be separated by one existing switch. The
two selected optimizations are current XGrammar-2 terms, have identifiable source boundaries, and
answer useful questions on a CPU-only machine. The XGrammar-2 paper describes TagDispatch, JIT
compilation, cross-grammar caching, an Earley-based design, and repetition compression; the
official overview also identifies cross-grammar caching and repetition compression as major
optimizations ([paper](https://arxiv.org/abs/2601.04426),
[overview](https://blog.mlc.ai/2026/05/04/xgrammar-2-fast-customizable-structured-generation)).

This is a library-level study. It will not load model weights, run GPU inference, or claim an
end-to-end LLM-serving speedup.

### Source pin

Implementation begins on a branch created from the release commit, not from the moving default
branch:

```bash
git switch --create codex/profile-v0.2.7 82505d0d987c36a4209fb3d8571cf6b0f28b5acd
```

The current local `main` is `ec67a61`, one documentation-only commit after the release. The two
core source files checked during this review are byte-identical at `main` and `82505d0`, but the
experiment still pins `82505d0`. There is no local `v0.2.7` tag, so scripts must use the full commit
hash rather than assuming a tag exists.

## 2. Profiling, in plain language

"Profiling" is the process of finding where a program spends its time and memory. It is related to,
but different from, benchmarking:

- A **benchmark** says what happened: compilation took 80 ms and used 120 MiB.
- A **profile** says where the cost came from: for example, token-mask construction dominated the
  run, or explicit repetition produced thousands of states.
- An **ablation** tests cause and effect: run equivalent code with one optimization disabled, then
  compare it with the normal implementation.
- A **correctness check** establishes that the faster and slower versions accept the same language.
  A speedup is meaningless if one version produces a different token mask.

For XGrammar, the unit of work is mainly grammar compilation. XGrammar converts a JSON schema,
regular expression, EBNF grammar, or Structural Tag into parser structures and precomputed adaptive
token masks. At generation time a matcher uses those structures to decide which tokens are legal.
This project will therefore measure two phases separately:

1. **Compile/preprocess:** conversion, grammar optimization, FSM construction, and adaptive-mask
   construction.
2. **Matcher replay:** `fill_next_token_bitmask` and `accept_token` on fixed, already-tokenized
   valid and invalid traces.

The normal workflow will be:

1. Pin the code, tokenizer, inputs, build flags, and machine details.
2. Build a normal variant and variants with exactly one optimization disabled.
3. Prove semantic equivalence on targeted and existing tests.
4. Run repeatable workloads many times, with process isolation and randomized variant order.
5. Collect timing, memory, and internal mechanism counters.
6. Use a sampling profiler only on representative cases found by the measurements.
7. Preserve raw data, analyze it, and write both reports.

What to expect:

- There will not be one universal "XGrammar speedup." Results will vary by tool count, repeated
  structure, cache history, cache budget, and repetition bound.
- Cold and warm behavior can differ substantially.
- Cloud measurements contain noise even on dedicated vCPUs, so isolated repetitions and uncertainty
  intervals matter.
- An intentionally uncompressed million-element grammar may time out or reach the memory guard.
  That is a scalability result and will be reported as censored data, never as an invented duration.

## 3. Facts from v0.2.7 that determine the design

### 3.1 The mask cache is eager, not JIT

`GrammarCompilerSub::MultiThreadCompileGrammar` visits reachable scannable FSM positions and builds
adaptive masks during compilation (`cpp/grammar_compiler.cc`). No separately configurable lazy/JIT
mask-generation pool is present in v0.2.7. "JIT ablation" is therefore out of scope.

### 3.2 The rule-level cache has two kinds of reuse

`GrammarMatcherForTokenMaskCache::GetAdaptiveTokenMask` looks up and inserts `RuleLevelCache`
entries for individual scannable states. An entry inserted for an early rule can be reused by a
later structurally identical rule in the **same compile**. If the compiler persists, it can also be
reused by a later grammar in a **different compile**.

Consequently, a cold first request can benefit greatly. An on/off comparison alone cannot claim
that the entire difference is cross-request reuse. The experiment must separate:

- no rule-level reuse;
- reuse within one grammar only; and
- reuse within and across grammars.

### 3.3 Cache identity is structural

The FSM hasher normalizes state IDs and incorporates referenced-rule content. Two tools can share
cached primitive/object/array substructures even when their names and exact JSON are different.
Therefore "0% repeated tools" is not "0% cache hits." The controlled variable will be the fraction
of tools seen anywhere earlier in a stream, while measured same-compile and prior-compile hit counts
will explain the actual structural reuse.

### 3.4 The configured cache budget is split

For a finite `cache_limit_bytes`, `GrammarCompiler::Impl` assigns about one third to
`RuleLevelCache` and two thirds to the exact whole-grammar LRU:

| Configured total | Rule-level share | Whole-grammar share |
|---:|---:|---:|
| 64 MiB | about 21.3 MiB | about 42.7 MiB |
| 256 MiB | about 85.3 MiB | about 170.7 MiB |
| 512 MiB | about 170.7 MiB | about 341.3 MiB |

The existing `get_cache_size_bytes()` sums the two. The study will add diagnostic accessors for
each cache and will always report the configured total and its actual split. The rule-cache share
is a hard capacity. The exact-cache share is a soft target: v0.2.7 evicts ready entries before it
computes and inserts a miss, so the post-call size can exceed the target by at most one compiled
grammar entry. Diagnostics validate that implementation-specific bound rather than incorrectly
treating the exact-cache target as a hard cap.

### 3.5 Repetition compression switches when the lowered range exceeds 128

`RepetitionRangeExpanderImpl::ExpandRepetitionRange` explicitly expands bounded repetitions whose
upper bound is at most 128. Above 128 it keeps a fixed explicit portion and represents the rest with
`kRepeat`. Regex ranges and JSON `maxLength=n` map directly enough that their user-facing transition
is between 128 and 129. JSON arrays lower the first item separately and repeat the separator-plus-item
tail, so `maxItems=129` still has a lowered upper bound of 128 and the array transition is between
129 and 130. The production cost should be approximately constant with respect to a very large upper
bound, but not literally zero or perfectly flat because the fixed 128-element portion and downstream
work remain.

Identical repetition expansions are memoized before this branch. The ablation must retain that
memoization and replace only the compressed branch with the existing
`LegacyHandleRepetitionRange` path.

## 4. Research questions and comparisons

### 4.1 Cross-Grammar Cache

Primary questions:

1. How much work does the rule-level cache save by deduplicating structures inside one grammar?
2. How much additional work does persistence save across a stream of non-identical grammars?
3. How do seen-before tool fraction, structural similarity, tool count, and cache budget affect the
   result?
4. Does a mask compiled from a partial/lookahead-adjusted hit change matcher replay cost even when
   the externally visible bitmask is identical?

There will be three causal arms:

| Arm | Exact whole-grammar LRU | Rule cache during compile | Rule cache across requests |
|---|---:|---:|---:|
| `rule-off` | On | Off | Off |
| `intra-only` | On | On | Cleared immediately before every new request |
| `full` | On | On | On |

The comparisons have distinct meanings:

- `rule-off / intra-only` latency ratio = benefit of same-compile structural deduplication.
- `intra-only / full` = additional benefit of reuse from earlier requests.
- `rule-off / full` = total rule-cache benefit.

All stream requests in the primary suite are non-identical, so the exact LRU should not create a
timing hit. A separate exact-repeat control verifies that all three arms still receive the same
whole-grammar-cache benefit.

Corrected hypotheses:

- The first cold grammar can already benefit from same-compile reuse.
- At 0% tool identity reuse, primitive and common JSON structures create a nonzero hit floor.
- Additional cross-request benefit should grow with the measured seen-before fraction until the
  cache saturates or evicts useful entries.
- The full cache can regress on novel structures because hashing, lookup, copying, and locking have
  costs.
- With a finite budget, memory and hit rate depend on both cache halves and shard-level eviction.

### 4.2 Repetition State Compression

Primary question:

> How do compilation time, working memory, compiled size, and parser/FSM structure change as a
> lowered bounded repetition crosses 128 and grows to very large values?

The two arms are:

- `production`: normal threshold/compressed representation.
- `no-repeat-compression`: always use the existing legacy explicit expansion for the tested bounded
  repetitions, while preserving repetition memoization and every other optimizer.

Corrected hypotheses:

- Regex and JSON-string cases should follow the same expansion path through 128 and diverge at 129.
- JSON-array cases should follow the same path through `maxItems=129` and diverge at 130 because of
  their separator-aware lowering.
- Explicit expansion should grow roughly with the bound; production should be bounded with respect
  to the upper bound after paying its fixed-size work.
- Very large uncompressed cases may time out or cross the RSS guard.

## 5. Source changes and build variants

All controls default to off, so a normal build retains v0.2.7 behavior. They are profiling-only
CMake options, not supported public XGrammar API:

```text
XGRAMMAR_PROFILE_DISABLE_RULE_LEVEL_CACHE=OFF
XGRAMMAR_PROFILE_DISABLE_REPETITION_COMPRESSION=OFF
XGRAMMAR_ENABLE_PROFILING_API=OFF
XGRAMMAR_ENABLE_PROFILING_STATS=OFF
```

`XGRAMMAR_ENABLE_PROFILING_STATS` implies the private profiling API.

### 5.1 Rule-cache ablation

When `XGRAMMAR_PROFILE_DISABLE_RULE_LEVEL_CACHE=ON` and the user constructs
`GrammarCompiler(..., cache_enabled=True)`, the exact whole-grammar LRU remains enabled with the same
two-thirds capacity, but `GrammarCompilerSub` receives no `RuleLevelCache`. This removes rule hashing,
lookups, insertions, locks, and reuse without changing the exact cache. It must not be implemented by
passing the existing public `cache_enabled=False`, because that disables both cache levels.

The existing `cache_enabled=False` path will be used only as a semantic cross-check.

### 5.2 Intra-only hook

The private profiling API adds `clear_rule_level_cache()` without clearing the exact LRU. The
harness invokes it outside the timed interval immediately before each `intra-only` request. Entries
created during that compile remain available to later rules in the same compile.

### 5.3 Repetition ablation

When `XGRAMMAR_PROFILE_DISABLE_REPETITION_COMPRESSION=ON`,
`ExpandRepetitionRange` calls the existing legacy expansion for bounded ranges regardless of the
upper bound. The memoization in `HandleRepetitionRange` stays unchanged. The initial workload uses
bounded ranges only; large-lower unbounded ranges are excluded because they test a second behavior
and can be added later.

### 5.4 Timing and diagnostic builds

Builds used for authoritative timing:

| Build | Profiling API | Stats | Rule cache | Repeat compression |
|---|---:|---:|---:|---:|
| `production-profile` | On | Off | On | On |
| `no-rule-cache` | On | Off | Off | On |
| `no-repeat-compression` | On | Off | On | Off |

`full` and `intra-only` use the exact same `production-profile` binary. A pristine default build
with all four options off is used for test and timing sanity checks, not as a fourth primary arm.
Diagnostic builds enable stats and are never used as authoritative latency measurements.

No `no-both` performance arm is needed for the first study; it answers an interaction question but
does not improve either primary causal estimate. A `no-both` smoke build may be added only after the
two studies finish.

Every build manifest records the base commit, dirty diff SHA-256, CMake cache, compiler/linker
versions, flags, Python package tree hash, and native extension hash. Variants are installed into
separate `--target` directories that share one dependency environment, avoiding several copies of
Torch on the 25 GiB disk. Workers set `PYTHONNOUSERSITE=1` and a variant-specific `PYTHONPATH` so a
wrong extension cannot be imported silently.

## 6. Diagnostic instrumentation

Stats are compiled out of timing builds. Diagnostic builds collect enough information to explain
results, not to replace timing.

### Rule-cache stats

- lookups and misses;
- exact/perfect hits;
- basic hits requiring lookahead adaptation;
- hits from an entry inserted in the current compile;
- hits from an entry inserted in an earlier compile;
- successful insertions, duplicate insert attempts, and oversized rejected entries;
- evictions, entries, and bytes, both total and per shard;
- time spent hashing FSMs and resolving adaptive masks (including cache lookup and adaptation), in
  diagnostic runs only.

To classify hit origin, a diagnostic-only compile epoch is assigned when an actual grammar
`Compute` begins. Cache values retain their insertion epoch. All worker tasks for one compile use the
same epoch. Exact whole-grammar hits do not begin a new compile epoch.

### Whole-grammar-cache stats

- hits, misses, evictions, entries, bytes, and configured soft-capacity target;
- separate `grammar_cache_size_bytes` and `rule_cache_size_bytes` accessors.

### Compiled-structure stats

- rule and grammar-expression counts;
- FSM state and edge counts;
- scannable state count;
- adaptive-mask entry count;
- accepted, rejected, and uncertain token classifications;
- compact `kRepeat` expression count;
- `CompiledGrammar.memory_size_bytes`.

The stats API lives under `xgrammar.testing`, and tests verify hand-constructed expected counts.

## 7. Fixed inputs

### 7.1 Tokenizer

The primary tokenizer is `Qwen/Qwen3-0.6B`, revision
`c916fa4defd319b7d4e4da17604ca7338f4d99f5`. Only tokenizer assets are acquired; model weights are
not. A preparation step loads the pinned tokenizer once, creates `TokenizerInfo`, and exports:

- decoded vocabulary;
- `TokenizerInfo.dump_metadata()`;
- tokenizer configuration needed to produce replay token IDs;
- source revision and SHA-256 of every downloaded and generated file.

Benchmark subprocesses reconstruct `TokenizerInfo.from_vocab_and_metadata` from that local snapshot.
They make no network calls and do not instantiate a Hugging Face tokenizer inside the timed region.
The snapshot is generated data and remains uncommitted; its manifest is committed.

### 7.2 Structural Tag format

The cache workload uses the v0.2.7 call signature:

```python
get_model_structural_tag(
    "qwen_3",
    tools=tools,
    tool_choice="auto",
    reasoning="disabled",
    parallel_tool_calls=True,
)
```

Each resulting Structural Tag JSON string is materialized before timing. Only
`GrammarCompiler.compile_structural_tag(serialized_tag)` is inside the compilation timer.

### 7.3 Realistic schemas

BFCL is an external-validity dataset, not the controlled causal workload. The preparation command
uses the official `ShishirPatil/gorilla` source, requires an immutable 40-character revision, writes
it to `data/manifest.json`, normalizes
function definitions deterministically using BFCL's pinned `GORILLA_TO_OPENAPI` semantics for the
`OSSMODEL` target, projects the OpenAI `name`, `description`, and `parameters` fields (excluding
BFCL-only metadata such as `response`), wraps that projection as OpenAI function tools, and records
accepted/rejected entries with a reason. Its manifest binds the reviewed conversion source files,
complete mapping, explicit projection, and data revision. It refuses a branch name such as `main`.
The authoritative run uses the exact prepared snapshot; later BFCL updates cannot change it. BFCL
publishes the data under
`berkeley-function-call-leaderboard/bfcl_eval/data` in its
[repository](https://github.com/ShishirPatil/gorilla/tree/main/berkeley-function-call-leaderboard/bfcl_eval/data).

If acquisition or normalization yields too few supported tools for a declared trace, that trace is
marked unavailable before the experiment is frozen. Synthetic results remain the primary evidence;
tools will not be duplicated merely to fill a BFCL cell.

## 8. Workloads

### 8.1 Controlled rule-cache streams

Synthetic tools use OpenAI function-tool objects. A deterministic generator varies names,
property-name byte strings, enums, nesting, arrays, and primitive types while preserving known shape
families. It can therefore distinguish exact tool reuse from generic structural sharing.

The primary matrix is intentionally small enough to finish:

| Factor | Primary values |
|---|---|
| tools per request | 10, 50, 100 |
| fraction of tools seen anywhere earlier in stream | 0%, 50%, 90% |
| requests per stream | 20, fixed after pilot validation |
| total cache budget | 512 MiB |
| compiler threads | 1 |
| arms | `rule-off`, `intra-only`, `full` |

Request 0 is necessarily all-new and is labeled separately. For request `i > 0`, the generator
selects the declared fraction from the union of tools used in requests `0..i-1`; the remainder have
never appeared before. It logs both the target and realized fraction. At 0%, names and literals are
new, but JSON primitives still share structure by design.

Additional, bounded subsets:

- 500 tools: 90% seen-before, five requests, only if the pilot meets time and memory gates.
- Budget sweep: 64, 256, and 512 MiB at 100 tools and 90% seen-before.
- Thread sweep: 1, 2, 4, and `min(physical cores, 8)` at 100 tools and 50% seen-before. This is
  secondary; its result includes cache-lock overhead.
- Exact-repeat control: the same tag repeated five times. After request 0, all arms should hit the
  whole-grammar LRU.

The BFCL validation suite contains three frozen traces rather than duplicating the full factorial
grid: a 10-tool zero-reuse schema sample plus 50-tool medium-reuse and 100-tool high-reuse traces
assembled deterministically from accepted schemas. The manifest records their realized reuse and
structure statistics.

The unit of replication is an independently started **stream worker**, not each request. Requests
inside one stream are dependent observations and will not be treated as 20 independent samples.

### 8.2 Repetition-compression cases

Core families:

1. JSON string with `maxLength=n`.
2. JSON array of a primitive item with `maxItems=n`.
3. Regex `[a]{0,n}`.

Core bounds:

```text
64, 127, 128, 129, 130, 256, 1,024, 4,096, 65,536, 1,000,000
```

Focused shape cases, run at 127, 128, 129, 130, 1,024, and 65,536:

- JSON array of a nested object;
- regex exact repetition `[a]{n}`;
- bounded nonzero minimum `[a]{floor(n/2),n}`;
- JSON array with both `minItems` and `maxItems`.

Each sample uses a fresh compiler with `cache_enabled=False` and `max_threads=1`, so neither cache
level affects this study. The tokenizer snapshot is loaded before the timer. One worker executes one
case and exits, allowing reliable status and peak-memory classification.

### 8.3 Matcher replay

Replay is secondary. It uses fixed token-ID sequences generated during input preparation, not model
generation. For the cache study, replay compares grammars compiled late in the same stream under all
three arms, because partial warm hits may change the accepted/rejected/uncertain representation. For
repetition, replay covers manageable lengths and the family-specific 127/128/129/130 boundary; it
does not emit one million tokens merely because a grammar permits them.

## 9. Correctness gates

No performance result is reportable until the relevant gate passes.

### 9.1 Existing tests

1. Run the complete C++ and Python suite on the pristine default build.
2. Run semantic tests on every timing variant.
3. Maintain an explicit allowlist for tests whose sole purpose is to assert that the disabled
   mechanism stores cache entries. Do not call such failures regressions, and do not broadly skip
   files. Initial tests to inspect include:
   - `test_grammar_compiler_crossing_cache_same_grammar`;
   - `test_grammar_compiler_crossing_cache_different_grammar_with_same_fsm`;
   - `test_sharded_rule_cache_concurrent_compilation`;
   - `test_rule_level_cache_cross_grammar`;
   - cache byte/limit tests in `test_grammar_compiler.py`.
4. Replace each allowlisted mechanism test with a profiling-specific assertion of the intended
   ablation behavior.

The final validation manifest records passed, skipped, expected-disabled, and failed tests. Any
unexplained failure blocks the run.

### 9.2 Differential cache validation

For identical prepared tags and token traces:

- compare the externally returned bitmask after every `fill_next_token_bitmask`;
- compare every `accept_token` result and termination state;
- test valid and deliberately invalid completions;
- compare `rule-off`, cold `intra-only`, and warm `full` builds;
- separately record internal accepted/rejected/uncertain category counts rather than requiring them
  to be identical.

Compiled serialization is **not** a correctness gate. Cache-hit adaptation may produce a different
internal uncertain-token representation while preserving the same externally resolved bitmask.

### 9.3 Differential repetition validation

- Exhaustively enumerate a small alphabet for small ranges.
- Compare external bitmasks at every replay prefix.
- Check acceptance at `min-1`, `min`, `max`, and `max+1` for manageable bounds.
- Always include 127, 128, 129, and 130 so both direct and array-lowered threshold crossings are
  covered.
- Use randomized property cases with fixed seeds for different bodies and min/max pairs.
- For very large bounds, validate representative short prefixes and structural invariants; do not
  require a million-element boundary replay.

## 10. Measurements

### Primary metrics

- wall-clock time around the compile call using `time.perf_counter_ns()`;
- process CPU time around the same call;
- per-request and cumulative stream compile time;
- post-baseline worker RSS: the maximum of periodic process-tree samples and a mandatory final
  sample while the compiler, caches, and compiled result are still live, plus its baseline delta;
- `CompiledGrammar.memory_size_bytes`;
- success, timeout, RSS-limit termination, kernel termination, or program error.

### Secondary metrics

- `fill_next_token_bitmask` plus `accept_token` replay time;
- median and descriptive p95 time per replayed output token;
- rule-cache and exact-cache bytes after each request;
- diagnostic mechanism and structure counters from section 6.

Tag construction, JSON serialization, tokenizer loading, imports, process startup, data validation,
and result writing are outside the compile timer. They remain inside end-to-end worker wall time,
which is logged separately for operational planning but is not called XGrammar compilation time.

## 11. Process isolation, timeouts, and memory safety

Do not use `RLIMIT_AS`. It limits virtual address reservations and can kill a process because of
Python/Torch/glibc mappings rather than XGrammar resident memory.

The parent harness starts every worker in its own process group and polls RSS for the worker plus its
children. A two-way completion handshake keeps the finished compiler, cache, and compiled result
alive until the parent takes and acknowledges a final sample; this prevents fast workers from
finishing between polls without any post-compile RSS observation. The reported value is therefore a
sampled peak with a guaranteed retained-state endpoint, not an assertion that every shorter-than-poll
transient was observed. On Linux, the guard also uses cgroup-v2 `memory.peak` and `memory.events`
when writable. The authoritative memory guard is 6 GiB resident/cgroup memory, leaving about 2 GiB
for the OS and harness. The fallback RSS poll interval is recorded, and allocation self-tests verify
both guard classification and fast-worker endpoint capture before benchmarks begin.

The worker timestamps benchmark completion before collecting endpoint RSS. The supervisor allows a
short bounded marker-publication grace, but still compares that embedded timestamp with the original
measurement deadline; endpoint-protocol overhead cannot convert on-time work into a timeout or make
late work appear successful.

Initial per-case timeout is 120 seconds. The pilot may lower it or raise it up to 300 seconds before
the configuration is frozen. A timeout sends `SIGTERM`, waits five seconds, then sends `SIGKILL` to
the process group. Raw status includes signal, exit code, last RSS, peak RSS, stderr tail, and whether
the parent, cgroup, or kernel ended the process, and whether both RSS handshakes completed.

Only one benchmark worker runs at a time.

## 12. Sampling and statistical plan

### Repetitions and stopping

For ordinary successful cells:

1. Run one unmeasured warm-up block.
2. Run at least seven independent measured blocks with a fresh process/compiler.
3. Variants within a block use the same frozen input/seed and a randomized order.
4. After seven blocks, compute a bootstrap 95% interval for the paired log speedup.
5. For the cache study, stop only if both primary paired ratios meet the criterion. Stop if each
   required interval's relative half-width is at most 5%; otherwise continue to at most 20 blocks.

The stopping rule and bootstrap seed are frozen before authoritative runs. Large cases that time out
or hit the memory guard use three confirmation attempts; they are reported as censored outcomes, not
included in a geometric mean.

### Summaries

- median and interquartile range for raw latency and memory;
- paired median speedup and bootstrap 95% confidence interval;
- descriptive p95 only where the sample count supports it;
- geometric-mean speedup across comparable successful cells;
- explicit timeout/RSS-limit counts;
- cluster bootstrap at the stream-worker level, never at the dependent request level.

For timing, speedup is `slower_or_disabled_time / optimized_time`; values above 1 favor the
optimization. Reports will also state percentage reduction to avoid ratio ambiguity.

Outliers are retained. Samples with excessive hypervisor steal time are flagged and rerun once at
the end; both original and rerun remain in raw data. The steal-time threshold is selected during the
pilot and frozen rather than chosen after seeing final results.

## 13. Ubuntu controls and profiler availability

Record before the first run:

- `uname`, kernel, distribution, virtualization type, and `/proc/cpuinfo`;
- `lscpu`, physical/logical core layout, and CPU affinity;
- RAM, swap, filesystem capacity, and free disk;
- compiler, linker, CMake, Ninja, Python, and dependency versions;
- mitigations and performance governor if visible, without assuming the VM permits changes;
- `/proc/stat` snapshots for steal time around every measured block.

Primary timing uses one pinned physical CPU where topology permits, Release `-O3`/LTO builds, no
simultaneous jobs, and no network activity from the harness. Variant order is randomized within
blocks. The pilot includes a 15-minute repeated sentinel case; authoritative work starts only if its
variance is acceptable under the frozen criterion.

Hardware counters are optional because a KVM droplet may not expose a virtual PMU. The day-one
preflight is:

```bash
perf stat -e cycles,instructions true
perf record -e cpu-clock -g -- true
```

If hardware events are unsupported, cycles, IPC, cache misses, and branch misses are removed from
the promised deliverables. If software sampling is permitted, representative cases use `cpu-clock`
flame graphs from a separate symbol/frame-pointer build. If neither command works, the report relies
on wall/CPU/RSS data plus internal diagnostic counters. No result will imply that unavailable
counters were measured.

## 14. Pilot and frozen scope

The Mac pilot proves commands, correctness, result schemas, and watchdog behavior. It cannot supply
reportable performance numbers.

The Ubuntu pilot may change only operational parameters before `frozen-config.json` is written:

- timeout (120–300 seconds);
- number of repetitions under the predeclared precision rule;
- whether the 500-tool subset passes the resource gate;
- stream length, but only if a diagnostic saturation curve shows 20 requests is materially too
  short or wastefully long;
- steal-time flag threshold;
- whether `perf` counters or software sampling are available.

It may not choose workloads because they make an optimization look good. Pilot observations are
stored separately and excluded from final estimates.

The 500-tool subset runs only if one `rule-off` pilot request completes in at most 30 seconds, peak
RSS remains under 4 GiB, and estimated full subset time fits the remaining run budget. Otherwise it
is documented as deferred, not silently sampled fewer times.

## 15. Implementation stages and exit gates

### Stage 1: Establish the release baseline

- Create `codex/profile-v0.2.7` from `82505d0`.
- Initialize submodules and build the pristine project.
- Run existing C++ and Python tests.
- Save source, dependency, and build manifests.

Exit: pristine v0.2.7 builds; zero unexplained test failures.

### Stage 2: Build the harness before changing algorithms

- Implement config loading, variant selection, tokenizer snapshot loading, process supervision,
  JSONL output, and environment capture.
- Generate tiny deterministic synthetic and repetition workloads.
- Validate every result record against the JSON schema.

Exit: a mock/tiny run is repeatable on the Mac and survives intentional timeout/error/RSS tests.

### Stage 3: Add minimal ablations and the intra-only hook

- Add CMake flags.
- Gate only `RuleLevelCache` construction/passing while preserving the exact LRU.
- Gate only the compressed repetition branch while retaining memoization.
- Add the private rule-only clear method and per-cache size accessors.
- Build pristine and three timing variants.
- Commit the profiling implementation before producing any authoritative build; record that commit
  in every build manifest.

Exit: default behavior is unchanged; each profiling control has a focused unit test.

### Stage 4: Add diagnostic counters

- Add compile epochs, cache-origin counters, eviction/byte stats, and compiled-structure accessors.
- Verify counters with hand-built grammars.
- Confirm all counters are absent from timing builds.

Exit: expected same-compile and prior-compile hits can be demonstrated independently.

### Stage 5: Correctness qualification

- Run applicable existing suites on all variants.
- Run differential cache and repetition protocols.
- Create `validation.json` tied to exact variant hashes.

Exit: zero unexplained semantic mismatch; otherwise stop and fix before measuring.

### Stage 6: Prepare the droplet and data

- Bootstrap dependencies without model weights.
- Build/install isolated variants and check disk headroom.
- Transfer or prepare pinned tokenizer/BFCL snapshots and verify hashes.
- Run memory-watchdog and `perf` preflights.
- Capture machine metadata.

Exit: at least 8 GiB disk remains free after all timing variants and data are installed; watchdog
works; profiler capability is explicitly recorded.

### Stage 7: Pilot and freeze

- Run sentinel variance test and resource estimates.
- Apply only the allowed pilot decisions from section 14.
- Write and hash `frozen-config.json`.

Exit: fixed authoritative matrix, repetition rule, timeout, exclusions, and environment.

### Stage 8: Authoritative measurements

Run in this order:

1. correctness smoke test;
2. controlled rule-cache matrix;
3. BFCL validation traces;
4. repetition-compression matrix;
5. diagnostic versions of representative cases;
6. available sampling profiles.

Raw JSONL files are append-only and never overwritten.

### Stage 9: Analysis and reports

- Validate records and variant/config hashes.
- Produce summaries and plots entirely from raw JSONL.
- Separate measured observations, causal interpretations, and prior paper claims.
- Write the comprehensive report first.
- Derive the 450–600-word report from verified findings.
- Run an automated word-count check and store the count.

Exit: every number in both reports is traceable to a result query and raw records.

## 16. Planned command surface

The exact CLI is part of the implementation contract:

```bash
python -m xgrammar_profile.cli build --all --output-root <six-variant-build-root>
PYTHONPATH=<six-variant-build-root>/production-profile/site-packages \
  python -m xgrammar_profile.cli prepare-tokenizer --config profiling/configs/v0.2.7.json
python -m xgrammar_profile.cli prepare-bfcl \
  --config profiling/configs/v0.2.7.json \
  --revision <40-hex-commit> \
  --variant-root <six-variant-build-root>
python -m xgrammar_profile.cli validate --all --variant-root <six-variant-build-root>
python -m xgrammar_profile.cli pilot --config profiling/configs/pilot.json \
  --variant-root <six-variant-build-root> --qualification <qualification.json>
python -m xgrammar_profile.cli freeze --pilot-results <run-dir>
python -m xgrammar_profile.cli run --config <run-dir>/frozen-config.json
python -m xgrammar_profile.cli analyze --run <run-dir>
python -m xgrammar_profile.cli verify-reports --run <run-dir>
```

`run` refuses a dirty/unrecognized variant, a mutable data revision, a tokenizer hash mismatch, a
non-frozen config, or output into an existing run directory.

## 17. Files to create

```text
PROFILING_PLAN_V2.md
profiling/
├── README.md
├── pyproject.toml
├── configs/
│   ├── v0.2.7.json
│   └── pilot.json
├── xgrammar_profile/
│   ├── __init__.py
│   ├── cli.py
│   ├── config.py
│   ├── environment.py
│   ├── variants.py
│   ├── process_guard.py
│   ├── measurement.py
│   ├── cache_workload.py
│   ├── repetition_workload.py
│   ├── dataset.py
│   ├── tokenizer_snapshot.py
│   ├── replay.py
│   ├── validation.py
│   ├── analysis.py
│   └── reports.py
├── workers/
│   ├── compile_stream.py
│   └── compile_repetition.py
├── scripts/
│   ├── bootstrap_ubuntu.sh
│   ├── build_variants.sh
│   ├── run_suite.sh
│   └── run_perf.sh
├── schemas/
│   ├── config.schema.json
│   └── result.schema.json
├── data/
│   ├── README.md
│   └── manifest.json
├── tests/
│   ├── test_cache_workload.py
│   ├── test_repetition_workload.py
│   ├── test_process_guard.py
│   ├── test_validation.py
│   └── test_analysis.py
├── results/
│   └── .gitignore
└── reports/
    ├── comprehensive-report.md
    └── one-page-report.md

cpp/
├── profiling.cc
└── profiling.h

tests/python/
└── test_profiling_hooks.py
```

Generated, uncommitted layout:

```text
profiling/results/<run-id>/
├── metadata.json
├── frozen-config.json
├── validation.json
├── variants/
├── raw/
│   ├── cache.jsonl
│   ├── repetition.jsonl
│   └── diagnostics.jsonl
├── perf/
├── logs/
├── summaries/
│   ├── cache.csv
│   └── repetition.csv
└── plots/
```

## 18. Existing files to modify

- `CMakeLists.txt`: define profiling options and compile definitions.
- `cpp/grammar_compiler.cc`: isolate rule-cache construction, compile epochs, rule-only clear,
  per-cache stats, and optional timings/counters.
- `cpp/grammar_functor.cc` and `cpp/grammar_functor.h`: repetition switch and diagnostic cache value,
  origin, insertion, eviction, shard, and byte counters.
- `cpp/testing.cc` and `cpp/testing.h`: private profiling accessors.
- `cpp/tvm_ffi/tvm_ffi.cc`: private Python bindings.
- `python/xgrammar/testing.py`: profiling wrappers under the testing namespace.
- `.gitignore`: generated variants, snapshots, raw results, profiler output, and plots while retaining
  manifests, source reports, and small summary tables.

No supported public Python or C++ API changes.

## 19. Report contents

The comprehensive report will contain:

1. plain-language executive summary;
2. profiling primer and XGrammar v0.2.7 architecture;
3. terminology and scope decision;
4. exact source/data/build/machine provenance;
5. hypotheses and three-arm/two-arm ablation designs;
6. correctness evidence;
7. controlled and BFCL workloads;
8. cache results split into intra-compile and cross-request effects;
9. repetition boundary and scalability results;
10. matcher replay and uncertain-token behavior;
11. memory, mechanism counters, and available profiles;
12. statistical uncertainty and censored failures;
13. limitations and threats to validity;
14. comparison with XGrammar-2 claims without treating paper numbers as this study's measurements;
15. conclusions, recommendations, and full reproduction commands;
16. raw-result, table, and figure index.

The one-page report will be 450–600 words, target about 525, and will include what was tested, why
the optimizations matter, how the ablations isolate them, the main latency/memory results, important
limitations, and a short conclusion. The word-count script uses one documented rule consistently
and prints the final count in `validation.json`.

## 20. Completion criteria

The first two optimizations are complete only when:

- source, tokenizer, BFCL snapshot, builds, frozen config, and machine are immutable and recorded;
- default v0.2.7 and every timing arm have zero unexplained semantic failures;
- cache results separately quantify same-compile and earlier-compile reuse;
- cache results report both cache halves, actual seen-before fraction, cold request, saturation, and
  budget behavior;
- repetition results include 127/128/129/130 and preserve timeout/RSS-limit outcomes;
- every primary cell follows the frozen stopping rule or is explicitly censored;
- unavailable PMU/perf features are omitted rather than guessed;
- all reported figures and speedups reproduce from append-only JSONL;
- the comprehensive report is complete;
- the short report is verified between 450 and 600 words;
- conclusions are limited to v0.2.7, the tested CPU/tokenizer/configurations, and library-level CPU
  work.

## 21. Review issues resolved in this revision

This revision explicitly fixes the critical review findings:

- replaces the cache on/off design with `rule-off`, `intra-only`, and `full`;
- defines reuse against all prior requests and logs realized reuse;
- treats structural primitive hits at 0% identity overlap as expected;
- separates rule-cache and exact-cache bytes and documents the one-third/two-thirds split;
- cuts the infeasible 80,000–400,000-compile grid to a bounded core matrix with a frozen precision
  rule and gated 500-tool subset;
- makes hardware counters conditional on an actual PMU preflight;
- removes serialization equality as a semantic gate and measures uncertain-token/runtime effects;
- replaces `RLIMIT_AS` with RSS/cgroup supervision;
- records steal time and does not assume governor control;
- times only XGrammar compile calls, uses compiled-size accessors, names mechanism-test exceptions,
  and labels thread scaling as cache benefit plus lock cost.
