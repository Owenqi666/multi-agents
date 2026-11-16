"""Planning may authorize a task only from the current, complete evidence chain."""

from dataclasses import FrozenInstanceError, replace

import pytest

from multi_agent.core.events import (
    CancelRequested,
    Completed,
    Error,
    FailureCategory,
    Message,
    ProcessExited,
    SessionStarted,
    Usage,
    thaw_json,
)
from multi_agent.core.planning import (
    PlanningInput,
    PlanningPhase,
    PlanningRequest,
    PlanningReview,
    PlanningState,
    UserDecision,
    planning_hash,
    planning_json,
)
from multi_agent.core.planning_events import (
    PlanningAttemptStarted,
    PlanningDiagnostic,
    PlanningDraftValidated,
    PlanningEnvelope,
    PlanningReconciled,
    PlanningReviewValidated,
    PlanningTaskBound,
    PlanningValidationRejected,
)
from multi_agent.core.state import AttemptStatus, Disposition
from multi_agent.core.tasks import (
    FrozenProjectRules,
    ImplementationConfig,
    NamedCommand,
    OpenQuestion,
    ProjectRules,
    TaskCard,
)
from multi_agent.core.transitions import apply_planning

BASELINE = "a" * 40
LOG_HASH = "c" * 64


def inputs(*, decisions=()):
    rules = FrozenProjectRules(
        "/synthetic/repository/.git",
        BASELINE,
        "b" * 40,
        "synthetic original rules\n",
        ProjectRules(
            1,
            (NamedCommand("unit", "synthetic-test", ()),),
            (".multi-agent/**",),
            (),
            ImplementationConfig("codex", "requested-coder", "medium", ()),
        ),
    )
    return PlanningInput(
        "Implement the stated requirement, retaining the public interface.",
        rules,
        PlanningRequest("claude", "requested-drafter", "high"),
        PlanningRequest("codex", "requested-reviewer", "high"),
        decisions=decisions,
    )


def card(*, goal="Implement the requirement", questions=()):
    return TaskCard(
        1,
        "T-9000",
        goal,
        BASELINE,
        ("src/**",),
        (),
        (),
        ("A concrete implementation",),
        ("The existing public interface remains valid",),
        ("unit",),
        questions,
    )


def start_event(value, stage, *, selected_card=None, draft_attempt=1):
    return PlanningAttemptStarted(
        value.draft_request if stage == "draft" else value.review_request,
        f"Exact frozen {stage} prompt",
        "planning-unit-v1",
        "2026-11-16T00:00:00Z",
        {"cli_version": "synthetic-cli", "logged_in": True},
        draft_attempt if stage == "review" else None,
        selected_card.task_hash if stage == "review" else None,
    )


def send(state, event, *, stage=None, attempt=None, disposition=Disposition.APPLIED):
    stage = stage or state.stage or "draft"
    current = state.current(stage)
    number = attempt if attempt is not None else current.number if current else 1
    updated, actual = apply_planning(state, PlanningEnvelope(stage, number, event))
    assert actual == disposition
    if disposition == Disposition.HISTORY_ONLY:
        assert updated == state
    return updated


def started(value=None):
    value = value or inputs()
    return send(PlanningState(value), start_event(value, "draft"))


def stopped(state, *, terminal=True, exit_code=-15, reconcile=True, result=None):
    if terminal:
        if result is None:
            result = (
                card().to_dict()
                if state.stage == "draft"
                else review_payload(
                    PlanningReview(
                        state.card.task_hash, "ready", "Checked the plan", ()
                    )
                )
            )
        state = send(state, Completed(result))
    if exit_code is not None:
        state = send(state, ProcessExited(exit_code))
    if reconcile is not None:
        state = send(
            state,
            PlanningReconciled(
                reconcile, "Observed process state", "/synthetic/raw.jsonl", LOG_HASH
            ),
        )
    return state


def review_payload(review):
    return {
        "task_hash": review.task_hash,
        "decision": review.decision,
        "reason": review.reason,
        "blocking_issues": list(review.blocking_issues),
    }


def draft_validated(value=None, selected_card=None):
    selected_card = selected_card or card()
    state = stopped(started(value), result=selected_card.to_dict())
    return send(state, PlanningDraftValidated(selected_card))


