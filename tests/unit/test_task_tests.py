"""Trusted synthetic test-stage observations, never an executed command."""

from dataclasses import FrozenInstanceError, replace

import pytest
import test_task_candidate as candidates
import test_task_execution as implementation_helpers

from multi_agent.core import Disposition
from multi_agent.core.events import Completed, FailureCategory, ProcessExited
from multi_agent.core.task_events import TaskEnvelope, TaskEvidenceRejected
from multi_agent.core.task_execution import reserve_task_execution, task_execution_hash
from multi_agent.core.task_test_events import (
    TaskTestAttemptFinished,
    TaskTestAttemptStarted,
    TaskTestCancelRequested,
    TaskTestCommandExited,
    TaskTestCommandStarted,
    TaskTestEnvelope,
    TaskTestProblem,
    TaskTestReconciled,
)
from multi_agent.core.task_test_input import TaskTestInput
from multi_agent.core.task_test_state import TaskTestPhase, TaskTestState
from multi_agent.core.tasks import NamedCommand
from multi_agent.core.transitions import (
    apply_task,
    apply_task_test,
    task_test_phase,
    task_test_ready,
)


def task_input():
    value = implementation_helpers.inputs()
    commands = (
        NamedCommand("checks", "synthetic-test", ("--never-execute",)),
        NamedCommand("checks-alias", "synthetic-test", ("--never-execute",)),
    )
    return replace(
        value,
        frozen_rules=replace(
            value.frozen_rules,
            rules=replace(value.frozen_rules.rules, commands=commands),
        ),
    )


def implementation():
    value = task_input()
    started, prepared = implementation_helpers.reservation(value=value)
    state = reserve_task_execution(
        implementation_helpers.initial(value), started, prepared
    )
    history = [(started, Disposition.APPLIED), (prepared, Disposition.APPLIED)]
    for event in (
        Completed({"synthetic": True}),
        ProcessExited(-15),
        candidates.reconciliation(),
    ):
        envelope = TaskEnvelope(1, event, "implementation-observed")
        state, disposition = apply_task(state, envelope)
        history.append((envelope, disposition))
    envelope = TaskEnvelope(1, candidates.candidate(state), "candidate")
    state, disposition = apply_task(state, envelope)
    history.append((envelope, disposition))
    return state, value, tuple(history)


def declared_input(impl, value, history):
    return TaskTestInput(
        task_run_id="run",
        implementation_attempt=impl.current_attempt.number,
        task_id=value.card.id,
        task_hash=value.card.task_hash,
        rules_hash=value.frozen_rules.rules_hash,
        task_input_hash=task_execution_hash(value),
        candidate=impl.current_candidate,
        commands=value.frozen_rules.rules.commands,
        declaration_source="Synthetic complete suite; no commands executed",
        task_history_length=len(history),
        task_history_hash=task_execution_hash(history),
    )


def test_task_test_finished_never_invents_exit_or_stop():
    impl, value, history = implementation()
    selected = declared_input(impl, value, history)
    state = TaskTestState("run", 1, selected)
    state, disposition = apply_task_test(
        state, TaskTestEnvelope(1, 1, TaskTestAttemptStarted(selected), "start"), impl
    )
    assert disposition == Disposition.APPLIED and state.phase == TaskTestPhase.RUNNING
    finished = TaskTestAttemptFinished()
    state, disposition = apply_task_test(
        state, TaskTestEnvelope(1, 1, finished, "finish"), impl
    )
    assert disposition == Disposition.APPLIED
    assert state.phase == TaskTestPhase.AWAITING_EVIDENCE
    assert state.current_attempt.commands == ()
    assert state.current_attempt.reconciliation is None
    assert state.current_attempt.finished
    assert state.current_attempt.events == (TaskTestAttemptStarted(selected), finished)


def setup_stage():
    impl, value, history = implementation()
    selected = declared_input(impl, value, history)
    state = TaskTestState("run", 1, selected)
    return impl, state


def observe(state, impl, event, *, attempt=None, implementation_attempt=1):
    if attempt is None:
        attempt = state.current_attempt.number if state.current_attempt else 1
    return apply_task_test(
        state,
        TaskTestEnvelope(
            implementation_attempt, attempt, event, "synthetic-observation"
        ),
        impl,
    )


def advance(state, impl, event, **kwargs):
    updated, disposition = observe(state, impl, event, **kwargs)
    assert disposition == Disposition.APPLIED
    assert updated.phase == task_test_phase(updated.current_attempt)
    return updated


