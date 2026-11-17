"""Bind an executed review to current task and test facts in one transaction."""

from collections.abc import Mapping
from typing import Any, cast

from multi_agent.core.events import JSONValue
from multi_agent.core.review_input import ExecutedCommand, ExecutedReviewInput
from multi_agent.core.state import RoundState
from multi_agent.core.task_execution import task_execution_hash
from multi_agent.core.task_test_events import (
    TaskTestCommandExited,
    TaskTestCommandReported,
)
from multi_agent.core.task_test_execution import prepared_for_test
from multi_agent.core.task_test_state import TaskTestAttempt, TaskTestCommandState

from .codec import encode_result
from .database import _canonical
from .task_codec import decode_task_input
from .task_test_store import TaskTestHistory, TaskTestStore


def _same(left: object, right: object) -> bool:
    return task_execution_hash(left) == task_execution_hash(right)


def _command_binding(
    value: ExecutedReviewInput,
    selected: TaskTestAttempt,
    history: TaskTestHistory,
    sequence: int,
    command: TaskTestCommandState,
    frozen: ExecutedCommand,
) -> None:
    suite = value.executed_suite
    prepared = suite.prepared
    source = prepared.checkout.input
    manifest = cast(Mapping[str, Any], frozen.manifest)
    result = cast(Mapping[str, Any], manifest["actual_result"])
    reconciliation = command.reconciliation
    if (
        command.sequence != sequence
        or command.exit_code is None
        or command.problems
        or reconciliation is None
        or reconciliation.sequence != sequence
        or reconciliation.process_stopped is not True
        or reconciliation.evidence_qualified is not True
        or not reconciliation.receipt_path
        or not reconciliation.receipt_sha256
    ):
        raise ValueError("review requires every declared command's complete evidence")
    expected = {
        "task_run_id": source.task_run_id,
        "implementation_attempt": source.implementation_attempt,
        "test_attempt": suite.test_attempt,
        "sequence": sequence,
        "input_hash": source.input_hash,
        "prepared_hash": task_execution_hash(prepared),
        "path": reconciliation.receipt_path,
        "sha256": reconciliation.receipt_sha256,
        "process_stopped": True,
        "qualified": True,
        "validation_error": None,
        "stop_scope": prepared.policy.required_stop_scope,
        "output_scope": prepared.policy.output_scope,
    }
    if not _same({name: manifest[name] for name in expected}, expected):
        raise ValueError("review command manifest differs from current reconciliation")
    prefix_length = manifest["history_length"]
    if not 0 <= prefix_length <= len(history) or manifest[
        "history_hash"
    ] != task_execution_hash(history[:prefix_length]):
        raise ValueError("review command manifest has another original history")
    reported = tuple(
        event
        for event in selected.events
        if isinstance(event, TaskTestCommandReported) and event.sequence == sequence
    )
    exited = tuple(
        event
        for event in selected.events
        if isinstance(event, TaskTestCommandExited) and event.sequence == sequence
    )
    if (
        len(reported) != 1
        or len(exited) != 1
        or not _same(
            (
                result["sequence"],
                result["reported_exit_code"],
                result["response"],
                result["exit_code"],
            ),
            (
                sequence,
                reported[0].reported_exit_code,
                reported[0].response,
                exited[0].exit_code,
            ),
        )
        or not _same(command.exit_code, exited[0].exit_code)
    ):
        raise ValueError("review manifest differs from actual command reports or exit")


class TaskReviewStore(TaskTestStore):
    """Reuse immutable round tables; source inspection remains a CLI boundary."""

    def create_task_review(
        self,
        round_id: str,
        state: RoundState,
        value: ExecutedReviewInput,
        metadata: JSONValue,
    ) -> None:
        suite = value.executed_suite
        source = suite.prepared.checkout.input
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            implementation = self._implementation(
                source.task_run_id, source.implementation_attempt
            )
            self._reservation_source(
                source.task_run_id,
                source.implementation_attempt,
                source,
                implementation,
            )
            original = decode_task_input(
                self._task_row(source.task_run_id)["input_payload"]
            )
            if not _same(suite.task_input, original):
                raise ValueError("review task input differs from its immutable source")
            expected_source = (
                original.frozen_rules.repository,
                original.card.baseline_commit,
                source.candidate.delivery.candidate,
                original.requirements,
                original.card.acceptance_criteria,
                (),
                (),
            )
            if not _same(
                (
                    value.source.repository,
                    value.source.baseline,
                    value.source.candidate,
                    value.source.requirements,
                    value.source.acceptance,
                    value.source.tests,
                    value.source.required_tests,
                ),
                expected_source,
            ):
                raise ValueError("review source differs from the frozen task candidate")
            test_state = self._read_test(
                self._required_stage(source.task_run_id, source.implementation_attempt)
            )
            history = self._read_test_history(
                source.task_run_id, source.implementation_attempt
            )
            self._reservation_cas(
                test_state,
                history,
                suite.test_attempt,
                suite.test_history_length,
                suite.test_history_hash,
            )
            selected = test_state.current_attempt
            if (
                selected is None
                or not _same(selected.input, source)
                or not _same(prepared_for_test(selected), suite.prepared)
                or not selected.finished
                or selected.cancel_requested
                or selected.problems
                or not selected.commands
                or len(selected.commands) != len(source.commands)
                or len(suite.commands) != len(source.commands)
                or not _same(
                    selected.reconciliation, selected.commands[-1].reconciliation
                )
            ):
                raise ValueError("review requires the current complete test attempt")
            for sequence, (command, frozen) in enumerate(
                zip(selected.commands, suite.commands, strict=True), 1
            ):
                _command_binding(value, selected, history, sequence, command, frozen)
            if (
                state.snapshot_id != value.snapshot_id
                or state.prompt_template_version != "executed-review-v2"
                or any(seat.attempts for seat in state.seats)
            ):
                raise ValueError("executed review must start a fresh bound round")
            encoded = _canonical(value.to_dict())
            existing = self._connection.execute(
                "SELECT payload FROM snapshots WHERE snapshot_id = ?",
                (state.snapshot_id,),
            ).fetchone()
            if existing is None:
                self._connection.execute(
                    "INSERT INTO snapshots VALUES (?, ?)", (state.snapshot_id, encoded)
                )
            elif existing["payload"] != encoded:
                raise ValueError("snapshot identity already has different content")
            self._insert_round(round_id, state)
            self._connection.execute(
                "INSERT INTO round_inputs VALUES (?, ?, ?, ?)",
                (round_id, state.snapshot_id, encode_result(metadata), None),
            )
