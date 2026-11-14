"""The immutable state of one review round."""

from dataclasses import dataclass
from enum import StrEnum

from .events import FailureCategory, JSONValue, freeze_json


class AttemptStatus(StrEnum):
    RUNNING = "running"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RoundStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    READY_FOR_ASSESSMENT = "ready_for_assessment"
    INCOMPLETE = "incomplete"


class Disposition(StrEnum):
    APPLIED = "applied"
    HISTORY_ONLY = "history_only"


@dataclass(frozen=True, slots=True)
class EvidenceSource:
    event_type: str
    raw_type: str | None


@dataclass(frozen=True, slots=True)
class AttemptState:
    number: int
    status: AttemptStatus
    failure_category: FailureCategory | None = None
    detail: str | None = None
    retry_after: str | None = None
    session_id: str | None = None
    reported_model: str | None = None
    reported_model_source: EvidenceSource | None = None
    reported_effort: str | None = None
    reported_effort_source: EvidenceSource | None = None
    result: JSONValue = None
    exit_code: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "result", freeze_json(self.result))


@dataclass(frozen=True, slots=True)
class SeatConfig:
    key: str
    provider: str
    model: str
    effort: str


@dataclass(frozen=True, slots=True)
class SeatState:
    config: SeatConfig
    attempts: tuple[AttemptState, ...] = ()

    @property
    def current_attempt(self) -> int | None:
        return self.attempts[-1].number if self.attempts else None


@dataclass(frozen=True, slots=True)
class RoundState:
    snapshot_id: str
    prompt_template_version: str
    seats: tuple[SeatState, ...]

    def __post_init__(self) -> None:
        if len(self.seats) not in (1, 2):
            raise ValueError("a review round must have one or two seats")
        if len({seat.config.key for seat in self.seats}) != len(self.seats):
            raise ValueError("seat keys must be unique")


def round_status(state: RoundState) -> RoundStatus:
    current = tuple(
        seat.attempts[-1].status if seat.attempts else None for seat in state.seats
    )
    if any(
        status in (None, AttemptStatus.RUNNING, AttemptStatus.CANCELLING)
        for status in current
    ):
        return RoundStatus.IN_PROGRESS
    if all(status == AttemptStatus.SUCCEEDED for status in current):
        return RoundStatus.READY_FOR_ASSESSMENT
    return RoundStatus.INCOMPLETE
