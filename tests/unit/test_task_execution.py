"""Managed task invariants use trusted synthetic records without filesystem I/O."""

import hashlib
from dataclasses import FrozenInstanceError, replace

import pytest
import test_task_store as legacy

from multi_agent.core import AttemptStatus, Disposition
from multi_agent.core.dispatch import ImplementationChoice, resolve_dispatch
from multi_agent.core.events import (
    CancelRequested,
    Completed,
    Error,
    Message,
    ProcessExited,
    SessionStarted,
    Usage,
)
from multi_agent.core.task_events import (
    TaskAttemptStarted,
    TaskDiagnostic,
    TaskEnvelope,
    TaskEnvironmentObserved,
    TaskEvidenceRejected,
    TaskExecutionPrepared,
    TaskReconciled,
    TaskScopeBlocked,
)
from multi_agent.core.task_execution import (
    TaskDispatchPolicy,
    TaskExecutionContext,
    TaskExecutionPolicy,
    prepared_for_task,
    reserve_task_execution,
    task_execution_hash,
    task_execution_json,
    task_execution_value,
)
from multi_agent.core.task_state import TaskPhase, TaskState
from multi_agent.core.transitions import apply_task
from multi_agent.core.workspaces import WorkspaceControl, WorkspaceRecord


def inputs():
    return legacy.inputs()


def initial(value=None):
    value = inputs() if value is None else value
    return TaskState(
        value.card.id,
        value.card.task_hash,
        value.frozen_rules.rules_hash,
        value.card.baseline_commit,
    )


def reservation(number=1, *, value=None, policy=None, dispatch=None):
    value = inputs() if value is None else value
    dispatch = dispatch or TaskDispatchPolicy(
        "backend",
        ImplementationChoice("codex", "model-a", "high"),
        (
            ImplementationChoice("codex", "not-allowed", "low"),
            ImplementationChoice("codex", "model-b", "medium"),
            ImplementationChoice("codex", "model-a", "low"),
        ),
    )
    decision = resolve_dispatch(
        value.card,
        value.frozen_rules,
        task_type=dispatch.task_type,
        requested=dispatch.requested,
        fallbacks=dispatch.fallbacks,
        unavailable=frozenset({("codex", "model-a")}),
    )
    workspace = WorkspaceRecord(
        value.frozen_rules.repository,
        "/synthetic/source",
        value.card.baseline_commit,
        value.card.baseline_commit,
        f"/synthetic/worktree-{number}",
        f"owner-{number}",
        (1, 100 + number),
        f"/synthetic/repository/.git/worktrees/task-{number}",
        (1, 200 + number),
        (
            WorkspaceControl(".git", (1, 300 + number), b"gitdir: synthetic\n"),
            WorkspaceControl("absent", None, None),
            WorkspaceControl("empty", (1, 400 + number), b""),
            WorkspaceControl("binary", (1, 500 + number), b"\x00\xff"),
        ),
        "sha1",
    )
    started = TaskAttemptStarted(
        decision, workspace.root, "implementation-v1", f"start-{number}", managed=True
    )
    prepared = TaskExecutionPrepared(
        workspace,
        f"initial-digest-{number}",
        f"Frozen prompt {number}",
        {"type": "object", "nested": {"required": ["status"], "unknown": None}},
        policy
        or TaskExecutionPolicy(
            "codex", "/synthetic/codex", (1, 42), "c" * 64, 2, 30, 1
        ),
        dispatch,
        TaskExecutionContext(
            f"/synthetic/context-{number}",
            (1, 600 + number),
            f"context-token-{number}",
            "d" * 64,
        ),
    )
    return TaskEnvelope(number, started, f"started-{number}"), TaskEnvelope(
        number, prepared, f"prepared-{number}"
    )


def managed(*, policy=None):
    started, prepared = reservation(policy=policy)
    return reserve_task_execution(initial(), started, prepared), started, prepared


def send(state, event, number=None):
    number = state.current_attempt.number if number is None else number
    return apply_task(state, TaskEnvelope(number, event, "observed"))


def failed(state, *, digest="final-digest", stopped=True):
    state, disposition = send(state, Error("Synthetic runtime failure"))
    assert disposition == Disposition.APPLIED
    state, disposition = send(
        state, TaskReconciled(digest, stopped, "Actual synthetic host facts")
    )
    assert disposition == Disposition.APPLIED
    return state


