"""Acquire and normalize a pinned BFCL snapshot for realistic validation traces."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import urllib.error
import urllib.request
import zipfile
import random
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Tuple

from .config import sha256_file, write_json_atomic

BFCL_ARCHIVE_URL = "https://github.com/ShishirPatil/gorilla/archive/{revision}.zip"


class DatasetError(RuntimeError):
    pass


def validate_revision(revision: str) -> str:
    normalized = revision.lower()
    if len(normalized) != 40 or any(c not in "0123456789abcdef" for c in normalized):
        raise DatasetError("BFCL revision must be an immutable 40-character hexadecimal commit")
    return normalized


def _safe_extract_zip(payload: bytes, destination: Path) -> Path:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = archive.namelist()
        roots = {name.split("/", 1)[0] for name in names if name}
        if len(roots) != 1:
            raise DatasetError("unexpected BFCL archive layout")
        root = next(iter(roots))
        for member in archive.infolist():
            relative = Path(member.filename)
            if relative.is_absolute() or ".." in relative.parts:
                raise DatasetError(f"unsafe archive member: {member.filename}")
            target = destination / relative
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("wb") as sink:
                    shutil.copyfileobj(source, sink)
    return destination / root


def _walk_values(value: Any, origin: str) -> Iterator[Tuple[Any, str]]:
    if isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_values(item, f"{origin}[{index}]")
    elif isinstance(value, dict):
        for key in ("tools", "functions", "function"):
            if key in value:
                yield from _walk_values(value[key], f"{origin}.{key}")
        if "name" in value and ("parameters" in value or "input_schema" in value):
            yield value, origin


def _normalize_function(value: Dict[str, Any]) -> Tuple[Dict[str, Any] | None, str | None]:
    name = value.get("name")
    parameters = value.get("parameters", value.get("input_schema"))
    if not isinstance(name, str) or not name:
        return None, "missing function name"
    if isinstance(parameters, str):
        try:
            parameters = json.loads(parameters)
        except json.JSONDecodeError:
            return None, "parameters is a non-JSON string"
    if not isinstance(parameters, dict):
        return None, "parameters is not an object"
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": value.get("description", ""),
            "parameters": parameters,
        },
    }, None


def _json_files(root: Path) -> Iterable[Path]:
    data_root = root / "berkeley-function-call-leaderboard" / "bfcl_eval" / "data"
    if not data_root.is_dir():
        data_root = root
    return sorted(
        path
        for suffix in ("*.json", "*.jsonl")
        for path in data_root.rglob(suffix)
        if path.is_file()
    )


def _json_or_jsonl_values(
    path: Path, *, origin_name: str | None = None
) -> Tuple[List[Tuple[Any, str]], List[Dict[str, str]]]:
    """Read either one ordinary JSON value or BFCL's line-delimited JSON records."""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return [], [{"origin": origin_name or path.name, "reason": f"invalid UTF-8: {exc}"}]
    origin_name = origin_name or path.name
    try:
        return [(json.loads(text), origin_name)], []
    except json.JSONDecodeError:
        values: List[Tuple[Any, str]] = []
        rejected: List[Dict[str, str]] = []
        for line_number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            origin = f"{origin_name}:{line_number}"
            try:
                values.append((json.loads(line), origin))
            except json.JSONDecodeError as exc:
                rejected.append({"origin": origin, "reason": f"invalid JSONL record: {exc}"})
        if not values and not rejected:
            rejected.append({"origin": origin_name, "reason": "empty JSON/JSONL file"})
        return values, rejected