def started():
    impl, state = setup_stage()
    return impl, advance(state, impl, TaskTestAttemptStarted(state.reference_input))


def receipt(
    sequence,
    *,
    stopped=True,
    qualified=True,
    anchored=True,
    detail="Synthetic host receipt",
):
    return TaskTestReconciled(
        sequence,
        stopped,
        detail,
        "/synthetic/test-receipt.json" if anchored else None,
        "f" * 64 if anchored else None,
        qualified,
    )


def complete_commands(state, impl, *, first_exit=0):
    for sequence in (1, 2):
        state = advance(state, impl, TaskTestCommandStarted(sequence))
        state = advance(
            state,
            impl,
            TaskTestCommandExited(sequence, first_exit if sequence == 1 else 0),
        )
        state = advance(state, impl, receipt(sequence))
    return state


def succeeded():
    impl, state = started()
    state = complete_commands(state, impl)
    return impl, advance(state, impl, TaskTestAttemptFinished())


def retryable(*, outcome="failed"):
    impl, state = started()
    if outcome == "failed":
        state = advance(
            state,
            impl,
            TaskTestProblem(
                None, FailureCategory.TESTS_FAILED, "Synthetic preflight failure"
            ),
        )
    elif outcome == "cancelled":
        state = advance(state, impl, TaskTestCancelRequested())
    state = advance(state, impl, receipt(None))
    state = advance(state, impl, TaskTestAttemptFinished())
    return impl, state


def test_task_test_named_order_preserves_nonzero_and_full_suite():
    impl, state = started()
    names = tuple(command.name for command in state.reference_input.commands)
    assert names == ("checks", "checks-alias")
    assert (
        state.reference_input.commands[0].argv == state.reference_input.commands[1].argv
    )
    with pytest.raises(ValueError):
        observe(state, impl, TaskTestCommandStarted(2))
    state = complete_commands(state, impl, first_exit=-9)
    assert state.phase == TaskTestPhase.RUNNING
    assert tuple(command.exit_code for command in state.current_attempt.commands) == (
        -9,
        0,
    )
    state = advance(state, impl, TaskTestAttemptFinished())
    assert state.phase == TaskTestPhase.FAILED
    assert not task_test_ready(state, impl)
    assert len(state.current_attempt.events) == 8
    with pytest.raises(ValueError):
        observe(state, impl, TaskTestCommandStarted(3))


@pytest.mark.parametrize("stopped", [None, False, True])
@pytest.mark.parametrize(
    "qualified,anchored", [(False, False), (False, True), (True, True)]
)
def test_task_test_success_requires_all_actual_evidence(stopped, qualified, anchored):
    impl, state = started()
    state = complete_commands(state, impl)
    latest = receipt(
        2, stopped=stopped, qualified=qualified, anchored=anchored, detail="latest"
    )
    state = advance(state, impl, latest)
    state = advance(state, impl, TaskTestAttemptFinished())
    expected = (
        TaskTestPhase.SUCCEEDED
        if stopped is True and qualified and anchored
        else TaskTestPhase.AWAITING_EVIDENCE
    )
    assert state.phase == expected
    assert task_test_ready(state, impl) is (expected == TaskTestPhase.SUCCEEDED)
    assert state.current_attempt.reconciliation.process_stopped is stopped
    assert tuple(item.exit_code for item in state.current_attempt.commands) == (0, 0)


@pytest.mark.parametrize("missing", ["not-run", "no-exit", "old-log-unqualified"])
def test_task_test_finished_cannot_fill_missing_named_evidence(missing):
    impl, state = started()
    state = advance(state, impl, TaskTestCommandStarted(1))
    if missing != "no-exit":
        state = advance(state, impl, TaskTestCommandExited(1, 0))
    state = advance(state, impl, receipt(1, qualified=missing != "old-log-unqualified"))
    if missing == "old-log-unqualified":
        state = advance(state, impl, TaskTestCommandStarted(2))
        state = advance(state, impl, TaskTestCommandExited(2, 0))
        state = advance(state, impl, receipt(2))
    state = advance(state, impl, TaskTestAttemptFinished())
    assert state.phase == TaskTestPhase.AWAITING_EVIDENCE
    if missing == "no-exit":
        assert state.current_attempt.commands[0].exit_code is None
    if missing == "old-log-unqualified":
        assert observe(state, impl, receipt(1)) == (state, Disposition.HISTORY_ONLY)
        assert not task_test_ready(state, impl)


