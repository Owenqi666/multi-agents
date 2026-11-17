"""Seal actual command reports and bounded logs against one prepared candidate."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, cast

from multi_agent.adapters.ownership import RuntimeOwnershipReceipt
from multi_agent.adapters.test_isolation import PROFILE, test_policy_config
from multi_agent.adapters.test_runtime import TestCommandResult
from multi_agent.core import Disposition
from multi_agent.core.events import JSONValue, freeze_json, thaw_json
from multi_agent.core.task_execution import task_execution_hash, task_execution_value
from multi_agent.core.task_test_events import (
    TaskTestCommandReported,
    TaskTestExecutionPrepared,
)
from multi_agent.core.task_test_state import TaskTestAttempt
from multi_agent.store.task_test_store import TaskTestHistory, TaskTestStore

from .planning_evidence import _receipt
from .task_context import (
    _canonical,
    _check_lease,
    _directory,
    _identity,
    _json,
    _json_bytes,
    _stable,
    _write_control,
)
from .task_test_context import TaskTestLaunch, _inspect_context
from .task_test_inputs import prepare_task_test_input
from .workspaces import ExecutorLease

_VERSION = "codex-cli 0.169.0"
_LIMITATIONS = {
    "command_group_gone": None,
    "all_descendants_stopped": None,
    "producer_stdout_eof": None,
    "producer_stderr_eof": None,
}


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskTestCommandManifest:
    path: str
    sha256: str
    task_run_id: str
    implementation_attempt: int
    test_attempt: int
    sequence: int
    input_hash: str
    prepared_hash: str
    history_length: int
    history_hash: str
    actual_result: JSONValue
    process_stopped: bool | None
    qualified: bool
    validation_error: str | None
    version: int = 1
    stop_scope: str = "command-request-and-owned-runtime-v1"
    output_scope: str = "server-emitted-notifications-v1"
    command_group_gone: None = None
    all_descendants_stopped: None = None
    producer_stdout_eof: None = None
    producer_stderr_eof: None = None

    @property
    def receipt(self) -> RuntimeOwnershipReceipt:
        return _receipt(cast(dict[str, Any], thaw_json(self.actual_result))["receipt"])


def command_process_stopped(result: TestCommandResult) -> bool | None:
    if not result.receipt.settled:
        return None
    if not result.receipt.processes_stopped:
        return False
    if result.request_attempted and not result.request_settled:
        return None
    return True


def _read(
    path: Path, budget: int, *, expected_size: int | None = None
) -> tuple[dict[str, Any], bytes]:
    _canonical(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size > budget
            or stat.S_IMODE(before.st_mode) != 0o400
        ):
            raise ValueError("test archive file is not a bounded sealed regular file")
        if expected_size is not None and before.st_size != expected_size:
            raise ValueError("test log size changed")
        with os.fdopen(os.dup(fd), "rb") as stream:
            raw = stream.read(budget + 1)
        if (
            len(raw) > budget
            or _stable(before) != _stable(os.fstat(fd))
            or _stable(before) != _stable(path.lstat())
        ):
            raise ValueError("test archive file changed while reading")
        return {
            "identity": _identity(before),
            "byte_count": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }, raw
    finally:
        os.close(fd)


def _result(value: Any) -> tuple[dict[str, Any], RuntimeOwnershipReceipt]:
    if not isinstance(value, dict) or set(value) != {
        item.name for item in fields(TestCommandResult)
    }:
        raise ValueError("test result does not have the frozen runtime fields")
    for name in ("sequence", "stdout_received_bytes", "stderr_received_bytes"):
        if type(value[name]) is not int or value[name] < (
            1 if name == "sequence" else 0
        ):
            raise ValueError("test result has an invalid counter")
    for name in (
        "request_attempted",
        "request_settled",
        "timeout_observed",
        "cancel_accepted",
        "output_limit_reached",
    ):
        if type(value[name]) is not bool:
            raise ValueError("test result has an invalid flag")
    for name in ("stdout_cap_reached", "stderr_cap_reached"):
        if value[name] is not None and type(value[name]) is not bool:
            raise ValueError("test result has an invalid cap observation")
    for name in ("reported_exit_code", "exit_code", "request_id"):
        if value[name] is not None and type(value[name]) is not int:
            raise ValueError("test result has an invalid reported integer")
    if value["request_id"] is not None and value["request_id"] < 1:
        raise ValueError("test result request id is out of range")
    if value["process_id"] is not None and not isinstance(value["process_id"], str):
        raise ValueError("test result has an invalid process identifier")
    response = value["response"]
    if response is not None and (
        not isinstance(response, dict)
        or type(response.get("exitCode")) is not int
        or response["exitCode"] != value["reported_exit_code"]
        or not isinstance(response.get("stdout"), str)
        or not isinstance(response.get("stderr"), str)
    ):
        raise ValueError("test response does not match its reported code")
    if value["request_settled"] and (
        not value["request_attempted"]
        or response is None
        or value["request_id"] is None
        or not value["process_id"]
    ):
        raise ValueError("test request settlement lacks a matching response")
    if not isinstance(value["problems"], list) or not isinstance(
        value["observations"], dict
    ):
        raise TypeError("test result observations are malformed")
    return value, _receipt(value["receipt"])


def _stopped(value: dict[str, Any], receipt: RuntimeOwnershipReceipt) -> bool | None:
    if not receipt.settled:
        return None
    if not receipt.processes_stopped:
        return False
    if value["request_attempted"] and not value["request_settled"]:
        return None
    return True


def _logs(
    directory: Path,
    prepared: TaskTestExecutionPrepared,
    receipt: RuntimeOwnershipReceipt,
) -> list[dict[str, Any]]:
    if {Path(item.path).name for item in receipt.logs} != {
        "stdout.bin",
        "stderr.bin",
        "wire.jsonl",
    } or len(receipt.logs) != 3:
        raise ValueError("test command must have exactly its three owned logs")
    output = []
    for log in receipt.logs:
        if (
            Path(log.path).parent != directory
            or not log.sealed
            or not log.healthy
            or not log.closed
            or log.byte_count is None
        ):
            raise ValueError("test log is outside the owned directory or is not sealed")
        budget = (
            prepared.policy.output_bytes_limit * 4 + 8 * 1024 * 1024
            if Path(log.path).name == "wire.jsonl"
            else prepared.policy.output_bytes_limit
        )
        metadata, _ = _read(Path(log.path), budget, expected_size=log.byte_count)
        if metadata["identity"] != log.identity or metadata["sha256"] != log.sha256:
            raise ValueError(
                "test log identity or bytes differ from the actual receipt"
            )
        output.append({"path": log.path, **metadata})
    return output


def _qualified(
    value: dict[str, Any],
    receipt: RuntimeOwnershipReceipt,
    selected: TaskTestAttempt,
    sequence: int,
) -> bool:
    command = next(
        (item for item in selected.commands if item.sequence == sequence), None
    )
    reports = [
        event
        for event in selected.events
        if isinstance(event, TaskTestCommandReported) and event.sequence == sequence
    ]
    report = reports[-1] if reports else None
    code = value["reported_exit_code"]
    environment = value["environment"]
    counts = {Path(item.path).name: item.byte_count for item in receipt.logs}
    return bool(
        value["sequence"] == sequence
        and value["request_attempted"]
        and value["request_settled"]
        and _stopped(value, receipt) is True
        and receipt.resources_released
        and receipt.logs_sealed
        and receipt.outputs_complete
        and counts.get("stdout.bin") == value["stdout_received_bytes"]
        and counts.get("stderr.bin") == value["stderr_received_bytes"]
        and value["response"] is not None
        and type(code) is int
        and 0 <= code <= 255
        and code != 124
        and value["exit_code"] == code
        and isinstance(value["exit_code_provenance"], str)
        and value["exit_code_provenance"]
        and isinstance(environment, dict)
        and environment.get("cli_version") == _VERSION
        and not value["timeout_observed"]
        and not value["cancel_accepted"]
        and not value["output_limit_reached"]
        and value["stdout_cap_reached"] is not True
        and value["stderr_cap_reached"] is not True
        and not value["problems"]
        and not selected.cancel_requested
        and not selected.problems
        and not any(item.problems for item in selected.commands)
        and command is not None
        and command.exit_code == code
        and report is not None
        and report.reported_exit_code == code
        and task_execution_hash(report.response)
        == task_execution_hash(value["response"])
    )


def _audit_matches(
    value: dict[str, Any],
    prepared: TaskTestExecutionPrepared,
    receipt: RuntimeOwnershipReceipt,
) -> bool:
    observed = value["observations"]
    audit = observed.get("policy_audit")
    if (
        len(receipt.isolations) != 1
        or not isinstance(audit, dict)
        or "requirements" not in audit
        or audit["requirements"] is not None
        or observed.get("version") != _VERSION
    ):
        return False
    root = receipt.isolations[0].root
    if observed.get("isolation_root") != root:
        return False
    expected = test_policy_config(prepared.checkout, prepared.scratch, prepared.policy)
    expected["permissions"][PROFILE]["filesystem"][root] = "deny"

    def same(actual: Any, requested: Any) -> bool:
        if not isinstance(requested, dict):
            return task_execution_hash(actual) == task_execution_hash(requested)
        return (
            isinstance(actual, dict)
            and requested.keys() <= actual.keys()
            and all(
                same(item, requested[key]) if key in requested else item is None
                for key, item in actual.items()
            )
        )

    layers = audit.get("layers")
    own = {"type": "user", "file": str(Path(root) / "codex/config.toml")}
    return (
        same(audit.get("config"), expected)
        and isinstance(layers, list)
        and sum(
            isinstance(item, dict)
            and all(item.get(key) == val for key, val in own.items())
            for item in layers
        )
        == 1
    )


def _current_source(
    store: TaskTestStore, prepared: TaskTestExecutionPrepared, lease: ExecutorLease
) -> None:
    value = prepared.checkout.input
    actual = prepare_task_test_input(
        store,
        value.task_run_id,
        value.implementation_attempt,
        lease,
        required_commands=tuple(command.name for command in value.commands),
        declaration_source=value.declaration_source,
    )
    if task_execution_hash(actual) != task_execution_hash(value):
        raise ValueError("test command original candidate or source history changed")


def _selection(
    store: TaskTestStore, run_id: str, impl: int, number: int, sequence: int
) -> tuple[TaskTestAttempt, TaskTestHistory]:
    state, history = store.load_task_test_checkpoint(run_id, impl)
    selected = state.current_attempt if state is not None else None
    if (
        selected is None
        or selected.number != number
        or not any(item.sequence == sequence for item in selected.commands)
    ):
        raise ValueError("test archive does not identify the current known command")
    return selected, history


def _recheck_selection(
    store: TaskTestStore,
    run_id: str,
    impl: int,
    number: int,
    sequence: int,
    selected: TaskTestAttempt,
    history: TaskTestHistory,
) -> None:
    actual, latest = _selection(store, run_id, impl, number, sequence)
    if (
        task_execution_hash(actual) != task_execution_hash(selected)
        or len(latest) < len(history)
        or task_execution_hash(latest[: len(history)]) != task_execution_hash(history)
        or any(
            disposition != Disposition.HISTORY_ONLY
            for _, disposition in latest[len(history) :]
        )
    ):
        raise ValueError("test command or original history changed during inspection")


def _payload(
    prepared: TaskTestExecutionPrepared,
    number: int,
    sequence: int,
    history: TaskTestHistory,
    result: dict[str, Any],
    stopped: bool | None,
    logs: list[dict[str, Any]],
    qualified: bool,
    error: str | None,
    directory_identity: tuple[int, int],
) -> dict[str, Any]:
    value = prepared.checkout.input
    return {
        "version": 1,
        "task_run_id": value.task_run_id,
        "implementation_attempt": value.implementation_attempt,
        "test_attempt": number,
        "sequence": sequence,
        "input_hash": value.input_hash,
        "prepared_hash": task_execution_hash(prepared),
        "prepared": task_execution_value(prepared),
        "command": task_execution_value(value.commands[sequence - 1]),
        "history_length": len(history),
        "history_hash": task_execution_hash(history),
        "actual_result": result,
        "process_stopped": stopped,
        "qualified": qualified,
        "validation_error": error,
        "stop_scope": prepared.policy.required_stop_scope,
        "output_scope": prepared.policy.output_scope,
        "logs": logs,
        "directory_identity": directory_identity,
        **_LIMITATIONS,
    }


def _manifest(
    path: Path,
    digest: str,
    payload: dict[str, Any],
    *,
    qualified: bool,
    error: str | None,
) -> TaskTestCommandManifest:
    return TaskTestCommandManifest(
        path=str(path),
        sha256=digest,
        task_run_id=payload["task_run_id"],
        implementation_attempt=payload["implementation_attempt"],
        test_attempt=payload["test_attempt"],
        sequence=payload["sequence"],
        input_hash=payload["input_hash"],
        prepared_hash=payload["prepared_hash"],
        history_length=payload["history_length"],
        history_hash=payload["history_hash"],
        actual_result=freeze_json(payload["actual_result"]),
        process_stopped=payload["process_stopped"],
        qualified=qualified,
        validation_error=error,
    )


def seal_task_test_command(
    launch: TaskTestLaunch, sequence: int, result: TestCommandResult
) -> TaskTestCommandManifest:
    launch.consume_seal(sequence)
    value = launch.prepared.checkout.input
    actual, receipt = _result(task_execution_value(result))
    if actual["sequence"] != sequence:
        raise ValueError("test result belongs to another command")
    stopped = _stopped(actual, receipt)
    prepared = _inspect_context(
        launch.store,
        value.task_run_id,
        value.implementation_attempt,
        launch.test_attempt,
        launch.lease,
        checkout_controls=stopped is True,
    )
    if prepared != launch.prepared:
        raise ValueError("test launch preparation changed before sealing")
    selected, history = _selection(
        launch.store,
        value.task_run_id,
        value.implementation_attempt,
        launch.test_attempt,
        sequence,
    )
    directory = launch.log_dir / f"command-{sequence}"
    info = _directory(directory)
    marker = {
        "version": 1,
        "prepared_hash": task_execution_hash(prepared),
        "sequence": sequence,
        "actual_result": actual,
    }
    _write_control(directory / ".seal.json", _json_bytes(marker))
    logs, error = [], None
    try:
        logs = _logs(directory, prepared, receipt)
        if stopped is True:
            _current_source(launch.store, prepared, launch.lease)
            if not _audit_matches(actual, prepared, receipt):
                raise ValueError("test command has no matching actual permission audit")
    except (OSError, ValueError) as failure:
        error = str(failure)
    qualified = error is None and _qualified(actual, receipt, selected, sequence)
    if not qualified and error is None:
        error = "Command report, ownership, output, or current test state is incomplete or negative"
    _recheck_selection(
        launch.store,
        value.task_run_id,
        value.implementation_attempt,
        launch.test_attempt,
        sequence,
        selected,
        history,
    )
    payload = _payload(
        prepared,
        launch.test_attempt,
        sequence,
        history,
        actual,
        stopped,
        logs,
        qualified,
        error,
        _identity(info),
    )
    metadata = _write_control(directory / "execution.json", _json_bytes(payload))
    return _manifest(
        directory / "execution.json",
        metadata["sha256"],
        payload,
        qualified=qualified,
        error=error,
    )


def inspect_task_test_command(
    store: TaskTestStore,
    run_id: str,
    implementation_attempt: int,
    test_attempt: int,
    sequence: int,
    lease: ExecutorLease,
) -> TaskTestCommandManifest:
    _check_lease(lease)
    selected, history = _selection(
        store, run_id, implementation_attempt, test_attempt, sequence
    )
    command = next(item for item in selected.commands if item.sequence == sequence)
    reconciliation = command.reconciliation
    if (
        reconciliation is None
        or reconciliation.receipt_path is None
        or reconciliation.receipt_sha256 is None
    ):
        raise ValueError("test command has no archive binding")
    prepared = _inspect_context(
        store,
        run_id,
        implementation_attempt,
        test_attempt,
        lease,
        checkout_controls=reconciliation.process_stopped is True,
    )
    directory = Path(prepared.context.path).parent / "logs" / f"command-{sequence}"
    path = directory / "execution.json"
    if reconciliation.receipt_path != str(path):
        raise ValueError("test command archive path changed")
    directory_info = _directory(directory)
    metadata, raw = _read(path, 8 * 1024 * 1024)
    if metadata["sha256"] != reconciliation.receipt_sha256:
        raise ValueError("test command archive hash changed")
    payload = _json(raw)
    result, receipt = _result(payload["actual_result"])
    length = payload["history_length"]
    if (
        type(length) is not int
        or length < 0
        or length > len(history)
        or task_execution_hash(history[:length]) != payload["history_hash"]
    ):
        raise ValueError("test command original history prefix changed")
    if any(payload.get(name) is not None for name in _LIMITATIONS):
        raise ValueError("test command archive invents unobserved process facts")
    expected = _payload(
        prepared,
        test_attempt,
        sequence,
        history[:length],
        result,
        _stopped(result, receipt),
        payload["logs"],
        payload["qualified"],
        payload["validation_error"],
        _identity(directory_info),
    )
    if (
        task_execution_hash(payload) != task_execution_hash(expected)
        or result["sequence"] != sequence
    ):
        raise ValueError("test command archive binding or observed facts changed")
    _, marker_raw = _read(directory / ".seal.json", 8 * 1024 * 1024)
    if task_execution_hash(_json(marker_raw)) != task_execution_hash(
        {
            "version": 1,
            "prepared_hash": task_execution_hash(prepared),
            "sequence": sequence,
            "actual_result": result,
        }
    ):
        raise ValueError("test command seal does not bind its actual result")
    error = None
    try:
        logs = _logs(directory, prepared, receipt)
        if task_execution_hash(logs) != task_execution_hash(payload["logs"]):
            raise ValueError("test command log metadata changed")
        if payload["process_stopped"] is not reconciliation.process_stopped:
            raise ValueError("test command stopping differs from its reconciliation")
        if payload["process_stopped"] is True:
            _current_source(store, prepared, lease)
            if not _audit_matches(result, prepared, receipt):
                raise ValueError("test command has no matching actual permission audit")
    except (OSError, ValueError) as failure:
        error = str(failure)
    qualified = error is None and _qualified(result, receipt, selected, sequence)
    if not qualified and error is None:
        error = "Current command evidence is incomplete or negative"
    _recheck_selection(
        store, run_id, implementation_attempt, test_attempt, sequence, selected, history
    )
    _check_lease(lease)
    return _manifest(
        path, metadata["sha256"], payload, qualified=qualified, error=error
    )