def normalize_bfcl(source_root: Path) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    accepted: List[Dict[str, Any]] = []
    rejected: List[Dict[str, str]] = []
    seen: set[str] = set()
    for path in _json_files(source_root):
        try:
            origin_name = path.relative_to(source_root).as_posix()
        except ValueError:
            origin_name = path.name
        values, parse_rejections = _json_or_jsonl_values(path, origin_name=origin_name)
        rejected.extend(parse_rejections)
        for value, origin in values:
            for candidate, location in _walk_values(value, origin):
                normalized, reason = _normalize_function(candidate)
                if normalized is None:
                    rejected.append({"origin": location, "reason": reason or "unsupported"})
                    continue
                fingerprint = hashlib.sha256(
                    json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                accepted.append(
                    {"origin": location, "fingerprint": fingerprint, "tool": normalized}
                )
    accepted.sort(key=lambda item: (item["fingerprint"], item["origin"]))
    rejected.sort(key=lambda item: (item["origin"], item["reason"]))
    return accepted, rejected


SupportValidator = Callable[
    [List[Dict[str, Any]]], Tuple[List[Dict[str, Any]], List[Dict[str, str]], Dict[str, Any]]
]
TraceValidator = Callable[[List[Dict[str, Any]]], Dict[str, Any]]


def prepare_bfcl(
    *,
    revision: str,
    output: Path,
    source_dir: Path | None = None,
    source_revision: str | None = None,
    support_validator: SupportValidator | None = None,
    trace_validator: TraceValidator | None = None,
    validation_provenance: Dict[str, Any] | None = None,
    candidate_limit: int = 500,
) -> Dict[str, Any]:
    revision = validate_revision(revision)
    if output.exists() and any(output.iterdir()):
        raise DatasetError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    temporary = output / "_source"
    if source_dir is None:
        url = BFCL_ARCHIVE_URL.format(revision=revision)
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                payload = response.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            raise DatasetError(f"could not download pinned BFCL archive {url}: {exc}") from exc
        archive_sha256 = hashlib.sha256(payload).hexdigest()
        source_root = _safe_extract_zip(payload, temporary)
    else:
        source_root = source_dir.resolve()
        if not source_root.is_dir():
            raise DatasetError(f"BFCL source directory does not exist: {source_root}")
        if source_revision != revision:
            raise DatasetError(
                "local BFCL source HEAD was not verified against the declared revision"
            )
        archive_sha256 = None
    accepted, rejected = normalize_bfcl(source_root)
    normalized_count = len(accepted)
    if support_validator is None or trace_validator is None:
        raise DatasetError(
            "BFCL preparation requires production-profile support and trace validators"
        )
    if candidate_limit < 150:
        raise DatasetError("BFCL candidate_limit must be at least 150")
    all_candidates = accepted
    supported: List[Dict[str, Any]] = []
    support_rejections: List[Dict[str, str]] = []
    support_batches: List[Dict[str, Any]] = []
    assessed_count = 0
    while (
        assessed_count < len(all_candidates)
        and len({item["tool"]["function"]["name"] for item in supported}) < 150
    ):
        candidates = all_candidates[assessed_count : assessed_count + candidate_limit]
        batch_supported, batch_rejections, batch_metadata = support_validator(candidates)
        candidate_by_fingerprint = {item["fingerprint"]: item for item in candidates}
        supported_fingerprints = [item.get("fingerprint") for item in batch_supported]
        rejected_fingerprints = [item.get("fingerprint") for item in batch_rejections]
        if (
            len(supported_fingerprints) != len(set(supported_fingerprints))
            or len(rejected_fingerprints) != len(set(rejected_fingerprints))
            or set(supported_fingerprints) & set(rejected_fingerprints)
            or set(supported_fingerprints) | set(rejected_fingerprints)
            != set(candidate_by_fingerprint)
            or any(
                fingerprint not in candidate_by_fingerprint
                for fingerprint in supported_fingerprints
            )
            or any(
                fingerprint not in candidate_by_fingerprint for fingerprint in rejected_fingerprints
            )
            or any(
                item != candidate_by_fingerprint[item["fingerprint"]] for item in batch_supported
            )
        ):
            raise DatasetError("BFCL support validator returned unknown or modified candidates")
        supported.extend(batch_supported)
        support_rejections.extend(batch_rejections)
        support_batches.append(batch_metadata)
        assessed_count += len(candidates)
    support_metadata = {
        "passed": True,
        "batch_size": candidate_limit,
        "assessed_count": assessed_count,
        "supported_count": len(supported),
        "rejected_count": len(support_rejections),
        "unassessed_count": len(all_candidates) - assessed_count,
        "batches": support_batches,
    }
    rejected.extend(support_rejections)
    accepted = sorted(supported, key=lambda item: (item["fingerprint"], item["origin"]))
    if len({item["tool"]["function"]["name"] for item in accepted}) < 150:
        raise DatasetError(
            "production-profile support validation yielded fewer than 150 unique tool names"
        )
    write_json_atomic(output / "accepted.json", accepted)
    write_json_atomic(output / "rejected.json", rejected)
    if temporary.exists():
        shutil.rmtree(temporary)
    traces = _trace_specs_from_accepted(accepted, requests=5, seed=270227)
    trace_metadata = trace_validator(traces)
    if trace_metadata.get("passed") is not True:
        raise DatasetError("production-profile BFCL trace smoke validation did not pass")
    traces_sha256 = hashlib.sha256(
        json.dumps(traces, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    provenance = dict(validation_provenance or {})
    required_provenance = (
        "production_variant_manifest_sha256",
        "tokenizer_manifest_sha256",
        "build_config",
    )
    if any(field not in provenance for field in required_provenance):
        raise DatasetError(
            "BFCL validation provenance must bind production variant, tokenizer, and build controls"
        )
    expected_build_config = provenance["build_config"]
    if not isinstance(expected_build_config, dict):
        raise DatasetError("BFCL validation build controls must be an object")
    for index, batch in enumerate(support_batches):
        if batch.get("runtime_build_config") != expected_build_config:
            raise DatasetError(f"BFCL support batch {index} ran under unexpected build controls")
    if trace_metadata.get("runtime_build_config") != expected_build_config:
        raise DatasetError("BFCL trace smoke ran under unexpected build controls")
    manifest = {
        "schema_version": 1,
        "source": "ShishirPatil/gorilla",
        "revision": revision,
        "archive_sha256": archive_sha256,
        "normalized_candidate_count": normalized_count,
        "support_batch_size": candidate_limit,
        "support_assessed_count": assessed_count,
        "support_unassessed_count": len(all_candidates) - assessed_count,
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
        "traces_sha256": traces_sha256,
        "trace_parameters": {"requests": 5, "seed": 270227},
        "validation": {
            "passed": True,
            **provenance,
            "support": support_metadata,
            "trace_smoke": trace_metadata,
        },
        "files": {
            "accepted.json": sha256_file(output / "accepted.json"),
            "rejected.json": sha256_file(output / "rejected.json"),
        },
    }
    write_json_atomic(output / "manifest.json", manifest)
    return manifest


def verify_bfcl_snapshot(output: Path) -> Dict[str, Any]:
    manifest_path = output / "manifest.json"
    if not manifest_path.is_file():
        raise DatasetError(f"missing BFCL manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validate_revision(str(manifest.get("revision", "")))
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise DatasetError("BFCL manifest has no file hashes")
    for name, expected in files.items():
        path = output / name
        if not path.is_file() or sha256_file(path) != expected:
            raise DatasetError(f"BFCL snapshot hash mismatch: {path}")
    validation = manifest.get("validation")
    if not isinstance(validation, dict) or validation.get("passed") is not True:
        raise DatasetError("BFCL manifest lacks passed production-profile validation")
    for field in ("production_variant_manifest_sha256", "tokenizer_manifest_sha256"):
        value = validation.get(field)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise DatasetError(f"BFCL validation has invalid {field}")
    if not isinstance(validation.get("build_config"), dict):
        raise DatasetError("BFCL validation lacks production build controls")
    if manifest.get("trace_parameters") != {"requests": 5, "seed": 270227}:
        raise DatasetError("BFCL manifest has unexpected trace parameters")
    accepted = json.loads((output / "accepted.json").read_text(encoding="utf-8"))
    rejected = json.loads((output / "rejected.json").read_text(encoding="utf-8"))
    if not isinstance(accepted, list) or manifest.get("accepted_count") != len(accepted):
        raise DatasetError("BFCL accepted count differs from its manifest")
    if not isinstance(rejected, list) or manifest.get("rejected_count") != len(rejected):
        raise DatasetError("BFCL rejected count differs from its manifest")
    traces = _trace_specs_from_accepted(accepted, requests=5, seed=270227)
    traces_sha256 = hashlib.sha256(
        json.dumps(traces, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if manifest.get("traces_sha256") != traces_sha256:
        raise DatasetError("BFCL validated trace hash differs from its manifest")
    support = validation.get("support")
    trace_smoke = validation.get("trace_smoke")
    expected_build_config = validation["build_config"]
    if not isinstance(support, dict) or support.get("passed") is not True:
        raise DatasetError("BFCL manifest lacks passed support validation")
    if (
        support.get("supported_count") != len(accepted)
        or support.get("assessed_count")
        != support.get("supported_count", 0) + support.get("rejected_count", 0)
        or manifest.get("support_assessed_count") != support.get("assessed_count")
        or manifest.get("support_unassessed_count") != support.get("unassessed_count")
        or manifest.get("normalized_candidate_count")
        != support.get("assessed_count", 0) + support.get("unassessed_count", 0)
    ):
        raise DatasetError("BFCL support-validation counts are inconsistent")
    batches = support.get("batches")
    if not isinstance(batches, list) or not batches:
        raise DatasetError("BFCL manifest lacks support-validation batches")
    if any(
        not isinstance(batch, dict) or batch.get("runtime_build_config") != expected_build_config
        for batch in batches
    ):
        raise DatasetError("BFCL support validation build controls differ from provenance")
    if (
        not isinstance(trace_smoke, dict)
        or trace_smoke.get("passed") is not True
        or trace_smoke.get("runtime_build_config") != expected_build_config
    ):
        raise DatasetError("BFCL trace-smoke build controls differ from provenance")
    return manifest


def _trace_specs_from_accepted(
    accepted: List[Dict[str, Any]], *, requests: int, seed: int
) -> List[Dict[str, Any]]:
    # Duplicate names cannot coexist in one Structural Tag. Keep the first deterministic
    # fingerprint for each name and retain the original official schema unchanged.
    unique: List[Dict[str, Any]] = []
    names: set[str] = set()
    for item in accepted:
        try:
            name = item["tool"]["function"]["name"]
        except (KeyError, TypeError):
            continue
        if name not in names:
            unique.append(item)
            names.add(name)
    if len(unique) < 150:
        raise DatasetError(
            f"BFCL snapshot has only {len(unique)} unique supported tool names; the frozen traces require 150"
        )

    def make_trace(label: str, count: int, reuse: float, trace_requests: int) -> Dict[str, Any]:
        rng = random.Random(seed + count + int(reuse * 1000))
        cursor = 0
        prior: List[int] = []
        stream = []
        for request_index in range(trace_requests):
            reuse_count = 0 if request_index == 0 else int(math.floor(count * reuse + 0.5))
            reuse_count = min(reuse_count, len(prior))
            reused = rng.sample(prior, reuse_count) if reuse_count else []
            need = count - reuse_count
            if cursor + need > len(unique):
                raise DatasetError(
                    f"BFCL snapshot has {len(unique)} unique supported tool names; {label} needs at least {cursor + need}"
                )
            new_ids = list(range(cursor, cursor + need))
            cursor += need
            prior.extend(new_ids)
            ids = reused + new_ids
            rng.shuffle(ids)
            stream.append(
                {
                    "request_index": request_index,
                    "target_seen_before_fraction": 0.0 if request_index == 0 else reuse,
                    "realized_seen_before_fraction": len(reused) / count,
                    "tool_ids": ids,
                    "tools": [unique[index]["tool"] for index in ids],
                    "validation_text": "",
                }
            )
        return {"label": label, "tools_per_request": count, "reuse": reuse, "requests": stream}

    import math

    return [
        # These are deterministic schema assemblies from the normalized BFCL corpus;
        # they do not claim to preserve an original benchmark request grouping.
        make_trace("bfcl-10-schema-sample", 10, 0.0, 1),
        make_trace("bfcl-50-medium-reuse", 50, 0.5, requests),
        make_trace("bfcl-100-high-reuse", 100, 0.9, requests),
    ]


def bfcl_trace_specs(
    output: Path, *, requests: int = 5, seed: int = 270227
) -> List[Dict[str, Any]]:
    """Build the three frozen realistic traces; fail before freeze if capacity is inadequate."""
    manifest = verify_bfcl_snapshot(output)
    if manifest.get("trace_parameters") != {"requests": requests, "seed": seed}:
        raise DatasetError(
            "requested BFCL trace parameters were not production-validated and frozen"
        )
    accepted = json.loads((output / "accepted.json").read_text(encoding="utf-8"))
    traces = _trace_specs_from_accepted(accepted, requests=requests, seed=seed)
    actual = hashlib.sha256(
        json.dumps(traces, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if manifest.get("traces_sha256") != actual:
        raise DatasetError("BFCL frozen traces differ from validated manifest")
    return traces


def _run_variant_validator(
    *, mode: str, payload: Any, tokenizer_snapshot: Path, variant: Any
) -> Dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="xgrammar-bfcl-validation-") as directory:
        job_path = Path(directory) / "job.json"
        write_json_atomic(
            job_path,
            {"mode": mode, "payload": payload, "tokenizer_snapshot": str(tokenizer_snapshot)},
        )
        worker = Path(__file__).resolve().parents[1] / "workers" / "validate_bfcl.py"
        try:
            completed = subprocess.run(
                [sys.executable, str(worker), "--job", str(job_path)],
                env=variant.environment(),
                text=True,
                capture_output=True,
                timeout=900,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DatasetError(f"BFCL {mode} validation timed out") from exc
    if completed.returncode != 0:
        raise DatasetError(f"BFCL {mode} validation failed: {completed.stderr[-4000:]}")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise DatasetError(f"BFCL {mode} validator emitted invalid JSON") from exc
    if not isinstance(result, dict):
        raise DatasetError(f"BFCL {mode} validator emitted a non-object")
    return result


def production_support_validator(
    candidates: List[Dict[str, Any]], *, tokenizer_snapshot: Path, variant: Any
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]], Dict[str, Any]]:
    result = _run_variant_validator(
        mode="support", payload=candidates, tokenizer_snapshot=tokenizer_snapshot, variant=variant
    )
    outcomes = result.get("outcomes")
    if not isinstance(outcomes, list) or len(outcomes) != len(candidates):
        raise DatasetError("BFCL support validator returned incomplete outcomes")
    by_fingerprint = {item["fingerprint"]: item for item in candidates}
    supported: List[Dict[str, Any]] = []
    rejected: List[Dict[str, str]] = []
    for outcome in outcomes:
        fingerprint = outcome.get("fingerprint")
        candidate = by_fingerprint.get(fingerprint)
        if candidate is None:
            raise DatasetError("BFCL support validator returned an unknown fingerprint")
        if outcome.get("supported") is True:
            supported.append(candidate)
        else:
            rejected.append(
                {
                    "origin": candidate["origin"],
                    "fingerprint": fingerprint,
                    "reason": str(outcome.get("reason", "unsupported by production-profile")),
                }
            )
    return (
        supported,
        rejected,
        {
            "passed": True,
            "candidate_count": len(candidates),
            "supported_count": len(supported),
            "rejected_count": len(rejected),
            "runtime_build_config": result.get("runtime_build_config"),
        },
    )


def production_trace_validator(
    traces: List[Dict[str, Any]], *, tokenizer_snapshot: Path, variant: Any
) -> Dict[str, Any]:
    result = _run_variant_validator(
        mode="traces", payload=traces, tokenizer_snapshot=tokenizer_snapshot, variant=variant
    )
    if result.get("passed") is not True:
        raise DatasetError(f"BFCL trace smoke failed: {result.get('error', 'unknown error')}")
    return result
