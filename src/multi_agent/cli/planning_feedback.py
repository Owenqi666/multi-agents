"""Derive immutable-context feedback from a persisted planning history prefix."""

from typing import Any

from multi_agent.core.planning import PlanningInput, planning_hash, planning_value
from multi_agent.core.planning_events import (
    PlanningAttemptStarted,
    PlanningDraftValidated,
    PlanningEnvelope,
    PlanningEvidenceRejected,
    PlanningReviewValidated,
    PlanningValidationRejected,
)
from multi_agent.core.state import Disposition
from multi_agent.core.tasks import TaskCard

from .planning_inputs import validate_planning_input


def feedback_for_prefix(
    history: tuple[tuple[PlanningEnvelope, Disposition], ...],
    planning_input: PlanningInput,
) -> dict[str, Any]:
    """Select applied host artifacts, never inferred results or current state.

    History is the exact prefix captured before the next Started reservation.
    An applied review with a broken association is corrupt evidence, not a
    reason to silently substitute another card or an older review.
    """
    validate_planning_input(planning_input, configured_roles=True)
    drafts: dict[int, TaskCard] = {}
    requests: dict[int, PlanningAttemptStarted] = {}
    reviewed_card: TaskCard | None = None
    reviewed_draft: dict[str, Any] | None = None
    review: dict[str, Any] | None = None
    rejections: list[dict[str, Any]] = []
    for envelope, disposition in history:
        if disposition != Disposition.APPLIED:
            continue
        event = envelope.event
        if isinstance(event, PlanningDraftValidated):
            if envelope.stage != "draft":
                raise ValueError("validated draft is not in the draft stage")
            drafts[envelope.attempt] = event.card
        elif isinstance(event, PlanningAttemptStarted) and envelope.stage == "review":
            requests[envelope.attempt] = event
        elif isinstance(event, PlanningReviewValidated):
            if envelope.stage != "review":
                raise ValueError("validated review is not in the review stage")
            request = requests.get(envelope.attempt)
            if request is None or request.draft_attempt not in drafts:
                raise ValueError(
                    "validated review has no matching started draft reference"
                )
            assert request.draft_attempt is not None
            card = drafts[request.draft_attempt]
            if (
                request.task_hash != card.task_hash
                or event.review.task_hash != card.task_hash
            ):
                raise ValueError("validated review and requested draft hashes disagree")
            reviewed_card = card
            reviewed_draft = {
                "attempt": request.draft_attempt,
                "task_hash": card.task_hash,
                "card": card.to_dict(),
            }
            review = {
                "attempt": envelope.attempt,
                "request": planning_value(request),
                "artifact_hash": event.review.artifact_hash,
                "artifact": planning_value(event.review),
            }
        elif isinstance(event, (PlanningValidationRejected, PlanningEvidenceRejected)):
            rejections.append(
                {
                    "stage": envelope.stage,
                    "attempt": envelope.attempt,
                    "kind": event.kind,
                    "detail": event.detail,
                    "occurred_at": envelope.occurred_at,
                }
            )
    answers = {(item.question, item.resolution) for item in planning_input.decisions}
    unresolved = (
        [
            planning_value(question)
            for question in reviewed_card.open_questions
            if question.blocks_dispatch
            and (question.question, question.resolution) not in answers
        ]
        if reviewed_card is not None
        else []
    )
    return {
        "version": 1,
        "history_prefix": {"length": len(history), "sha256": planning_hash(history)},
        "reviewed_draft": reviewed_draft,
        "review": review,
        "unresolved_blocking_questions": unresolved,
        "host_rejections": rejections,
    }
