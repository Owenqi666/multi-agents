"""Host observations cannot replace runtime facts or revive rejected evidence."""

import json
from dataclasses import FrozenInstanceError

import pytest

from multi_agent.core.events import (
    CancelRequested,
    Completed,
    Error,
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
    planning_hash,
    planning_value,
)
from multi_agent.core.planning_events import (
    PlanningAttemptStarted,
    PlanningDiagnostic,
    PlanningDraftValidated,
    PlanningEnvelope,
    PlanningEnvironmentObserved,
    PlanningEvidenceRejected,
    PlanningExecutionPrepared,
    PlanningReconciled,
    PlanningReviewValidated,
    PlanningTaskBound,
    PlanningValidationRejected,
)
from multi_agent.core.planning_execution import (
    PlanningAttemptContext,
    PlanningExecutionPolicy,
    PlanningProviderPolicy,
    PlanningRunOwner,
)
from multi_agent.core.state import AttemptStatus, Disposition
from multi_agent.core.tasks import (
    FrozenProjectRules,
    ImplementationConfig,
    NamedCommand,
    ProjectRules,
    TaskCard,
)
from multi_agent.core.transitions import apply_planning
from multi_agent.store.planning_codec import (
    decode_planning_envelope,
    encode_planning_envelope,
)


def planning_input():
    return PlanningInput(
        "Implement the original requirement.",
        FrozenProjectRules(
            "/synthetic/source/.git",
            "a" * 40,
            "b" * 40,
            "original frozen rules\n",
            ProjectRules(
                1,
                (NamedCommand("unit", "synthetic-test", ()),),
                (".multi-agent/**",),
                (),
                ImplementationConfig("codex", "requested-coder", "medium", ()),
            ),
        ),
        PlanningRequest("claude", "requested-drafter", "high"),
        PlanningRequest("codex", "requested-reviewer", "high"),
    )


def task_card():
    return TaskCard(
        1,
        "T-9000",
        "Implement the original requirement",
        "a" * 40,
        ("src/**",),
        (),
        (),
        ("Implementation",),
        ("Public interface remains compatible",),
        ("unit",),
        (),
    )


def emit(state, event, *, stage=None, attempt=None, expected=Disposition.APPLIED):
    stage = stage or state.stage or "draft"
    current = state.current(stage)
    attempt = attempt if attempt is not None else current.number if current else 1
    updated, disposition = apply_planning(
        state, PlanningEnvelope(stage, attempt, event, "observed-at")
    )
    assert disposition == expected
    if expected == Disposition.HISTORY_ONLY:
        assert updated is state
    return updated


def start(state=None, stage="draft"):
    state = state or PlanningState(planning_input())
    current = state.current(stage)
    return emit(
        state,
        PlanningAttemptStarted(
            getattr(state.planning_input, stage + "_request"),
            f"Exact {stage} instructions",
            f"planning-{stage}-v2",
            "started-at",
            draft_attempt=state.current("draft").number if stage == "review" else None,
            task_hash=state.card.task_hash if stage == "review" else None,
        ),
        stage=stage,
        attempt=current.number + 1 if current else 1,
    )


def prepare(state, *, limit=2):
    stage, number = state.stage, state.current().number
    return emit(
        state,
        PlanningExecutionPrepared(
            "c" * 64,
            PlanningExecutionPolicy(
                tuple(
                    PlanningProviderPolicy(
                        provider, f"/synthetic/{provider}", (1, index), "d" * 64, limit
                    )
                    for index, provider in enumerate(("claude", "codex"), start=1)
                ),
                120,
                2,
            ),
            PlanningRunOwner("/synthetic/owner", (2, 1), "owner", "e" * 64),
            PlanningAttemptContext(
                f"/synthetic/owner/{stage}-{number}",
                (2, number + (100 if stage == "draft" else 200)),
                f"{stage}-{number}",
                "f" * 64,
                "0" * 64,
            ),
        ),
    )


def artifact(state):
    if state.stage == "draft":
        return task_card()
    return PlanningReview(
        state.card.task_hash, "ready", "Original requirement checked", ()
    )


def runtime_succeeded(state, *, exit_code=-15):
    result = artifact(state)
    state = emit(state, SessionStarted("session", "reported-model", "reported-effort"))
    state = emit(state, Usage({"output_tokens": 7}))
    state = emit(state, Completed(planning_value(result), "reported-model"))
    if exit_code is not None:
        state = emit(state, ProcessExited(exit_code))
    return state


def reconcile(state, stopped=True):
    return emit(
        state,
        PlanningReconciled(
            stopped, "Actual owned resources inspected", "/logs/manifest", "1" * 64
        ),
    )


def validate(state):
    value = artifact(state)
    event = (
        PlanningDraftValidated(value)
        if state.stage == "draft"
        else PlanningReviewValidated(value)
    )
    return emit(state, event)


