"""Trusted immutable planning launch records; no filesystem or runtime access."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .planning import PlanningAttempt, PlanningState
    from .planning_events import PlanningExecutionPrepared


@dataclass(frozen=True, slots=True)
class PlanningProviderPolicy:
    provider: str
    executable: str
    executable_identity: tuple[int, int]
    executable_sha256: str
    max_attempts: int


@dataclass(frozen=True, slots=True)
class PlanningExecutionPolicy:
    providers: tuple[PlanningProviderPolicy, ...]
    timeout: float
    grace: float

    def for_provider(self, provider: str) -> PlanningProviderPolicy:
        return next(item for item in self.providers if item.provider == provider)


@dataclass(frozen=True, slots=True)
class PlanningRunOwner:
    root: str
    identity: tuple[int, int]
    token: str
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class PlanningAttemptContext:
    path: str
    identity: tuple[int, int]
    token: str
    digest: str
    manifest_sha256: str


def prepared_for(attempt: PlanningAttempt) -> PlanningExecutionPrepared | None:
    from .planning_events import PlanningExecutionPrepared

    return next(
        (
            event
            for event in attempt.events
            if isinstance(event, PlanningExecutionPrepared)
        ),
        None,
    )


def prepared_attempts(state: PlanningState) -> tuple[PlanningExecutionPrepared, ...]:
    return tuple(
        record
        for stage in ("draft", "review")
        for attempt in state.attempts(stage)
        if (record := prepared_for(attempt)) is not None
    )
