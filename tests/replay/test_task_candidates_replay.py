"""Fold fixed candidate histories through pure transitions and SQLite twice.

Every path, object ID and occurrence time is synthetic. Expected runtime fields
and APPLIED event lists come from JSON, never from a prior reducer execution.
"""

import json
from pathlib import Path

import pytest

from multi_agent.core.dispatch import resolve_dispatch
from multi_agent.core.events import Completed, Error, FailureCategory, ProcessExited
from multi_agent.core.state import AttemptState, AttemptStatus, Disposition
from multi_agent.core.task_events import (
    TaskAttemptStarted,
    TaskCandidateFrozen,
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
from multi_agent.core.task_state import TaskAttempt, TaskInput, TaskPhase, TaskState
from multi_agent.core.tasks import (
    FrozenProjectRules,
    ImplementationConfig,
    NamedCommand,
    ProjectRules,
    TaskCard,
)
from multi_agent.core.transitions import apply_task
from multi_agent.core.workspaces import (
    WorkspaceControl,
    WorkspaceDelivery,
    WorkspaceRecord,
)
from multi_agent.store.task_store import TaskStore

SCENARIOS = Path(__file__).parent / "scenarios" / "candidates"
REQUIRED_SCENARIOS = {
    "task_candidate_published",
    "task_candidate_revoked",
    "task_candidate_late_after_retry",
    "task_candidate_conflict",
}


def _inputs():
    card = TaskCard(
        1,
        "T-9046",
        "Keep a candidate tied to its actual execution",
        "a" * 40,
        ("src/**",),
        (),
        (),
        ("A host-owned candidate",),
        ("Preserve original observations",),
        ("unit",),
        (),
    )
    rules = FrozenProjectRules(
        "/synthetic/repository/.git",
        card.baseline_commit,
        "b" * 40,
        "synthetic frozen rules: never read from a working directory\n",
        ProjectRules(
            1,
            (NamedCommand("unit", "synthetic-test", ("--never-execute",)),),
            (".multi-agent/**",),
            (),
            ImplementationConfig(
                "codex", "requested-coder", "medium", ("requested-coder",)
            ),
        ),
    )
    return TaskInput(card, rules, "Original synthetic requirement", "created")


def _reservation(inputs, attempt, occurred_at):
    root = f"/synthetic/attempt-{attempt}/workspace"
    started = TaskAttemptStarted(
        resolve_dispatch(inputs.card, inputs.frozen_rules),
        root,
        "candidate-replay-v1",
        occurred_at,
        managed=True,
    )
    prepared = TaskExecutionPrepared(
        WorkspaceRecord(
            inputs.frozen_rules.repository,
            "/synthetic/source",
            inputs.card.baseline_commit,
            inputs.card.baseline_commit,
            root,
            f"owner-{attempt}",
            (1, 100 + attempt),
            f"/synthetic/repository/.git/worktrees/attempt-{attempt}",
            (1, 200 + attempt),
            (
                WorkspaceControl(f"{root}/.git", (1, 300 + attempt), b"gitdir: mock\n"),
                WorkspaceControl(f"{root}/absent", None, None),
                WorkspaceControl(f"{root}/binary", (1, 400 + attempt), b"\x00\xff"),
            ),
            "sha1",
        ),
        f"initial-{attempt}",
        f"Frozen synthetic prompt {attempt}",
        {"type": "object", "additionalProperties": False, "properties": {}},
        TaskExecutionPolicy("codex", "/synthetic/codex", (1, 42), "c" * 64, 3, 30, 1),
        TaskDispatchPolicy(),
        TaskExecutionContext(
            f"/synthetic/attempt-{attempt}/context",
            (1, 500 + attempt),
            f"context-{attempt}",
            "d" * 64,
        ),
    )
    return (
        TaskEnvelope(attempt, started, occurred_at),
        TaskEnvelope(attempt, prepared, occurred_at),
    )


def _event(value):
    fields = dict(value)
    kind = fields.pop("type")
    if kind == TaskCandidateFrozen.kind:
        delivery = dict(fields.pop("delivery"))
        delivery["changed_paths"] = tuple(delivery["changed_paths"])
        return TaskCandidateFrozen(WorkspaceDelivery(**delivery), **fields)
    if kind == Error.kind:
        if fields.get("failure_category") is not None:
            fields["failure_category"] = FailureCategory(fields["failure_category"])
        return Error(**fields)
    if kind == TaskDiagnostic.kind:
        return TaskDiagnostic(_event(fields["diagnostic"]))
    if kind == TaskScopeBlocked.kind:
        fields["required_paths"] = tuple(fields["required_paths"])
        return TaskScopeBlocked(**fields)
    constructors = {
        item.kind: item
        for item in (
            Completed,
            ProcessExited,
            TaskReconciled,
            TaskEnvironmentObserved,
            TaskEvidenceRejected,
        )
    }
    return constructors[kind](**fields)


class CoreBackend:
    def __init__(self, inputs):
        self.state = TaskState(
            inputs.card.id,
            inputs.card.task_hash,
            inputs.frozen_rules.rules_hash,
            inputs.card.baseline_commit,
        )
        self.history = []

    def apply(self, envelope):
        updated, disposition = apply_task(self.state, envelope)
        self.state = updated
        self.history.append((envelope, disposition))
        return disposition

    def reserve(self, started, prepared):
        updated = reserve_task_execution(self.state, started, prepared)
        self.state = updated
        self.history.extend(
            ((started, Disposition.APPLIED), (prepared, Disposition.APPLIED))
        )

    def candidate(self, envelope, *, stale_cas):
        # CAS belongs to the store; pure routing must also keep old events
        # without inspecting their obsolete candidate qualification.
        return self.apply(envelope)

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
        self.path, self.inputs = path, inputs
        self.store = TaskStore(path)
        try:
            self.store.create_task(
                "candidate-replay",
                inputs.card,
                inputs.frozen_rules,
                inputs.requirements,
                inputs.created_at,
            )
        except BaseException:
            self.store.close()
            raise

    def apply(self, envelope):
        return self.store.apply_task("candidate-replay", envelope)

    def reserve(self, started, prepared):
        history = self.load_history()
        self.store.reserve_task_attempt(
            "candidate-replay",
            started,
            prepared,
            expected_history_length=len(history),
            expected_history_hash=task_execution_hash(history),
        )

    def candidate(self, envelope, *, stale_cas):
        history = () if stale_cas else self.load_history()
        return self.store.record_task_candidate(
            "candidate-replay",
            envelope,
            expected_history_length=len(history),
            expected_history_hash=task_execution_hash(history),
        )

    def load(self):
        return self.store.load_task("candidate-replay")

    def load_history(self):
        return self.store.load_task_history("candidate-replay")

    def reopen(self):
        self.store.close()
        self.store = TaskStore(self.path)
        assert self.store.load_task_input("candidate-replay") == self.inputs

    def close(self):
        self.store.close()


def _expect(state, expected, records):
    assert state.phase == TaskPhase(expected["phase"])
    assert (
        state.current_attempt.number if state.current_attempt is not None else None
    ) == expected["attempt"]
    assert state.delivery_ready is expected["delivery_ready"]
    assert state.candidate_ready is expected["candidate_ready"]
    candidate = expected["candidate"]
    assert state.current_candidate == (
        records[candidate].event if candidate is not None else None
    )


def _expected_state(inputs, value, records):
    attempts = []
    for item in value["attempts"]:
        runtime = dict(item["runtime"])
        runtime["status"] = AttemptStatus(runtime["status"])
        if runtime["failure_category"] is not None:
            runtime["failure_category"] = FailureCategory(runtime["failure_category"])
        reconciliation = item["reconciliation"]
        attempts.append(
            TaskAttempt(
                records[item["request"]].event,
                AttemptState(**runtime),
                records[reconciliation].event if reconciliation is not None else None,
                tuple(records[key].event for key in item["events"]),
            )
        )
    return TaskState(
        inputs.card.id,
        inputs.card.task_hash,
        inputs.frozen_rules.rules_hash,
        inputs.card.baseline_commit,
        TaskPhase(value["phase"]),
        tuple(attempts),
        _event(value["scope_block"]) if value["scope_block"] is not None else None,
    )


def _perform(backend, inputs, step, records):
    operation = step["op"]
    if operation == "reserve":
        started, prepared = _reservation(inputs, step["attempt"], step["at"])
        records[step["id"] + ".started"] = started
        records[step["id"] + ".prepared"] = prepared
        backend.reserve(started, prepared)
        return None, (started, prepared)
    if operation == "reopen":
        backend.reopen()
        return None, ()
    assert operation in {"event", "candidate"}
    envelope = TaskEnvelope(step["attempt"], _event(step["event"]), step["at"])
    records[step["id"]] = envelope
    disposition = (
        backend.candidate(envelope, stale_cas=step.get("stale_cas", False))
        if operation == "candidate"
        else backend.apply(envelope)
    )
    return disposition, (envelope,)


def _replay(backend, inputs, scenario):
    records, expected_history = {}, []
    for step in scenario["steps"]:
        before, before_history = backend.load(), backend.load_history()
        if "error" in step:
            assert step["error"] == "ValueError"
            with pytest.raises(ValueError) as raised:
                _perform(backend, inputs, step, records)
            assert type(raised.value) is ValueError
            assert backend.load() == before
            assert backend.load_history() == before_history
        else:
            disposition, envelopes = _perform(backend, inputs, step, records)
            expected = Disposition(step.get("disposition", "applied"))
            if "disposition" in step:
                assert disposition == expected
            expected_history.extend((envelope, expected) for envelope in envelopes)
        assert backend.load_history() == tuple(expected_history)
        _expect(backend.load(), step["expect"], records)
    state = backend.load()
    assert state == _expected_state(inputs, scenario["final"], records)
    _expect(state, scenario["final"], records)
    return state, backend.load_history()


@pytest.mark.parametrize(
    "path",
    [
        SCENARIOS / f"{name}.json"
        for name in sorted(
            REQUIRED_SCENARIOS
            | {path.stem for path in SCENARIOS.glob("task_candidate_*.json")}
        )
    ],
    ids=lambda path: path.stem,
)
def test_task_candidate_scenario(path, tmp_path):
    assert {
        item.stem for item in SCENARIOS.glob("task_candidate_*.json")
    } == REQUIRED_SCENARIOS
    document = json.loads(path.read_text(encoding="utf-8"))
    inputs = _inputs()
    for case_number, scenario in enumerate(document["cases"]):
        results = []
        for repetition in range(2):
            for kind in ("core", "sqlite"):
                backend = (
                    CoreBackend(inputs)
                    if kind == "core"
                    else StoreBackend(
                        tmp_path / f"{case_number}-{repetition}.sqlite", inputs
                    )
                )
                try:
                    results.append(_replay(backend, inputs, scenario))
                finally:
                    backend.close()
        assert all(result == results[0] for result in results)
