from dataclasses import replace

import pytest

from multi_agent.core import (
    AttemptState,
    AttemptStatus,
    Disposition,
    RoundState,
    SeatConfig,
    SeatState,
    apply,
)
from multi_agent.core.dispatch import resolve_dispatch
from multi_agent.core.events import (
    AttemptStarted,
    CancelRequested,
    Completed,
    Envelope,
    Error,
    FailureCategory,
    ProcessExited,
    SessionStarted,
)
from multi_agent.core.task_events import (
    TaskAttemptStarted,
    TaskDiagnostic,
    TaskEnvelope,
    TaskReconciled,
    TaskScopeBlocked,
)
from multi_agent.core.task_state import TaskPhase, TaskState
from multi_agent.core.tasks import (
    FrozenProjectRules,
    ImplementationConfig,
    ImplementationRule,
    ProjectRules,
    TaskCard,
)
from multi_agent.core.transitions import apply_task


def inputs():
    card = TaskCard.from_dict(
        {
            "schema_version": 1,
            "id": "T-0100",
            "goal": "Implement the specified behavior",
            "baseline_commit": "a" * 40,
            "write_scope": ["src/**"],
            "depends_on": [],
            "interface_contracts": [],
            "deliverables": ["The implementation"],
            "acceptance_criteria": ["Configured tests pass"],
            "test_refs": [],
            "open_questions": [],
        }
    )
    config = ImplementationConfig(
        "codex", "requested-model", "high", ("requested-model",)
    )
    rules = FrozenProjectRules(
        "/repository/.git",
        card.baseline_commit,
        "b" * 40,
        "frozen source bytes",
        ProjectRules(1, (), (), (ImplementationRule(("src/**",), config),), None),
    )
    decision = resolve_dispatch(card, rules)
    state = TaskState(card.id, card.task_hash, rules.rules_hash, card.baseline_commit)
    start = TaskAttemptStarted(
        decision, "/owned/worktree", "implementation-v1", "fixture-time"
    )
    return state, start


def send(state, event, number=1):
    return apply_task(state, TaskEnvelope(number, event))


def started():
    state, start = inputs()
    state, disposition = send(state, start)
    assert disposition == Disposition.APPLIED
    return state, start


def test_late_implementation_result_is_history_only():
    state, start = started()
    state, _ = send(state, Error("retryable failure"))
    state, _ = send(
        state, TaskReconciled("tree-1", True, "owned process stopped; tree inspected")
    )
    state, _ = send(state, start, 2)
    current = state
    for event in (
        start,
        SessionStarted("old-session", reported_model="old-model"),
        Completed({"old": True}, reported_model="old-model"),
        Error("old failure", FailureCategory.AUTH_EXPIRED),
        ProcessExited(0),
        TaskReconciled("old-tree", True, "old recovery"),
        TaskScopeBlocked("old scope", ("outside.py",)),
        TaskDiagnostic(Error("old diagnostic")),
        CancelRequested(),
    ):
        state, disposition = send(state, event)
        assert disposition == Disposition.HISTORY_ONLY
        assert state is current
    assert state.current_attempt.number == 2
    assert state.current_attempt.runtime.reported_model is None


def test_restart_requires_attempt_and_workspace_reconciliation():
    state, start = started()
    with pytest.raises(ValueError):
        send(state, start, 2)
    state, _ = send(state, TaskReconciled("inspected", False, "still running"))
    assert state.phase == TaskPhase.IMPLEMENTING
    with pytest.raises(ValueError):
        send(state, start, 2)
    state, _ = send(state, TaskReconciled("inspected", True, "owned process stopped"))
    assert state.phase == TaskPhase.FAILED
    assert state.current_attempt.runtime.status == AttemptStatus.RUNNING
    assert state.current_attempt.runtime.failure_category is None
    assert state.current_attempt.runtime.exit_code is None
    state, _ = send(state, start, 2)
    assert state.phase == TaskPhase.IMPLEMENTING
    assert state.current_attempt.number == 2
    assert state.attempts[0].reconciliation.process_stopped is True


def test_latest_reconciliation_can_revoke_retry_permission():
    state, start = started()
    state, _ = send(state, Error("failed"))
    state, _ = send(state, TaskReconciled("first", True, "stopped"))
    state, _ = send(
        state, TaskReconciled("second", False, "new inspection is not stopped")
    )
    assert state.current_attempt.reconciliation.worktree_digest == "second"
    with pytest.raises(ValueError):
        send(state, start, 2)
    state, _ = send(state, TaskReconciled("third", True, "stopped and inspected again"))
    state, _ = send(state, start, 2)
    assert state.current_attempt.number == 2


@pytest.mark.parametrize(
    "terminal", [Completed({"result": "after cancel"}), Error("stopped")]
)
def test_task_cancellation_preserves_terminal_and_exit(terminal):
    state, _ = started()
    state, _ = send(state, CancelRequested())
    assert state.phase == TaskPhase.CANCELLING
    diagnostic = TaskDiagnostic(
        Error("stop unconfirmed", raw_type="adapter.cleanup_incomplete")
    )
    unchanged, disposition = send(state, diagnostic)
    assert unchanged is state
    assert disposition == Disposition.HISTORY_ONLY
    state, _ = send(state, terminal)
    assert state.phase == TaskPhase.CANCELLED
    assert state.current_attempt.runtime.failure_category == FailureCategory.CANCELLED
    assert state.current_attempt.runtime.exit_code is None
    state, _ = send(state, ProcessExited(-2))
    assert state.current_attempt.runtime.exit_code == -2
    assert not state.delivery_ready
    unchanged, disposition = send(state, Completed({"late": True}))
    assert unchanged is state and disposition == Disposition.HISTORY_ONLY
    unchanged, disposition = send(state, ProcessExited(0))
    assert unchanged is state and disposition == Disposition.HISTORY_ONLY


