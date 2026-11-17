"""Bind an executed review to current task and test facts in one transaction."""

from collections.abc import Mapping
from typing import Any, cast

from multi_agent.core.dispatch import resolve_dispatch
from multi_agent.core.events import JSONValue, freeze_json, thaw_json
from multi_agent.core.review_input import ExecutedCommand, ExecutedReviewInput
from multi_agent.core.state import Disposition, RoundState
from multi_agent.core.task_events import (
    TaskEnvelope,
    TaskExecutionPrepared,
    TaskReworkStarted,
)
from multi_agent.core.task_execution import (
    reserve_task_execution,
    task_execution_hash,
    task_execution_value,
)
from multi_agent.core.task_rework import TaskReworkSource
from multi_agent.core.task_state import TaskInput, TaskState
from multi_agent.core.task_test_events import (
    TaskTestCommandExited,
    TaskTestCommandReported,
)
from multi_agent.core.task_test_execution import prepared_for_test
from multi_agent.core.task_test_state import TaskTestAttempt, TaskTestCommandState

from .codec import decode_envelope, decode_result, encode_result
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

    def planning_run_for_task(self, task_run_id: str) -> str:
        with self._connection:
            self._connection.execute("BEGIN")
            row = self._connection.execute(
                "SELECT planning_run_id FROM task_planning_provenance WHERE task_run_id = ?",
                (task_run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(task_run_id)
            planning_run_id = str(row["planning_run_id"])
            if self._read_planning(planning_run_id).bound_task_run_id != task_run_id:
                raise ValueError("task and planning binding differ")
            return planning_run_id

    def _read_rework_round(self, round_id: str) -> dict[str, Any]:
        state = self._read_round(round_id)
        row = self._connection.execute(
            "SELECT snapshot_id, parent_round_id, metadata FROM round_inputs WHERE round_id = ?",
            (round_id,),
        ).fetchone()
        if row is None:
            raise KeyError(round_id)
        history = tuple(
            (decode_envelope(item["envelope"]), Disposition(item["disposition"]))
            for item in self._connection.execute(
                "SELECT envelope, disposition FROM history WHERE round_id = ? ORDER BY sequence",
                (round_id,),
            ).fetchall()
        )
        return {
            "round_id": round_id,
            "state": task_execution_value(state),
            "input_snapshot_id": row["snapshot_id"],
            "parent_round_id": row["parent_round_id"],
            "metadata": thaw_json(decode_result(row["metadata"])),
            "execution": thaw_json(self.load_execution(round_id)),
            "history": task_execution_value(history),
        }

    def _read_task_rework_checkpoint(self, review_round_id: str) -> dict[str, Any]:
        review = self._read_rework_round(review_round_id)
        confirmation_id = self.confirmation_for(review_round_id)
        return {
            "version": 1,
            "review": review,
            "confirmation": self._read_rework_round(confirmation_id)
            if confirmation_id is not None
            else None,
        }

    def load_task_rework_checkpoint(self, review_round_id: str) -> JSONValue:
        """Capture every review and confirmation fact on one database snapshot."""
        with self._connection:
            self._connection.execute("BEGIN")
            return freeze_json(self._read_task_rework_checkpoint(review_round_id))

    def _rework_review_binding(
        self, source: TaskReworkSource, original: TaskInput
    ) -> dict[str, Any]:
        checkpoint = self._read_task_rework_checkpoint(source.review_round_id)
        if task_execution_hash(checkpoint) != source.review_checkpoint_hash:
            raise ValueError("review or confirmation changed before task rework")
        review, confirmation = checkpoint["review"], checkpoint["confirmation"]
        if (
            review["state"]["snapshot_id"] != source.review_snapshot_id
            or review["input_snapshot_id"] != source.review_snapshot_id
            or review["parent_round_id"] is not None
            or review["metadata"]["kind"] != "review"
            or confirmation is not None
            and (
                confirmation["parent_round_id"] != source.review_round_id
                or confirmation["state"]["snapshot_id"] != source.review_snapshot_id
                or confirmation["input_snapshot_id"] != source.review_snapshot_id
                or confirmation["metadata"]["kind"] != "confirmation"
            )
        ):
            raise ValueError("task rework review associations differ")
        payload = cast(
            dict[str, Any], thaw_json(self.load_snapshot(source.review_snapshot_id))
        )
        if (
            task_execution_hash(payload) != source.review_snapshot_id
            or payload["version"] != 2
            or payload["kind"] != "executed_task_review"
        ):
            raise ValueError("task rework requires its immutable executed review input")
        suite = cast(dict[str, Any], payload["executed_suite"])
        declared = suite["prepared"]["checkout"]["input"]
        if (
            not _same(suite["task_input"], original)
            or declared["task_run_id"] != source.task_run_id
            or declared["implementation_attempt"] != source.implementation_attempt
            or declared["task_input_hash"] != source.task_input_hash
            or declared["candidate"]["delivery"]["candidate"] != source.candidate_commit
            or task_execution_hash(declared["candidate"]) != source.candidate_hash
            or suite["test_attempt"] != source.test_attempt
            or payload["source"]["candidate"] != source.candidate_commit
            or payload["source"]["baseline"] != original.card.baseline_commit
            or payload["source"]["repository"] != original.frozen_rules.repository
        ):
            raise ValueError("task rework review belongs to another task or candidate")
        return suite

    def _rework_test_binding(
        self,
        source: TaskReworkSource,
        suite: dict[str, Any],
        implementation: TaskState,
    ) -> None:
        state = self._read_test(
            self._required_stage(source.task_run_id, source.implementation_attempt)
        )
        history = self._read_test_history(
            source.task_run_id, source.implementation_attempt
        )
        self._reservation_cas(
            state,
            history,
            source.test_attempt,
            source.test_history_length,
            source.test_history_hash,
        )
        selected = state.current_attempt
        if (
            selected is None
            or not _same(selected.input, suite["prepared"]["checkout"]["input"])
            or not _same(prepared_for_test(selected), suite["prepared"])
            or not selected.finished
            or selected.cancel_requested
            or selected.problems
        ):
            raise ValueError(
                "task rework test source differs from its reviewed attempt"
            )
        prefix_length = suite["test_history_length"]
        if (
            not 0 <= prefix_length <= len(history)
            or task_execution_hash(history[:prefix_length])
            != suite["test_history_hash"]
            or any(
                disposition != Disposition.HISTORY_ONLY
                for _, disposition in history[prefix_length:]
            )
        ):
            raise ValueError(
                "task rework test history differs from its reviewed prefix"
            )
        self._reservation_source(
            source.task_run_id,
            source.implementation_attempt,
            selected.input,
            implementation,
        )

    def reserve_task_rework_execution(
        self, run_id: str, started: TaskEnvelope, prepared: TaskEnvelope
    ) -> None:
        """Consume the sole rework and reserve its owned pair atomically."""
        if not isinstance(started.event, TaskReworkStarted) or not isinstance(
            prepared.event, TaskExecutionPrepared
        ):
            raise TypeError("rework reservation requires ReworkStarted and Prepared")
        source = started.event.source
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            implementation = self._read_task(run_id)
            original = decode_task_input(self._task_row(run_id)["input_payload"])
            history = self._read_task_history(run_id)
            current = implementation.current_attempt
            candidate = implementation.current_candidate
            if (
                source.task_run_id != run_id
                or current is None
                or current.number != source.implementation_attempt
                or not implementation.candidate_ready
                or candidate is None
                or source.candidate_commit != candidate.delivery.candidate
                or source.candidate_hash != task_execution_hash(candidate)
                or source.task_input_hash != task_execution_hash(original)
                or source.task_history_length != len(history)
                or source.task_history_hash != task_execution_hash(history)
            ):
                raise ValueError("task source changed before rework reservation")
            suite = self._rework_review_binding(source, original)
            self._rework_test_binding(source, suite, implementation)
            if prepared.event.workspace.repository != original.frozen_rules.repository:
                raise ValueError(
                    "rework workspace repository differs from frozen rules"
                )
            policy = prepared.event.dispatch_policy
            decision = resolve_dispatch(
                original.card,
                original.frozen_rules,
                task_type=policy.task_type,
                requested=policy.requested,
                fallbacks=policy.fallbacks,
                unavailable=frozenset(current.request.decision.unavailable_models),
            )
            if not _same(decision, started.event.decision):
                raise ValueError("rework dispatch differs from its frozen task inputs")
            reserved = reserve_task_execution(implementation, started, prepared)
            self._write_task(run_id, reserved)
            self._append_task_history(run_id, started, Disposition.APPLIED)
            self._append_task_history(run_id, prepared, Disposition.APPLIED)

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