def test_task_execution_canonical_values_keep_bytes_and_boolean_identity():
    assert task_execution_json(
        {"empty": b"", "missing": None, "binary": b"\xff\x00"}
    ) == ('{"binary":{"hex":"ff00"},"empty":{"hex":""},"missing":null}')
    assert task_execution_hash(True) != task_execution_hash(1)
    assert task_execution_hash(b"") != task_execution_hash(None)
    workspace = reservation()[1].event.workspace
    controls = task_execution_value(workspace)["controls"]
    assert controls[1]["content"] is None
    assert controls[2]["content"] == {"hex": ""}
    assert controls[3]["content"] == {"hex": "00ff"}


def test_task_execution_history_hash_has_exact_canonical_golden():
    value = (
        (TaskEnvelope(1, TaskDiagnostic(Error("x")), "time"), Disposition.HISTORY_ONLY),
    )
    expected = '[[{"attempt":1,"event":{"diagnostic":{"detail":"x","failure_category":null,"kind":"error","raw_type":null,"retry_after":null},"kind":"task_diagnostic"},"occurred_at":"time"},"history_only"]]'
    assert task_execution_json(value) == expected
    assert task_execution_hash(value) == hashlib.sha256(expected.encode()).hexdigest()
    assert task_execution_hash(value) != task_execution_hash(())
    assert task_execution_hash(value) != task_execution_hash(
        (replace(value[0][0], occurred_at="other"), value[0][1])
    )


def test_managed_task_requires_atomic_preparation():
    before = initial()
    started, prepared = reservation()
    invalid = replace(
        prepared,
        event=replace(
            prepared.event, workspace=replace(prepared.event.workspace, root="/wrong")
        ),
    )
    with pytest.raises(ValueError):
        reserve_task_execution(before, started, invalid)
    assert before == initial() and before.attempts == ()
    state = reserve_task_execution(before, started, prepared)
    assert state.current_attempt.request.managed
    assert state.current_attempt.events == (started.event, prepared.event)
    assert prepared_for_task(state.current_attempt) == prepared.event
    assert state.phase == TaskPhase.IMPLEMENTING and not state.delivery_ready
    assert state.current_attempt.runtime.reported_model is None
    assert state.current_attempt.runtime.reported_effort is None
    assert state.current_attempt.request.environment is None
    with pytest.raises(ValueError):
        reserve_task_execution(state, started, prepared)


@pytest.mark.parametrize(
    "event",
    [
        SessionStarted("actual"),
        Message("actual"),
        Usage({"tokens": 1}),
        Completed({"actual": True}),
        Error("actual"),
        ProcessExited(0),
        CancelRequested(),
        TaskDiagnostic(Error("host")),
        TaskEnvironmentObserved({"actual": None}),
        TaskReconciled(None, False, "unknown"),
    ],
)
def test_managed_task_cannot_observe_before_prepared(event):
    started, prepared = reservation()
    state, disposition = apply_task(initial(), started)
    assert disposition == Disposition.APPLIED
    assert state.current_attempt.events == (started.event,)
    with pytest.raises(ValueError):
        send(state, event)
    ready, disposition = apply_task(state, prepared)
    assert disposition == Disposition.APPLIED
    assert prepared_for_task(ready.current_attempt) == prepared.event


def test_task_preparation_duplicate_is_exact_and_schema_frozen():
    schema = {"nested": {"values": [True, None]}}
    started, prepared = reservation()
    prepared = replace(prepared, event=replace(prepared.event, output_schema=schema))
    schema["nested"]["values"].append("mutation")
    state = reserve_task_execution(initial(), started, prepared)
    assert prepared.event.output_schema["nested"]["values"] == (True, None)
    with pytest.raises(TypeError):
        prepared.event.output_schema["nested"]["extra"] = False
    with pytest.raises(FrozenInstanceError):
        prepared.event.prompt = "changed"
    unchanged, disposition = apply_task(state, prepared)
    assert unchanged == state and disposition == Disposition.HISTORY_ONLY
    changed = replace(
        prepared,
        event=replace(prepared.event, output_schema={"nested": {"values": [1, None]}}),
    )
    with pytest.raises(ValueError):
        apply_task(state, changed)


