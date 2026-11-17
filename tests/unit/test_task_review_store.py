"""Real existing-table transactions bound to independently executed test inputs."""

import sqlite3
from dataclasses import replace

import pytest
import test_executed_review_input as inputs

from multi_agent.core import Disposition, RoundState, SeatConfig, SeatState
from multi_agent.core.events import CancelRequested, thaw_json
from multi_agent.core.task_events import TaskEnvelope
from multi_agent.core.task_test_events import TaskTestCancelRequested, TaskTestEnvelope

feature = inputs.feature
frozen = inputs.frozen


def round_state(value):
    return RoundState(
        value.snapshot_id,
        "executed-review-v2",
        (SeatState(SeatConfig("gpt", "codex", "model-1", "high")),),
    )


def table_rows(store):
    return {
        name: tuple(
            tuple(row)
            for row in store._connection.execute(f"SELECT * FROM {name} ORDER BY rowid")
        )
        for name in (
            "snapshots",
            "rounds",
            "seats",
            "attempts",
            "round_inputs",
            "executions",
        )
    }


def create(store, value, *, identifier="executed-review"):
    store.create_task_review(
        identifier,
        round_state(value),
        value,
        {
            "kind": "review",
            "created_at": "fixed-created",
            "timeout": 15,
            "grace": 0.2,
            "executables": {
                "codex": "/synthetic/not-executed",
                "claude": "/synthetic/not-executed",
            },
            "parent_round_id": None,
        },
    )


def test_task_review_atomic_source_binding_and_reopen(frozen):
    from multi_agent.store.task_review_store import TaskReviewStore

    before = frozen.store.load_task_test_history("implementation", 1)
    schema = tuple(
        tuple(row)
        for row in frozen.store._connection.execute(
            "SELECT name,sql FROM sqlite_master ORDER BY name"
        )
    )
    create(frozen.store, frozen.value)
    with TaskReviewStore(frozen.feature.database) as reopened:
        assert reopened.load_round("executed-review") == round_state(frozen.value)
        assert (
            thaw_json(reopened.load_snapshot(frozen.value.snapshot_id))
            == frozen.value.to_dict()
        )
        assert reopened.load_task_test_history("implementation", 1) == before
        assert (
            tuple(
                tuple(row)
                for row in reopened._connection.execute(
                    "SELECT name,sql FROM sqlite_master ORDER BY name"
                )
            )
            == schema
        )


@pytest.mark.parametrize("history", ["task", "test"])
def test_task_review_binding_rejects_history_only_cas_drift(frozen, history):
    if history == "task":
        disposition = frozen.store.apply_task(
            "implementation", TaskEnvelope(1, CancelRequested(), "late")
        )
    else:
        disposition = frozen.store.apply_task_test(
            "implementation", TaskTestEnvelope(1, 1, TaskTestCancelRequested(), "late")
        )
    assert disposition == Disposition.HISTORY_ONLY
    before = table_rows(frozen.store)
    with pytest.raises(ValueError):
        create(frozen.store, frozen.value)
    assert table_rows(frozen.store) == before


def test_task_review_binding_requires_complete_original_task_input(frozen):
    value = frozen.value
    invented = replace(
        value.executed_suite.task_input, created_at="different-original-input"
    )
    changed = replace(
        value, executed_suite=replace(value.executed_suite, task_input=invented)
    )
    before = table_rows(frozen.store)
    with pytest.raises(ValueError):
        create(frozen.store, changed)
    assert table_rows(frozen.store) == before


def test_task_review_precommit_failure_leaves_no_partial_round(frozen):
    before = table_rows(frozen.store)
    frozen.store._connection.execute(
        "CREATE TEMP TRIGGER fail_review_input BEFORE INSERT ON round_inputs BEGIN SELECT RAISE(ABORT, 'injected binding failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="injected binding failure"):
        create(frozen.store, frozen.value)
    assert table_rows(frozen.store) == before


def test_task_review_postcommit_exception_keeps_exact_bound_round(frozen, monkeypatch):
    from multi_agent.store.task_review_store import TaskReviewStore

    original = TaskReviewStore.create_task_review
    failure = sqlite3.OperationalError("injected postcommit failure")

    def after_commit(store, *args, **kwargs):
        original(store, *args, **kwargs)
        raise failure

    monkeypatch.setattr(TaskReviewStore, "create_task_review", after_commit)
    with pytest.raises(sqlite3.OperationalError) as caught:
        create(frozen.store, frozen.value)
    assert caught.value is failure
    assert frozen.store.load_round("executed-review") == round_state(frozen.value)
    assert (
        thaw_json(frozen.store.load_snapshot(frozen.value.snapshot_id))
        == frozen.value.to_dict()
    )
    assert not frozen.store.load_history("executed-review")
