"""Transactional planning histories and one-time task provenance bindings."""

import json
import sqlite3
from typing import Any, cast

from multi_agent.core import Disposition, transitions
from multi_agent.core.planning import (
    PlanningInput,
    PlanningPhase,
    PlanningProvenance,
    PlanningState,
    planning_hash,
    planning_json,
)
from multi_agent.core.planning_events import (
    PlanningAttemptStarted,
    PlanningEnvelope,
    PlanningExecutionPrepared,
    PlanningTaskBound,
)
from multi_agent.core.snapshot import FrozenSnapshot
from multi_agent.core.task_state import TaskInput
from multi_agent.core.tasks import TaskCard

from .planning_codec import (
    decode_planning_attempt,
    decode_planning_envelope,
    decode_planning_input,
    decode_planning_provenance,
    decode_planning_review,
    encode_planning_attempt,
    encode_planning_envelope,
    encode_planning_input,
    encode_planning_provenance,
    encode_planning_review,
)
from .task_store import TaskStore


class PlanningStore(TaskStore):
    """One caller-owned connection; all state updates share their history transaction."""

    def create_planning(
        self, run_id: str, planning_input: PlanningInput, created_at: str
    ) -> None:
        payload = encode_planning_input(planning_input)
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            self._connection.execute(
                """INSERT INTO planning_runs (
                       run_id, input_hash, input_payload, created_at, phase
                   ) VALUES (?, ?, ?, ?, ?)""",
                (
                    run_id,
                    planning_input.input_hash,
                    payload,
                    created_at,
                    PlanningPhase.PENDING,
                ),
            )

    def apply_planning(self, run_id: str, envelope: PlanningEnvelope) -> Disposition:
        if isinstance(envelope.event, PlanningTaskBound):
            raise TypeError("task binding requires create_planned_task")
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            state = self._read_planning(run_id)
            updated, disposition = transitions.apply_planning(state, envelope)
            if updated != state:
                self._write_planning(run_id, updated)
            self._append_planning_history(run_id, envelope, disposition)
        return disposition

    def reserve_planning_attempt(
        self,
        run_id: str,
        started: PlanningEnvelope,
        prepared: PlanningEnvelope,
        *,
        expected_history_length: int | None = None,
        expected_history_hash: str | None = None,
    ) -> None:
        """Persist a current launch and its frozen inputs in one reservation."""
        if not isinstance(started.event, PlanningAttemptStarted) or not isinstance(
            prepared.event, PlanningExecutionPrepared
        ):
            raise TypeError("reservation requires Started and Prepared envelopes")
        if (started.stage, started.attempt) != (prepared.stage, prepared.attempt):
            raise ValueError("reservation envelopes must identify the same attempt")
        if (expected_history_length is None) != (expected_history_hash is None):
            raise ValueError("reservation requires both history prefix values")
        if (
            started.event.prompt_template_version
            in (
                "planning-draft-v2",
                "planning-review-v2",
            )
            and expected_history_length is None
        ):
            raise ValueError("v2 reservation requires a history prefix")
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            state = self._read_planning(run_id)
            if expected_history_length is not None:
                history = self._read_planning_history(run_id)
                if (
                    len(history) != expected_history_length
                    or planning_hash(history) != expected_history_hash
                ):
                    raise ValueError("planning history changed before reservation")
            source_id = prepared.event.source_snapshot_id
            snapshot = FrozenSnapshot.from_dict(
                cast(dict[str, Any], self.load_snapshot(source_id))
            )
            rules = state.planning_input.frozen_rules
            if (
                snapshot.snapshot_id != source_id
                or snapshot.repository != rules.repository
                or snapshot.baseline != rules.baseline_commit
                or snapshot.candidate != rules.baseline_commit
                or snapshot.requirements != state.planning_input.requirements
                or snapshot.tests
                or snapshot.required_tests
                or snapshot.diff
            ):
                raise ValueError(
                    "planning source snapshot does not match frozen inputs"
                )
            updated, start_disposition = transitions.apply_planning(state, started)
            if start_disposition != Disposition.APPLIED:
                raise ValueError("reservation must start a current planning attempt")
            reserved, prepare_disposition = transitions.apply_planning(
                updated, prepared
            )
            current = reserved.current(started.stage)
            if (
                prepare_disposition != Disposition.APPLIED
                or reserved.stage != started.stage
                or current is None
                or current.number != started.attempt
            ):
                raise ValueError(
                    "reservation must prepare the current planning attempt"
                )
            self._write_planning(run_id, reserved)
            self._append_planning_history(run_id, started, start_disposition)
            self._append_planning_history(run_id, prepared, prepare_disposition)

    def load_planning(self, run_id: str) -> PlanningState:
        with self._connection:
            self._connection.execute("BEGIN")
            return self._read_planning(run_id)

    def load_planning_input(self, run_id: str) -> PlanningInput:
        with self._connection:
            self._connection.execute("BEGIN")
            return decode_planning_input(self._planning_row(run_id)["input_payload"])

    def load_planning_history(
        self, run_id: str
    ) -> tuple[tuple[PlanningEnvelope, Disposition], ...]:
        with self._connection:
            self._connection.execute("BEGIN")
            self._planning_row(run_id)
            return self._read_planning_history(run_id)

    def _read_planning_history(
        self, run_id: str
    ) -> tuple[tuple[PlanningEnvelope, Disposition], ...]:
        rows = self._connection.execute(
            "SELECT envelope, disposition FROM planning_history WHERE run_id = ? ORDER BY sequence",
            (run_id,),
        ).fetchall()
        return tuple(
            (
                decode_planning_envelope(row["envelope"]),
                Disposition(row["disposition"]),
            )
            for row in rows
        )

    def list_recoverable_planning(self) -> tuple[str, ...]:
        """List unbound runs, including failed/cancelled work; never grant a retry."""
        with self._connection:
            self._connection.execute("BEGIN")
            rows = self._connection.execute(
                "SELECT run_id FROM planning_runs WHERE phase != ? ORDER BY rowid",
                (PlanningPhase.BOUND,),
            ).fetchall()
            return tuple(row["run_id"] for row in rows)

    def create_planned_task(
        self,
        task_run_id: str,
        planning_run_id: str,
        created_at: str,
        *,
        expected_plan_hash: str | None = None,
    ) -> None:
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            state = self._read_planning(planning_run_id)
            provenance = state.provenance()
            if (
                expected_plan_hash is not None
                and provenance.plan_hash != expected_plan_hash
            ):
                raise ValueError("planning provenance changed before task binding")
            envelope = PlanningEnvelope(
                "review",
                provenance.review.number,
                PlanningTaskBound(task_run_id),
                created_at,
            )
            updated, disposition = transitions.apply_planning(state, envelope)
            if (
                disposition != Disposition.APPLIED
                or updated.bound_task_run_id != task_run_id
            ):
                raise ValueError(
                    "planning evidence is not available for a new task binding"
                )
            self._insert_task(
                task_run_id,
                TaskInput(
                    provenance.card,
                    provenance.planning_input.frozen_rules,
                    provenance.planning_input.requirements,
                    created_at,
                ),
            )
            self._connection.execute(
                """INSERT INTO task_planning_provenance (
                       task_run_id, planning_run_id, plan_hash, payload
                   ) VALUES (?, ?, ?, ?)""",
                (
                    task_run_id,
                    planning_run_id,
                    provenance.plan_hash,
                    encode_planning_provenance(provenance),
                ),
            )
            self._write_planning(planning_run_id, updated)
            self._append_planning_history(planning_run_id, envelope, disposition)

    def load_task_provenance(self, task_run_id: str) -> PlanningProvenance:
        with self._connection:
            self._connection.execute("BEGIN")
            row = self._connection.execute(
                "SELECT payload FROM task_planning_provenance WHERE task_run_id = ?",
                (task_run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(task_run_id)
            return decode_planning_provenance(row["payload"])

    def _planning_row(self, run_id: str) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM planning_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return cast(sqlite3.Row, row)

    def _read_planning(self, run_id: str) -> PlanningState:
        row = self._planning_row(run_id)
        attempts = self._connection.execute(
            "SELECT stage, payload FROM planning_attempts WHERE run_id = ? ORDER BY number",
            (run_id,),
        ).fetchall()
        return PlanningState(
            planning_input=decode_planning_input(row["input_payload"]),
            phase=PlanningPhase(row["phase"]),
            stage=row["stage"],
            draft_attempts=tuple(
                decode_planning_attempt(item["payload"])
                for item in attempts
                if item["stage"] == "draft"
            ),
            review_attempts=tuple(
                decode_planning_attempt(item["payload"])
                for item in attempts
                if item["stage"] == "review"
            ),
            card=TaskCard.from_dict(json.loads(row["card_payload"]))
            if row["card_payload"] is not None
            else None,
            review_artifact=decode_planning_review(row["review_artifact_payload"])
            if row["review_artifact_payload"] is not None
            else None,
            validation_error=row["validation_error"],
            bound_task_run_id=row["bound_task_run_id"],
        )

    def _write_planning(self, run_id: str, state: PlanningState) -> None:
        for stage in ("draft", "review"):
            for attempt in state.attempts(stage):
                self._connection.execute(
                    """INSERT INTO planning_attempts (run_id, stage, number, payload)
                       VALUES (?, ?, ?, ?) ON CONFLICT (run_id, stage, number)
                       DO UPDATE SET payload = excluded.payload""",
                    (run_id, stage, attempt.number, encode_planning_attempt(attempt)),
                )
        self._connection.execute(
            """UPDATE planning_runs SET phase = ?, stage = ?, card_payload = ?,
                   review_artifact_payload = ?, validation_error = ?, bound_task_run_id = ?
               WHERE run_id = ?""",
            (
                state.phase,
                state.stage,
                planning_json(state.card) if state.card is not None else None,
                encode_planning_review(state.review_artifact)
                if state.review_artifact is not None
                else None,
                state.validation_error,
                state.bound_task_run_id,
                run_id,
            ),
        )

    def _append_planning_history(
        self, run_id: str, envelope: PlanningEnvelope, disposition: Disposition
    ) -> None:
        self._connection.execute(
            """INSERT INTO planning_history (run_id, sequence, envelope, disposition)
               SELECT ?, COALESCE(MAX(sequence), 0) + 1, ?, ?
               FROM planning_history WHERE run_id = ?""",
            (run_id, encode_planning_envelope(envelope), disposition, run_id),
        )
