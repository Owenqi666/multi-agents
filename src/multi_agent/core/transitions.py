"""Transition tables for events addressed to the current attempt."""

from collections.abc import Callable
from dataclasses import replace
from typing import Any, cast

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
    thaw_json,
)
from .planning import PlanningAttempt, PlanningPhase, PlanningState, planning_value
from .planning_events import (
    PlanningAttemptStarted,
    PlanningDiagnostic,
    PlanningDraftValidated,
    PlanningEnvelope,
    PlanningEvent,
    PlanningReconciled,
    PlanningReviewValidated,
    PlanningTaskBound,
    PlanningValidationRejected,
)
from .state import (
    AttemptState,
    AttemptStatus,
    Disposition,
    EvidenceSource,
    RoundState,
    SeatConfig,
    SeatState,
)
from .task_events import (
    TaskAttemptStarted,
    TaskDiagnostic,
    TaskEnvelope,
    TaskEvent,
    TaskReconciled,
    TaskScopeBlocked,
)
from .task_state import TaskAttempt, TaskPhase, TaskState
from .tasks import TaskCard

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


# Task lifecycle registration is separate from review-seat registration. In
# particular, task retries require reconciliation and exits are first-write
# evidence. Do not change the established review semantics to implement these.
type TaskTransition = Callable[[TaskState, TaskEnvelope], tuple[TaskState, Disposition]]


def _task_start(
    state: TaskState, envelope: TaskEnvelope
) -> tuple[TaskState, Disposition]:
    event = envelope.event
    assert isinstance(event, TaskAttemptStarted)
    current = state.current_attempt
    if current is not None and (
        current.reconciliation is None or not current.reconciliation.process_stopped
    ):
        raise ValueError("retry requires current process and workspace reconciliation")
    if (
        event.decision.task_hash != state.task_hash
        or event.decision.rules_hash != state.rules_hash
    ):
        raise ValueError("dispatch decision does not match frozen task and rules")
    started = TaskAttempt(event, AttemptState(envelope.attempt, AttemptStatus.RUNNING))
    return (
        replace(
            state,
            phase=TaskPhase.IMPLEMENTING,
            attempts=state.attempts + (started,),
        ),
        Disposition.APPLIED,
    )


_TASK_RUNTIME_PHASES = {
    AttemptStatus.RUNNING: TaskPhase.IMPLEMENTING,
    AttemptStatus.CANCELLING: TaskPhase.CANCELLING,
    AttemptStatus.SUCCEEDED: TaskPhase.AWAITING_DELIVERY,
    AttemptStatus.FAILED: TaskPhase.FAILED,
}


def _task_runtime(
    state: TaskState, envelope: TaskEnvelope
) -> tuple[TaskState, Disposition]:
    event = envelope.event
    assert isinstance(
        event, (SessionStarted, Message, Usage, Completed, Error, CancelRequested)
    )
    current = state.attempts[-1]
    # The existing table operates on a seat, but stores no model request evidence
    # into runtime observations. Its provider/model fields here are only routing
    # context; every task attempt retains its own immutable requested decision.
    choice = current.request.decision.choice
    seat = SeatState(
        SeatConfig("implementation", choice.provider, choice.model, choice.effort),
        tuple(attempt.runtime for attempt in state.attempts),
    )
    updated, disposition = _apply_to_seat(
        seat, Envelope("implementation", envelope.attempt, event, envelope.occurred_at)
    )
    if disposition == Disposition.HISTORY_ONLY:
        return state, disposition
    runtime = updated.attempts[-1]
    phase = _TASK_RUNTIME_PHASES[runtime.status]
    if (
        runtime.status == AttemptStatus.FAILED
        and runtime.failure_category == FailureCategory.CANCELLED
    ):
        phase = TaskPhase.CANCELLED
    return (
        replace(
            state,
            phase=phase,
            attempts=state.attempts[:-1] + (replace(current, runtime=runtime),),
        ),
        disposition,
    )


def _task_exit(
    state: TaskState, envelope: TaskEnvelope
) -> tuple[TaskState, Disposition]:
    event = envelope.event
    assert isinstance(event, ProcessExited)
    current = state.attempts[-1]
    if current.runtime.exit_code is not None:
        return state, Disposition.HISTORY_ONLY
    phase = state.phase
    if phase == TaskPhase.CANCELLING:
        runtime = _cancel_with_exit(current.runtime, event)
        phase = TaskPhase.CANCELLED
    else:
        # A confirmed host recovery can already have closed the task phase
        # without a model terminal. Record this later actual exit independently.
        runtime = _record_exit(current.runtime, event)
    return (
        replace(
            state,
            phase=phase,
            attempts=state.attempts[:-1] + (replace(current, runtime=runtime),),
        ),
        Disposition.APPLIED,
    )


