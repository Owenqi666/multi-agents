"""Atomic reservations and observations for tests of one implementation attempt."""

import sqlite3
from typing import cast

from multi_agent.core import Disposition, transitions
from multi_agent.core.task_execution import task_execution_hash
from multi_agent.core.task_state import TaskState
from multi_agent.core.task_test_events import (
    TaskTestAttemptStarted,
    TaskTestCommandExited,
    TaskTestCommandStarted,
    TaskTestEnvelope,
    TaskTestProblem,
    TaskTestReconciled,
)
from multi_agent.core.task_test_input import TaskTestInput
from multi_agent.core.task_test_state import TaskTestState

from .planning_store import PlanningStore
from .task_codec import decode_task_input
from .task_test_codec import (
    decode_task_test_attempt,
    decode_task_test_envelope,
    decode_task_test_input,
    encode_task_test_attempt,
    encode_task_test_envelope,
    encode_task_test_input,
)

type TaskTestHistory = tuple[tuple[TaskTestEnvelope, Disposition], ...]


def _number(value: int, *, zero: bool = False) -> None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < (0 if zero else 1)
    ):
        raise ValueError("test reservation coordinates must be integers in range")


class TaskTestStore(PlanningStore):
    """One caller-owned connection; no Git access, execution, or automatic retry."""

    def reserve_task_test_attempt(
        self,
        task_run_id: str,
        envelope: TaskTestEnvelope,
        *,
        expected_test_attempt: int | None,
        expected_history_length: int,
        expected_history_hash: str,
    ) -> None:
        if not isinstance(envelope.event, TaskTestAttemptStarted):
            raise TypeError("test attempt reservation requires AttemptStarted")
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            implementation = self._implementation(
                task_run_id, envelope.implementation_attempt
            )
            _number(envelope.test_attempt)
            value = envelope.event.input
            self._reservation_source(
                task_run_id, envelope.implementation_attempt, value, implementation
            )
            row = self._stage_row(task_run_id, envelope.implementation_attempt)
            if row is None:
                state = TaskTestState(
                    task_run_id, envelope.implementation_attempt, value
                )
                history: TaskTestHistory = ()
            else:
                state = self._read_test(row)
                history = self._read_test_history(
                    task_run_id, envelope.implementation_attempt
                )
            self._reservation_cas(
                state,
                history,
                expected_test_attempt,
                expected_history_length,
                expected_history_hash,
            )
            updated, disposition = transitions.apply_task_test(
                state, envelope, implementation
            )
            if disposition != Disposition.APPLIED:
                raise ValueError("test attempt reservation must apply")
            if row is None:
                self._connection.execute(
                    """INSERT INTO task_test_stages (
                           task_run_id, implementation_attempt, reference_input_payload,
                           current_test_attempt
                       ) VALUES (?, ?, ?, ?)""",
                    (
                        task_run_id,
                        envelope.implementation_attempt,
                        encode_task_test_input(value),
                        envelope.test_attempt,
                    ),
                )
            self._write_test(updated)
            self._append_test_history(task_run_id, envelope, disposition)

    def reserve_task_test_command(
        self,
        task_run_id: str,
        envelope: TaskTestEnvelope,
        *,
        expected_test_attempt: int,
        expected_command_sequence: int | None,
        expected_history_length: int,
        expected_history_hash: str,
    ) -> None:
        if not isinstance(envelope.event, TaskTestCommandStarted):
            raise TypeError("test command reservation requires CommandStarted")
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            implementation = self._implementation(
                task_run_id, envelope.implementation_attempt
            )
            _number(envelope.test_attempt)
            _number(envelope.event.sequence)
            state = self._read_test(
                self._required_stage(task_run_id, envelope.implementation_attempt)
            )
            current = state.current_attempt
            assert current is not None
            self._reservation_source(
                task_run_id,
                envelope.implementation_attempt,
                current.input,
                implementation,
            )
            history = self._read_test_history(
                task_run_id, envelope.implementation_attempt
            )
            self._reservation_cas(
                state,
                history,
                expected_test_attempt,
                expected_history_length,
                expected_history_hash,
            )
            if expected_command_sequence is not None:
                _number(expected_command_sequence)
            command = current.current_command
            if expected_command_sequence != (
                command.sequence if command is not None else None
            ):
                raise ValueError("current test command changed before reservation")
            updated, disposition = transitions.apply_task_test(
                state, envelope, implementation
            )
            if disposition != Disposition.APPLIED:
                raise ValueError("test command reservation must apply")
            self._write_test(updated)
            self._append_test_history(task_run_id, envelope, disposition)

    def apply_task_test(
        self, task_run_id: str, envelope: TaskTestEnvelope
    ) -> Disposition:
        if envelope.event.kind in (
            TaskTestAttemptStarted.kind,
            TaskTestCommandStarted.kind,
        ):
            raise ValueError("test controller requests require atomic reservation")
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            implementation = self._implementation(
                task_run_id, envelope.implementation_attempt
            )
            _number(envelope.test_attempt)
            if (
                isinstance(
                    envelope.event,
                    (TaskTestCommandExited, TaskTestProblem, TaskTestReconciled),
                )
                and envelope.event.sequence is not None
            ):
                _number(envelope.event.sequence)
            state = self._read_test(
                self._required_stage(task_run_id, envelope.implementation_attempt)
            )
            updated, disposition = transitions.apply_task_test(
                state, envelope, implementation
            )
            if updated != state:
                self._write_test(updated)
            self._append_test_history(task_run_id, envelope, disposition)
        return disposition

    def load_task_test(
        self, task_run_id: str, implementation_attempt: int
    ) -> TaskTestState:
        with self._connection:
            self._connection.execute("BEGIN")
            self._implementation(task_run_id, implementation_attempt)
            return self._read_test(
                self._required_stage(task_run_id, implementation_attempt)
            )

    def load_task_test_history(
        self, task_run_id: str, implementation_attempt: int
    ) -> TaskTestHistory:
        with self._connection:
            self._connection.execute("BEGIN")
            self._implementation(task_run_id, implementation_attempt)
            self._required_stage(task_run_id, implementation_attempt)
            return self._read_test_history(task_run_id, implementation_attempt)

    def _implementation(self, task_run_id: str, number: int) -> TaskState:
        implementation = self._read_task(task_run_id)
        _number(number)
        if not any(item.number == number for item in implementation.attempts):
            raise ValueError("test implementation attempt has not started")
        return implementation

    def _reservation_source(
        self,
        task_run_id: str,
        number: int,
        value: TaskTestInput,
        implementation: TaskState,
    ) -> None:
        current = implementation.current_attempt
        _number(value.implementation_attempt)
        original = decode_task_input(self._task_row(task_run_id)["input_payload"])
        history = self._read_task_history(task_run_id)
        if (
            current is None
            or current.number != number
            or value.task_run_id != task_run_id
            or value.implementation_attempt != number
            or not implementation.candidate_ready
            or task_execution_hash(value.candidate)
            != task_execution_hash(implementation.current_candidate)
            or value.task_id != original.card.id
            or value.task_hash != original.card.task_hash
            or value.rules_hash != original.frozen_rules.rules_hash
            or value.task_input_hash != task_execution_hash(original)
        ):
            raise ValueError(
                "test reservation source differs from the current implementation"
            )
        _number(value.task_history_length, zero=True)
        if value.task_history_length != len(
            history
        ) or value.task_history_hash != task_execution_hash(history):
            raise ValueError("implementation history changed before test reservation")
        registry = {
            command.name: command for command in original.frozen_rules.rules.commands
        }
        if not value.commands or len(
            {command.name for command in value.commands}
        ) != len(value.commands):
            raise ValueError(
                "test reservation requires distinct declared command names"
            )
        for command in value.commands:
            if command.name not in registry or task_execution_hash(
                command
            ) != task_execution_hash(registry[command.name]):
                raise ValueError(
                    "test command differs from the original approved registry"
                )

    @staticmethod
    def _reservation_cas(
        state: TaskTestState,
        history: TaskTestHistory,
        expected_test_attempt: int | None,
        expected_history_length: int,
        expected_history_hash: str,
    ) -> None:
        if expected_test_attempt is not None:
            _number(expected_test_attempt)
        _number(expected_history_length, zero=True)
        current = state.current_attempt
        if (
            expected_test_attempt != (current.number if current is not None else None)
            or expected_history_length != len(history)
            or expected_history_hash != task_execution_hash(history)
        ):
            raise ValueError("test stage changed before reservation")

    def _stage_row(
        self, task_run_id: str, implementation_attempt: int
    ) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            self._connection.execute(
                "SELECT * FROM task_test_stages WHERE task_run_id = ? AND implementation_attempt = ?",
                (task_run_id, implementation_attempt),
            ).fetchone(),
        )

    def _required_stage(
        self, task_run_id: str, implementation_attempt: int
    ) -> sqlite3.Row:
        row = self._stage_row(task_run_id, implementation_attempt)
        if row is None:
            raise KeyError((task_run_id, implementation_attempt))
        return row

    def _read_test(self, row: sqlite3.Row) -> TaskTestState:
        attempts = tuple(
            decode_task_test_attempt(item["payload"])
            for item in self._connection.execute(
                """SELECT payload FROM task_test_attempts
                   WHERE task_run_id = ? AND implementation_attempt = ? ORDER BY number""",
                (row["task_run_id"], row["implementation_attempt"]),
            ).fetchall()
        )
        if not attempts or attempts[-1].number != row["current_test_attempt"]:
            raise ValueError("persisted test stage current attempt is inconsistent")
        return TaskTestState(
            row["task_run_id"],
            row["implementation_attempt"],
            decode_task_test_input(row["reference_input_payload"]),
            transitions.task_test_phase(attempts[-1]),
            attempts,
        )

    def _read_test_history(
        self, task_run_id: str, implementation_attempt: int
    ) -> TaskTestHistory:
        rows = self._connection.execute(
            """SELECT envelope, disposition FROM task_test_history
               WHERE task_run_id = ? AND implementation_attempt = ? ORDER BY sequence""",
            (task_run_id, implementation_attempt),
        ).fetchall()
        return tuple(
            (
                decode_task_test_envelope(row["envelope"]),
                Disposition(row["disposition"]),
            )
            for row in rows
        )

    def _write_test(self, state: TaskTestState) -> None:
        current = state.current_attempt
        assert current is not None
        for attempt in state.attempts:
            self._connection.execute(
                """INSERT INTO task_test_attempts (
                       task_run_id, implementation_attempt, number, payload
                   ) VALUES (?, ?, ?, ?)
                   ON CONFLICT (task_run_id, implementation_attempt, number)
                   DO UPDATE SET payload = excluded.payload""",
                (
                    state.task_run_id,
                    state.implementation_attempt,
                    attempt.number,
                    encode_task_test_attempt(attempt),
                ),
            )
        self._connection.execute(
            """UPDATE task_test_stages SET current_test_attempt = ?
               WHERE task_run_id = ? AND implementation_attempt = ?""",
            (current.number, state.task_run_id, state.implementation_attempt),
        )

    def _append_test_history(
        self, task_run_id: str, envelope: TaskTestEnvelope, disposition: Disposition
    ) -> None:
        self._connection.execute(
            """INSERT INTO task_test_history (
                   task_run_id, implementation_attempt, sequence, envelope, disposition
               ) SELECT ?, ?, COALESCE(MAX(sequence), 0) + 1, ?, ?
                 FROM task_test_history WHERE task_run_id = ? AND implementation_attempt = ?""",
            (
                task_run_id,
                envelope.implementation_attempt,
                encode_task_test_envelope(envelope),
                disposition,
                task_run_id,
                envelope.implementation_attempt,
            ),
        )