@pytest.mark.parametrize(
    "mismatch", ["worktree", "baseline", "provider", "environment", "attempt"]
)
def test_task_preparation_binds_workspace_and_dispatch(mismatch):
    started, prepared = reservation()
    if mismatch == "worktree":
        started = replace(started, event=replace(started.event, worktree="/wrong"))
    elif mismatch == "baseline":
        prepared = replace(
            prepared,
            event=replace(
                prepared.event,
                workspace=replace(prepared.event.workspace, task_baseline="e" * 40),
            ),
        )
    elif mismatch == "provider":
        prepared = replace(
            prepared,
            event=replace(
                prepared.event, policy=replace(prepared.event.policy, provider="claude")
            ),
        )
    elif mismatch == "environment":
        started = replace(
            started,
            event=replace(started.event, environment={"requested": "not actual"}),
        )
    else:
        prepared = replace(prepared, attempt=2)
    with pytest.raises(ValueError):
        reserve_task_execution(initial(), started, prepared)


@pytest.mark.parametrize("managed_first", [False, True])
def test_managed_task_chain_cannot_downgrade(managed_first):
    started, prepared = reservation()
    if managed_first:
        state = failed(reserve_task_execution(initial(), started, prepared))
        next_start, _ = reservation(2)
        with pytest.raises(ValueError):
            apply_task(
                state,
                replace(next_start, event=replace(next_start.event, managed=False)),
            )
    else:
        old = replace(started, event=replace(started.event, managed=False))
        state, _ = apply_task(initial(), old)
        diagnostic = TaskDiagnostic(Error("legacy diagnostic"))
        unchanged, disposition = send(state, diagnostic)
        assert unchanged == state and disposition == Disposition.HISTORY_ONLY
        assert state.current_attempt.events == ()
        state = failed(state)
        with pytest.raises(ValueError):
            reserve_task_execution(state, *reservation(2))


def test_task_environment_observation_is_actual_immutable():
    state, _, _ = managed()
    state, _ = send(state, TaskDiagnostic(Error("before actual preflight")))
    value = {"logged_in": None, "model": None, "metadata": {"flags": [True]}}
    event = TaskEnvironmentObserved(value)
    value["metadata"]["flags"].append(False)
    state, disposition = send(state, event)
    assert disposition == Disposition.APPLIED
    assert event.environment["metadata"]["flags"] == (True,)
    assert state.current_attempt.request.environment is None
    assert state.current_attempt.runtime.reported_model is None
    assert state.current_attempt.events[-1] == event
    unchanged, disposition = send(state, event)
    assert unchanged == state and disposition == Disposition.HISTORY_ONLY
    with pytest.raises(ValueError):
        send(
            state,
            TaskEnvironmentObserved(
                {"logged_in": None, "model": None, "metadata": {"flags": [1]}}
            ),
        )
    with pytest.raises(TypeError):
        event.environment["model"] = "invented"
    state, _ = send(state, SessionStarted("real", reported_model=None))
    unchanged, disposition = send(state, event)
    assert unchanged == state and disposition == Disposition.HISTORY_ONLY


@pytest.mark.parametrize(
    "before",
    [
        SessionStarted("runtime"),
        Message("runtime"),
        Usage({"tokens": 1}),
        CancelRequested(),
        TaskReconciled(None, False, "already reconciled"),
    ],
)
def test_task_environment_first_observation_rejects_wrong_order(before):
    state, _, _ = managed()
    state, _ = send(state, before)
    with pytest.raises(ValueError):
        send(state, TaskEnvironmentObserved({"late": True}))


@pytest.mark.parametrize("exit_seen", [False, True])
def test_task_evidence_rejection_preserves_runtime(exit_seen):
    state, _, _ = managed()
    complete = Completed(
        {"changed": ["src/module.py"]},
        reported_model="actual",
        raw_type="actual.complete",
    )
    state, _ = send(state, complete)
    if exit_seen:
        state, _ = send(state, ProcessExited(-15, raw_type="actual.exit"))
    runtime, reconciliation = (
        state.current_attempt.runtime,
        state.current_attempt.reconciliation,
    )
    rejected = TaskEvidenceRejected("output evidence is missing")
    state, disposition = send(state, rejected)
    assert disposition == Disposition.APPLIED and state.phase == TaskPhase.FAILED
    assert state.current_attempt.runtime == runtime
    assert state.current_attempt.reconciliation == reconciliation
    assert complete in state.current_attempt.events
    unchanged, disposition = send(state, rejected)
    assert unchanged == state and disposition == Disposition.HISTORY_ONLY
    with pytest.raises(ValueError):
        send(state, TaskEvidenceRejected("different explanation"))
    state, _ = send(
        state, TaskReconciled("final", True, "later facts", "/execution.json", "e" * 64)
    )
    assert state.phase == TaskPhase.FAILED and not state.delivery_ready
    assert state.current_attempt.runtime == runtime


