"""Independent declared rework scenarios, pure/SQLite, each repeated twice."""

import importlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from multi_agent.core import AttemptState, AttemptStatus, Disposition
from multi_agent.core.events import (
    CancelRequested,
    Completed,
    Error,
    FailureCategory,
    ProcessExited,
)
from multi_agent.core.task_events import TaskEnvelope, TaskReconciled
from multi_agent.core.task_execution import reserve_task_execution, task_execution_json
from multi_agent.core.task_state import TaskAttempt, TaskPhase
from multi_agent.core.transitions import apply_task
from multi_agent.store.task_review_store import TaskReviewStore

DIRECTORY = Path(__file__).parent / "scenarios/task-rework"
REQUIRED = {
    "one-rework-late",
    "failed-rework-retry-rejected",
    "cancelled-rework-retry-rejected",
}


def test_task_rework_required_scenarios_present():
    assert {path.stem for path in DIRECTORY.glob("*.json")} == REQUIRED


def event(step):
    kind = step["event"]
    if kind == "completed":
        return Completed(step["result"])
    if kind == "error":
        return Error(step["detail"])
    if kind == "cancel_requested":
        return CancelRequested()
    if kind == "process_exited":
        return ProcessExited(step["exit_code"])
    if kind == "task_reconciled":
        return TaskReconciled(
            step["digest"],
            True,
            "Synthetic stopped observation",
            "/synthetic/rework/execution.json",
            "e" * 64,
        )
    raise AssertionError(f"Unknown independently declared replay event {kind}")


def replay(scenario, backend, path, helpers):
    # This fixture builds only trusted synthetic baseline records; its expected
    # checkpoint is explicitly constructed, not accepted from the new reducer.
    # Both backends start with the same domain values. Only SQLite applies the
    # new events through real transactions; pure uses local immutable folding.
    with TaskReviewStore(path) as store:
        seed = helpers.synthetic(store)
        state = seed.state
        history = seed.history
        initial = state
        started, prepared = seed.pair
        envelopes = {"started": started, "prepared": prepared}
        for step in scenario["steps"]:
            operation = step["operation"]
            before = state, history
            if operation == "reserve":
                if backend == "pure":
                    state = reserve_task_execution(state, started, prepared)
                    history = (
                        *history,
                        (started, Disposition.APPLIED),
                        (prepared, Disposition.APPLIED),
                    )
                else:
                    store.reserve_task_rework_execution("run", started, prepared)
                    state, history = (
                        store.load_task("run"),
                        store.load_task_history("run"),
                    )
            elif operation == "old_control":
                old = replace(started, attempt=1, occurred_at=step["label"])
                envelopes[step["label"]] = old
                if backend == "pure":
                    state, disposition = apply_task(state, old)
                    assert disposition == Disposition.HISTORY_ONLY
                    history = (*history, (old, disposition))
                else:
                    with pytest.raises(ValueError):
                        store.reserve_task_rework_execution(
                            "run", old, replace(prepared, attempt=1)
                        )
                    assert (
                        store.load_task("run"),
                        store.load_task_history("run"),
                    ) == before
            elif operation in {"retry", "second_rework"}:
                request = (
                    helpers.implementations.reservation(3, value=seed.value)[0]
                    if operation == "retry"
                    else replace(started, attempt=3)
                )
                if backend == "pure":
                    with pytest.raises(ValueError):
                        apply_task(state, request)
                elif operation == "retry":
                    with pytest.raises(ValueError):
                        store.reserve_task_attempt(
                            "run",
                            request,
                            helpers.implementations.reservation(3, value=seed.value)[1],
                            expected_history_length=len(history),
                            expected_history_hash=helpers.task_execution_hash(history),
                        )
                else:
                    with pytest.raises(ValueError):
                        store.reserve_task_rework_execution(
                            "run", request, replace(prepared, attempt=3)
                        )
                assert (state, history) == before
                if backend == "sqlite":
                    assert (
                        store.load_task("run"),
                        store.load_task_history("run"),
                    ) == before
            else:
                envelope = TaskEnvelope(step["attempt"], event(step), step["label"])
                envelopes[step["label"]] = envelope
                if backend == "pure":
                    state, disposition = apply_task(state, envelope)
                    history = (*history, (envelope, disposition))
                else:
                    disposition = store.apply_task("run", envelope)
                    state, history = (
                        store.load_task("run"),
                        store.load_task_history("run"),
                    )
                assert disposition.value == step["disposition"]
            assert state.phase.value == step["phase"]
            assert state.current_attempt.runtime.status.value == step["runtime_status"]

        expected = scenario["expected"]
        runtime = AttemptState(
            2,
            AttemptStatus(expected["runtime_status"]),
            failure_category=FailureCategory(expected["failure_category"])
            if expected["failure_category"]
            else None,
            detail=expected["detail"],
            result=expected["result"],
            exit_code=expected["exit_code"],
        )
        expected_state = replace(
            initial,
            phase=TaskPhase(expected["phase"]),
            attempts=(
                *initial.attempts,
                TaskAttempt(
                    started.event,
                    runtime,
                    envelopes[expected["reconciliation"]].event,
                    tuple(
                        envelopes[label].event for label in expected["current_events"]
                    ),
                ),
            ),
        )
        assert state == expected_state
        expected_history = (
            *seed.history,
            *(
                (envelopes[label], Disposition(disposition))
                for label, disposition in expected["history"][backend]
            ),
        )
        assert history == expected_history
        if backend == "sqlite":
            with TaskReviewStore(path) as reopened:
                assert reopened.load_task("run") == expected_state
                assert reopened.load_task_history("run") == expected_history
        return task_execution_json((state, history))


@pytest.mark.parametrize(
    "scenario_path", sorted(DIRECTORY.glob("*.json")), ids=lambda path: path.stem
)
@pytest.mark.parametrize("backend", ["pure", "sqlite"])
def test_task_rework_replay(scenario_path, backend, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "unit"))
    # Import the helper module, never individual tests; no old test is collected
    # or executed as a fixture, and the existing replay scenarios remain intact.
    helpers = importlib.import_module("test_task_rework")
    scenario = json.loads(scenario_path.read_text())
    assert scenario["id"] == scenario_path.stem
    first = replay(scenario, backend, tmp_path / "first.sqlite3", helpers)
    second = replay(scenario, backend, tmp_path / "second.sqlite3", helpers)
    assert first == second