@pytest.mark.parametrize("sequence", [None, 1])
@pytest.mark.parametrize("stop_before_finished", [False, True])
def test_task_test_cancel_and_finished_are_attempt_events(
    sequence, stop_before_finished
):
    impl, state = started()
    if sequence is not None:
        state = advance(state, impl, TaskTestCommandStarted(sequence))
    state = advance(state, impl, TaskTestCancelRequested())
    if stop_before_finished:
        state = advance(state, impl, receipt(sequence))
    assert state.phase == TaskTestPhase.CANCELLING
    state = advance(state, impl, TaskTestAttemptFinished())
    assert state.phase == (
        TaskTestPhase.CANCELLED if stop_before_finished else TaskTestPhase.CANCELLING
    )
    if sequence is not None:
        state = advance(state, impl, TaskTestCommandExited(sequence, 0))
        assert state.current_command.exit_code == 0
    if not stop_before_finished:
        state = advance(state, impl, receipt(sequence))
    assert state.phase == TaskTestPhase.CANCELLED
    assert state.current_attempt.cancel_requested and state.current_attempt.finished
    assert observe(state, impl, TaskTestCancelRequested()) == (
        state,
        Disposition.HISTORY_ONLY,
    )
    assert observe(state, impl, TaskTestAttemptFinished()) == (
        state,
        Disposition.HISTORY_ONLY,
    )
    state = advance(state, impl, receipt(sequence, stopped=False))
    assert state.phase == TaskTestPhase.CANCELLING
    state = advance(state, impl, receipt(sequence))
    assert state.phase == TaskTestPhase.CANCELLED


@pytest.mark.parametrize("kind", ["success", "failed", "cancelled"])
def test_task_test_reconciliation_revokes_and_restores_only_current_success(kind):
    if kind == "success":
        impl, state = succeeded()
        sequence = 2
    else:
        impl, state = retryable(outcome=kind)
        sequence = None
    original_events = state.current_attempt.events
    state = advance(
        state, impl, receipt(sequence, stopped=None, detail="stop withdrawn")
    )
    expected = {
        "success": TaskTestPhase.AWAITING_EVIDENCE,
        "failed": TaskTestPhase.FAILED,
        "cancelled": TaskTestPhase.CANCELLING,
    }[kind]
    assert state.phase == expected
    assert state.current_attempt.events[:-1] == original_events
    state = advance(state, impl, receipt(sequence))
    assert (
        state.phase
        == {
            "success": TaskTestPhase.SUCCEEDED,
            "failed": TaskTestPhase.FAILED,
            "cancelled": TaskTestPhase.CANCELLED,
        }[kind]
    )
    assert task_test_ready(state, impl) is (kind == "success")


def test_task_test_receipt_is_per_command_epoch():
    impl, state = started()
    state = advance(state, impl, receipt(None))
    state = advance(state, impl, TaskTestCommandStarted(1))
    assert state.current_attempt.reconciliation is None
    assert observe(state, impl, receipt(None, stopped=False)) == (
        state,
        Disposition.HISTORY_ONLY,
    )
    assert observe(state, impl, TaskTestProblem(None, None, "late preflight")) == (
        state,
        Disposition.HISTORY_ONLY,
    )
    state = advance(state, impl, TaskTestCommandExited(1, 3))
    with pytest.raises(ValueError):
        observe(state, impl, TaskTestCommandStarted(2))
    state = advance(state, impl, receipt(1, qualified=False, anchored=False))
    state = advance(state, impl, TaskTestCommandStarted(2))
    assert state.current_attempt.reconciliation is None
    assert state.current_command.reconciliation is None
    assert state.current_attempt.commands[0].reconciliation.process_stopped is True
    assert observe(state, impl, receipt(1)) == (state, Disposition.HISTORY_ONLY)


@pytest.mark.parametrize("stopped", [None, False])
def test_task_test_explicit_unknown_preflight_stop_blocks_command(stopped):
    impl, state = started()
    state = advance(state, impl, receipt(None, stopped=stopped))
    with pytest.raises(ValueError):
        observe(state, impl, TaskTestCommandStarted(1))


