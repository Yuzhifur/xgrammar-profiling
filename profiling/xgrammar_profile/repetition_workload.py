"""Deterministic cases for repetition-state compression."""

from __future__ import annotations

import json
import hashlib
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List

FAMILIES = {
    "json-string",
    "json-array-primitive",
    "regex-range",
    "json-array-object",
    "regex-exact",
    "regex-nonzero-min",
    "json-array-minmax",
}


@dataclass(frozen=True)
class RepetitionCase:
    case_id: str
    family: str
    bound: int
    compile_kind: str
    source: Any
    acceptance_examples: Dict[str, str]
    expected_acceptance: Dict[str, bool]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _array_text(count: int, value: Any = 0) -> str:
    return json.dumps([value] * count, separators=(",", ":"))


def make_case(family: str, bound: int) -> RepetitionCase:
    if family not in FAMILIES:
        raise ValueError(f"unknown repetition family: {family}")
    if bound <= 0:
        raise ValueError("bound must be positive")
    case_id = f"{family}-n{bound}"
    if family == "json-string":
        source = {"type": "string", "maxLength": bound}
        examples = {"min": '""', "short": json.dumps("a" * min(4, bound))}
        if bound <= 4096:
            examples.update(
                {"max": json.dumps("a" * bound), "max_plus_one": json.dumps("a" * (bound + 1))}
            )
        expected = {name: name != "max_plus_one" for name in examples}
        return RepetitionCase(case_id, family, bound, "json-schema", source, examples, expected)
    if family == "json-array-primitive":
        source = {"type": "array", "items": {"type": "integer"}, "maxItems": bound}
        examples = {"min": "[]", "short": _array_text(min(4, bound))}
        if bound <= 4096:
            examples.update({"max": _array_text(bound), "max_plus_one": _array_text(bound + 1)})
        expected = {name: name != "max_plus_one" for name in examples}
        return RepetitionCase(case_id, family, bound, "json-schema", source, examples, expected)
    if family == "json-array-object":
        item = {
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "required": ["x"],
            "additionalProperties": False,
        }
        source = {"type": "array", "items": item, "maxItems": bound}
        examples = {"min": "[]", "short": _array_text(min(2, bound), {"x": 1})}
        expected = {name: True for name in examples}
        if bound <= 4096:
            examples.update(
                {
                    "max": _array_text(bound, {"x": 1}),
                    "max_plus_one": _array_text(bound + 1, {"x": 1}),
                }
            )
            expected.update({"max": True, "max_plus_one": False})
        return RepetitionCase(case_id, family, bound, "json-schema", source, examples, expected)
    if family == "json-array-minmax":
        minimum = bound // 2
        source = {
            "type": "array",
            "items": {"type": "integer"},
            "minItems": minimum,
            "maxItems": bound,
        }
        examples = {
            "min_minus_one": _array_text(max(0, minimum - 1)),
            "min": _array_text(minimum),
            "short_valid": _array_text(min(bound, minimum + 1)),
            "max": _array_text(bound),
            "max_plus_one": _array_text(bound + 1),
        }
        return RepetitionCase(
            case_id,
            family,
            bound,
            "json-schema",
            source,
            examples,
            {
                "min_minus_one": False,
                "min": True,
                "short_valid": True,
                "max": True,
                "max_plus_one": False,
            },
        )
    if family == "regex-range":
        examples = {"min": "", "short": "a" * min(4, bound)}
        expected = {"min": True, "short": True}
        if bound <= 4096:
            examples["max"] = "a" * bound
            examples["max_plus_one"] = "a" * (bound + 1)
            expected["max"] = True
            expected["max_plus_one"] = False
        return RepetitionCase(
            case_id, family, bound, "regex", f"[a]{{0,{bound}}}", examples, expected
        )
    if family == "regex-exact":
        examples = {
            "short_invalid": "a" * min(max(0, bound - 1), 4096),
            "prefix": "a" * min(bound, 32),
        }
        expected = {"short_invalid": False, "prefix": bound <= 32}
        if bound <= 4096:
            examples.update({"exact": "a" * bound, "max_plus_one": "a" * (bound + 1)})
            expected.update({"exact": True, "max_plus_one": False})
        return RepetitionCase(
            case_id, family, bound, "regex", f"[a]{{{bound}}}", examples, expected
        )
    minimum = bound // 2
    examples = {
        "min_minus_one": "a" * max(0, minimum - 1),
        "min": "a" * minimum,
        "prefix": "a" * min(minimum, 32),
    }
    expected = {"min_minus_one": False, "min": True, "prefix": minimum <= 32}
    if bound <= 4096:
        examples.update({"max": "a" * bound, "max_plus_one": "a" * (bound + 1)})
        expected.update({"max": True, "max_plus_one": False})
    return RepetitionCase(
        case_id, family, bound, "regex", f"[a]{{{minimum},{bound}}}", examples, expected
    )


def generate_cases(families: Iterable[str], bounds: Iterable[int]) -> List[RepetitionCase]:
    return [make_case(family, bound) for family in families for bound in bounds]


def case_fingerprint(case: RepetitionCase) -> str:
    material = {
        "case_id": case.case_id,
        "family": case.family,
        "bound": case.bound,
        "compile_kind": case.compile_kind,
        "source": case.source,
        "acceptance_examples": case.acceptance_examples,
        "expected_acceptance": case.expected_acceptance,
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def cases_from_config(config: Dict[str, Any]) -> List[RepetitionCase]:
    repetition = config["repetition"]
    cases = generate_cases(repetition["families"], repetition["bounds"])
    cases.extend(
        generate_cases(repetition.get("focused_families", []), repetition.get("focused_bounds", []))
    )
    seen: set[str] = set()
    unique: List[RepetitionCase] = []
    for case in cases:
        if case.case_id not in seen:
            unique.append(case)
            seen.add(case.case_id)
    return unique
