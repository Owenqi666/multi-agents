"""Bind an actual Git candidate to an independently rechecked task execution.

The caller holds the common-directory executor lease throughout this operation.
Git publication and SQLite recording are deliberately not presented as one
transaction: every failure retains the published ref and all execution evidence.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from multi_agent.core.path_patterns import path_matches
from multi_agent.core.state import Disposition
from multi_agent.core.task_events import (
    TaskCandidateFrozen,
    TaskEnvelope,
    TaskExecutionPrepared,
)
from multi_agent.core.task_execution import prepared_for_task, task_execution_hash
from multi_agent.core.task_state import TaskInput, TaskPhase, TaskState
from multi_agent.core.workspaces import WorkspaceDelivery, WorkspaceFile
from multi_agent.store.planning_store import PlanningStore

from .snapshot import MAX_FILE_BYTES, MAX_FILES, MAX_SNAPSHOT_BYTES
from .task_context import _check_lease, _Database, _store_database
from .task_evidence import TaskExecutionManifest, inspect_task_execution
from .workspaces import (
    DeliveryRejected,
    ExecutorLease,
    _baseline_files,
    _blob_id,
    _git,
    _inspection,
    freeze_delivery,
)

type _History = tuple[tuple[TaskEnvelope, Disposition], ...]
type _Metadata = tuple[int, ...]


def _metadata(info: os.stat_result) -> _Metadata:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _gate(
    store: PlanningStore, run_id: str, attempt: int, *, bound: bool
) -> tuple[TaskState, TaskExecutionPrepared]:
    # No Git or workspace access is allowed before this state-only gate.
    state = store.load_task(run_id)
    current = state.current_attempt
    if (
        not isinstance(attempt, int)
        or isinstance(attempt, bool)
        or current is None
        or current.number != attempt
        or not current.request.managed
        or state.phase != TaskPhase.AWAITING_DELIVERY
        or not state.delivery_ready
    ):
        raise ValueError("candidate requires the current qualified managed delivery")
    prepared = prepared_for_task(current)
    if prepared is None:
        raise ValueError("candidate requires an execution preparation")
    if bound:
        if not state.candidate_ready or state.current_candidate is None:
            raise ValueError("candidate has no current qualifying binding")
    elif state.current_candidate is not None:
        raise ValueError("candidate already recorded; inspect the existing binding")
    return state, prepared


@dataclass(frozen=True, slots=True)
class _Guard:
    store: PlanningStore
    run_id: str
    attempt: int
    lease: ExecutorLease
    database: _Database
    lease_identity: tuple[Path, Path, int, int, int]
    state: TaskState
    prepared: TaskExecutionPrepared
    value: TaskInput
    history: _History

    def check_identity(self) -> None:
        _check_lease(self.lease)
        held = os.fstat(self.lease.fd)
        if self.lease_identity != (
            self.lease.source,
            self.lease.repository,
            self.lease.fd,
            held.st_dev,
            held.st_ino,
        ):
            raise ValueError("candidate executor lease identity changed")
        self.database.check()
        if _store_database(self.store) != self.database:
            raise ValueError("candidate store connection identity changed")

    def check(self, envelope: TaskEnvelope | None = None) -> None:
        self.check_identity()
        expected = self.state
        history = self.history
        if envelope is not None:
            current = expected.current_attempt
            assert current is not None
            expected = replace(
                expected,
                attempts=expected.attempts[:-1]
                + (replace(current, events=current.events + (envelope.event,)),),
            )
            history += ((envelope, Disposition.APPLIED),)
        if (
            self.store.load_task_history(self.run_id) != history
            or self.store.load_task(self.run_id) != expected
            or self.store.load_task_input(self.run_id) != self.value
        ):
            raise ValueError("candidate source or original task history changed")
        self.check_identity()


def _capture(
    store: PlanningStore,
    run_id: str,
    attempt: int,
    lease: ExecutorLease,
    *,
    bound: bool,
) -> _Guard:
    state, prepared = _gate(store, run_id, attempt, bound=bound)
    history = store.load_task_history(run_id)
    value = store.load_task_input(run_id)
    _check_lease(lease)
    database = _store_database(store)
    if str(lease.repository) != prepared.workspace.repository:
        raise ValueError("candidate lease belongs to another common directory")
    held = os.fstat(lease.fd)
    guard = _Guard(
        store,
        run_id,
        attempt,
        lease,
        database,
        (lease.source, lease.repository, lease.fd, held.st_dev, held.st_ino),
        state,
        prepared,
        value,
        history,
    )
    guard.check()
    return guard


def _archive(guard: _Guard) -> TaskExecutionManifest:
    guard.check()
    archive = inspect_task_execution(
        guard.store, guard.run_id, guard.attempt, guard.lease
    )
    current = guard.state.current_attempt
    assert current is not None and current.reconciliation is not None
    reconciliation = current.reconciliation
    if (
        not archive.qualified
        or not archive.delivery_eligible
        or archive.report is None
        or archive.report.outcome != "completed"
        or archive.final_digest != reconciliation.worktree_digest
        or archive.path != reconciliation.log_path
        or archive.sha256 != reconciliation.log_sha256
    ):
        raise ValueError("candidate execution archive does not qualify for delivery")
    guard.check()
    return archive


def _oid(value: str, object_format: str) -> None:
    if (
        object_format not in ("sha1", "sha256")
        or re.fullmatch(rf"[0-9a-f]{{{40 if object_format == 'sha1' else 64}}}", value)
        is None
    ):
        raise ValueError(
            "candidate requires a full Git object ID of the recorded format"
        )


def _ref_name(guard: _Guard) -> str:
    owner = guard.prepared.workspace.owner_token
    if re.fullmatch(r"[0-9a-f]{32}", owner) is None:
        raise ValueError("candidate workspace owner token is invalid")
    return f"refs/multi-agent/candidates/{owner}"


def _read_ref_file(path: Path) -> tuple[_Metadata, bytes] | None:
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"candidate ref control must be a regular file: {path}")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened = os.fstat(fd)
        if _metadata(opened) != _metadata(before):
            raise ValueError("candidate ref control changed before opening")
        with os.fdopen(os.dup(fd), "rb") as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError("candidate ref control exceeds the byte limit")
        if _metadata(os.fstat(fd)) != _metadata(before) or _metadata(
            path.lstat()
        ) != _metadata(before):
            raise ValueError("candidate ref control changed while reading")
        return _metadata(before), raw
    finally:
        os.close(fd)


@dataclass(frozen=True, slots=True)
class _Refs:
    directories: tuple[tuple[Path, _Metadata | None], ...]
    loose: tuple[_Metadata, bytes] | None
    packed: tuple[_Metadata, bytes] | None


def _ref_files(guard: _Guard, expected: str | None) -> _Refs:
    ref = _ref_name(guard)
    parent = guard.lease.repository
    directories: list[tuple[Path, _Metadata | None]] = []
    for part in ("refs", "multi-agent", "candidates"):
        parent /= part
        try:
            info = parent.lstat()
        except FileNotFoundError:
            directories.append((parent, None))
        else:
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("candidate ref namespace is not a real directory")
            directories.append((parent, _metadata(info)))
    loose = _read_ref_file(guard.lease.repository / ref)
    packed = _read_ref_file(guard.lease.repository / "packed-refs")
    packed_ids: list[str] = []
    if packed is not None:
        for line in packed[1].splitlines():
            if not line or line.startswith((b"#", b"^")):
                continue
            parts = line.split(b" ")
            if len(parts) != 2:
                raise ValueError("malformed packed Git ref")
            if parts[1] == ref.encode("ascii"):
                oid = parts[0].decode("ascii")
                _oid(oid, guard.prepared.workspace.object_format)
                packed_ids.append(oid)
    if len(packed_ids) > 1:
        raise ValueError("duplicate packed candidate ref")
    if expected is None:
        if loose is not None or packed_ids:
            raise ValueError("unbound candidate ref already exists; refusing adoption")
    else:
        _oid(expected, guard.prepared.workspace.object_format)
        if loose is not None:
            if loose[1] != (expected + "\n").encode("ascii"):
                raise ValueError(
                    "loose candidate ref is not the exact direct object ID"
                )
        elif packed_ids != [expected]:
            raise ValueError("candidate has no matching direct loose or packed ref")
    return _Refs(tuple(directories), loose, packed)


def _refs(guard: _Guard, expected: str | None) -> _Refs:
    before = _ref_files(guard, expected)
    ref = _ref_name(guard)
    raw = _git(
        guard.lease.repository,
        "for-each-ref",
        "--format=%(refname)%00%(objectname)%00%(objecttype)%00%(symref)",
        ref,
    )
    wanted = (
        b"" if expected is None else f"{ref}\0{expected}\0commit\0\n".encode("ascii")
    )
    if raw != wanted:
        raise ValueError("Git candidate ref/type/symbolic target does not match")
    if _ref_files(guard, expected) != before:
        raise ValueError("candidate ref metadata changed during Git inspection")
    return before


def _objects(
    repository: Path, identifiers: tuple[str, ...], kind: str, object_format: str
) -> tuple[bytes, ...]:
    for oid in identifiers:
        _oid(oid, object_format)
    raw = _git(
        repository,
        "cat-file",
        "--batch",
        input_bytes="".join(f"{oid}\n" for oid in identifiers).encode("ascii"),
    )
    offset = 0
    bodies: list[bytes] = []
    total = 0
    for oid in identifiers:
        end = raw.find(b"\n", offset)
        if end < 0:
            raise ValueError("Git object response is incomplete")
        parts = raw[offset:end].split(b" ")
        if len(parts) != 3 or parts[:2] != [oid.encode("ascii"), kind.encode("ascii")]:
            raise ValueError("Git object response identity or type differs")
        size = int(parts[2])
        total += size
        if size < 0 or size > MAX_FILE_BYTES or total > MAX_SNAPSHOT_BYTES:
            raise ValueError("Git object content exceeds the byte limit")
        body = raw[end + 1 : end + 1 + size]
        offset = end + size + 2
        if len(body) != size or raw[offset - 1 : offset] != b"\n":
            raise ValueError("Git object body is incomplete")
        actual = hashlib.new(
            object_format, f"{kind} {size}\0".encode("ascii") + body
        ).hexdigest()
        if actual != oid:
            raise ValueError("Git object bytes do not match their content address")
        bodies.append(body)
    if offset != len(raw):
        raise ValueError("Git object response contains extra bytes")
    return tuple(bodies)


def _revision(
    repository: Path, revision: str, object_format: str
) -> tuple[str, tuple[str, ...], tuple[WorkspaceFile, ...]]:
    (commit,) = _objects(repository, (revision,), "commit", object_format)
    header, separator, _ = commit.partition(b"\n\n")
    if not separator:
        raise ValueError("Git commit has no complete header")
    # Other headers and the message may legitimately contain non-UTF8 bytes.
    trees = [
        line[5:].decode("ascii")
        for line in header.splitlines()
        if line.startswith(b"tree ")
    ]
    parents = tuple(
        line[7:].decode("ascii")
        for line in header.splitlines()
        if line.startswith(b"parent ")
    )
    if len(trees) != 1:
        raise ValueError("Git commit must identify exactly one tree")
    tree = trees[0]
    for oid in (tree, *parents):
        _oid(oid, object_format)
    entries = _git(repository, "ls-tree", "-r", "-t", "-z", "--full-tree", tree)
    tree_ids = {tree}
    records = entries.split(b"\0")
    if len(records) > MAX_FILES * 4 + 1:
        raise ValueError("Git tree exceeds the entry limit")
    for record in records:
        if not record:
            continue
        metadata, _ = record.split(b"\t", 1)
        mode, kind, oid = metadata.decode("ascii").split()
        _oid(oid, object_format)
        if kind == "tree" and mode == "040000":
            tree_ids.add(oid)
        elif kind != "blob" or mode not in ("100644", "100755", "120000"):
            raise ValueError("candidate tree contains a gitlink or special entry")
    _objects(repository, tuple(sorted(tree_ids)), "tree", object_format)
    files = _baseline_files(repository, revision)
    if any(_blob_id(item.content, object_format) != item.oid for item in files):
        raise ValueError("Git blob bytes do not match their content address")
    return tree, parents, files


def _verify_git(
    guard: _Guard, candidate: TaskCandidateFrozen, archive: TaskExecutionManifest
) -> _Refs:
    delivery = candidate.delivery
    record = guard.prepared.workspace
    if (
        delivery.ref != _ref_name(guard)
        or delivery.baseline != guard.state.baseline_commit
        or delivery.baseline != record.task_baseline
        or delivery.baseline != guard.value.card.baseline_commit
        or delivery.digest != archive.final_digest
        or candidate.execution_log_path != archive.path
        or candidate.execution_log_sha256 != archive.sha256
    ):
        raise ValueError(
            "candidate differs from its original task or execution archive"
        )
    before = _refs(guard, delivery.candidate)
    repository = guard.lease.repository
    if _git(repository, "rev-parse", "--show-object-format") != (
        record.object_format + "\n"
    ).encode("ascii"):
        raise ValueError("candidate repository object format changed")
    tree, parents, files = _revision(
        repository, delivery.candidate, record.object_format
    )
    if tree != delivery.tree or parents != (record.start_commit,):
        raise ValueError("candidate tree or exact single parent differs")
    _, _, baseline = _revision(repository, delivery.baseline, record.object_format)
    if record.start_commit not in (delivery.baseline, delivery.candidate):
        _revision(repository, record.start_commit, record.object_format)
    if _inspection(files).digest != delivery.digest:
        raise ValueError("candidate Git files differ from the final execution digest")
    original = {item.path: item for item in baseline}
    current = {item.path: item for item in files}
    changed = tuple(
        sorted(
            path
            for path in original.keys() | current.keys()
            if original.get(path) != current.get(path)
        )
    )
    if delivery.changed_paths != changed:
        raise ValueError("candidate changed paths differ from the original baseline")
    forbidden = tuple(
        path
        for path in changed
        if not any(
            path_matches(path, pattern) for pattern in guard.value.card.write_scope
        )
        or any(
            path_matches(path, pattern)
            for pattern in guard.value.frozen_rules.rules.protected_paths
        )
    )
    if forbidden:
        raise DeliveryRejected(forbidden)
    if _refs(guard, delivery.candidate) != before:
        raise ValueError("candidate ref changed during object inspection")
    return before


class _HistoryUnavailable(Exception):
    """Defer only the recovery read's error, retaining the original write error."""


