"""Managed reservations preserve full history and immutable evidence atomically."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest
import test_task_execution as fixtures
import test_task_store as legacy

from multi_agent.core import Disposition
from multi_agent.core.dispatch import ImplementationChoice
from multi_agent.core.events import Completed, Error, ProcessExited
from multi_agent.core.task_events import (
    TaskDiagnostic,
    TaskEnvelope,
    TaskEnvironmentObserved,
    TaskEvidenceRejected,
    TaskExecutionPrepared,
    TaskReconciled,
)
from multi_agent.core.task_execution import prepared_for_task, task_execution_hash
from multi_agent.core.task_state import TaskPhase
from multi_agent.store.task_codec import (
    decode_task_attempt,
    decode_task_envelope,
    encode_task_attempt,
    encode_task_envelope,
)
from multi_agent.store.task_store import TaskStore


@pytest.fixture
def store(tmp_path):
    with TaskStore(tmp_path / "task.sqlite") as value:
        legacy.create(value)
        yield value


def reserve(store, number=1, *, pair=None, prefix=None):
    started, prepared = fixtures.reservation(number) if pair is None else pair
    history = store.load_task_history("run") if prefix is None else prefix
    store.reserve_task_attempt(
        "run",
        started,
        prepared,
        expected_history_length=len(history),
        expected_history_hash=task_execution_hash(history),
    )
    return started, prepared


def fail(store):
    number = store.load_task("run").current_attempt.number
    store.apply_task("run", TaskEnvelope(number, Error("actual failure"), "failed"))
    store.apply_task(
        "run",
        TaskEnvelope(
            number,
            TaskReconciled("actual-final", True, "host stopped and scanned"),
            "reconciled",
        ),
    )


def test_managed_task_requires_atomic_preparation(store):
    started, prepared = fixtures.reservation()
    before = store.load_task("run")
    for event in (started, prepared):
        with pytest.raises(ValueError):
            store.apply_task("run", event)
        assert store.load_task("run") == before
        assert store.load_task_history("run") == ()
    reserve(store, pair=(started, prepared))
    assert store.load_task_history("run") == (
        (started, Disposition.APPLIED),
        (prepared, Disposition.APPLIED),
    )
    current = store.load_task("run").current_attempt
    assert current.events == (started.event, prepared.event)
    assert prepared_for_task(current) == prepared.event
    with pytest.raises(ValueError):
        store.apply_task("run", prepared)
    assert len(store.load_task_history("run")) == 2


@pytest.mark.parametrize(
    "changed",
    [
        "repository",
        "choice",
        "reason",
        "history",
        "unavailable",
        "requested",
        "dispatch_order",
    ],
)
def test_task_preparation_binds_workspace_and_dispatch(store, changed):
    started, prepared = fixtures.reservation()
    if changed == "repository":
        prepared = replace(
            prepared,
            event=replace(
                prepared.event,
                workspace=replace(
                    prepared.event.workspace, repository="/other/repository/.git"
                ),
            ),
        )
    elif changed == "dispatch_order":
        policy = prepared.event.dispatch_policy
        prepared = replace(
            prepared,
            event=replace(
                prepared.event,
                dispatch_policy=replace(
                    policy, fallbacks=tuple(reversed(policy.fallbacks))
                ),
            ),
        )
    else:
        decision = started.event.decision
        replacements = {
            "choice": {"choice": ImplementationChoice("codex", "model-b", "high")},
            "reason": {"reason": "invented reason"},
            "history": {"selection_history": decision.selection_history[:-1]},
            "unavailable": {"unavailable_models": ()},
            "requested": {"requested": None},
        }[changed]
        started = replace(
            started,
            event=replace(started.event, decision=replace(decision, **replacements)),
        )
    before = store.load_task("run")
    with pytest.raises(ValueError):
        reserve(store, pair=(started, prepared))
    assert store.load_task("run") == before
    assert store.load_task_history("run") == ()


def test_full_fallback_policy_survives_selection_history_truncation(store, tmp_path):
    started, prepared = reserve(store)
    decision = started.event.decision
    policy = prepared.event.dispatch_policy
    assert decision.choice == policy.fallbacks[1]
    assert decision.selection_history[-1].choice == policy.fallbacks[1]
    assert all(
        record.choice != policy.fallbacks[-1] for record in decision.selection_history
    )
    with TaskStore(tmp_path / "task.sqlite") as reopened:
        restored = prepared_for_task(reopened.load_task("run").current_attempt)
        assert restored.dispatch_policy.fallbacks == policy.fallbacks
        assert restored.dispatch_policy.requested == policy.requested
        assert restored.policy == prepared.event.policy
        assert reopened.load_task_input("run") == fixtures.inputs()


@pytest.mark.parametrize("failure", ["prepared", "state_write", "second_history"])
def test_task_reservation_history_cas_rolls_back(store, monkeypatch, failure):
    started, prepared = fixtures.reservation()
    before = store.load_task("run"), store.load_task_history("run")
    problem = sqlite3.OperationalError("injected reservation write failure")
    if failure == "prepared":
        prepared = replace(
            prepared,
            event=replace(
                prepared.event, policy=replace(prepared.event.policy, provider="claude")
            ),
        )
        expected = ValueError
    elif failure == "state_write":
        original = store._write_task

        def failed_write(*args):
            original(*args)
            raise problem

        monkeypatch.setattr(store, "_write_task", failed_write)
        expected = sqlite3.OperationalError
    else:
        original = store._append_task_history

        def failed_history(run_id, envelope, disposition):
            original(run_id, envelope, disposition)
            if isinstance(envelope.event, TaskExecutionPrepared):
                raise problem

        monkeypatch.setattr(store, "_append_task_history", failed_history)
        expected = sqlite3.OperationalError
    with pytest.raises(expected):
        reserve(store, pair=(started, prepared))
    assert (store.load_task("run"), store.load_task_history("run")) == before
    assert (
        store._connection.execute("SELECT COUNT(*) FROM task_attempts").fetchone()[0]
        == 0
    )


def test_task_reservation_rejects_history_only_cas_change(store, tmp_path):
    reserve(store)
    fail(store)
    prefix = store.load_task_history("run")
    before = store.load_task("run")
    with TaskStore(tmp_path / "task.sqlite") as other:
        late = TaskEnvelope(1, Completed({"late": True}), "late-time")
        assert other.apply_task("run", late) == Disposition.HISTORY_ONLY
        assert other.load_task("run") == before
    with pytest.raises(ValueError):
        reserve(store, 2, prefix=prefix)
    assert store.load_task("run") == before
    actual = store.load_task_history("run")
    assert actual == (*prefix, (late, Disposition.HISTORY_ONLY))
    assert task_execution_hash(actual) != task_execution_hash(prefix)
    reserve(store, 2)
    assert store.load_task("run").current_attempt.number == 2


def test_task_reservation_compares_hash_not_only_history_length(store):
    reserve(store)
    fail(store)
    prefix = store.load_task_history("run")
    wrong = (
        *prefix[:-1],
        (replace(prefix[-1][0], occurred_at="different"), prefix[-1][1]),
    )
    assert len(wrong) == len(prefix) and task_execution_hash(
        wrong
    ) != task_execution_hash(prefix)
    before = store.load_task("run")
    with pytest.raises(ValueError):
        reserve(store, 2, prefix=wrong)
    assert store.load_task("run") == before and store.load_task_history("run") == prefix


def test_task_reservation_two_connections_commit_only_one_pair(store, tmp_path):
    prefix = store.load_task_history("run")
    barrier = Barrier(2)

    def competing(index):
        started, prepared = fixtures.reservation()
        workspace = replace(
            prepared.event.workspace,
            root=f"/competitor-{index}",
            owner_token=f"competitor-{index}",
        )
        started = replace(
            started, event=replace(started.event, worktree=workspace.root)
        )
        prepared = replace(prepared, event=replace(prepared.event, workspace=workspace))
        with TaskStore(tmp_path / "task.sqlite") as connection:
            barrier.wait(timeout=5)
            try:
                reserve(connection, pair=(started, prepared), prefix=prefix)
            except ValueError:
                return None
        return started, prepared

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(competing, index) for index in range(2)]
        outcomes = [future.result(timeout=10) for future in futures]
    winners = [result for result in outcomes if result is not None]
    assert len(winners) == 1
    assert store.load_task_history("run") == tuple(
        (event, Disposition.APPLIED) for event in winners[0]
    )
    assert len(store.load_task("run").attempts) == 1


def test_workspace_control_bytes_and_legacy_codec_roundtrip(store, tmp_path):
    started, prepared = reserve(store)
    events = (
        TaskEnvironmentObserved({"models": None, "nested": {"flags": [True, None]}}),
        TaskDiagnostic(Error("preflight detail")),
        Completed({"changed": ["src/new.py"]}, reported_model="actual"),
        ProcessExited(-15, raw_type="actual.exit"),
        TaskReconciled(None, True, "scan unknown", "/execution.json", "a" * 64),
        TaskEvidenceRejected("final digest missing"),
    )
    for event in events:
        envelope = TaskEnvelope(1, event, "observed")
        assert decode_task_envelope(encode_task_envelope(envelope)) == envelope
        assert store.apply_task("run", envelope) == Disposition.APPLIED
    state = store.load_task("run")
    assert state.current_attempt.events == (started.event, prepared.event, *events)
    assert (
        decode_task_attempt(encode_task_attempt(state.current_attempt))
        == state.current_attempt
    )
    with TaskStore(tmp_path / "task.sqlite") as reopened:
        assert reopened.load_task("run") == state
        assert reopened.load_task_history("run") == store.load_task_history("run")
    encoded = json.loads(encode_task_envelope(prepared))
    controls = encoded["event"]["workspace"]["controls"]
    assert controls[1]["content"] is None
    assert controls[2]["content"] == {"hex": ""}
    assert controls[3]["content"] == {"hex": "00ff"}
    restored = decode_task_envelope(encode_task_envelope(prepared))
    assert tuple(item.content for item in restored.event.workspace.controls) == (
        b"gitdir: synthetic\n",
        None,
        b"",
        b"\x00\xff",
    )
    assert state.phase == TaskPhase.FAILED
    assert state.current_attempt.reconciliation.worktree_digest is None
    assert state.current_attempt.runtime.reported_model == "actual"


def test_legacy_codec_keeps_exact_old_optional_field_shape(store):
    request = legacy.started()
    envelope = TaskEnvelope(1, request, "legacy")
    raw = json.loads(encode_task_envelope(envelope))
    assert set(raw["event"]) == {
        "type",
        "decision",
        "worktree",
        "prompt_template_version",
        "started_at",
        "environment",
    }
    assert decode_task_envelope(json.dumps(raw)) == envelope
    store.apply_task("run", envelope)
    store.apply_task("run", TaskEnvelope(1, Error("legacy failed")))
    store.apply_task(
        "run", TaskEnvelope(1, TaskReconciled("old-final", True, "old receipt"))
    )
    attempt = store.load_task("run").current_attempt
    payload = json.loads(encode_task_attempt(attempt))
    assert set(payload) == {"request", "runtime", "reconciliation"}
    assert set(payload["reconciliation"]) == {
        "worktree_digest",
        "process_stopped",
        "detail",
    }
    restored = decode_task_attempt(json.dumps(payload))
    assert not restored.request.managed and restored.events == ()
    assert (
        restored.reconciliation.log_path is None
        and restored.reconciliation.log_sha256 is None
    )
    assert encode_task_attempt(restored) == encode_task_attempt(attempt)


@pytest.mark.parametrize(
    "path",
    [
        ("request", "environment"),
        ("request", "started_at"),
        ("request", "decision", "allowed_models"),
        ("runtime", "status"),
        ("runtime", "exit_code"),
        ("reconciliation",),
    ],
)
def test_legacy_codec_does_not_default_original_required_fields(store, path):
    store.apply_task("run", TaskEnvelope(1, legacy.started()))
    payload = json.loads(encode_task_attempt(store.load_task("run").current_attempt))
    target = payload
    for part in path[:-1]:
        target = target[part]
    del target[path[-1]]
    with pytest.raises(KeyError):
        decode_task_attempt(json.dumps(payload))


@pytest.mark.parametrize(
    "field", ["workspace", "policy", "dispatch_policy", "context", "output_schema"]
)
def test_managed_codec_requires_complete_new_preparation_fields(field):
    _, prepared = fixtures.reservation()
    payload = json.loads(encode_task_envelope(prepared))
    del payload["event"][field]
    with pytest.raises(KeyError):
        decode_task_envelope(json.dumps(payload))


def test_task_store_managed_retry_preserves_actual_events_and_input(store):
    value = store.load_task_input("run")
    reserve(store)
    fail(store)
    first = store.load_task("run").current_attempt
    reserve(store, 2)
    current = store.load_task("run")
    assert current.attempts[0] == first
    assert store.load_task_input("run") == value
    assert (
        prepared_for_task(current.current_attempt).dispatch_policy
        == prepared_for_task(first).dispatch_policy
    )
    late = TaskEnvelope(1, TaskEnvironmentObserved({"late": True}))
    assert store.apply_task("run", late) == Disposition.HISTORY_ONLY
    assert store.load_task("run") == current
    assert store.load_task_history("run")[-1] == (late, Disposition.HISTORY_ONLY)
