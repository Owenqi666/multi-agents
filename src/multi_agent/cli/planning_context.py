"""Freeze and inspect owned planning inputs before any runtime can be launched."""

import hashlib
import math
import os
import re
import sqlite3
import stat
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from multi_agent.core import Disposition, transitions
from multi_agent.core.events import thaw_json
from multi_agent.core.planning import (
    PlanningAttempt,
    PlanningInput,
    PlanningState,
    planning_hash,
    planning_json,
    planning_value,
)
from multi_agent.core.planning_events import (
    PlanningAttemptStarted,
    PlanningDraftValidated,
    PlanningEnvelope,
    PlanningExecutionPrepared,
)
from multi_agent.core.planning_execution import (
    PlanningAttemptContext,
    PlanningExecutionPolicy,
    PlanningProviderPolicy,
    PlanningRunOwner,
    prepared_for,
)
from multi_agent.core.snapshot import FrozenSnapshot
from multi_agent.core.tasks import TaskCard
from multi_agent.store.planning_store import PlanningStore

from .planning_feedback import feedback_for_prefix
from .planning_inputs import validate_planning_input
from .planning_prompt import (
    planning_output_schema,
    planning_prompt,
    planning_prompt_version,
)
from .snapshot import _git, _string, freeze_repository, materialize
from .task_inputs import freeze_project_rules

_PROVIDERS = ("claude", "codex")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN = re.compile(r"[0-9a-f]{32}\Z")
_OWNER_FILE = ".planning-owner.json"
_CONTEXT_FILE = ".planning-context.json"
_CONTEXT_ACCEPTANCE = ("Plan from the frozen baseline without executing project code.",)
_NOFOLLOW = os.O_NOFOLLOW | os.O_NONBLOCK
type PlanningHistory = tuple[tuple[PlanningEnvelope, Disposition], ...]


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


def _canonical(path: Path, *, existing: bool = True) -> Path:
    if not isinstance(path, Path):
        raise TypeError("planning filesystem paths must be Path values")
    absolute = path.absolute()
    try:
        resolved = path.resolve(strict=existing)
    except OSError as error:
        raise ValueError("planning path cannot be resolved") from error
    if absolute != resolved:
        raise ValueError(
            "planning path must be canonical and contain no symlink aliases"
        )
    return absolute


def _directory(
    path: Path, *, identity: tuple[int, int] | None = None, mode: int | None = None
) -> os.stat_result:
    _canonical(path)
    try:
        info = path.lstat()
    except OSError as error:
        raise ValueError("planning directory cannot be inspected") from error
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("planning directory must not be a symlink or special file")
    if identity is not None and _identity(info) != identity:
        raise ValueError("planning directory identity changed")
    if mode is not None and stat.S_IMODE(info.st_mode) != mode:
        raise ValueError("planning directory mode changed")
    return info


def _read_fd(fd: int, *, keep: bool = False) -> tuple[os.stat_result, str, bytes]:
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("planning file must be regular")
    digest = hashlib.sha256()
    chunks = []
    total = 0
    while block := os.read(fd, 1024 * 1024):
        digest.update(block)
        if keep:
            total += len(block)
            if total > 2 * 1024 * 1024:
                raise ValueError("planning manifest exceeds its byte limit")
            chunks.append(block)
    if _stable(before) != _stable(os.fstat(fd)):
        raise ValueError("planning file changed while being read")
    return before, digest.hexdigest(), b"".join(chunks)


def _read_file(path: Path, *, keep: bool = False) -> tuple[os.stat_result, str, bytes]:
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
        try:
            result = _read_fd(fd, keep=keep)
            if _stable(path.lstat()) != _stable(result[0]):
                raise ValueError("planning file identity changed while being read")
            return result
        finally:
            os.close(fd)
    except OSError as error:
        raise ValueError("planning file cannot be safely read") from error


