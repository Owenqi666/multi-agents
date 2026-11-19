"""Hold a fresh implementation reservation and its owned filesystem lifetime.

Registry entries are negative gates, never proof of stopped processes. A durable
Prepared record can be inspected but cannot recreate the live launch capability.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from multi_agent.core import Disposition
from multi_agent.core.dispatch import ImplementationChoice, resolve_dispatch
from multi_agent.core.planning import (
    PlanningAttempt,
    PlanningPhase,
    PlanningProvenance,
    VerifiedPlanningProvenance,
    planning_json,
)
from multi_agent.core.review_input import ExecutedReviewInput
from multi_agent.core.task_events import (
    TaskAttemptStarted,
    TaskEnvelope,
    TaskExecutionPrepared,
    TaskReworkStarted,
)
from multi_agent.core.task_execution import (
    TaskDispatchPolicy,
    TaskExecutionContext,
    TaskExecutionPolicy,
    prepared_for_task,
    task_execution_hash,
    task_execution_json,
    task_execution_value,
)
from multi_agent.core.task_rework import TaskReworkSource
from multi_agent.core.task_state import TaskAttempt, TaskInput, TaskPhase, TaskState
from multi_agent.core.transitions import apply_task
from multi_agent.store.planning_store import PlanningStore

from .planning_evidence import inspect_planning_execution, inspect_projected_draft
from .planning_inputs import parse_planning_draft, validate_planning_input
from .task_inputs import (
    freeze_project_rules,
    parse_project_rules,
    validate_task_references,
)
from .task_prompt import (
    TASK_IMPLEMENTATION_VERSION,
    TASK_REWORK_VERSION,
    task_output_schema,
    task_prompt,
)
from .workspaces import (
    ExecutorLease,
    _git,
    create_workspace,
    executor_lock,
    inspect_workspace,
)

_REGISTRY = "multi-agent-task-preparations"
_MANIFEST = ".task-context.json"
_LIMIT = 8 * 1024 * 1024
_NOFOLLOW = os.O_NOFOLLOW | os.O_NONBLOCK
_TOKEN = re.compile(r"[0-9a-f]{32}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*\Z")
_ISSUED = object()
type TaskHistory = tuple[tuple[TaskEnvelope, Disposition], ...]


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


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{name} must be nonempty text")
    value.encode("utf-8")
    return value


def _ident(value: Any) -> tuple[int, int]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or any(type(item) is not int or item < 0 for item in value)
    ):
        raise ValueError("invalid filesystem identity")
    return value[0], value[1]


def _canonical(value: str | Path, *, existing: bool = True) -> Path:
    path = Path(value).expanduser().absolute()
    if path.resolve(strict=existing) != path:
        raise ValueError("task paths must be canonical, without symlink aliases")
    return path


def _directory(
    path: Path, *, identity: tuple[int, int] | None = None, mode: int = 0o700
) -> os.stat_result:
    _canonical(path)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != mode
        or identity is not None
        and _identity(info) != identity
    ):
        raise ValueError(f"task directory identity or mode changed: {path}")
    return info


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_file(
    path: Path, *, mode: int | None = None, keep: bool = True
) -> tuple[os.stat_result, str, bytes]:
    _canonical(path)
    fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or (
            mode is not None and stat.S_IMODE(before.st_mode) != mode
        ):
            raise ValueError(
                f"task file must be regular with its recorded mode: {path}"
            )
        digest, chunks, total = hashlib.sha256(), [], 0
        while block := os.read(fd, 1024 * 1024):
            digest.update(block)
            if keep:
                total += len(block)
                if total > _LIMIT:
                    raise ValueError("task control file exceeds its byte limit")
                chunks.append(block)
        if _stable(before) != _stable(os.fstat(fd)) or _stable(before) != _stable(
            path.lstat()
        ):
            raise ValueError(f"task file changed during inspection: {path}")
        return before, digest.hexdigest(), b"".join(chunks)
    finally:
        os.close(fd)


def _json_bytes(value: Any) -> bytes:
    raw = (task_execution_json(value) + "\n").encode("utf-8")
    if len(raw) > _LIMIT:
        raise ValueError("task control JSON exceeds its byte limit")
    return raw


def _json(raw: bytes) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict) or _json_bytes(value) != raw:
        raise ValueError("task control file is not a canonical JSON object")
    return value


def _write_bytes(fd: int, raw: bytes) -> None:
    if len(raw) > _LIMIT:
        raise ValueError("task control file exceeds its byte limit")
    remaining = memoryview(raw)
    while remaining:
        count = os.write(fd, remaining)
        if count <= 0:
            raise OSError("task control write made no progress")
        remaining = remaining[count:]


def _metadata(info: os.stat_result, digest: str) -> dict[str, Any]:
    return {"identity": _identity(info), "sha256": digest, "byte_count": info.st_size}


def _write_control(
    path: Path, raw: bytes | Callable[[tuple[int, int]], bytes]
) -> dict[str, Any]:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o400)
    try:
        os.fchmod(fd, 0o400)
        content = raw(_identity(os.fstat(fd))) if callable(raw) else raw
        _write_bytes(fd, content)
        os.fsync(fd)
    finally:
        os.close(fd)
    info, digest, actual = _read_file(path, mode=0o400)
    if actual != content:
        raise ValueError("new task control file changed after writing")
    return _metadata(info, digest)


def _check_lease(lease: ExecutorLease) -> None:
    if not lease.active:
        raise ValueError("task executor lease is no longer active")
    current = (lease.repository / "multi-agent-executor.lock").lstat()
    held = os.fstat(lease.fd)
    if not stat.S_ISREG(current.st_mode) or _identity(current) != _identity(held):
        raise ValueError("task executor lock identity changed")
    _canonical(lease.repository)


@dataclass(frozen=True, slots=True)
class _Database:
    path: Path
    identity: tuple[int, int]
    parent_identity: tuple[int, int]

    def check(self) -> None:
        actual = _database(self.path)
        if actual != self:
            raise ValueError("task database or parent identity changed")

    def value(self) -> dict[str, Any]:
        return {"path": str(self.path), "identity": self.identity}


def _database(path: str | Path) -> _Database:
    actual = _canonical(path)
    info, parent = actual.lstat(), actual.parent.lstat()
    if not stat.S_ISREG(info.st_mode) or not stat.S_ISDIR(parent.st_mode):
        raise ValueError("task database must be an existing canonical regular file")
    return _Database(actual, _identity(info), _identity(parent))


def _store_database(store: PlanningStore) -> _Database:
    # Narrow read-only connection metadata: never follow a registry's DB pointer.
    rows = store._connection.execute("PRAGMA database_list").fetchall()
    main = next(row for row in rows if row["name"] == "main")
    return _database(_text(main["file"], "SQLite main database path"))


def _positive(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be positive and finite")
    return float(value)


def capture_task_policy(
    provider: str,
    executable: str | Path,
    *,
    max_attempts: int,
    timeout: float,
    grace: float,
) -> TaskExecutionPolicy:
    """Capture executable bytes and identity without executing it."""
    if provider not in ("claude", "codex"):
        raise ValueError("task runtime provider must be claude or codex")
    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("task attempt budget must be a positive integer")
    limit, cleanup = _positive(timeout, "timeout"), _positive(grace, "grace")
    path = _canonical(executable)
    info, digest, _ = _read_file(path, keep=False)
    if not info.st_mode & 0o111:
        raise ValueError("task executable must have executable permission")
    return TaskExecutionPolicy(
        provider, str(path), _identity(info), digest, max_attempts, limit, cleanup
    )


def _policy(policy: TaskExecutionPolicy) -> None:
    if not isinstance(policy, TaskExecutionPolicy):
        raise TypeError("task policy must be a TaskExecutionPolicy")
    if not isinstance(policy.executable_identity, tuple):
        raise TypeError("executable identity must be an immutable tuple")
    _ident(policy.executable_identity)
    if not _SHA256.fullmatch(policy.executable_sha256):
        raise ValueError("invalid executable SHA256")
    actual = capture_task_policy(
        policy.provider,
        policy.executable,
        max_attempts=policy.max_attempts,
        timeout=policy.timeout,
        grace=policy.grace,
    )
    if (
        actual.executable_identity != policy.executable_identity
        or actual.executable_sha256 != policy.executable_sha256
        or actual.executable != policy.executable
    ):
        raise ValueError("task executable identity or bytes changed")


def _choice(value: ImplementationChoice) -> None:
    if not isinstance(value, ImplementationChoice) or value.provider not in (
        "codex",
        "claude-code",
    ):
        raise ValueError("invalid implementation choice")
    if not _MODEL.fullmatch(_text(value.model, "implementation model")):
        raise ValueError("invalid implementation model")
    efforts = ("low", "medium", "high", "xhigh", "max", "ultra")
    if (
        value.effort not in efforts
        or value.provider == "claude-code"
        and value.effort == "ultra"
    ):
        raise ValueError("invalid requested implementation effort")


def _dispatch(
    policy: TaskDispatchPolicy, unavailable: frozenset[tuple[str, str]]
) -> None:
    if not isinstance(policy, TaskDispatchPolicy) or not isinstance(
        policy.fallbacks, tuple
    ):
        raise TypeError("dispatch policy must be immutable")
    if policy.task_type is not None:
        _text(policy.task_type, "task type")
    if policy.requested is not None:
        _choice(policy.requested)
    for choice in policy.fallbacks:
        _choice(choice)
    if not isinstance(unavailable, frozenset):
        raise TypeError("known unavailable requests must be a frozenset")
    for item in unavailable:
        if not isinstance(item, tuple) or len(item) != 2:
            raise ValueError("invalid known unavailable request")
        _choice(ImplementationChoice(item[0], item[1], "medium"))


def _provenance(
    store: PlanningStore, run_id: str, planning_run_id: str, lease: ExecutorLease
) -> tuple[TaskInput, VerifiedPlanningProvenance]:
    value = store.load_task_input(run_id)
    stored = store.load_task_provenance(run_id)
    state = store.load_planning(planning_run_id)
    if state.phase != PlanningPhase.BOUND or state.bound_task_run_id != run_id:
        raise ValueError("task requires its exact bound planning run")
    if stored.plan_hash != state.provenance().plan_hash or not state.evidence_ready:
        raise ValueError("bound planning provenance changed")
    if task_execution_hash(
        (value.card, value.frozen_rules, value.requirements)
    ) != task_execution_hash(
        (
            stored.card,
            stored.planning_input.frozen_rules,
            stored.planning_input.requirements,
        )
    ):
        raise ValueError("task input differs from its bound planning provenance")
    validate_planning_input(stored.planning_input, configured_roles=True)
    if (
        parse_planning_draft(
            planning_json(value.card.to_dict()),
            stored.planning_input,
            configured_roles=True,
            verified_dependency_ids=state.projection.dependency_ids
            if state.projection
            else frozenset(),
        )
        != value.card
    ):
        raise ValueError("planning task card is not canonical")
    validate_task_references(value.card, value.frozen_rules)
    if task_execution_hash(
        parse_project_rules(value.frozen_rules.raw_text)
    ) != task_execution_hash(value.frozen_rules.rules):
        raise ValueError("frozen rule text differs from its parsed rules")
    if value.frozen_rules.repository != str(lease.repository):
        raise ValueError("task rules belong to another Git common directory")
    if (
        freeze_project_rules(lease.source, value.card.baseline_commit)
        != value.frozen_rules
    ):
        raise ValueError("baseline rules differ from the frozen task input")
    attempts: tuple[tuple[str, PlanningAttempt], ...] = (("review", stored.review),)
    if isinstance(stored, PlanningProvenance):
        attempts = (("draft", stored.draft), *attempts)
    else:
        inspect_projected_draft(store, planning_run_id, state, lease=lease)
    for stage, attempt in attempts:
        if not inspect_planning_execution(
            store, planning_run_id, stage, attempt.number
        ).qualified:
            raise ValueError("planning execution does not qualify")
    return value, stored


def _outside(root: Path, lease: ExecutorLease) -> None:
    forbidden = [lease.source, lease.repository]
    for entry in _git(lease.repository, "worktree", "list", "--porcelain", "-z").split(
        b"\0"
    ):
        if entry.startswith(b"worktree "):
            forbidden.append(_canonical(entry[9:].decode("utf-8")))
    identities = {_identity(path.stat()) for path in forbidden}
    if any(root.is_relative_to(path) for path in forbidden) or any(
        _identity(parent.stat()) in identities for parent in root.parents
    ):
        raise ValueError("task root must be outside worktrees and Git metadata")


def _new_root(directory: Path, database: _Database, lease: ExecutorLease) -> Path:
    root = _canonical(directory, existing=False)
    if root.exists() or root.is_symlink() or root.parent != database.path.parent:
        raise ValueError("task root must be new and a sibling of its database")
    _outside(root, lease)
    return root


def _registry(lease: ExecutorLease, *, create: bool = False) -> Path:
    _check_lease(lease)
    path = lease.repository / _REGISTRY
    if create and not path.exists() and not path.is_symlink():
        path.mkdir(mode=0o700)
        _sync_directory(lease.repository)
    _directory(path)
    return path


def _write_marker(fd: int, path: Path, value: dict[str, Any]) -> None:
    """Update the held regular inode; a torn write remains a blocking marker."""
    raw = _json_bytes(value)
    before, current = os.fstat(fd), path.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or _identity(before) != _identity(current)
        or stat.S_IMODE(current.st_mode) != 0o600
    ):
        raise ValueError("task creation marker identity or mode changed")
    os.lseek(fd, 0, os.SEEK_SET)
    _write_bytes(fd, raw)
    os.ftruncate(fd, len(raw))
    os.fsync(fd)
    if _stable(os.fstat(fd)) != _stable(path.lstat()):
        raise ValueError("task creation marker was replaced while writing")


def _marker_value(
    database: _Database,
    run_id: str,
    planning_run_id: str,
    number: int,
    root: Path,
    token: str,
    history: TaskHistory,
) -> dict[str, Any]:
    return {
        "version": 1,
        "token": token,
        "database": database.value(),
        "task_run_id": run_id,
        "planning_run_id": planning_run_id,
        "attempt": number,
        "directory": {
            "path": str(root),
            "parent_identity": database.parent_identity,
            "identity": None,
        },
        "workspace": None,
        "context": {"identity": None, "token": token, "manifest_sha256": None},
        "logs_identity": None,
        "history_prefix": {
            "length": len(history),
            "sha256": task_execution_hash(history),
        },
        "expected_pair": None,
    }


def _load_marker(
    path: Path, database: _Database
) -> tuple[dict[str, Any], tuple[int, int]]:
    if not path.name.endswith(".json") or not _TOKEN.fullmatch(path.stem):
        raise ValueError("unrecognized task registry entry")
    info, _, raw = _read_file(path, mode=0o600)
    value = _json(raw)
    if task_execution_hash(value.get("database")) != task_execution_hash(
        database.value()
    ):
        raise ValueError("task registry belongs to a different or replaced database")
    if (
        value.get("token") != path.stem
        or type(value.get("version")) is not int
        or value["version"] != 1
    ):
        raise ValueError("task creation marker token or version is invalid")
    return value, _identity(info)


def _pair(
    store: PlanningStore,
    run_id: str,
    number: int,
) -> tuple[TaskAttempt, TaskEnvelope, TaskEnvelope, TaskHistory]:
    state = store.load_task(run_id)
    selected = next((item for item in state.attempts if item.number == number), None)
    if selected is None or not selected.request.managed:
        raise ValueError("task inspection requires a managed attempt")
    history = store.load_task_history(run_id)
    applied = [
        (index, envelope)
        for index, (envelope, disposition) in enumerate(history)
        if envelope.attempt == number and disposition == Disposition.APPLIED
    ]
    if (
        len(applied) < 2
        or not isinstance(applied[0][1].event, TaskAttemptStarted)
        or not isinstance(applied[1][1].event, TaskExecutionPrepared)
        or applied[1][0] != applied[0][0] + 1
        or any(
            isinstance(item.event, (TaskAttemptStarted, TaskExecutionPrepared))
            for _, item in applied[2:]
        )
        or task_execution_hash(tuple(item.event for _, item in applied))
        != task_execution_hash(selected.events)
        or task_execution_hash(applied[0][1].event)
        != task_execution_hash(selected.request)
    ):
        raise ValueError("task history does not contain one exact atomic managed pair")
    return selected, applied[0][1], applied[1][1], history[: applied[0][0]]


def _manifest_value(
    database: _Database,
    run_id: str,
    planning_run_id: str,
    value: TaskInput,
    provenance: VerifiedPlanningProvenance,
    started: TaskEnvelope,
    prepared: TaskExecutionPrepared,
    history: TaskHistory,
    marker_path: Path,
    marker_identity: tuple[int, int],
    registry_identity: tuple[int, int],
    root_identity: tuple[int, int],
    logs_identity: tuple[int, int],
    files: dict[str, Any],
    manifest_identity: tuple[int, int],
) -> dict[str, Any]:
    context = prepared.context
    root = Path(context.path).parent
    return {
        "version": 1,
        "database": database.value(),
        "task_run_id": run_id,
        "planning_run_id": planning_run_id,
        "plan_hash": provenance.plan_hash,
        "task_input": task_execution_value(value),
        "directory": {
            "path": str(root),
            "identity": root_identity,
            "parent_identity": database.parent_identity,
        },
        "context": {
            "path": context.path,
            "identity": context.identity,
            "token": context.token,
        },
        "logs": {"path": str(root / "logs"), "identity": logs_identity},
        "registry": {
            "path": str(marker_path.parent),
            "identity": registry_identity,
            "marker_path": str(marker_path),
            "marker_identity": marker_identity,
        },
        "history_prefix": {
            "length": len(history),
            "sha256": task_execution_hash(history),
        },
        "started": task_execution_value(started),
        "workspace": task_execution_value(prepared.workspace),
        "initial_workspace_digest": prepared.initial_workspace_digest,
        "policy": task_execution_value(prepared.policy),
        "dispatch_policy": task_execution_value(prepared.dispatch_policy),
        "prompt": prepared.prompt,
        "output_schema": task_execution_value(prepared.output_schema),
        "files": files,
        "manifest_identity": manifest_identity,
    }


def _inspect_files(
    store: PlanningStore,
    run_id: str,
    planning_run_id: str,
    lease: ExecutorLease,
    database: _Database,
    value: TaskInput,
    provenance: VerifiedPlanningProvenance,
    started: TaskEnvelope,
    prepared: TaskExecutionPrepared,
    history: TaskHistory,
) -> None:
    _check_lease(lease)
    database.check()
    if _store_database(store) != database:
        raise ValueError("task store connection does not own this context database")
    _policy(prepared.policy)
    assert isinstance(started.event, TaskAttemptStarted)
    _dispatch(
        prepared.dispatch_policy, frozenset(started.event.decision.unavailable_models)
    )
    root = _canonical(Path(prepared.context.path).parent)
    if (
        root.parent != database.path.parent
        or Path(prepared.context.path) != root / "context"
    ):
        raise ValueError("task context is not in its database's sibling root")
    if prepared.workspace.root != str(root / "workspace"):
        raise ValueError("task workspace is not its root's fixed workspace member")
    _outside(root, lease)
    root_info = _directory(root)
    _directory(
        Path(prepared.context.path), identity=prepared.context.identity, mode=0o500
    )
    if set(os.listdir(root)) != {"workspace", "context", "logs"}:
        raise ValueError("task root members changed")
    if set(os.listdir(prepared.context.path)) != {
        _MANIFEST,
        "prompt.txt",
        "output-schema.json",
    }:
        raise ValueError("task context members changed")
    logs_info = _directory(root / "logs")
    registry = _registry(lease)
    if not _TOKEN.fullmatch(prepared.context.token):
        raise ValueError("invalid task context token")
    marker_path = registry / f"{prepared.context.token}.json"
    marker, marker_identity = _load_marker(marker_path, database)
    manifest_info, manifest_hash, raw = _read_file(
        root / "context" / _MANIFEST, mode=0o400
    )
    if manifest_hash != prepared.context.manifest_sha256:
        raise ValueError("task context manifest bytes changed")
    payload = _json(raw)
    files = {}
    for name, expected in (
        ("prompt.txt", prepared.prompt.encode("utf-8")),
        ("output-schema.json", _json_bytes(prepared.output_schema)),
    ):
        info, digest, content = _read_file(root / "context" / name, mode=0o400)
        if content != expected:
            raise ValueError(f"task context control bytes changed: {name}")
        files[name] = _metadata(info, digest)
    expected_manifest = _manifest_value(
        database,
        run_id,
        planning_run_id,
        value,
        provenance,
        started,
        prepared,
        history,
        marker_path,
        marker_identity,
        _identity(registry.stat()),
        _identity(root_info),
        _identity(logs_info),
        files,
        _identity(manifest_info),
    )
    if raw != _json_bytes(expected_manifest):
        raise ValueError("task context manifest binding or control identity changed")
    expected_marker = _marker_value(
        database,
        run_id,
        planning_run_id,
        started.attempt,
        root,
        prepared.context.token,
        history,
    )
    expected_marker["directory"]["identity"] = _identity(root_info)
    expected_marker["workspace"] = task_execution_value(prepared.workspace)
    expected_marker["context"] = {
        "identity": prepared.context.identity,
        "token": prepared.context.token,
        "manifest_sha256": prepared.context.manifest_sha256,
    }
    expected_marker["logs_identity"] = _identity(logs_info)
    expected_marker["expected_pair"] = {
        "started_sha256": task_execution_hash(started),
        "prepared_sha256": task_execution_hash(
            TaskEnvelope(started.attempt, prepared, started.occurred_at)
        ),
    }
    if task_execution_hash(marker) != task_execution_hash(expected_marker):
        raise ValueError("task creation marker does not match its exact preparation")
    if payload["planning_run_id"] != planning_run_id:
        raise ValueError("task context planning binding changed")
    inspect_workspace(lease, prepared.workspace)
    _check_lease(lease)
    database.check()


def inspect_task_context(
    store: PlanningStore, run_id: str, attempt: int, lease: ExecutorLease
) -> TaskExecutionPrepared:
    """Inspect durable preparation; never grant a launch or infer process stop."""
    _check_lease(lease)
    database = _store_database(store)
    selected, started, prepared_envelope, prefix = _pair(store, run_id, attempt)
    prepared = prepared_envelope.event
    assert isinstance(prepared, TaskExecutionPrepared)
    if prepared_for_task(selected) != prepared:
        raise ValueError("task state and preparation history differ")
    root = _canonical(Path(prepared.context.path).parent)
    if (
        root.parent != database.path.parent
        or Path(prepared.context.path) != root / "context"
        or prepared.workspace.root != str(root / "workspace")
    ):
        raise ValueError("task context must use the database's owned sibling layout")
    _outside(root, lease)
    _directory(root)
    _directory(
        Path(prepared.context.path), identity=prepared.context.identity, mode=0o500
    )
    path = Path(prepared.context.path) / _MANIFEST
    _, digest, raw = _read_file(path, mode=0o400)
    if digest != prepared.context.manifest_sha256:
        raise ValueError("task context manifest hash changed")
    payload = _json(raw)
    if task_execution_hash(payload.get("database")) != task_execution_hash(
        database.value()
    ):
        raise ValueError("task context belongs to another database")
    planning_run_id = _text(payload["planning_run_id"], "planning run id")
    value, provenance = _provenance(store, run_id, planning_run_id, lease)
    request = selected.request
    rework: dict[str, Any] = {}
    if isinstance(request, TaskReworkStarted):
        from .task_rework import _frozen_input

        rework = {
            "rework_source": request.source,
            "rework_input": _frozen_input(store, request.source),
        }
    prompt = task_prompt(
        value,
        provenance=provenance,
        decision=request.decision,
        dispatch_policy=prepared.dispatch_policy,
        workspace=prepared.workspace,
        initial_workspace_digest=prepared.initial_workspace_digest,
        template_version=request.prompt_template_version,
        **rework,
    )
    if prompt != prepared.prompt or task_execution_hash(
        task_output_schema()
    ) != task_execution_hash(prepared.output_schema):
        raise ValueError(
            "task frozen prompt or schema differs from its versioned inputs"
        )
    _inspect_files(
        store,
        run_id,
        planning_run_id,
        lease,
        database,
        value,
        provenance,
        started,
        prepared,
        prefix,
    )
    # The reserve API records both envelopes at this same host occurrence time.
    if prepared_envelope.occurred_at != started.occurred_at:
        raise ValueError("task preparation envelope occurrence time changed")
    return prepared


def _settled(
    store: PlanningStore, run_id: str, attempt: TaskAttempt, lease: ExecutorLease
) -> None:
    reconciliation = attempt.reconciliation
    if (
        not attempt.request.managed
        or prepared_for_task(attempt) is None
        or reconciliation is None
        or not reconciliation.process_stopped
        or not reconciliation.worktree_digest
    ):
        raise ValueError(
            f"task workspace or stopping is unknown: {run_id}/{attempt.number}"
        )
    prepared = inspect_task_context(store, run_id, attempt.number, lease)
    if (
        inspect_workspace(lease, prepared.workspace).digest
        != reconciliation.worktree_digest
    ):
        raise ValueError(
            "historical task workspace differs from its final reconciliation"
        )


def _scan_registry(
    store: PlanningStore, lease: ExecutorLease, database: _Database
) -> Path:
    registry = _registry(lease, create=True)
    for name in sorted(os.listdir(registry)):
        path = registry / name
        marker, _ = _load_marker(path, database)
        run_id = _text(marker.get("task_run_id"), "registry task run id")
        number = marker.get("attempt")
        if type(number) is not int or number < 1 or marker.get("expected_pair") is None:
            raise ValueError(f"task creation remains partial or unmatched: {path}")
        state = store.load_task(run_id)
        selected = next(
            (item for item in state.attempts if item.number == number), None
        )
        if selected is None:
            raise ValueError(f"task creation has no matching reservation: {path}")
        _settled(store, run_id, selected, lease)
        prepared = prepared_for_task(selected)
        assert prepared is not None
        if path != registry / f"{prepared.context.token}.json":
            raise ValueError("task registry entry identifies a different context")
    # A deleted marker must not make an existing DB attempt disappear from gates.
    for run_id in store.list_recoverable_tasks():
        value = store.load_task_input(run_id)
        if value.frozen_rules.repository != str(lease.repository):
            continue
        for attempt in store.load_task(run_id).attempts:
            _settled(store, run_id, attempt, lease)
    return registry


def check_task_launch_owners(store: PlanningStore, lease: ExecutorLease) -> None:
    """Retain the implementation ownership gate for the test launch route."""
    _scan_registry(store, lease, _store_database(store))


def _check_launch_owners(
    store: PlanningStore, lease: ExecutorLease, database: _Database
) -> Path:
    from multi_agent.store.task_test_store import TaskTestStore

    from .task_test_context import check_task_test_launch_owners

    registry = _scan_registry(store, lease, database)
    with TaskTestStore.open_readonly(database.path) as test_store:
        check_task_test_launch_owners(test_store, lease)
    return registry


@dataclass(frozen=True, slots=True)
class TaskLaunchBatch:
    database: _Database
    lease: ExecutorLease
    registry: Path
    task_run_ids: frozenset[str]


def prepare_task_batch(
    store: PlanningStore, lease: ExecutorLease, task_run_ids: frozenset[str]
) -> TaskLaunchBatch:
    """Check prior owners before any of the batch's fresh attempts are reserved."""
    database = _store_database(store)
    registry = _check_launch_owners(store, lease, database)
    return TaskLaunchBatch(database, lease, registry, task_run_ids)


