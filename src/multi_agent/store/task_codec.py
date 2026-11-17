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
    TaskCandidateFrozen,
    TaskDiagnostic,
    TaskEnvelope,
    TaskEnvironmentObserved,
    TaskEvent,
    TaskEvidenceRejected,
    TaskExecutionPrepared,
    TaskReconciled,
    TaskReworkStarted,
    TaskScopeBlocked,
)
from multi_agent.core.task_execution import (
    TaskDispatchPolicy,
    TaskExecutionContext,
    TaskExecutionPolicy,
    task_execution_value,
)
from multi_agent.core.task_rework import TaskReworkSource
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
from multi_agent.core.workspaces import (
    WorkspaceControl,
    WorkspaceDelivery,
    WorkspaceRecord,
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
        value.get("managed", False),
    )


def _rework_started(value: dict[str, Any]) -> TaskReworkStarted:
    source = value["source"]
    return TaskReworkStarted(
        _decision(value["decision"]),
        value["worktree"],
        value["prompt_template_version"],
        value["started_at"],
        value["environment"],
        value["managed"],
        source=TaskReworkSource(
            task_run_id=source["task_run_id"],
            implementation_attempt=source["implementation_attempt"],
            task_input_hash=source["task_input_hash"],
            candidate_commit=source["candidate_commit"],
            candidate_hash=source["candidate_hash"],
            task_history_length=source["task_history_length"],
            task_history_hash=source["task_history_hash"],
            test_attempt=source["test_attempt"],
            test_history_length=source["test_history_length"],
            test_history_hash=source["test_history_hash"],
            review_round_id=source["review_round_id"],
            review_snapshot_id=source["review_snapshot_id"],
            review_checkpoint_hash=source["review_checkpoint_hash"],
            assessment=source["assessment"],
            version=source["version"],
        ),
    )


def _reconciliation_payload(event: TaskReconciled) -> dict[str, Any]:
    value: dict[str, Any] = {
        "worktree_digest": event.worktree_digest,
        "process_stopped": event.process_stopped,
        "detail": event.detail,
    }
    if event.log_path is not None or event.log_sha256 is not None:
        value["log_path"] = event.log_path
        value["log_sha256"] = event.log_sha256
    return value


def _reconciled(value: dict[str, Any]) -> TaskReconciled:
    return TaskReconciled(
        value["worktree_digest"],
        value["process_stopped"],
        value["detail"],
        value.get("log_path"),
        value.get("log_sha256"),
    )


def _workspace(value: dict[str, Any]) -> WorkspaceRecord:
    return WorkspaceRecord(
        repository=value["repository"],
        source=value["source"],
        task_baseline=value["task_baseline"],
        start_commit=value["start_commit"],
        root=value["root"],
        owner_token=value["owner_token"],
        root_identity=tuple(value["root_identity"]),
        git_directory=value["git_directory"],
        git_identity=tuple(value["git_identity"]),
        controls=tuple(
            WorkspaceControl(
                item["path"],
                tuple(item["identity"]) if item["identity"] is not None else None,
                bytes.fromhex(item["content"]["hex"])
                if item["content"] is not None
                else None,
            )
            for item in value["controls"]
        ),
        object_format=value["object_format"],
    )


def _prepared(value: dict[str, Any]) -> TaskExecutionPrepared:
    policy = value["policy"]
    dispatch = value["dispatch_policy"]
    context = value["context"]
    return TaskExecutionPrepared(
        workspace=_workspace(value["workspace"]),
        initial_workspace_digest=value["initial_workspace_digest"],
        prompt=value["prompt"],
        output_schema=value["output_schema"],
        policy=TaskExecutionPolicy(
            policy["provider"],
            policy["executable"],
            tuple(policy["executable_identity"]),
            policy["executable_sha256"],
            policy["max_attempts"],
            policy["timeout"],
            policy["grace"],
        ),
        dispatch_policy=TaskDispatchPolicy(
            dispatch["task_type"],
            ImplementationChoice(**dispatch["requested"])
            if dispatch["requested"] is not None
            else None,
            tuple(ImplementationChoice(**item) for item in dispatch["fallbacks"]),
        ),
        context=TaskExecutionContext(
            context["path"],
            tuple(context["identity"]),
            context["token"],
            context["manifest_sha256"],
        ),
    )


