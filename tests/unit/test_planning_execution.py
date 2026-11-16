"""Prepared launches bind immutable inputs without inventing runtime evidence."""

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
    PlanningExecutionPrepared,
    PlanningReconciled,
    PlanningReviewValidated,
    PlanningTaskBound,
)
from multi_agent.core.planning_execution import (
    PlanningAttemptContext,
    PlanningExecutionPolicy,
    PlanningProviderPolicy,
    PlanningRunOwner,
    prepared_attempts,
    prepared_for,
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
    decode_planning_provenance,
    encode_planning_provenance,
)

LEGACY_PLAN_HASH = "0f7b34c9cafb2fb0edf50b0be00a07c5eaed8469a154fde3528f647f66e583df"


def inputs():
    # Exact T34 synthetic input, pinned before adding Prepared events.
    return PlanningInput(
        "Implement the stated requirement, retaining the public interface.",
        FrozenProjectRules(
            "/synthetic/repository/.git",
            "a" * 40,
            "b" * 40,
            "synthetic original rules\n",
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


def card():
    return TaskCard(
        1,
        "T-9000",
        "Implement the requirement",
        "a" * 40,
        ("src/**",),
        (),
        (),
        ("A concrete implementation",),
        ("The existing public interface remains valid",),
        ("unit",),
        (),
    )


def policy(*, draft_limit=2, review_limit=2):
    return PlanningExecutionPolicy(
        (
            PlanningProviderPolicy(
                "claude", "/synthetic/bin/claude", (1, 10), "d" * 64, draft_limit
            ),
            PlanningProviderPolicy(
                "codex", "/synthetic/bin/codex", (1, 11), "e" * 64, review_limit
            ),
        ),
        120.0,
        2.0,
    )


def preparation(stage="draft", number=1, *, execution_policy=None):
    token = f"{stage}-{number}"
    return PlanningExecutionPrepared(
        "f" * 64,
        execution_policy or policy(),
        PlanningRunOwner("/synthetic/run", (2, 10), "run-token", "1" * 64),
        PlanningAttemptContext(
            f"/synthetic/run/{token}",
            (2, (100 if stage == "draft" else 200) + number),
            token,
            planning_hash({"context": token}),
            planning_hash({"manifest": token}),
        ),
    )


def emit(state, event, *, stage=None, number=None, disposition=Disposition.APPLIED):
    stage = stage or state.stage or "draft"
    current = state.current(stage)
    number = number if number is not None else current.number if current else 1
    updated, actual = apply_planning(state, PlanningEnvelope(stage, number, event))
    assert actual == disposition
    if disposition == Disposition.HISTORY_ONLY:
        assert updated == state
    return updated


def start(state=None, stage="draft", *, legacy=False):
    state = state or PlanningState(inputs())
    previous = state.current(stage)
    number = previous.number + 1 if previous else 1
    event = PlanningAttemptStarted(
        getattr(state.planning_input, stage + "_request"),
        f"Exact frozen {stage} prompt",
        "planning-unit-v1",
        "2026-11-16T00:00:00Z",
        {"cli_version": "synthetic-cli", "logged_in": True} if legacy else None,
        state.current("draft").number if stage == "review" else None,
        state.card.task_hash if stage == "review" else None,
    )
    return emit(state, event, stage=stage, number=number)


def reserve(state=None, stage="draft", *, execution_policy=None):
    state = start(state, stage)
    return emit(
        state,
        preparation(stage, state.current().number, execution_policy=execution_policy),
    )


def finish(state, *, result=None, error=None, stopped=True):
    if error is not None:
        state = emit(state, error)
    else:
        state = emit(state, Completed(card().to_dict() if result is None else result))
    state = emit(state, ProcessExited(-15))
    if stopped is not None:
        state = emit(
            state,
            PlanningReconciled(
                stopped, "Observed process state", "/synthetic/raw.jsonl", "c" * 64
            ),
        )
    return state


def validated_draft(*, execution_policy=None, legacy=False):
    state = start(legacy=True) if legacy else reserve(execution_policy=execution_policy)
    return emit(finish(state), PlanningDraftValidated(card()))


def ready(*, legacy=False):
    state = validated_draft(legacy=legacy)
    state = start(state, "review", legacy=True) if legacy else reserve(state, "review")
    review = PlanningReview(card().task_hash, "ready", "Checked the plan", ())
    state = finish(state, result=planning_value(review))
    return emit(state, PlanningReviewValidated(review))


@pytest.mark.parametrize("stage", ["draft", "review"])
def test_prepared_reservation_contains_no_runtime_or_stop_evidence(stage):
    initial = None if stage == "draft" else validated_draft()
    state = reserve(initial, stage)
    attempt = state.current()
    expected = preparation(stage)
    assert prepared_for(attempt) == expected
    assert attempt.events == (attempt.request, expected)
    assert attempt.request.environment is None
    assert attempt.runtime.status == AttemptStatus.RUNNING
    assert attempt.runtime.session_id is None
    assert attempt.runtime.reported_model is None
    assert attempt.runtime.reported_effort is None
    assert attempt.runtime.result is None
    assert attempt.runtime.exit_code is None
    assert attempt.reconciliation is None
    assert not attempt.stopped and not attempt.usable and not state.can_bind


@pytest.mark.parametrize(
    "intervening",
    [
        SessionStarted("already-launched"),
        Message("already-observed"),
        Usage({"input_tokens": 1}),
        Completed(card().to_dict()),
        Error("preflight failed"),
        CancelRequested(),
        ProcessExited(0),
        PlanningReconciled(False, "stop unknown"),
        PlanningDiagnostic(Error("host observation")),
    ],
)
def test_first_preparation_cannot_follow_any_observation(intervening):
    state = emit(start(), intervening)
    with pytest.raises(ValueError):
        emit(state, preparation())
    assert prepared_for(state.current()) is None


def test_preparation_rejects_unstarted_future_and_preobserved_environment():
    for state, number in ((PlanningState(inputs()), 1), (start(), 2)):
        with pytest.raises(ValueError):
            emit(state, preparation(), number=number)
    with pytest.raises(ValueError):
        emit(start(legacy=True), preparation())


@pytest.mark.parametrize("after_completion", [False, True])
def test_identical_current_preparation_only_enters_history(after_completion):
    state = reserve()
    if after_completion:
        state = finish(state)
    original = state.current().events
    repeated = emit(state, preparation(), disposition=Disposition.HISTORY_ONLY)
    assert repeated.current().events == original
    altered = replace(preparation(), source_snapshot_id="0" * 64)
    with pytest.raises(ValueError):
        emit(state, altered)


def test_prepared_policy_owner_and_context_are_immutable_values():
    record = preparation()
    for value, field, replacement in (
        (record, "source_snapshot_id", "0" * 64),
        (record.policy, "timeout", 1.0),
        (record.policy.providers[0], "executable", "/other"),
        (record.run_owner, "token", "other"),
        (record.attempt_context, "digest", "0" * 64),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(value, field, replacement)
    assert record.policy.for_provider("claude") == record.policy.providers[0]
    assert record.policy.for_provider("codex") == record.policy.providers[1]


def altered_preparations(record):
    provider = record.policy.providers[0]
    values = {
        "source": replace(record, source_snapshot_id="0" * 64),
        "timeout": replace(record, policy=replace(record.policy, timeout=121.0)),
        "grace": replace(record, policy=replace(record.policy, grace=3.0)),
    }
    for field, value in (
        ("executable", "/synthetic/bin/changed"),
        ("executable_identity", (1, 99)),
        ("executable_sha256", "0" * 64),
        ("max_attempts", 3),
    ):
        values[field] = replace(
            record,
            policy=replace(
                record.policy,
                providers=(
                    replace(provider, **{field: value}),
                    record.policy.providers[1],
                ),
            ),
        )
    for field, value in (
        ("root", "/synthetic/other-run"),
        ("identity", (2, 99)),
        ("token", "other-owner"),
        ("manifest_sha256", "0" * 64),
    ):
        values["owner_" + field] = replace(
            record, run_owner=replace(record.run_owner, **{field: value})
        )
    return values


@pytest.mark.parametrize("stage", ["draft", "review"])
@pytest.mark.parametrize("changed", sorted(altered_preparations(preparation())))
def test_retries_and_other_stage_retain_source_policy_and_owner(stage, changed):
    previous = (
        finish(reserve(), error=Error("preflight rejected"))
        if stage == "draft"
        else validated_draft()
    )
    state = start(previous, stage)
    record = preparation(stage, state.current().number)
    with pytest.raises(ValueError):
        emit(state, altered_preparations(record)[changed])
    assert prepared_for(state.current()) is None
    accepted = emit(state, record)
    assert prepared_attempts(accepted)[0] == preparation()


@pytest.mark.parametrize("stage", ["draft", "review"])
@pytest.mark.parametrize("field", ["path", "identity", "token"])
def test_attempt_context_cannot_reuse_any_previous_identity(stage, field):
    previous = (
        finish(reserve(), error=Error("preflight rejected"))
        if stage == "draft"
        else validated_draft()
    )
    state = start(previous, stage)
    record = preparation(stage, state.current().number)
    reused = replace(
        record,
        attempt_context=replace(
            record.attempt_context,
            **{field: getattr(preparation().attempt_context, field)},
        ),
    )
    with pytest.raises(ValueError):
        emit(state, reused)


@pytest.mark.parametrize("stage", ["draft", "review"])
def test_preparation_cannot_adopt_legacy_history_without_a_complete_chain(stage):
    previous = (
        finish(start(legacy=True), error=Error("old preflight rejected"))
        if stage == "draft"
        else validated_draft(legacy=True)
    )
    state = start(previous, stage)
    with pytest.raises(ValueError):
        emit(state, preparation(stage, state.current().number))


def test_preflight_failures_consume_only_their_provider_attempt_budget():
    execution_policy = policy(draft_limit=2, review_limit=1)
    first = reserve(execution_policy=execution_policy)
    first = finish(first, error=Error("auth preflight", FailureCategory.AUTH_EXPIRED))
    assert first.current().runtime.session_id is None
    assert first.current().runtime.result is None
    second = reserve(first, execution_policy=execution_policy)
    draft = emit(finish(second), PlanningDraftValidated(card()))
    review = reserve(draft, "review", execution_policy=execution_policy)
    assert review.current().number == 1
    failed = finish(review, error=Error("review preflight rejected"))
    for stage, number in (("draft", 3), ("review", 2)):
        state = start(failed, stage)
        with pytest.raises(ValueError, match="budget"):
            emit(state, preparation(stage, number, execution_policy=execution_policy))


@pytest.mark.parametrize("stopped", [None, False])
@pytest.mark.parametrize("cancelled", [False, True])
def test_prepared_attempt_cannot_retry_without_latest_confirmed_stop(
    stopped, cancelled
):
    state = reserve()
    if cancelled:
        state = emit(state, CancelRequested())
    state = finish(state, error=Error("preflight failed"), stopped=stopped)
    assert state.current().runtime.exit_code == -15
    assert not state.current().stopped
    with pytest.raises(ValueError, match="stop"):
        start(state)
    assert len(state.draft_attempts) == 1


def test_late_preparation_cannot_replace_old_attempt_or_selected_draft():
    first = finish(reserve(), error=Error("first rejected"))
    second = reserve(first)
    altered = replace(preparation(), source_snapshot_id="0" * 64)
    assert (
        emit(second, altered, number=1, disposition=Disposition.HISTORY_ONLY) == second
    )
    draft = emit(finish(second), PlanningDraftValidated(card()))
    review = reserve(draft, "review")
    assert (
        emit(
            review,
            altered,
            stage="draft",
            number=2,
            disposition=Disposition.HISTORY_ONLY,
        )
        == review
    )
    assert prepared_for(review.current("draft")) == preparation("draft", 2)


def test_prepared_records_are_part_of_selected_provenance_hash():
    state = ready()
    original = state.provenance()
    assert len(prepared_attempts(state)) == 2
    assert decode_planning_provenance(encode_planning_provenance(original)) == original
    record = prepared_for(original.review)
    changed = replace(
        record,
        attempt_context=replace(record.attempt_context, digest="0" * 64),
    )
    altered_review = replace(
        original.review,
        events=tuple(
            changed if event == record else event for event in original.review.events
        ),
    )
    assert replace(original, review=altered_review).plan_hash != original.plan_hash


def test_legacy_planning_provenance_hash_survives_execution_events():
    state = ready(legacy=True)
    assert prepared_attempts(state) == ()
    assert state.provenance().plan_hash == LEGACY_PLAN_HASH
    encoded = encode_planning_provenance(state.provenance())
    assert "planning_execution_prepared" not in encoded
    assert decode_planning_provenance(encoded).plan_hash == LEGACY_PLAN_HASH
    # A late event cannot retrofit the completed old draft or a sealed binding.
    state = emit(
        state, preparation(), stage="draft", disposition=Disposition.HISTORY_ONLY
    )
    state = emit(state, PlanningTaskBound("task"))
    assert state.phase == PlanningPhase.BOUND
    state = emit(state, preparation("review"), disposition=Disposition.HISTORY_ONLY)
    assert state.provenance().plan_hash == LEGACY_PLAN_HASH
