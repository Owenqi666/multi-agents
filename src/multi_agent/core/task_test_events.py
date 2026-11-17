"""Trusted controller requests and observations for independent task tests."""

from dataclasses import dataclass
from typing import ClassVar

from .events import FailureCategory, JSONValue, freeze_json
from .task_test_execution import (
    TaskTestCheckout,
    TaskTestContext,
    TaskTestPolicy,
    TaskTestScratch,
)
from .task_test_input import TaskTestInput


@dataclass(frozen=True, slots=True)
class TaskTestAttemptStarted:
    kind: ClassVar[str] = "task_test_attempt_started"
    input: TaskTestInput


@dataclass(frozen=True, slots=True)
class TaskTestExecutionPrepared:
    kind: ClassVar[str] = "task_test_execution_prepared"
    checkout: TaskTestCheckout
    context: TaskTestContext
    policy: TaskTestPolicy
    scratch: TaskTestScratch


@dataclass(frozen=True, slots=True)
class TaskTestCommandReported:
    kind: ClassVar[str] = "task_test_command_reported"
    sequence: int
    reported_exit_code: int
    response: JSONValue

    def __post_init__(self) -> None:
        object.__setattr__(self, "response", freeze_json(self.response))


@dataclass(frozen=True, slots=True)
class TaskTestCommandStarted:
    kind: ClassVar[str] = "task_test_command_started"
    sequence: int


@dataclass(frozen=True, slots=True)
class TaskTestCommandExited:
    kind: ClassVar[str] = "task_test_command_exited"
    sequence: int
    exit_code: int


@dataclass(frozen=True, slots=True)
class TaskTestProblem:
    kind: ClassVar[str] = "task_test_problem"
    sequence: int | None
    category: FailureCategory | None
    detail: str


@dataclass(frozen=True, slots=True)
class TaskTestCancelRequested:
    kind: ClassVar[str] = "task_test_cancel_requested"


@dataclass(frozen=True, slots=True)
class TaskTestReconciled:
    kind: ClassVar[str] = "task_test_reconciled"
    sequence: int | None
    process_stopped: bool | None
    detail: str
    receipt_path: str | None = None
    receipt_sha256: str | None = None
    evidence_qualified: bool = False


@dataclass(frozen=True, slots=True)
class TaskTestAttemptFinished:
    kind: ClassVar[str] = "task_test_attempt_finished"


type TaskTestEvent = (
    TaskTestAttemptStarted
    | TaskTestExecutionPrepared
    | TaskTestCommandReported
    | TaskTestCommandStarted
    | TaskTestCommandExited
    | TaskTestProblem
    | TaskTestCancelRequested
    | TaskTestReconciled
    | TaskTestAttemptFinished
)


@dataclass(frozen=True, slots=True)
class TaskTestEnvelope:
    implementation_attempt: int
    test_attempt: int
    event: TaskTestEvent
    occurred_at: str | None = None
