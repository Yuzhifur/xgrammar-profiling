"""Configuration loading, validation, hashing, and freezing."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping


class ConfigError(ValueError):
    """Raised when a profiling configuration is incomplete or inconsistent."""


RELEASE_COMMIT = "82505d0d987c36a4209fb3d8571cf6b0f28b5acd"
TOKENIZER_REVISION = "c916fa4defd319b7d4e4da17604ca7338f4d99f5"
EXPECTED_QUALIFICATION_SUITES = {
    "differential",
    "pristine_ctest",
    "pristine_pytest",
    "no_repeat_semantic_replacements",
    "full_python_production-profile",
    "full_python_no-rule-cache",
    "full_python_no-repeat-compression",
    "profiling_hooks_production-profile",
    "profiling_hooks_no-rule-cache",
    "profiling_hooks_no-repeat-compression",
    "profiling_hooks_production-diagnostic",
    "profiling_hooks_no-rule-cache-diagnostic",
}
EXPECTED_NO_REPEAT_FAILURES = (
    "tests/python/test_grammar_matcher_ebnf.py::test_repeat_ref_exact",
    "tests/python/test_grammar_matcher_ebnf.py::test_repeat_ref_range",
    "tests/python/test_grammar_matcher_ebnf.py::test_repeat_ref_boundary",
    "tests/python/test_grammar_matcher_ebnf.py::test_repeat_ref_multichar_rule",
    "tests/python/test_grammar_matcher_ebnf.py::test_repeat_ref_range_from_zero",
    "tests/python/test_grammar_matcher_ebnf.py::test_repeat_ref_nested_inner",
    "tests/python/test_grammar_matcher_ebnf.py::test_repeat_ref_nested_outer",
    "tests/python/test_grammar_matcher_ebnf.py::test_repeat_ref_sequence_with_repeat",
    "tests/python/test_grammar_matcher_ebnf.py::test_repeat_ref_complex_nested",
    "tests/python/test_grammar_parser.py::test_repetition_normalizer",
)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def profiling_root() -> Path:
    return Path(__file__).resolve().parents[1]


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def config_hash(config: Mapping[str, Any]) -> str:
    material = copy.deepcopy(dict(config))
    material.pop("config_hash", None)
    return sha256_bytes(canonical_json(material))


def load_json(path: Path | str) -> Dict[str, Any]:
    path = Path(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"configuration must be a JSON object: {path}")
    return value


def resolve_path(value: str | Path, *, base: Path | None = None) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (base or repo_root()) / path
    return path.resolve()


def _require(config: Mapping[str, Any], dotted: str, expected: type | tuple[type, ...]) -> Any:
    cursor: Any = config
    for part in dotted.split("."):
        if not isinstance(cursor, Mapping) or part not in cursor:
            raise ConfigError(f"missing required configuration field: {dotted}")
        cursor = cursor[part]
    if not isinstance(cursor, expected):
        names = (
            ", ".join(item.__name__ for item in expected)
            if isinstance(expected, tuple)
            else expected.__name__
        )
        raise ConfigError(f"{dotted} must be {names}, got {type(cursor).__name__}")
    return cursor


def _positive_ints(values: Iterable[Any], field: str) -> None:
    if not values:
        raise ConfigError(f"{field} must not be empty")
    for value in values:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ConfigError(f"{field} values must be positive integers")


def validate_qualification_structure(qualification: Mapping[str, Any]) -> None:
    suites = qualification.get("suites")
    if not isinstance(suites, Mapping) or set(suites) != EXPECTED_QUALIFICATION_SUITES:
        raise ConfigError("qualification does not contain the exact reviewed suite matrix")
    if any(
        not isinstance(value, Mapping) or value.get("status") != "passed"
        for value in suites.values()
    ):
        raise ConfigError("qualification contains a suite that did not pass")
    disabled = qualification.get("expected_disabled")
    if not isinstance(disabled, Mapping):
        raise ConfigError("qualification lacks the narrow no-repeat mechanism allowlist")
    expected_ids = list(EXPECTED_NO_REPEAT_FAILURES)
    observed = disabled.get("observed_expected_failures")
    if (
        disabled.get("expected_node_ids") != expected_ids
        or disabled.get("zero_unexplained_failures") is not True
        or disabled.get("unexpected_outcomes") != []
        or not isinstance(observed, list)
        or [item.get("node_id") for item in observed if isinstance(item, Mapping)] != expected_ids
        or any(
            not isinstance(item, Mapping) or item.get("status") != "expected_failure"
            for item in observed
        )
    ):
        raise ConfigError("qualification no-repeat allowlist evidence is incomplete or broadened")


def validate_config(config: Mapping[str, Any], *, require_frozen: bool = False) -> None:
    if _require(config, "schema_version", int) != 1:
        raise ConfigError("only config schema_version 1 is supported")
    commit = _require(config, "source.release_commit", str)
    if commit != RELEASE_COMMIT:
        raise ConfigError(f"source.release_commit must pin v0.2.7 ({RELEASE_COMMIT})")
    revision = _require(config, "tokenizer.revision", str)
    if revision != TOKENIZER_REVISION:
        raise ConfigError(
            f"tokenizer.revision must be the reviewed immutable revision {TOKENIZER_REVISION}"
        )
    if len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision.lower()):
        raise ConfigError("tokenizer.revision must be a 40-character hexadecimal commit")

    tools = _require(config, "cache.tools_per_request", list)
    reuse = _require(config, "cache.seen_before_fractions", list)
    _positive_ints(tools, "cache.tools_per_request")
    if any(not isinstance(value, (int, float)) or value < 0 or value > 1 for value in reuse):
        raise ConfigError("cache.seen_before_fractions values must be in [0, 1]")
    if len(set(reuse)) != len(reuse):
        raise ConfigError("cache.seen_before_fractions contains duplicates")
    _positive_ints(_require(config, "repetition.bounds", list), "repetition.bounds")

    timeout = _require(config, "execution.timeout_seconds", (int, float))
    if not 1 <= timeout <= 300:
        raise ConfigError("execution.timeout_seconds must be between 1 and 300")
    rss = _require(config, "execution.rss_limit_bytes", int)
    if rss < 32 * 1024 * 1024:
        raise ConfigError("execution.rss_limit_bytes is implausibly small")
    poll = _require(config, "execution.rss_poll_interval_seconds", (int, float))
    if not 0.01 <= poll <= 5:
        raise ConfigError("execution.rss_poll_interval_seconds must be in [0.01, 5]")
    minimum = _require(config, "sampling.minimum_blocks", int)
    maximum = _require(config, "sampling.maximum_blocks", int)
    if minimum < 1 or maximum < minimum:
        raise ConfigError("sampling block limits are invalid")
    if require_frozen:
        if config.get("frozen") is not True:
            raise ConfigError("authoritative run requires a config produced by `freeze`")
        expected = config.get("config_hash")
        if not isinstance(expected, str) or expected != config_hash(config):
            raise ConfigError("frozen config hash is missing or does not match")
        variant_hashes = config.get("variant_manifest_hashes")
        if not isinstance(variant_hashes, dict):
            raise ConfigError("frozen config lacks variant_manifest_hashes")
        required_variants = _require(config, "variants.required", list)
        if set(variant_hashes) != set(required_variants):
            raise ConfigError("frozen variant hashes do not exactly cover required variants")
        if any(
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in variant_hashes.values()
        ):
            raise ConfigError("frozen variant manifest hashes must be lowercase SHA-256 values")
        tokenizer_hash = config.get("tokenizer_manifest_sha256")
        if not isinstance(tokenizer_hash, str):
            raise ConfigError("frozen config lacks tokenizer_manifest_sha256")
        if len(tokenizer_hash) != 64 or any(c not in "0123456789abcdef" for c in tokenizer_hash):
            raise ConfigError("frozen tokenizer manifest hash is not a lowercase SHA-256")
        source_head = config.get("source_head")
        if (
            not isinstance(source_head, str)
            or len(source_head) != 40
            or any(c not in "0123456789abcdef" for c in source_head)
        ):
            raise ConfigError("frozen source_head must be a lowercase 40-hex commit")
        machine_identity = config.get("machine_identity")
        machine_fingerprint = config.get("machine_fingerprint")
        if not isinstance(machine_identity, Mapping):
            raise ConfigError("frozen config lacks stable machine identity")
        if (
            not isinstance(machine_fingerprint, str)
            or len(machine_fingerprint) != 64
            or any(c not in "0123456789abcdef" for c in machine_fingerprint)
        ):
            raise ConfigError("frozen config lacks a valid machine fingerprint")
        if sha256_bytes(canonical_json(machine_identity)) != machine_fingerprint:
            raise ConfigError("frozen machine identity does not match its fingerprint")
        qualification = config.get("qualification")
        if not isinstance(qualification, Mapping) or qualification.get("passed") is not True:
            raise ConfigError("frozen config lacks passed qualification evidence")
        validate_qualification_structure(qualification)
        qualification_hash = config.get("qualification_canonical_sha256")
        if (
            not isinstance(qualification_hash, str)
            or len(qualification_hash) != 64
            or any(c not in "0123456789abcdef" for c in qualification_hash)
            or sha256_bytes(canonical_json(qualification)) != qualification_hash
        ):
            raise ConfigError("frozen qualification evidence does not match its canonical SHA-256")
        if qualification.get("source_commit") != source_head:
            raise ConfigError("qualification source commit differs from frozen source_head")
        if qualification.get("release_commit") != RELEASE_COMMIT:
            raise ConfigError("qualification release commit differs from v0.2.7")
        if qualification.get("variant_manifest_hashes") != variant_hashes:
            raise ConfigError("qualification variant hashes differ from frozen variants")
        if qualification.get("tokenizer_manifest_sha256") != tokenizer_hash:
            raise ConfigError("qualification tokenizer hash differs from frozen tokenizer")
        physical = _require(config, "execution.physical_cpu_ids", list)
        if any(not isinstance(cpu, int) or isinstance(cpu, bool) or cpu < 0 for cpu in physical):
            raise ConfigError("execution.physical_cpu_ids must contain non-negative integers")
        if len(set(physical)) != len(physical):
            raise ConfigError("execution.physical_cpu_ids must be distinct")
        primary = _require(config, "execution.primary_cpu", int)
        if primary not in physical:
            raise ConfigError("execution.primary_cpu must be in execution.physical_cpu_ids")
        secondary = config.get("cache", {}).get("secondary", {})
        if secondary.get("enabled"):
            resolved_threads = secondary.get("resolved_thread_sweep")
            if not isinstance(resolved_threads, list):
                raise ConfigError("frozen secondary matrix lacks resolved_thread_sweep")
            _positive_ints(resolved_threads, "cache.secondary.resolved_thread_sweep")
            if any(value > len(physical) for value in resolved_threads):
                raise ConfigError("resolved thread sweep exceeds frozen physical CPUs")
        if config.get("bfcl", {}).get("enabled"):
            bfcl_revision = config.get("bfcl_revision")
            if (
                not isinstance(bfcl_revision, str)
                or len(bfcl_revision) != 40
                or any(c not in "0123456789abcdef" for c in bfcl_revision)
            ):
                raise ConfigError("frozen BFCL revision must be a lowercase 40-hex commit")
            for field in ("bfcl_manifest_sha256", "bfcl_traces_sha256"):
                value = config.get(field)
                if (
                    not isinstance(value, str)
                    or len(value) != 64
                    or any(c not in "0123456789abcdef" for c in value)
                ):
                    raise ConfigError(f"frozen {field} is not a lowercase SHA-256")
            bfcl_validation = config.get("bfcl_validation")
            if (
                not isinstance(bfcl_validation, Mapping)
                or bfcl_validation.get("passed") is not True
            ):
                raise ConfigError("frozen config lacks passed BFCL support validation")
            if (
                qualification.get("bfcl_production_variant_manifest_sha256")
                != bfcl_validation.get("production_variant_manifest_sha256")
                or qualification.get("bfcl_tokenizer_manifest_sha256")
                != bfcl_validation.get("tokenizer_manifest_sha256")
                or qualification.get("bfcl_validation_build_config")
                != bfcl_validation.get("build_config")
            ):
                raise ConfigError("qualification BFCL support provenance differs from frozen data")


def load_config(path: Path | str, *, require_frozen: bool = False) -> Dict[str, Any]:
    config = load_json(path)
    validate_config(config, require_frozen=require_frozen)
    return config


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
