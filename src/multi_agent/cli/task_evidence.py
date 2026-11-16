"""Bind one live implementation owner and seal its actual facts once.

This boundary neither runs nor stops a runtime, and never writes task state.
Preparation, stopping, output qualification and delivery remain separate facts.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from multi_agent.adapters.base import EnvironmentInfo, TurnHandle, TurnResult
from multi_agent.adapters.implementation_runtime import ImplementationRuntimeAdapter
from multi_agent.adapters.ownership import RuntimeOwnershipReceipt
from multi_agent.adapters.runtime_decode import strict_json_loads
from multi_agent.core.events import CancelRequested, Completed, Error, ProcessExited
from multi_agent.core.state import AttemptStatus, Disposition
from multi_agent.core.task_events import (
    TaskEnvironmentObserved,
    TaskEvidenceRejected,
    TaskExecutionPrepared,
    TaskScopeBlocked,
)
from multi_agent.core.task_execution import task_execution_hash, task_execution_json
from multi_agent.core.task_execution import task_execution_value as _value
from multi_agent.core.task_state import TaskAttempt, TaskPhase, TaskState
from multi_agent.store.planning_store import PlanningStore

from .planning_evidence import _facts, _receipt
from .task_context import (
    TaskHistory,
    TaskLaunch,
    _canonical,
    _check_lease,
    _Database,
    _directory,
    _pair,
    _policy,
    _store_database,
    inspect_task_context,
)
from .task_output import TaskImplementationReport, parse_task_output
from .workspaces import ExecutorLease, inspect_workspace

MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_MARKER = ".seal-attempt.json"
_MANIFEST = "execution.json"
_NOFOLLOW = os.O_NOFOLLOW | os.O_NONBLOCK
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ISSUED = object()


@dataclass(frozen=True, slots=True)
class TaskExecutionManifest:
    path: str
    identity: tuple[int, int]
    byte_count: int
    sha256: str
    qualified: bool
    receipt: RuntimeOwnershipReceipt
    delivery_eligible: bool
    final_digest: str | None
    report: TaskImplementationReport | None
    validation_error: str | None
    actual_result: TurnResult
    environment: EnvironmentInfo | None


@dataclass(frozen=True, slots=True)
class _Logs:
    path: Path
    identity: tuple[int, int]


@dataclass(slots=True)
class _BindingState:
    launch: TaskLaunch
    store: PlanningStore
    lease: ExecutorLease
    adapter: ImplementationRuntimeAdapter
    run_id: str
    prepared: TaskExecutionPrepared
    database: _Database
    root_identity: tuple[int, int]
    logs: _Logs
    configuration: str
    binding: str
    consumed: bool = False


@dataclass(frozen=True, slots=True)
class TaskExecutionBinding:
    """Copies share the first seal attempt; durable files cannot issue a binding."""

    launch: TaskLaunch
    run_id: str
    adapter: ImplementationRuntimeAdapter
    _state: _BindingState = field(repr=False, compare=False)
    _issue: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._issue is not _ISSUED:
            raise ValueError("task execution bindings cannot be reconstructed")

    def __deepcopy__(self, memo: dict[int, Any]) -> TaskExecutionBinding:
        return self


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _stable(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _object(value: Any, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("invalid task execution evidence schema")
    return cast(dict[str, Any], value)


def _json_bytes(value: Any) -> bytes:
    raw = (task_execution_json(value) + "\n").encode("utf-8")
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError("task execution control file exceeds 16 MiB")
    return raw


def _json(raw: bytes) -> dict[str, Any]:
    value = strict_json_loads(raw.decode("utf-8"))
    if not isinstance(value, dict) or _json_bytes(value) != raw:
        raise ValueError("task execution evidence is not canonical JSON")
    return value


def _finite(value: Any, *, positive: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("runtime durations must be finite non-bool numbers")
    try:
        finite = math.isfinite(value)
    except OverflowError as error:
        raise ValueError("runtime durations must be finite") from error
    if not finite or positive and value <= 0:
        raise ValueError("runtime duration is not a valid finite value")


def _configuration(adapter: ImplementationRuntimeAdapter) -> dict[str, Any]:
    return {
        "provider": adapter.provider,
        "executable": adapter.executable,
        "workspace": _value(adapter.workspace),
        "output_schema": _value(adapter.output_schema),
        "log_dir": str(adapter.log_dir),
        "timeout": adapter.timeout,
        "grace": adapter.grace,
        "deadline_at": adapter.deadline_at,
    }


def _validate_configuration(
    value: Any, prepared: TaskExecutionPrepared, logs: _Logs
) -> dict[str, Any]:
    value = _object(
        value,
        {
            "provider",
            "executable",
            "workspace",
            "output_schema",
            "log_dir",
            "timeout",
            "grace",
            "deadline_at",
        },
    )
    for name in ("timeout", "grace"):
        _finite(value[name], positive=True)
        if value[name] != getattr(prepared.policy, name):
            raise ValueError("runtime duration differs from the frozen task policy")
    if value["deadline_at"] is not None:
        _finite(value["deadline_at"])
    expected = {
        "provider": prepared.policy.provider,
        "executable": prepared.policy.executable,
        "workspace": _value(prepared.workspace),
        "output_schema": _value(prepared.output_schema),
        "log_dir": str(logs.path),
        "timeout": value["timeout"],
        "grace": value["grace"],
        "deadline_at": value["deadline_at"],
    }
    if task_execution_hash(value) != task_execution_hash(expected):
        raise ValueError("implementation adapter differs from its task preparation")
    return cast(dict[str, Any], value)


def _layout(prepared: TaskExecutionPrepared, database: _Database) -> tuple[Path, _Logs]:
    root = _canonical(Path(prepared.context.path).parent)
    if (
        root.parent != database.path.parent
        or Path(prepared.context.path) != root / "context"
        or prepared.workspace.root != str(root / "workspace")
    ):
        raise ValueError("task execution paths differ from the owned sibling layout")
    _directory(root)
    path = root / "logs"
    return root, _Logs(path, _identity(_directory(path)))


def _binding_value(
    store: PlanningStore,
    run_id: str,
    attempt: int,
    database: _Database,
    root_identity: tuple[int, int],
    logs: _Logs,
    configuration: dict[str, Any],
    owner_id: str,
) -> dict[str, Any]:
    selected, started, preparation, _ = _pair(store, run_id, attempt)
    prepared = preparation.event
    assert isinstance(prepared, TaskExecutionPrepared)
    state = store.load_task(run_id)
    return {
        "run_id": run_id,
        "attempt": attempt,
        "task_hash": state.task_hash,
        "rules_hash": state.rules_hash,
        "plan_hash": store.load_task_provenance(run_id).plan_hash,
        "started_sha256": task_execution_hash(started),
        "prepared_sha256": task_execution_hash(preparation),
        "requested": _value(selected.request),
        "context": _value(prepared.context),
        "workspace": _value(prepared.workspace),
        "initial_digest": prepared.initial_workspace_digest,
        "policy": _value(prepared.policy),
        "dispatch_policy": _value(prepared.dispatch_policy),
        "database": database.value(),
        "root": {
            "path": str(logs.path.parent),
            "identity": root_identity,
            "parent_identity": database.parent_identity,
        },
        "logs": {"path": str(logs.path), "identity": logs.identity},
        "configuration": configuration,
        "owner_id": owner_id,
    }


def _light_guard(binding: TaskExecutionBinding) -> tuple[TaskState, TaskAttempt]:
    """Check live identity and DB facts only; never inspect workspace contents."""
    owned = binding._state
    if (
        binding._issue is not _ISSUED
        or binding.launch is not owned.launch
        or binding.adapter is not owned.adapter
        or binding.run_id != owned.run_id
        or binding.launch.store is not owned.store
        or binding.launch.lease is not owned.lease
    ):
        raise ValueError("task execution binding objects changed")
    _check_lease(owned.lease)
    owned.database.check()
    if _store_database(owned.store) != owned.database:
        raise ValueError("task execution database changed")
    root, logs = _layout(owned.prepared, owned.database)
    _directory(root, identity=owned.root_identity)
    if logs != owned.logs or owned.launch.log_dir != logs.path:
        raise ValueError("task execution log directory changed")
    if task_execution_json(_configuration(owned.adapter)) != owned.configuration:
        raise ValueError("live implementation adapter configuration changed")
    if task_execution_hash(owned.launch.prepared) != task_execution_hash(
        owned.prepared
    ):
        raise ValueError("task launch preparation changed")
    state = owned.store.load_task(owned.run_id)
    selected = state.current_attempt
    if selected is None or selected.number != owned.launch.attempt:
        raise ValueError("task execution is no longer the current attempt")
    expected = _binding_value(
        owned.store,
        owned.run_id,
        selected.number,
        owned.database,
        owned.root_identity,
        logs,
        _configuration(owned.adapter),
        _json((owned.binding + "\n").encode())["owner_id"],
    )
    if task_execution_json(expected) != owned.binding:
        raise ValueError("task execution exact reservation changed")
    return state, selected


def bind_task_execution(
    launch: TaskLaunch, run_id: str, adapter: ImplementationRuntimeAdapter
) -> TaskExecutionBinding:
    """Claim this fresh launch for one pristine implementation adapter instance."""
    if not isinstance(launch, TaskLaunch) or not isinstance(
        adapter, ImplementationRuntimeAdapter
    ):
        raise TypeError(
            "task execution requires a live implementation launch and adapter"
        )
    if run_id != launch._run_id:
        raise ValueError("task execution run differs from the issued launch")
    launch.check()
    prepared = inspect_task_context(launch.store, run_id, launch.attempt, launch.lease)
    if task_execution_hash(prepared) != task_execution_hash(launch.prepared):
        raise ValueError("task execution identifies a different preparation")
    _policy(prepared.policy)
    database = _store_database(launch.store)
    root, logs = _layout(prepared, database)
    if launch.log_dir != logs.path:
        raise ValueError("task execution must use the T43 log directory")
    configuration = _validate_configuration(_configuration(adapter), prepared, logs)
    receipt = _receipt(_value(adapter.ownership_receipt()))
    if any(
        (
            receipt.operations,
            receipt.acquisitions,
            receipt.processes,
            receipt.isolations,
            receipt.logs,
            receipt.directories,
        )
    ):
        raise ValueError("task binding requires a pristine runtime owner")
    root_identity = _identity(_directory(root))
    value = _binding_value(
        launch.store,
        run_id,
        launch.attempt,
        database,
        root_identity,
        logs,
        configuration,
        receipt.owner_id,
    )
    owned = _BindingState(
        launch,
        launch.store,
        launch.lease,
        adapter,
        run_id,
        prepared,
        database,
        root_identity,
        logs,
        task_execution_json(configuration),
        task_execution_json(value),
    )
    result = TaskExecutionBinding(launch, run_id, adapter, owned, _ISSUED)
    _light_guard(result)
    launch.claim()
    return result


@contextmanager
def _descriptor(fd: int) -> Iterator[int]:
    failure: BaseException | None = None
    try:
        yield fd
    except BaseException as error:
        failure = error
        raise
    finally:
        try:
            os.close(fd)
        except OSError as error:
            if failure is None:
                raise
            failure.add_note(f"Closing task evidence descriptor also failed: {error!r}")


def _open_logs(logs: _Logs) -> int:
    _directory(logs.path, identity=logs.identity)
    fd = os.open(logs.path, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW)
    try:
        info = os.fstat(fd)
        if _identity(info) != logs.identity or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("task log directory changed while opening")
    except BaseException as error:
        try:
            os.close(fd)
        except OSError as close_error:
            error.add_note(f"Closing task log directory also failed: {close_error!r}")
        raise
    return fd


def _read_owned_file(
    directory: int,
    logs: _Logs,
    name: str,
    *,
    byte_count: int | None = None,
    keep: bool = False,
    mode: int | None = None,
) -> tuple[dict[str, Any], bytes]:
    if not name or Path(name).name != name or name in (".", ".."):
        raise ValueError("task evidence must be a direct owned file")
    with _descriptor(os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=directory)) as fd:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("task evidence file must be regular")
        if mode is not None and stat.S_IMODE(before.st_mode) != mode:
            raise ValueError("task evidence file mode changed")
        expected = before.st_size if byte_count is None else byte_count
        if type(expected) is not int or expected < 0 or before.st_size != expected:
            raise ValueError("task evidence size differs from its recorded byte count")
        if keep and expected > MAX_MANIFEST_BYTES:
            raise ValueError("task execution control file exceeds 16 MiB")
        remaining, digest, chunks = expected, hashlib.sha256(), []
        while remaining:
            block = os.read(fd, min(remaining, 1024 * 1024))
            if not block:
                raise ValueError("task evidence was truncated while being read")
            remaining -= len(block)
            digest.update(block)
            if keep:
                chunks.append(block)
        # Never chase a growing file to EOF. Size and stable metadata close the read.
        if _stable(before) != _stable(os.fstat(fd)) or _stable(before) != _stable(
            os.stat(name, dir_fd=directory, follow_symlinks=False)
        ):
            raise ValueError("task evidence changed during its bounded read")
        _directory(logs.path, identity=logs.identity)
        return {
            "path": str(logs.path / name),
            "identity": list(_identity(before)),
            "mode": stat.S_IMODE(before.st_mode),
            "byte_count": expected,
            "sha256": digest.hexdigest(),
        }, b"".join(chunks)


def _write_owned_json(
    directory: int, logs: _Logs, name: str, payload: dict[str, Any]
) -> dict[str, Any]:
    if name not in (_MARKER, _MANIFEST):
        raise ValueError("unrecognized task execution control file")
    fd = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
        0o600,
        dir_fd=directory,
    )
    with _descriptor(fd):
        identity = _identity(os.fstat(fd))
        raw = _json_bytes({**payload, "identity": list(identity)})
        remaining = memoryview(raw)
        while remaining:
            count = os.write(fd, remaining)
            if count <= 0:
                raise OSError("task evidence write made no progress")
            remaining = remaining[count:]
        os.fchmod(fd, 0o400)
        os.fsync(fd)
    os.fsync(directory)
    metadata, actual = _read_owned_file(directory, logs, name, keep=True, mode=0o400)
    if tuple(metadata["identity"]) != identity or actual != raw:
        raise ValueError("task execution control file changed while being written")
    return metadata


def _raw_logs(
    directory: int, logs: _Logs, receipt: RuntimeOwnershipReceipt
) -> list[dict[str, Any]]:
    paths: list[Path] = []
    for record in receipt.logs:
        path = Path(record.path)
        if (
            not path.is_absolute()
            or path.parent != logs.path
            or path.name in (_MARKER, _MANIFEST)
            or path in paths
        ):
            raise ValueError("runtime log is outside its exclusive task directory")
        paths.append(path)
    result = []
    for record, path in zip(receipt.logs, paths, strict=True):
        sealed = (
            record.sealed
            and record.closed
            and record.healthy
            and record.identity is not None
            and record.byte_count is not None
            and record.sha256 is not None
            and _SHA256.fullmatch(record.sha256) is not None
        )
        metadata = None
        if sealed:
            metadata, _ = _read_owned_file(
                directory, logs, path.name, byte_count=record.byte_count
            )
            if (
                tuple(metadata["identity"]) != record.identity
                or metadata["sha256"] != record.sha256
            ):
                raise ValueError("runtime log differs from its actual sealed receipt")
        result.append(
            {
                "resource_id": record.resource_id,
                "path": str(path),
                "verified": bool(sealed),
                "metadata": metadata,
            }
        )
    return result


def _environment(value: Any) -> EnvironmentInfo | None:
    if value is None:
        return None
    value = _object(value, {"cli_version", "logged_in", "models", "model_efforts"})
    if value["cli_version"] is not None and not isinstance(value["cli_version"], str):
        raise ValueError("invalid reported CLI version")
    if value["logged_in"] is not None and type(value["logged_in"]) is not bool:
        raise ValueError("invalid reported authentication state")
    models = value["models"]
    if models is not None and (
        not isinstance(models, list)
        or any(not isinstance(item, str) for item in models)
    ):
        raise ValueError("invalid reported model catalog")
    efforts = value["model_efforts"]
    if efforts is not None and (
        not isinstance(efforts, dict)
        or any(
            not isinstance(key, str)
            or not isinstance(items, list)
            or any(not isinstance(item, str) for item in items)
            for key, items in efforts.items()
        )
    ):
        raise ValueError("invalid reported model effort catalog")
    return EnvironmentInfo(
        value["cli_version"],
        value["logged_in"],
        tuple(models) if models is not None else None,
        {key: tuple(items) for key, items in efforts.items()}
        if efforts is not None
        else None,
    )


def _history_prefix(history: TaskHistory) -> dict[str, Any]:
    return {"length": len(history), "sha256": task_execution_hash(history)}


def _check_prefix(history: TaskHistory, prefix: Any) -> None:
    prefix = _object(prefix, {"length", "sha256"})
    length, digest = prefix["length"], prefix["sha256"]
    if (
        type(length) is not int
        or length < 0
        or length > len(history)
        or not isinstance(digest, str)
        or _SHA256.fullmatch(digest) is None
        or task_execution_hash(history[:length]) != digest
    ):
        raise ValueError("the original task execution history prefix changed")


def _runtime_events(history: TaskHistory, attempt: int) -> tuple[list[Any], list[Any]]:
    terminals, exits = {}, {}
    for envelope, _ in history:
        if envelope.attempt != attempt:
            continue
        event = envelope.event
        if isinstance(event, (Completed, Error)):
            terminals[task_execution_hash(event)] = event
        elif isinstance(event, ProcessExited):
            exits[task_execution_hash(event)] = event
    return list(terminals.values()), list(exits.values())


def _applied(history: TaskHistory, attempt: int) -> list[Any]:
    return [
        envelope.event
        for envelope, disposition in history
        if envelope.attempt == attempt and disposition == Disposition.APPLIED
    ]


def _report(
    actual: TurnResult, store: PlanningStore, run_id: str
) -> tuple[TaskImplementationReport | None, str | None]:
    if not isinstance(actual.terminal, Completed):
        return None, None
    card = store.load_task_input(run_id).card
    result = actual.terminal.result
    try:
        text = result if isinstance(result, str) else task_execution_json(result)
        return parse_task_output(text, card), None
    except (ValueError, TypeError, RecursionError) as error:
        return None, str(error)


def _qualified(
    state: TaskState,
    selected: TaskAttempt,
    history: TaskHistory,
    receipt: RuntimeOwnershipReceipt,
    actual: TurnResult,
    environment: EnvironmentInfo | None,
    observations: Any,
    report: TaskImplementationReport | None,
    final_digest: str | None,
    model_handle_present: bool,
    *,
    archive: bool = False,
) -> bool:
    events = _applied(history, selected.number)
    if any(
        isinstance(item, (CancelRequested, TaskEvidenceRejected)) for item in events
    ):
        return False
    handbacks = [item for item in events if isinstance(item, TaskScopeBlocked)]
    phase_allowed = state.phase == TaskPhase.AWAITING_DELIVERY and not handbacks
    if archive and state.phase == TaskPhase.PLANNING_REQUIRED:
        phase_allowed = (
            report is not None
            and report.outcome == "scope_blocked"
            and len(handbacks) == 1
            and handbacks[0].reason == report.summary
            and handbacks[0].required_paths == report.required_paths
            and state.scope_block == handbacks[0]
        )
    terminals, exits = _runtime_events(history, selected.number)
    observed = [
        item.environment for item in events if isinstance(item, TaskEnvironmentObserved)
    ]
    return bool(
        phase_allowed
        and state.current_attempt == selected
        and selected.runtime.status == AttemptStatus.SUCCEEDED
        and selected.runtime.exit_code is not None
        and task_execution_hash(selected.runtime) == task_execution_hash(observations)
        and model_handle_present
        and isinstance(actual.terminal, Completed)
        and isinstance(actual.process_exit, ProcessExited)
        and len(terminals) == len(exits) == 1
        and task_execution_hash(terminals[0]) == task_execution_hash(actual.terminal)
        and task_execution_hash(exits[0]) == task_execution_hash(actual.process_exit)
        and environment is not None
        and len(observed) == 1
        and task_execution_hash(observed[0]) == task_execution_hash(environment)
        and sorted(item.kind for item in receipt.operations) == ["environment", "turn"]
        and receipt.settled
        and bool(receipt.processes)
        and receipt.processes_stopped
        and receipt.resources_released
        and receipt.outputs_complete
        and receipt.logs_sealed
        and all(
            log.sealed
            and log.closed
            and log.healthy
            and log.identity is not None
            and log.byte_count is not None
            and log.sha256 is not None
            and _SHA256.fullmatch(log.sha256) is not None
            for log in receipt.logs
        )
        and final_digest is not None
        and report is not None
    )


def _recheck(
    binding: TaskExecutionBinding,
    prefix: dict[str, Any],
    receipt: RuntimeOwnershipReceipt,
) -> tuple[TaskState, TaskAttempt, TaskHistory]:
    state, selected = _light_guard(binding)
    history = binding.launch.store.load_task_history(binding.run_id)
    _check_prefix(history, prefix)
    # A later sample is only a consistency check, never replacement evidence.
    if task_execution_hash(binding.adapter.ownership_receipt()) != task_execution_hash(
        receipt
    ):
        raise ValueError("actual task ownership changed after its first sample")
    return state, selected, history


def _observed_workspace(binding: TaskExecutionBinding) -> str:
    binding.launch.check()
    prepared = inspect_task_context(
        binding.launch.store,
        binding.run_id,
        binding.launch.attempt,
        binding.launch.lease,
    )
    if task_execution_hash(prepared) != task_execution_hash(binding._state.prepared):
        raise ValueError("task execution preparation changed after stopping")
    return inspect_workspace(binding.launch.lease, prepared.workspace).digest


def _manifest_result(
    metadata: dict[str, Any],
    qualified: bool,
    receipt: RuntimeOwnershipReceipt,
    final_digest: str | None,
    report: TaskImplementationReport | None,
    validation_error: str | None,
    actual: TurnResult,
    environment: EnvironmentInfo | None,
) -> TaskExecutionManifest:
    return TaskExecutionManifest(
        metadata["path"],
        tuple(metadata["identity"]),
        metadata["byte_count"],
        metadata["sha256"],
        qualified,
        receipt,
        qualified and report is not None and report.outcome == "completed",
        final_digest,
        report,
        validation_error,
        actual,
        environment,
    )


async def seal_task_execution(
    binding: TaskExecutionBinding,
    *,
    turn: TurnHandle | None,
    environment: EnvironmentInfo | None,
) -> TaskExecutionManifest:
    """Seal the first actual sample, without invoking execution or cleanup APIs."""
    if not isinstance(binding, TaskExecutionBinding):
        raise TypeError("task sealing requires a fresh live binding")
    owned = binding._state
    if owned.consumed:
        raise ValueError("task execution seal was already attempted")
    owned.consumed = True
    logs = owned.logs
    try:
        _light_guard(binding)
        if turn is not None and not isinstance(turn, TurnHandle):
            raise TypeError("task turn must be an actual TurnHandle")
        actual = (
            await binding.adapter.result(turn) if turn is not None else TurnResult()
        )
        if not isinstance(actual, TurnResult):
            raise TypeError("implementation result must be a TurnResult")
        receipt = _receipt(_value(binding.adapter.ownership_receipt()))
        state, selected = _light_guard(binding)
        bound_value = _json((owned.binding + "\n").encode())
        if receipt.owner_id != bound_value["owner_id"]:
            raise ValueError("task runtime owner changed")
        if environment is not None and not isinstance(environment, EnvironmentInfo):
            raise TypeError("task environment must be an actual EnvironmentInfo")
        environment = _environment(_value(environment))
        history = owned.store.load_task_history(owned.run_id)
        prefix = _history_prefix(history)
        observations = _value(selected.runtime)
        sample = {
            "version": 1,
            "binding": bound_value,
            "history_prefix": prefix,
            "runtime_observations": observations,
            "environment": _value(environment),
            "receipt": _value(receipt),
            "facts": _facts(receipt),
            "result": _value(actual),
            "model_handle_present": turn is not None,
        }
        with _descriptor(_open_logs(logs)) as directory:
            marker = _write_owned_json(directory, logs, _MARKER, sample)
            final_digest = (
                _observed_workspace(binding) if receipt.processes_stopped else None
            )
            report, validation_error = _report(actual, owned.store, owned.run_id)
            raw_logs = _raw_logs(directory, logs, receipt)
            state, selected, history = _recheck(binding, prefix, receipt)
            if (
                receipt.processes_stopped
                and _observed_workspace(binding) != final_digest
            ):
                raise ValueError("task final workspace changed while sealing")
            qualified = _qualified(
                state,
                selected,
                history,
                receipt,
                actual,
                environment,
                observations,
                report,
                final_digest,
                turn is not None,
            )
            payload = {
                **sample,
                "raw_logs": raw_logs,
                "marker": marker,
                "final_digest": final_digest,
                "report": _value(report),
                "validation_error": validation_error,
                "qualified": qualified,
                "delivery_eligible": qualified
                and report is not None
                and report.outcome == "completed",
            }
            metadata = _write_owned_json(directory, logs, _MANIFEST, payload)
            if _raw_logs(directory, logs, receipt) != raw_logs:
                raise ValueError("task raw log metadata changed while sealing")
            if (
                receipt.processes_stopped
                and _observed_workspace(binding) != final_digest
            ):
                raise ValueError("task final workspace changed after index creation")
            state, selected, history = _recheck(binding, prefix, receipt)
            if qualified != _qualified(
                state,
                selected,
                history,
                receipt,
                actual,
                environment,
                observations,
                report,
                final_digest,
                turn is not None,
            ):
                raise ValueError("task execution qualification changed while sealing")
            return _manifest_result(
                metadata,
                qualified,
                receipt,
                final_digest,
                report,
                validation_error,
                actual,
                environment,
            )
    except BaseException as error:
        error.add_note(
            f"Task execution seal attempted {logs.path / _MARKER}; index {logs.path / _MANIFEST}. "
            "Existing and partial evidence is retained; this binding cannot reseal."
        )
        raise


def _archive_actual(payload: Any, history: TaskHistory, attempt: int) -> TurnResult:
    payload = _object(payload, {"terminal", "process_exit"})
    terminals, exits = _runtime_events(history, attempt)
    if (
        len(terminals) != 1
        or len(exits) != 1
        or not isinstance(terminals[0], Completed)
    ):
        raise ValueError(
            "task archive lacks unambiguous actual success and exit events"
        )
    result = TurnResult(terminals[0], exits[0])
    if task_execution_hash(result) != task_execution_hash(payload):
        raise ValueError("task archive does not match actual terminal and exit history")
    return result


def inspect_task_execution(
    store: PlanningStore, run_id: str, attempt: int, lease: ExecutorLease
) -> TaskExecutionManifest:
    """Verify an anchored positive archive; never recover a binding or live owner."""
    _check_lease(lease)
    database = _store_database(store)
    selected, _, preparation, _ = _pair(store, run_id, attempt)
    prepared = preparation.event
    assert isinstance(prepared, TaskExecutionPrepared)
    state = store.load_task(run_id)
    reconciliation = selected.reconciliation
    expected_path = Path(prepared.context.path).parent / "logs" / _MANIFEST
    if (
        state.current_attempt != selected
        or reconciliation is None
        or not reconciliation.process_stopped
        or not reconciliation.worktree_digest
        or reconciliation.log_path != str(expected_path)
        or not reconciliation.log_sha256
    ):
        raise ValueError("task execution has no qualifying reconciliation anchor")
    history = store.load_task_history(run_id)
    if any(
        isinstance(item, (CancelRequested, TaskEvidenceRejected))
        for item in _applied(history, attempt)
    ):
        raise ValueError("cancelled or rejected task execution cannot qualify")
    root, logs = _layout(prepared, database)
    _directory(
        Path(prepared.context.path), identity=prepared.context.identity, mode=0o500
    )
    with _descriptor(_open_logs(logs)) as directory:
        metadata, raw = _read_owned_file(
            directory, logs, _MANIFEST, keep=True, mode=0o400
        )
        if metadata["sha256"] != reconciliation.log_sha256:
            raise ValueError("task execution archive hash changed")
        payload = _json(raw)
        if payload.get("qualified") is not True:
            raise ValueError("negative task execution archives do not qualify")
        _check_prefix(history, payload.get("history_prefix"))
        receipt = _receipt(payload.get("receipt"))
        actual = _archive_actual(payload.get("result"), history, attempt)
        environment = _environment(payload.get("environment"))
        report, validation_error = _report(actual, store, run_id)
        final_digest = payload.get("final_digest")
        if (
            not isinstance(final_digest, str)
            or not final_digest
            or final_digest != reconciliation.worktree_digest
            or not _qualified(
                state,
                selected,
                history,
                receipt,
                actual,
                environment,
                payload.get("runtime_observations"),
                report,
                final_digest,
                payload.get("model_handle_present") is True,
                archive=True,
            )
        ):
            raise ValueError("task archive facts do not qualify")
        bound = _object(
            payload.get("binding"),
            {
                "run_id",
                "attempt",
                "task_hash",
                "rules_hash",
                "plan_hash",
                "started_sha256",
                "prepared_sha256",
                "requested",
                "context",
                "workspace",
                "initial_digest",
                "policy",
                "dispatch_policy",
                "database",
                "root",
                "logs",
                "configuration",
                "owner_id",
            },
        )
        configuration = _validate_configuration(
            bound.get("configuration"), prepared, logs
        )
        binding = _binding_value(
            store,
            run_id,
            attempt,
            database,
            _identity(_directory(root)),
            logs,
            configuration,
            receipt.owner_id,
        )
        if task_execution_hash(bound) != task_execution_hash(binding):
            raise ValueError("task archive preparation or directory identity changed")
        marker_metadata, marker_raw = _read_owned_file(
            directory, logs, _MARKER, keep=True, mode=0o400
        )
        sample = {
            "version": 1,
            "binding": binding,
            "history_prefix": payload["history_prefix"],
            "runtime_observations": _value(selected.runtime),
            "environment": _value(environment),
            "receipt": _value(receipt),
            "facts": _facts(receipt),
            "result": _value(actual),
            "model_handle_present": True,
        }
        _json(marker_raw)
        if marker_raw != _json_bytes(
            {**sample, "identity": marker_metadata["identity"]}
        ):
            raise ValueError("task archive first-sample marker changed")
        inspected = inspect_task_context(store, run_id, attempt, lease)
        if task_execution_hash(inspected) != task_execution_hash(prepared):
            raise ValueError("task archive context changed")
        if inspect_workspace(lease, prepared.workspace).digest != final_digest:
            raise ValueError("task archived workspace changed from its final digest")
        raw_logs = _raw_logs(directory, logs, receipt)
        expected = {
            **sample,
            "raw_logs": raw_logs,
            "marker": marker_metadata,
            "final_digest": final_digest,
            "report": _value(report),
            "validation_error": validation_error,
            "qualified": True,
            "delivery_eligible": report is not None and report.outcome == "completed",
            "identity": metadata["identity"],
        }
        if raw != _json_bytes(expected):
            raise ValueError("task execution archive schema or derived facts changed")
        # Check DB facts again after all reads; permitted suffixes are re-evaluated.
        checked, _, _, _ = _pair(store, run_id, attempt)
        checked_state, checked_history = (
            store.load_task(run_id),
            store.load_task_history(run_id),
        )
        _check_prefix(checked_history, payload["history_prefix"])
        if checked.reconciliation != reconciliation or not _qualified(
            checked_state,
            checked,
            checked_history,
            receipt,
            actual,
            environment,
            payload["runtime_observations"],
            report,
            final_digest,
            True,
            archive=True,
        ):
            raise ValueError("task archive qualification changed during inspection")
        _check_lease(lease)
        database.check()
        _directory(logs.path, identity=logs.identity)
        return _manifest_result(
            metadata,
            True,
            receipt,
            final_digest,
            report,
            validation_error,
            actual,
            environment,
        )
