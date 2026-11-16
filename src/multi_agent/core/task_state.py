"""Immutable implementation-stage state, separate from review-round state."""

from dataclasses import dataclass
from enum import StrEnum

from .state import AttemptState, AttemptStatus
from .task_events import (
    TaskAttemptStarted,
    TaskCandidateFrozen,
    TaskEvent,
    TaskReconciled,
    TaskScopeBlocked,
)
from .task_execution import prepared_for_task
from .tasks import FrozenProjectRules, TaskCard


class TaskPhase(StrEnum):
    READY = "ready"
    IMPLEMENTING = "implementing"
    CANCELLING = "cancelling"
    AWAITING_DELIVERY = "awaiting_delivery"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PLANNING_REQUIRED = "planning_required"


@dataclass(frozen=True, slots=True)
class TaskAttempt:
    request: TaskAttemptStarted
    runtime: AttemptState
    reconciliation: TaskReconciled | None = None
    events: tuple[TaskEvent, ...] = ()

    @property
    def number(self) -> int:
        return self.runtime.number


@dataclass(frozen=True, slots=True)
class TaskState:
    task_id: str
    task_hash: str
    rules_hash: str
    baseline_commit: str
    phase: TaskPhase = TaskPhase.READY
    attempts: tuple[TaskAttempt, ...] = ()
    scope_block: TaskScopeBlocked | None = None

    @property
    def current_attempt(self) -> TaskAttempt | None:
        return self.attempts[-1] if self.attempts else None

    @property
    def current_candidate(self) -> TaskCandidateFrozen | None:
        current = self.current_attempt
        if current is None:
            return None
        return next(
            (
                event
                for event in current.events
                if isinstance(event, TaskCandidateFrozen)
            ),
            None,
        )

    @property
    def candidate_ready(self) -> bool:
        """A fixed candidate still bound to the latest qualified execution facts."""
        current = self.current_attempt
        candidate = self.current_candidate
        if (
            current is None
            or not current.request.managed
            or not self.delivery_ready
            or candidate is None
            or current.reconciliation is None
        ):
            return False
        reconciliation = current.reconciliation
        return (
            candidate.delivery.baseline == self.baseline_commit
            and candidate.delivery.digest == reconciliation.worktree_digest
            and candidate.execution_log_path == reconciliation.log_path
            and candidate.execution_log_sha256 == reconciliation.log_sha256
        )

    @property
    def delivery_ready(self) -> bool:
        """Eligible for separate delivery validation, never acceptance by itself.

        A service runtime may be deliberately stopped with a signal after a
        successful turn. Preserve its actual exit code without reinterpreting
        the model terminal. Missing exit evidence is not replaced by a guess.
        """
        current = self.current_attempt
        ready = (
            self.phase == TaskPhase.AWAITING_DELIVERY
            and current is not None
            and current.runtime.status == AttemptStatus.SUCCEEDED
            and current.runtime.exit_code is not None
            and (
                current.reconciliation is None or current.reconciliation.process_stopped
            )
        )
        if not ready or current is None or not current.request.managed:
            return ready
        reconciliation = current.reconciliation
        return (
            prepared_for_task(current) is not None
            and reconciliation is not None
            and reconciliation.process_stopped
            and bool(reconciliation.worktree_digest)
            and bool(reconciliation.log_path)
            and bool(reconciliation.log_sha256)
        )


@dataclass(frozen=True, slots=True)
class TaskInput:
    card: TaskCard
    frozen_rules: FrozenProjectRules
    requirements: str
    created_at: str