def _candidate(value: dict[str, Any]) -> TaskCandidateFrozen:
    delivery = value["delivery"]
    return TaskCandidateFrozen(
        WorkspaceDelivery(
            baseline=delivery["baseline"],
            candidate=delivery["candidate"],
            changed_paths=tuple(delivery["changed_paths"]),
            digest=delivery["digest"],
            ref=delivery["ref"],
            tree=delivery["tree"],
        ),
        value["execution_log_path"],
        value["execution_log_sha256"],
    )


def _event_payload(event: TaskEvent) -> dict[str, Any]:
    if isinstance(event, TaskReworkStarted):
        return {
            "type": event.kind,
            "decision": asdict(event.decision),
            "worktree": event.worktree,
            "prompt_template_version": event.prompt_template_version,
            "started_at": event.started_at,
            "environment": thaw_json(event.environment),
            "managed": event.managed,
            "source": task_execution_value(event.source),
        }
    if isinstance(event, TaskAttemptStarted):
        value: dict[str, Any] = {
            "type": event.kind,
            "decision": asdict(event.decision),
            "worktree": event.worktree,
            "prompt_template_version": event.prompt_template_version,
            "started_at": event.started_at,
            "environment": thaw_json(event.environment),
        }
        if event.managed:
            value["managed"] = True
        return value
    if isinstance(event, TaskExecutionPrepared):
        return {
            "type": event.kind,
            "workspace": task_execution_value(event.workspace),
            "initial_workspace_digest": event.initial_workspace_digest,
            "prompt": event.prompt,
            "output_schema": thaw_json(event.output_schema),
            "policy": asdict(event.policy),
            "dispatch_policy": asdict(event.dispatch_policy),
            "context": asdict(event.context),
        }
    if isinstance(event, TaskEnvironmentObserved):
        return {"type": event.kind, "environment": thaw_json(event.environment)}
    if isinstance(event, TaskCandidateFrozen):
        delivery = event.delivery
        return {
            "type": event.kind,
            "delivery": {
                "baseline": delivery.baseline,
                "candidate": delivery.candidate,
                "changed_paths": list(delivery.changed_paths),
                "digest": delivery.digest,
                "ref": delivery.ref,
                "tree": delivery.tree,
            },
            "execution_log_path": event.execution_log_path,
            "execution_log_sha256": event.execution_log_sha256,
        }
    if isinstance(event, TaskReconciled):
        return {"type": event.kind, **_reconciliation_payload(event)}
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
    if kind == TaskReworkStarted.kind:
        return _rework_started(value)
    if kind == TaskAttemptStarted.kind:
        return _started(value)
    if kind == TaskExecutionPrepared.kind:
        return _prepared(value)
    if kind == TaskEnvironmentObserved.kind:
        return TaskEnvironmentObserved(value["environment"])
    if kind == TaskEvidenceRejected.kind:
        return TaskEvidenceRejected(value["detail"])
    if kind == TaskCandidateFrozen.kind:
        return _candidate(value)
    if kind == TaskReconciled.kind:
        return _reconciled(value)
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
    value: dict[str, Any] = {
        "request": _event_payload(attempt.request),
        "runtime": _runtime_payload(attempt.runtime),
        "reconciliation": _reconciliation_payload(attempt.reconciliation)
        if attempt.reconciliation is not None
        else None,
    }
    if attempt.request.managed or attempt.events:
        value["events"] = [_event_payload(event) for event in attempt.events]
    return _encode(value)


def decode_task_attempt(encoded: str) -> TaskAttempt:
    value = json.loads(encoded)
    request = _event(value["request"])
    if not isinstance(request, TaskAttemptStarted):
        raise TypeError("task attempt request must be a task start event")
    return TaskAttempt(
        request,
        _runtime(value["runtime"]),
        _reconciled(value["reconciliation"])
        if value["reconciliation"] is not None
        else None,
        tuple(_event(event) for event in value.get("events", [])),
    )
