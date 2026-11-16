"""Trusted candidate state is independent of Git existence or executed checks."""

from dataclasses import FrozenInstanceError, replace

import pytest
import test_task_execution as fixtures
import test_task_store as legacy

from multi_agent.core import Disposition
from multi_agent.core.events import CancelRequested, Completed, ProcessExited
from multi_agent.core.task_events import (
    TaskCandidateFrozen,
    TaskEnvelope,
    TaskEvidenceRejected,
    TaskReconciled,
    TaskScopeBlocked,
)
from multi_agent.core.task_execution import prepared_for_task, reserve_task_execution
from multi_agent.core.task_state import TaskPhase
from multi_agent.core.transitions import apply_task
from multi_agent.core.workspaces import WorkspaceDelivery

LOG_PATH = "/synthetic/execution.json"
LOG_HASH = "e" * 64
DIGEST = "actual-final-digest"


def reconciliation(**changes):
    return replace(
        TaskReconciled(
            DIGEST, True, "Synthetic supplied host facts", LOG_PATH, LOG_HASH
        ),
        **changes,
    )


def qualified():
    state, _, _ = fixtures.managed()
    for event in (
        Completed({"actual": True}, raw_type="synthetic.completed"),
        ProcessExited(-15, raw_type="synthetic.exit"),
        reconciliation(),
    ):
        state, disposition = fixtures.send(state, event)
        assert disposition == Disposition.APPLIED
    assert state.delivery_ready
    return state


def candidate(state=None):
    state = qualified() if state is None else state
    prepared = prepared_for_task(state.current_attempt)
    return TaskCandidateFrozen(
        WorkspaceDelivery(
            state.baseline_commit,
            "c" * 40,
            ("src/changed.py", "src/新增.py"),
            DIGEST,
            f"refs/multi-agent/candidates/{prepared.workspace.owner_token}",
            "d" * 40,
        ),
        LOG_PATH,
        LOG_HASH,
    )


def changed_candidate(event, field):
    if field in {"execution_log_path", "execution_log_sha256"}:
        return replace(
            event,
            **{
                field: "/other/execution.json"
                if field == "execution_log_path"
                else "f" * 64
            },
        )
    changes = {
        "baseline": "b" * 40,
        "candidate": "f" * 40,
        "changed_paths": tuple(reversed(event.delivery.changed_paths)),
        "digest": "different-digest",
        "ref": "refs/multi-agent/candidates/other-owner",
        "tree": "f" * 40,
    }
    return replace(event, delivery=replace(event.delivery, **{field: changes[field]}))