def test_task_evidence_rejection_requires_successful_managed_phase():
    state, _, _ = managed()
    with pytest.raises(ValueError):
        send(state, TaskEvidenceRejected("too early"))
    state, _ = send(state, Error("actual failed"))
    with pytest.raises(ValueError):
        send(state, TaskEvidenceRejected("not a success"))


@pytest.mark.parametrize("missing", ["exit", "receipt", "stop", "digest", "logs"])
def test_managed_task_delivery_needs_final_evidence(missing):
    state, _, prepared = managed()
    state, _ = send(
        state, Completed({"status": "scope_blocked"}, reported_model="actual")
    )
    if missing != "exit":
        state, _ = send(state, ProcessExited(-15))
    if missing != "receipt":
        state, _ = send(
            state,
            TaskReconciled(
                None if missing == "digest" else "actual-final-digest",
                missing != "stop",
                "Actual host receipt",
                None if missing == "logs" else "/owned/execution.json",
                None if missing == "logs" else "f" * 64,
            ),
        )
    assert not state.delivery_ready
    assert prepared.event.initial_workspace_digest != "actual-final-digest"
    with pytest.raises(ValueError):
        send(state, TaskScopeBlocked("needs scope", ("outside.py",)))
    if missing == "exit":
        state, _ = send(state, ProcessExited(-15))
    state, _ = send(
        state,
        TaskReconciled(
            "actual-final-digest",
            True,
            "actual stopped",
            "/owned/execution.json",
            "f" * 64,
        ),
    )
    assert state.delivery_ready
    state, _ = send(state, TaskScopeBlocked("needs scope", ("outside.py",)))
    assert state.phase == TaskPhase.PLANNING_REQUIRED and not state.delivery_ready


@pytest.mark.parametrize("pair", [(None, "a" * 64), ("/execution.json", None)])
def test_task_reconciliation_requires_paired_execution_receipt(pair):
    state, _, _ = managed()
    with pytest.raises(ValueError):
        send(state, TaskReconciled("actual", True, "invalid pair", *pair))


@pytest.mark.parametrize("missing", ["stopped", "digest"])
def test_managed_task_retry_requires_final_workspace_reconciliation(missing):
    state, _, _ = managed()
    state = failed(
        state,
        digest=None if missing == "digest" else "actual-final",
        stopped=missing != "stopped",
    )
    with pytest.raises(ValueError):
        reserve_task_execution(state, *reservation(2))


def test_managed_task_retry_keeps_policy_and_budget():
    state, _, prepared = managed()
    state = failed(state)
    state = reserve_task_execution(state, *reservation(2))
    second = prepared_for_task(state.current_attempt)
    assert second.policy == prepared.event.policy
    assert second.dispatch_policy == prepared.event.dispatch_policy
    assert second.workspace.root != prepared.event.workspace.root
    state = failed(state)
    with pytest.raises(ValueError):
        reserve_task_execution(state, *reservation(3))
    assert len(state.attempts) == 2
    assert state.attempts[0].runtime.status == AttemptStatus.FAILED


