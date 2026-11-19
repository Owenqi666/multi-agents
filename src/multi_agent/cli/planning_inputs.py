"""Validate planning inputs and model artifacts without executing their content."""

import json
import math
import re
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

from multi_agent.cli.snapshot import _string
from multi_agent.cli.task_inputs import (
    _COMMIT_ID,
    _TASK_ID,
    _input_text,
    _json_strings,
    _unique_json,
    parse_task_card,
    validate_task_graph,
)
from multi_agent.core.planning import (
    LEGACY_PLANNING_PROVIDERS,
    DependencyReceipt,
    PlanningInput,
    PlanningRequest,
    PlanningReview,
    UserDecision,
)
from multi_agent.core.tasks import TaskCard

_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*\Z")
_TASK_HASH = re.compile(r"[0-9a-f]{64}\Z")
# Static accepted vocabulary, not evidence of model catalog support.
_EFFORTS = {
    "claude": frozenset(("low", "medium", "high", "xhigh", "max")),
    "codex": frozenset(("low", "medium", "high", "xhigh", "max", "ultra")),
}

REVIEW_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Planning review",
    "type": "object",
    "additionalProperties": False,
    "required": ["task_hash", "decision", "reason", "blocking_issues"],
    "properties": {
        "task_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "decision": {"type": "string", "enum": ["ready", "needs_revision"]},
        "reason": {"type": "string", "pattern": r"\S"},
        "blocking_issues": {
            "type": "array",
            "uniqueItems": True,
            "items": {"type": "string", "pattern": r"\S"},
        },
    },
}


def _record[T](value: object, expected: type[T], name: str) -> T:
    if not isinstance(value, expected):
        raise TypeError(f"{name} must be a {expected.__name__}")
    return value


def _request(value: PlanningRequest, expected_provider: str | None) -> None:
    _record(value, PlanningRequest, "planning request")
    provider = _string(value.provider, "requested provider")
    if provider not in _EFFORTS:
        raise ValueError("planning provider must be claude or codex")
    if expected_provider is not None and provider != expected_provider:
        raise ValueError(f"planning role requires provider {expected_provider}")
    model = _string(value.model, "requested model", nonempty=True)
    effort = _string(value.effort, "requested effort", nonempty=True)
    if not _MODEL.fullmatch(model):
        raise ValueError("requested model must be an explicit model identifier")
    if effort not in _EFFORTS[provider]:
        raise ValueError("requested effort is outside the pinned provider vocabulary")


def validate_planning_input(
    value: PlanningInput, *, configured_roles: bool = False
) -> None:
    """Check user declarations, preserving their exact text and provenance."""
    _record(value, PlanningInput, "planning input")
    _input_text(value.requirements)
    for stage, provider in LEGACY_PLANNING_PROVIDERS.items():
        _request(value.request_for(stage), None if configured_roles else provider)
    baseline = _string(value.frozen_rules.baseline_commit, "rules baseline")
    if not _COMMIT_ID.fullmatch(baseline):
        raise ValueError("invalid frozen rules baseline")
    if not isinstance(value.dependencies, tuple) or not isinstance(
        value.decisions, tuple
    ):
        raise TypeError("planning declarations must be immutable tuples")
    dependencies: set[str] = set()
    for receipt in value.dependencies:
        _record(receipt, DependencyReceipt, "dependency receipt")
        task_id = _string(receipt.task_id, "dependency task id")
        if not _TASK_ID.fullmatch(task_id) or task_id in dependencies:
            raise ValueError("invalid or duplicate dependency task id")
        dependencies.add(task_id)
        if _string(receipt.baseline_commit, "dependency baseline") != baseline:
            raise ValueError("dependency receipt baseline does not match frozen rules")
        if _string(receipt.source, "dependency source") != "user_declared":
            raise ValueError("dependency receipt must be user_declared")
        _string(receipt.detail, "dependency receipt detail", nonempty=True)
    questions: set[str] = set()
    for decision in value.decisions:
        _record(decision, UserDecision, "user decision")
        question = _string(decision.question, "user question", nonempty=True)
        if question in questions:
            raise ValueError("duplicate user decision question")
        questions.add(question)
        _string(decision.resolution, "user resolution", nonempty=True)
        _string(decision.detail, "user decision detail", nonempty=True)
        if _string(decision.source, "user decision source") != "user_declared":
            raise ValueError("user decision must be user_declared")


def parse_planning_draft(
    text: str, planning_input: PlanningInput, *, configured_roles: bool = False
) -> TaskCard:
    """Validate a single draft; unanswered blocking questions remain visible."""
    validate_planning_input(planning_input, configured_roles=configured_roles)
    try:
        card = parse_task_card(text)
    except RecursionError as error:
        raise ValueError("planning draft nesting exceeds the parser limit") from error
    rules = planning_input.frozen_rules
    if card.baseline_commit != rules.baseline_commit:
        raise ValueError("draft baseline does not match frozen rules")
    if set(card.test_refs) - {command.name for command in rules.rules.commands}:
        raise ValueError("draft references an unknown test command")
    validate_task_graph(
        (card,), frozenset(receipt.task_id for receipt in planning_input.dependencies)
    )
    decisions = {
        (decision.question, decision.resolution)
        for decision in planning_input.decisions
    }
    for question in card.open_questions:
        if (
            question.blocks_dispatch
            and question.resolution is not None
            and (question.question, question.resolution) not in decisions
        ):
            raise ValueError("blocking resolution does not match a user decision")
    return card


def _reject_constant(value: str) -> Any:
    raise ValueError(f"nonfinite JSON constant: {value}")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite JSON number")
    return number


def parse_planning_review(text: str, card: TaskCard) -> PlanningReview:
    """Accept only an explicit review of this exact task artifact."""
    try:
        value = json.loads(
            _input_text(text),
            object_pairs_hook=_unique_json,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
        Draft202012Validator(REVIEW_SCHEMA).validate(value)
        _json_strings(value)
    except json.JSONDecodeError as error:
        raise ValueError("invalid planning review JSON") from error
    except ValidationError as error:
        raise ValueError("invalid planning review schema") from error
    except RecursionError as error:
        raise ValueError("planning review nesting exceeds the parser limit") from error
    if (
        not _TASK_HASH.fullmatch(value["task_hash"])
        or value["task_hash"] != card.task_hash
    ):
        raise ValueError("review task hash does not match the draft")
    if value["decision"] == "ready" and value["blocking_issues"]:
        raise ValueError("ready review cannot contain blocking issues")
    return PlanningReview(
        value["task_hash"],
        value["decision"],
        value["reason"],
        tuple(value["blocking_issues"]),
    )