def _policies(
    state: TaskState,
    policy: TaskExecutionPolicy | None,
    dispatch_policy: TaskDispatchPolicy | None,
) -> tuple[TaskExecutionPolicy, TaskDispatchPolicy]:
    if state.attempts:
        first = prepared_for_task(state.attempts[0])
        if first is None or not all(item.request.managed for item in state.attempts):
            raise ValueError("legacy task attempts cannot be adopted")
        if policy is not None and task_execution_hash(policy) != task_execution_hash(
            first.policy
        ):
            raise ValueError("task execution policy cannot change")
        if dispatch_policy is not None and task_execution_hash(
            dispatch_policy
        ) != task_execution_hash(first.dispatch_policy):
            raise ValueError("task dispatch policy cannot change")
        return first.policy, first.dispatch_policy
    if policy is None or dispatch_policy is None:
        raise ValueError("first preparation requires execution and dispatch policies")
    return policy, dispatch_policy


@dataclass(slots=True)
class _LaunchCapability:
    active: bool = True
    claimed: bool = False


@dataclass(frozen=True, slots=True)
class TaskLaunch:
    """An in-memory, one-use capability issued only inside a held fresh scope."""

    store: PlanningStore
    lease: ExecutorLease
    attempt: int
    prepared: TaskExecutionPrepared
    log_dir: Path
    _run_id: str = field(repr=False)
    _database: _Database = field(repr=False)
    _issue: object = field(repr=False)
    _capability: _LaunchCapability = field(
        default_factory=_LaunchCapability, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if self._issue is not _ISSUED:
            raise ValueError("task launch capabilities cannot be reconstructed")

    def __deepcopy__(self, memo: dict[int, Any]) -> TaskLaunch:
        # The immutable handle and its live store/lease are not new resources.
        # All copies must consume and expire the same issued capability.
        return self

    def check(self) -> None:
        if not self._capability.active:
            raise ValueError("task launch scope is no longer active")
        _check_lease(self.lease)
        self._database.check()
        state = self.store.load_task(self._run_id)
        selected = state.current_attempt
        if selected is None or selected.number != self.attempt:
            raise ValueError("task launch is no longer the current attempt")
        actual = inspect_task_context(
            self.store, self._run_id, self.attempt, self.lease
        )
        if task_execution_hash(actual) != task_execution_hash(self.prepared):
            raise ValueError("task launch preparation changed")

    def claim(self) -> TaskExecutionPrepared:
        if self._capability.claimed:
            raise ValueError("task launch was already claimed")
        self.check()
        state = self.store.load_task(self._run_id)
        selected = state.current_attempt
        if (
            state.phase != TaskPhase.IMPLEMENTING
            or selected is None
            or len(selected.events) != 2
        ):
            raise ValueError("task launch must precede all runtime and host events")
        if (
            inspect_workspace(self.lease, self.prepared.workspace).digest
            != self.prepared.initial_workspace_digest
        ):
            raise ValueError("task workspace changed before its fresh launch")
        self._capability.claimed = True
        return self.prepared

    def _expire(self) -> None:
        self._capability.active = False


def _create_preparation(
    store: PlanningStore,
    lease: ExecutorLease,
    database: _Database,
    run_id: str,
    planning_run_id: str,
    root: Path,
    registry: Path,
    value: TaskInput,
    provenance: VerifiedPlanningProvenance,
    history: TaskHistory,
    started: TaskEnvelope,
    policy: TaskExecutionPolicy,
    dispatch_policy: TaskDispatchPolicy,
    *,
    rework_source: TaskReworkSource | None = None,
    rework_input: ExecutedReviewInput | None = None,
) -> TaskExecutionPrepared:
    token = uuid4().hex
    marker_path = registry / f"{token}.json"
    marker = _marker_value(
        database, run_id, planning_run_id, started.attempt, root, token, history
    )
    fd: int | None = None
    failure: BaseException | None = None
    retained = (
        f"Task preparation retained registry marker {marker_path}; target root {root}. "
        "No recovery or cleanup is inferred."
    )
    try:
        fd = os.open(marker_path, os.O_RDWR | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        marker_identity = _identity(os.fstat(fd))
        registry_identity = _identity(_directory(registry))
        _write_marker(fd, marker_path, marker)
        _sync_directory(registry)
        root.mkdir(mode=0o700)
        root_identity = _identity(_directory(root))
        marker["directory"]["identity"] = root_identity
        _write_marker(fd, marker_path, marker)
        if rework_source is None:
            workspace = create_workspace(
                lease, value.card.baseline_commit, root / "workspace"
            )
        else:
            workspace = create_workspace(
                lease,
                value.card.baseline_commit,
                root / "workspace",
                start_commit=rework_source.candidate_commit,
            )
        marker["workspace"] = task_execution_value(workspace)
        _write_marker(fd, marker_path, marker)
        if (
            workspace.root != str(root / "workspace")
            or workspace.source != str(lease.source)
            or workspace.repository != str(lease.repository)
            or workspace.task_baseline != value.card.baseline_commit
            or workspace.start_commit
            != (
                value.card.baseline_commit
                if rework_source is None
                else rework_source.candidate_commit
            )
        ):
            raise ValueError("new workspace differs from its frozen task baseline")
        initial = inspect_workspace(lease, workspace).digest
        context_path, logs = root / "context", root / "logs"
        context_path.mkdir(mode=0o700)
        context_identity = _identity(_directory(context_path))
        marker["context"]["identity"] = context_identity
        _write_marker(fd, marker_path, marker)
        logs.mkdir(mode=0o700)
        logs_identity = _identity(_directory(logs))
        marker["logs_identity"] = logs_identity
        _write_marker(fd, marker_path, marker)
        assert isinstance(started.event, TaskAttemptStarted)
        rework: dict[str, Any] = {}
        if rework_source is not None:
            rework = {"rework_source": rework_source, "rework_input": rework_input}
        prompt = task_prompt(
            value,
            provenance=provenance,
            decision=started.event.decision,
            dispatch_policy=dispatch_policy,
            workspace=workspace,
            initial_workspace_digest=initial,
            template_version=started.event.prompt_template_version,
            **rework,
        )
        prepared = TaskExecutionPrepared(
            workspace,
            initial,
            prompt,
            task_output_schema(),
            policy,
            dispatch_policy,
            TaskExecutionContext(str(context_path), context_identity, token, ""),
        )
        files = {
            "prompt.txt": _write_control(
                context_path / "prompt.txt", prompt.encode("utf-8")
            ),
            "output-schema.json": _write_control(
                context_path / "output-schema.json", _json_bytes(prepared.output_schema)
            ),
        }

        def manifest_bytes(identity: tuple[int, int]) -> bytes:
            raw = _json_bytes(
                _manifest_value(
                    database,
                    run_id,
                    planning_run_id,
                    value,
                    provenance,
                    started,
                    prepared,
                    history,
                    marker_path,
                    marker_identity,
                    registry_identity,
                    root_identity,
                    logs_identity,
                    files,
                    identity,
                )
            )
            if rework_source is not None and len(raw) > _LIMIT:
                raise ValueError("rework context manifest exceeds the 8 MiB byte limit")
            return raw

        manifest = _write_control(context_path / _MANIFEST, manifest_bytes)
        context_path.chmod(0o500)
        _sync_directory(context_path)
        prepared = TaskExecutionPrepared(
            workspace,
            initial,
            prompt,
            prepared.output_schema,
            policy,
            dispatch_policy,
            TaskExecutionContext(
                str(context_path), context_identity, token, manifest["sha256"]
            ),
        )
        prepared_envelope = TaskEnvelope(started.attempt, prepared, started.occurred_at)
        marker["context"]["manifest_sha256"] = prepared.context.manifest_sha256
        marker["expected_pair"] = {
            "started_sha256": task_execution_hash(started),
            "prepared_sha256": task_execution_hash(prepared_envelope),
        }
        _write_marker(fd, marker_path, marker)
        _sync_directory(root)
        current_value, current_provenance = _provenance(
            store, run_id, planning_run_id, lease
        )
        if task_execution_hash(
            (current_value, current_provenance)
        ) != task_execution_hash((value, provenance)):
            raise ValueError("task planning inputs changed during preparation")
        _inspect_files(
            store,
            run_id,
            planning_run_id,
            lease,
            database,
            value,
            provenance,
            started,
            prepared,
            history,
        )
        if inspect_workspace(lease, workspace).digest != initial:
            raise ValueError("task workspace changed during preparation")
        if rework_source is None:
            store.reserve_task_attempt(
                run_id,
                started,
                prepared_envelope,
                expected_history_length=len(history),
                expected_history_hash=task_execution_hash(history),
            )
        else:
            from multi_agent.store.task_review_store import TaskReviewStore

            if not isinstance(store, TaskReviewStore):
                raise TypeError("rework reservation requires a TaskReviewStore")
            store.reserve_task_rework_execution(run_id, started, prepared_envelope)
        actual = inspect_task_context(store, run_id, started.attempt, lease)
        if task_execution_hash(actual) != task_execution_hash(prepared):
            raise ValueError("task reservation did not retain this exact preparation")
        current = store.load_task(run_id).current_attempt
        if current is None or current.number != started.attempt:
            raise ValueError("new task reservation is no longer current")
        return prepared
    except BaseException as error:
        failure = error
        error.add_note(retained)
        raise
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError as error:
                if failure is None:
                    error.add_note(retained)
                    raise
                failure.add_note(
                    f"Closing retained registry marker {marker_path} also failed: {error!r}"
                )


@contextmanager
def prepare_task_launch(
    database_path: str | Path,
    task_run_id: str,
    planning_run_id: str,
    *,
    repository: Path,
    directory: Path,
    policy: TaskExecutionPolicy | None = None,
    dispatch_policy: TaskDispatchPolicy | None = None,
    unavailable: frozenset[tuple[str, str]] = frozenset(),
    batch: TaskLaunchBatch | None = None,
) -> Iterator[TaskLaunch]:
    """Reserve and yield one new launch while retaining its store and executor lease."""
    database = _database(database_path)
    _text(task_run_id, "task run id")
    _text(planning_run_id, "planning run id")
    owner = executor_lock(repository) if batch is None else nullcontext(batch.lease)
    with PlanningStore(database.path) as store, owner as lease:
        if batch is not None and (
            database != batch.database or task_run_id not in batch.task_run_ids
        ):
            raise ValueError("task is outside the admitted execution batch")
        _check_lease(lease)
        database.check()
        if _store_database(store) != database:
            raise ValueError("task database connection identity differs")
        value, provenance = _provenance(store, task_run_id, planning_run_id, lease)
        root = _new_root(directory, database, lease)
        state = store.load_task(task_run_id)
        policy, dispatch_policy = _policies(state, policy, dispatch_policy)
        _policy(policy)
        _dispatch(dispatch_policy, unavailable)
        decision = resolve_dispatch(
            value.card,
            value.frozen_rules,
            task_type=dispatch_policy.task_type,
            requested=dispatch_policy.requested,
            fallbacks=dispatch_policy.fallbacks,
            unavailable=unavailable,
        )
        if (
            policy.provider
            != {"codex": "codex", "claude-code": "claude"}[decision.choice.provider]
        ):
            raise ValueError("task policy provider differs from its selected request")
        history = store.load_task_history(task_run_id)
        number = len(state.attempts) + 1
        when = datetime.now(UTC).isoformat()
        started = TaskEnvelope(
            number,
            TaskAttemptStarted(
                decision,
                str(root / "workspace"),
                TASK_IMPLEMENTATION_VERSION,
                when,
                managed=True,
            ),
            when,
        )
        if number > policy.max_attempts:
            raise ValueError("task attempt budget is exhausted")
        _, disposition = apply_task(state, started)
        if disposition != Disposition.APPLIED:
            raise ValueError("task launch must reserve a new current attempt")
        # The batch checked prior owners under this same continuously held lease.
        registry = (
            _check_launch_owners(store, lease, database)
            if batch is None
            else batch.registry
        )
        database.check()
        prepared = _create_preparation(
            store,
            lease,
            database,
            task_run_id,
            planning_run_id,
            root,
            registry,
            value,
            provenance,
            history,
            started,
            policy,
            dispatch_policy,
        )
        launch = TaskLaunch(
            store,
            lease,
            number,
            prepared,
            root / "logs",
            task_run_id,
            database,
            _ISSUED,
        )
        try:
            launch.check()
            yield launch
        finally:
            launch._expire()


@contextmanager
def prepare_task_rework_launch(
    database_path: str | Path,
    task_run_id: str,
    planning_run_id: str,
    review_round_id: str,
    *,
    repository: Path,
    directory: Path,
    unavailable: frozenset[tuple[str, str]] = frozenset(),
) -> Iterator[TaskLaunch]:
    """Reserve the one rework and retain both existing locks through settlement."""
    from multi_agent.store.task_review_store import TaskReviewStore

    from . import runner
    from .task_review_cli import _input, _origin, _review_directory
    from .task_rework import ReworkRefused, prepare_task_rework_source
    from .task_test_context import check_task_test_launch_owners

    database = _database(database_path)
    _text(task_run_id, "task run id")
    _text(planning_run_id, "planning run id")
    _text(review_round_id, "review round id")
    with TaskReviewStore(database.path) as store, executor_lock(repository) as lease:
        database.check()
        if _store_database(store) != database:
            raise ValueError("task database connection identity differs")
        review_input = _input(store, review_round_id)
        metadata = runner.metadata_for(store, review_round_id)
        if _origin(store, review_input, metadata) != lease.source:
            raise ValueError("rework repository differs from the reviewed source")
        review_directory = _review_directory(metadata, lease)
        with runner.execution_lock(review_directory):
            source = prepare_task_rework_source(store, review_round_id, lease)
            if source.task_run_id != task_run_id:
                raise ValueError("rework round belongs to a different task run")
            value, provenance = _provenance(store, task_run_id, planning_run_id, lease)
            root = _new_root(directory, database, lease)
            state = store.load_task(task_run_id)
            current = state.current_attempt
            if current is None or current.number != source.implementation_attempt:
                raise ValueError("rework implementation changed before preparation")
            policy, dispatch_policy = _policies(state, None, None)
            _policy(policy)
            _dispatch(dispatch_policy, unavailable)
            original_unavailable = frozenset(
                current.request.decision.unavailable_models
            )
            if unavailable and unavailable != original_unavailable:
                raise ValueError("rework cannot change frozen unavailable requests")
            decision = resolve_dispatch(
                value.card,
                value.frozen_rules,
                task_type=dispatch_policy.task_type,
                requested=dispatch_policy.requested,
                fallbacks=dispatch_policy.fallbacks,
                unavailable=original_unavailable,
            )
            if (
                policy.provider
                != {"codex": "codex", "claude-code": "claude"}[decision.choice.provider]
            ):
                raise ValueError(
                    "task policy provider differs from its selected request"
                )
            history = store.load_task_history(task_run_id)
            if (
                len(history) != source.task_history_length
                or task_execution_hash(history) != source.task_history_hash
            ):
                raise ValueError("task history changed before rework preparation")
            number = current.number + 1
            if number > policy.max_attempts:
                raise ReworkRefused(
                    "budget_exhausted",
                    "the frozen implementation attempt budget is exhausted",
                )
            when = datetime.now(UTC).isoformat()
            started = TaskEnvelope(
                number,
                TaskReworkStarted(
                    decision,
                    str(root / "workspace"),
                    TASK_REWORK_VERSION,
                    when,
                    managed=True,
                    source=source,
                ),
                when,
            )
            _, disposition = apply_task(state, started)
            if disposition != Disposition.APPLIED:
                raise ValueError("rework must reserve a new current attempt")
            registry = _scan_registry(store, lease, database)
            check_task_test_launch_owners(store, lease)
            database.check()
            prepared = _create_preparation(
                store,
                lease,
                database,
                task_run_id,
                planning_run_id,
                root,
                registry,
                value,
                provenance,
                history,
                started,
                policy,
                dispatch_policy,
                rework_source=source,
                rework_input=review_input,
            )
            launch = TaskLaunch(
                store,
                lease,
                number,
                prepared,
                root / "logs",
                task_run_id,
                database,
                _ISSUED,
            )
            try:
                launch.check()
                yield launch
            finally:
                launch._expire()