def review_started(value=None, selected_card=None):
    state = draft_validated(value, selected_card)
    return send(
        state,
        start_event(state.planning_input, "review", selected_card=state.card),
        stage="review",
    )


def ready(value=None, selected_card=None, *, decision="ready", issues=()):
    state = review_started(value, selected_card)
    review = PlanningReview(state.card.task_hash, decision, "Checked the plan", issues)
    state = stopped(state, result=review_payload(review))
    return send(state, PlanningReviewValidated(review))


def test_planning_requires_claude_draft_and_gpt_review():
    initial = PlanningState(inputs())
    assert initial.phase == PlanningPhase.PENDING
    assert not initial.can_bind
    with pytest.raises(ValueError):
        initial.provenance()
    state = ready()
    assert state.can_bind
    assert state.phase == PlanningPhase.READY
    assert state.current("draft").request.request.provider == "claude"
    assert state.current("review").request.request.provider == "codex"
    assert state.current("draft").runtime.exit_code == -15
    assert state.current("review").runtime.exit_code == -15
    assert state.provenance().card.task_hash == card().task_hash


@pytest.mark.parametrize("stage,wrong", [("draft", "codex"), ("review", "claude")])
def test_planning_rejects_the_wrong_runtime_provider(stage, wrong):
    state = PlanningState(inputs()) if stage == "draft" else draft_validated()
    event = start_event(state.planning_input, stage, selected_card=state.card)
    event = replace(event, request=replace(event.request, provider=wrong))
    with pytest.raises(ValueError):
        send(state, event, stage=stage)


@pytest.mark.parametrize("field,value", [("model", "other-model"), ("effort", "low")])
def test_planning_attempt_cannot_replace_frozen_role_configuration(field, value):
    state = PlanningState(inputs())
    event = start_event(state.planning_input, "draft")
    event = replace(event, request=replace(event.request, **{field: value}))
    with pytest.raises(ValueError):
        send(state, event)


@pytest.mark.parametrize(
    "terminal,exit_code,reconcile",
    [(False, 0, True), (True, None, True), (True, -15, None), (True, -15, False)],
)
def test_planning_validation_requires_terminal_exit_and_confirmed_stop(
    terminal, exit_code, reconcile
):
    state = stopped(
        started(), terminal=terminal, exit_code=exit_code, reconcile=reconcile
    )
    with pytest.raises(ValueError):
        send(state, PlanningDraftValidated(card()))
    assert not state.can_bind


@pytest.mark.parametrize(
    "log_path,log_hash", [(None, None), (None, LOG_HASH), ("/log", None)]
)
def test_planning_validation_requires_a_frozen_log_receipt(log_path, log_hash):
    state = stopped(started(), reconcile=None)
    state = send(state, PlanningReconciled(True, "Stopped", log_path, log_hash))
    assert not state.current().usable
    with pytest.raises(ValueError):
        send(state, PlanningDraftValidated(card()))


def test_planning_requested_values_never_fill_unreported_actual_values():
    state = ready()
    for stage in ("draft", "review"):
        attempt = state.current(stage)
        assert attempt.request.request.model.startswith("requested-")
        assert attempt.runtime.reported_model is None
        assert attempt.runtime.reported_effort is None
        assert attempt.runtime.reported_model_source is None
        assert attempt.runtime.reported_effort_source is None


def test_planning_preserves_reported_values_and_usage_even_when_request_differs():
    state = started()
    state = send(
        state,
        SessionStarted(
            "observed-session", "first-observed", "low", raw_type="native.init"
        ),
    )
    usage = Usage({"input_tokens": 7, "output_tokens": 3})
    state = send(state, usage)
    state = send(
        state, Completed(card().to_dict(), "final-observed", raw_type="native.result")
    )
    state = send(state, ProcessExited(-15))
    state = send(state, PlanningReconciled(True, "Stopped", "/log", LOG_HASH))
    state = send(state, PlanningDraftValidated(card()))
    attempt = state.current("draft")
    assert attempt.runtime.reported_model == "final-observed"
    assert attempt.runtime.reported_model_source.raw_type == "native.result"
    assert attempt.runtime.reported_effort == "low"
    assert attempt.runtime.reported_effort_source.raw_type == "native.init"
    assert usage in attempt.events


