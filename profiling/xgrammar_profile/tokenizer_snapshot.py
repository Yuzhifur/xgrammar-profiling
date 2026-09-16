"""Prepare and verify an offline tokenizer snapshot without model weights."""

from __future__ import annotations

import base64
import gzip
import io
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List

from .config import TOKENIZER_REVISION, sha256_file, write_json_atomic


class TokenizerSnapshotError(RuntimeError):
    pass


def _require_dependencies() -> tuple[Any, Any]:
    try:
        from transformers import AutoTokenizer
        from xgrammar import TokenizerInfo
    except ImportError as exc:
        raise TokenizerSnapshotError(
            "tokenizer preparation requires the profiling 'prepare' extra and a built XGrammar "
            "variant (install with `pip install -e profiling[prepare]` and set PYTHONPATH)"
        ) from exc
    return AutoTokenizer, TokenizerInfo


def _iter_files(root: Path) -> Iterable[Path]:
    return sorted(
        path for path in root.rglob("*") if path.is_file() and path.name != "manifest.json"
    )


def file_hashes(root: Path) -> Dict[str, str]:
    return {path.relative_to(root).as_posix(): sha256_file(path) for path in _iter_files(root)}


def prepare_tokenizer(
    *, repository: str, revision: str, output: Path, local_source: Path | None = None
) -> Dict[str, Any]:
    if revision != TOKENIZER_REVISION:
        raise TokenizerSnapshotError(f"refusing unreviewed tokenizer revision: {revision}")
    if output.exists() and any(output.iterdir()):
        raise TokenizerSnapshotError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    if local_source is not None:
        local_source = local_source.expanduser().resolve()
        completed = subprocess.run(
            ["git", "-C", str(local_source), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0 or completed.stdout.strip().lower() != revision:
            raise TokenizerSnapshotError(
                "local tokenizer source must be a git checkout at the reviewed immutable revision"
            )
        status = subprocess.run(
            ["git", "-C", str(local_source), "status", "--porcelain", "--untracked-files=normal"],
            text=True,
            capture_output=True,
            check=False,
        )
        if status.returncode != 0 or status.stdout.strip():
            raise TokenizerSnapshotError(
                "local tokenizer source must be a clean checkout with no tracked or untracked changes"
            )
    AutoTokenizer, TokenizerInfo = _require_dependencies()
    source: str | Path = local_source if local_source is not None else repository
    tokenizer = AutoTokenizer.from_pretrained(
        source,
        revision=None if local_source is not None else revision,
        local_files_only=local_source is not None,
        trust_remote_code=False,
        use_fast=True,
    )
    hf_dir = output / "hf_tokenizer"
    tokenizer.save_pretrained(hf_dir)
    info = TokenizerInfo.from_huggingface(tokenizer)
    vocab_path = output / "decoded_vocab.json.gz"
    encoded = [base64.b64encode(bytes(token)).decode("ascii") for token in info.decoded_vocab]
    with vocab_path.open("wb") as raw_stream:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw_stream, mtime=0) as compressed:
            with io.TextIOWrapper(compressed, encoding="utf-8") as stream:
                json.dump(encoded, stream, separators=(",", ":"))
    (output / "metadata.json").write_text(info.dump_metadata() + "\n", encoding="utf-8")

    smoke_texts = [
        "",
        "a",
        '"a"',
        "[]",
        "[0]",
        '<tool_call>\n{"name": "profile_tool_00000000", "arguments": {}}\n</tool_call>',
    ]
    replay = {text: list(tokenizer.encode(text, add_special_tokens=False)) for text in smoke_texts}
    write_json_atomic(output / "replay_tokens.json", replay)
    manifest = {
        "schema_version": 1,
        "repository": repository,
        "revision": revision,
        "acquisition": "verified-local-git" if local_source else "huggingface-hub",
        "model_weights_downloaded": False,
        "vocab_size": info.vocab_size,
        "files": file_hashes(output),
    }
    write_json_atomic(output / "manifest.json", manifest)
    verify_snapshot(output, expected_revision=revision)
    return manifest


def verify_snapshot(output: Path, *, expected_revision: str | None = None) -> Dict[str, Any]:
    manifest_path = output / "manifest.json"
    if not manifest_path.is_file():
        raise TokenizerSnapshotError(f"missing tokenizer manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if expected_revision is not None and manifest.get("revision") != expected_revision:
        raise TokenizerSnapshotError("tokenizer snapshot revision does not match configuration")
    expected = manifest.get("files")
    if not isinstance(expected, dict) or not expected:
        raise TokenizerSnapshotError("tokenizer manifest has no file hashes")
    actual = file_hashes(output)
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        changed = sorted(
            name for name in set(expected) & set(actual) if expected[name] != actual[name]
        )
        raise TokenizerSnapshotError(
            f"tokenizer snapshot hash mismatch; missing={missing}, extra={extra}, changed={changed}"
        )
    return manifest


def load_tokenizer_info(snapshot: Path) -> Any:
    try:
        from xgrammar import TokenizerInfo
    except ImportError as exc:
        raise TokenizerSnapshotError("the selected XGrammar variant cannot be imported") from exc
    verify_snapshot(snapshot)
    with gzip.open(snapshot / "decoded_vocab.json.gz", "rt", encoding="utf-8") as stream:
        encoded = json.load(stream)
    vocab: List[bytes] = [base64.b64decode(value) for value in encoded]
    metadata = (snapshot / "metadata.json").read_text(encoding="utf-8").strip()
    return TokenizerInfo.from_vocab_and_metadata(vocab, metadata)


def load_hf_tokenizer(snapshot: Path) -> Any:
    try:
        from transformers import AutoTokenizer
    except ImportError as exc:
        raise TokenizerSnapshotError("validation trace preparation requires transformers") from exc
    verify_snapshot(snapshot)
    return AutoTokenizer.from_pretrained(
        snapshot / "hf_tokenizer", local_files_only=True, trust_remote_code=False, use_fast=True
    )


def copy_snapshot(source: Path, destination: Path) -> None:
    """Copy a verified snapshot, mainly for explicit transfer workflows."""
    verify_snapshot(source)
    if destination.exists():
        raise TokenizerSnapshotError(f"destination already exists: {destination}")
    shutil.copytree(source, destination)
    verify_snapshot(destination)
