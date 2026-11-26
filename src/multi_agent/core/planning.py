"""Frozen planning inputs and evidence, separate from implementation attempts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from .dispatch import ImplementationChoice
from .state import AttemptState, AttemptStatus
from .tasks import FrozenProjectRules, TaskCard

if TYPE_CHECKING:
    from .planning_events import (
        PlanningAttemptStarted,
        PlanningEvent,
        PlanningReconciled,
    )


def planning_value(value: Any) -> Any:
    """Serialize trusted immutable values without deepcopying MappingProxy."""
    if is_dataclass(value) and not isinstance(value, type):
        result = {
            field.name: planning_value(getattr(value, field.name))
            for field in fields(value)
        }
        if hasattr(value, "kind"):
            result["kind"] = value.kind
        return result
    if isinstance(value, Mapping):
        return {key: planning_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [planning_value(item) for item in value]
    return value


def planning_json(value: Any) -> str:
    return json.dumps(
        planning_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def planning_hash(value: Any) -> str:
    return hashlib.sha256(planning_json(value).encode("utf-8")).hexdigest()


LEGACY_PLANNING_PROVIDERS = {"draft": "claude", "review": "codex"}


@dataclass(frozen=True, slots=True)
class PlanningRequest:
    provider: str
    model: str
    effort: str


DEFAULT_TASK_DRAFT = PlanningRequest("claude", "claude-opus-5-5", "high")
DEFAULT_TASK_PLAN_REVIEW = PlanningRequest("codex", "gpt-6.1-sol", "high")


@dataclass(frozen=True, slots=True)
class DependencyReceipt:
    task_id: str
    baseline_commit: str
    source: str
    detail: str


@dataclass(frozen=True, slots=True)
class UserDecision:
    question: str
    resolution: str
    source: str
    detail: str


@dataclass(frozen=True, slots=True)
class PlanningInput:
    requirements: str
    frozen_rules: FrozenProjectRules
    draft_request: PlanningRequest
    review_request: PlanningRequest
    dependencies: tuple[DependencyReceipt, ...] = ()
    decisions: tuple[UserDecision, ...] = ()

    def request_for(self, stage: str) -> PlanningRequest:
        return {"draft": self.draft_request, "review": self.review_request}[stage]

    @property
    def input_hash(self) -> str:
        return planning_hash(self)


@dataclass(frozen=True, slots=True)
class PlanningReview:
    task_hash: str
    decision: str
    reason: str
    blocking_issues: tuple[str, ...]

    @property
    def artifact_hash(self) -> str:
        return planning_hash(self)


@dataclass(frozen=True, slots=True)
class PlanningAttempt:
    request: PlanningAttemptStarted
    runtime: AttemptState
    reconciliation: PlanningReconciled | None = None
    events: tuple[PlanningEvent, ...] = ()

    @property
    def number(self) -> int:
        return self.runtime.number

    @property
    def stopped(self) -> bool:
        return self.reconciliation is not None and self.reconciliation.process_stopped

    @property
    def usable(self) -> bool:
        return (
            self.runtime.status == AttemptStatus.SUCCEEDED
            and self.runtime.exit_code is not None
            and self.stopped
            and self.reconciliation is not None
            and self.reconciliation.log_path is not None
            and self.reconciliation.log_sha256 is not None
        )


class PlanningPhase(StrEnum):
    PENDING = "pending"
    DRAFTING = "drafting"
    REVIEWING = "reviewing"
    AWAITING_VALIDATION = "awaiting_validation"
    AWAITING_REVIEW = "awaiting_review"
    REVISION_REQUIRED = "revision_required"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    READY = "ready"
    BOUND = "bound"


@dataclass(frozen=True, slots=True)
class PlanningProvenance:
    planning_input: PlanningInput
    card: TaskCard
    draft: PlanningAttempt
    review: PlanningAttempt
    review_artifact: PlanningReview

    @property
    def plan_hash(self) -> str:
        return planning_hash(self)


@dataclass(frozen=True, slots=True)
class ProjectedDependency:
    card_id: str
    planning_run_id: str
    task_run_id: str
    implementation_attempt: int
    candidate: str


@dataclass(frozen=True, slots=True)
class ProjectedDraft:
    stage_run_id: str
    split_run_id: str
    split_attempt: int
    split_manifest_sha256: str
    entry_index: int
    entry_hash: str
    card_hash: str
    executor: ImplementationChoice
    dependencies: tuple[ProjectedDependency, ...]

    @property
    def dependency_ids(self) -> frozenset[str]:
        return frozenset(item.card_id for item in self.dependencies)


@dataclass(frozen=True, slots=True)
class ProjectedPlanningProvenance:
    planning_input: PlanningInput
    card: TaskCard
    projection: ProjectedDraft
    review: PlanningAttempt
    review_artifact: PlanningReview

    @property
    def plan_hash(self) -> str:
        return planning_hash(self)


type VerifiedPlanningProvenance = PlanningProvenance | ProjectedPlanningProvenance


@dataclass(frozen=True, slots=True)
class PlanningState:
    planning_input: PlanningInput
    phase: PlanningPhase = PlanningPhase.PENDING
    stage: str | None = None
    draft_attempts: tuple[PlanningAttempt, ...] = ()
    review_attempts: tuple[PlanningAttempt, ...] = ()
    card: TaskCard | None = None
    review_artifact: PlanningReview | None = None
    validation_error: str | None = None
    bound_task_run_id: str | None = None
    split: bool = False
    projection: ProjectedDraft | None = None

    def attempts(self, stage: str) -> tuple[PlanningAttempt, ...]:
        return {"draft": self.draft_attempts, "review": self.review_attempts}[stage]

    def current(self, stage: str | None = None) -> PlanningAttempt | None:
        selected = self.stage if stage is None else stage
        if selected is None:
            return None
        attempts = self.attempts(selected)
        return attempts[-1] if attempts else None

    @property
    def questions_resolved(self) -> bool:
        if self.card is None:
            return False
        answers = {
            (item.question, item.resolution) for item in self.planning_input.decisions
        }
        return all(
            not item.blocks_dispatch or (item.question, item.resolution) in answers
            for item in self.card.open_questions
        )

    def matches_draft(self, draft_attempt: int | None) -> bool:
        draft = self.current("draft")
        if self.projection is not None:
            return (
                draft is None
                and draft_attempt is None
                and self.card is not None
                and self.card.task_hash == self.projection.card_hash
            )
        return draft is not None and draft.usable and draft_attempt == draft.number

    @property
    def evidence_ready(self) -> bool:
        review = self.current("review")
        return (
            self.card is not None
            and review is not None
            and review.usable
            and self.matches_draft(review.request.draft_attempt)
            and review.request.task_hash == self.card.task_hash
            and self.review_artifact is not None
            and self.review_artifact.task_hash == self.card.task_hash
            and self.review_artifact.decision == "ready"
            and not self.review_artifact.blocking_issues
            and self.questions_resolved
        )

    @property
    def can_bind(self) -> bool:
        return self.phase == PlanningPhase.READY and self.evidence_ready

    def provenance(self) -> VerifiedPlanningProvenance:
        if not self.can_bind and self.phase != PlanningPhase.BOUND:
            raise ValueError("planning evidence does not permit a task binding")
        draft = self.current("draft")
        review = self.current("review")
        assert self.card is not None and self.review_artifact is not None
        assert review is not None
        if self.projection is not None:
            return ProjectedPlanningProvenance(
                self.planning_input,
                self.card,
                self.projection,
                review,
                self.review_artifact,
            )
        assert draft is not None
        return PlanningProvenance(
            self.planning_input, self.card, draft, review, self.review_artifact
        )