def _write(path: Path, value: object) -> str:
    raw = (planning_json(value) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
        os.fchmod(stream.fileno(), 0o400)
    return hashlib.sha256(raw).hexdigest()


def _positive(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return float(value)


def _capture_executable(
    provider: str, executable: Path, limit: int
) -> PlanningProviderPolicy:
    if type(limit) is not int or limit < 1:
        raise ValueError("max_attempts must be a positive integer")
    try:
        path = executable.resolve(strict=True)
    except OSError as error:
        raise ValueError("configured executable cannot be resolved") from error
    info, digest, _ = _read_file(path)
    if not info.st_mode & 0o111:
        raise ValueError("configured CLI file must be executable")
    return PlanningProviderPolicy(provider, str(path), _identity(info), digest, limit)


def capture_planning_policy(
    executables: Mapping[str, str | Path],
    max_attempts: Mapping[str, int],
    timeout: float,
    grace: float,
) -> PlanningExecutionPolicy:
    """Read executable identity and bytes; never execute a version or login check."""
    if set(executables) != set(_PROVIDERS) or set(max_attempts) != set(_PROVIDERS):
        raise ValueError("policy must configure exactly claude and codex")
    return PlanningExecutionPolicy(
        tuple(
            _capture_executable(
                provider, Path(executables[provider]), max_attempts[provider]
            )
            for provider in _PROVIDERS
        ),
        _positive(timeout, "timeout"),
        _positive(grace, "grace"),
    )


def _validate_policy(policy: PlanningExecutionPolicy) -> None:
    if not isinstance(policy, PlanningExecutionPolicy):
        raise TypeError("policy must be a PlanningExecutionPolicy")
    _positive(policy.timeout, "timeout")
    _positive(policy.grace, "grace")
    if not isinstance(policy.providers, tuple):
        raise TypeError("policy providers must be an immutable tuple")
    if (
        len(policy.providers) != 2
        or any(
            not isinstance(item, PlanningProviderPolicy) for item in policy.providers
        )
        or {item.provider for item in policy.providers} != set(_PROVIDERS)
    ):
        raise ValueError("policy must configure exactly claude and codex")
    for item in policy.providers:
        path = _canonical(Path(_string(item.executable, "executable", nonempty=True)))
        if not Path(item.executable).is_absolute():
            raise ValueError("executable path must be absolute")
        captured = _capture_executable(item.provider, path, item.max_attempts)
        if captured != item:
            raise ValueError("configured executable identity or bytes changed")


def _tree_digest(root: Path) -> str:
    """Traverse by directory descriptors, refusing links and special files."""
    entries: list[tuple[Any, ...]] = []

    def visit(fd: int, relative: str) -> None:
        before = os.fstat(fd)
        entries.append(
            (relative, "directory", stat.S_IMODE(before.st_mode), *_identity(before))
        )
        with os.scandir(fd) as iterator:
            names = sorted(entry.name for entry in iterator)
        for name in names:
            location = f"{relative}/{name}" if relative != "." else name
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW, dir_fd=fd
                )
                try:
                    if _identity(os.fstat(child)) != _identity(info):
                        raise ValueError("planning child directory identity changed")
                    visit(child, location)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                child = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=fd)
                try:
                    observed, digest, _ = _read_fd(child)
                finally:
                    os.close(child)
                if _stable(observed) != _stable(info):
                    raise ValueError("planning child file changed")
                entries.append(
                    (
                        location,
                        "file",
                        stat.S_IMODE(info.st_mode),
                        *_identity(info),
                        info.st_size,
                        digest,
                    )
                )
            else:
                raise ValueError("planning context contains a symlink or special file")
        if _stable(before) != _stable(os.fstat(fd)):
            raise ValueError("planning directory changed while being inspected")

    identity = _identity(_directory(root))
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW)
    try:
        if _identity(os.fstat(fd)) != identity:
            raise ValueError("planning context identity changed")
        visit(fd, ".")
    finally:
        os.close(fd)
    _directory(root, identity=identity)
    return planning_hash(entries)


def _managed(
    state: PlanningState,
) -> tuple[tuple[str, PlanningAttempt, PlanningExecutionPrepared], ...]:
    records = []
    for stage in ("draft", "review"):
        for attempt in state.attempts(stage):
            prepared = prepared_for(attempt)
            if prepared is None:
                raise ValueError(
                    "legacy planning attempts have no complete Prepared chain"
                )
            records.append((stage, attempt, prepared))
    return tuple(records)