def _task_reconcile(
    state: TaskState, envelope: TaskEnvelope
) -> tuple[TaskState, Disposition]:
    event = envelope.event
    assert isinstance(event, TaskReconciled)
    phase = state.phase
    if event.process_stopped:
        phase = {
            TaskPhase.IMPLEMENTING: TaskPhase.FAILED,
            TaskPhase.CANCELLING: TaskPhase.CANCELLED,
        }.get(phase, phase)
    # Only the task phase changes on host recovery. No model Error, exit code,
    # reported setting or runtime status is invented from the host observation.
    current = replace(state.attempts[-1], reconciliation=event)
    return (
        replace(state, phase=phase, attempts=state.attempts[:-1] + (current,)),
        Disposition.APPLIED,
    )


def _task_scope_block(
    state: TaskState, envelope: TaskEnvelope
) -> tuple[TaskState, Disposition]:
    event = envelope.event
    assert isinstance(event, TaskScopeBlocked)
    if not state.delivery_ready:
        raise ValueError(
            "scope handback requires a successful terminal and actual exit"
        )
    return (
        replace(state, phase=TaskPhase.PLANNING_REQUIRED, scope_block=event),
        Disposition.APPLIED,
    )


_TASK_ACTIVE: dict[type[TaskEvent], TaskTransition | None] = {
    SessionStarted: _task_runtime,
    Message: _task_runtime,
    Usage: _task_runtime,
    Completed: _task_runtime,
    Error: _task_runtime,
    CancelRequested: _task_runtime,
    TaskReconciled: _task_reconcile,
    TaskDiagnostic: None,
}
_TASK_CLOSED: dict[type[TaskEvent], TaskTransition | None] = {
    SessionStarted: None,
    Message: None,
    Usage: None,
    Completed: None,
    Error: None,
    CancelRequested: None,
    ProcessExited: _task_exit,
    TaskDiagnostic: None,
    TaskReconciled: _task_reconcile,
}
_TASK_TRANSITIONS: dict[TaskPhase, dict[type[TaskEvent], TaskTransition | None]] = {
    TaskPhase.READY: {TaskAttemptStarted: _task_start},
    TaskPhase.IMPLEMENTING: _TASK_ACTIVE,
    TaskPhase.CANCELLING: {**_TASK_ACTIVE, ProcessExited: _task_exit},
    TaskPhase.AWAITING_DELIVERY: {**_TASK_CLOSED, TaskScopeBlocked: _task_scope_block},
    TaskPhase.FAILED: {**_TASK_CLOSED, TaskAttemptStarted: _task_start},
    TaskPhase.CANCELLED: {**_TASK_CLOSED, TaskAttemptStarted: _task_start},
    TaskPhase.PLANNING_REQUIRED: {
        **dict.fromkeys(_TASK_CLOSED),
        TaskScopeBlocked: None,
    },
}


def apply_task(
    state: TaskState, envelope: TaskEnvelope
) -> tuple[TaskState, Disposition]:
    """Pure task transition; a store must route and write within one transaction."""
    current = state.current_attempt
    if envelope.attempt < 1:
        raise ValueError("task attempt numbers start at 1")
    if current is not None and envelope.attempt < current.number:
        return state, Disposition.HISTORY_ONLY
    if isinstance(envelope.event, TaskAttemptStarted):
        expected = current.number + 1 if current is not None else 1
        if envelope.attempt != expected:
            raise ValueError("task start must use the next attempt number")
    elif current is None or envelope.attempt != current.number:
        raise ValueError("task attempt has not started")
    registered = _TASK_TRANSITIONS[state.phase]
    if type(envelope.event) not in registered:
        raise ValueError(
            f"event {envelope.event.kind} is invalid in task phase {state.phase}"
        )
    transition = registered[type(envelope.event)]
    if transition is None:
        return state, Disposition.HISTORY_ONLY
    return transition(state, envelope)


# Planning has sequential provider roles and independent attempt numbers. The
# shared runtime evidence reducer does not grant any planning or task approval.
type PlanningTransition = Callable[
    [PlanningState, PlanningEnvelope], tuple[PlanningState, Disposition]
]


def _planning_attempt(
    state: PlanningState, stage: str, attempt: PlanningAttempt
) -> PlanningState:
    attempts = state.attempts(stage)
    return _planning_attempts(state, stage, attempts[:-1] + (attempt,))


