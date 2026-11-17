"""Fixed trusted-host facts through pure and SQLite controllers, twice each."""

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest
import test_task_candidates_replay as implementation_replays

from multi_agent.core import Disposition
from multi_agent.core.events import (
    CancelRequested,
    Completed,
    FailureCategory,
    ProcessExited,
)
from multi_agent.core.task_events import (
    TaskCandidateFrozen,
    TaskEnvelope,
    TaskEvidenceRejected,
    TaskReconciled,
)
from multi_agent.core.task_execution import reserve_task_execution, task_execution_hash
from multi_agent.core.task_state import TaskState
from multi_agent.core.task_test_events import (
    TaskTestAttemptFinished,
    TaskTestAttemptStarted,
    TaskTestCancelRequested,
    TaskTestCommandExited,
    TaskTestCommandReported,
    TaskTestCommandStarted,
    TaskTestEnvelope,
    TaskTestExecutionPrepared,
    TaskTestProblem,
    TaskTestReconciled,
)
from multi_agent.core.task_test_execution import (
    TaskTestCheckout,
    TaskTestContext,
    TaskTestPolicy,
    TaskTestScratch,
    reserve_task_test_execution,
)
from multi_agent.core.task_test_input import TaskTestInput
from multi_agent.core.task_test_state import (
    TaskTestAttempt,
    TaskTestCommandState,
    TaskTestPhase,
    TaskTestState,
)
from multi_agent.core.tasks import NamedCommand
from multi_agent.core.transitions import (
    apply_task,
    apply_task_test,
    task_test_phase,
    task_test_ready,
)
from multi_agent.core.workspaces import WorkspaceControl, WorkspaceDelivery
from multi_agent.store.task_test_store import TaskTestStore

SCENARIOS = Path(__file__).parent / "scenarios" / "test-stages"
REQUIRED_SCENARIOS = {
    "task_test_named_order",
    "task_test_unknown_stop_retry",
    "task_test_cancel_late_exit",
    "task_test_late_observations",
    "task_test_fresh_checkpoint",
    "task_test_reconcile_revoked",
    "task_test_managed_happy",
    "task_test_managed_nonzero",
    "task_test_managed_cancel_unknown",
    "task_test_managed_host_failure",
    "task_test_managed_restart_stale",
}


def source():
    original = implementation_replays._inputs()
    value = replace(
        original,
        frozen_rules=replace(
            original.frozen_rules,
            rules=replace(
                original.frozen_rules.rules,
                commands=(
                    NamedCommand("unit", "synthetic-test", ("--never-execute",)),
                    NamedCommand("unit-alias", "synthetic-test", ("--never-execute",)),
                ),
            ),
        ),
    )
    started, prepared = implementation_replays._reservation(
        value, 1, "implementation-start"
    )
    state = TaskState(
        value.card.id,
        value.card.task_hash,
        value.frozen_rules.rules_hash,
        value.card.baseline_commit,
    )
    state = reserve_task_execution(state, started, prepared)
    history = [(started, Disposition.APPLIED), (prepared, Disposition.APPLIED)]
    for event in (
        Completed({"synthetic": True}),
        ProcessExited(-15),
        TaskReconciled(
            "digest", True, "Synthetic stopped", "/synthetic/execution.json", "e" * 64
        ),
        TaskCandidateFrozen(
            WorkspaceDelivery(
                value.card.baseline_commit,
                "c" * 40,
                ("src/code.py",),
                "digest",
                "refs/multi-agent/candidates/owner-1",
                "d" * 40,
            ),
            "/synthetic/execution.json",
            "e" * 64,
        ),
    ):
        envelope = TaskEnvelope(1, event, "implementation-observed")
        state, disposition = apply_task(state, envelope)
        history.append((envelope, disposition))
    return state, value, tuple(history)


def declared(implementation, value, history):
    return TaskTestInput(
        task_run_id="test-replay",
        implementation_attempt=1,
        task_id=value.card.id,
        task_hash=value.card.task_hash,
        rules_hash=value.frozen_rules.rules_hash,
        task_input_hash=task_execution_hash(value),
        candidate=implementation.current_candidate,
        commands=value.frozen_rules.rules.commands,
        declaration_source="Synthetic suite; no execution",
        task_history_length=len(history),
        task_history_hash=task_execution_hash(history),
    )


