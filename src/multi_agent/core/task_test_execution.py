"""Immutable host preparations for test execution, never reusable launch rights."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePath
from typing import TYPE_CHECKING

from .task_events import TaskCandidateFrozen
from .task_test_input import TaskTestInput
from .tasks import NamedCommand
from .workspaces import WorkspaceControl

if TYPE_CHECKING:
    from .task_state import TaskState
    from .task_test_events import TaskTestEnvelope, TaskTestExecutionPrepared
    from .task_test_state import TaskTestAttempt, TaskTestState


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskTestCheckout:
    input: TaskTestInput
    repository: str
    database_path: str
    database_identity: tuple[int, int]
    database_parent_identity: tuple[int, int]
    root: str
    root_identity: tuple[int, int]
    git_directory: str
    git_identity: tuple[int, int]
    owner_token: str
    object_format: str
    initial_digest: str
    controls: tuple[WorkspaceControl, ...]
    objects: tuple[tuple[str, str], ...]
    observed_test_attempt: int | None
    test_history_length: int
    test_history_hash: str
    version: int = 1
    layout: str = "standalone-shallow-git-v1"

    @property
    def candidate(self) -> TaskCandidateFrozen:
        return self.input.candidate

    @property
    def commit(self) -> str:
        return self.candidate.delivery.candidate

    @property
    def tree(self) -> str:
        return self.candidate.delivery.tree

    @property
    def commands(self) -> tuple[NamedCommand, ...]:
        return self.input.commands


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskTestPolicy:
    executable: str
    executable_identity: tuple[int, int]
    executable_sha256: str
    timeout: float
    grace: float
    output_bytes_limit: int
    tool_directories: tuple[str, ...] = ()
    runtime_read_paths: tuple[str, ...] = ()
    required_stop_scope: str = "command-request-and-owned-runtime-v1"
    output_scope: str = "server-emitted-notifications-v1"
    version: int = 1


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskTestContext:
    path: str
    identity: tuple[int, int]
    token: str
    manifest_sha256: str


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskTestScratch:
    path: str
    identity: tuple[int, int]
    home_identity: tuple[int, int]
    tmp_identity: tuple[int, int]
    cache_identity: tuple[int, int]

    @property
    def home(self) -> str:
        return str(PurePath(self.path) / "home")

    @property
    def tmp(self) -> str:
        return str(PurePath(self.path) / "tmp")

    @property
    def cache(self) -> str:
        return str(PurePath(self.path) / "cache")


def prepared_for_test(attempt: TaskTestAttempt) -> TaskTestExecutionPrepared | None:
    from .task_test_events import TaskTestExecutionPrepared

    return next(
        (
            event
            for event in attempt.events
            if isinstance(event, TaskTestExecutionPrepared)
        ),
        None,
    )


def reserve_task_test_execution(
    state: TaskTestState,
    started: TaskTestEnvelope,
    prepared: TaskTestEnvelope,
    implementation: TaskState,
) -> TaskTestState:
    """Fold a managed pair locally; no caller can observe a half reservation."""
    from .state import Disposition
    from .task_test_events import TaskTestAttemptStarted, TaskTestExecutionPrepared
    from .transitions import _apply_task_test

    if not isinstance(started.event, TaskTestAttemptStarted) or not isinstance(
        prepared.event, TaskTestExecutionPrepared
    ):
        raise TypeError("test execution reservation requires Started and Prepared")
    if (started.implementation_attempt, started.test_attempt) != (
        prepared.implementation_attempt,
        prepared.test_attempt,
    ):
        raise ValueError("test execution reservation must identify one attempt")
    if any(prepared_for_test(attempt) is None for attempt in state.attempts):
        raise ValueError("legacy test stages cannot become managed")
    updated, start_disposition = _apply_task_test(
        state, started, implementation, managed_reservation=True
    )
    reserved, prepare_disposition = _apply_task_test(
        updated, prepared, implementation, managed_reservation=True
    )
    if (
        start_disposition != Disposition.APPLIED
        or prepare_disposition != Disposition.APPLIED
    ):
        raise ValueError("test execution reservation must apply both events")
    return reserved
