"""Validated host planning events plus the shared normalized runtime events."""

from dataclasses import dataclass
from typing import ClassVar

from .events import AdapterEvent, CancelRequested, Error, JSONValue, freeze_json
from .planning import PlanningRequest, PlanningReview
from .tasks import TaskCard


@dataclass(frozen=True, slots=True)
class PlanningAttemptStarted:
    kind: ClassVar[str] = "planning_attempt_started"
    request: PlanningRequest
    prompt: str
    prompt_template_version: str
    started_at: str
    environment: JSONValue = None
    draft_attempt: int | None = None
    task_hash: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "environment", freeze_json(self.environment))


@dataclass(frozen=True, slots=True)
class PlanningDraftValidated:
    kind: ClassVar[str] = "planning_draft_validated"
    card: TaskCard


@dataclass(frozen=True, slots=True)
class PlanningReviewValidated:
    kind: ClassVar[str] = "planning_review_validated"
    review: PlanningReview


@dataclass(frozen=True, slots=True)
class PlanningValidationRejected:
    kind: ClassVar[str] = "planning_validation_rejected"
    detail: str


@dataclass(frozen=True, slots=True)
class PlanningReconciled:
    kind: ClassVar[str] = "planning_reconciled"
    process_stopped: bool
    detail: str
    log_path: str | None = None
    log_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class PlanningDiagnostic:
    kind: ClassVar[str] = "planning_diagnostic"
    diagnostic: Error


@dataclass(frozen=True, slots=True)
class PlanningTaskBound:
    kind: ClassVar[str] = "planning_task_bound"
    task_run_id: str


type PlanningEvent = (
    PlanningAttemptStarted
    | PlanningDraftValidated
    | PlanningReviewValidated
    | PlanningValidationRejected
    | PlanningReconciled
    | PlanningDiagnostic
    | PlanningTaskBound
    | CancelRequested
    | AdapterEvent
)


@dataclass(frozen=True, slots=True)
class PlanningEnvelope:
    stage: str
    attempt: int
    event: PlanningEvent
    occurred_at: str | None = None
