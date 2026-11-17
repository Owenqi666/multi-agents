"""Freeze and recheck actual candidate/test evidence for a read-only review."""

from pathlib import Path

from multi_agent.core import Disposition
from multi_agent.core.review_input import (
    ExecutedCommand,
    ExecutedLog,
    ExecutedReviewInput,
    ExecutedSuite,
)
from multi_agent.core.task_execution import (
    task_execution_hash,
    task_execution_json,
    task_execution_value,
)
from multi_agent.core.task_test_events import TaskTestProblem
from multi_agent.core.task_test_execution import prepared_for_test
from multi_agent.core.task_test_state import TaskTestAttempt, TaskTestState
from multi_agent.store.task_test_store import TaskTestHistory, TaskTestStore

from .review_inputs import MAX_REVIEW_INPUT_BYTES, decode_review_input
from .snapshot import _string, freeze_repository
from .task_candidates import _capture, inspect_task_candidate
from .task_test_evidence import (
    TaskTestCommandManifest,
    _read,
    inspect_task_test_command,
)
from .workspaces import ExecutorLease


def _coordinates(run_id: str, implementation: int, attempt: int) -> None:
    _string(run_id, "task run id", nonempty=True)
    if any(
        type(number) is not int or number < 1 for number in (implementation, attempt)
    ):
        raise ValueError(
            "review implementation and test attempts must be positive integers"
        )


def _selection(
    store: TaskTestStore, run_id: str, implementation: int, attempt: int
) -> tuple[TaskTestState, TaskTestAttempt, TaskTestHistory]:
    state, history = store.load_task_test_checkpoint(run_id, implementation)
    selected = state.current_attempt if state is not None else None
    if selected is None or selected.number != attempt:
        raise ValueError("review requires the current test attempt")
    if (
        not selected.finished
        or selected.cancel_requested
        or any(isinstance(event, TaskTestProblem) for event in selected.events)
        or tuple(command.sequence for command in selected.commands)
        != tuple(range(1, len(selected.input.commands) + 1))
        or any(
            command.exit_code is None
            or command.problems
            or command.reconciliation is None
            or command.reconciliation.process_stopped is not True
            or not command.reconciliation.evidence_qualified
            for command in selected.commands
        )
    ):
        raise ValueError("review requires a complete, settled and qualified test suite")
    assert state is not None
    return state, selected, history


def _log(archive: TaskTestCommandManifest, name: str, budget: int) -> ExecutedLog:
    receipt = next(
        item for item in archive.receipt.logs if Path(item.path).name == name
    )
    expected = Path(archive.path).parent / name
    if receipt.path != str(expected) or receipt.byte_count is None:
        raise ValueError("review log does not belong to the inspected command archive")
    metadata, raw = _read(expected, budget, expected_size=receipt.byte_count)
    if metadata["identity"] != receipt.identity or metadata["sha256"] != receipt.sha256:
        raise ValueError("review log changed after test archive inspection")
    return ExecutedLog(content=raw)


