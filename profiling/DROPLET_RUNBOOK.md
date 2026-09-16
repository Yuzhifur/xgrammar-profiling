# Authoritative Ubuntu droplet runbook

This is the end-to-end operator procedure for collecting reportable XGrammar v0.2.7 results. Read
the whole document once before starting. Commands assume a dedicated-CPU Ubuntu droplet with 8 GB
RAM and 25 GB disk. Run one benchmark process at a time and do not use the droplet for unrelated
work during measurement.

The study covers Cross-Grammar Cache and Repetition State Compression. It uses tokenizer assets,
but never downloads model weights or runs an LLM.

## 0. Create the immutable implementation point on the Mac

Finish review and tests before selecting the commit. Commit every source, harness, config, script,
and test change. Do not include generated data, builds, or results.

```bash
cd /Users/administrator/Documents/Local/xgrammar-profiling
git status --short
git log -1 --oneline
git rev-parse HEAD
git merge-base --is-ancestor 82505d0d987c36a4209fb3d8571cf6b0f28b5acd HEAD
git push origin codex/profile-v0.2.7
```

Copy the 40-character value from `git rev-parse HEAD`. This is the **implementation commit**. The
older `82505d0...` value is the v0.2.7 release base; checking out the base alone would omit the
profiling implementation.

Do not continue if the merge-base command fails, any intended file is uncommitted, or local
correctness tests have unexplained failures.

## 1. Provision and enter the droplet

Use a dedicated-CPU Ubuntu droplet in the chosen region. Record the provider plan, region, creation
time, and whether the provider reports SMT. Do not resize, snapshot/restore, or change droplet type
between pilot and final run.

SSH in as the normal operator account. Install the small tools needed to obtain the repository and
keep a persistent terminal; the full bootstrap later installs the remaining dependencies. A
persistent terminal is important because the suite can outlive an SSH connection:

```bash
sudo apt-get update
sudo apt-get install -y --no-install-recommends ca-certificates git tmux
tmux new -s xgrammar-profile
```

Clone the profiling repository, then detach at the exact implementation commit. Replace the two
quoted placeholders; do not literally run them unchanged.

```bash
git clone https://github.com/Yuzhifur/xgrammar-profiling.git
cd xgrammar-profiling
set -o pipefail
export XGRAMMAR_PROFILE_COMMIT="PASTE_40_HEX_IMPLEMENTATION_COMMIT"
test "$(printf '%s' "$XGRAMMAR_PROFILE_COMMIT" | wc -c)" -eq 40
git fetch origin "$XGRAMMAR_PROFILE_COMMIT"
git switch --detach "$XGRAMMAR_PROFILE_COMMIT"
test "$(git rev-parse HEAD)" = "$XGRAMMAR_PROFILE_COMMIT"
git merge-base --is-ancestor 82505d0d987c36a4209fb3d8571cf6b0f28b5acd HEAD
test -z "$(git status --porcelain --untracked-files=normal)"
export REPO_ROOT="$PWD"
```

If the repository is private, use the normal authenticated clone method without writing a token to
the shell history. Keep `XGRAMMAR_PROFILE_COMMIT`, `REPO_ROOT`, and the later variables set in this
same `tmux` shell.

## 2. Bootstrap without model weights

The bootstrap installs compilers, CMake/Ninja, shared Python dependencies, and optional `perf`. It
does not upgrade Ubuntu and does not fetch model files. It requires at least 7 GiB visible RAM and
12 GiB free disk before setup, then rechecks the plan's 8 GiB free-disk floor.

```bash
profiling/scripts/bootstrap_ubuntu.sh 2>&1 | tee /tmp/xgrammar-bootstrap.log
source profiling/.venv/bin/activate
export PYTHONNOUSERSITE=1
python -m xgrammar_profile.cli --help
```

Record the basic host state before data or builds obscure the initial capacity:

```bash
mkdir -p /tmp/xgrammar-host-preflight
uname -a | tee /tmp/xgrammar-host-preflight/uname.txt
cat /etc/os-release | tee /tmp/xgrammar-host-preflight/os-release.txt
lscpu | tee /tmp/xgrammar-host-preflight/lscpu.txt
lscpu -e=CPU,CORE,SOCKET,NODE,ONLINE | tee /tmp/xgrammar-host-preflight/cpu-topology.txt
free -h | tee /tmp/xgrammar-host-preflight/memory.txt
swapon --show | tee /tmp/xgrammar-host-preflight/swap.txt
df -h "$REPO_ROOT" | tee /tmp/xgrammar-host-preflight/disk.txt
systemd-detect-virt | tee /tmp/xgrammar-host-preflight/virtualization.txt
cat /proc/cmdline | tee /tmp/xgrammar-host-preflight/kernel-command-line.txt
if compgen -G '/sys/devices/system/cpu/cpufreq/policy*/scaling_governor' >/dev/null; then
  grep -H . /sys/devices/system/cpu/cpufreq/policy*/scaling_governor \
    | tee /tmp/xgrammar-host-preflight/cpu-governors.txt
else
  printf 'unavailable\n' | tee /tmp/xgrammar-host-preflight/cpu-governors.txt
fi
if compgen -G '/sys/devices/system/cpu/vulnerabilities/*' >/dev/null; then
  grep -H . /sys/devices/system/cpu/vulnerabilities/* \
    | tee /tmp/xgrammar-host-preflight/cpu-vulnerabilities.txt
else
  printf 'unavailable\n' | tee /tmp/xgrammar-host-preflight/cpu-vulnerabilities.txt
fi
```

Do not assume the VM lets you change its governor, PMU, kernel parameters, or cgroup delegation.
Record what is visible; the harness falls back to process-tree RSS when cgroup v2 is not writable.
Its reported post-baseline RSS is the maximum of periodic samples and a required retained-state
endpoint sample; cgroup `memory.peak`, when available, additionally protects against short transient
spikes. Do not use `ulimit -v`/`RLIMIT_AS`.

## 3. Build isolated variants

Use a commit-specific root so a package from another checkout cannot be imported silently:

```bash
export XGRAMMAR_VARIANT_ROOT="$REPO_ROOT/profiling/build/variants/$XGRAMMAR_PROFILE_COMMIT"
python -m xgrammar_profile.cli build --all --output-root "$XGRAMMAR_VARIANT_ROOT"
find "$XGRAMMAR_VARIANT_ROOT" -maxdepth 2 -name manifest.json -print -exec jq \
  '{variant,source_commit,source_dirty_patch_sha256,cmake_options,python_path,native_extension_sha256}' \
  {} \;
df -h "$REPO_ROOT"
```

`--all` produces a pristine baseline, the three timing variants, and two cache-diagnostic variants.
The required timing variants are `production-profile`, `no-rule-cache`, and
`no-repeat-compression`. Every manifest must name the implementation commit, an empty dirty patch,
and a different variant-specific Python path. The build script stops if fewer than 8 GiB remain.

Timing variants have diagnostic statistics disabled. Never substitute a `*-diagnostic` binary in
a timing run.

## 4. Prepare immutable data while the network is allowed

Tokenizer preparation needs XGrammar's `TokenizerInfo`, so use the just-built production timing
variant explicitly. This one-command `PYTHONPATH` prevents an installed or source-tree copy from
being selected accidentally.

```bash
PYTHONPATH="$XGRAMMAR_VARIANT_ROOT/production-profile/site-packages" \
  PYTHONNOUSERSITE=1 \
  python -m xgrammar_profile.cli prepare-tokenizer \
  --config profiling/configs/v0.2.7.json \
  --output profiling/data/tokenizer
test -f profiling/data/tokenizer/manifest.json
```

Confirm that no model-weight file slipped into the snapshot:

```bash
if find profiling/data/tokenizer -type f \( \
  -name '*.safetensors' -o -name '*.bin' -o -name '*.pt' -o -name '*.pth' -o -name '*.gguf' \
  \) -print | grep -q .; then
  echo "unexpected model-weight file in tokenizer snapshot" >&2
  exit 1
fi
```

Resolve BFCL once to a real Git commit. The first command consults the moving upstream branch; the
result is immediately converted into and thereafter used as an immutable 40-hex identifier.