def _planning_attempts(
    state: PlanningState, stage: str, attempts: tuple[PlanningAttempt, ...]
) -> PlanningState:
    if stage == "draft":
        return replace(state, draft_attempts=attempts)
    return replace(state, review_attempts=attempts)


def _planning_record(
    state: PlanningState, envelope: PlanningEnvelope, attempt: PlanningAttempt
) -> PlanningState:
    return _planning_attempt(
        state,
        envelope.stage,
        replace(attempt, events=attempt.events + (envelope.event,)),
    )


def _planning_start(
    state: PlanningState, envelope: PlanningEnvelope
) -> tuple[PlanningState, Disposition]:
    event = envelope.event
    assert isinstance(event, PlanningAttemptStarted)
    previous = state.current(envelope.stage)
    if envelope.attempt != (previous.number + 1 if previous is not None else 1):
        raise ValueError("planning attempts must start in sequence")
    if any(
        current is not None and not current.stopped
        for current in (state.current("draft"), state.current("review"))
    ):
        raise ValueError("planning requires latest process-stop reconciliation")
    expected = getattr(state.planning_input, envelope.stage + "_request")
    provider = {"draft": "claude", "review": "codex"}[envelope.stage]
    if event.request != expected or event.request.provider != provider:
        raise ValueError("planning role must retain its frozen provider and request")
    if envelope.stage == "draft":
        if event.draft_attempt is not None or event.task_hash is not None:
            raise ValueError("draft attempt cannot claim a reviewed artifact")
        updated = replace(
            state,
            phase=PlanningPhase.DRAFTING,
            card=None,
            review_artifact=None,
        )
    else:
        if state.phase not in (
            PlanningPhase.AWAITING_REVIEW,
            PlanningPhase.FAILED,
            PlanningPhase.CANCELLED,
        ):
            raise ValueError("review requires a validated draft or failed review retry")
        draft = state.current("draft")
        if (
            state.card is None
            or draft is None
            or not draft.usable
            or event.draft_attempt != draft.number
            or event.task_hash != state.card.task_hash
        ):
            raise ValueError("review must bind the current usable draft and task hash")
        updated = replace(state, phase=PlanningPhase.REVIEWING, review_artifact=None)
    attempt = PlanningAttempt(
        event,
        AttemptState(envelope.attempt, AttemptStatus.RUNNING),
        events=(event,),
    )
    return (
        _planning_attempts(
            replace(updated, stage=envelope.stage, validation_error=None),
            envelope.stage,
            state.attempts(envelope.stage) + (attempt,),
        ),
        Disposition.APPLIED,
    )


def _planning_runtime(
    state: PlanningState, envelope: PlanningEnvelope
) -> tuple[PlanningState, Disposition]:
    event = envelope.event
    assert isinstance(
        event, (SessionStarted, Message, Usage, Completed, Error, CancelRequested)
    )
    current = state.current(envelope.stage)
    assert current is not None
    request = current.request.request
    seat = SeatState(
        SeatConfig(envelope.stage, request.provider, request.model, request.effort),
        tuple(attempt.runtime for attempt in state.attempts(envelope.stage)),
    )
    reduced, disposition = _apply_to_seat(
        seat,
        Envelope(envelope.stage, envelope.attempt, event, envelope.occurred_at),
    )
    if disposition == Disposition.HISTORY_ONLY:
        return state, disposition
    runtime = reduced.attempts[-1]
    phase = {
        AttemptStatus.RUNNING: PlanningPhase.DRAFTING
        if envelope.stage == "draft"
        else PlanningPhase.REVIEWING,
        AttemptStatus.CANCELLING: PlanningPhase.CANCELLING,
        AttemptStatus.SUCCEEDED: PlanningPhase.AWAITING_VALIDATION,
        AttemptStatus.FAILED: PlanningPhase.CANCELLED
        if runtime.failure_category == FailureCategory.CANCELLED
        else PlanningPhase.FAILED,
    }[runtime.status]
    return (
        replace(
            _planning_record(state, envelope, replace(current, runtime=runtime)),
            phase=phase,
        ),
        disposition,
    )


def _planning_exit(
    state: PlanningState, envelope: PlanningEnvelope
) -> tuple[PlanningState, Disposition]:
    event = envelope.event
    assert isinstance(event, ProcessExited)
    current = state.current(envelope.stage)
    assert current is not None
    if current.runtime.exit_code is not None:
        return state, Disposition.HISTORY_ONLY
    cancelling = state.phase == PlanningPhase.CANCELLING
    runtime = (
        _cancel_with_exit(current.runtime, event)
        if cancelling
        else _record_exit(current.runtime, event)
    )
    return (
        replace(
            _planning_record(state, envelope, replace(current, runtime=runtime)),
            phase=PlanningPhase.CANCELLED if cancelling else state.phase,
        ),
        Disposition.APPLIED,
    )


