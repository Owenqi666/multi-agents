"""Transition tables for events addressed to the current attempt."""

from collections.abc import Callable, Mapping
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
from .planning import (
    PlanningAttempt,
    PlanningPhase,
    PlanningState,
    planning_hash,
    planning_value,
)
from .planning_events import (
    PlanningAttemptStarted,
    PlanningDiagnostic,
    PlanningDraftValidated,
    PlanningEnvelope,
    PlanningEnvironmentObserved,
    PlanningEvent,
    PlanningEvidenceRejected,
    PlanningExecutionPrepared,
    PlanningReconciled,
    PlanningReviewValidated,
    PlanningTaskBound,
    PlanningValidationRejected,
)
from .planning_execution import prepared_attempts, prepared_for
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
    TaskCandidateFrozen,
    TaskDiagnostic,
    TaskEnvelope,
    TaskEnvironmentObserved,
    TaskEvent,
    TaskEvidenceRejected,
    TaskExecutionPrepared,
    TaskReconciled,
    TaskReworkStarted,
    TaskScopeBlocked,
)
from .task_execution import (
    prepared_for_task,
    task_execution_hash,
    task_execution_value,
)
from .task_state import TaskAttempt, TaskPhase, TaskState
from .task_test_events import (
    TaskTestAttemptFinished,
    TaskTestAttemptStarted,
    TaskTestCancelRequested,
    TaskTestCommandExited,
    TaskTestCommandReported,
    TaskTestCommandStarted,
    TaskTestEnvelope,
    TaskTestEvent,
    TaskTestExecutionPrepared,
    TaskTestProblem,
    TaskTestReconciled,
)
from .task_test_execution import prepared_for_test
from .task_test_input import TaskTestInput
from .task_test_state import (
    TaskTestAttempt,
    TaskTestCommandState,
    TaskTestPhase,
    TaskTestState,
)
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
    if any(
        isinstance(attempt.request, TaskReworkStarted) for attempt in state.attempts
    ):
        raise ValueError("task has already consumed its only rework")
    if any(attempt.request.managed != event.managed for attempt in state.attempts):
        raise ValueError("managed and legacy task attempts cannot share a chain")
    if event.managed:
        if event.environment is not None:
            raise ValueError("managed task environment must be observed separately")
        for attempt in state.attempts:
            prepared = prepared_for_task(attempt)
            if prepared is None:
                raise ValueError(
                    "managed task retries require a complete preparation chain"
                )
            if envelope.attempt > prepared.policy.max_attempts:
                raise ValueError("managed task attempt budget is exhausted")
    if current is not None and (
        current.reconciliation is None
        or not current.reconciliation.process_stopped
        or event.managed
        and not current.reconciliation.worktree_digest
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


def _task_rework_start(
    state: TaskState, envelope: TaskEnvelope
) -> tuple[TaskState, Disposition]:
    event = envelope.event
    assert isinstance(event, TaskReworkStarted)
    current = state.current_attempt
    candidate = state.current_candidate
    source = event.source
    if (
        current is None
        or not current.request.managed
        or not event.managed
        or not state.candidate_ready
        or candidate is None
        or source.implementation_attempt != current.number
        or source.candidate_commit != candidate.delivery.candidate
        or source.candidate_hash != task_execution_hash(candidate)
        or event.decision.unavailable_models
        != current.request.decision.unavailable_models
    ):
        raise ValueError("task rework source differs from the current candidate")
    return _task_start(state, envelope)


def _task_prepare(
    state: TaskState, envelope: TaskEnvelope
) -> tuple[TaskState, Disposition]:
    event = envelope.event
    assert isinstance(event, TaskExecutionPrepared)
    current = state.attempts[-1]
    if not current.request.managed:
        raise ValueError("legacy tasks cannot adopt managed execution preparation")
    existing = prepared_for_task(current)
    if existing is not None:
        if task_execution_hash(existing) != task_execution_hash(event):
            raise ValueError("task execution preparation is immutable")
        return state, Disposition.HISTORY_ONLY
    if state.phase != TaskPhase.IMPLEMENTING or current.events != (current.request,):
        raise ValueError("task preparation must immediately follow a managed start")
    workspace = event.workspace
    if (
        isinstance(current.request, TaskReworkStarted)
        and workspace.start_commit != current.request.source.candidate_commit
    ):
        raise ValueError("task rework workspace must start from its source candidate")
    expected_provider = {"codex": "codex", "claude-code": "claude"}[
        current.request.decision.choice.provider
    ]
    if (
        workspace.root != current.request.worktree
        or workspace.task_baseline != state.baseline_commit
        or event.policy.provider != expected_provider
    ):
        raise ValueError(
            "task workspace, baseline or runtime provider differs from its request"
        )
    if len(state.attempts) > event.policy.max_attempts:
        raise ValueError("managed task attempt budget is exhausted")
    for previous in state.attempts[:-1]:
        prior = prepared_for_task(previous)
        if not previous.request.managed or prior is None:
            raise ValueError(
                "managed task requires a complete managed preparation chain"
            )
        if task_execution_hash(prior.policy) != task_execution_hash(
            event.policy
        ) or task_execution_hash(prior.dispatch_policy) != task_execution_hash(
            event.dispatch_policy
        ):
            raise ValueError("task execution and dispatch policies are frozen")
        if (
            workspace.root == prior.workspace.root
            or workspace.owner_token == prior.workspace.owner_token
            or workspace.root_identity == prior.workspace.root_identity
            or workspace.git_directory == prior.workspace.git_directory
            or workspace.git_identity == prior.workspace.git_identity
            or event.context.path == prior.context.path
            or event.context.identity == prior.context.identity
            or event.context.token == prior.context.token
        ):
            raise ValueError(
                "task attempts require a fresh workspace owner and context"
            )
    return state, Disposition.APPLIED


def _task_environment(
    state: TaskState, envelope: TaskEnvelope
) -> tuple[TaskState, Disposition]:
    event = envelope.event
    assert isinstance(event, TaskEnvironmentObserved)
    current = state.attempts[-1]
    if not current.request.managed:
        raise ValueError("environment observations require a managed task")
    existing = next(
        (item for item in current.events if isinstance(item, TaskEnvironmentObserved)),
        None,
    )
    if existing is not None:
        if task_execution_hash(existing) != task_execution_hash(event):
            raise ValueError("task environment observation is immutable")
        return state, Disposition.HISTORY_ONLY
    if (
        state.phase != TaskPhase.IMPLEMENTING
        or prepared_for_task(current) is None
        or any(
            not isinstance(
                item, (TaskAttemptStarted, TaskExecutionPrepared, TaskDiagnostic)
            )
            for item in current.events
        )
    ):
        raise ValueError(
            "task environment must precede runtime, cancellation and reconciliation"
        )
    return state, Disposition.APPLIED


def _task_reject_evidence(
    state: TaskState, envelope: TaskEnvelope
) -> tuple[TaskState, Disposition]:
    event = envelope.event
    assert isinstance(event, TaskEvidenceRejected)
    current = state.attempts[-1]
    if not current.request.managed:
        raise ValueError("execution evidence rejection requires a managed task")
    existing = next(
        (item for item in current.events if isinstance(item, TaskEvidenceRejected)),
        None,
    )
    if existing is not None:
        if existing != event:
            raise ValueError("task execution evidence rejection is immutable")
        return state, Disposition.HISTORY_ONLY
    if state.phase != TaskPhase.AWAITING_DELIVERY:
        raise ValueError("task execution evidence rejection requires awaiting delivery")
    return replace(state, phase=TaskPhase.FAILED), Disposition.APPLIED


def _task_diagnostic(
    state: TaskState, envelope: TaskEnvelope
) -> tuple[TaskState, Disposition]:
    disposition = (
        Disposition.APPLIED
        if state.attempts[-1].request.managed
        else Disposition.HISTORY_ONLY
    )
    return state, disposition


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
    if state.phase == TaskPhase.IMPLEMENTING and not current.request.managed:
        raise ValueError(f"event {event.kind} is invalid in task phase {state.phase}")
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
    if (
        state.phase == TaskPhase.PLANNING_REQUIRED
        and not state.attempts[-1].request.managed
    ):
        return state, Disposition.HISTORY_ONLY
    if (event.log_path is None) != (event.log_sha256 is None):
        raise ValueError("task execution log receipt requires both path and hash")
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
    if state.current_candidate is not None:
        raise ValueError("a frozen task candidate cannot become a scope handback")
    if not state.delivery_ready:
        raise ValueError(
            "scope handback requires a successful terminal and actual exit"
        )
    return (
        replace(state, phase=TaskPhase.PLANNING_REQUIRED, scope_block=event),
        Disposition.APPLIED,
    )


def _task_candidate(
    state: TaskState, envelope: TaskEnvelope
) -> tuple[TaskState, Disposition]:
    event = envelope.event
    assert isinstance(event, TaskCandidateFrozen)
    existing = state.current_candidate
    if existing is not None:
        if existing != event:
            raise ValueError("task candidate is immutable")
        return state, Disposition.HISTORY_ONLY
    current = state.attempts[-1]
    prepared = prepared_for_task(current)
    if (
        not current.request.managed
        or prepared is None
        or state.phase != TaskPhase.AWAITING_DELIVERY
        or not state.delivery_ready
    ):
        raise ValueError("task candidate requires a qualified managed delivery")
    reconciliation = current.reconciliation
    assert reconciliation is not None
    delivery = event.delivery
    if (
        delivery.baseline != state.baseline_commit
        or delivery.baseline != prepared.workspace.task_baseline
        or delivery.digest != reconciliation.worktree_digest
        or not event.execution_log_path
        or not event.execution_log_sha256
        or event.execution_log_path != reconciliation.log_path
        or event.execution_log_sha256 != reconciliation.log_sha256
        or delivery.ref
        != f"refs/multi-agent/candidates/{prepared.workspace.owner_token}"
    ):
        raise ValueError("task candidate does not match its execution and workspace")
    return state, Disposition.APPLIED


_TASK_ACTIVE: dict[type[TaskEvent], TaskTransition | None] = {
    SessionStarted: _task_runtime,
    Message: _task_runtime,
    Usage: _task_runtime,
    Completed: _task_runtime,
    Error: _task_runtime,
    CancelRequested: _task_runtime,
    ProcessExited: _task_exit,
    TaskReconciled: _task_reconcile,
    TaskDiagnostic: _task_diagnostic,
    TaskExecutionPrepared: _task_prepare,
    TaskEnvironmentObserved: _task_environment,
    TaskEvidenceRejected: _task_reject_evidence,
    TaskCandidateFrozen: _task_candidate,
}
_TASK_CLOSED: dict[type[TaskEvent], TaskTransition | None] = {
    SessionStarted: None,
    Message: None,
    Usage: None,
    Completed: None,
    Error: None,
    CancelRequested: None,
    ProcessExited: _task_exit,
    TaskDiagnostic: _task_diagnostic,
    TaskReconciled: _task_reconcile,
    TaskExecutionPrepared: _task_prepare,
    TaskEnvironmentObserved: _task_environment,
    TaskEvidenceRejected: _task_reject_evidence,
    TaskCandidateFrozen: _task_candidate,
}
_TASK_TRANSITIONS: dict[TaskPhase, dict[type[TaskEvent], TaskTransition | None]] = {
    TaskPhase.READY: {TaskAttemptStarted: _task_start},
    TaskPhase.IMPLEMENTING: _TASK_ACTIVE,
    TaskPhase.CANCELLING: {**_TASK_ACTIVE, ProcessExited: _task_exit},
    TaskPhase.AWAITING_DELIVERY: {
        **_TASK_CLOSED,
        TaskScopeBlocked: _task_scope_block,
        TaskReworkStarted: _task_rework_start,
    },
    TaskPhase.FAILED: {**_TASK_CLOSED, TaskAttemptStarted: _task_start},
    TaskPhase.CANCELLED: {**_TASK_CLOSED, TaskAttemptStarted: _task_start},
    TaskPhase.PLANNING_REQUIRED: {
        **dict.fromkeys(_TASK_CLOSED),
        TaskScopeBlocked: None,
        TaskReconciled: _task_reconcile,
        TaskDiagnostic: _task_diagnostic,
        TaskExecutionPrepared: _task_prepare,
        TaskEnvironmentObserved: _task_environment,
        TaskEvidenceRejected: _task_reject_evidence,
        TaskCandidateFrozen: _task_candidate,
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
    if (
        current is not None
        and current.request.managed
        and not isinstance(
            envelope.event,
            (TaskAttemptStarted, TaskExecutionPrepared, TaskCandidateFrozen),
        )
        and prepared_for_task(current) is None
    ):
        raise ValueError("managed task events require execution preparation")
    registered = _TASK_TRANSITIONS[state.phase]
    if type(envelope.event) not in registered:
        raise ValueError(
            f"event {envelope.event.kind} is invalid in task phase {state.phase}"
        )
    transition = registered[type(envelope.event)]
    if transition is None:
        return state, Disposition.HISTORY_ONLY
    updated, disposition = transition(state, envelope)
    selected = updated.current_attempt
    if (
        disposition == Disposition.APPLIED
        and selected is not None
        and selected.request.managed
    ):
        updated = replace(
            updated,
            attempts=updated.attempts[:-1]
            + (replace(selected, events=selected.events + (envelope.event,)),),
        )
    return updated, disposition


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


def _planning_prepare(
    state: PlanningState, envelope: PlanningEnvelope
) -> tuple[PlanningState, Disposition]:
    """Reserve immutable host inputs before any runtime observation exists."""
    event = envelope.event
    assert isinstance(event, PlanningExecutionPrepared)
    current = state.current(envelope.stage)
    assert current is not None
    existing = prepared_for(current)
    if existing is not None:
        if planning_hash(existing) != planning_hash(event):
            raise ValueError("current planning preparation is immutable")
        return state, Disposition.HISTORY_ONLY
    if (
        state.phase not in (PlanningPhase.DRAFTING, PlanningPhase.REVIEWING)
        or current.events != (current.request,)
        or current.request.environment is not None
    ):
        raise ValueError("planning preparation must immediately follow a fresh start")
    previous_attempts = tuple(
        attempt
        for stage in ("draft", "review")
        for attempt in state.attempts(stage)
        if not (stage == envelope.stage and attempt.number == envelope.attempt)
    )
    if any(prepared_for(attempt) is None for attempt in previous_attempts):
        raise ValueError("legacy planning attempts have no complete preparation chain")
    previous = prepared_attempts(state)
    if previous:
        first = previous[0]
        if (
            event.source_snapshot_id != first.source_snapshot_id
            or planning_hash(event.policy) != planning_hash(first.policy)
            or event.run_owner != first.run_owner
        ):
            raise ValueError("planning source, policy and run owner are frozen")
    selected = event.policy.for_provider(current.request.request.provider)
    if len(state.attempts(envelope.stage)) > selected.max_attempts:
        raise ValueError("planning provider attempt budget is exhausted")
    context = event.attempt_context
    if any(
        context.path == item.attempt_context.path
        or context.identity == item.attempt_context.identity
        or context.token == item.attempt_context.token
        for item in previous
    ):
        raise ValueError("planning attempts require a fresh owned context")
    return _planning_record(state, envelope, current), Disposition.APPLIED


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


def _planning_environment(
    state: PlanningState, envelope: PlanningEnvelope
) -> tuple[PlanningState, Disposition]:
    event = envelope.event
    assert isinstance(event, PlanningEnvironmentObserved)
    current = state.current(envelope.stage)
    assert current is not None
    existing = next(
        (
            item
            for item in current.events
            if isinstance(item, PlanningEnvironmentObserved)
        ),
        None,
    )
    if existing is not None:
        if planning_hash(existing) != planning_hash(event):
            raise ValueError("planning environment observation is immutable")
        return state, Disposition.HISTORY_ONLY
    if (
        state.phase not in (PlanningPhase.DRAFTING, PlanningPhase.REVIEWING)
        or prepared_for(current) is None
        or any(
            not isinstance(
                item,
                (PlanningAttemptStarted, PlanningExecutionPrepared, PlanningDiagnostic),
            )
            for item in current.events
        )
    ):
        raise ValueError(
            "planning environment requires preparation before runtime or cancellation"
        )
    return _planning_record(state, envelope, current), Disposition.APPLIED


def _planning_reject_evidence(
    state: PlanningState, envelope: PlanningEnvelope
) -> tuple[PlanningState, Disposition]:
    event = envelope.event
    assert isinstance(event, PlanningEvidenceRejected)
    current = state.current(envelope.stage)
    assert current is not None
    existing = next(
        (item for item in current.events if isinstance(item, PlanningEvidenceRejected)),
        None,
    )
    if existing is not None:
        if existing != event:
            raise ValueError("planning evidence rejection is immutable")
        return state, Disposition.HISTORY_ONLY
    if state.phase != PlanningPhase.AWAITING_VALIDATION:
        raise ValueError("planning evidence rejection requires awaiting validation")
    updated = replace(state, phase=PlanningPhase.FAILED, validation_error=event.detail)
    return _planning_record(updated, envelope, current), Disposition.APPLIED


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
    current = state.current(envelope.stage)
    assert current is not None
    repeated = (
        isinstance(event, PlanningDraftValidated)
        and envelope.stage == "draft"
        and state.card == event.card
        or isinstance(event, PlanningReviewValidated)
        and envelope.stage == "review"
        and state.review_artifact == event.review
        or isinstance(event, PlanningValidationRejected)
        and state.validation_error == event.detail
        and any(
            isinstance(item, PlanningValidationRejected) and item == event
            for item in current.events
        )
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
        PlanningExecutionPrepared: _planning_prepare,
        ProcessExited: _planning_exit,
        PlanningReconciled: _planning_reconcile,
        PlanningDiagnostic: _planning_diagnostic,
        PlanningEnvironmentObserved: _planning_environment,
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
    PlanningExecutionPrepared: _planning_prepare,
    PlanningEnvironmentObserved: _planning_environment,
    PlanningEvidenceRejected: _planning_reject_evidence,
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


def _task_test_stopped(attempt: TaskTestAttempt) -> bool:
    current = attempt.current_command
    receipt = attempt.reconciliation
    return (
        receipt is not None
        and receipt.sequence == (current.sequence if current is not None else None)
        and receipt.process_stopped is True
    )


def _task_test_receipt_ready(receipt: TaskTestReconciled | None) -> bool:
    return (
        receipt is not None
        and receipt.process_stopped is True
        and receipt.evidence_qualified is True
        and bool(receipt.receipt_path)
        and bool(receipt.receipt_sha256)
    )


def task_test_phase(attempt: TaskTestAttempt | None) -> TaskTestPhase:
    """One derivation for both a pure fold and reconstructed persisted facts."""
    if attempt is None:
        return TaskTestPhase.READY
    if attempt.cancel_requested:
        return (
            TaskTestPhase.CANCELLED
            if attempt.finished and _task_test_stopped(attempt)
            else TaskTestPhase.CANCELLING
        )
    if not attempt.finished:
        return TaskTestPhase.RUNNING
    if attempt.problems or any(
        command.problems or command.exit_code is not None and command.exit_code != 0
        for command in attempt.commands
    ):
        return TaskTestPhase.FAILED
    if (
        bool(attempt.input.commands)
        and len(attempt.commands) == len(attempt.input.commands)
        and all(
            command.sequence == number
            and command.exit_code == 0
            and _task_test_receipt_ready(command.reconciliation)
            and command.reconciliation is not None
            and command.reconciliation.sequence == number
            for number, command in enumerate(attempt.commands, 1)
        )
        and _task_test_stopped(attempt)
        and _task_test_receipt_ready(attempt.reconciliation)
    ):
        return TaskTestPhase.SUCCEEDED
    return TaskTestPhase.AWAITING_EVIDENCE


def _task_test_source(
    state: TaskTestState, value: TaskTestInput, implementation: TaskState
) -> bool:
    current = implementation.current_attempt
    return (
        current is not None
        and current.number == state.implementation_attempt
        and isinstance(value.implementation_attempt, int)
        and not isinstance(value.implementation_attempt, bool)
        and value.implementation_attempt == state.implementation_attempt
        and value.task_run_id == state.task_run_id
        and value.task_id == implementation.task_id
        and value.task_hash == implementation.task_hash
        and value.rules_hash == implementation.rules_hash
        and implementation.candidate_ready
        and task_execution_hash(value.candidate)
        == task_execution_hash(implementation.current_candidate)
    )


def task_test_ready(state: TaskTestState, implementation: TaskState) -> bool:
    """A current-state gate, never actual archive verification or launch rights."""
    current = state.current_attempt
    return (
        current is not None
        and state.phase == TaskTestPhase.SUCCEEDED
        and task_test_phase(current) == TaskTestPhase.SUCCEEDED
        and _task_test_source(state, current.input, implementation)
    )


def _task_test_semantics(value: TaskTestInput) -> str:
    semantic = task_execution_value(value)
    del semantic["task_history_length"]
    del semantic["task_history_hash"]
    return task_execution_hash(semantic)


def _task_test_replace(state: TaskTestState, attempt: TaskTestAttempt) -> TaskTestState:
    return replace(
        state,
        attempts=state.attempts[:-1] + (attempt,),
        phase=task_test_phase(attempt),
    )


type TaskTestTransition = Callable[
    [TaskTestState, TaskTestEnvelope, TaskState], tuple[TaskTestState, Disposition]
]


def _task_test_start(
    state: TaskTestState, envelope: TaskTestEnvelope, implementation: TaskState
) -> tuple[TaskTestState, Disposition]:
    event = envelope.event
    assert isinstance(event, TaskTestAttemptStarted)
    if not _task_test_source(state, event.input, implementation):
        raise ValueError("test reservation does not identify the current candidate")
    if _task_test_semantics(event.input) != _task_test_semantics(state.reference_input):
        raise ValueError("test attempt changes the stage's frozen semantic input")
    current = state.current_attempt
    if current is not None and (
        not current.finished
        or task_test_phase(current)
        not in (
            TaskTestPhase.FAILED,
            TaskTestPhase.CANCELLED,
            TaskTestPhase.AWAITING_EVIDENCE,
        )
        or not _task_test_stopped(current)
    ):
        raise ValueError("test retry requires logical completion and actual settlement")
    attempt = TaskTestAttempt(envelope.test_attempt, event.input)
    return (
        replace(
            state, attempts=state.attempts + (attempt,), phase=task_test_phase(attempt)
        ),
        Disposition.APPLIED,
    )


def _task_test_command_start(
    state: TaskTestState, envelope: TaskTestEnvelope, implementation: TaskState
) -> tuple[TaskTestState, Disposition]:
    event = envelope.event
    assert isinstance(event, TaskTestCommandStarted)
    current = state.current_attempt
    assert current is not None
    if any(prepared_for_test(attempt) is not None for attempt in state.attempts) and (
        prepared_for_test(current) is None
    ):
        raise ValueError("managed test commands require their committed preparation")
    if (
        not _task_test_source(state, current.input, implementation)
        or current.finished
        or current.cancel_requested
        or current.problems
        or event.sequence != len(current.commands) + 1
        or event.sequence > len(current.input.commands)
    ):
        raise ValueError("test command is not an authorized next command")
    previous = current.current_command
    if previous is not None:
        if (
            previous.exit_code is None
            and not previous.problems
            or previous.reconciliation is None
            or previous.reconciliation.process_stopped is not True
            or not _task_test_stopped(current)
        ):
            raise ValueError("previous test command has not actually settled")
    elif current.reconciliation is not None and not _task_test_stopped(current):
        raise ValueError("test preflight resources have not actually settled")
    return (
        _task_test_replace(
            state,
            replace(
                current,
                commands=current.commands + (TaskTestCommandState(event.sequence),),
                reconciliation=None,
            ),
        ),
        Disposition.APPLIED,
    )


def _task_test_owned(
    prepared: TaskTestExecutionPrepared,
) -> tuple[set[str], set[tuple[int, int]], set[str]]:
    checkout, context, scratch = prepared.checkout, prepared.context, prepared.scratch
    return (
        {
            checkout.root,
            checkout.git_directory,
            context.path,
            scratch.path,
            scratch.home,
            scratch.tmp,
            scratch.cache,
        },
        {
            checkout.root_identity,
            checkout.git_identity,
            context.identity,
            scratch.identity,
            scratch.home_identity,
            scratch.tmp_identity,
            scratch.cache_identity,
            *(
                control.identity
                for control in checkout.controls
                if control.identity is not None
            ),
        },
        {checkout.owner_token, context.token},
    )


def _task_test_prepare(
    state: TaskTestState, envelope: TaskTestEnvelope, implementation: TaskState
) -> tuple[TaskTestState, Disposition]:
    event = envelope.event
    assert isinstance(event, TaskTestExecutionPrepared)
    current = state.current_attempt
    assert current is not None
    existing = prepared_for_test(current)
    if existing is not None:
        if task_execution_hash(existing) == task_execution_hash(event):
            return state, Disposition.HISTORY_ONLY
        raise ValueError("test attempt already has a different preparation")
    if (
        len(current.events) != 1
        or not isinstance(current.events[0], TaskTestAttemptStarted)
        or task_execution_hash(event.checkout.input)
        != task_execution_hash(current.input)
        or event.checkout.observed_test_attempt
        != (state.attempts[-2].number if len(state.attempts) > 1 else None)
        or not _task_test_source(state, current.input, implementation)
    ):
        raise ValueError("test preparation must follow its exact current Started")
    paths, identities, tokens = _task_test_owned(event)
    for attempt in state.attempts[:-1]:
        previous = prepared_for_test(attempt)
        if previous is None:
            raise ValueError("legacy test stages cannot become managed")
        if task_execution_hash(previous.policy) != task_execution_hash(event.policy):
            raise ValueError("managed test policy cannot change on retry")
        old_paths, old_identities, old_tokens = _task_test_owned(previous)
        if paths & old_paths or identities & old_identities or tokens & old_tokens:
            raise ValueError("managed retry must own fresh test resources")
    return state, Disposition.APPLIED


def _task_test_report(
    state: TaskTestState, envelope: TaskTestEnvelope, implementation: TaskState
) -> tuple[TaskTestState, Disposition]:
    event = envelope.event
    assert isinstance(event, TaskTestCommandReported)
    current = state.current_attempt
    assert current is not None
    response = event.response
    if (
        type(event.reported_exit_code) is not int
        or not isinstance(response, Mapping)
        or type(response.get("exitCode")) is not int
        or response["exitCode"] != event.reported_exit_code
        or not isinstance(response.get("stdout"), str)
        or not isinstance(response.get("stderr"), str)
    ):
        raise ValueError("test report must preserve its matching result exit code")
    for previous in current.events:
        if (
            isinstance(previous, TaskTestCommandReported)
            and previous.sequence == event.sequence
        ):
            if task_execution_hash(previous) == task_execution_hash(event):
                return state, Disposition.HISTORY_ONLY
            raise ValueError("test command already has another final report")
    return state, Disposition.APPLIED


def _task_test_exit(
    state: TaskTestState, envelope: TaskTestEnvelope, implementation: TaskState
) -> tuple[TaskTestState, Disposition]:
    event = envelope.event
    assert isinstance(event, TaskTestCommandExited)
    current = state.current_attempt
    assert current is not None and current.current_command is not None
    command = current.current_command
    if not isinstance(event.exit_code, int) or isinstance(event.exit_code, bool):
        raise TypeError("test exit code must be an actual integer")
    if command.exit_code is not None:
        if event.exit_code == command.exit_code:
            return state, Disposition.HISTORY_ONLY
        raise ValueError("test command already has another actual exit code")
    return (
        _task_test_replace(
            state,
            replace(
                current,
                commands=current.commands[:-1]
                + (replace(command, exit_code=event.exit_code),),
            ),
        ),
        Disposition.APPLIED,
    )


def _task_test_problem(
    state: TaskTestState, envelope: TaskTestEnvelope, implementation: TaskState
) -> tuple[TaskTestState, Disposition]:
    event = envelope.event
    assert isinstance(event, TaskTestProblem)
    current = state.current_attempt
    assert current is not None
    command = current.current_command
    problems = (
        current.problems
        if event.sequence is None
        else command.problems
        if command
        else ()
    )
    if any(
        task_execution_hash(item) == task_execution_hash(event) for item in problems
    ):
        return state, Disposition.HISTORY_ONLY
    if event.sequence is None:
        updated = replace(current, problems=current.problems + (event,))
    else:
        assert command is not None
        updated = replace(
            current,
            commands=current.commands[:-1]
            + (replace(command, problems=command.problems + (event,)),),
        )
    return _task_test_replace(state, updated), Disposition.APPLIED


def _task_test_reconcile(
    state: TaskTestState, envelope: TaskTestEnvelope, implementation: TaskState
) -> tuple[TaskTestState, Disposition]:
    event = envelope.event
    assert isinstance(event, TaskTestReconciled)
    current = state.current_attempt
    assert current is not None
    if (
        event.process_stopped is not None
        and not isinstance(event.process_stopped, bool)
        or not isinstance(event.evidence_qualified, bool)
        or (event.receipt_path is None) != (event.receipt_sha256 is None)
        or event.evidence_qualified
        and (not event.receipt_path or not event.receipt_sha256)
    ):
        raise ValueError("test reconciliation has inconsistent evidence facts")
    if task_execution_hash(current.reconciliation) == task_execution_hash(event):
        return state, Disposition.HISTORY_ONLY
    commands = current.commands
    if event.sequence is not None:
        command = current.current_command
        assert command is not None
        commands = commands[:-1] + (replace(command, reconciliation=event),)
    return (
        _task_test_replace(
            state, replace(current, commands=commands, reconciliation=event)
        ),
        Disposition.APPLIED,
    )


def _task_test_cancel(
    state: TaskTestState, envelope: TaskTestEnvelope, implementation: TaskState
) -> tuple[TaskTestState, Disposition]:
    current = state.current_attempt
    assert current is not None
    if current.cancel_requested or (
        current.finished and task_test_phase(current) != TaskTestPhase.AWAITING_EVIDENCE
    ):
        return state, Disposition.HISTORY_ONLY
    return _task_test_replace(
        state, replace(current, cancel_requested=True)
    ), Disposition.APPLIED


def _task_test_finish(
    state: TaskTestState, envelope: TaskTestEnvelope, implementation: TaskState
) -> tuple[TaskTestState, Disposition]:
    current = state.current_attempt
    assert current is not None
    if current.finished:
        return state, Disposition.HISTORY_ONLY
    return _task_test_replace(
        state, replace(current, finished=True)
    ), Disposition.APPLIED


_TASK_TEST_OBSERVATIONS: dict[type[TaskTestEvent], TaskTestTransition] = {
    TaskTestExecutionPrepared: _task_test_prepare,
    TaskTestCommandReported: _task_test_report,
    TaskTestCommandExited: _task_test_exit,
    TaskTestProblem: _task_test_problem,
    TaskTestReconciled: _task_test_reconcile,
    TaskTestCancelRequested: _task_test_cancel,
    TaskTestAttemptFinished: _task_test_finish,
}
_TASK_TEST_TRANSITIONS: dict[
    TaskTestPhase, dict[type[TaskTestEvent], TaskTestTransition]
] = {
    TaskTestPhase.READY: {TaskTestAttemptStarted: _task_test_start},
    TaskTestPhase.RUNNING: {
        **_TASK_TEST_OBSERVATIONS,
        TaskTestCommandStarted: _task_test_command_start,
    },
    TaskTestPhase.CANCELLING: _TASK_TEST_OBSERVATIONS,
    TaskTestPhase.SUCCEEDED: _TASK_TEST_OBSERVATIONS,
    TaskTestPhase.AWAITING_EVIDENCE: {
        **_TASK_TEST_OBSERVATIONS,
        TaskTestAttemptStarted: _task_test_start,
    },
    TaskTestPhase.FAILED: {
        **_TASK_TEST_OBSERVATIONS,
        TaskTestAttemptStarted: _task_test_start,
    },
    TaskTestPhase.CANCELLED: {
        **_TASK_TEST_OBSERVATIONS,
        TaskTestAttemptStarted: _task_test_start,
    },
}


def _task_test_number(value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("test coordinates must be positive integers")


def apply_task_test(
    state: TaskTestState, envelope: TaskTestEnvelope, implementation: TaskState
) -> tuple[TaskTestState, Disposition]:
    return _apply_task_test(state, envelope, implementation)


def _apply_task_test(
    state: TaskTestState,
    envelope: TaskTestEnvelope,
    implementation: TaskState,
    *,
    managed_reservation: bool = False,
) -> tuple[TaskTestState, Disposition]:
    """Keep old observations, but never reinterpret stale controller requests."""
    _task_test_number(envelope.implementation_attempt)
    _task_test_number(envelope.test_attempt)
    if envelope.implementation_attempt != state.implementation_attempt:
        raise ValueError("test envelope addresses another implementation stage")
    actual = implementation.current_attempt
    if actual is None or not any(
        item.number == state.implementation_attempt for item in implementation.attempts
    ):
        raise ValueError("test implementation attempt has not started")
    event = envelope.event
    if isinstance(
        event, (TaskTestCommandStarted, TaskTestCommandExited, TaskTestCommandReported)
    ) or (
        isinstance(event, (TaskTestProblem, TaskTestReconciled))
        and event.sequence is not None
    ):
        _task_test_number(event.sequence)
    current = state.current_attempt
    if isinstance(event, (TaskTestAttemptStarted, TaskTestCommandStarted)):
        if state.implementation_attempt != actual.number:
            raise ValueError("cannot reserve tests for an old implementation")
        next_number = 1 if current is None else current.number + 1
        if isinstance(event, TaskTestAttemptStarted):
            if not managed_reservation and any(
                prepared_for_test(attempt) is not None for attempt in state.attempts
            ):
                raise ValueError(
                    "managed test stages require an atomic preparation pair"
                )
            if envelope.test_attempt != next_number:
                raise ValueError(
                    "test attempt reservation is not the unique next number"
                )
        elif current is None or envelope.test_attempt != current.number:
            raise ValueError(
                "test command reservation requires the current test attempt"
            )
    else:
        selected = next(
            (item for item in state.attempts if item.number == envelope.test_attempt),
            None,
        )
        if selected is None:
            raise ValueError("test attempt has not started")
        if isinstance(
            event,
            (
                TaskTestCommandExited,
                TaskTestCommandReported,
                TaskTestProblem,
                TaskTestReconciled,
            ),
        ):
            sequence = event.sequence
            if sequence is not None and not any(
                item.sequence == sequence for item in selected.commands
            ):
                raise ValueError("test command has not started")
            old_epoch = (
                selected.current_command is not None
                and sequence != selected.current_command.sequence
            )
        else:
            old_epoch = False
        if (
            state.implementation_attempt < actual.number
            or selected != current
            or old_epoch
        ):
            return state, Disposition.HISTORY_ONLY
    if (
        isinstance(event, TaskTestExecutionPrepared)
        and not managed_reservation
        and current is not None
        and prepared_for_test(current) is None
    ):
        raise ValueError("new test preparations require an atomic reservation")
    registered = _TASK_TEST_TRANSITIONS[state.phase]
    if type(event) not in registered:
        raise ValueError(f"event {event.kind} is invalid in test phase {state.phase}")
    updated, disposition = registered[type(event)](state, envelope, implementation)
    if disposition == Disposition.APPLIED:
        attempt = updated.current_attempt
        assert attempt is not None
        updated = _task_test_replace(
            updated, replace(attempt, events=attempt.events + (event,))
        )
    return updated, disposition
