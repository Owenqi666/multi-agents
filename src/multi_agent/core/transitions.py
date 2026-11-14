"""Transition tables for events addressed to the current attempt."""

from collections.abc import Callable
from dataclasses import replace

from .events import (
    AttemptStarted,
    CancelRequested,
    Completed,
    Envelope,
    Error,
    FailureCategory,
    Message,
    ProcessExited,
    ReviewEvent,
    SessionStarted,
    Usage,
)
from .state import (
    AttemptState,
    AttemptStatus,
    Disposition,
    EvidenceSource,
    RoundState,
    SeatState,
)

type Transition = Callable[[AttemptState, ReviewEvent], AttemptState]


def _on[Event: ReviewEvent](
    event_type: type[Event], handler: Callable[[AttemptState, Event], AttemptState]
) -> Transition:
    def transition(attempt: AttemptState, event: ReviewEvent) -> AttemptState:
        assert isinstance(event, event_type)
        return handler(attempt, event)

    return transition


def _keep(attempt: AttemptState, event: ReviewEvent) -> AttemptState:
    return attempt


def _fail(attempt: AttemptState, event: Error) -> AttemptState:
    return replace(
        attempt,
        status=AttemptStatus.FAILED,
        failure_category=event.failure_category,
        detail=event.detail,
        retry_after=event.retry_after,
    )


def _request_cancel(attempt: AttemptState, event: ReviewEvent) -> AttemptState:
    return replace(attempt, status=AttemptStatus.CANCELLING)


def _cancel(
    attempt: AttemptState, *, detail: str | None = None, exit_code: int | None = None
) -> AttemptState:
    return replace(
        attempt,
        status=AttemptStatus.FAILED,
        failure_category=FailureCategory.CANCELLED,
        detail=detail,
        exit_code=exit_code,
        result=None,
    )


def _cancel_with_error(attempt: AttemptState, event: Error) -> AttemptState:
    return _cancel(attempt, detail=event.detail)


def _record_exit(attempt: AttemptState, event: ProcessExited) -> AttemptState:
    return replace(attempt, exit_code=event.exit_code)


def _cancel_with_exit(attempt: AttemptState, event: ProcessExited) -> AttemptState:
    return _cancel(attempt, exit_code=event.exit_code)


def _record_evidence(
    attempt: AttemptState, event: SessionStarted | Completed
) -> AttemptState:
    source = EvidenceSource(event.kind, event.raw_type)
    if event.reported_model is not None:
        attempt = replace(
            attempt, reported_model=event.reported_model, reported_model_source=source
        )
    if isinstance(event, SessionStarted) and event.reported_effort is not None:
        attempt = replace(
            attempt,
            reported_effort=event.reported_effort,
            reported_effort_source=source,
        )
    return attempt


def _report_session(attempt: AttemptState, event: SessionStarted) -> AttemptState:
    return _record_evidence(replace(attempt, session_id=event.session_id), event)


def _report_completion(attempt: AttemptState, event: Completed) -> AttemptState:
    return _record_evidence(
        replace(attempt, status=AttemptStatus.SUCCEEDED, result=event.result), event
    )


def _report_cancelled_completion(
    attempt: AttemptState, event: Completed
) -> AttemptState:
    return _record_evidence(_cancel(attempt), event)


_START_FROM = {None, AttemptStatus.FAILED}
_TRANSITIONS: dict[AttemptStatus, dict[type[ReviewEvent], Transition | None]] = {
    AttemptStatus.RUNNING: {
        SessionStarted: _on(SessionStarted, _report_session),
        Message: _keep,
        Usage: _keep,
        Completed: _on(Completed, _report_completion),
        Error: _on(Error, _fail),
        CancelRequested: _request_cancel,
    },
    AttemptStatus.CANCELLING: {
        SessionStarted: _on(SessionStarted, _report_session),
        Message: _keep,
        Usage: _keep,
        Completed: _on(Completed, _report_cancelled_completion),
        Error: _on(Error, _cancel_with_error),
        ProcessExited: _on(ProcessExited, _cancel_with_exit),
        CancelRequested: None,
    },
}
_TERMINAL_TRANSITIONS: dict[type[ReviewEvent], Transition | None] = {
    SessionStarted: None,
    Message: None,
    Usage: None,
    Completed: None,
    Error: None,
    ProcessExited: _on(ProcessExited, _record_exit),
    CancelRequested: None,
}
_TRANSITIONS[AttemptStatus.SUCCEEDED] = _TERMINAL_TRANSITIONS
_TRANSITIONS[AttemptStatus.FAILED] = _TERMINAL_TRANSITIONS


def apply(state: RoundState, envelope: Envelope) -> tuple[RoundState, Disposition]:
    """Route an event and return the updated state and its history disposition."""
    for index, seat in enumerate(state.seats):
        if seat.config.key == envelope.seat:
            break
    else:
        raise ValueError(f"unknown seat {envelope.seat!r}")

    updated, disposition = _apply_to_seat(seat, envelope)
    if updated is seat:
        return state, disposition
    seats = state.seats[:index] + (updated,) + state.seats[index + 1 :]
    return replace(state, seats=seats), disposition


def _apply_to_seat(
    seat: SeatState, envelope: Envelope
) -> tuple[SeatState, Disposition]:
    event = envelope.event
    current = seat.attempts[-1] if seat.attempts else None
    if envelope.attempt < 1:
        raise ValueError("attempt numbers start at 1")

    if isinstance(event, AttemptStarted):
        starting_status = current.status if current else None
        next_number = current.number + 1 if current else 1
        if starting_status not in _START_FROM or envelope.attempt != next_number:
            raise ValueError(
                f"cannot start attempt {envelope.attempt} from {starting_status}"
            )
        started = AttemptState(number=next_number, status=AttemptStatus.RUNNING)
        return replace(seat, attempts=seat.attempts + (started,)), Disposition.APPLIED

    if current is None or envelope.attempt > current.number:
        raise ValueError(f"attempt {envelope.attempt} has not started")
    if envelope.attempt < current.number:
        return seat, Disposition.HISTORY_ONLY

    registered = _TRANSITIONS[current.status]
    if type(event) not in registered:
        raise ValueError(f"event {event.kind} is invalid in {current.status}")
    transition = registered[type(event)]
    if transition is None:
        return seat, Disposition.HISTORY_ONLY
    updated = transition(current, event)
    if updated is current:
        return seat, Disposition.APPLIED
    return replace(seat, attempts=seat.attempts[:-1] + (updated,)), Disposition.APPLIED