def _snapshot(
    store: PlanningStore, identifier: str, value: PlanningInput
) -> FrozenSnapshot:
    snapshot = FrozenSnapshot.from_dict(
        cast(dict[str, Any], thaw_json(store.load_snapshot(identifier)))
    )
    rules = value.frozen_rules
    if (
        snapshot.snapshot_id != identifier
        or snapshot.repository != rules.repository
        or snapshot.baseline != rules.baseline_commit
        or snapshot.candidate != rules.baseline_commit
        or snapshot.requirements != value.requirements
        or snapshot.diff
        or snapshot.tests
        or snapshot.required_tests
        or snapshot.acceptance != _CONTEXT_ACCEPTANCE
    ):
        raise ValueError("planning source snapshot does not match frozen input")
    return snapshot


def _owner_value(
    run_id: str, value: PlanningInput, prepared: PlanningExecutionPrepared
) -> dict[str, Any]:
    owner = prepared.run_owner
    return {
        "version": 1,
        "run_id": run_id,
        "input_hash": value.input_hash,
        "source_snapshot_id": prepared.source_snapshot_id,
        "policy_hash": planning_hash(prepared.policy),
        "root": owner.root,
        "identity": list(owner.identity),
        "token": owner.token,
    }


def _owner(
    store: PlanningStore,
    run_id: str,
    state: PlanningState,
    prepared: PlanningExecutionPrepared,
) -> Path:
    owner = prepared.run_owner
    root = Path(owner.root)
    _directory(root, identity=owner.identity, mode=0o700)
    if not _TOKEN.fullmatch(owner.token) or not _SHA256.fullmatch(
        owner.manifest_sha256
    ):
        raise ValueError("invalid planning owner identity")
    info, digest, raw = _read_file(root / _OWNER_FILE, keep=True)
    if stat.S_IMODE(info.st_mode) != 0o400 or digest != owner.manifest_sha256:
        raise ValueError("planning owner manifest changed")
    expected = (
        planning_json(_owner_value(run_id, state.planning_input, prepared)) + "\n"
    ).encode("utf-8")
    if raw != expected:
        raise ValueError("planning owner manifest binding differs")
    if {entry.name for entry in root.iterdir()} != {_OWNER_FILE, "contexts", "logs"}:
        raise ValueError("planning root contains unexpected entries")
    _directory(root / "contexts", mode=0o700)
    _directory(root / "logs", mode=0o700)
    _validate_policy(prepared.policy)
    _snapshot(store, prepared.source_snapshot_id, state.planning_input)
    return root


def _review_card(
    state: PlanningState, started: PlanningAttemptStarted
) -> TaskCard | None:
    if started.draft_attempt is None:
        if started.task_hash is not None:
            raise ValueError("draft context has an unexpected task hash")
        return None
    draft = next(
        (item for item in state.draft_attempts if item.number == started.draft_attempt),
        None,
    )
    if draft is None:
        raise ValueError("review context references an absent draft")
    card = next(
        (
            event.card
            for event in draft.events
            if isinstance(event, PlanningDraftValidated)
        ),
        None,
    )
    if card is None or card.task_hash != started.task_hash:
        raise ValueError("review context does not bind the exact validated card")
    return card


def _feedback_bytes(feedback: dict[str, Any]) -> bytes:
    return (planning_json(feedback) + "\n").encode("utf-8")


def _history_before(
    store: PlanningStore,
    run_id: str,
    stage: str,
    attempt: PlanningAttempt,
) -> PlanningHistory:
    history = store.load_planning_history(run_id)
    matches = [
        index
        for index, (envelope, disposition) in enumerate(history)
        if disposition == Disposition.APPLIED
        and envelope.stage == stage
        and envelope.attempt == attempt.number
        and isinstance(envelope.event, PlanningAttemptStarted)
        and planning_hash(envelope.event) == planning_hash(attempt.request)
    ]
    if len(matches) != 1:
        raise ValueError("planning context has no unique applied Started history")
    return history[: matches[0]]