@pytest.mark.parametrize("outcome", ["failed", "cancelled", "incomplete"])
def test_task_test_retry_requires_finished_and_actual_settlement(outcome):
    impl, state = started()
    if outcome == "failed":
        state = advance(state, impl, TaskTestProblem(None, None, "preflight failed"))
    elif outcome == "cancelled":
        state = advance(state, impl, TaskTestCancelRequested())
    newer = replace(
        state.reference_input, task_history_length=99, task_history_hash="9" * 64
    )
    state = advance(state, impl, receipt(None))
    with pytest.raises(ValueError):
        observe(state, impl, TaskTestAttemptStarted(newer), attempt=2)
    state = advance(state, impl, receipt(None, stopped=None))
    state = advance(state, impl, TaskTestAttemptFinished())
    with pytest.raises(ValueError):
        observe(state, impl, TaskTestAttemptStarted(newer), attempt=2)
    state = advance(state, impl, receipt(None))
    prior = state.current_attempt
    state = advance(state, impl, TaskTestAttemptStarted(newer), attempt=2)
    assert state.attempts[0] == prior
    assert state.reference_input != newer and state.current_attempt.input == newer
    assert state.phase == TaskTestPhase.RUNNING
    assert state.current_attempt.commands == state.current_attempt.problems == ()
    assert state.current_attempt.reconciliation is None
    assert (
        not state.current_attempt.finished
        and not state.current_attempt.cancel_requested
    )
    assert state.current_attempt.events == (TaskTestAttemptStarted(newer),)


def test_task_test_success_never_retries_and_late_cancel_is_history_only():
    impl, state = succeeded()
    with pytest.raises(ValueError):
        observe(state, impl, TaskTestAttemptStarted(state.reference_input), attempt=2)
    assert observe(state, impl, TaskTestCancelRequested()) == (
        state,
        Disposition.HISTORY_ONLY,
    )


SEMANTIC_CHANGES = [
    "version",
    "version-bool",
    "source_kind",
    "declaration_source",
    "commands-shrink",
    "commands-order",
    "command-name",
    "command-argv",
    "task_run_id",
    "implementation_attempt",
    "task_id",
    "task_hash",
    "rules_hash",
    "task_input_hash",
    "candidate-anchor",
    "candidate-tree",
    "candidate-paths",
]


def changed_input(value, field):
    if field == "version-bool":
        return replace(value, version=True)
    if field.startswith("candidate-"):
        candidate = value.candidate
        if field == "candidate-anchor":
            candidate = replace(candidate, execution_log_sha256="0" * 64)
        else:
            delivery = replace(
                candidate.delivery,
                **(
                    {"tree": "0" * 40}
                    if field == "candidate-tree"
                    else {"changed_paths": ("src/another.py",)}
                ),
            )
            candidate = replace(candidate, delivery=delivery)
        return replace(value, candidate=candidate)
    if field.startswith("command"):
        commands = value.commands
        if field == "commands-shrink":
            commands = commands[:1]
        elif field == "commands-order":
            commands = tuple(reversed(commands))
        else:
            commands = (
                replace(
                    commands[0],
                    **(
                        {"name": "renamed"}
                        if field == "command-name"
                        else {"args": ("--different",)}
                    ),
                ),
                *commands[1:],
            )
        return replace(value, commands=commands)
    return replace(
        value,
        **{field: 2 if field in {"version", "implementation_attempt"} else "different"},
    )


@pytest.mark.parametrize("field", SEMANTIC_CHANGES)
def test_task_test_retry_semantics_compare_every_field_except_history(field):
    impl, state = retryable()
    changed = changed_input(state.reference_input, field)
    with pytest.raises(ValueError):
        observe(state, impl, TaskTestAttemptStarted(changed), attempt=2)


@pytest.mark.parametrize(
    "event",
    [
        TaskTestCommandExited(1, 0),
        TaskTestProblem(1, None, "late"),
        receipt(1),
        TaskTestCancelRequested(),
        TaskTestAttemptFinished(),
    ],
)
def test_task_test_old_observations_cannot_advance_state(event):
    impl, state = started()
    state = advance(state, impl, TaskTestCommandStarted(1))
    state = advance(state, impl, TaskTestCommandExited(1, 1))
    state = advance(state, impl, receipt(1))
    state = advance(state, impl, TaskTestAttemptFinished())
    state = advance(
        state, impl, TaskTestAttemptStarted(state.reference_input), attempt=2
    )
    assert observe(state, impl, event, attempt=1) == (state, Disposition.HISTORY_ONLY)
    newer_impl = replace(
        impl,
        attempts=(
            *impl.attempts,
            replace(
                impl.current_attempt,
                runtime=replace(impl.current_attempt.runtime, number=2),
            ),
        ),
    )
    assert observe(state, newer_impl, event, attempt=1) == (
        state,
        Disposition.HISTORY_ONLY,
    )
    for request in (
        TaskTestAttemptStarted(state.reference_input),
        TaskTestCommandStarted(1),
    ):
        with pytest.raises(ValueError):
            observe(state, newer_impl, request, attempt=1)


