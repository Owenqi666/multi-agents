"""Seal planning execution facts; never launch, stop or recover a runtime."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from multi_agent.adapters.base import EnvironmentInfo, TurnHandle, TurnResult
from multi_agent.adapters.ownership import (
    AcquisitionOwnership,
    DirectoryOwnership,
    IsolationOwnership,
    LogOwnership,
    OperationOwnership,
    ProcessOwnership,
    RuntimeOwnershipReceipt,
)
from multi_agent.adapters.runtime import RuntimeAdapter
from multi_agent.core.events import Completed, Error, ProcessExited, thaw_json
from multi_agent.core.planning import (
    PlanningAttempt,
    PlanningPhase,
    PlanningState,
    planning_hash,
    planning_json,
    planning_value,
)
from multi_agent.core.planning_events import (
    PlanningEnvironmentObserved,
    PlanningEvidenceRejected,
    PlanningExecutionPrepared,
    PlanningSplitValidated,
)
from multi_agent.core.state import AttemptStatus
from multi_agent.core.tasks import TaskCard
from multi_agent.store.planning_store import PlanningStore

from .planning_context import _canonical, _directory, inspect_planning_context
from .planning_inputs import parse_project_split

if TYPE_CHECKING:
    from .workspaces import ExecutorLease

MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_NOFOLLOW = os.O_NOFOLLOW | os.O_NONBLOCK
_MANIFEST = "execution.json"
_MARKER = ".seal-attempt.json"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class PlanningLogDirectory:
    path: str
    identity: tuple[int, int]
    run_id: str
    stage: str
    attempt: int
    context_token: str


@dataclass(frozen=True, slots=True)
class PlanningExecutionManifest:
    path: str
    identity: tuple[int, int]
    byte_count: int
    sha256: str
    qualified: bool
    receipt: RuntimeOwnershipReceipt


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


def _selection(
    store: PlanningStore,
    run_id: str,
    stage: str,
    attempt: int,
    *,
    current: bool = False,
) -> tuple[PlanningState, PlanningAttempt, PlanningExecutionPrepared]:
    prepared = inspect_planning_context(store, run_id, stage, attempt)
    state = store.load_planning(run_id)
    selected = next(item for item in state.attempts(stage) if item.number == attempt)
    if current and (state.stage != stage or state.current(stage) != selected):
        raise ValueError("planning execution must identify the current attempt")
    return state, selected, prepared


def _log_path(prepared: PlanningExecutionPrepared, stage: str, attempt: int) -> Path:
    return (
        Path(prepared.run_owner.root)
        / "logs"
        / (f"{stage}-{attempt}-{prepared.attempt_context.token}")
    )


def _check_logs(
    logs: PlanningLogDirectory,
    run_id: str,
    stage: str,
    attempt: int,
    prepared: PlanningExecutionPrepared,
) -> Path:
    path = _log_path(prepared, stage, attempt)
    if logs.path != str(path) or (
        logs.run_id,
        logs.stage,
        logs.attempt,
        logs.context_token,
    ) != (run_id, stage, attempt, prepared.attempt_context.token):
        raise ValueError("planning log directory binding differs")
    _directory(path, identity=logs.identity, mode=0o700)
    return path


def prepare_planning_logs(
    store: PlanningStore, run_id: str, stage: str, attempt: int
) -> PlanningLogDirectory:
    """Create a new private log container, without authorizing execution."""
    _, _, prepared = _selection(store, run_id, stage, attempt, current=True)
    path = _log_path(prepared, stage, attempt)
    _canonical(path, existing=False)
    path.mkdir(mode=0o700)
    info = _directory(path, mode=0o700)
    return PlanningLogDirectory(
        str(path),
        _identity(info),
        run_id,
        stage,
        attempt,
        prepared.attempt_context.token,
    )


def _open_logs(logs: PlanningLogDirectory) -> int:
    path = Path(logs.path)
    _directory(path, identity=logs.identity, mode=0o700)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW)
    try:
        info = os.fstat(fd)
        if _identity(info) != logs.identity or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("planning log directory changed while opening")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_owned_file(
    directory: int,
    logs: PlanningLogDirectory,
    name: str,
    *,
    keep: bool = False,
    mode: int | None = None,
) -> tuple[dict[str, Any], bytes]:
    if not name or Path(name).name != name or name in (".", ".."):
        raise ValueError("planning evidence must be a direct owned file")
    fd = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=directory)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("planning evidence file must be regular")
        if mode is not None and stat.S_IMODE(before.st_mode) != mode:
            raise ValueError("planning evidence file mode changed")
        digest = hashlib.sha256()
        count = 0
        chunks = []
        while block := os.read(fd, 1024 * 1024):
            count += len(block)
            if keep and count > MAX_MANIFEST_BYTES:
                raise ValueError("planning execution manifest exceeds 16 MiB limit")
            digest.update(block)
            if keep:
                chunks.append(block)
        if (
            count != before.st_size
            or _stable(before) != _stable(os.fstat(fd))
            or _stable(before)
            != _stable(os.stat(name, dir_fd=directory, follow_symlinks=False))
        ):
            raise ValueError("planning evidence changed while being read")
        _directory(Path(logs.path), identity=logs.identity, mode=0o700)
        return {
            "path": str(Path(logs.path) / name),
            "identity": list(_identity(before)),
            "mode": stat.S_IMODE(before.st_mode),
            "byte_count": count,
            "sha256": digest.hexdigest(),
        }, b"".join(chunks)
    finally:
        os.close(fd)


def _write_owned_json(
    directory: int, logs: PlanningLogDirectory, name: str, payload: dict[str, Any]
) -> dict[str, Any]:
    fd = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
        0o600,
        dir_fd=directory,
    )
    try:
        identity = _identity(os.fstat(fd))
        value = {**payload, "identity": list(identity)}
        raw = (planning_json(value) + "\n").encode("utf-8")
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ValueError("planning execution manifest exceeds 16 MiB limit")
        with os.fdopen(fd, "wb", closefd=False) as stream:
            if stream.write(raw) != len(raw):
                raise OSError("planning evidence write was incomplete")
            stream.flush()
            os.fsync(fd)
            os.fchmod(fd, 0o400)
    finally:
        os.close(fd)
    metadata, observed = _read_owned_file(directory, logs, name, keep=True, mode=0o400)
    if tuple(metadata["identity"]) != identity or observed != raw:
        raise ValueError("planning evidence changed while being sealed")
    return metadata


def _binding(
    run_id: str,
    stage: str,
    attempt: PlanningAttempt,
    state: PlanningState,
    prepared: PlanningExecutionPrepared,
    logs: PlanningLogDirectory,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "input_hash": state.planning_input.input_hash,
        "stage": stage,
        "attempt": attempt.number,
        "requested": planning_value(attempt.request.request),
        "started_hash": planning_hash(attempt.request),
        "prepared_hash": planning_hash(prepared),
        "source_snapshot_id": prepared.source_snapshot_id,
        "context": planning_value(prepared.attempt_context),
        "owner": planning_value(prepared.run_owner),
        "logs": planning_value(logs),
    }


def _facts(receipt: RuntimeOwnershipReceipt) -> dict[str, Any]:
    return {
        "settled": receipt.settled,
        "active_operations": receipt.active_operations,
        "pending_acquisitions": receipt.pending_acquisitions,
        "unknown_acquisitions": receipt.unknown_acquisitions,
        "processes_stopped": receipt.processes_stopped,
        "resources_released": receipt.resources_released,
        "logs_sealed": receipt.logs_sealed,
        "outputs_complete": receipt.outputs_complete,
    }


def _stored_result(attempt: PlanningAttempt) -> dict[str, Any]:
    terminals = [
        item for item in attempt.events if isinstance(item, (Completed, Error))
    ]
    exits = [item for item in attempt.events if isinstance(item, ProcessExited)]
    if len(terminals) > 1 or len(exits) > 1:
        raise ValueError("stored planning runtime events are ambiguous")
    return {
        "terminal": planning_value(terminals[0]) if terminals else None,
        "process_exit": planning_value(exits[0]) if exits else None,
    }


def _environment(attempt: PlanningAttempt) -> Any:
    observed = [
        item.environment
        for item in attempt.events
        if isinstance(item, PlanningEnvironmentObserved)
    ]
    if len(observed) > 1:
        raise ValueError("stored planning environment is ambiguous")
    return planning_value(observed[0]) if observed else None


def _raw_logs(
    directory: int, logs: PlanningLogDirectory, receipt: RuntimeOwnershipReceipt
) -> list[dict[str, Any]]:
    paths = []
    for log in receipt.logs:
        path = Path(log.path)
        # Reject an outside path before resolving or opening it. All runtime
        # recorders create direct children of this unique owned directory.
        if (
            not path.is_absolute()
            or path.parent != Path(logs.path)
            or path.name in (_MARKER, _MANIFEST)
            or path in paths
        ):
            raise ValueError("runtime log is outside its exclusive planning directory")
        paths.append(path)
    result = []
    for log, path in zip(receipt.logs, paths, strict=True):
        metadata, _ = _read_owned_file(directory, logs, path.name)
        if log.identity is not None and tuple(metadata["identity"]) != log.identity:
            raise ValueError("runtime log identity changed")
        if log.sealed and (
            metadata["byte_count"] != log.byte_count or metadata["sha256"] != log.sha256
        ):
            raise ValueError("runtime log bytes differ from the live receipt")
        result.append(metadata)
    return result


def _qualified(
    receipt: RuntimeOwnershipReceipt,
    attempt: PlanningAttempt,
    result: dict[str, Any],
    environment: Any,
    model_handle_present: bool,
) -> bool:
    return (
        model_handle_present
        and sorted(item.kind for item in receipt.operations) == ["environment", "turn"]
        and bool(receipt.processes)
        and receipt.processes_stopped
        and receipt.resources_released
        and receipt.logs_sealed
        and all(
            log.closed
            and log.healthy
            and log.identity is not None
            and log.byte_count is not None
            and log.sha256 is not None
            and _SHA256.fullmatch(log.sha256) is not None
            for log in receipt.logs
        )
        and receipt.outputs_complete
        and attempt.runtime.status == AttemptStatus.SUCCEEDED
        and attempt.runtime.exit_code is not None
        and result["terminal"] is not None
        and result["terminal"].get("kind") == "completed"
        and planning_hash(result) == planning_hash(_stored_result(attempt))
        and environment is not None
        and planning_hash(environment) == planning_hash(_environment(attempt))
    )


async def seal_planning_execution(
    store: PlanningStore,
    run_id: str,
    stage: str,
    attempt: int,
    *,
    logs: PlanningLogDirectory,
    adapter: RuntimeAdapter,
    turn: TurnHandle | None,
    environment: EnvironmentInfo | None,
) -> PlanningExecutionManifest:
    """Record this live adapter's facts once; callers persist reconciliation."""
    state, selected, prepared = _selection(store, run_id, stage, attempt, current=True)
    if any(isinstance(event, PlanningEvidenceRejected) for event in selected.events):
        raise ValueError("rejected planning execution cannot be sealed again")
    path = _check_logs(logs, run_id, stage, attempt, prepared)
    provider = selected.request.request.provider
    executable = prepared.policy.for_provider(provider).executable
    if (
        adapter.provider != provider
        or adapter.log_dir != path
        or str(Path(adapter.executable).resolve(strict=True)) != executable
    ):
        raise ValueError("live adapter does not match the prepared execution")
    actual = await adapter.result(turn) if turn is not None else TurnResult()
    receipt = adapter.ownership_receipt()
    binding = _binding(run_id, stage, selected, state, prepared, logs)
    facts = _facts(receipt)
    result = {
        "terminal": planning_value(actual.terminal),
        "process_exit": planning_value(actual.process_exit),
    }
    environment_value = planning_value(environment)
    qualified = _qualified(
        receipt, selected, result, environment_value, turn is not None
    )
    directory = _open_logs(logs)
    try:
        marker = _write_owned_json(
            directory,
            logs,
            _MARKER,
            {
                "version": 1,
                "binding": binding,
                "receipt": planning_value(receipt),
                "facts": facts,
            },
        )
        raw_logs = _raw_logs(directory, logs, receipt)
        # Recheck source/context/executable after the actual result await and
        # disk reads. No helper here changes state or adopts old ownership.
        checked_state, checked_attempt, checked_prepared = _selection(
            store, run_id, stage, attempt, current=True
        )
        if any(
            isinstance(event, PlanningEvidenceRejected)
            for event in checked_attempt.events
        ):
            raise ValueError("rejected planning execution cannot be sealed again")
        if (
            _binding(
                run_id, stage, checked_attempt, checked_state, checked_prepared, logs
            )
            != binding
            or _stored_result(checked_attempt) != _stored_result(selected)
            or planning_hash(_environment(checked_attempt))
            != planning_hash(_environment(selected))
        ):
            raise ValueError("planning execution binding changed while sealing")
        if planning_hash(adapter.ownership_receipt()) != planning_hash(receipt):
            raise ValueError("live planning ownership changed while sealing")
        metadata = _write_owned_json(
            directory,
            logs,
            _MANIFEST,
            {
                "version": 1,
                "binding": binding,
                "environment": environment_value,
                "receipt": planning_value(receipt),
                "facts": facts,
                "raw_logs": raw_logs,
                "result": result,
                "model_handle_present": turn is not None,
                "qualified": qualified,
                "marker": marker,
            },
        )
        _check_logs(logs, run_id, stage, attempt, checked_prepared)
        return PlanningExecutionManifest(
            metadata["path"],
            tuple(metadata["identity"]),
            metadata["byte_count"],
            metadata["sha256"],
            qualified,
            receipt,
        )
    finally:
        os.close(directory)