class CoreBackend:
    def __init__(self, _path):
        self.implementation, self.value, self.task_history = source()
        self.reference = declared(self.implementation, self.value, self.task_history)
        self.state = TaskTestState("test-replay", 1, self.reference)
        self.history = ()

    def close(self):
        pass

    def reopen(self):
        pass

    def reserve_pair(self, started, prepared, cas):
        self._reservation(started, cas)
        checkout = prepared.event.checkout
        if (
            checkout.observed_test_attempt,
            checkout.test_history_length,
            checkout.test_history_hash,
        ) != (
            self.state.current_attempt.number if self.state.current_attempt else None,
            len(self.history),
            task_execution_hash(self.history),
        ):
            raise ValueError("managed artifact checkpoint changed")
        updated = reserve_task_test_execution(
            self.state, started, prepared, self.implementation
        )
        self.state = updated
        self.history = (
            *self.history,
            (started, Disposition.APPLIED),
            (prepared, Disposition.APPLIED),
        )

    def fresh_input(self):
        return declared(self.implementation, self.value, self.task_history)

    def observe_implementation(self, action):
        event = (
            CancelRequested()
            if action == "append-history"
            else TaskEvidenceRejected("Synthetic implementation retry")
        )
        envelope = TaskEnvelope(1, event, f"source-{action}")
        updated, disposition = apply_task(self.implementation, envelope)
        self.implementation = updated
        self.task_history = (*self.task_history, (envelope, disposition))
        if action == "retry":
            pair = implementation_replays._reservation(
                self.value, 2, "implementation-retry"
            )
            self.implementation = reserve_task_execution(self.implementation, *pair)
            self.task_history = (
                *self.task_history,
                *((item, Disposition.APPLIED) for item in pair),
            )

    def apply(self, envelope, cas):
        request = isinstance(
            envelope.event, (TaskTestAttemptStarted, TaskTestCommandStarted)
        )
        if request:
            self._reservation(envelope, cas)
        updated, disposition = apply_task_test(
            self.state, envelope, self.implementation
        )
        # The controller commits neither state nor backend history until every guard succeeds.
        self.state = updated
        self.history = (*self.history, (envelope, disposition))

    def _reservation(self, envelope, cas):
        state = self.state
        current = state.current_attempt
        value = (
            envelope.event.input
            if isinstance(envelope.event, TaskTestAttemptStarted)
            else current.input
        )
        actual = self.implementation
        if (
            not actual.candidate_ready
            or actual.current_attempt.number != envelope.implementation_attempt
        ):
            raise ValueError("current implementation candidate required")
        if (
            value.task_run_id,
            value.implementation_attempt,
            value.task_id,
            value.task_hash,
            value.rules_hash,
            value.task_input_hash,
        ) != (
            state.task_run_id,
            state.implementation_attempt,
            self.value.card.id,
            self.value.card.task_hash,
            self.value.frozen_rules.rules_hash,
            task_execution_hash(self.value),
        ):
            raise ValueError("source identity changed")
        if task_execution_hash(value.candidate) != task_execution_hash(
            actual.current_candidate
        ):
            raise ValueError("candidate changed")
        registry = {item.name: item for item in self.value.frozen_rules.rules.commands}
        if any(
            item.name not in registry or item != registry[item.name]
            for item in value.commands
        ):
            raise ValueError("registry changed")
        if (value.task_history_length, value.task_history_hash) != (
            len(self.task_history),
            task_execution_hash(self.task_history),
        ):
            raise ValueError("implementation history changed")
        if (
            cas["expected_test_attempt"],
            cas["expected_history_length"],
            cas["expected_history_hash"],
        ) != (
            current.number if current else None,
            len(self.history),
            task_execution_hash(self.history),
        ):
            raise ValueError("test history changed")
        if isinstance(envelope.event, TaskTestCommandStarted) and cas[
            "expected_command_sequence"
        ] != (state.current_command.sequence if state.current_command else None):
            raise ValueError("command changed")


