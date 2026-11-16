"""Explicit codecs for trusted planning records; no boundary repair or defaults."""

import json
from typing import Any, cast

from multi_agent.core.planning import (
    DependencyReceipt,
    PlanningAttempt,
    PlanningInput,
    PlanningProvenance,
    PlanningRequest,
    PlanningReview,
    UserDecision,
    planning_json,
)
from multi_agent.core.planning_events import (
    PlanningAttemptStarted,
    PlanningDiagnostic,
    PlanningDraftValidated,
    PlanningEnvelope,
    PlanningEnvironmentObserved,
    PlanningEvent,
    PlanningEvidenceRejected,
    PlanningExecutionPrepared,
    PlanningReconciled,
    PlanningReviewValidated,
    PlanningTaskBound,
    PlanningValidationRejected,
)
from multi_agent.core.planning_execution import (
    PlanningAttemptContext,
    PlanningExecutionPolicy,
    PlanningProviderPolicy,
    PlanningRunOwner,
)
from multi_agent.core.tasks import FrozenProjectRules, TaskCard

from .task_codec import _RUNTIME_TYPES, _error, _event, _rules, _runtime


def _input(value: dict[str, Any]) -> PlanningInput:
    rules = value["frozen_rules"]
    return PlanningInput(
        requirements=value["requirements"],
        frozen_rules=FrozenProjectRules(
            rules["repository"],
            rules["baseline_commit"],
            rules["blob_oid"],
            rules["raw_text"],
            _rules(rules["rules"]),
        ),
        draft_request=PlanningRequest(**value["draft_request"]),
        review_request=PlanningRequest(**value["review_request"]),
        dependencies=tuple(DependencyReceipt(**item) for item in value["dependencies"]),
        decisions=tuple(UserDecision(**item) for item in value["decisions"]),
    )


def _review(value: dict[str, Any]) -> PlanningReview:
    return PlanningReview(
        value["task_hash"],
        value["decision"],
        value["reason"],
        tuple(value["blocking_issues"]),
    )


def _started(value: dict[str, Any]) -> PlanningAttemptStarted:
    return PlanningAttemptStarted(
        request=PlanningRequest(**value["request"]),
        prompt=value["prompt"],
        prompt_template_version=value["prompt_template_version"],
        started_at=value["started_at"],
        environment=value["environment"],
        draft_attempt=value["draft_attempt"],
        task_hash=value["task_hash"],
    )


def _reconciled(value: dict[str, Any]) -> PlanningReconciled:
    return PlanningReconciled(
        value["process_stopped"],
        value["detail"],
        value["log_path"],
        value["log_sha256"],
    )


def _prepared(value: dict[str, Any]) -> PlanningExecutionPrepared:
    policy = value["policy"]
    owner = value["run_owner"]
    context = value["attempt_context"]
    return PlanningExecutionPrepared(
        source_snapshot_id=value["source_snapshot_id"],
        policy=PlanningExecutionPolicy(
            providers=tuple(
                PlanningProviderPolicy(
                    provider=item["provider"],
                    executable=item["executable"],
                    executable_identity=tuple(item["executable_identity"]),
                    executable_sha256=item["executable_sha256"],
                    max_attempts=item["max_attempts"],
                )
                for item in policy["providers"]
            ),
            timeout=policy["timeout"],
            grace=policy["grace"],
        ),
        run_owner=PlanningRunOwner(
            root=owner["root"],
            identity=tuple(owner["identity"]),
            token=owner["token"],
            manifest_sha256=owner["manifest_sha256"],
        ),
        attempt_context=PlanningAttemptContext(
            path=context["path"],
            identity=tuple(context["identity"]),
            token=context["token"],
            digest=context["digest"],
            manifest_sha256=context["manifest_sha256"],
        ),
    )


def _planning_event(value: dict[str, Any]) -> PlanningEvent:
    kind = value["kind"]
    if kind == PlanningAttemptStarted.kind:
        return _started(value)
    if kind == PlanningExecutionPrepared.kind:
        return _prepared(value)
    if kind == PlanningEnvironmentObserved.kind:
        return PlanningEnvironmentObserved(value["environment"])
    if kind == PlanningEvidenceRejected.kind:
        return PlanningEvidenceRejected(value["detail"])
    if kind == PlanningDraftValidated.kind:
        return PlanningDraftValidated(TaskCard.from_dict(value["card"]))
    if kind == PlanningReviewValidated.kind:
        return PlanningReviewValidated(_review(value["review"]))
    if kind == PlanningValidationRejected.kind:
        return PlanningValidationRejected(value["detail"])
    if kind == PlanningReconciled.kind:
        return _reconciled(value)
    if kind == PlanningDiagnostic.kind:
        return PlanningDiagnostic(_error(value["diagnostic"]))
    if kind == PlanningTaskBound.kind:
        return PlanningTaskBound(value["task_run_id"])
    if kind in _RUNTIME_TYPES:
        return cast(
            PlanningEvent,
            _event(
                {
                    "type": kind,
                    **{key: item for key, item in value.items() if key != "kind"},
                }
            ),
        )
    raise ValueError(f"unknown stored planning event: {kind}")


def _attempt(value: dict[str, Any]) -> PlanningAttempt:
    return PlanningAttempt(
        request=_started(value["request"]),
        runtime=_runtime(value["runtime"]),
        reconciliation=_reconciled(value["reconciliation"])
        if value["reconciliation"] is not None
        else None,
        events=tuple(_planning_event(event) for event in value["events"]),
    )


def encode_planning_input(value: PlanningInput) -> str:
    return planning_json(value)


def decode_planning_input(encoded: str) -> PlanningInput:
    return _input(json.loads(encoded))


def encode_planning_review(value: PlanningReview) -> str:
    return planning_json(value)


def decode_planning_review(encoded: str) -> PlanningReview:
    return _review(json.loads(encoded))


def encode_planning_attempt(value: PlanningAttempt) -> str:
    return planning_json(value)


def decode_planning_attempt(encoded: str) -> PlanningAttempt:
    return _attempt(json.loads(encoded))


def encode_planning_envelope(value: PlanningEnvelope) -> str:
    return planning_json(value)


def decode_planning_envelope(encoded: str) -> PlanningEnvelope:
    value = json.loads(encoded)
    return PlanningEnvelope(
        value["stage"],
        value["attempt"],
        _planning_event(value["event"]),
        value["occurred_at"],
    )


def encode_planning_provenance(value: PlanningProvenance) -> str:
    return planning_json(value)


def decode_planning_provenance(encoded: str) -> PlanningProvenance:
    value = json.loads(encoded)
    return PlanningProvenance(
        _input(value["planning_input"]),
        TaskCard.from_dict(value["card"]),
        _attempt(value["draft"]),
        _attempt(value["review"]),
        _review(value["review_artifact"]),
    )