def _planning_reconcile(
    state: PlanningState, envelope: PlanningEnvelope
) -> tuple[PlanningState, Disposition]:
    event = envelope.event
    assert isinstance(event, PlanningReconciled)
    current = state.current(envelope.stage)
    assert current is not None
    updated = _planning_record(state, envelope, replace(current, reconciliation=event))
    phase = state.phase
    if envelope.stage == state.stage and event.process_stopped:
        # Recovery closes the host phase; it never invents a model terminal,
        # exit code, or actual model/effort value.
        phase = {
            PlanningPhase.DRAFTING: PlanningPhase.FAILED,
            PlanningPhase.REVIEWING: PlanningPhase.FAILED,
            PlanningPhase.CANCELLING: PlanningPhase.CANCELLED,
        }.get(phase, phase)
    if phase == PlanningPhase.READY and not updated.evidence_ready:
        phase = PlanningPhase.RECONCILIATION_REQUIRED
    elif phase == PlanningPhase.RECONCILIATION_REQUIRED and updated.evidence_ready:
        phase = PlanningPhase.READY
    return replace(updated, phase=phase), Disposition.APPLIED


def _planning_diagnostic(
    state: PlanningState, envelope: PlanningEnvelope
) -> tuple[PlanningState, Disposition]:
    current = state.current(envelope.stage)
    assert current is not None
    return _planning_record(state, envelope, current), Disposition.APPLIED


def _planning_validate(
    state: PlanningState, envelope: PlanningEnvelope
) -> tuple[PlanningState, Disposition]:
    current = state.current(envelope.stage)
    assert current is not None
    if not current.usable:
        raise ValueError(
            "planning validation requires success, exit, stop and log receipt"
        )
    event = envelope.event
    if isinstance(event, PlanningDraftValidated):
        if envelope.stage != "draft":
            raise ValueError("only a draft attempt can validate a task card")
        source_card = TaskCard.from_dict(
            cast(dict[str, Any], thaw_json(current.runtime.result))
        )
        if source_card != event.card:
            raise ValueError("validated draft differs from the current runtime result")
        if (
            event.card.baseline_commit
            != state.planning_input.frozen_rules.baseline_commit
        ):
            raise ValueError("draft baseline differs from frozen planning input")
        updated = replace(state, card=event.card, phase=PlanningPhase.AWAITING_REVIEW)
    elif isinstance(event, PlanningReviewValidated):
        if planning_value(event.review) != thaw_json(current.runtime.result):
            raise ValueError("validated review differs from the current runtime result")
        draft = state.current("draft")
        if (
            envelope.stage != "review"
            or state.card is None
            or draft is None
            or not draft.usable
            or current.request.draft_attempt != draft.number
            or current.request.task_hash != state.card.task_hash
            or event.review.task_hash != state.card.task_hash
        ):
            raise ValueError("planning review does not bind the usable current draft")
        updated = replace(state, review_artifact=event.review)
        updated = replace(
            updated,
            phase=PlanningPhase.READY
            if updated.evidence_ready
            else PlanningPhase.REVISION_REQUIRED,
        )
    else:
        assert isinstance(event, PlanningValidationRejected)
        updated = replace(
            state, phase=PlanningPhase.FAILED, validation_error=event.detail
        )
    return _planning_record(updated, envelope, current), Disposition.APPLIED


def _planning_bind(
    state: PlanningState, envelope: PlanningEnvelope
) -> tuple[PlanningState, Disposition]:
    event = envelope.event
    assert isinstance(event, PlanningTaskBound)
    if envelope.stage != "review" or not state.can_bind:
        raise ValueError("only complete current planning evidence can bind a task")
    # Binding is in the history, outside the already selected artifact evidence.
    return (
        replace(state, phase=PlanningPhase.BOUND, bound_task_run_id=event.task_run_id),
        Disposition.APPLIED,
    )


def _planning_repeat_validation(
    state: PlanningState, envelope: PlanningEnvelope
) -> tuple[PlanningState, Disposition]:
    event = envelope.event
    repeated = (
        isinstance(event, PlanningDraftValidated)
        and envelope.stage == "draft"
        and state.card == event.card
        or isinstance(event, PlanningReviewValidated)
        and envelope.stage == "review"
        and state.review_artifact == event.review
        or isinstance(event, PlanningValidationRejected)
        and state.validation_error == event.detail
    )
    if not repeated:
        raise ValueError("current planning artifact has not been validated")
    return state, Disposition.HISTORY_ONLY