class StoreBackend:
    def __init__(self, path):
        self.path = path
        self.store = TaskTestStore(path)
        implementation, value, history = source()
        self.value = value
        self.reference = declared(implementation, value, history)
        self.store.create_task(
            "test-replay",
            value.card,
            value.frozen_rules,
            value.requirements,
            value.created_at,
        )
        self.store.reserve_task_attempt(
            "test-replay",
            history[0][0],
            history[1][0],
            expected_history_length=0,
            expected_history_hash=task_execution_hash(()),
        )
        for envelope, disposition in history[2:-1]:
            assert self.store.apply_task("test-replay", envelope) == disposition
        prefix = self.store.load_task_history("test-replay")
        self.store.record_task_candidate(
            "test-replay",
            history[-1][0],
            expected_history_length=len(prefix),
            expected_history_hash=task_execution_hash(prefix),
        )

    @property
    def implementation(self):
        return self.store.load_task("test-replay")

    @property
    def task_history(self):
        return self.store.load_task_history("test-replay")

    @property
    def state(self):
        try:
            return self.store.load_task_test("test-replay", 1)
        except KeyError as error:
            if error.args != (("test-replay", 1),):
                raise
            return TaskTestState("test-replay", 1, self.reference)

    @property
    def history(self):
        try:
            return self.store.load_task_test_history("test-replay", 1)
        except KeyError as error:
            if error.args != (("test-replay", 1),):
                raise
            return ()

    def fresh_input(self):
        return declared(self.implementation, self.value, self.task_history)

    def close(self):
        self.store.close()

    def reopen(self):
        self.store.close()
        self.store = TaskTestStore(self.path)

    def reserve_pair(self, started, prepared, cas):
        self.store.reserve_task_test_execution("test-replay", started, prepared, **cas)

    def observe_implementation(self, action):
        event = (
            CancelRequested()
            if action == "append-history"
            else TaskEvidenceRejected("Synthetic implementation retry")
        )
        self.store.apply_task("test-replay", TaskEnvelope(1, event, f"source-{action}"))
        if action == "retry":
            pair = implementation_replays._reservation(
                self.value, 2, "implementation-retry"
            )
            prefix = self.task_history
            self.store.reserve_task_attempt(
                "test-replay",
                *pair,
                expected_history_length=len(prefix),
                expected_history_hash=task_execution_hash(prefix),
            )

    def apply(self, envelope, cas):
        if isinstance(envelope.event, TaskTestAttemptStarted):
            self.store.reserve_task_test_attempt("test-replay", envelope, **cas)
        elif isinstance(envelope.event, TaskTestCommandStarted):
            self.store.reserve_task_test_command("test-replay", envelope, **cas)
        else:
            self.store.apply_task_test("test-replay", envelope)


def event_for(step, backend):
    raw = copy.deepcopy(step["event"])
    kind = raw.pop("type")
    constructors = {
        item.kind: item
        for item in (
            TaskTestAttemptFinished,
            TaskTestCancelRequested,
            TaskTestCommandStarted,
            TaskTestCommandExited,
            TaskTestCommandReported,
            TaskTestProblem,
            TaskTestReconciled,
        )
    }
    if kind == TaskTestAttemptStarted.kind:
        value = (
            backend.fresh_input()
            if step.get("input", "fresh") == "fresh"
            else backend.reference
        )
        mutation = step.get("input_change")
        if mutation == "shrink":
            value = replace(value, commands=value.commands[:1])
        elif mutation == "order":
            value = replace(value, commands=tuple(reversed(value.commands)))
        elif mutation == "source":
            value = replace(value, declaration_source="different declared suite")
        elif mutation == "anchor":
            value = replace(
                value, candidate=replace(value.candidate, execution_log_sha256="0" * 64)
            )
        return TaskTestAttemptStarted(value)
    if kind == TaskTestProblem.kind and raw["category"] is not None:
        raw["category"] = FailureCategory(raw["category"])
    return constructors[kind](**raw)


def managed_preparation(started, backend, step):
    number = started.test_attempt
    root, inode = f"/synthetic/test-stage-{number}", number * 100
    value = started.event.input
    current = backend.state.current_attempt
    prepared = TaskTestExecutionPrepared(
        TaskTestCheckout(
            input=value,
            repository=backend.value.frozen_rules.repository,
            database_path="/synthetic/test-stage.sqlite",
            database_identity=(1, 1),
            database_parent_identity=(1, 2),
            root=f"{root}/checkout",
            root_identity=(1, inode),
            git_directory=f"{root}/checkout/.git",
            git_identity=(1, inode + 1),
            owner_token=f"owner-{number}",
            object_format="sha1",
            initial_digest=value.candidate.delivery.digest,
            controls=(WorkspaceControl("HEAD", (1, inode + 2), b"synthetic\n"),),
            objects=(
                ("commit", value.candidate.delivery.candidate),
                ("tree", value.candidate.delivery.tree),
            ),
            observed_test_attempt=current.number if current else None,
            test_history_length=len(backend.history),
            test_history_hash=task_execution_hash(backend.history),
        ),
        TaskTestContext(
            path=f"{root}/context",
            identity=(1, inode + 3),
            token=f"context-{number}",
            manifest_sha256="c" * 64,
        ),
        TaskTestPolicy(
            executable="/synthetic/codex",
            executable_identity=(1, 9),
            executable_sha256="d" * 64,
            timeout=30,
            grace=2,
            output_bytes_limit=8192,
        ),
        TaskTestScratch(
            path=f"{root}/scratch",
            identity=(1, inode + 4),
            home_identity=(1, inode + 5),
            tmp_identity=(1, inode + 6),
            cache_identity=(1, inode + 7),
        ),
    )
    if step.get("prepared_change") == "checkpoint":
        prepared = replace(
            prepared, checkout=replace(prepared.checkout, test_history_hash="0" * 64)
        )
    return replace(
        started, event=prepared, occurred_at=started.occurred_at + ":prepared"
    )


