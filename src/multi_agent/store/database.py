"""Transactional SQLite storage for review rounds and their ordered histories."""

import json
import sqlite3
from importlib.resources import files
from pathlib import Path
from types import TracebackType
from typing import Self, cast

from multi_agent import core
from multi_agent.core import (
    AttemptState,
    AttemptStatus,
    Disposition,
    RoundState,
    SeatConfig,
    SeatState,
)
from multi_agent.core.events import Envelope, FailureCategory, JSONValue, thaw_json

from .codec import (
    decode_envelope,
    decode_result,
    decode_source,
    encode_envelope,
    encode_result,
    encode_source,
)


class ReviewStore:
    """One connection, owned by its caller. Separate callers use separate connections."""

    def __init__(self, path: str | Path, *, timeout: float = 5.0) -> None:
        self._connection = sqlite3.connect(path, timeout=timeout, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._migrate()
        except BaseException:
            self._connection.close()
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def _migrate(self) -> None:
        migrations = sorted(
            files("multi_agent.store").joinpath("migrations").iterdir(),
            key=lambda path: path.name,
        )
        scripts = [
            (int(path.name.split("_", 1)[0]), path)
            for path in migrations
            if path.name.endswith(".sql")
        ]
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            version = self._connection.execute("PRAGMA user_version").fetchone()[0]
            if version > scripts[-1][0]:
                raise ValueError(f"unsupported database version {version}")
            for number, path in scripts:
                if number <= version:
                    continue
                statement = ""
                for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        self._connection.execute(statement)
                        statement = ""
                if statement.strip():
                    raise ValueError(f"incomplete SQL in migration {path.name}")
                self._connection.execute(f"PRAGMA user_version = {number}")

    def create_round(self, round_id: str, state: RoundState) -> None:
        """Create a round with a caller-supplied identity and immutable state."""
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            self._insert_round(round_id, state)

    def _insert_round(self, round_id: str, state: RoundState) -> None:
        self._connection.execute(
            "INSERT INTO rounds VALUES (?, ?, ?)",
            (round_id, state.snapshot_id, state.prompt_template_version),
        )
        for position, seat in enumerate(state.seats):
            config = seat.config
            self._connection.execute(
                "INSERT INTO seats VALUES (?, ?, ?, ?, ?, ?)",
                (
                    round_id,
                    config.key,
                    position,
                    config.provider,
                    config.model,
                    config.effort,
                ),
            )
        self._write_attempts(round_id, state)

    def apply(self, round_id: str, envelope: Envelope) -> Disposition:
        with self._connection:
            # Acquire the writer lock before reading the current attempt.
            self._connection.execute("BEGIN IMMEDIATE")
            state = self._read_round(round_id)
            updated, disposition = core.apply(state, envelope)
            if updated != state:
                self._write_attempts(round_id, updated)
            self._connection.execute(
                """INSERT INTO history (round_id, sequence, envelope, disposition)
                   SELECT ?, COALESCE(MAX(sequence), 0) + 1, ?, ?
                   FROM history WHERE round_id = ?""",
                (round_id, encode_envelope(envelope), disposition, round_id),
            )
        return disposition

    def load_round(self, round_id: str) -> RoundState:
        # A read transaction keeps the several SELECTs on one database snapshot.
        with self._connection:
            self._connection.execute("BEGIN")
            return self._read_round(round_id)

    def load_history(self, round_id: str) -> tuple[tuple[Envelope, Disposition], ...]:
        with self._connection:
            self._connection.execute("BEGIN")
            self._round_row(round_id)
            rows = self._connection.execute(
                "SELECT envelope, disposition FROM history WHERE round_id = ? ORDER BY sequence",
                (round_id,),
            ).fetchall()
            return tuple(
                (decode_envelope(row["envelope"]), Disposition(row["disposition"]))
                for row in rows
            )

    def save_snapshot(self, snapshot_id: str, payload: JSONValue) -> None:
        """Insert once; repeated identical input is idempotent."""
        encoded = _canonical(payload)
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT payload FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()
            if row is None:
                self._connection.execute(
                    "INSERT INTO snapshots VALUES (?, ?)",
                    (snapshot_id, encoded),
                )
            elif row["payload"] != encoded:
                raise ValueError("snapshot identity already has different content")

    def load_snapshot(self, snapshot_id: str) -> JSONValue:
        row = self._connection.execute(
            "SELECT payload FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchone()
        if row is None:
            raise KeyError(snapshot_id)
        return decode_result(row["payload"])

    def create_review(
        self,
        round_id: str,
        state: RoundState,
        metadata: JSONValue,
        parent_round_id: str | None = None,
    ) -> None:
        """Persist the round and its frozen-input association in one transaction."""
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            self._insert_round(round_id, state)
            self._connection.execute(
                "INSERT INTO round_inputs VALUES (?, ?, ?, ?)",
                (round_id, state.snapshot_id, encode_result(metadata), parent_round_id),
            )

    def load_review(self, round_id: str) -> JSONValue:
        row = self._connection.execute(
            "SELECT metadata FROM round_inputs WHERE round_id = ?", (round_id,)
        ).fetchone()
        if row is None:
            raise KeyError(round_id)
        return decode_result(row["metadata"])

    def confirmation_for(self, parent_round_id: str) -> str | None:
        row = self._connection.execute(
            "SELECT round_id FROM round_inputs WHERE parent_round_id = ?",
            (parent_round_id,),
        ).fetchone()
        return str(row["round_id"]) if row is not None else None

    def list_reviews(self) -> tuple[str, ...]:
        return tuple(
            row["round_id"]
            for row in self._connection.execute(
                "SELECT round_id FROM round_inputs ORDER BY rowid"
            )
        )

    def set_execution(
        self, round_id: str, seat: str, attempt: int, metadata: JSONValue
    ) -> None:
        encoded = _canonical(metadata)
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            key = (round_id, seat, attempt)
            row = self._connection.execute(
                "SELECT metadata FROM executions WHERE round_id=? AND seat_key=? AND attempt=?",
                key,
            ).fetchone()
            if row is None:
                self._connection.execute(
                    "INSERT INTO executions VALUES (?, ?, ?, ?)",
                    (*key, encoded),
                )
            elif row["metadata"] != encoded:
                raise ValueError("execution evidence is immutable")

    def load_execution(self, round_id: str) -> JSONValue:
        result: dict[str, JSONValue] = {}
        rows = self._connection.execute(
            "SELECT * FROM executions WHERE round_id = ? ORDER BY seat_key, attempt",
            (round_id,),
        ).fetchall()
        seats: dict[str, dict[str, JSONValue]] = {}
        for row in rows:
            seats.setdefault(row["seat_key"], {})[str(row["attempt"])] = decode_result(
                row["metadata"]
            )
        result.update(seats)
        return result

    def _round_row(self, round_id: str) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT snapshot_id, prompt_template_version FROM rounds WHERE round_id = ?",
            (round_id,),
        ).fetchone()
        if row is None:
            raise KeyError(round_id)
        return cast(sqlite3.Row, row)

    def _read_round(self, round_id: str) -> RoundState:
        row = self._round_row(round_id)
        seats = []
        for seat in self._connection.execute(
            "SELECT * FROM seats WHERE round_id = ? ORDER BY position", (round_id,)
        ).fetchall():
            attempts = self._connection.execute(
                "SELECT * FROM attempts WHERE round_id = ? AND seat_key = ? ORDER BY number",
                (round_id, seat["seat_key"]),
            ).fetchall()
            config = SeatConfig(
                seat["seat_key"], seat["provider"], seat["model"], seat["effort"]
            )
            seats.append(
                SeatState(config, tuple(_read_attempt(attempt) for attempt in attempts))
            )
        return RoundState(
            row["snapshot_id"], row["prompt_template_version"], tuple(seats)
        )

    def _write_attempts(self, round_id: str, state: RoundState) -> None:
        for seat in state.seats:
            for attempt in seat.attempts:
                self._connection.execute(
                    """INSERT INTO attempts (
                           round_id, seat_key, number, status, failure_category, detail,
                           retry_after, session_id, reported_model, reported_model_source,
                           reported_effort, reported_effort_source, result, exit_code
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT (round_id, seat_key, number) DO UPDATE SET
                           status = excluded.status,
                           failure_category = excluded.failure_category,
                           detail = excluded.detail,
                           retry_after = excluded.retry_after,
                           session_id = excluded.session_id,
                           reported_model = excluded.reported_model,
                           reported_model_source = excluded.reported_model_source,
                           reported_effort = excluded.reported_effort,
                           reported_effort_source = excluded.reported_effort_source,
                           result = excluded.result,
                           exit_code = excluded.exit_code""",
                    (
                        round_id,
                        seat.config.key,
                        attempt.number,
                        attempt.status,
                        attempt.failure_category,
                        attempt.detail,
                        attempt.retry_after,
                        attempt.session_id,
                        attempt.reported_model,
                        encode_source(attempt.reported_model_source),
                        attempt.reported_effort,
                        encode_source(attempt.reported_effort_source),
                        encode_result(attempt.result),
                        attempt.exit_code,
                    ),
                )


def _canonical(value: JSONValue) -> str:
    return json.dumps(thaw_json(value), sort_keys=True, separators=(",", ":"))


def _read_attempt(row: sqlite3.Row) -> AttemptState:
    category = row["failure_category"]
    return AttemptState(
        number=row["number"],
        status=AttemptStatus(row["status"]),
        failure_category=FailureCategory(category) if category is not None else None,
        detail=row["detail"],
        retry_after=row["retry_after"],
        session_id=row["session_id"],
        reported_model=row["reported_model"],
        reported_model_source=decode_source(row["reported_model_source"]),
        reported_effort=row["reported_effort"],
        reported_effort_source=decode_source(row["reported_effort_source"]),
        result=decode_result(row["result"]),
        exit_code=row["exit_code"],
    )