_PLANNING_ACTIVE: dict[type[PlanningEvent], PlanningTransition | None] = {
    event: _planning_runtime
    for event in (SessionStarted, Message, Usage, Completed, Error, CancelRequested)
}
_PLANNING_ACTIVE.update(
    {
        ProcessExited: _planning_exit,
        PlanningReconciled: _planning_reconcile,
        PlanningDiagnostic: _planning_diagnostic,
    }
)
_PLANNING_CLOSED: dict[type[PlanningEvent], PlanningTransition | None] = {
    **dict.fromkeys(
        (SessionStarted, Message, Usage, Completed, Error, CancelRequested)
    ),
    PlanningDraftValidated: _planning_repeat_validation,
    PlanningReviewValidated: _planning_repeat_validation,
    PlanningValidationRejected: _planning_repeat_validation,
    ProcessExited: _planning_exit,
    PlanningReconciled: _planning_reconcile,
    PlanningDiagnostic: _planning_diagnostic,
}
_PLANNING_TRANSITIONS: dict[
    PlanningPhase, dict[type[PlanningEvent], PlanningTransition | None]
] = {
    PlanningPhase.PENDING: {PlanningAttemptStarted: _planning_start},
    PlanningPhase.DRAFTING: _PLANNING_ACTIVE,
    PlanningPhase.REVIEWING: _PLANNING_ACTIVE,
    PlanningPhase.CANCELLING: _PLANNING_ACTIVE,
    PlanningPhase.AWAITING_VALIDATION: {
        **_PLANNING_CLOSED,
        PlanningDraftValidated: _planning_validate,
        PlanningReviewValidated: _planning_validate,
        PlanningValidationRejected: _planning_validate,
    },
    PlanningPhase.AWAITING_REVIEW: {
        **_PLANNING_CLOSED,
        PlanningAttemptStarted: _planning_start,
    },
    PlanningPhase.REVISION_REQUIRED: {
        **_PLANNING_CLOSED,
        PlanningAttemptStarted: _planning_start,
    },
    PlanningPhase.FAILED: {**_PLANNING_CLOSED, PlanningAttemptStarted: _planning_start},
    PlanningPhase.CANCELLED: {
        **_PLANNING_CLOSED,
        PlanningAttemptStarted: _planning_start,
    },
    PlanningPhase.READY: {
        **_PLANNING_CLOSED,
        PlanningAttemptStarted: _planning_start,
        PlanningTaskBound: _planning_bind,
    },
    PlanningPhase.RECONCILIATION_REQUIRED: _PLANNING_CLOSED,
    PlanningPhase.BOUND: {},
}


def apply_planning(
    state: PlanningState, envelope: PlanningEnvelope
) -> tuple[PlanningState, Disposition]:
    """Route current planning attempts, retaining late evidence in the history."""
    if envelope.stage not in ("draft", "review") or envelope.attempt < 1:
        raise ValueError("invalid planning stage or attempt")
    if state.phase == PlanningPhase.BOUND:
        return state, Disposition.HISTORY_ONLY
    current = state.current(envelope.stage)
    if isinstance(envelope.event, PlanningAttemptStarted):
        if current is not None and (
            envelope.attempt < current.number
            or envelope.stage != state.stage
            and envelope.attempt == current.number
        ):
            return state, Disposition.HISTORY_ONLY
        if current is not None and envelope.attempt == current.number:
            raise ValueError("current planning attempt already started")
    else:
        if current is None or envelope.attempt > current.number:
            raise ValueError("planning attempt has not started")
        if envelope.attempt < current.number:
            return state, Disposition.HISTORY_ONLY
        if envelope.stage != state.stage:
            review = state.current("review")
            selected_draft = (
                envelope.stage == "draft"
                and isinstance(envelope.event, PlanningReconciled)
                and review is not None
                and review.request.draft_attempt == current.number
            )
            if selected_draft:
                return _planning_reconcile(state, envelope)
            return state, Disposition.HISTORY_ONLY
    registered = _PLANNING_TRANSITIONS[state.phase]
    if type(envelope.event) not in registered:
        raise ValueError(f"event {envelope.event.kind} is invalid in {state.phase}")
    transition = registered[type(envelope.event)]
    if transition is None:
        return state, Disposition.HISTORY_ONLY
    return transition(state, envelope)
