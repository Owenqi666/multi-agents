"""Fresh test launches, their durable preparation and shared executor ownership."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from multi_agent.core import Disposition
from multi_agent.core.task_execution import task_execution_hash, task_execution_value
from multi_agent.core.task_test_events import (
    TaskTestAttemptStarted,
    TaskTestCommandStarted,
    TaskTestEnvelope,
    TaskTestExecutionPrepared,
)
from multi_agent.core.task_test_execution import (
    TaskTestContext,
    TaskTestPolicy,
    TaskTestScratch,
    prepared_for_test,
)
from multi_agent.core.task_test_state import TaskTestAttempt, TaskTestState
from multi_agent.core.transitions import _apply_task_test
from multi_agent.store.task_test_store import TaskTestHistory, TaskTestStore

from .task_context import (
    _canonical,
    _check_lease,
    _Database,
    _database,
    _directory,
    _identity,
    _json_bytes,
    _load_marker,
    _new_root,
    _outside,
    _positive,
    _read_file,
    _stable,
    _store_database,
    _sync_directory,
    _text,
    _write_control,
    _write_marker,
    check_task_launch_owners,
)
from .task_test_checkouts import (
    create_task_test_checkout,
    inspect_initial_task_test_checkout,
    inspect_task_test_checkout_controls,
)
from .task_test_inputs import _declaration, prepare_task_test_input
from .workspaces import ExecutorLease, executor_lock

_REGISTRY = "multi-agent-task-test-preparations"
_MANIFEST = ".task-test-context.json"
_ISSUED = object()


def capture_task_test_policy(
    executable: str | Path = "codex",
    *,
    timeout: float,
    grace: float,
    output_bytes_limit: int = 8 * 1024 * 1024,
    tool_directories: tuple[str, ...] = (),
    runtime_read_paths: tuple[str, ...] = (),
) -> TaskTestPolicy:
    limit, cleanup = _positive(timeout, "timeout"), _positive(grace, "grace")
    if (
        type(output_bytes_limit) is not int
        or not 1 <= output_bytes_limit <= 64 * 1024 * 1024
    ):
        raise ValueError("test output budget must be between 1 and 67108864 bytes")
    name = str(executable)
    resolved = shutil.which(name) if "/" not in name else name
    if resolved is None:
        raise ValueError("test Codex executable was not found")
    path = Path(resolved).expanduser().resolve(strict=True)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or not before.st_mode & 0o111
            or before.st_size > 512 * 1024 * 1024
        ):
            raise ValueError(
                "test Codex executable must be a bounded regular executable"
            )
        digest, total = hashlib.sha256(), 0
        while block := os.read(fd, 1024 * 1024):
            total += len(block)
            if total > before.st_size:
                raise ValueError("test Codex executable grew during capture")
            digest.update(block)
        if _stable(before) != _stable(os.fstat(fd)) or _stable(before) != _stable(
            path.lstat()
        ):
            raise ValueError("test Codex executable changed during capture")
    finally:
        os.close(fd)
    paths: list[tuple[str, ...]] = []
    home = Path.home().resolve()
    credential_roots = (
        home / ".codex",
        home / ".claude",
        home / ".ssh",
        home / ".aws",
        home / ".config",
    )
    for values in (tool_directories, runtime_read_paths):
        if not isinstance(values, tuple):
            raise TypeError("test runtime paths must be immutable tuples")
        actual = tuple(
            str(Path(_text(item, "runtime path")).expanduser().resolve(strict=True))
            for item in values
        )
        if len(set(actual)) != len(actual):
            raise ValueError("test runtime paths must be unique")
        for item in actual:
            root = Path(item)
            if (
                not root.is_dir()
                or home.is_relative_to(root)
                or any(
                    secret.is_relative_to(root) or root.is_relative_to(secret)
                    for secret in credential_roots
                )
            ):
                raise ValueError(
                    "test runtime roots cannot expose host home or credentials"
                )
        paths.append(actual)
    return TaskTestPolicy(
        executable=str(path),
        executable_identity=_identity(before),
        executable_sha256=digest.hexdigest(),
        timeout=limit,
        grace=cleanup,
        output_bytes_limit=output_bytes_limit,
        tool_directories=paths[0],
        runtime_read_paths=paths[1],
    )


def _policy(value: TaskTestPolicy) -> None:
    actual = capture_task_test_policy(
        value.executable,
        timeout=value.timeout,
        grace=value.grace,
        output_bytes_limit=value.output_bytes_limit,
        tool_directories=value.tool_directories,
        runtime_read_paths=value.runtime_read_paths,
    )
    if task_execution_hash(actual) != task_execution_hash(value):
        raise ValueError("test policy executable or fixed scope changed")


def _registry(lease: ExecutorLease, *, create: bool = False) -> Path:
    _check_lease(lease)
    path = lease.repository / _REGISTRY
    if create and not path.exists() and not path.is_symlink():
        path.mkdir(mode=0o700)
        _sync_directory(lease.repository)
    _directory(path)
    return path


def _test_root_owner(store: TaskTestStore, lease: ExecutorLease, root: Path) -> str:
    database = _store_database(store)
    _directory(root)
    _outside(root, lease)
    matches = []
    for path in _registry(lease).iterdir():
        marker, _ = _load_marker(path, database)
        if marker.get("directory") == str(root):
            if marker.get("root_identity") != list(
                _identity(root.stat())
            ) or marker.get("parent_identity") != list(database.parent_identity):
                raise ValueError("test root marker identity changed")
            matches.append(path.stem)
    if len(matches) != 1:
        raise ValueError("test checkout has no unique durable owned root")
    return matches[0]


def _scratch(value: TaskTestScratch, root: Path) -> None:
    if Path(value.path) != root / "scratch":
        raise ValueError("test scratch path differs from its owned layout")
    for path, identity in (
        (Path(value.path), value.identity),
        (Path(value.path) / "home", value.home_identity),
        (Path(value.path) / "tmp", value.tmp_identity),
        (Path(value.path) / "cache", value.cache_identity),
    ):
        _directory(path, identity=identity)


def create_test_scratch(root: Path) -> TaskTestScratch:
    for name in ("scratch", "scratch/home", "scratch/tmp", "scratch/cache"):
        (root / name).mkdir(mode=0o700)
    return TaskTestScratch(
        path=str(root / "scratch"),
        identity=_identity(_directory(root / "scratch")),
        home_identity=_identity(_directory(root / "scratch/home")),
        tmp_identity=_identity(_directory(root / "scratch/tmp")),
        cache_identity=_identity(_directory(root / "scratch/cache")),
    )


def check_test_runtime_paths(
    policy: TaskTestPolicy, root: Path, database: Path, lease: ExecutorLease
) -> None:
    for item in (*policy.tool_directories, *policy.runtime_read_paths):
        path = Path(item)
        _outside(path, lease)
        if any(
            forbidden.is_relative_to(path) or path.is_relative_to(forbidden)
            for forbidden in (root, database, lease.source, lease.repository)
        ):
            raise ValueError(
                "test runtime path exposes protected source or host storage"
            )


def _pair(
    store: TaskTestStore, run_id: str, implementation_attempt: int, number: int
) -> tuple[TaskTestAttempt, TaskTestEnvelope, TaskTestEnvelope, TaskTestHistory]:
    state = store.load_task_test(run_id, implementation_attempt)
    selected = next((item for item in state.attempts if item.number == number), None)
    if selected is None:
        raise ValueError("test attempt has not started")
    history = store.load_task_test_history(run_id, implementation_attempt)
    applied = [
        (index, envelope)
        for index, (envelope, disposition) in enumerate(history)
        if envelope.test_attempt == number and disposition == Disposition.APPLIED
    ]
    if (
        len(applied) < 2
        or not isinstance(applied[0][1].event, TaskTestAttemptStarted)
        or not isinstance(applied[1][1].event, TaskTestExecutionPrepared)
        or applied[1][0] != applied[0][0] + 1
    ):
        raise ValueError("test attempt has no exact atomic preparation pair")
    started, prepared = applied[0][1], applied[1][1]
    if started.occurred_at != prepared.occurred_at or task_execution_hash(
        tuple(item.event for _, item in applied)
    ) != task_execution_hash(selected.events):
        raise ValueError("test preparation history differs from its attempt")
    return selected, started, prepared, history[: applied[0][0]]


def _manifest(
    database: _Database,
    started: TaskTestEnvelope,
    prepared: TaskTestExecutionPrepared,
    history: TaskTestHistory,
    marker_path: Path,
    marker_identity: tuple[int, int],
    registry_identity: tuple[int, int],
    root_identity: tuple[int, int],
    logs_identity: tuple[int, int],
    manifest_identity: tuple[int, int],
) -> dict[str, Any]:
    return {
        "version": 1,
        "database": database.value(),
        "directory": str(Path(prepared.context.path).parent),
        "parent_identity": database.parent_identity,
        "root_identity": root_identity,
        "logs_identity": logs_identity,
        "started": task_execution_value(started),
        "checkout": task_execution_value(prepared.checkout),
        "policy": task_execution_value(prepared.policy),
        "scratch": task_execution_value(prepared.scratch),
        "context": {
            "path": prepared.context.path,
            "identity": prepared.context.identity,
            "token": prepared.context.token,
        },
        "history_prefix": {
            "length": len(history),
            "sha256": task_execution_hash(history),
        },
        "registry": {
            "path": str(marker_path.parent),
            "identity": registry_identity,
            "marker_path": str(marker_path),
            "marker_identity": marker_identity,
        },
        "manifest_identity": manifest_identity,
    }


def _marker(
    database: _Database,
    root: Path,
    run_id: str,
    implementation_attempt: int,
    number: int,
    token: str,
    history: TaskTestHistory,
) -> dict[str, Any]:
    return {
        "version": 1,
        "token": token,
        "database": database.value(),
        "directory": str(root),
        "parent_identity": database.parent_identity,
        "root_identity": None,
        "task_run_id": run_id,
        "implementation_attempt": implementation_attempt,
        "test_attempt": number,
        "history_prefix": {
            "length": len(history),
            "sha256": task_execution_hash(history),
        },
        "checkout": None,
        "context": None,
        "scratch": None,
        "logs_identity": None,
        "expected_pair": None,
    }


def _inspect_context(
    store: TaskTestStore,
    run_id: str,
    implementation_attempt: int,
    number: int,
    lease: ExecutorLease,
    *,
    checkout_controls: bool,
) -> TaskTestExecutionPrepared:
    _check_lease(lease)
    database = _store_database(store)
    selected, started, envelope, history = _pair(
        store, run_id, implementation_attempt, number
    )
    prepared = envelope.event
    assert isinstance(prepared, TaskTestExecutionPrepared)
    if prepared_for_test(selected) != prepared:
        raise ValueError("test attempt preparation differs from its history")
    root = _canonical(Path(prepared.context.path).parent)
    if (
        root.parent != database.path.parent
        or Path(prepared.context.path) != root / "context"
        or Path(prepared.checkout.root) != root / "checkout"
    ):
        raise ValueError("test preparation is outside its owned layout")
    if (
        prepared.checkout.database_path != str(database.path)
        or prepared.checkout.database_identity != database.identity
        or prepared.checkout.database_parent_identity != database.parent_identity
    ):
        raise ValueError("test preparation belongs to a different database")
    if prepared.checkout.repository != str(lease.repository):
        raise ValueError("test preparation belongs to another repository")
    _outside(root, lease)
    root_info = _directory(root)
    _directory(root / "context", identity=prepared.context.identity, mode=0o500)
    logs_info = _directory(root / "logs")
    if set(os.listdir(root)) != {"checkout", "context", "logs", "scratch"} or set(
        os.listdir(root / "context")
    ) != {_MANIFEST}:
        raise ValueError("test preparation layout changed")
    _scratch(prepared.scratch, root)
    registry = _registry(lease)
    path = registry / f"{prepared.context.token}.json"
    marker, marker_identity = _load_marker(path, database)
    info, digest, raw = _read_file(root / "context" / _MANIFEST, mode=0o400)
    if digest != prepared.context.manifest_sha256:
        raise ValueError("test context manifest changed")
    expected = _manifest(
        database,
        started,
        prepared,
        history,
        path,
        marker_identity,
        _identity(registry.stat()),
        _identity(root_info),
        _identity(logs_info),
        _identity(info),
    )
    if raw != _json_bytes(expected):
        raise ValueError("test context manifest identity or binding changed")
    expected_marker = _marker(
        database,
        root,
        run_id,
        implementation_attempt,
        number,
        prepared.context.token,
        history,
    )
    expected_marker.update(
        root_identity=_identity(root_info),
        checkout=task_execution_value(prepared.checkout),
        context=task_execution_value(prepared.context),
        scratch=task_execution_value(prepared.scratch),
        logs_identity=_identity(logs_info),
        expected_pair={
            "started_sha256": task_execution_hash(started),
            "prepared_sha256": task_execution_hash(envelope),
        },
    )
    if task_execution_hash(marker) != task_execution_hash(expected_marker):
        raise ValueError("test ownership marker does not match the exact pair")
    if checkout_controls:
        inspect_task_test_checkout_controls(lease, prepared.checkout)
    database.check()
    _check_lease(lease)
    return prepared


def inspect_task_test_context(
    store: TaskTestStore,
    run_id: str,
    implementation_attempt: int,
    test_attempt: int,
    lease: ExecutorLease,
) -> TaskTestExecutionPrepared:
    return _inspect_context(
        store,
        run_id,
        implementation_attempt,
        test_attempt,
        lease,
        checkout_controls=True,
    )


def check_task_test_launch_owners(store: TaskTestStore, lease: ExecutorLease) -> None:
    from multi_agent.store.integration_store import check_integration_test_launch_owners

    check_integration_test_launch_owners(store, str(lease.repository))
    database = _store_database(store)
    registry = _registry(lease, create=True)
    visited: set[tuple[str, int, int]] = set()

    def settled(run_id: str, impl: int, number: int) -> None:
        state = store.load_task_test(run_id, impl)
        selected = next(
            (item for item in state.attempts if item.number == number), None
        )
        if (
            selected is None
            or selected.reconciliation is None
            or selected.reconciliation.process_stopped is not True
        ):
            raise ValueError("previous test resources have not been confirmed stopped")
        inspect_task_test_context(store, run_id, impl, number, lease)
        visited.add((run_id, impl, number))

    for path in sorted(registry.iterdir()):
        marker, _ = _load_marker(path, database)
        run_id = _text(marker.get("task_run_id"), "test registry task run id")
        impl, number = marker.get("implementation_attempt"), marker.get("test_attempt")
        if (
            type(impl) is not int
            or type(number) is not int
            or impl < 1
            or number < 1
            or marker.get("expected_pair") is None
        ):
            raise ValueError(f"test preparation remains partial or unmatched: {path}")
        settled(run_id, impl, number)
    for run_id, impl in store.list_task_test_stages():
        for attempt in store.load_task_test(run_id, impl).attempts:
            prepared = prepared_for_test(attempt)
            if (
                prepared is not None
                and prepared.checkout.repository == str(lease.repository)
                and (run_id, impl, attempt.number) not in visited
            ):
                settled(run_id, impl, attempt.number)


@dataclass(slots=True)
class _Capability:
    active: bool = True
    claimed: set[int] = field(default_factory=set)
    sealed: set[int] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class TaskTestLaunch:
    store: TaskTestStore
    lease: ExecutorLease
    test_attempt: int
    prepared: TaskTestExecutionPrepared
    log_dir: Path
    _database: _Database = field(repr=False)
    _issue: object = field(repr=False)
    _capability: _Capability = field(
        default_factory=_Capability, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if self._issue is not _ISSUED:
            raise ValueError("test launch capability cannot be reconstructed")

    def __deepcopy__(self, memo: dict[int, Any]) -> TaskTestLaunch:
        return self

    @property
    def scratch(self) -> TaskTestScratch:
        return self.prepared.scratch

    def check(self) -> None:
        if not self._capability.active:
            raise ValueError("test launch scope expired")
        self._database.check()
        value = self.prepared.checkout.input
        state = self.store.load_task_test(
            value.task_run_id, value.implementation_attempt
        )
        if (
            state.current_attempt is None
            or state.current_attempt.number != self.test_attempt
        ):
            raise ValueError("test launch is no longer current")
        actual = inspect_task_test_context(
            self.store,
            value.task_run_id,
            value.implementation_attempt,
            self.test_attempt,
            self.lease,
        )
        if task_execution_hash(actual) != task_execution_hash(self.prepared):
            raise ValueError("test launch preparation changed")
        current = prepare_task_test_input(
            self.store,
            value.task_run_id,
            value.implementation_attempt,
            self.lease,
            required_commands=tuple(command.name for command in value.commands),
            declaration_source=value.declaration_source,
        )
        if task_execution_hash(current) != task_execution_hash(value):
            raise ValueError("test launch source or original task history changed")
        _policy(self.prepared.policy)

    def claim(self, sequence: int) -> TaskTestExecutionPrepared:
        if type(sequence) is not int or sequence in self._capability.claimed:
            raise ValueError("test command launch was already consumed or is invalid")
        self.check()
        value = self.prepared.checkout.input
        state = self.store.load_task_test(
            value.task_run_id, value.implementation_attempt
        )
        current = state.current_attempt
        if (
            current is None
            or current.current_command is None
            or current.current_command.sequence != sequence
            or current.cancel_requested
            or current.finished
            or not isinstance(current.events[-1], TaskTestCommandStarted)
        ):
            raise ValueError("test command requires its exact fresh committed start")
        if sequence == 1:
            inspect_initial_task_test_checkout(self.lease, self.prepared.checkout)
        self._capability.claimed.add(sequence)
        return self.prepared

    def consume_seal(self, sequence: int) -> None:
        if (
            not self._capability.active
            or sequence not in self._capability.claimed
            or sequence in self._capability.sealed
        ):
            raise ValueError("test command sealing capability is unavailable")
        self._capability.sealed.add(sequence)


@contextmanager
def prepare_task_test_launch(
    database_path: str | Path,
    task_run_id: str,
    implementation_attempt: int,
    *,
    repository: Path,
    directory: Path,
    required_commands: tuple[str, ...],
    declaration_source: str,
    policy: TaskTestPolicy,
) -> Iterator[TaskTestLaunch]:
    _declaration(
        task_run_id, implementation_attempt, required_commands, declaration_source
    )
    _policy(policy)
    database = _database(database_path)
    with TaskTestStore(database.path) as store, executor_lock(repository) as lease:
        root = _new_root(directory, database, lease)
        check_test_runtime_paths(policy, root, database.path, lease)
        value = prepare_task_test_input(
            store,
            task_run_id,
            implementation_attempt,
            lease,
            required_commands=required_commands,
            declaration_source=declaration_source,
        )
        state, history = store.load_task_test_checkpoint(
            task_run_id, implementation_attempt
        )
        previous = (
            state.current_attempt.number
            if state is not None and state.current_attempt is not None
            else None
        )
        number = 1 if previous is None else previous + 1
        when = datetime.now(UTC).isoformat()
        started = TaskTestEnvelope(
            implementation_attempt, number, TaskTestAttemptStarted(value), when
        )
        initial = state or TaskTestState(task_run_id, implementation_attempt, value)
        # Validate the existing transition before allocating artifacts. The
        # local intermediate state is discarded; only the atomic pair reserves.
        _apply_task_test(
            initial, started, store.load_task(task_run_id), managed_reservation=True
        )
        for attempt in initial.attempts:
            previous_prepared = prepared_for_test(attempt)
            if previous_prepared is None or task_execution_hash(
                previous_prepared.policy
            ) != task_execution_hash(policy):
                raise ValueError("test retry must preserve its managed policy")
        check_task_launch_owners(store, lease)
        check_task_test_launch_owners(store, lease)
        registry = _registry(lease)
        token = uuid4().hex
        path = registry / f"{token}.json"
        marker = _marker(
            database, root, task_run_id, implementation_attempt, number, token, history
        )
        fd: int | None = None
        failure: BaseException | None = None
        launch: TaskTestLaunch | None = None
        try:
            fd = os.open(
                path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            os.fchmod(fd, 0o600)
            _write_marker(fd, path, marker)
            _sync_directory(registry)
            root.mkdir(mode=0o700)
            marker["root_identity"] = _identity(_directory(root))
            _write_marker(fd, path, marker)
            checkout = create_task_test_checkout(store, value, lease, root / "checkout")
            marker["checkout"] = task_execution_value(checkout)
            _write_marker(fd, path, marker)
            for name in ("context", "logs"):
                (root / name).mkdir(mode=0o700)
            scratch = create_test_scratch(root)
            context = TaskTestContext(
                path=str(root / "context"),
                identity=_identity(_directory(root / "context")),
                token=token,
                manifest_sha256="",
            )
            marker.update(
                scratch=task_execution_value(scratch),
                context=task_execution_value(context),
                logs_identity=_identity(_directory(root / "logs")),
            )
            _write_marker(fd, path, marker)
            prepared = TaskTestExecutionPrepared(checkout, context, policy, scratch)
            metadata = _write_control(
                root / "context" / _MANIFEST,
                lambda identity: _json_bytes(
                    _manifest(
                        database,
                        started,
                        prepared,
                        history,
                        path,
                        _identity(os.fstat(fd)),
                        _identity(registry.stat()),
                        _identity(root.stat()),
                        _identity((root / "logs").stat()),
                        identity,
                    )
                ),
            )
            prepared = replace(
                prepared, context=replace(context, manifest_sha256=metadata["sha256"])
            )
            (root / "context").chmod(0o500)
            envelope = TaskTestEnvelope(implementation_attempt, number, prepared, when)
            marker.update(
                context=task_execution_value(prepared.context),
                expected_pair={
                    "started_sha256": task_execution_hash(started),
                    "prepared_sha256": task_execution_hash(envelope),
                },
            )
            _write_marker(fd, path, marker)
            _sync_directory(root)
            current = prepare_task_test_input(
                store,
                task_run_id,
                implementation_attempt,
                lease,
                required_commands=required_commands,
                declaration_source=declaration_source,
            )
            if current != value:
                raise ValueError("test source changed during preparation")
            store.reserve_task_test_execution(
                task_run_id,
                started,
                envelope,
                expected_test_attempt=previous,
                expected_history_length=len(history),
                expected_history_hash=task_execution_hash(history),
            )
            launch = TaskTestLaunch(
                store, lease, number, prepared, root / "logs", database, _ISSUED
            )
            launch.check()
            yield launch
        except BaseException as error:
            failure = error
            error.add_note(
                f"Retained test preparation: marker {path}; root {root}. No cleanup or recovery is inferred."
            )
            raise
        finally:
            if launch is not None:
                launch._capability.active = False
            if fd is not None:
                try:
                    os.close(fd)
                except OSError as error:
                    if failure is None:
                        error.add_note(
                            f"Retained test preparation: marker {path}; root {root}. No cleanup or recovery is inferred."
                        )
                        raise
                    failure.add_note(
                        f"Closing retained test marker also failed: {error!r}"
                    )
