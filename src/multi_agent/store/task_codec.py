"""Encode trusted task state with explicit constructors and preserved evidence.

Schema and provider validation belong to the CLI boundary. Missing stored fields
or invalid enum values raise directly; this codec does not repair stored data.
"""

import json
from dataclasses import asdict, fields
from typing import Any

from multi_agent.core.dispatch import (
    DispatchDecision,
    ImplementationChoice,
    SelectionRecord,
)
from multi_agent.core.events import (
    AdapterEvent,
    CancelRequested,
    Completed,
    Error,
    FailureCategory,
    Message,
    ProcessExited,
    SessionStarted,
    Usage,
    thaw_json,
)
from multi_agent.core.state import AttemptState, AttemptStatus, EvidenceSource
from multi_agent.core.task_events import (
    TaskAttemptStarted,
    TaskDiagnostic,
    TaskEnvelope,
    TaskEvent,
    TaskReconciled,
    TaskScopeBlocked,
)
from multi_agent.core.task_state import TaskAttempt, TaskInput
from multi_agent.core.tasks import (
    FrozenProjectRules,
    ImplementationConfig,
    ImplementationRule,
    NamedCommand,
    ProjectRules,
    TaskCard,
    TaskTypeRestriction,
)

_RUNTIME_TYPES: dict[str, type[AdapterEvent | CancelRequested]] = {
    event.kind: event
    for event in (
        CancelRequested,
        SessionStarted,
        Message,
        Usage,
        Completed,
        Error,
        ProcessExited,
    )
}