```bash
export BFCL_REVISION="$(git ls-remote \
  https://github.com/ShishirPatil/gorilla.git refs/heads/main | awk 'NR == 1 {print $1}')"
printf 'BFCL_REVISION=%s\n' "$BFCL_REVISION"
[[ "$BFCL_REVISION" =~ ^[0-9a-f]{40}$ ]]
python -m xgrammar_profile.cli prepare-bfcl \
  --config profiling/configs/v0.2.7.json \
  --revision "$BFCL_REVISION" \
  --variant-root "$XGRAMMAR_VARIANT_ROOT" \
  --tokenizer-snapshot profiling/data/tokenizer \
  --output profiling/data/bfcl
test -f profiling/data/bfcl/manifest.json
```

Inspect both manifests, rejected-entry reasons, accepted counts, and every printed revision. If the
BFCL preparation says a planned trace is unavailable, record that before freezing; do not duplicate
tools to make it available.

```bash
jq . profiling/data/tokenizer/manifest.json
jq . profiling/data/bfcl/manifest.json
jq -e \
  --arg variant_sha "$(sha256sum "$XGRAMMAR_VARIANT_ROOT/production-profile/manifest.json" | awk '{print $1}')" \
  --arg tokenizer_sha "$(sha256sum profiling/data/tokenizer/manifest.json | awk '{print $1}')" \
  '.validation.passed == true
   and .validation.production_variant_manifest_sha256 == $variant_sha
   and .validation.tokenizer_manifest_sha256 == $tokenizer_sha
   and .validation.build_config.XGRAMMAR_ENABLE_PROFILING_API == true
   and .validation.build_config.XGRAMMAR_ENABLE_PROFILING_STATS == false
   and .validation.build_config.XGRAMMAR_PROFILE_DISABLE_RULE_LEVEL_CACHE == false
   and .validation.build_config.XGRAMMAR_PROFILE_DISABLE_REPETITION_COMPRESSION == false
   and .validation.support.passed == true
   and .validation.trace_smoke.passed == true' \
  profiling/data/bfcl/manifest.json >/dev/null
sha256sum profiling/data/tokenizer/manifest.json profiling/data/bfcl/manifest.json
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
```

From this point onward, no benchmark command needs the network.

## 5. Select CPU affinity and run capability preflights

Review `lscpu -e` and select one online logical CPU. Prefer a CPU whose sibling is not used by any
other workload; leave all unrelated processes off the droplet. Replace `N` with its integer ID:

```bash
export XGRAMMAR_PROFILE_CPU="N"
taskset --cpu-list "$XGRAMMAR_PROFILE_CPU" true
```

Do not launch the whole suite through `taskset`. The pilot freezes this CPU and the available
physical-core CPU list. The harness pins each ordinary worker to the primary CPU, while a thread
sweep with `N` threads is given `N` distinct physical-core CPUs. Pinning the parent process to one
CPU would silently invalidate that secondary sweep.

Capture the exact optional profiler and cgroup capabilities in a new directory:

```bash
export PREFLIGHT_DIR="$REPO_ROOT/profiling/results/perf-preflight-$(date -u +%Y%m%dT%H%M%SZ)"
profiling/scripts/run_perf.sh preflight "$PREFLIGHT_DIR"
cat "$PREFLIGHT_DIR/capabilities.txt" 2>/dev/null || true
cat "$PREFLIGHT_DIR/environment.txt"
```

The hardware test is exactly `perf stat -e cycles,instructions true`; software sampling is exactly
`perf record -e cpu-clock -g -- true`. Nonzero status is acceptable. It means the report must omit
the unavailable counters/profile rather than estimate them or rerun benchmarks with `sudo`.

If and only if `software_sampling_available=yes`, build the separate symbol/frame-pointer variant.
It is stats-enabled, excluded from qualification/timing, and stored outside the six authoritative
variants:

```bash
export XGRAMMAR_SAMPLING_VARIANT_ROOT="$REPO_ROOT/profiling/build/sampling/$XGRAMMAR_PROFILE_COMMIT"
python -m xgrammar_profile.cli build --variant sampling-diagnostic \
  --output-root "$XGRAMMAR_SAMPLING_VARIANT_ROOT"
jq . "$XGRAMMAR_SAMPLING_VARIANT_ROOT/sampling-diagnostic/manifest.json"
```

Skip this build when software sampling is unavailable.

## 6. Correctness qualification

