"""Deterministic controlled workloads for the cross-grammar cache study."""

from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Sequence


@dataclass(frozen=True)
class CacheRequest:
    request_index: int
    target_seen_before_fraction: float
    realized_seen_before_fraction: float
    tool_ids: List[int]
    tools: List[Dict[str, Any]]
    validation_text: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _name(tool_id: int) -> str:
    return f"profile_tool_{tool_id:08d}"


def _property_name(tool_id: int, index: int) -> str:
    digest = hashlib.sha256(f"property:{tool_id}:{index}".encode()).hexdigest()[:8]
    return f"p_{index}_{digest}"


def _schema_and_example(tool_id: int) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Return one of six shape families plus a valid deterministic instance."""
    family = tool_id % 6
    p0, p1 = _property_name(tool_id, 0), _property_name(tool_id, 1)
    if family == 0:
        return (
            {
                "type": "object",
                "properties": {p0: {"type": "string", "enum": [f"v{tool_id}", f"w{tool_id}"]}},
                "required": [p0],
                "additionalProperties": False,
            },
            {p0: f"v{tool_id}"},
        )
    if family == 1:
        return (
            {
                "type": "object",
                "properties": {
                    p0: {"type": "integer", "minimum": tool_id % 17},
                    p1: {"type": "boolean"},
                },
                "required": [p0, p1],
                "additionalProperties": False,
            },
            {p0: tool_id % 17, p1: bool(tool_id % 2)},
        )
    if family == 2:
        return (
            {
                "type": "object",
                "properties": {
                    p0: {"type": "array", "items": {"type": "number"}, "minItems": 1, "maxItems": 4}
                },
                "required": [p0],
                "additionalProperties": False,
            },
            {p0: [float((tool_id % 7) + 1)]},
        )
    if family == 3:
        return (
            {
                "type": "object",
                "properties": {
                    p0: {
                        "type": "object",
                        "properties": {p1: {"type": "string", "minLength": 1, "maxLength": 12}},
                        "required": [p1],
                        "additionalProperties": False,
                    }
                },
                "required": [p0],
                "additionalProperties": False,
            },
            {p0: {p1: f"n{tool_id}"}},
        )
    if family == 4:
        return (
            {
                "type": "object",
                "properties": {p0: {"anyOf": [{"type": "null"}, {"type": "string"}]}},
                "required": [p0],
                "additionalProperties": False,
            },
            {p0: None if tool_id % 2 else f"x{tool_id}"},
        )
    return (
        {
            "type": "object",
            "properties": {
                p0: {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {p1: {"type": "integer"}},
                        "required": [p1],
                        "additionalProperties": False,
                    },
                    "maxItems": 3,
                }
            },
            "required": [p0],
            "additionalProperties": False,
        },
        {p0: [{p1: tool_id % 13}]},
    )


def generate_tool(tool_id: int) -> Dict[str, Any]:
    if tool_id < 0:
        raise ValueError("tool_id must be non-negative")
    schema, _ = _schema_and_example(tool_id)
    return {
        "type": "function",
        "function": {
            "name": _name(tool_id),
            "description": f"Deterministic profiling tool {tool_id}",
            "parameters": schema,
            "strict": True,
        },
    }


def validation_text(tool_id: int) -> str:
    _, example = _schema_and_example(tool_id)
    arguments = json.dumps(example, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    # Qwen3's v0.2.7 Structural Tag grammar contains these separators as literals.
    # Preserve them exactly; compacting the outer object produces a false oracle.
    payload = (
        '{"name": '
        + json.dumps(_name(tool_id), ensure_ascii=False)
        + ', "arguments": '
        + arguments
        + "}"
    )
    return f"<tool_call>\n{payload}\n</tool_call>"


def generate_stream(
    *, tools_per_request: int, seen_before_fraction: float, requests: int, seed: int
) -> List[CacheRequest]:
    if tools_per_request <= 0 or requests <= 0:
        raise ValueError("tools_per_request and requests must be positive")
    if not 0 <= seen_before_fraction <= 1:
        raise ValueError("seen_before_fraction must be in [0, 1]")
    rng = random.Random(seed)
    seen: List[int] = []
    seen_set: set[int] = set()
    next_tool_id = seed * 1_000_000
    result: List[CacheRequest] = []
    for request_index in range(requests):
        target_count = (
            0
            if request_index == 0
            else int(math.floor(tools_per_request * seen_before_fraction + 0.5))
        )
        reuse_count = min(target_count, len(seen), tools_per_request)
        reused = rng.sample(seen, reuse_count) if reuse_count else []
        new_count = tools_per_request - reuse_count
        new_ids = list(range(next_tool_id, next_tool_id + new_count))
        next_tool_id += new_count
        ids = reused + new_ids
        rng.shuffle(ids)
        prior_seen = set(seen_set)
        realized = sum(tool_id in prior_seen for tool_id in ids) / tools_per_request
        for tool_id in new_ids:
            seen.append(tool_id)
            seen_set.add(tool_id)
        request = CacheRequest(
            request_index=request_index,
            target_seen_before_fraction=0.0 if request_index == 0 else seen_before_fraction,
            realized_seen_before_fraction=realized,
            tool_ids=ids,
            tools=[generate_tool(tool_id) for tool_id in ids],
            validation_text=validation_text(ids[0]),
        )
        result.append(request)
    return result


def stream_fingerprint(stream: Sequence[Any]) -> str:
    payload = [
        request.to_dict() if hasattr(request, "to_dict") else dict(request) for request in stream
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def exact_repeat_stream(*, tools_per_request: int, requests: int, seed: int) -> List[CacheRequest]:
    """Repeat byte-identical tool lists to exercise the whole-grammar LRU control."""
    first = generate_stream(
        tools_per_request=tools_per_request, seen_before_fraction=0.0, requests=1, seed=seed
    )[0]
    result = []
    for index in range(requests):
        result.append(
            CacheRequest(
                request_index=index,
                target_seen_before_fraction=0.0 if index == 0 else 1.0,
                realized_seen_before_fraction=0.0 if index == 0 else 1.0,
                tool_ids=list(first.tool_ids),
                tools=first.tools,
                validation_text=first.validation_text,
            )
        )
    return result
