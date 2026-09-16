# XGrammar v0.2.7 profiling implementation

This directory implements the accepted CPU-only ablation study for the first two **current
XGrammar-2 optimizations selected for v0.2.7**:

1. Cross-Grammar Cache (`RuleLevelCache`), separated into no rule cache, same-compile reuse, and
   persistent cross-request reuse.
2. Repetition State Compression, compared with the existing explicit legacy expansion path.

It does not profile the first two labels from the older slide deck, load an LLM, download model
weights, run inference, or support claims about end-to-end model-serving speed.

The Mac is for implementation and correctness smoke tests. Numbers used in the reports must come
from a clean, exact implementation commit on the dedicated-CPU Ubuntu droplet. Read
[DROPLET_RUNBOOK.md](DROPLET_RUNBOOK.md) before producing authoritative results.

## Command surface

The harness exposes the implementation contract from `PROFILING_PLAN_V2.md`:

```bash
python -m xgrammar_profile.cli build --all --output-root "$XGRAMMAR_VARIANT_ROOT"
PYTHONPATH="$XGRAMMAR_VARIANT_ROOT/production-profile/site-packages" \
  PYTHONNOUSERSITE=1 \
  python -m xgrammar_profile.cli prepare-tokenizer --config profiling/configs/v0.2.7.json
python -m xgrammar_profile.cli prepare-bfcl \
  --config profiling/configs/v0.2.7.json \
  --revision 0123456789abcdef0123456789abcdef01234567 \
  --variant-root "$XGRAMMAR_VARIANT_ROOT"
python -m xgrammar_profile.cli validate --all --variant-root "$XGRAMMAR_VARIANT_ROOT"
python -m xgrammar_profile.cli pilot --config profiling/configs/pilot.json \
  --variant-root "$XGRAMMAR_VARIANT_ROOT" \
  --qualification profiling/results/QUALIFICATION_DIR/qualification.json
python -m xgrammar_profile.cli freeze --pilot-results profiling/results/PILOT_DIR
python -m xgrammar_profile.cli run --config profiling/results/PILOT_DIR/frozen-config.json
python -m xgrammar_profile.cli analyze --run profiling/results/RUN_DIR
python -m xgrammar_profile.cli verify-reports --run profiling/results/RUN_DIR
```

Use the scripts in `scripts/` on the droplet. They add exact-commit, clean-tree, resource,
non-overwrite, and variant-isolation checks:

- `bootstrap_ubuntu.sh` installs system/shared Python dependencies but no model weights.
- `build_variants.sh` builds each variant into a separate target and records a manifest.
- `run_suite.sh` wraps qualification, pilot, freeze, authoritative run, analysis, and report checks.
- `run_perf.sh` records whether PMU/software sampling is available and optionally collects
  cycles/instructions or CPU stacks for a representative diagnostic command. `perf` output is
  never authoritative timing data.

`build_variants.sh --variant sampling-diagnostic` is optional after a successful software-sampling
preflight. It produces the separate symbol/frame-pointer build required for useful stacks and is
never part of authoritative timing or the six-build qualification map.

All scripts require `XGRAMMAR_PROFILE_COMMIT` to be the exact 40-character implementation commit.
The commit must descend from release commit `82505d0d987c36a4209fb3d8571cf6b0f28b5acd`.

## Generated and committed material

Committed:

- workload and pilot configurations;
- source code, schemas, tests, scripts, data provenance stub, and report templates.

The report templates intentionally contain a `TBD` sentinel, so `verify-reports` fails until real
findings replace every placeholder.

Generated and ignored:

- the virtual environment and isolated builds;
- pinned tokenizer and BFCL snapshots;
- qualification, pilot, raw result, analysis, plot, and `perf` artifacts.

Generated snapshot directories contain their own hash manifests. Result directories are immutable
evidence: never reuse an output path or edit raw JSONL. If a command is interrupted, keep that
directory and start the replacement under a new name.

## Authority boundaries

The timing build has profiling hooks but no diagnostic counters. Diagnostic variants explain
mechanisms and must not contribute latency claims. A result is reportable only after differential
bitmask/acceptance qualification and pristine suites pass, their asset-bound evidence is accepted
by the pilot, the pilot configuration is frozen, and the source, variant, tokenizer, BFCL, and
config hashes all match.

Expected timeouts or 6 GiB memory-guard terminations in very large explicit-repetition cases are
censored scalability outcomes. They are not converted into invented durations. Hardware counters
are reported only when the exact `perf` preflight succeeds.

The RSS metric is a sampled post-baseline process-tree peak with a mandatory final sample while the
compiler, caches, and compiled result remain live. It captures retained end state even for workers
that finish inside one polling interval; it is not labeled as a continuous instantaneous peak when
cgroup peak accounting is unavailable.