Use a unique UTC label for each attempt. Qualification runs the pristine C++ and non-credentialed
Python suites, focused ablation tests, differential acceptance, bitmask/replay checks, and the
variant-import/hash checks. The wrapper rejects a pristine CTest build containing zero tests. The
focused profiling-hook file also runs against every API/diagnostic ablation variant, so tests that
are correctly conditional on build flags cannot simply pass by being skipped in the pristine
build. The memory/timeout watchdog self-test runs in the next pilot stage, where it is a freeze gate.

```bash
export SESSION_ID="$(date -u +%Y%m%dT%H%M%SZ)-$XGRAMMAR_PROFILE_COMMIT"
export QUAL_DIR="$REPO_ROOT/profiling/results/qualification-$SESSION_ID"
profiling/scripts/run_suite.sh validate "$QUAL_DIR"
find "$QUAL_DIR" -maxdepth 2 -type f -print
jq . "$QUAL_DIR/validation.json"
jq . "$QUAL_DIR/baseline-tests.json"
jq . "$QUAL_DIR/timing-variant-suites.json"
jq . "$QUAL_DIR/expected-disabled.json"
jq . "$QUAL_DIR/variant-hook-tests.json"
jq . "$QUAL_DIR/qualification.json"
```

`qualification.json` binds the tokenizer, pinned BFCL manifest/revision/normalized-trace hashes,
and all six variant manifest hashes to the differential, pristine CTest, pristine/full
timing-variant Python suites, narrow expected-disabled results, and focused hook outcomes. It is
evidence, not a user-editable checklist. The no-repeat build's ten
allowlisted nodes must each produce one assertion failure—not an error, skip, or unexpected pass—and
all other non-HF tests must pass. The differential 127/128/129/130 checks and focused ablation hook
are the semantic replacements for those representation-only assertions. Direct string/regex
lowerings change between 128 and 129; JSON arrays isolate their first item and therefore change
between user-facing `maxItems=129` and `maxItems=130`.

Stop on any unexplained test failure, bitmask difference, acceptance/termination difference, wrong
hash, or import path. The explicit expected-disabled allowlist contains only the ten reviewed
no-repeat representation/serialization node IDs. Cache tests and all non-allowlisted repetition
tests must pass. Never broaden the allowlist to make validation green.

Do not treat compiled serialization differences as semantic failures; external bitmasks,
`accept_token`, and termination behavior are the gates.

## 7. Pilot, inspect, and freeze

The Mac never supplies performance data. Run the Ubuntu pilot under the same affinity and offline
environment as the final suite:

```bash
export PILOT_DIR="$REPO_ROOT/profiling/results/pilot-$SESSION_ID"
profiling/scripts/run_suite.sh pilot "$QUAL_DIR/qualification.json" "$PILOT_DIR"
find "$PILOT_DIR" -maxdepth 2 -type f -print
```

The pilot refuses qualification evidence whose source, tokenizer, or variant manifest hashes differ
from the assets it is about to use. `freeze` binds the passing qualification evidence, and the final
runner checks it again.

Inspect the pilot summary and raw records before freezing. Confirm:

- the repeated sentinel's variance is below the predeclared pilot threshold;
- memory supervision terminated and correctly classified its allocation self-test;
- one worker ran at a time, the selected primary CPU was recorded, and the frozen physical-core
  list can supply distinct CPUs to every requested thread-sweep size;
- the fixed timeout is 120 seconds and the fixed controlled-stream length is 20 requests;
- the automatic 500-tool gate enabled the subset only if its `rule-off` request took at most 30
  seconds and peak RSS stayed below 4 GiB;
- if the automatically enabled 500-tool subset would make the projected final suite exceed the
  available run budget, do not freeze it;
- the steal-time flag threshold was selected now, before final results;
- `perf` capabilities agree with the separate preflight;
- at least 8 GiB disk remains free.

This implementation conservatively keeps the predeclared 120-second timeout and 20-request stream
length; it does not implement post-pilot saturation tuning. Changing either value, disabling an
automatically eligible 500-tool subset for run-budget reasons, or fixing any code, correctness,
workload, or schema defect requires a reviewed Mac-side config/code change, a new implementation
commit, and a complete rebuild, qualification, and pilot. Do not hand-edit evidence or patch the
droplet checkout.