def test_planning_review_cannot_start_before_validated_draft():
    state = stopped(started())
    with pytest.raises(ValueError):
        send(
            state,
            start_event(state.planning_input, "review", selected_card=card()),
            stage="review",
        )


@pytest.mark.parametrize("field,value", [("draft_attempt", 2), ("task_hash", "d" * 64)])
def test_planning_review_start_binds_exact_draft_attempt_and_hash(field, value):
    state = draft_validated()
    event = start_event(state.planning_input, "review", selected_card=state.card)
    with pytest.raises(ValueError):
        send(state, replace(event, **{field: value}), stage="review")


def test_planning_review_artifact_cannot_approve_a_different_card():
    artifact = PlanningReview("d" * 64, "ready", "Wrong card", ())
    state = stopped(review_started(), result=review_payload(artifact))
    with pytest.raises(ValueError):
        send(state, PlanningReviewValidated(artifact))


def test_validated_card_cannot_replace_the_successful_runtime_card():
    original = card()
    replacement = card(goal="Another otherwise valid task")
    state = stopped(started(), result=original.to_dict())
    with pytest.raises(ValueError, match="current runtime result"):
        send(state, PlanningDraftValidated(replacement))
    assert state.card is None
    assert thaw_json(state.current().runtime.result) == original.to_dict()
    assert send(state, PlanningDraftValidated(original)).card == original


def test_ready_review_cannot_replace_runtime_needs_revision_result():
    state = review_started()
    original = PlanningReview(
        state.card.task_hash,
        "needs_revision",
        "Missing a required check",
        ("Add the check",),
    )
    state = stopped(state, result=review_payload(original))
    replacement = PlanningReview(state.card.task_hash, "ready", "Approved", ())
    with pytest.raises(ValueError, match="current runtime result"):
        send(state, PlanningReviewValidated(replacement))
    assert state.review_artifact is None
    assert thaw_json(state.current().runtime.result) == review_payload(original)
    state = send(state, PlanningReviewValidated(original))
    assert state.phase == PlanningPhase.REVISION_REQUIRED
    assert not state.can_bind


def test_runtime_card_missing_optional_proposed_tests_normalizes_to_same_card():
    selected = card()
    payload = selected.to_dict()
    del payload["proposed_tests"]
    state = stopped(started(), result=payload)
    state = send(state, PlanningDraftValidated(selected))
    assert state.card == selected
    assert state.phase == PlanningPhase.AWAITING_REVIEW


@pytest.mark.parametrize("resolution", [None, "The model invented this answer"])
def test_ready_review_with_unresolved_user_question_remains_revision_required(
    resolution,
):
    selected = card(questions=(OpenQuestion("Which interface?", True, resolution),))
    state = ready(selected_card=selected)
    assert state.phase == PlanningPhase.REVISION_REQUIRED
    assert not state.questions_resolved
    assert not state.can_bind
    revised = send(
        state, start_event(state.planning_input, "draft"), stage="draft", attempt=2
    )
    assert revised.phase == PlanningPhase.DRAFTING
    assert revised.card is None


def test_explicit_frozen_user_decision_resolves_only_the_exact_question_and_answer():
    decision = UserDecision(
        "Which interface?", "Keep the current one", "user_declared", "User instruction"
    )
    selected = card(
        questions=(OpenQuestion(decision.question, True, decision.resolution),)
    )
    assert ready(inputs(decisions=(decision,)), selected).can_bind
    wrong = replace(decision, resolution="Use a new interface")
    assert not ready(inputs(decisions=(wrong,)), selected).can_bind
    assert (
        selected.task_hash
        != card(
            questions=(replace(selected.open_questions[0], resolution=None),)
        ).task_hash
    )


