"""Fold identical synthetic task histories in memory and SQLite, twice each."""

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from multi_agent.core import Disposition
from multi_agent.core.dispatch import ImplementationChoice, resolve_dispatch
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
    reserve_task_execution,
    task_execution_hash,
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
from multi_agent.core.workspaces import WorkspaceControl, WorkspaceRecord
from multi_agent.store.task_store import TaskStore

SCENARIOS = Path(__file__).parent / "scenarios" / "tasks"
REQUIRED_SCENARIOS = {
    "retry-late",
    "cancel-diagnostic-exit",
    "restart-reconcile",
    "cancel-terminal-exit",
    "managed-preparation",
    "managed-evidence-rejected",
    "managed-late-result",
    "managed-retry-budget",
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
    if kind == TaskExecutionPrepared.kind:
        workspace = dict(values["workspace"])
        workspace["root_identity"] = tuple(workspace["root_identity"])
        workspace["git_identity"] = tuple(workspace["git_identity"])
        workspace["controls"] = tuple(
            WorkspaceControl(
                item["path"],
                tuple(item["identity"]) if item["identity"] is not None else None,
                bytes.fromhex(item["content"]["hex"])
                if item["content"] is not None
                else None,
            )
            for item in workspace["controls"]
        )
        policy = dict(values["policy"])
        policy["executable_identity"] = tuple(policy["executable_identity"])
        dispatch = dict(values["dispatch_policy"])
        dispatch["requested"] = (
            ImplementationChoice(**dispatch["requested"])
            if dispatch["requested"] is not None
            else None
        )
        dispatch["fallbacks"] = tuple(
            ImplementationChoice(**item) for item in dispatch["fallbacks"]
        )
        context = dict(values["context"])
        context["identity"] = tuple(context["identity"])
        return TaskExecutionPrepared(
            WorkspaceRecord(**workspace),
            values["initial_workspace_digest"],
            values["prompt"],
            values["output_schema"],
            TaskExecutionPolicy(**policy),
            TaskDispatchPolicy(**dispatch),
            TaskExecutionContext(**context),
        )
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
            TaskEnvironmentObserved,
            TaskEvidenceRejected,
        )
    }
    return constructors[kind](**values)


def _envelope(fields, decision):
    return TaskEnvelope(
        fields["attempt"],
        _event(fields["event"], decision),
        fields.get("occurred_at"),
    )


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

    def reserve(self, started, prepared):
        # Publish neither the intermediate state nor half a history on failure.
        state = reserve_task_execution(self.state, started, prepared)
        self.state = state
        self.history.extend(
            ((started, Disposition.APPLIED), (prepared, Disposition.APPLIED))
        )

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

    def reserve(self, started, prepared):
        history = self.load_history()
        self.store.reserve_task_attempt(
            "replay",
            started,
            prepared,
            expected_history_length=len(history),
            expected_history_hash=task_execution_hash(history),
        )

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
            "reconciliation": _reconciliation(attempt),
        }
        if attempt.request.managed:
            attempts[str(attempt.number)]["managed"] = True
            attempts[str(attempt.number)]["event_types"] = [
                event.kind for event in attempt.events
            ]
    scope_block = (
        None
        if state.scope_block is None
        else {
            "reason": state.scope_block.reason,
            "required_paths": list(state.scope_block.required_paths),
        }
    )
    return {**_observation(state), "attempts": attempts, "scope_block": scope_block}


def _reconciliation(attempt):
    receipt = attempt.reconciliation
    if receipt is None:
        return None
    if attempt.request.managed:
        return asdict(receipt)
    # The four frozen legacy fixtures predate the optional execution log receipt.
    return {
        "worktree_digest": receipt.worktree_digest,
        "process_stopped": receipt.process_stopped,
        "detail": receipt.detail,
    }


def _assert_managed_events(state, history):
    for attempt in state.attempts:
        if attempt.request.managed:
            events = tuple(
                envelope.event
                for envelope, disposition in history
                if envelope.attempt == attempt.number
                and disposition == Disposition.APPLIED
            )
            # Compare the whole immutable objects, not just event names/counts.
            assert attempt.events == events
            receipts = [event for event in events if isinstance(event, TaskReconciled)]
            assert attempt.reconciliation == (receipts[-1] if receipts else None)


def _replay(backend, scenario, decision):
    expected_history = []
    trace = []
    for step in scenario["steps"]:
        before = (backend.load(), backend.load_history())
        if step["op"] == "reopen":
            backend.reopen()
            assert (backend.load(), backend.load_history()) == before
        elif step["op"] == "reserve":
            started = _envelope(step["started"], decision)
            prepared = _envelope(step["prepared"], decision)
            if step.get("error") == "ValueError":
                with pytest.raises(ValueError):
                    backend.reserve(started, prepared)
                assert (backend.load(), backend.load_history()) == before
            else:
                backend.reserve(started, prepared)
                expected_history.extend(
                    ((started, Disposition.APPLIED), (prepared, Disposition.APPLIED))
                )
        else:
            assert step["op"] == "event"
            envelope = _envelope(step["envelope"], decision)
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
        _assert_managed_events(backend.load(), expected_history)
        trace.append((backend.load(), backend.load_history()))
    assert _final(backend.load()) == scenario["final"]
    backend.reopen()
    assert _final(backend.load()) == scenario["final"]
    assert backend.load_history() == tuple(expected_history)
    _assert_managed_events(backend.load(), expected_history)
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