Freeze only after review:

```bash
export FROZEN_CONFIG="$PILOT_DIR/frozen-config.json"
profiling/scripts/run_suite.sh freeze "$PILOT_DIR" "$FROZEN_CONFIG"
jq . "$FROZEN_CONFIG"
jq -e '.frozen == true and (.config_hash | length == 64)' "$FROZEN_CONFIG"
sha256sum "$FROZEN_CONFIG" | tee "$PILOT_DIR/frozen-config.sha256"
```

Never hand-edit the frozen file. A changed decision requires a new pilot directory and a new freeze.

## 8. Run authoritative measurements

Check for obvious host contention immediately before starting. If the droplet is busy, wait and
record why; do not compensate by deleting samples.

```bash
uptime
free -h
df -h "$REPO_ROOT"
ps -eo pid,psr,pcpu,pmem,comm --sort=-pcpu | head -20
export RUN_DIR="$REPO_ROOT/profiling/results/authoritative-$SESSION_ID"
profiling/scripts/run_suite.sh run "$FROZEN_CONFIG" "$RUN_DIR"
```

The order is controlled cache streams, declared cache controls/sweeps, BFCL traces, repetition
cases, then diagnostics. Ordinary cells use at least seven fresh-process blocks and can continue to
twenty under the frozen paired-CI rule. Large explicit cases use three confirmation attempts when
censored. Expected timeout and RSS guard outcomes remain in raw JSONL. The runner records
`/proc/stat` steal-time snapshots around each worker and retains flagged samples.

The command refuses an existing `RUN_DIR`, dirty source, unknown variant, hash mismatch, mutable
data, or non-frozen config. If SSH disconnects, reattach with `tmux attach -t xgrammar-profile`.
If the process itself stops unexpectedly, retain the partial directory and start a new full run with
a new `SESSION_ID`; never erase, edit, or resume into the old raw files.

## 9. Analyze, inspect diagnostics, and optionally sample

Generate summaries only from the untouched authoritative records:

```bash
profiling/scripts/run_suite.sh analyze "$RUN_DIR"
find "$RUN_DIR/analysis" -maxdepth 2 -type f -print
jq . "$RUN_DIR/analysis/summary.json"
```

Before writing conclusions, confirm record/config/variant hashes, sample counts, randomized order,
timeout/RSS classifications, confidence intervals, and rerun flags. Treat requests within one cache
stream as dependent; the stream worker is the replication unit.

If either `perf` capability passed, inspect the saved jobs and choose one manageable `full`-arm
cache-stream job whose result is representative; never select it because it has the largest
speedup. Make an unmeasured standalone copy with supervisor handshake fields removed:

```bash
find "$RUN_DIR" -type f -path '*/jobs/*.json' ! -name '*.baseline.json' -print | head -30
export REPRESENTATIVE_JOB="PASTE_PATH_TO_ONE_CACHE_JOB_JSON"
test -f "$REPRESENTATIVE_JOB"
jq -e '.arm == "full"
       and .variant == "production-profile"
       and .compiler_threads == 1
       and .workload_class == "controlled-primary"' "$REPRESENTATIVE_JOB"
mkdir -p "$RUN_DIR/perf"
export COUNTER_JOB="$RUN_DIR/perf/cache-representative-counter-job.json"
test ! -e "$COUNTER_JOB"
jq 'del(.baseline_ready_path, .baseline_ack_path) | .measured = false' \
  "$REPRESENTATIVE_JOB" > "$COUNTER_JOB"
```

If `hardware_stat_available=yes`, collect cycles and instructions with the normal timing binary in
a new directory. This invocation is diagnostic; its wall time is not added to timing tables.

```bash
export DIAG_COUNTER_DIR="$RUN_DIR/perf/cache-representative-counters"
PYTHONPATH="$XGRAMMAR_VARIANT_ROOT/production-profile/site-packages:$REPO_ROOT/profiling" \
  PYTHONNOUSERSITE=1 \
  profiling/scripts/run_perf.sh stat "$DIAG_COUNTER_DIR" -- \
  taskset --cpu-list "$XGRAMMAR_PROFILE_CPU" \
  python profiling/workers/compile_stream.py --job "$COUNTER_JOB"
cat "$DIAG_COUNTER_DIR/counters.txt"
```