@pytest.mark.parametrize(
    "missing",
    [
        "legacy",
        "no-current",
        "no-prepared",
        "running",
        "failed",
        "cancelled",
        "planning-required",
        "exit",
        "reconciliation",
        "stop",
        "digest",
        "anchor",
        "empty-anchor",
        "baseline",
        "prepared-baseline",
        "ref",
        "different-digest",
        "different-path",
        "different-hash",
    ],
)
def test_candidate_requires_current_qualified_managed_attempt(missing):
    state = qualified()
    event = candidate(state)
    if missing == "legacy":
        state, _ = apply_task(fixtures.initial(), TaskEnvelope(1, legacy.started()))
        for observed in (
            Completed({"actual": True}),
            ProcessExited(-15),
            reconciliation(),
        ):
            state, _ = fixtures.send(state, observed)
        assert state.delivery_ready
    elif missing == "no-current":
        state = fixtures.initial()
    elif missing == "no-prepared":
        state, _ = apply_task(fixtures.initial(), fixtures.reservation()[0])
    elif missing in {"running", "failed", "cancelled", "exit", "reconciliation"}:
        state, _, _ = fixtures.managed()
        if missing == "failed":
            state = fixtures.failed(state)
        elif missing == "cancelled":
            for observed in (CancelRequested(), ProcessExited(-15), reconciliation()):
                state, _ = fixtures.send(state, observed)
        elif missing in {"exit", "reconciliation"}:
            state, _ = fixtures.send(state, Completed({"actual": True}))
            state, _ = fixtures.send(
                state, reconciliation() if missing == "exit" else ProcessExited(-15)
            )
    elif missing == "planning-required":
        state, _ = fixtures.send(
            state, TaskScopeBlocked("Need shared", ("shared/api.py",))
        )
        assert state.phase == TaskPhase.PLANNING_REQUIRED
    elif missing in {"stop", "digest", "anchor", "empty-anchor"}:
        changes = {
            "stop": {"process_stopped": False},
            "digest": {"worktree_digest": None},
            "anchor": {"log_path": None, "log_sha256": None},
            "empty-anchor": {"log_path": "", "log_sha256": ""},
        }[missing]
        state, _ = fixtures.send(state, reconciliation(**changes))
    elif missing == "prepared-baseline":
        current = state.current_attempt
        prepared = prepared_for_task(current)
        changed = replace(
            prepared, workspace=replace(prepared.workspace, task_baseline="b" * 40)
        )
        state = replace(
            state,
            attempts=(
                replace(
                    current,
                    events=tuple(
                        changed if item is prepared else item for item in current.events
                    ),
                ),
            ),
        )
    else:
        field = {
            "baseline": "baseline",
            "ref": "ref",
            "different-digest": "digest",
            "different-path": "execution_log_path",
            "different-hash": "execution_log_sha256",
        }[missing]
        event = changed_candidate(event, field)
    original = state
    runtime = (
        state.current_attempt.runtime if state.current_attempt is not None else None
    )
    with pytest.raises(ValueError):
        apply_task(state, TaskEnvelope(1, event, "candidate"))
    assert state == original and state.current_candidate is None
    assert not state.candidate_ready
    if runtime is not None:
        assert state.current_attempt.runtime == runtime


@pytest.mark.parametrize(
    "field",
    [
        "baseline",
        "candidate",
        "changed_paths",
        "digest",
        "ref",
        "tree",
        "execution_log_path",
        "execution_log_sha256",
    ],
)
@pytest.mark.parametrize("rejected", [False, True])
def test_candidate_record_is_immutable_and_exact_duplicate_is_history_only(
    field, rejected
):
    state = qualified()
    event = candidate(state)
    state, disposition = fixtures.send(state, event)
    assert disposition == Disposition.APPLIED
    if rejected:
        state, _ = fixtures.send(state, TaskEvidenceRejected("later host rejection"))
        assert state.phase == TaskPhase.FAILED
    before = state
    repeated, disposition = apply_task(
        state, TaskEnvelope(1, event, "different-observed-time")
    )
    assert repeated == before and disposition == Disposition.HISTORY_ONLY
    assert repeated.current_attempt.events.count(event) == 1
    assert repeated.candidate_ready is not rejected
    with pytest.raises(ValueError):
        fixtures.send(state, changed_candidate(event, field))
    assert state == before and state.current_candidate == event


@pytest.mark.parametrize(
    "change",
    [
        "stop",
        "missing-digest",
        "different-digest",
        "missing-anchor",
        "different-path",
        "different-hash",
    ],
)
def test_candidate_ready_tracks_latest_execution_anchor(change):
    state = qualified()
    event = candidate(state)
    state, _ = fixtures.send(state, event)
    runtime = state.current_attempt.runtime
    changes = {
        "stop": {"process_stopped": False},
        "missing-digest": {"worktree_digest": None},
        "different-digest": {"worktree_digest": "different"},
        "missing-anchor": {"log_path": None, "log_sha256": None},
        "different-path": {"log_path": "/other/execution.json"},
        "different-hash": {"log_sha256": "f" * 64},
    }[change]
    state, disposition = fixtures.send(state, reconciliation(**changes))
    assert disposition == Disposition.APPLIED
    assert state.phase == TaskPhase.AWAITING_DELIVERY
    assert state.current_candidate == event and not state.candidate_ready
    assert state.current_attempt.runtime == runtime
    repeated, disposition = fixtures.send(state, event)
    assert repeated == state and disposition == Disposition.HISTORY_ONLY
    assert not repeated.candidate_ready
    state, _ = fixtures.send(state, reconciliation())
    assert state.candidate_ready
    state, _ = fixtures.send(state, TaskEvidenceRejected("archive no longer usable"))
    assert state.phase == TaskPhase.FAILED and state.current_candidate == event
    assert not state.candidate_ready and not state.delivery_ready
    state, _ = fixtures.send(state, reconciliation())
    assert state.phase == TaskPhase.FAILED and not state.candidate_ready
    assert state.current_attempt.runtime == runtime


