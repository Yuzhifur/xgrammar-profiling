"""Result-record and differential-signature validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping

RESULT_STATUSES = {
    "success",
    "timeout",
    "rss_limit",
    "kernel_termination",
    "program_error",
    "invalid_output",
    "skipped",
}
RECORD_TYPES = {"sample", "stream-summary", "guard", "validation", "diagnostic"}
EXPERIMENTS = {"cache", "repetition", "watchdog", "validation"}


class RecordValidationError(ValueError):
    pass


def validate_result_record(record: Mapping[str, Any]) -> None:
    required = (
        "schema_version",
        "record_type",
        "experiment",
        "status",
        "case_id",
        "block_id",
        "measured",
    )
    missing = [field for field in required if field not in record]
    if missing:
        raise RecordValidationError(f"missing result fields: {', '.join(missing)}")
    if record["schema_version"] != 1:
        raise RecordValidationError("unsupported result schema_version")
    if record["record_type"] not in RECORD_TYPES:
        raise RecordValidationError(f"invalid record_type: {record['record_type']}")
    if record["experiment"] not in EXPERIMENTS:
        raise RecordValidationError(f"invalid experiment: {record['experiment']}")
    if record["status"] not in RESULT_STATUSES:
        raise RecordValidationError(f"invalid status: {record['status']}")
    if not isinstance(record["case_id"], str) or not record["case_id"]:
        raise RecordValidationError("case_id must be a non-empty string")
    if not isinstance(record["block_id"], int) or isinstance(record["block_id"], bool):
        raise RecordValidationError("block_id must be an integer")
    if not isinstance(record["measured"], bool):
        raise RecordValidationError("measured must be boolean")
    for field in (
        "wall_time_ns",
        "cpu_time_ns",
        "compile_time_ns",
        "peak_rss_bytes",
        "baseline_rss_bytes",
        "compiled_grammar_bytes",
    ):
        value = record.get(field)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            raise RecordValidationError(f"{field} must be a non-negative integer or null")
    if record["status"] == "success" and record["record_type"] in {"sample", "stream-summary"}:
        if record.get("compile_time_ns") is None:
            raise RecordValidationError("successful measurement lacks compile_time_ns")


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RecordValidationError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            if not isinstance(record, dict):
                raise RecordValidationError(f"{path}:{line_number}: record is not an object")
            try:
                validate_result_record(record)
            except RecordValidationError as exc:
                raise RecordValidationError(f"{path}:{line_number}: {exc}") from exc
            records.append(record)
    return records


def parse_worker_jsonl(output: str) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for line_number, line in enumerate(output.splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RecordValidationError(
                f"worker stdout line {line_number} is not JSON: {line!r}"
            ) from exc
        validate_result_record(record)
        records.append(record)
    return records


def compare_signatures(signatures: Mapping[str, Any], *, context: str) -> None:
    def semantic_only(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: semantic_only(item)
                for key, item in value.items()
                if key not in {"per_token_time_ns", "median_time_ns", "p95_time_ns"}
            }
        if isinstance(value, list):
            return [semantic_only(item) for item in value]
        return value

    values = list(signatures.items())
    if not values:
        raise RecordValidationError(f"no signatures supplied for {context}")
    reference_name, reference = values[0]
    reference = semantic_only(reference)
    for name, value in values[1:]:
        if semantic_only(value) != reference:
            raise RecordValidationError(
                f"semantic mismatch for {context}: {name} differs from {reference_name}"
            )


def assert_signature_expected(
    signature: Mapping[str, Any], *, expected: bool, context: str
) -> None:
    string_result = bool(signature.get("string_accepted") and signature.get("string_terminated"))
    if string_result != expected:
        raise RecordValidationError(
            f"{context}: string oracle expected {expected}, got {string_result}"
        )
    tokens = signature.get("tokens")
    if isinstance(tokens, Mapping):
        accepted = tokens.get("accepted")
        token_result = bool(
            isinstance(accepted, list)
            and all(value is True for value in accepted)
            and tokens.get("terminated") is True
        )
        if token_result != expected:
            raise RecordValidationError(
                f"{context}: token replay expected {expected}, got {token_result}"
            )


def validate_files(paths: Iterable[Path]) -> int:
    return sum(len(read_jsonl(path)) for path in paths)