If `software_sampling_available=yes`, copy the standalone job, label the separate
symbol/frame-pointer variant, and capture stacks:

```bash
export SAMPLE_JOB="$RUN_DIR/perf/cache-representative-sampling-job.json"
test ! -e "$SAMPLE_JOB"
jq '.variant = "sampling-diagnostic"' "$COUNTER_JOB" > "$SAMPLE_JOB"
export DIAG_PERF_DIR="$RUN_DIR/perf/cache-representative-profile"
PYTHONPATH="$XGRAMMAR_SAMPLING_VARIANT_ROOT/sampling-diagnostic/site-packages:$REPO_ROOT/profiling" \
  PYTHONNOUSERSITE=1 \
  profiling/scripts/run_perf.sh record "$DIAG_PERF_DIR" -- \
  taskset --cpu-list "$XGRAMMAR_PROFILE_CPU" \
  python profiling/workers/compile_stream.py --job "$SAMPLE_JOB"
```

These diagnostics can explain instruction cost and where CPU time went. If a capability failed
preflight, skip its corresponding command and state that those counters or stacks were unavailable.

## 10. Write and verify both reports

Fill `profiling/reports/comprehensive-report.md` first. Every table/figure/number must identify the
analysis query and raw records behind it. Clearly separate observation, causal interpretation, and
prior XGrammar-2 claims. Then derive the plain-language one-page report from verified findings.
You may draft on the Mac after copying the evidence, but copy the final two files back to these exact
paths on the unchanged droplet checkout for verification.

```bash
profiling/scripts/run_suite.sh verify-reports "$RUN_DIR"
```

Verification must report 450–600 words for the one-page report and no provenance/hash mismatch.
Do not publish a template containing bracketed placeholders or `TBD` markers.

## 11. Seal, copy, and retain the evidence

Create checksums once, after analysis and report verification. This does not replace the manifests;
it makes transfer corruption visible.

```bash
test ! -e "$RUN_DIR/SHA256SUMS"
find "$RUN_DIR" -type f ! -name SHA256SUMS -print0 | LC_ALL=C sort -z | \
  xargs -0 sha256sum > "$RUN_DIR/SHA256SUMS"
cd "$(dirname "$RUN_DIR")"
tar --create --gzip --file "$(basename "$RUN_DIR").tar.gz" "$(basename "$RUN_DIR")"
sha256sum "$(basename "$RUN_DIR").tar.gz" | tee "$(basename "$RUN_DIR").tar.gz.sha256"
```

Copy the archive, the pilot directory, qualification directory, preflight directory,
`/tmp/xgrammar-host-preflight`, `/tmp/xgrammar-bootstrap.log`, both final report Markdown files, and
`$RUN_DIR/analysis/report-verification.json` back to the Mac. Also retain the prepared tokenizer and
BFCL directories, `$XGRAMMAR_VARIANT_ROOT/manifest-sha256.txt`,
`$XGRAMMAR_VARIANT_ROOT/dependency-freeze.txt`, all six variant manifests, their
`cmake-build/CMakeCache.txt`, build/import logs, and CTest/Pytest qualification logs. Copying the
variant wheels themselves gives the strongest long-term artifact retention if space permits.
Verify archive SHA-256 after transfer. Retain the original droplet until both reports have been
independently checked; do not commit bulky raw results to Git.

## Failure rules

- **Wrong/dirty commit:** stop. Commit and review on the Mac, then use a fresh checkout/build root.
- **Less than 8 GiB free after build:** stop before pilot. Move recoverable artifacts elsewhere or
  provision a larger disk; do not delete the only copy of evidence.
- **Correctness mismatch:** no performance result is reportable until explained and fixed.
- **Pilot instability or excessive steal:** defer the final run or reprovision; do not cherry-pick
  favorable samples.
- **Unavailable cgroup/PMU/perf:** use the documented RSS fallback and omit unavailable counters.
- **Expected timeout/RSS termination:** preserve it as censored data.
- **Interrupted final suite:** preserve the partial directory and restart into a new directory.
- **Any implementation/config/data change after freeze:** new commit if applicable, rebuild,
  requalify, repilot, and refreeze.