def _context_value(
    run_id: str,
    state: PlanningState,
    stage: str,
    number: int,
    started: PlanningAttemptStarted,
    prepared: PlanningExecutionPrepared,
    feedback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    context = prepared.attempt_context
    version = started.prompt_template_version
    if version not in (f"planning-{stage}-v1", f"planning-{stage}-v2"):
        raise ValueError("unsupported frozen planning prompt version")
    if (version.endswith("-v2")) != (feedback is not None):
        raise ValueError("planning feedback does not match the template version")
    value = {
        "version": 1 if feedback is None else 2,
        "run_id": run_id,
        "stage": stage,
        "attempt": number,
        "input_hash": state.planning_input.input_hash,
        "source_snapshot_id": prepared.source_snapshot_id,
        "policy_hash": planning_hash(prepared.policy),
        "run_owner_token": prepared.run_owner.token,
        "path": context.path,
        "identity": list(context.identity),
        "token": context.token,
        "prompt_sha256": hashlib.sha256(started.prompt.encode("utf-8")).hexdigest(),
        "prompt_template_version": version,
        "draft_attempt": started.draft_attempt,
        "task_hash": started.task_hash,
    }
    if feedback is not None:
        value.update(
            history_prefix_length=feedback["history_prefix"]["length"],
            history_prefix_hash=feedback["history_prefix"]["sha256"],
            feedback_sha256=hashlib.sha256(_feedback_bytes(feedback)).hexdigest(),
        )
    return value


def _inspect(
    store: PlanningStore,
    run_id: str,
    state: PlanningState,
    stage: str,
    attempt: PlanningAttempt,
    prepared: PlanningExecutionPrepared,
    *,
    prospective_history: PlanningHistory | None = None,
) -> None:
    root = _owner(store, run_id, state, prepared)
    context = prepared.attempt_context
    path = Path(context.path)
    if path.parent != root / "contexts" or not _TOKEN.fullmatch(context.token):
        raise ValueError("planning context is outside its owner")
    _directory(path, identity=context.identity, mode=0o500)
    info, manifest_hash, raw = _read_file(path / _CONTEXT_FILE, keep=True)
    if stat.S_IMODE(info.st_mode) != 0o400 or manifest_hash != context.manifest_sha256:
        raise ValueError("planning context manifest changed")
    feedback = None
    feedback_hash = None
    if attempt.request.prompt_template_version == f"planning-{stage}-v2":
        prefix = (
            _history_before(store, run_id, stage, attempt)
            if prospective_history is None
            else prospective_history
        )
        feedback = feedback_for_prefix(prefix, state.planning_input)
        expected_feedback = _feedback_bytes(feedback)
        feedback_info, feedback_hash, _ = _read_file(path / "revision-feedback.json")
        if (
            stat.S_IMODE(feedback_info.st_mode) != 0o400
            or feedback_info.st_size != len(expected_feedback)
            or feedback_hash != hashlib.sha256(expected_feedback).hexdigest()
        ):
            raise ValueError("planning feedback differs from its frozen history")
    expected = _context_value(
        run_id, state, stage, attempt.number, attempt.request, prepared, feedback
    )
    if raw != (planning_json(expected) + "\n").encode("utf-8"):
        raise ValueError("planning context manifest binding differs")
    card = _review_card(state, attempt.request)
    prompt = planning_prompt(
        stage,
        state.planning_input,
        prepared.source_snapshot_id,
        draft_attempt=attempt.request.draft_attempt,
        card=card,
        template_version=attempt.request.prompt_template_version,
        feedback_sha256=feedback_hash,
    )
    if prompt != attempt.request.prompt:
        raise ValueError("planning prompt differs from its frozen context")
    if _tree_digest(path) != context.digest:
        raise ValueError("planning context content, mode or identity changed")


def inspect_planning_context(
    store: PlanningStore, run_id: str, stage: str, attempt: int
) -> PlanningExecutionPrepared:
    """Check stored ownership and files; never infer stopping or restart a process."""
    planning_prompt_version(stage)
    state = store.load_planning(run_id)
    records = _managed(state)
    selected = next(
        (item for item in records if item[0] == stage and item[1].number == attempt),
        None,
    )
    if selected is None:
        raise ValueError("planning attempt has no prepared context")
    try:
        _inspect(store, run_id, state, *selected)
    except OSError as error:
        raise ValueError("planning context cannot be safely inspected") from error
    return selected[2]


def _source(
    store: PlanningStore, state: PlanningState, repository: Path, directory: Path
) -> FrozenSnapshot:
    repository = repository.resolve(strict=True)
    rules = state.planning_input.frozen_rules
    if freeze_project_rules(repository, rules.baseline_commit) != rules:
        raise ValueError("source repository or baseline rules differ from frozen input")
    worktree = Path(
        _git(repository, "rev-parse", "--show-toplevel")
        .decode("utf-8")
        .removesuffix("\n")
    ).resolve()
    admin = Path(
        _git(repository, "rev-parse", "--absolute-git-dir")
        .decode("utf-8")
        .removesuffix("\n")
    ).resolve()
    common = Path(rules.repository)
    forbidden = [worktree, common, admin]
    if common.name == ".git":
        forbidden.append(common.parent)
    forbidden_identities = {_identity(path.stat()) for path in forbidden}
    if any(directory.is_relative_to(path) for path in forbidden) or any(
        _identity(parent.stat()) in forbidden_identities for parent in directory.parents
    ):
        raise ValueError(
            "planning root must be outside source worktrees and Git metadata"
        )
    snapshot = freeze_repository(
        repository,
        rules.baseline_commit,
        rules.baseline_commit,
        state.planning_input.requirements,
        _CONTEXT_ACCEPTANCE,
    )
    store.save_snapshot(snapshot.snapshot_id, snapshot.to_dict())
    return _snapshot(store, snapshot.snapshot_id, state.planning_input)


def _prepare_planning_attempt(
    store: PlanningStore,
    run_id: str,
    stage: str,
    *,
    repository: Path | None = None,
    directory: Path | None = None,
    policy: PlanningExecutionPolicy | None = None,
) -> PlanningExecutionPrepared:
    """Create a fresh context and atomically reserve it before any runtime I/O."""
    template_version = planning_prompt_version(stage)
    state = store.load_planning(run_id)
    history_prefix = store.load_planning_history(run_id)
    validate_planning_input(state.planning_input)
    records = _managed(state)
    if records:
        first = records[0][2]
        if repository is not None:
            raise ValueError("prepared planning runs do not reread a repository")
        if directory is not None and _canonical(directory) != Path(
            first.run_owner.root
        ):
            raise ValueError("planning run root cannot change")
        if policy is not None and planning_hash(policy) != planning_hash(first.policy):
            raise ValueError("planning execution policy cannot change")
        for record in records:
            _inspect(store, run_id, state, *record)
        root = Path(first.run_owner.root)
        policy = first.policy
        snapshot = _snapshot(store, first.source_snapshot_id, state.planning_input)
        owner = first.run_owner
    else:
        if repository is None or directory is None or policy is None:
            raise ValueError(
                "first preparation requires repository, new directory and policy"
            )
        _validate_policy(policy)
        root = _canonical(directory, existing=False)
        if root.exists() or root.is_symlink():
            raise ValueError(
                "first planning root must be new; residual roots are not adopted"
            )
        _directory(root.parent)
        snapshot = _source(store, state, repository, root)
        owner = None
    current = state.current(stage)
    number = (current.number if current is not None else 0) + 1
    card = state.card if stage == "review" else None
    draft = state.current("draft") if stage == "review" else None
    feedback = (
        feedback_for_prefix(history_prefix, state.planning_input)
        if template_version == f"planning-{stage}-v2"
        else None
    )
    feedback_hash = (
        hashlib.sha256(_feedback_bytes(feedback)).hexdigest()
        if feedback is not None
        else None
    )
    started = PlanningAttemptStarted(
        getattr(state.planning_input, stage + "_request"),
        planning_prompt(
            stage,
            state.planning_input,
            snapshot.snapshot_id,
            draft_attempt=draft.number if draft is not None else None,
            card=card,
            template_version=template_version,
            feedback_sha256=feedback_hash,
        ),
        template_version,
        datetime.now(UTC).isoformat(),
        draft_attempt=draft.number if draft is not None else None,
        task_hash=card.task_hash if card is not None else None,
    )
    envelope = PlanningEnvelope(stage, number, started, started.started_at)
    _, disposition = transitions.apply_planning(state, envelope)
    if disposition != Disposition.APPLIED:
        raise ValueError("planning reservation is not a fresh attempt")
    if number > policy.for_provider(started.request.provider).max_attempts:
        raise ValueError("planning attempt budget exhausted")
    try:
        if owner is None:
            root.mkdir(mode=0o700)
            (root / "contexts").mkdir(mode=0o700)
            (root / "logs").mkdir(mode=0o700)
            owner = PlanningRunOwner(str(root), _identity(root.stat()), uuid4().hex, "")
            placeholder = PlanningExecutionPrepared(
                snapshot.snapshot_id,
                policy,
                owner,
                PlanningAttemptContext("", (0, 0), "", "", ""),
            )
            owner_hash = _write(
                root / _OWNER_FILE,
                _owner_value(run_id, state.planning_input, placeholder),
            )
            owner = PlanningRunOwner(
                owner.root, owner.identity, owner.token, owner_hash
            )
        token = uuid4().hex
        path = root / "contexts" / f"{stage}-{number}-{token}"
        path.mkdir(mode=0o700)
        materialize(snapshot, path / "source")
        _write(path / "planning-input.json", planning_value(state.planning_input))
        _write(path / "output-schema.json", planning_output_schema(stage))
        if card is not None:
            _write(path / "task-card.json", card.to_dict())
        if feedback is not None:
            _write(path / "revision-feedback.json", feedback)
        context = PlanningAttemptContext(
            str(path), _identity(path.stat()), token, "", ""
        )
        prepared = PlanningExecutionPrepared(
            snapshot.snapshot_id, policy, owner, context
        )
        manifest_hash = _write(
            path / _CONTEXT_FILE,
            _context_value(run_id, state, stage, number, started, prepared, feedback),
        )
        path.chmod(0o500)
        context = PlanningAttemptContext(
            context.path, context.identity, token, _tree_digest(path), manifest_hash
        )
        prepared = PlanningExecutionPrepared(
            snapshot.snapshot_id, policy, owner, context
        )
        _inspect(
            store,
            run_id,
            state,
            stage,
            _new_attempt(started, number),
            prepared,
            prospective_history=history_prefix,
        )
        store.reserve_planning_attempt(
            run_id,
            envelope,
            PlanningEnvelope(stage, number, prepared, started.started_at),
            expected_history_length=len(history_prefix)
            if feedback is not None
            else None,
            expected_history_hash=planning_hash(history_prefix)
            if feedback is not None
            else None,
        )
        return prepared
    except (OSError, ValueError, TypeError, sqlite3.Error) as error:
        raise ValueError(
            f"planning preparation failed; owned files retained at {root}"
        ) from error


def _new_attempt(started: PlanningAttemptStarted, number: int) -> PlanningAttempt:
    from multi_agent.core.state import AttemptState, AttemptStatus

    return PlanningAttempt(started, AttemptState(number, AttemptStatus.RUNNING))


def prepare_planning_attempt(
    store: PlanningStore,
    run_id: str,
    stage: str,
    *,
    repository: Path | None = None,
    directory: Path | None = None,
    policy: PlanningExecutionPolicy | None = None,
) -> PlanningExecutionPrepared:
    """Freeze and reserve one attempt; filesystem failures stay at this boundary."""
    try:
        return _prepare_planning_attempt(
            store,
            run_id,
            stage,
            repository=repository,
            directory=directory,
            policy=policy,
        )
    except OSError as error:
        raise ValueError("planning files cannot be safely prepared") from error
