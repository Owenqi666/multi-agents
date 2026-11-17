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
    TaskTestCommandReported,
    TaskTestCommandStarted,
    TaskTestEnvelope,
    TaskTestEvent,
    TaskTestExecutionPrepared,
    TaskTestProblem,
    TaskTestReconciled,
)
from multi_agent.core.task_test_execution import (
    TaskTestCheckout,
    TaskTestContext,
    TaskTestPolicy,
    TaskTestScratch,
)
from multi_agent.core.task_test_input import TaskTestInput
from multi_agent.core.task_test_state import TaskTestAttempt, TaskTestCommandState
from multi_agent.core.tasks import NamedCommand
from multi_agent.core.workspaces import WorkspaceControl

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


def _identity(value: list[int]) -> tuple[int, int]:
    left, right = value
    return left, right


def _prepared(value: dict[str, Any]) -> TaskTestExecutionPrepared:
    checkout, context, policy, scratch = (
        value["checkout"],
        value["context"],
        value["policy"],
        value["scratch"],
    )
    return TaskTestExecutionPrepared(
        TaskTestCheckout(
            version=checkout["version"],
            layout=checkout["layout"],
            input=_input(checkout["input"]),
            repository=checkout["repository"],
            database_path=checkout["database_path"],
            database_identity=_identity(checkout["database_identity"]),
            database_parent_identity=_identity(checkout["database_parent_identity"]),
            root=checkout["root"],
            root_identity=_identity(checkout["root_identity"]),
            git_directory=checkout["git_directory"],
            git_identity=_identity(checkout["git_identity"]),
            owner_token=checkout["owner_token"],
            object_format=checkout["object_format"],
            initial_digest=checkout["initial_digest"],
            controls=tuple(
                WorkspaceControl(
                    item["path"],
                    _identity(item["identity"])
                    if item["identity"] is not None
                    else None,
                    bytes.fromhex(item["content"]["hex"])
                    if item["content"] is not None
                    else None,
                )
                for item in checkout["controls"]
            ),
            objects=tuple((item[0], item[1]) for item in checkout["objects"]),
            observed_test_attempt=checkout["observed_test_attempt"],
            test_history_length=checkout["test_history_length"],
            test_history_hash=checkout["test_history_hash"],
        ),
        TaskTestContext(
            path=context["path"],
            identity=_identity(context["identity"]),
            token=context["token"],
            manifest_sha256=context["manifest_sha256"],
        ),
        TaskTestPolicy(
            version=policy["version"],
            executable=policy["executable"],
            executable_identity=_identity(policy["executable_identity"]),
            executable_sha256=policy["executable_sha256"],
            timeout=policy["timeout"],
            grace=policy["grace"],
            output_bytes_limit=policy["output_bytes_limit"],
            tool_directories=tuple(policy["tool_directories"]),
            runtime_read_paths=tuple(policy["runtime_read_paths"]),
            required_stop_scope=policy["required_stop_scope"],
            output_scope=policy["output_scope"],
        ),
        TaskTestScratch(
            path=scratch["path"],
            identity=_identity(scratch["identity"]),
            home_identity=_identity(scratch["home_identity"]),
            tmp_identity=_identity(scratch["tmp_identity"]),
            cache_identity=_identity(scratch["cache_identity"]),
        ),
    )


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
    if kind == TaskTestExecutionPrepared.kind:
        return _prepared(value)
    if kind == TaskTestCommandReported.kind:
        return TaskTestCommandReported(
            value["sequence"], value["reported_exit_code"], value["response"]
        )
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
