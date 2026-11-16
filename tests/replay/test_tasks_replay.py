"""Fold identical synthetic task histories in memory and SQLite, twice each."""

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from multi_agent.core import Disposition
from multi_agent.core.dispatch import resolve_dispatch
from multi_agent.core.events import (
    CancelRequested,
    Completed,
    Error,
    FailureCategory,
    Message,
    ProcessExited,
    SessionStarted,
    Usage,
    thaw_json,
)
from multi_agent.core.task_events import (
    TaskAttemptStarted,
    TaskDiagnostic,
    TaskEnvelope,
    TaskReconciled,
    TaskScopeBlocked,
)
from multi_agent.core.task_state import TaskInput, TaskState
from multi_agent.core.tasks import (
    FrozenProjectRules,
    ImplementationConfig,
    NamedCommand,
    ProjectRules,
    TaskCard,
)
from multi_agent.core.transitions import apply_task
from multi_agent.store.task_store import TaskStore

SCENARIOS = Path(__file__).parent / "scenarios" / "tasks"
REQUIRED_SCENARIOS = {
    "retry-late",
    "cancel-diagnostic-exit",
    "restart-reconcile",
    "cancel-terminal-exit",
}


def _inputs():
    card = TaskCard(
        1,
        "T-9000",
        "Synthetic implementation",
        "a" * 40,
        ("src/**",),
        (),
        (),
        ("A changed module",),
        ("Keep the public interface",),
        ("unit",),
        (),
    )
    text = (
        "version: 1\ncommands:\n  unit:\n    executable: synthetic-test\n    args: []\n"
        "protected_paths: [.multi-agent/**]\nimplementation: []\n"
        "default_implementation:\n  provider: codex\n  default_model: requested-coder\n"
        "  default_effort: medium\n"
    )
    rules = FrozenProjectRules(
        "/synthetic/repository/.git",
        card.baseline_commit,
        "b" * 40,
        text,
        ProjectRules(
            1,
            (NamedCommand("unit", "synthetic-test", ()),),
            (".multi-agent/**",),
            (),
            ImplementationConfig(
                "codex", "requested-coder", "medium", ("requested-coder",)
            ),
        ),
    )
    return TaskInput(
        card, rules, "Synthetic original requirement", "2026-11-16T00:00:00Z"
    )


def _event(fields, decision):
    values = dict(fields)
    kind = values.pop("type")
    if kind == TaskAttemptStarted.kind:
        return TaskAttemptStarted(decision=decision, **values)
    if kind == Error.kind:
        if values.get("failure_category") is not None:
            values["failure_category"] = FailureCategory(values["failure_category"])
        return Error(**values)
    if kind == TaskDiagnostic.kind:
        return TaskDiagnostic(_event(values["diagnostic"], decision))
    if kind == TaskScopeBlocked.kind:
        values["required_paths"] = tuple(values["required_paths"])
        return TaskScopeBlocked(**values)
    constructors = {
        event.kind: event
        for event in (
            CancelRequested,
            Completed,
            Message,
            ProcessExited,
            SessionStarted,
            Usage,
            TaskReconciled,
        )
    }
    return constructors[kind](**values)


class CoreBackend:
    def __init__(self, inputs):
        self.inputs = inputs
        self.state = TaskState(
            inputs.card.id,
            inputs.card.task_hash,
            inputs.frozen_rules.rules_hash,
            inputs.card.baseline_commit,
        )
        self.history = []

    def apply(self, envelope):
        self.state, disposition = apply_task(self.state, envelope)
        self.history.append((envelope, disposition))
        return disposition

    def load(self):
        return self.state

    def load_history(self):
        return tuple(self.history)

    def reopen(self):
        pass

    def close(self):
        pass


class StoreBackend:
    def __init__(self, path, inputs):
        self.path = path
        self.inputs = inputs
        self.store = TaskStore(path)
        try:
            self.store.create_task(
                "replay",
                inputs.card,
                inputs.frozen_rules,
                inputs.requirements,
                inputs.created_at,
            )
        except BaseException:
            self.store.close()
            raise

    def apply(self, envelope):
        return self.store.apply_task("replay", envelope)

    def load(self):
        return self.store.load_task("replay")

    def load_history(self):
        return self.store.load_task_history("replay")

    def reopen(self):
        self.store.close()
        self.store = TaskStore(self.path)
        assert self.store.load_task_input("replay") == self.inputs

    def close(self):
        self.store.close()


def _observation(state):
    return {
        "phase": state.phase,
        "current_attempt": state.current_attempt.number
        if state.current_attempt
        else None,
        "delivery_ready": state.delivery_ready,
    }


def _final(state):
    attempts = {}
    for attempt in state.attempts:
        runtime = attempt.runtime
        attempts[str(attempt.number)] = {
            "status": runtime.status,
            "failure_category": runtime.failure_category,
            "detail": runtime.detail,
            "retry_after": runtime.retry_after,
            "session_id": runtime.session_id,
            "reported_model": runtime.reported_model,
            "reported_effort": runtime.reported_effort,
            "reported_model_source": asdict(runtime.reported_model_source)
            if runtime.reported_model_source
            else None,
            "reported_effort_source": asdict(runtime.reported_effort_source)
            if runtime.reported_effort_source
            else None,
            "result": thaw_json(runtime.result),
            "exit_code": runtime.exit_code,
            "reconciliation": asdict(attempt.reconciliation)
            if attempt.reconciliation
            else None,
        }
    scope_block = (
        None
        if state.scope_block is None
        else {
            "reason": state.scope_block.reason,
            "required_paths": list(state.scope_block.required_paths),
        }
    )
    return {**_observation(state), "attempts": attempts, "scope_block": scope_block}


def _replay(backend, scenario, decision):
    expected_history = []
    trace = []
    for step in scenario["steps"]:
        before = (backend.load(), backend.load_history())
        if step["op"] == "reopen":
            backend.reopen()
            assert (backend.load(), backend.load_history()) == before
        else:
            assert step["op"] == "event"
            fields = step["envelope"]
            envelope = TaskEnvelope(
                fields["attempt"],
                _event(fields["event"], decision),
                fields.get("occurred_at"),
            )
            if step.get("error") == "ValueError":
                with pytest.raises(ValueError):
                    backend.apply(envelope)
                assert (backend.load(), backend.load_history()) == before
            else:
                disposition = Disposition(step["disposition"])
                assert backend.apply(envelope) == disposition
                expected_history.append((envelope, disposition))
                if disposition == Disposition.HISTORY_ONLY:
                    assert backend.load() == before[0]
        assert _observation(backend.load()) == step["expect"]
        assert backend.load_history() == tuple(expected_history)
        trace.append((backend.load(), backend.load_history()))
    assert _final(backend.load()) == scenario["final"]
    backend.reopen()
    assert _final(backend.load()) == scenario["final"]
    assert backend.load_history() == tuple(expected_history)
    return tuple(trace)


@pytest.mark.parametrize(
    "name",
    sorted(REQUIRED_SCENARIOS | {path.stem for path in SCENARIOS.glob("*.json")}),
)
def test_task_scenario(name, tmp_path):
    scenario = json.loads((SCENARIOS / f"{name}.json").read_text())
    inputs = _inputs()
    decision = resolve_dispatch(inputs.card, inputs.frozen_rules)
    expected = _replay(CoreBackend(inputs), scenario, decision)
    assert _replay(CoreBackend(inputs), scenario, decision) == expected
    for index in range(2):
        backend = StoreBackend(tmp_path / f"{name}-{index}.sqlite", inputs)
        try:
            assert _replay(backend, scenario, decision) == expected
        finally:
            backend.close()