def _failure_history(store: PlanningStore, run_id: str) -> _History:
    try:
        return store.load_task_history(run_id)
    except Exception as error:
        raise _HistoryUnavailable(str(error)) from error


def _record_failure_note(
    error: Exception, guard: _Guard, envelope: TaskEnvelope
) -> None:
    try:
        history = _failure_history(guard.store, guard.run_id)
    except _HistoryUnavailable as read_error:
        error.add_note(f"candidate binding unknown: history read failed: {read_error}")
    else:
        if (envelope, Disposition.APPLIED) in history:
            error.add_note(
                "candidate binding confirmed committed: exact envelope is APPLIED"
            )
        else:
            error.add_note(
                "candidate binding not confirmed: exact APPLIED envelope not found"
            )
    error.add_note(
        "candidate ref and original workspace/context/logs retained; no rollback attempted"
    )


def freeze_task_candidate(
    store: PlanningStore, task_run_id: str, attempt: int, lease: ExecutorLease
) -> TaskCandidateFrozen:
    """Publish once, then bind using the original complete-history CAS."""
    guard = _capture(store, task_run_id, attempt, lease, bound=False)
    original_refs = _ref_files(guard, None)
    archive = _archive(guard)
    if _refs(guard, None) != original_refs:
        raise ValueError("candidate ref metadata changed during archive inspection")
    guard.check()
    delivery: WorkspaceDelivery = freeze_delivery(
        lease, guard.prepared.workspace, guard.value.card, guard.value.frozen_rules
    )
    guard.check()
    candidate = TaskCandidateFrozen(delivery, archive.path, archive.sha256)
    refs = _verify_git(guard, candidate, archive)
    checked_archive = _archive(guard)
    if (
        checked_archive != archive
        or _verify_git(guard, candidate, checked_archive) != refs
    ):
        raise ValueError("candidate or execution archive changed before recording")
    guard.check()
    envelope = TaskEnvelope(attempt, candidate, datetime.now(UTC).isoformat())
    try:
        disposition = store.record_task_candidate(
            task_run_id,
            envelope,
            expected_history_length=len(guard.history),
            expected_history_hash=task_execution_hash(guard.history),
        )
    except Exception as error:
        _record_failure_note(error, guard, envelope)
        raise
    if disposition != Disposition.APPLIED:
        raise ValueError("candidate recording did not apply the current host event")
    guard.check(envelope)
    inspected = inspect_task_candidate(store, task_run_id, attempt, lease)
    if inspected != candidate:
        raise ValueError("candidate binding changed after recording")
    # Public inspection captures its own H. It cannot refresh this call's H.
    guard.check(envelope)
    return candidate


def inspect_task_candidate(
    store: PlanningStore, task_run_id: str, attempt: int, lease: ExecutorLease
) -> TaskCandidateFrozen:
    """Recheck the persisted candidate without writing or recreating a runtime."""
    guard = _capture(store, task_run_id, attempt, lease, bound=True)
    candidate = guard.state.current_candidate
    assert candidate is not None
    original_refs = _ref_files(guard, candidate.delivery.candidate)
    archive = _archive(guard)
    refs = _verify_git(guard, candidate, archive)
    if refs != original_refs:
        raise ValueError("candidate ref metadata changed during archive inspection")
    checked_archive = _archive(guard)
    if (
        checked_archive != archive
        or _verify_git(guard, candidate, checked_archive) != refs
    ):
        raise ValueError("candidate or execution archive changed during inspection")
    guard.check()
    return candidate