@pytest.mark.parametrize(
    "field", ["implementation_attempt", "test_attempt", "sequence"]
)
@pytest.mark.parametrize("number", [0, -1, True, 1.5, "1", 99])
def test_task_test_number_routing_rejects_unknown_before_old_isolation(field, number):
    impl, state = started()
    state = advance(state, impl, TaskTestCommandStarted(1))
    envelope = TaskTestEnvelope(1, 1, TaskTestCommandExited(1, 0))
    envelope = (
        replace(envelope, event=replace(envelope.event, sequence=number))
        if field == "sequence"
        else replace(envelope, **{field: number})
    )
    with pytest.raises(ValueError):
        apply_task_test(state, envelope, impl)


def test_task_test_source_revocation_blocks_requests_but_keeps_actual_facts():
    impl, state = started()
    state = advance(state, impl, TaskTestCommandStarted(1))
    revoked, _ = apply_task(
        impl, TaskEnvelope(1, TaskEvidenceRejected("archive no longer valid"))
    )
    assert not revoked.candidate_ready
    state = advance(state, revoked, TaskTestCommandExited(1, 0))
    state = advance(state, revoked, receipt(1))
    with pytest.raises(ValueError):
        observe(state, revoked, TaskTestCommandStarted(2))
    state = advance(state, revoked, TaskTestAttemptFinished())
    assert state.current_command.exit_code == 0 and not task_test_ready(state, revoked)
    with pytest.raises(ValueError):
        observe(
            state, revoked, TaskTestAttemptStarted(state.reference_input), attempt=2
        )


def test_task_test_duplicate_facts_and_conflicting_exit():
    impl, state = started()
    state = advance(state, impl, TaskTestCommandStarted(1))
    events = (
        TaskTestCommandExited(1, 0),
        TaskTestProblem(1, None, "first problem"),
        receipt(1),
    )
    for event in events:
        state = advance(state, impl, event)
        assert observe(state, impl, event) == (state, Disposition.HISTORY_ONLY)
    with pytest.raises(ValueError):
        observe(state, impl, TaskTestCommandExited(1, 1))
    state = advance(
        state, impl, TaskTestProblem(1, FailureCategory.TESTS_FAILED, "second problem")
    )
    state = advance(state, impl, receipt(1, stopped=False))
    state = advance(state, impl, receipt(1))
    assert state.current_attempt.problems == ()
    assert len(state.current_command.problems) == 2
    state = advance(state, impl, TaskTestAttemptFinished())
    assert state.phase == TaskTestPhase.FAILED and state.current_command.exit_code == 0


@pytest.mark.parametrize(
    "fields",
    [
        {"receipt_path": None},
        {"receipt_sha256": None},
        {"receipt_path": None, "receipt_sha256": None},
        {"receipt_path": ""},
        {"receipt_sha256": ""},
    ],
)
def test_task_test_incomplete_receipt_pair_is_rejected(fields):
    impl, state = started()
    with pytest.raises(ValueError):
        observe(state, impl, replace(receipt(None), **fields))


def test_task_test_frozen_values_and_phase_rebuild():
    impl, state = succeeded()
    assert task_test_phase(None) == TaskTestPhase.READY
    assert state.current_command is state.current_attempt.current_command
    for value, field in (
        (state, "phase"),
        (state.current_attempt, "finished"),
        (state.current_command, "exit_code"),
        (state.current_attempt.events[0], "input"),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(value, field, None)
        assert not hasattr(value, "__dict__")
    assert task_test_ready(state, impl)


@pytest.mark.parametrize("failed", [False, True])
def test_task_test_finished_incomplete_accepts_cancel_but_failed_does_not(failed):
    impl, state = started()
    state = advance(state, impl, TaskTestCommandStarted(1))
    if failed:
        state = advance(state, impl, TaskTestCommandExited(1, -9))
    state = advance(state, impl, TaskTestAttemptFinished())
    assert state.phase == (
        TaskTestPhase.FAILED if failed else TaskTestPhase.AWAITING_EVIDENCE
    )
    updated, disposition = observe(state, impl, TaskTestCancelRequested())
    if failed:
        assert updated == state and disposition == Disposition.HISTORY_ONLY
        assert not updated.current_attempt.cancel_requested
    else:
        assert disposition == Disposition.APPLIED
        assert updated.current_attempt.cancel_requested
        assert updated.phase == TaskTestPhase.CANCELLING
        updated = advance(updated, impl, receipt(1))
        assert updated.phase == TaskTestPhase.CANCELLED