def test_revised_draft_invalidates_previous_review():
    state = ready(decision="needs_revision", issues=("Clarify the write scope",))
    old = state
    state = send(
        state, start_event(state.planning_input, "draft"), stage="draft", attempt=2
    )
    assert state.card is None and state.review_artifact is None
    assert not state.can_bind
    assert state.review_attempts == old.review_attempts
    for event in (
        Completed(
            review_payload(
                PlanningReview(card().task_hash, "ready", "Late approval", ())
            ),
            "late-model",
        ),
        PlanningReviewValidated(PlanningReview(card().task_hash, "ready", "Late", ())),
        ProcessExited(0),
    ):
        state = send(
            state,
            event,
            stage="review",
            attempt=1,
            disposition=Disposition.HISTORY_ONLY,
        )
    for event in (
        PlanningDraftValidated(card()),
        PlanningReconciled(False, "Late old stop"),
    ):
        state = send(
            state, event, stage="draft", attempt=1, disposition=Disposition.HISTORY_ONLY
        )
    selected = card(goal="Revised concrete scope")
    state = stopped(state, result=selected.to_dict())
    state = send(state, PlanningDraftValidated(selected))
    state = send(
        state,
        start_event(
            state.planning_input, "review", selected_card=selected, draft_attempt=2
        ),
        stage="review",
        attempt=2,
    )
    artifact = PlanningReview(
        selected.task_hash, "ready", "Rechecked revised scope", ()
    )
    state = stopped(state, result=review_payload(artifact))
    state = send(
        state,
        PlanningReviewValidated(artifact),
    )
    assert state.can_bind
    assert state.provenance().draft.number == 2
    assert state.provenance().review.number == 2


def test_unknown_future_attempts_and_duplicate_current_start_are_rejected():
    state = started()
    for envelope in (
        PlanningEnvelope("draft", 2, Completed(card().to_dict())),
        PlanningEnvelope(
            "review",
            1,
            Completed(
                review_payload(
                    PlanningReview(card().task_hash, "ready", "Unstarted", ())
                )
            ),
        ),
        PlanningEnvelope("draft", 1, start_event(state.planning_input, "draft")),
        PlanningEnvelope("draft", 0, Message("Invalid number")),
    ):
        with pytest.raises(ValueError):
            apply_planning(state, envelope)


def test_duplicate_runtime_terminal_and_exit_preserve_first_observations():
    state = stopped(started())
    runtime = state.current().runtime
    for event in (
        Completed(card(goal="Late alternative task").to_dict(), "late-model"),
        Error("Late error"),
        ProcessExited(0),
    ):
        state = send(state, event, disposition=Disposition.HISTORY_ONLY)
    assert state.current().runtime == runtime


def test_planning_cancel_diagnostic_is_not_a_terminal_or_stop_confirmation():
    state = send(started(), CancelRequested())
    state = send(
        state,
        PlanningDiagnostic(
            Error("Cleanup pending", raw_type="adapter.cleanup_incomplete")
        ),
    )
    assert state.phase == PlanningPhase.CANCELLING
    assert state.current().runtime.status == AttemptStatus.CANCELLING
    assert state.current().runtime.exit_code is None
    assert state.current().runtime.failure_category is None
    with pytest.raises(ValueError):
        send(state, start_event(state.planning_input, "draft"), attempt=2)
    state = send(state, ProcessExited(-2))
    assert state.phase == PlanningPhase.CANCELLED
    assert state.current().runtime.failure_category == FailureCategory.CANCELLED
    state = send(state, PlanningReconciled(False, "Still unconfirmed"))
    with pytest.raises(ValueError):
        send(state, start_event(state.planning_input, "draft"), attempt=2)
    state = send(state, PlanningReconciled(True, "Process stopped"))
    state = send(state, start_event(state.planning_input, "draft"), attempt=2)
    assert state.current().number == 2


def test_recovery_does_not_invent_terminal_exit_or_actual_configuration():
    state = send(started(), PlanningReconciled(True, "Host confirmed process stopped"))
    assert state.phase == PlanningPhase.FAILED
    assert state.current().runtime.status == AttemptStatus.RUNNING
    assert state.current().runtime.exit_code is None
    assert state.current().runtime.result is None
    assert state.current().runtime.reported_model is None
    assert state.current().runtime.failure_category is None
    assert (
        send(state, start_event(state.planning_input, "draft"), attempt=2)
        .current()
        .number
        == 2
    )


def test_selected_draft_stop_revocation_applies_during_review_and_blocks_validation():
    state = review_started()
    artifact = PlanningReview(state.card.task_hash, "ready", "Looks ready", ())
    state = stopped(state, result=review_payload(artifact))
    state = send(
        state,
        PlanningReconciled(False, "Draft cleanup now unconfirmed"),
        stage="draft",
        attempt=1,
    )
    assert not state.current("draft").stopped
    with pytest.raises(ValueError):
        send(state, PlanningReviewValidated(artifact))


