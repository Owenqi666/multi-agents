"""Validated host lifecycle inputs and normalized runtime events for a task."""

from dataclasses import dataclass
from typing import ClassVar

from .dispatch import DispatchDecision
from .events import AdapterEvent, CancelRequested, Error, JSONValue, freeze_json


@dataclass(frozen=True, slots=True)
class TaskAttemptStarted:
    kind: ClassVar[str] = "task_attempt_started"
    decision: DispatchDecision
    worktree: str
    prompt_template_version: str
    started_at: str
    environment: JSONValue = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "environment", freeze_json(self.environment))


@dataclass(frozen=True, slots=True)
class TaskReconciled:
    kind: ClassVar[str] = "task_reconciled"
    worktree_digest: str
    process_stopped: bool
    detail: str


@dataclass(frozen=True, slots=True)
class TaskScopeBlocked:
    kind: ClassVar[str] = "task_scope_blocked"
    reason: str
    required_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TaskDiagnostic:
    """A boundary-classified diagnostic, explicitly NOT a runtime terminal.

    The caller must distinguish incomplete-cleanup diagnostics from actual
    terminal Error events, as the existing review runner already does.
    """

    kind: ClassVar[str] = "task_diagnostic"
    diagnostic: Error


type TaskEvent = (
    TaskAttemptStarted
    | TaskReconciled
    | TaskScopeBlocked
    | TaskDiagnostic
    | CancelRequested
    | AdapterEvent
)


@dataclass(frozen=True, slots=True)
class TaskEnvelope:
    attempt: int
    event: TaskEvent
    occurred_at: str | None = None
