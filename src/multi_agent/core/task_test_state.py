"""Immutable test facts; phases are derived by the shared transition module."""

from dataclasses import dataclass
from enum import StrEnum

from .task_test_events import TaskTestEvent, TaskTestProblem, TaskTestReconciled
from .task_test_input import TaskTestInput


class TaskTestPhase(StrEnum):
    READY = "ready"
    RUNNING = "running"
    CANCELLING = "cancelling"
    AWAITING_EVIDENCE = "awaiting_evidence"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class TaskTestCommandState:
    sequence: int
    exit_code: int | None = None
    reconciliation: TaskTestReconciled | None = None
    problems: tuple[TaskTestProblem, ...] = ()


@dataclass(frozen=True, slots=True)
class TaskTestAttempt:
    number: int
    input: TaskTestInput
    commands: tuple[TaskTestCommandState, ...] = ()
    problems: tuple[TaskTestProblem, ...] = ()
    cancel_requested: bool = False
    finished: bool = False
    reconciliation: TaskTestReconciled | None = None
    events: tuple[TaskTestEvent, ...] = ()

    @property
    def current_command(self) -> TaskTestCommandState | None:
        return self.commands[-1] if self.commands else None


@dataclass(frozen=True, slots=True)
class TaskTestState:
    task_run_id: str
    implementation_attempt: int
    reference_input: TaskTestInput
    phase: TaskTestPhase = TaskTestPhase.READY
    attempts: tuple[TaskTestAttempt, ...] = ()

    @property
    def current_attempt(self) -> TaskTestAttempt | None:
        return self.attempts[-1] if self.attempts else None

    @property
    def current_command(self) -> TaskTestCommandState | None:
        current = self.current_attempt
        return current.current_command if current is not None else None