def test_planning_restart_requires_reconciliation():
    state = ready()
    state = send(state, PlanningReconciled(False, "Draft stop revoked"), stage="draft")
    assert state.phase == PlanningPhase.RECONCILIATION_REQUIRED
    assert not state.can_bind
    with pytest.raises(ValueError):
        state.provenance()
    state = send(
        state, PlanningReconciled(False, "Review stop revoked"), stage="review"
    )
    state = send(
        state,
        PlanningReconciled(True, "Draft checked", "/draft.log", LOG_HASH),
        stage="draft",
    )
    assert not state.can_bind
    state = send(
        state,
        PlanningReconciled(True, "Review checked", "/review.log", LOG_HASH),
        stage="review",
    )
    assert state.phase == PlanningPhase.READY
    assert state.can_bind


def test_validation_rejection_preserves_model_success_and_permits_stopped_retry():
    state = stopped(started(), result={"invalid": "Not a task card"})
    state = send(
        state, PlanningValidationRejected("Structured output does not match schema")
    )
    assert state.phase == PlanningPhase.FAILED
    assert state.validation_error == "Structured output does not match schema"
    assert state.current().runtime.status == AttemptStatus.SUCCEEDED
    assert state.current().runtime.failure_category is None
    state = send(state, start_event(state.planning_input, "draft"), attempt=2)
    assert state.validation_error is None


def test_bound_planning_is_sealed_against_late_evidence_and_revisions():
    state = ready()
    before = state.provenance()
    state = send(state, PlanningTaskBound("implementation-run"))
    assert state.phase == PlanningPhase.BOUND
    assert state.bound_task_run_id == "implementation-run"
    assert state.provenance() == before
    assert not state.can_bind
    for stage, attempt, event in (
        ("draft", 1, PlanningReconciled(False, "Late")),
        (
            "review",
            1,
            Completed(
                review_payload(PlanningReview(card().task_hash, "ready", "Late", ()))
            ),
        ),
        ("draft", 2, start_event(state.planning_input, "draft")),
    ):
        state = send(
            state,
            event,
            stage=stage,
            attempt=attempt,
            disposition=Disposition.HISTORY_ONLY,
        )
    assert state.provenance().plan_hash == before.plan_hash


def test_frozen_inputs_and_evidence_have_stable_content_hashes():
    value = inputs()
    assert planning_hash({"b": [2, 3], "a": "中文"}) == planning_hash(
        {"a": "中文", "b": (2, 3)}
    )
    assert planning_hash([2, 3]) != planning_hash([3, 2])
    assert planning_hash("é") != planning_hash("e\u0301")
    assert "中文" in planning_json({"text": "中文"})
    assert (
        value.input_hash
        != replace(value, requirements=value.requirements + "\n").input_hash
    )
    with pytest.raises(FrozenInstanceError):
        value.requirements = "Changed"
    with pytest.raises(ValueError):
        planning_json({"invalid": float("nan")})
    decision = UserDecision("Which?", "This", "user_declared", "User message")
    assert replace(value, decisions=(decision,)).input_hash != value.input_hash
    review = PlanningReview(card().task_hash, "ready", "Reason", ())
    assert replace(review, reason="New reason").artifact_hash != review.artifact_hash
    provenance = ready().provenance()
    assert (
        replace(
            provenance, planning_input=replace(value, requirements="Changed")
        ).plan_hash
        != provenance.plan_hash
    )


def test_attempt_environment_and_runtime_usage_are_deeply_frozen():
    environment = {"nested": {"versions": ["original"]}}
    event = replace(start_event(inputs(), "draft"), environment=environment)
    environment["nested"]["versions"].append("mutated")
    assert tuple(event.environment["nested"]["versions"]) == ("original",)
    counters = {"input_tokens": 7}
    usage = Usage(counters)
    counters["input_tokens"] = 999
    state = send(send(PlanningState(inputs()), event), usage)
    assert (
        next(item for item in state.current().events if isinstance(item, Usage)).counts[
            "input_tokens"
        ]
        == 7
    )