def test_cancel_requires_a_terminal_or_confirmed_process_stop():
    state, start = started()
    state, _ = send(state, CancelRequested())
    state, _ = send(state, TaskReconciled("tree", False, "stop unknown"))
    assert state.phase == TaskPhase.CANCELLING
    state, _ = send(state, TaskReconciled("tree", True, "stop observed by boundary"))
    assert state.phase == TaskPhase.CANCELLED
    assert state.current_attempt.runtime.status == AttemptStatus.CANCELLING
    assert state.current_attempt.runtime.exit_code is None
    state, _ = send(state, start, 2)
    assert state.current_attempt.number == 2


def test_cancellation_can_finish_from_exit_before_model_terminal():
    state, _ = started()
    state, _ = send(state, CancelRequested())
    state, _ = send(state, ProcessExited(0))
    assert state.phase == TaskPhase.CANCELLED
    assert state.current_attempt.runtime.failure_category == FailureCategory.CANCELLED


@pytest.mark.parametrize("code", [0, -15, 17])
def test_task_success_requires_exit_for_delivery(code):
    state, start = started()
    result = {"changed": ["src/new.py"]}
    state, _ = send(state, Completed(result, reported_model="actual-model"))
    result["changed"].append("mutated.py")
    assert state.phase == TaskPhase.AWAITING_DELIVERY
    assert not state.delivery_ready
    assert state.current_attempt.runtime.result == {"changed": ("src/new.py",)}
    state, _ = send(state, ProcessExited(code))
    assert state.phase == TaskPhase.AWAITING_DELIVERY
    assert state.delivery_ready
    assert state.current_attempt.runtime.exit_code == code
    assert state.current_attempt.runtime.reported_model == "actual-model"
    with pytest.raises(ValueError):
        send(state, start, 2)
    unchanged, disposition = send(state, ProcessExited(code + 1))
    assert unchanged is state and disposition == Disposition.HISTORY_ONLY
    state, _ = send(state, TaskReconciled("tree", False, "stop evidence now uncertain"))
    assert not state.delivery_ready
    state, _ = send(state, TaskReconciled("tree", True, "reconciled"))
    assert state.delivery_ready


def test_scope_block_requires_replanning():
    state, start = started()
    blocked = TaskScopeBlocked("requires another file", ("outside.py",))
    with pytest.raises(ValueError):
        send(state, blocked)
    state, _ = send(state, Completed({"status": "scope_blocked"}))
    with pytest.raises(ValueError):
        send(state, blocked)
    state, _ = send(state, ProcessExited(-15))
    state, _ = send(state, blocked)
    assert state.phase == TaskPhase.PLANNING_REQUIRED
    assert state.scope_block == blocked
    assert not state.delivery_ready
    with pytest.raises(ValueError):
        send(state, start, 2)
    unchanged, disposition = send(state, TaskScopeBlocked("changed", ("other.py",)))
    assert unchanged is state and disposition == Disposition.HISTORY_ONLY


def test_task_inputs_and_request_evidence_are_frozen():
    state, start = inputs()
    environment = {"cli_version": "fixture-1", "reported_effort": None}
    start = replace(start, environment=environment)
    environment["cli_version"] = "mutated"
    state, _ = send(state, start)
    attempt = state.current_attempt
    assert attempt.request.environment["cli_version"] == "fixture-1"
    assert attempt.request.decision.choice.model == "requested-model"
    assert attempt.runtime.reported_model is None
    assert attempt.runtime.reported_effort is None
    state, _ = send(
        state, SessionStarted("session", reported_model="reported", raw_type="observed")
    )
    assert state.current_attempt.runtime.reported_model == "reported"
    assert state.current_attempt.runtime.reported_model_source.raw_type == "observed"
    assert state.current_attempt.runtime.reported_effort is None
    original, request = inputs()
    for field in ("task_hash", "rules_hash"):
        request = replace(start, decision=replace(start.decision, **{field: "wrong"}))
        with pytest.raises(ValueError):
            send(original, request)


def test_unconfirmed_diagnostics_never_become_terminal_events():
    state, _ = started()
    diagnostic = TaskDiagnostic(
        Error("stop unknown", raw_type="adapter.cleanup_incomplete")
    )
    actual, disposition = send(state, diagnostic)
    assert actual is state and disposition == Disposition.HISTORY_ONLY
    assert actual.phase == TaskPhase.IMPLEMENTING
    assert actual.current_attempt.runtime.status == AttemptStatus.RUNNING


def test_unknown_attempts_and_current_duplicate_start_are_rejected():
    state, start = inputs()
    with pytest.raises(ValueError):
        send(state, Completed(None))
    with pytest.raises(ValueError):
        send(state, start, 0)
    state, _ = send(state, start)
    for event, number in ((start, 1), (Completed(None), 2), (start, 3)):
        with pytest.raises(ValueError):
            send(state, event, number)


def test_existing_review_exit_behavior_is_unchanged():
    state = RoundState(
        "snapshot", "v1", (SeatState(SeatConfig("one", "codex", "m", "high")),)
    )
    for event in (AttemptStarted(), Completed({}), ProcessExited(17), ProcessExited(0)):
        state, disposition = apply(state, Envelope("one", 1, event))
        assert disposition == Disposition.APPLIED
    assert state.seats[0].attempts[-1] == AttemptState(
        1, AttemptStatus.SUCCEEDED, result={}, exit_code=0
    )