def expected_state(step, envelopes, reference):
    attempts = []
    for expected in step["expect"]["attempts"]:

        def event_at(index):
            return envelopes[index].event if index is not None else None

        commands = tuple(
            TaskTestCommandState(
                item["sequence"],
                item["exit_code"],
                event_at(item["reconciliation"]),
                tuple(event_at(index) for index in item["problems"]),
            )
            for item in expected["commands"]
        )
        attempts.append(
            TaskTestAttempt(
                expected["number"],
                event_at(expected["input_step"]).input,
                commands,
                tuple(event_at(index) for index in expected["problems"]),
                expected["cancel_requested"],
                expected["finished"],
                event_at(expected["reconciliation"]),
                tuple(event_at(index) for index in expected["events"]),
            )
        )
    return TaskTestState(
        "test-replay",
        1,
        reference,
        TaskTestPhase(step["expect"]["phase"]),
        tuple(attempts),
    )


def test_task_test_required_scenarios_present():
    assert {
        file.stem for file in SCENARIOS.glob("task_test_*.json")
    } == REQUIRED_SCENARIOS


@pytest.mark.parametrize(
    "backend_type", [CoreBackend, StoreBackend], ids=["core", "sqlite"]
)
@pytest.mark.parametrize(
    "path", sorted(SCENARIOS.glob("task_test_*.json")), ids=lambda path: path.stem
)
def test_task_test_replays(path, backend_type, tmp_path):
    assert {
        file.stem for file in SCENARIOS.glob("task_test_*.json")
    } == REQUIRED_SCENARIOS
    scenario = json.loads(path.read_text())
    assert scenario["evidence"] == "synthetic trusted host facts; no commands executed"
    for repeat in range(2):
        backend = backend_type(tmp_path / f"{path.stem}-{repeat}.sqlite")
        envelopes = {}
        try:
            for index, step in enumerate(scenario["steps"]):
                before = backend.state, backend.history
                if step.get("restart"):
                    backend.reopen()
                elif "implementation" in step:
                    backend.observe_implementation(step["implementation"])
                else:
                    event = event_for(step, backend)
                    envelope = TaskTestEnvelope(
                        step.get("implementation_attempt", 1),
                        step.get("test_attempt", 1),
                        event,
                        step["occurred_at"],
                    )
                    envelopes[index] = envelope
                    prepared = None
                    if step.get("managed"):
                        prepared = managed_preparation(envelope, backend, step)
                        envelopes[f"{index}:prepared"] = prepared
                    current = backend.state.current_attempt
                    cas = {
                        "expected_test_attempt": current.number if current else None,
                        "expected_history_length": len(backend.history),
                        "expected_history_hash": task_execution_hash(backend.history),
                    }
                    if isinstance(event, TaskTestCommandStarted):
                        cas["expected_command_sequence"] = (
                            backend.state.current_command.sequence
                            if backend.state.current_command
                            else None
                        )
                    cas.update(step.get("cas", {}))
                    if step.get("error"):
                        with pytest.raises(ValueError):
                            if prepared is None:
                                backend.apply(envelope, cas)
                            else:
                                backend.reserve_pair(envelope, prepared, cas)
                        assert (backend.state, backend.history) == before
                    else:
                        if prepared is None:
                            backend.apply(envelope, cas)
                        else:
                            backend.reserve_pair(envelope, prepared, cas)
                wanted = expected_state(step, envelopes, backend.reference)
                assert backend.state == wanted
                assert task_execution_hash(backend.state) == task_execution_hash(wanted)
                assert backend.state.phase == task_test_phase(
                    backend.state.current_attempt
                )
                expected_history = tuple(
                    (envelopes[item["step"]], Disposition(item["disposition"]))
                    for item in step["expect"]["history"]
                )
                assert backend.history == expected_history
                assert (
                    task_test_ready(backend.state, backend.implementation)
                    is step["expect"]["ready"]
                )
        finally:
            backend.close()