@pytest.mark.parametrize(
    "field",
    [
        "workspace_root",
        "owner",
        "root_identity",
        "git_identity",
        "context_path",
        "context_identity",
        "context_token",
        "policy",
        "fallback_order",
        "fallback_tail",
    ],
)
def test_managed_retry_rejects_reused_identity_or_changed_frozen_policy(field):
    state, _, first = managed()
    state = failed(state)
    started, prepared = reservation(2)
    value = prepared.event
    if field.startswith("workspace_") or field in (
        "owner",
        "root_identity",
        "git_identity",
    ):
        attr = {
            "workspace_root": "root",
            "owner": "owner_token",
            "root_identity": "root_identity",
            "git_identity": "git_identity",
        }[field]
        value = replace(
            value,
            workspace=replace(
                value.workspace, **{attr: getattr(first.event.workspace, attr)}
            ),
        )
        if field == "workspace_root":
            started = replace(
                started, event=replace(started.event, worktree=value.workspace.root)
            )
    elif field.startswith("context_"):
        attr = field.removeprefix("context_")
        value = replace(
            value,
            context=replace(
                value.context, **{attr: getattr(first.event.context, attr)}
            ),
        )
    elif field == "policy":
        value = replace(value, policy=replace(value.policy, timeout=31))
    elif field == "fallback_order":
        value = replace(
            value,
            dispatch_policy=replace(
                value.dispatch_policy,
                fallbacks=tuple(reversed(value.dispatch_policy.fallbacks)),
            ),
        )
    else:
        value = replace(
            value,
            dispatch_policy=replace(
                value.dispatch_policy, fallbacks=value.dispatch_policy.fallbacks[:-1]
            ),
        )
    with pytest.raises(ValueError):
        reserve_task_execution(state, started, replace(prepared, event=value))


def test_managed_late_events_leave_current_state_and_event_list_unchanged():
    state, _, first = managed()
    state = reserve_task_execution(failed(state), *reservation(2))
    current = state
    for event in (
        TaskEnvironmentObserved({"old": True}),
        TaskEvidenceRejected("old rejection"),
        first.event,
        Completed({"old": True}),
        ProcessExited(0),
        TaskDiagnostic(Error("old")),
    ):
        state, disposition = send(state, event, number=1)
        assert state == current and disposition == Disposition.HISTORY_ONLY
    assert len(state.current_attempt.events) == 2


def test_managed_task_exit_without_terminal_is_recorded():
    state, _, _ = managed()
    actual = ProcessExited(-15, raw_type="actual.model.exit")
    state, disposition = send(state, actual)
    assert disposition == Disposition.APPLIED
    assert state.phase == TaskPhase.IMPLEMENTING
    assert state.current_attempt.runtime.status == AttemptStatus.RUNNING
    assert state.current_attempt.runtime.exit_code == -15
    assert state.current_attempt.runtime.result is None
    assert state.current_attempt.reconciliation is None
    assert state.current_attempt.events[-1] == actual
    assert not state.delivery_ready
    runtime = state.current_attempt.runtime
    state, disposition = send(
        state, TaskReconciled("actual-final", True, "stopped without terminal")
    )
    assert disposition == Disposition.APPLIED and state.phase == TaskPhase.FAILED
    assert state.current_attempt.runtime == runtime
    legacy_state, _ = apply_task(initial(), TaskEnvelope(1, legacy.started()))
    with pytest.raises(ValueError):
        send(legacy_state, actual)


@pytest.mark.parametrize("is_managed", [True, False])
def test_managed_planning_required_keeps_latest_reconciliation(is_managed):
    if is_managed:
        state, _, _ = managed()
    else:
        state, _ = apply_task(initial(), TaskEnvelope(1, legacy.started()))
    for event in (
        Completed({"reason": "larger scope"}),
        ProcessExited(-15),
        TaskReconciled("actual-final", True, "stopped", "/execution.json", "e" * 64),
        TaskScopeBlocked("larger scope", ("shared/interface.py",)),
    ):
        state, disposition = send(state, event)
        assert disposition == Disposition.APPLIED
    before = state
    revoked = TaskReconciled(None, False, "new stop observation is unknown")
    state, disposition = send(state, revoked)
    assert state.phase == TaskPhase.PLANNING_REQUIRED and not state.delivery_ready
    assert state.current_attempt.runtime == before.current_attempt.runtime
    assert state.scope_block == before.scope_block
    if is_managed:
        assert disposition == Disposition.APPLIED
        assert state.current_attempt.reconciliation == revoked
        assert state.current_attempt.events == (*before.current_attempt.events, revoked)
        with pytest.raises(ValueError):
            reserve_task_execution(state, *reservation(2))
    else:
        assert disposition == Disposition.HISTORY_ONLY and state == before
        with pytest.raises(ValueError):
            apply_task(state, TaskEnvelope(2, legacy.started()))
