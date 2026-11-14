from collections.abc import Mapping

import pytest

from multi_agent.core import (
    Disposition,
    EvidenceSource,
    RoundState,
    SeatConfig,
    SeatState,
    apply,
    round_status,
)
from multi_agent.core.events import (
    AttemptStarted,
    CancelRequested,
    Completed,
    Envelope,
    Error,
    FailureCategory,
    Message,
    ProcessExited,
    SessionStarted,
    Usage,
)


def new_round(*seats: str) -> RoundState:
    return RoundState(
        "snapshot-1",
        "prompt-1",
        tuple(
            SeatState(SeatConfig(key, "test-provider", "requested-model", "high"))
            for key in seats
        ),
    )


START = Envelope("a", 1, AttemptStarted())
CANCEL = Envelope("a", 1, CancelRequested())
COMPLETE = Envelope("a", 1, Completed("report"))
FAIL = Envelope("a", 1, Error("failure"))


def apply_all(*events: Envelope) -> RoundState:
    state = new_round("a")
    for envelope in events:
        state, disposition = apply(state, envelope)
        assert disposition is Disposition.APPLIED
    return state


@pytest.mark.parametrize("seats", [(), ("a", "b", "c"), ("a", "a")])
def test_invalid_seat_configuration(seats: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        new_round(*seats)


@pytest.mark.parametrize(
    ("prior", "invalid"),
    [
        pytest.param((), Envelope("unknown", 1, AttemptStarted()), id="unknown-start"),
        pytest.param((), Envelope("a", 2, AttemptStarted()), id="first-not-one"),
        pytest.param((START,), Envelope("a", 2, AttemptStarted()), id="start-running"),
        pytest.param(
            (START, CANCEL), Envelope("a", 2, AttemptStarted()), id="start-cancelling"
        ),
        pytest.param(
            (START, COMPLETE), Envelope("a", 2, AttemptStarted()), id="start-succeeded"
        ),
        pytest.param((START, FAIL), START, id="repeat-failed-number"),
        pytest.param(
            (START, FAIL), Envelope("a", 3, AttemptStarted()), id="skip-failed-number"
        ),
        pytest.param((START,), Envelope("unknown", 1, Message("text")), id="unknown"),
        pytest.param((), COMPLETE, id="no-attempt"),
        pytest.param((START,), Envelope("a", 0, Message("text")), id="zero-attempt"),
        pytest.param((START,), Envelope("a", 2, Message("text")), id="future-attempt"),
        pytest.param((START,), Envelope("a", 1, ProcessExited(1)), id="running-exit"),
    ],
)
def test_invalid_transitions_preserve_input(
    prior: tuple[Envelope, ...], invalid: Envelope
) -> None:
    state = apply_all(*prior)
    original = apply_all(*prior)

    with pytest.raises(ValueError):
        apply(state, invalid)

    assert state == original


def test_single_seat_round_uses_current_attempt_status() -> None:
    initial = new_round("a")
    assert round_status(initial) == "in_progress"
    assert round_status(apply_all(START)) == "in_progress"
    assert round_status(apply_all(START, CANCEL)) == "in_progress"
    assert round_status(apply_all(START, FAIL)) == "incomplete"
    assert round_status(apply_all(START, COMPLETE)) == "ready_for_assessment"
    assert initial.seats[0].current_attempt is None


def test_retry_ignores_terminal_events_from_previous_attempt() -> None:
    retry_after = "2026-11-14T10:00:30Z"
    error = Error("try later", FailureCategory.RATE_LIMITED, retry_after)
    state = apply_all(
        START, Envelope("a", 1, error), Envelope("a", 2, AttemptStarted())
    )
    late_completion = Completed("stale", reported_model="old-model")
    latest, disposition = apply(state, Envelope("a", 1, late_completion))

    assert disposition is Disposition.HISTORY_ONLY
    assert latest == state
    assert latest.seats[0].current_attempt == 2
    failed, running = latest.seats[0].attempts
    assert failed.status == "failed"
    assert failed.failure_category == FailureCategory.RATE_LIMITED
    assert failed.detail == "try later"
    assert failed.retry_after == retry_after
    assert failed.result is None
    assert failed.reported_model is None
    assert running.status == "running"
    assert running.reported_model is None


def test_missing_reports_do_not_copy_requested_values() -> None:
    state = apply_all(START, Envelope("a", 1, SessionStarted("session")), COMPLETE)
    (attempt,) = state.seats[0].attempts

    assert attempt.session_id == "session"
    assert attempt.reported_model is None
    assert attempt.reported_model_source is None
    assert attempt.reported_effort is None
    assert attempt.reported_effort_source is None


@pytest.mark.parametrize("later_model", [None, "reported-model-v2"])
def test_reported_values_keep_or_update_their_own_source(
    later_model: str | None,
) -> None:
    first_report = SessionStarted(
        "session", "reported-model", "medium", raw_type="init"
    )
    started = apply_all(START, Envelope("a", 1, first_report))
    completion = Completed("report", reported_model=later_model, raw_type="result")
    state, disposition = apply(started, Envelope("a", 1, completion))

    assert disposition is Disposition.APPLIED
    (attempt,) = state.seats[0].attempts
    assert attempt.reported_effort == "medium"
    assert attempt.reported_effort_source == EvidenceSource("session_started", "init")
    if later_model is None:
        assert attempt.reported_model == "reported-model"
        assert attempt.reported_model_source == EvidenceSource(
            "session_started", "init"
        )
    else:
        assert attempt.reported_model == later_model
        assert attempt.reported_model_source == EvidenceSource("completed", "result")
    assert started.seats[0].attempts[0].reported_model == "reported-model"


def test_running_message_and_usage_leave_attempt_unchanged() -> None:
    running = apply_all(START)
    after_updates = apply_all(
        START, Envelope("a", 1, Message("text")), Envelope("a", 1, Usage({"tokens": 4}))
    )
    assert after_updates == running


def test_cancelling_keeps_activity_in_history_without_changing_state() -> None:
    cancelling = apply_all(START, CANCEL)
    result, disposition = apply(cancelling, CANCEL)
    assert result == cancelling
    assert disposition is Disposition.HISTORY_ONLY

    after_updates = apply_all(
        START,
        CANCEL,
        Envelope("a", 1, SessionStarted("session", "late-model", "medium")),
        Envelope("a", 1, Message("text")),
        Envelope("a", 1, Usage({"tokens": 4})),
    )
    assert after_updates == cancelling


@pytest.mark.parametrize(
    ("ending", "detail", "exit_code"),
    [
        (Completed("discarded report"), None, None),
        (Error("interrupted", FailureCategory.TIMEOUT), "interrupted", None),
        (ProcessExited(130), None, 130),
    ],
)
def test_cancellation_waits_for_terminal_or_exit(
    ending: Completed | Error | ProcessExited, detail: str | None, exit_code: int | None
) -> None:
    state = apply_all(START, CANCEL, Envelope("a", 1, ending))
    (attempt,) = state.seats[0].attempts

    assert round_status(state) == "incomplete"
    assert attempt.status == "failed"
    assert attempt.failure_category == FailureCategory.CANCELLED
    assert attempt.detail == detail
    assert attempt.exit_code == exit_code
    assert attempt.result is None


def test_cancelled_completion_retains_reported_model_evidence() -> None:
    reported = Completed("discarded report", "actual-model", raw_type="result")
    state = apply_all(START, CANCEL, Envelope("a", 1, reported))
    (attempt,) = state.seats[0].attempts

    assert attempt.result is None
    assert attempt.reported_model == "actual-model"
    assert attempt.reported_model_source == EvidenceSource("completed", "result")


@pytest.mark.parametrize("terminal", [COMPLETE, FAIL])
def test_terminal_attempt_accepts_exit_metadata_and_ignores_other_events(
    terminal: Envelope,
) -> None:
    exited = apply_all(START, terminal, Envelope("a", 1, ProcessExited(7)))
    (attempt,) = exited.seats[0].attempts
    assert attempt.exit_code == 7
    assert attempt.failure_category is None
    assert attempt.result == ("report" if terminal == COMPLETE else None)

    for event in (
        SessionStarted("late"),
        Message("late"),
        Usage({"tokens": 80}),
        Completed("late"),
        Error("late"),
        CancelRequested(),
    ):
        latest, disposition = apply(exited, Envelope("a", 1, event))
        assert disposition is Disposition.HISTORY_ONLY
        assert latest == exited


def test_event_payloads_snapshot_nested_data() -> None:
    report = {"findings": [{"summary": "original"}]}
    counts = {"input_tokens": 11}
    completed = Completed(report)
    usage = Usage(counts)
    report["findings"][0]["summary"] = "changed outside the event"
    counts["input_tokens"] = 999

    state = apply_all(START, Envelope("a", 1, usage), Envelope("a", 1, completed))
    assert usage.counts["input_tokens"] == 11
    result = state.seats[0].attempts[0].result
    assert isinstance(result, Mapping)
    assert result["findings"][0]["summary"] == "original"
    with pytest.raises(TypeError):
        result["findings"][0]["summary"] = "changed inside the event"
