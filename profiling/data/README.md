# Profiling data

Only this provenance documentation and `manifest.json` are committed. Generated snapshots are
ignored because they are reproducibly acquired from immutable revisions and may be large.

## Tokenizer snapshot

- Repository: `Qwen/Qwen3-0.6B`
- Revision: `c916fa4defd319b7d4e4da17604ca7338f4d99f5`
- Generated directory: `profiling/data/tokenizer/`

Preparation asks `AutoTokenizer` for tokenizer/configuration assets only; it never instantiates a
model. The mandatory runbook check rejects a snapshot containing model-weight formats such as
`*.safetensors`, `*.bin`, `*.pt`, `*.pth`, or `*.gguf`. The prepared snapshot contains the
decoded vocabulary, `TokenizerInfo` metadata, replay token IDs, source provenance, and SHA-256 for
every retained snapshot file. Benchmark workers reconstruct `TokenizerInfo` locally and do not
contact Hugging Face or instantiate a tokenizer inside the measured interval.

```bash
PYTHONPATH="$XGRAMMAR_VARIANT_ROOT/production-profile/site-packages" \
  PYTHONNOUSERSITE=1 \
  python -m xgrammar_profile.cli prepare-tokenizer \
  --config profiling/configs/v0.2.7.json \
  --output profiling/data/tokenizer
```

The production profiling variant must be built first because preparation uses XGrammar's
`TokenizerInfo`; see `../DROPLET_RUNBOOK.md`.

Use `--local-source DIR` when an already acquired, verified tokenizer-only directory is supplied.

## BFCL snapshot

- Repository: `https://github.com/ShishirPatil/gorilla.git`
- Revision: selected before the pilot and required to be an immutable 40-hex commit
- Generated directory: `profiling/data/bfcl/`

BFCL is a realistic validation subset, not the primary controlled workload. Preparation normalizes
supported OpenAI-style function definitions deterministically and records every accepted or
rejected entry plus its reason. It refuses a moving branch name. Do not duplicate tools to fill a
cell when the pinned source has too few supported entries.

```bash
python -m xgrammar_profile.cli prepare-bfcl \
  --config profiling/configs/v0.2.7.json \
  --revision "$BFCL_REVISION" \
  --variant-root "$XGRAMMAR_VARIANT_ROOT" \
  --tokenizer-snapshot profiling/data/tokenizer \
  --output profiling/data/bfcl
```

The six variants and tokenizer snapshot must already exist. Preparation uses `production-profile`
to reject unsupported schemas, compile every frozen BFCL trace, and bind the production variant,
tokenizer, and profiling build configuration into the BFCL manifest. Use `--source-dir DIR` for a
local checkout whose Git `HEAD` is exactly the requested commit.

## Integrity rules

- Resolve and record revisions before the authoritative run.
- Never modify a prepared snapshot in place. Prepare a new directory and repeat qualification.
- Retain the manifest's upstream repository/revision provenance and the applicable upstream
  licensing information; the normalized snapshot does not relicense the source data.
- After preparation, set `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` for pilot and final runs.
- A missing or mismatched snapshot manifest blocks freezing or running.
