"""Validate review output at the runtime/CLI boundary.

The schema is application-owned, shared by both runtime output mechanisms. The
additional cross-field checks (unique IDs and ordered ranges) run locally before
a successful report reaches the state machine.
"""

from collections.abc import Mapping
from typing import Any

_TEXT = {"type": "string", "pattern": r"\S"}
_FINDING_PROPERTIES: dict[str, Any] = {
    "id": _TEXT,
    "severity": {"type": "string", "enum": ["P0", "P1", "P2", "P3"]},
    "category": _TEXT,
    "side": {"type": "string", "enum": ["baseline", "candidate"]},
    "path": _TEXT,
    "line_start": {"type": "integer", "minimum": 1},
    "line_end": {"type": "integer", "minimum": 1},
    "quote": {"type": "string"},
    "title": _TEXT,
    "impact": _TEXT,
    "rationale": _TEXT,
}
REPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": _TEXT,
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": _FINDING_PROPERTIES,
                "required": list(_FINDING_PROPERTIES),
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "findings"],
    "additionalProperties": False,
}


class InvalidReport(ValueError):
    """The structured runtime response violates the review-output contract."""


def _object(value: object, keys: set[str], name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise InvalidReport(f"{name} must contain exactly {sorted(keys)}")
    return value


def _text(value: object, name: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise InvalidReport(
            f"{name} must be {'a nonempty' if nonempty else 'a'} string"
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise InvalidReport(f"{name} must be valid UTF-8") from error
    return value


def validate_report(value: object) -> dict[str, Any]:
    """Return a fresh JSON-native report, accepting frozen JSON containers too."""
    report = _object(value, {"summary", "findings"}, "report")
    summary = _text(report["summary"], "summary")
    findings = report["findings"]
    if not isinstance(findings, (list, tuple)):
        raise InvalidReport("findings must be an array")
    result: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    for index, item_value in enumerate(findings):
        finding = _object(item_value, set(_FINDING_PROPERTIES), f"finding {index}")
        normalized: dict[str, Any] = {}
        for name in _FINDING_PROPERTIES:
            item = finding[name]
            if name in ("line_start", "line_end"):
                if type(item) is not int or item < 1:
                    raise InvalidReport(f"{name} must be an integer at least 1")
                normalized[name] = item
            else:
                normalized[name] = _text(item, name, nonempty=name != "quote")
        if normalized["severity"] not in ("P0", "P1", "P2", "P3"):
            raise InvalidReport("severity must be P0, P1, P2, or P3")
        if normalized["side"] not in ("baseline", "candidate"):
            raise InvalidReport("side must be baseline or candidate")
        if normalized["line_end"] < normalized["line_start"]:
            raise InvalidReport("line_end must not precede line_start")
        if normalized["id"] in identifiers:
            raise InvalidReport("finding IDs must be unique within a report")
        identifiers.add(normalized["id"])
        result.append(normalized)
    return {"summary": summary, "findings": result}