def test_candidate_freeze_preserves_delivery_phase_and_blocks_scope_handback():
    state = qualified()
    event = candidate(state)
    original_runtime = state.current_attempt.runtime
    state, disposition = fixtures.send(state, event)
    assert disposition == Disposition.APPLIED
    assert state.phase == TaskPhase.AWAITING_DELIVERY
    assert state.delivery_ready and state.candidate_ready
    assert state.current_candidate == event
    assert state.current_attempt.runtime == original_runtime
    assert original_runtime.reported_model is original_runtime.reported_effort is None
    assert original_runtime.exit_code == -15
    with pytest.raises(ValueError):
        fixtures.send(state, TaskScopeBlocked("Too late", ("shared/api.py",)))
    with pytest.raises(FrozenInstanceError):
        event.execution_log_path = "changed"
    with pytest.raises(FrozenInstanceError):
        event.delivery.digest = "changed"
    assert not hasattr(event, "__dict__")


@pytest.mark.parametrize("published", [False, True])
def test_late_candidate_conflict_or_unqualified_is_still_history_only(published):
    old = qualified()
    event = candidate(old)
    if published:
        old, _ = fixtures.send(old, event)
        old, _ = fixtures.send(old, TaskEvidenceRejected("retry explicitly"))
    else:
        old, _, _ = fixtures.managed()
        old = fixtures.failed(old)
    original_events = old.current_attempt.events
    started, prepared = fixtures.reservation(2)
    current = reserve_task_execution(old, started, prepared)
    assert current.current_candidate is None and not current.candidate_ready
    conflicting = changed_candidate(event, "baseline")
    after, disposition = apply_task(current, TaskEnvelope(1, conflicting, "late"))
    assert disposition == Disposition.HISTORY_ONLY and after == current
    assert after.attempts[0].events == original_events
    assert after.current_attempt.events == (started.event, prepared.event)


@pytest.mark.parametrize("number", [0, -1, 2, 99])
def test_candidate_pure_unknown_attempts_raise_value_error(number):
    state = qualified()
    with pytest.raises(ValueError):
        apply_task(state, TaskEnvelope(number, candidate(state)))


@pytest.mark.parametrize("rejected", [False, True])
def test_candidate_duplicate_priority_precedes_missing_preparation(rejected):
    state = qualified()
    event = candidate(state)
    state, _ = fixtures.send(state, event)
    if rejected:
        state, _ = fixtures.send(state, TaskEvidenceRejected("already rejected"))
    current = state.current_attempt
    prepared = prepared_for_task(current)
    # Trusted-state fixture isolates duplicate ordering, without claiming this
    # missing Prepared can be produced by the public reservation API.
    state = replace(
        state,
        attempts=(
            replace(
                current,
                events=tuple(item for item in current.events if item is not prepared),
            ),
        ),
    )
    unchanged, disposition = fixtures.send(state, event)
    assert unchanged == state and disposition == Disposition.HISTORY_ONLY
    assert state.current_candidate == event and not state.candidate_ready
    with pytest.raises(ValueError):
        fixtures.send(state, changed_candidate(event, "tree"))