def initial(stage="draft", *, prepared=True, limit=2):
    state = start()
    if stage == "review":
        state = validate(reconcile(runtime_succeeded(prepare(state, limit=limit))))
        state = start(state, "review")
    return prepare(state, limit=limit) if prepared else state


@pytest.mark.parametrize("stage", ["draft", "review"])
@pytest.mark.parametrize(
    "before",
    [
        None,
        SessionStarted("model-started"),
        Message("model-message"),
        Usage({"tokens": 1}),
        Completed({}),
        Error("runtime failed"),
        ProcessExited(0),
        CancelRequested(),
        PlanningReconciled(False, "unconfirmed stop"),
    ],
    ids=[
        "unprepared",
        "session",
        "message",
        "usage",
        "completed",
        "error",
        "exit",
        "cancel",
        "reconciled",
    ],
)
def test_planning_environment_observed_requires_prepared_and_precedes_session(
    stage, before
):
    state = initial(stage, prepared=before is not None)
    if before is not None:
        state = emit(state, before)
    original_hash = planning_hash(state)
    with pytest.raises(ValueError):
        emit(state, PlanningEnvironmentObserved({"logged_in": True}))
    assert planning_hash(state) == original_hash


@pytest.mark.parametrize("stage", ["draft", "review"])
@pytest.mark.parametrize(
    "environment",
    [
        None,
        {
            "logged_in": False,
            "cli_version": None,
            "models": None,
            "model_efforts": None,
        },
    ],
)
def test_planning_environment_observed_keeps_unknown_and_negative_actual_values(
    stage, environment
):
    state = emit(initial(stage), PlanningDiagnostic(Error("host preflight diagnostic")))
    current = state.current()
    updated = emit(state, PlanningEnvironmentObserved(environment))
    assert updated.phase == state.phase
    assert updated.current().runtime == current.runtime
    assert updated.current().request == current.request
    assert updated.current().request.environment is None
    assert updated.current().reconciliation is None
    assert thaw_json(updated.current().events[-1].environment) == environment
    assert updated.current().runtime.reported_model is None
    assert updated.current().runtime.reported_effort is None
    assert not updated.current().usable and not updated.can_bind


def test_planning_environment_observed_is_immutable():
    source = {"logged_in": False, "model_efforts": {"reported-model": ["low"]}}
    event = PlanningEnvironmentObserved(source)
    frozen_hash = planning_hash(event)
    source["logged_in"] = True
    source["model_efforts"]["reported-model"].append("high")
    assert thaw_json(event.environment) == {
        "logged_in": False,
        "model_efforts": {"reported-model": ["low"]},
    }
    with pytest.raises(FrozenInstanceError):
        event.environment = None
    with pytest.raises(TypeError):
        event.environment["logged_in"] = True
    with pytest.raises(TypeError):
        event.environment["model_efforts"]["reported-model"][0] = "high"
    assert planning_hash(event) == frozen_hash


@pytest.mark.parametrize(
    "later",
    [
        None,
        SessionStarted("model-started"),
        CancelRequested(),
        Completed({}),
        Error("runtime failure"),
        PlanningReconciled(True, "known stop"),
    ],
)
def test_planning_environment_duplicate_is_history_only_after_later_events(later):
    event = PlanningEnvironmentObserved({"logged_in": False, "models": None})
    state = emit(initial(), event)
    if later is not None:
        state = emit(state, later)
    emit(
        state,
        PlanningEnvironmentObserved({"models": None, "logged_in": False}),
        expected=Disposition.HISTORY_ONLY,
    )
    with pytest.raises(ValueError, match="immutable"):
        emit(state, PlanningEnvironmentObserved({"logged_in": True, "models": None}))
    assert (
        sum(
            isinstance(item, PlanningEnvironmentObserved)
            for item in state.current().events
        )
        == 1
    )


def test_planning_environment_duplicate_uses_json_identity():
    state = emit(initial(), PlanningEnvironmentObserved({"logged_in": True}))
    with pytest.raises(ValueError, match="immutable"):
        emit(state, PlanningEnvironmentObserved({"logged_in": 1}))


@pytest.mark.parametrize("stage", ["draft", "review"])
@pytest.mark.parametrize("exit_code", [None, 0, -15])
@pytest.mark.parametrize("stopped", [None, False, True])
def test_planning_evidence_rejected_preserves_runtime_success_and_exit(
    stage, exit_code, stopped
):
    state = runtime_succeeded(initial(stage), exit_code=exit_code)
    if stopped is not None:
        state = emit(
            state, PlanningReconciled(stopped, "Observed stop, log unavailable")
        )
    previous = state.current()
    assert not previous.usable
    event = PlanningEvidenceRejected("required receipt unavailable")
    rejected = emit(state, event)
    assert rejected.phase == PlanningPhase.FAILED
    assert rejected.validation_error == event.detail
    assert rejected.current().runtime == previous.runtime
    assert rejected.current().runtime.status == AttemptStatus.SUCCEEDED
    assert rejected.current().runtime.failure_category is None
    assert rejected.current().runtime.detail is None
    assert rejected.current().runtime.exit_code == exit_code
    assert rejected.current().request == previous.request
    assert rejected.current().reconciliation == previous.reconciliation
    assert rejected.current().events == previous.events + (event,)
    assert not rejected.can_bind