def _object(value: Any, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("invalid planning evidence schema")
    return cast(dict[str, Any], value)


def _integer(value: Any, *, minimum: int | None = 0) -> int:
    if type(value) is not int or minimum is not None and value < minimum:
        raise ValueError("invalid integer in planning evidence")
    return value


def _record[T](record_type: type[T], value: Any) -> T:
    value = dict(_object(value, {item.name for item in fields(cast(Any, record_type))}))
    bools = {
        "active",
        "owns_group",
        "group_gone",
        "watcher_settled",
        "readers_settled",
        "stdout_eof",
        "stderr_eof",
        "removed",
        "closed",
        "healthy",
        "sealed",
    }
    integers = {
        "number",
        "pid",
        "pending_spawns",
        "unknown_spawns",
        "process_count",
        "unstopped_processes",
        "records",
    }
    for name, item in value.items():
        if name in bools:
            if type(item) is not bool:
                raise ValueError("invalid boolean in planning evidence")
        elif name in integers:
            _integer(item, minimum=1 if name in ("number", "pid") else 0)
        elif name in ("exit_code", "byte_count"):
            if item is not None:
                _integer(item, minimum=None if name == "exit_code" else 0)
        elif name == "identity":
            if item is not None:
                if not isinstance(item, list) or len(item) != 2:
                    raise ValueError("invalid file identity in planning evidence")
                value[name] = tuple(_integer(part) for part in item)
        elif not isinstance(item, str) and not (
            item is None and name in ("resource_id", "sha256")
        ):
            raise ValueError("invalid text in planning evidence")
    return record_type(**value)


def _receipt(value: Any) -> RuntimeOwnershipReceipt:
    value = _object(value, {item.name for item in fields(RuntimeOwnershipReceipt)})
    if not isinstance(value["owner_id"], str) or not value["owner_id"]:
        raise ValueError("invalid planning runtime owner")

    def records[T](name: str, record_type: type[T]) -> tuple[T, ...]:
        items = value[name]
        if type(items) is not list:
            raise ValueError("planning ownership records must be arrays")
        return tuple(_record(record_type, item) for item in items)

    receipt = RuntimeOwnershipReceipt(
        value["owner_id"],
        records("operations", OperationOwnership),
        records("acquisitions", AcquisitionOwnership),
        records("processes", ProcessOwnership),
        records("isolations", IsolationOwnership),
        records("logs", LogOwnership),
        records("directories", DirectoryOwnership),
    )
    for log in receipt.logs:
        if log.sealed and (
            not log.closed
            or not log.healthy
            or log.identity is None
            or log.byte_count is None
            or log.sha256 is None
            or _SHA256.fullmatch(log.sha256) is None
        ):
            raise ValueError("sealed planning log has inconsistent ownership facts")
    resources = {
        item.resource_id: kind
        for kind, items in (
            ("process", receipt.processes),
            ("isolation", receipt.isolations),
            ("log", receipt.logs),
            ("directory", receipt.directories),
        )
        for item in items
    }
    if len(resources) != sum(
        map(
            len,
            (receipt.processes, receipt.isolations, receipt.logs, receipt.directories),
        )
    ):
        raise ValueError("duplicate planning ownership resource")
    for items in (receipt.operations, receipt.acquisitions):
        if len({item.number for item in items}) != len(items):
            raise ValueError("duplicate planning ownership sequence")
    published = set()
    for acquisition in receipt.acquisitions:
        if acquisition.outcome not in (
            "pending",
            "not_created",
            "published",
            "unknown",
        ):
            raise ValueError("invalid planning acquisition outcome")
        if acquisition.outcome == "published":
            if resources.get(acquisition.resource_id or "") != acquisition.kind:
                raise ValueError("planning acquisition does not identify its resource")
            published.add(acquisition.resource_id)
        elif acquisition.resource_id is not None:
            raise ValueError("unpublished planning acquisition claims a resource")
    if set(resources) != published:
        raise ValueError("planning resource has no published acquisition")
    return receipt


def _json(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or raw != (planning_json(value) + "\n").encode(
            "utf-8"
        ):
            raise ValueError("planning evidence is not canonical JSON")
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("invalid planning evidence JSON") from error
    return value


def inspect_planning_execution(
    store: PlanningStore, run_id: str, stage: str, attempt: int
) -> PlanningExecutionManifest:
    """Verify qualifying persisted evidence, without recovering live ownership."""
    state, selected, prepared = _selection(store, run_id, stage, attempt)
    reconciliation = selected.reconciliation
    expected_path = _log_path(prepared, stage, attempt) / _MANIFEST
    if (
        reconciliation is None
        or not reconciliation.process_stopped
        or reconciliation.log_path != str(expected_path)
        or reconciliation.log_sha256 is None
    ):
        raise ValueError("planning execution has no qualifying reconciliation")
    path = expected_path.parent
    info = _directory(path, mode=0o700)
    logs = PlanningLogDirectory(
        str(path),
        _identity(info),
        run_id,
        stage,
        attempt,
        prepared.attempt_context.token,
    )
    directory = _open_logs(logs)
    try:
        metadata, raw = _read_owned_file(
            directory, logs, _MANIFEST, keep=True, mode=0o400
        )
        if metadata["sha256"] != reconciliation.log_sha256:
            raise ValueError("planning execution manifest hash changed")
        payload = _json(raw)
        receipt = _receipt(payload.get("receipt"))
        result = _stored_result(selected)
        environment = _environment(selected)
        if not _qualified(receipt, selected, result, environment, True):
            raise ValueError("planning execution receipt does not qualify")
        marker_metadata, marker_raw = _read_owned_file(
            directory, logs, _MARKER, keep=True, mode=0o400
        )
        binding = _binding(run_id, stage, selected, state, prepared, logs)
        marker = {
            "version": 1,
            "identity": marker_metadata["identity"],
            "binding": binding,
            "receipt": planning_value(receipt),
            "facts": _facts(receipt),
        }
        _json(marker_raw)
        if marker_raw != (planning_json(marker) + "\n").encode("utf-8"):
            raise ValueError("planning seal marker binding changed")
        expected = {
            "version": 1,
            "identity": metadata["identity"],
            "binding": binding,
            "environment": environment,
            "receipt": planning_value(receipt),
            "facts": _facts(receipt),
            "raw_logs": _raw_logs(directory, logs, receipt),
            "result": result,
            "model_handle_present": True,
            "qualified": True,
            "marker": marker_metadata,
        }
        if raw != (planning_json(expected) + "\n").encode("utf-8"):
            raise ValueError("planning execution manifest binding or facts changed")
        _check_logs(
            logs,
            run_id,
            stage,
            attempt,
            inspect_planning_context(store, run_id, stage, attempt),
        )
        return PlanningExecutionManifest(
            metadata["path"],
            tuple(metadata["identity"]),
            metadata["byte_count"],
            metadata["sha256"],
            True,
            receipt,
        )
    finally:
        os.close(directory)


def inspect_project_split(
    store: PlanningStore, run_id: str
) -> tuple[PlanningAttempt, tuple[TaskCard, ...], PlanningExecutionManifest]:
    state = store.load_planning(run_id)
    attempt = state.current("draft")
    if (
        not state.split
        or state.phase != PlanningPhase.READY
        or attempt is None
        or not attempt.usable
    ):
        raise ValueError("a verified parent stage split is required")
    manifest = inspect_planning_execution(store, run_id, "draft", attempt.number)
    cards = parse_project_split(
        planning_json(attempt.runtime.result), state.planning_input
    )
    if PlanningSplitValidated(cards) not in attempt.events:
        raise ValueError("validated stage definitions differ from sealed split output")
    return attempt, cards, manifest


def split_entry_hash(attempt: PlanningAttempt, index: int) -> str:
    return planning_hash(
        cast(dict[str, Any], thaw_json(attempt.runtime.result))["cards"][index]
    )


def inspect_projected_draft(
    store: PlanningStore,
    run_id: str,
    state: PlanningState,
    *,
    lease: ExecutorLease | None = None,
) -> None:
    source = state.projection
    card = state.card
    assert source is not None and card is not None
    store.require_latest_projection(run_id, card.id, source)
    attempt, cards, manifest = inspect_project_split(store, source.split_run_id)
    index = source.entry_index
    if (
        source.split_attempt != attempt.number
        or source.split_manifest_sha256 != manifest.sha256
        or not 0 <= index < len(cards)
        or split_entry_hash(attempt, index) != source.entry_hash
        or replace(
            cards[index],
            baseline_commit=state.planning_input.frozen_rules.baseline_commit,
        )
        != card
        or card.task_hash != source.card_hash
        or set(card.depends_on) != source.dependency_ids
    ):
        raise ValueError("the projected card differs from the selected split entry")
    if source.dependencies:
        _inspect_projected_prerequisites(
            store, state, cards[index].baseline_commit, lease
        )


def _inspect_projected_prerequisites(
    store: PlanningStore,
    state: PlanningState,
    stage_baseline: str,
    lease: ExecutorLease | None,
) -> None:
    from .integration_cli import inspect_delegated_prerequisite_integration
    from .project_stage import _contains
    from .task_candidates import inspect_task_candidate
    from .task_context import _store_database

    source = state.projection
    assert source is not None and state.card is not None
    value = state.planning_input
    common = Path(value.frozen_rules.repository)
    baseline = state.card.baseline_commit
    handoff = json.loads(value.requirements.partition("\n")[2])
    mapping = handoff["upstream_integration"]
    if not _contains(common, baseline, stage_baseline):
        raise ValueError("delegated leaf baseline does not contain its stage baseline")
    if mapping is None:
        if not all(
            _contains(common, baseline, item.candidate) for item in source.dependencies
        ):
            raise ValueError(
                "delegated leaf baseline does not contain its prerequisites"
            )
    else:
        if not _contains(common, baseline, mapping["candidate"]):
            raise ValueError(
                "delegated leaf baseline does not contain its frozen integration"
            )
        try:
            inspect_delegated_prerequisite_integration(
                _store_database(store).path,
                common,
                stage_baseline,
                tuple(
                    (item.task_run_id, item.implementation_attempt, item.candidate)
                    for item in source.dependencies
                ),
                mapping["integration_id"],
                mapping["candidate"],
                lease=lease,
            )
        except (OSError, TypeError, ValueError) as error:
            raise ValueError(
                f"delegated prerequisite integration {mapping['integration_id']} "
                f"has no current verified review for the frozen candidate: {error}"
            ) from error
    if lease is not None:
        for item in source.dependencies:
            inspect_task_candidate(
                store, item.task_run_id, item.implementation_attempt, lease
            )


def inspect_bound_planning(
    store: PlanningStore,
    run_id: str,
    state: PlanningState,
    *,
    lease: ExecutorLease | None = None,
) -> None:
    provenance = state.provenance()
    if state.projection is not None:
        inspect_projected_draft(store, run_id, state, lease=lease)
    else:
        draft = state.current("draft")
        assert draft is not None
        inspect_planning_execution(store, run_id, "draft", draft.number)
    inspect_planning_execution(store, run_id, "review", provenance.review.number)


def bind_planning_task(
    store: PlanningStore,
    planning_run_id: str,
    task_run_id: str,
    created_at: str,
    *,
    lease: ExecutorLease | None = None,
) -> None:
    """Bind only the selected pair that was just verified, using a store CAS."""
    state = store.load_planning(planning_run_id)
    if not state.can_bind:
        raise ValueError("planning evidence does not permit a task binding")
    provenance = state.provenance()
    inspect_bound_planning(store, planning_run_id, state, lease=lease)
    store.create_planned_task(
        task_run_id,
        planning_run_id,
        created_at,
        expected_plan_hash=provenance.plan_hash,
    )