def prepare_task_review_input(
    store: TaskTestStore,
    task_run_id: str,
    implementation_attempt: int,
    test_attempt: int,
    lease: ExecutorLease,
) -> ExecutedReviewInput:
    """Read-only preparation; the returned value is not a round or launch grant."""
    _coordinates(task_run_id, implementation_attempt, test_attempt)
    guard = _capture(store, task_run_id, implementation_attempt, lease, bound=True)
    state, selected, history = _selection(
        store, task_run_id, implementation_attempt, test_attempt
    )
    prepared = prepared_for_test(selected)
    if prepared is None:
        raise ValueError("review requires an actual managed test preparation")
    test_input = prepared.checkout.input
    if (
        task_execution_hash(guard.value) != test_input.task_input_hash
        or len(guard.history) != test_input.task_history_length
        or task_execution_hash(guard.history) != test_input.task_history_hash
        or task_execution_hash(selected.input) != task_execution_hash(test_input)
    ):
        raise ValueError("test suite differs from the original task input or history")
    candidate = inspect_task_candidate(
        store, task_run_id, implementation_attempt, lease
    )
    if task_execution_hash(candidate) != task_execution_hash(test_input.candidate):
        raise ValueError("review and test evidence refer to different candidates")
    source = freeze_repository(
        lease.source,
        guard.value.card.baseline_commit,
        candidate.delivery.candidate,
        guard.value.requirements,
        guard.value.card.acceptance_criteria,
    )
    commands: list[ExecutedCommand] = []
    # Raw bytes are encoded as hex. This early bound prevents allocating logs
    # whose encoded contents alone would exceed the complete input budget.
    raw_bytes = 0
    for sequence in range(1, len(test_input.commands) + 1):
        archive = inspect_task_test_command(
            store, task_run_id, implementation_attempt, test_attempt, sequence, lease
        )
        if not archive.qualified or archive.process_stopped is not True:
            raise ValueError(
                archive.validation_error or "test archive is not qualified"
            )
        for item in archive.receipt.logs:
            if Path(item.path).name in ("stdout.bin", "stderr.bin"):
                if item.byte_count is None:
                    raise ValueError("review output has no observed byte count")
                raw_bytes += item.byte_count
        if raw_bytes * 2 > MAX_REVIEW_INPUT_BYTES:
            raise ValueError("executed review input exceeds its canonical byte budget")
        commands.append(
            ExecutedCommand(
                manifest=task_execution_value(archive),
                stdout=_log(archive, "stdout.bin", prepared.policy.output_bytes_limit),
                stderr=_log(archive, "stderr.bin", prepared.policy.output_bytes_limit),
            )
        )
    value = ExecutedReviewInput(
        source=source,
        executed_suite=ExecutedSuite(
            task_input=guard.value,
            prepared=prepared,
            test_attempt=test_attempt,
            test_history_length=len(history),
            test_history_hash=task_execution_hash(history),
            commands=tuple(commands),
        ),
    )
    if (
        len(task_execution_json(value.to_dict()).encode("utf-8"))
        > MAX_REVIEW_INPUT_BYTES
    ):
        raise ValueError("executed review input exceeds its canonical byte budget")
    decode_review_input(value.to_dict(), expected_id=value.snapshot_id)
    if task_execution_hash(
        inspect_task_candidate(store, task_run_id, implementation_attempt, lease)
    ) != task_execution_hash(candidate):
        raise ValueError("candidate changed while freezing the review input")
    guard.check()
    current_state, _, current_history = _selection(
        store, task_run_id, implementation_attempt, test_attempt
    )
    if task_execution_hash((current_state, current_history)) != task_execution_hash(
        (state, history)
    ):
        raise ValueError("test state or original history changed while freezing review")
    guard.check()
    return value


def inspect_task_review_source(
    store: TaskTestStore, value: ExecutedReviewInput, lease: ExecutorLease
) -> None:
    """Recheck current eligibility without replacing the frozen input/checkpoint."""
    suite = value.executed_suite
    test_input = suite.prepared.checkout.input
    run_id, implementation = test_input.task_run_id, test_input.implementation_attempt
    guard = _capture(store, run_id, implementation, lease, bound=True)
    state, selected, history = _selection(
        store, run_id, implementation, suite.test_attempt
    )
    prepared = prepared_for_test(selected)
    if (
        task_execution_hash(guard.value) != task_execution_hash(suite.task_input)
        or len(guard.history) != test_input.task_history_length
        or task_execution_hash(guard.history) != test_input.task_history_hash
        or task_execution_hash(prepared) != task_execution_hash(suite.prepared)
        or suite.test_history_length > len(history)
        or task_execution_hash(history[: suite.test_history_length])
        != suite.test_history_hash
        or any(
            disposition != Disposition.HISTORY_ONLY
            for _, disposition in history[suite.test_history_length :]
        )
    ):
        raise ValueError("review source or its original history checkpoint changed")
    for sequence, command in enumerate(suite.commands, 1):
        archive = inspect_task_test_command(
            store, run_id, implementation, suite.test_attempt, sequence, lease
        )
        if not archive.qualified or task_execution_hash(archive) != task_execution_hash(
            command.manifest
        ):
            raise ValueError(
                "current test evidence differs from the frozen review input"
            )
        if (
            _log(archive, "stdout.bin", suite.prepared.policy.output_bytes_limit)
            != command.stdout
            or _log(archive, "stderr.bin", suite.prepared.policy.output_bytes_limit)
            != command.stderr
        ):
            raise ValueError("current test output differs from the frozen review input")
    candidate = inspect_task_candidate(store, run_id, implementation, lease)
    if task_execution_hash(candidate) != task_execution_hash(test_input.candidate):
        raise ValueError("review candidate is no longer current")
    current_state, _, current_history = _selection(
        store, run_id, implementation, suite.test_attempt
    )
    if (
        task_execution_hash(current_state) != task_execution_hash(state)
        or len(current_history) < len(history)
        or task_execution_hash(current_history[: len(history)])
        != task_execution_hash(history)
        or any(
            disposition != Disposition.HISTORY_ONLY
            for _, disposition in current_history[len(history) :]
        )
    ):
        raise ValueError("test source changed during current review verification")
    guard.check()