@pytest.mark.parametrize("stage", ["draft", "review"])
def test_planning_evidence_rejected_retry_requires_actual_stop(stage):
    state = emit(
        runtime_succeeded(initial(stage)), PlanningEvidenceRejected("unsealed log")
    )
    for current in (state, reconcile(state, False)):
        with pytest.raises(ValueError, match="process-stop"):
            start(current, stage)
    state = reconcile(state)
    assert state.phase == PlanningPhase.FAILED
    with pytest.raises(ValueError):
        validate(state)
    second = prepare(start(state, stage))
    assert second.current().number == 2
    second = emit(runtime_succeeded(second), PlanningEvidenceRejected("unsealed log"))
    second = reconcile(second)
    third = start(second, stage)
    with pytest.raises(ValueError, match="budget"):
        prepare(third)
    assert second.current().number == 2


def test_planning_evidence_rejection_is_immutable_and_distinct_from_validation_rejection():
    rejected = emit(
        runtime_succeeded(initial()), PlanningEvidenceRejected("first reason")
    )
    emit(
        rejected,
        PlanningEvidenceRejected("first reason"),
        expected=Disposition.HISTORY_ONLY,
    )
    with pytest.raises(ValueError, match="immutable"):
        emit(rejected, PlanningEvidenceRejected("different reason"))
    with pytest.raises(ValueError):
        emit(rejected, PlanningValidationRejected("first reason"))
    validated_failure = emit(
        reconcile(runtime_succeeded(initial())),
        PlanningValidationRejected("first reason"),
    )
    with pytest.raises(ValueError):
        emit(validated_failure, PlanningEvidenceRejected("first reason"))
    assert validated_failure.validation_error == "first reason"
    with pytest.raises(FrozenInstanceError):
        PlanningEvidenceRejected("immutable").detail = "changed"


@pytest.mark.parametrize(
    "phase", ["drafting", "cancelling", "runtime_failed", "awaiting_review", "ready"]
)
def test_first_evidence_rejection_requires_awaiting_validation(phase):
    state = initial()
    if phase == "cancelling":
        state = emit(state, CancelRequested())
    elif phase == "runtime_failed":
        state = emit(state, Error("runtime failed"))
    elif phase in ("awaiting_review", "ready"):
        state = validate(reconcile(runtime_succeeded(state)))
        if phase == "ready":
            state = validate(
                reconcile(runtime_succeeded(prepare(start(state, "review"))))
            )
    with pytest.raises(ValueError):
        emit(state, PlanningEvidenceRejected("too late or no model success"))


@pytest.mark.parametrize(
    "event",
    [
        PlanningEnvironmentObserved({"logged_in": False}),
        PlanningEvidenceRejected("old rejection"),
    ],
)
def test_late_planning_evidence_rejection_is_history_only(event):
    state = emit(initial(), PlanningEnvironmentObserved({"logged_in": True}))
    state = emit(runtime_succeeded(state), PlanningEvidenceRejected("first failure"))
    state = prepare(start(reconcile(state)))
    emit(state, event, attempt=1, expected=Disposition.HISTORY_ONLY)

    reviewing = initial("review")
    emit(reviewing, event, stage="draft", attempt=1, expected=Disposition.HISTORY_ONLY)
    ready = validate(reconcile(runtime_succeeded(reviewing)))
    bound = emit(ready, PlanningTaskBound("bound-task"))
    original = bound.provenance().plan_hash
    emit(bound, event, stage="review", attempt=1, expected=Disposition.HISTORY_ONLY)
    assert bound.provenance().plan_hash == original


@pytest.mark.parametrize(
    "event", [PlanningEnvironmentObserved(None), PlanningEvidenceRejected("unstarted")]
)
def test_planning_observations_reject_unstarted_and_future_attempts(event):
    for state, number in ((PlanningState(planning_input()), 1), (initial(), 2)):
        with pytest.raises(ValueError, match="not started"):
            emit(state, event, attempt=number)


@pytest.mark.parametrize(
    "event",
    [
        PlanningEnvironmentObserved(
            {
                "cli_version": "actual-cli",
                "logged_in": False,
                "models": None,
                "model_efforts": None,
            }
        ),
        PlanningEnvironmentObserved(None),
        PlanningEvidenceRejected("no sealed receipt"),
    ],
)
def test_planning_execution_observation_codec_round_trip(event):
    original = PlanningEnvelope("draft", 1, event, "actual-observation-time")
    encoded = encode_planning_envelope(original)
    assert json.loads(encoded)["event"]["kind"] == event.kind
    decoded = decode_planning_envelope(encoded)
    assert decoded == original
    assert encode_planning_envelope(decoded) == encoded