def _encode(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _implementation(value: dict[str, Any]) -> ImplementationConfig:
    return ImplementationConfig(
        value["provider"],
        value["default_model"],
        value["default_effort"],
        tuple(value["allowed_models"]),
    )


def _rules(value: dict[str, Any]) -> ProjectRules:
    return ProjectRules(
        version=value["version"],
        commands=tuple(
            NamedCommand(item["name"], item["executable"], tuple(item["args"]))
            for item in value["commands"]
        ),
        protected_paths=tuple(value["protected_paths"]),
        implementation=tuple(
            ImplementationRule(tuple(item["paths"]), _implementation(item["config"]))
            for item in value["implementation"]
        ),
        default_implementation=(
            _implementation(value["default_implementation"])
            if value["default_implementation"] is not None
            else None
        ),
        task_types=tuple(
            TaskTypeRestriction(
                item["name"], item["provider"], tuple(item["allowed_models"])
            )
            for item in value["task_types"]
        ),
    )


def encode_task_input(value: TaskInput) -> str:
    return _encode(
        {
            "card": value.card.to_dict(),
            "frozen_rules": asdict(value.frozen_rules),
            "requirements": value.requirements,
            "created_at": value.created_at,
        }
    )


def decode_task_input(encoded: str) -> TaskInput:
    value = json.loads(encoded)
    rules = value["frozen_rules"]
    return TaskInput(
        TaskCard.from_dict(value["card"]),
        FrozenProjectRules(
            rules["repository"],
            rules["baseline_commit"],
            rules["blob_oid"],
            rules["raw_text"],
            _rules(rules["rules"]),
        ),
        value["requirements"],
        value["created_at"],
    )


def _decision(value: dict[str, Any]) -> DispatchDecision:
    return DispatchDecision(
        choice=ImplementationChoice(**value["choice"]),
        reason=value["reason"],
        selection_history=tuple(
            SelectionRecord(ImplementationChoice(**item["choice"]), item["outcome"])
            for item in value["selection_history"]
        ),
        matched_rule_indices=tuple(value["matched_rule_indices"]),
        used_default=value["used_default"],
        task_hash=value["task_hash"],
        rules_hash=value["rules_hash"],
        task_type=value["task_type"],
        requested=(
            ImplementationChoice(**value["requested"])
            if value["requested"] is not None
            else None
        ),
        unavailable_models=tuple(
            (item[0], item[1]) for item in value["unavailable_models"]
        ),
        allowed_models=tuple(value["allowed_models"]),
    )


def _started(value: dict[str, Any]) -> TaskAttemptStarted:
    return TaskAttemptStarted(
        _decision(value["decision"]),
        value["worktree"],
        value["prompt_template_version"],
        value["started_at"],
        value["environment"],
    )


def _event_payload(event: TaskEvent) -> dict[str, Any]:
    if isinstance(event, TaskAttemptStarted):
        return {
            "type": event.kind,
            "decision": asdict(event.decision),
            "worktree": event.worktree,
            "prompt_template_version": event.prompt_template_version,
            "started_at": event.started_at,
            "environment": thaw_json(event.environment),
        }
    if isinstance(event, TaskDiagnostic):
        return {"type": event.kind, "diagnostic": _event_payload(event.diagnostic)}
    result = {field.name: getattr(event, field.name) for field in fields(event)}
    result["type"] = event.kind
    if isinstance(event, Completed):
        result["result"] = thaw_json(event.result)
    elif isinstance(event, Usage):
        result["counts"] = dict(event.counts)
    return result


def _error(value: dict[str, Any]) -> Error:
    return Error(
        value["detail"],
        FailureCategory(value["failure_category"])
        if value["failure_category"] is not None
        else None,
        value["retry_after"],
        raw_type=value["raw_type"],
    )


def _event(value: dict[str, Any]) -> TaskEvent:
    kind = value["type"]
    if kind == TaskAttemptStarted.kind:
        return _started(value)
    if kind == TaskReconciled.kind:
        return TaskReconciled(
            value["worktree_digest"], value["process_stopped"], value["detail"]
        )
    if kind == TaskScopeBlocked.kind:
        return TaskScopeBlocked(value["reason"], tuple(value["required_paths"]))
    if kind == TaskDiagnostic.kind:
        return TaskDiagnostic(_error(value["diagnostic"]))
    if kind == Error.kind:
        return _error(value)
    event_type = _RUNTIME_TYPES[kind]
    return event_type(**{key: item for key, item in value.items() if key != "type"})


def encode_task_envelope(envelope: TaskEnvelope) -> str:
    return _encode(
        {
            "attempt": envelope.attempt,
            "occurred_at": envelope.occurred_at,
            "event": _event_payload(envelope.event),
        }
    )


def decode_task_envelope(encoded: str) -> TaskEnvelope:
    value = json.loads(encoded)
    return TaskEnvelope(value["attempt"], _event(value["event"]), value["occurred_at"])


def _runtime_payload(runtime: AttemptState) -> dict[str, Any]:
    result = {field.name: getattr(runtime, field.name) for field in fields(runtime)}
    result["result"] = thaw_json(runtime.result)
    for name in ("reported_model_source", "reported_effort_source"):
        source = getattr(runtime, name)
        result[name] = asdict(source) if source is not None else None
    return result


def _runtime(value: dict[str, Any]) -> AttemptState:
    return AttemptState(
        number=value["number"],
        status=AttemptStatus(value["status"]),
        failure_category=(
            FailureCategory(value["failure_category"])
            if value["failure_category"] is not None
            else None
        ),
        detail=value["detail"],
        retry_after=value["retry_after"],
        session_id=value["session_id"],
        reported_model=value["reported_model"],
        reported_model_source=(
            EvidenceSource(**value["reported_model_source"])
            if value["reported_model_source"] is not None
            else None
        ),
        reported_effort=value["reported_effort"],
        reported_effort_source=(
            EvidenceSource(**value["reported_effort_source"])
            if value["reported_effort_source"] is not None
            else None
        ),
        result=value["result"],
        exit_code=value["exit_code"],
    )


def encode_task_attempt(attempt: TaskAttempt) -> str:
    return _encode(
        {
            "request": _event_payload(attempt.request),
            "runtime": _runtime_payload(attempt.runtime),
            "reconciliation": asdict(attempt.reconciliation)
            if attempt.reconciliation is not None
            else None,
        }
    )


def decode_task_attempt(encoded: str) -> TaskAttempt:
    value = json.loads(encoded)
    return TaskAttempt(
        _started(value["request"]),
        _runtime(value["runtime"]),
        TaskReconciled(**value["reconciliation"])
        if value["reconciliation"] is not None
        else None,
    )
