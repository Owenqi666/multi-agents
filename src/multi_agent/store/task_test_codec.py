"""Explicit test-stage codecs; old task and snapshot encodings stay unchanged."""

import json
from typing import Any

from multi_agent.core.events import FailureCategory
from multi_agent.core.task_events import TaskCandidateFrozen
from multi_agent.core.task_execution import task_execution_json
from multi_agent.core.task_test_events import (
    TaskTestAttemptFinished,
    TaskTestAttemptStarted,
    TaskTestCancelRequested,
    TaskTestCommandExited,
    TaskTestCommandStarted,
    TaskTestEnvelope,
    TaskTestEvent,
    TaskTestProblem,
    TaskTestReconciled,
)
from multi_agent.core.task_test_input import TaskTestInput
from multi_agent.core.task_test_state import TaskTestAttempt, TaskTestCommandState
from multi_agent.core.tasks import NamedCommand

from .task_codec import _candidate


def _input(value: dict[str, Any]) -> TaskTestInput:
    if value["candidate"]["kind"] != TaskCandidateFrozen.kind:
        raise ValueError("stored test candidate has another event kind")
    return TaskTestInput(
        version=value["version"],
        task_run_id=value["task_run_id"],
        implementation_attempt=value["implementation_attempt"],
        task_id=value["task_id"],
        task_hash=value["task_hash"],
        rules_hash=value["rules_hash"],
        task_input_hash=value["task_input_hash"],
        candidate=_candidate(value["candidate"]),
        commands=tuple(
            NamedCommand(item["name"], item["executable"], tuple(item["args"]))
            for item in value["commands"]
        ),
        declaration_source=value["declaration_source"],
        source_kind=value["source_kind"],
        task_history_length=value["task_history_length"],
        task_history_hash=value["task_history_hash"],
    )


def _boolean(value: Any) -> bool:
    if not isinstance(value, bool):
        raise TypeError("stored test flag is not a boolean")
    return value


def _problem(value: dict[str, Any]) -> TaskTestProblem:
    if value["kind"] != TaskTestProblem.kind:
        raise ValueError("stored test problem has another event kind")
    return TaskTestProblem(
        value["sequence"],
        FailureCategory(value["category"]) if value["category"] is not None else None,
        value["detail"],
    )


def _reconciled(value: dict[str, Any]) -> TaskTestReconciled:
    if value["kind"] != TaskTestReconciled.kind:
        raise ValueError("stored test reconciliation has another event kind")
    stopped = value["process_stopped"]
    return TaskTestReconciled(
        value["sequence"],
        None if stopped is None else _boolean(stopped),
        value["detail"],
        value["receipt_path"],
        value["receipt_sha256"],
        _boolean(value["evidence_qualified"]),
    )


def _event(value: dict[str, Any]) -> TaskTestEvent:
    kind = value["kind"]
    if kind == TaskTestAttemptStarted.kind:
        return TaskTestAttemptStarted(_input(value["input"]))
    if kind == TaskTestCommandStarted.kind:
        return TaskTestCommandStarted(value["sequence"])
    if kind == TaskTestCommandExited.kind:
        return TaskTestCommandExited(value["sequence"], value["exit_code"])
    if kind == TaskTestProblem.kind:
        return _problem(value)
    if kind == TaskTestReconciled.kind:
        return _reconciled(value)
    if kind == TaskTestCancelRequested.kind:
        return TaskTestCancelRequested()
    if kind == TaskTestAttemptFinished.kind:
        return TaskTestAttemptFinished()
    raise ValueError(f"unknown test event kind: {kind}")


def encode_task_test_input(value: TaskTestInput) -> str:
    return task_execution_json(value)


def decode_task_test_input(encoded: str) -> TaskTestInput:
    return _input(json.loads(encoded))


def encode_task_test_envelope(envelope: TaskTestEnvelope) -> str:
    return task_execution_json(envelope)


def decode_task_test_envelope(encoded: str) -> TaskTestEnvelope:
    value = json.loads(encoded)
    return TaskTestEnvelope(
        value["implementation_attempt"],
        value["test_attempt"],
        _event(value["event"]),
        value["occurred_at"],
    )


def encode_task_test_attempt(attempt: TaskTestAttempt) -> str:
    return task_execution_json(attempt)


def decode_task_test_attempt(encoded: str) -> TaskTestAttempt:
    value = json.loads(encoded)
    return TaskTestAttempt(
        number=value["number"],
        input=_input(value["input"]),
        commands=tuple(
            TaskTestCommandState(
                item["sequence"],
                item["exit_code"],
                _reconciled(item["reconciliation"])
                if item["reconciliation"] is not None
                else None,
                tuple(_problem(problem) for problem in item["problems"]),
            )
            for item in value["commands"]
        ),
        problems=tuple(_problem(item) for item in value["problems"]),
        cancel_requested=_boolean(value["cancel_requested"]),
        finished=_boolean(value["finished"]),
        reconciliation=_reconciled(value["reconciliation"])
        if value["reconciliation"] is not None
        else None,
        events=tuple(_event(item) for item in value["events"]),
    )
