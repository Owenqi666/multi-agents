"""Atomic task histories sharing the existing review database and migration path."""

import json
import sqlite3
from dataclasses import asdict
from typing import cast

from multi_agent.core import Disposition, transitions
from multi_agent.core.task_events import TaskEnvelope, TaskScopeBlocked
from multi_agent.core.task_state import TaskInput, TaskPhase, TaskState
from multi_agent.core.tasks import FrozenProjectRules, TaskCard

from .database import ReviewStore
from .task_codec import (
    decode_task_attempt,
    decode_task_envelope,
    decode_task_input,
    encode_task_attempt,
    encode_task_envelope,
    encode_task_input,
)


class TaskStore(ReviewStore):
    """Persistence of validated task inputs; no subprocesses or automatic retry."""

    def create_task(
        self,
        run_id: str,
        card: TaskCard,
        rules: FrozenProjectRules,
        requirements: str,
        created_at: str,
    ) -> None:
        if card.baseline_commit != rules.baseline_commit:
            raise ValueError("task and frozen rules must have the same baseline")
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            self._insert_task(run_id, TaskInput(card, rules, requirements, created_at))

    def _insert_task(self, run_id: str, value: TaskInput) -> None:
        """Insert trusted inputs inside the caller's existing write transaction."""
        self._connection.execute(
            """INSERT INTO task_runs (
                   run_id, task_id, task_hash, rules_hash, baseline_commit,
                   input_payload, phase, current_attempt, scope_block
               ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)""",
            (
                run_id,
                value.card.id,
                value.card.task_hash,
                value.frozen_rules.rules_hash,
                value.card.baseline_commit,
                encode_task_input(value),
                TaskPhase.READY,
            ),
        )

    def apply_task(self, run_id: str, envelope: TaskEnvelope) -> Disposition:
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            state = self._read_task(run_id)
            updated, disposition = transitions.apply_task(state, envelope)
            if updated != state:
                self._write_task(run_id, updated)
            self._connection.execute(
                """INSERT INTO task_history (run_id, sequence, envelope, disposition)
                   SELECT ?, COALESCE(MAX(sequence), 0) + 1, ?, ?
                   FROM task_history WHERE run_id = ?""",
                (run_id, encode_task_envelope(envelope), disposition, run_id),
            )
        return disposition

    def load_task(self, run_id: str) -> TaskState:
        with self._connection:
            self._connection.execute("BEGIN")
            return self._read_task(run_id)

    def load_task_input(self, run_id: str) -> TaskInput:
        with self._connection:
            self._connection.execute("BEGIN")
            return decode_task_input(self._task_row(run_id)["input_payload"])

    def load_task_history(
        self, run_id: str
    ) -> tuple[tuple[TaskEnvelope, Disposition], ...]:
        with self._connection:
            self._connection.execute("BEGIN")
            self._task_row(run_id)
            rows = self._connection.execute(
                "SELECT envelope, disposition FROM task_history WHERE run_id = ? ORDER BY sequence",
                (run_id,),
            ).fetchall()
            return tuple(
                (decode_task_envelope(row["envelope"]), Disposition(row["disposition"]))
                for row in rows
            )

    def list_recoverable_tasks(self) -> tuple[str, ...]:
        """List pending runs, including planning/delivery waits, not retry permission."""
        with self._connection:
            self._connection.execute("BEGIN")
            rows = self._connection.execute(
                "SELECT run_id FROM task_runs ORDER BY rowid"
            ).fetchall()
            return tuple(row["run_id"] for row in rows)

    def _task_row(self, run_id: str) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM task_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return cast(sqlite3.Row, row)

    def _read_task(self, run_id: str) -> TaskState:
        row = self._task_row(run_id)
        attempts = self._connection.execute(
            "SELECT payload FROM task_attempts WHERE run_id = ? ORDER BY number",
            (run_id,),
        ).fetchall()
        blocked = (
            json.loads(row["scope_block"]) if row["scope_block"] is not None else None
        )
        return TaskState(
            task_id=row["task_id"],
            task_hash=row["task_hash"],
            rules_hash=row["rules_hash"],
            baseline_commit=row["baseline_commit"],
            phase=TaskPhase(row["phase"]),
            attempts=tuple(
                decode_task_attempt(attempt["payload"]) for attempt in attempts
            ),
            scope_block=(
                TaskScopeBlocked(blocked["reason"], tuple(blocked["required_paths"]))
                if blocked is not None
                else None
            ),
        )

    def _write_task(self, run_id: str, state: TaskState) -> None:
        for attempt in state.attempts:
            self._connection.execute(
                """INSERT INTO task_attempts (run_id, number, payload) VALUES (?, ?, ?)
                   ON CONFLICT (run_id, number) DO UPDATE SET payload = excluded.payload""",
                (run_id, attempt.number, encode_task_attempt(attempt)),
            )
        current = state.current_attempt
        blocked = (
            json.dumps(asdict(state.scope_block), ensure_ascii=False)
            if state.scope_block is not None
            else None
        )
        self._connection.execute(
            "UPDATE task_runs SET phase = ?, current_attempt = ?, scope_block = ? WHERE run_id = ?",
            (
                state.phase,
                current.number if current is not None else None,
                blocked,
                run_id,
            ),
        )
