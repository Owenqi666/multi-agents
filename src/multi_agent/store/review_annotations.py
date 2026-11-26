"""Human and follow-up annotations refer to one completed review seat attempt."""

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Self

from multi_agent.adapters.report_validation import validate_report
from multi_agent.core.state import AttemptStatus, RoundState, SeatConfig


@dataclass(frozen=True, slots=True)
class FindingAnchor:
    round_id: str
    snapshot_id: str
    seat: str
    attempt: int
    finding_id: str

    @classmethod
    def from_dict(cls, value: object) -> Self:
        fields = ("round_id", "snapshot_id", "seat", "finding_id")
        if not isinstance(value, dict) or set(value) != {*fields, "attempt"}:
            raise ValueError(
                "finding anchor requires round_id, snapshot_id, seat, attempt and finding_id"
            )
        if any(
            not isinstance(value[key], str) or not value[key].strip() for key in fields
        ):
            raise ValueError("finding anchor IDs must be nonempty strings")
        if type(value["attempt"]) is not int or not 0 < value["attempt"] < 2**63:
            raise ValueError("invalid finding anchor attempt")
        return cls(**value)

    def find(self, state: RoundState) -> tuple[dict[str, Any], SeatConfig]:
        if self.snapshot_id != state.snapshot_id:
            raise ValueError("finding snapshot differs from the round snapshot")
        for seat in state.seats:
            if seat.config.key != self.seat:
                continue
            if (
                seat.current_attempt != self.attempt
                or seat.attempts[-1].status != AttemptStatus.SUCCEEDED
            ):
                raise ValueError(
                    "finding anchor requires the current completed seat attempt"
                )
            for finding in validate_report(seat.attempts[-1].result)["findings"]:
                if finding["id"] == self.finding_id:
                    return finding, seat.config
        raise ValueError("finding is absent from the selected seat report")


def append_activity(
    connection: sqlite3.Connection,
    anchor: FindingAnchor,
    kind: str,
    payload: Mapping[str, Any],
    *,
    planning_run_id: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Append inside the caller's validated write transaction."""
    when = created_at or datetime.now(UTC).isoformat()
    result = connection.execute(
        """INSERT INTO review_finding_activity
           (round_id, snapshot_id, seat, attempt, finding_id, created_at, kind, payload, planning_run_id)
           VALUES (:round_id, :snapshot_id, :seat, :attempt, :finding_id, :when, :kind, :payload, :plan)""",
        {
            **asdict(anchor),
            "when": when,
            "kind": kind,
            "payload": json.dumps(payload, ensure_ascii=False, allow_nan=False),
            "plan": planning_run_id,
        },
    )
    entry = {"id": result.lastrowid, "kind": kind, "created_at": when, **payload}
    if planning_run_id is not None:
        entry["planning_run_id"] = planning_run_id
    return entry


def finding_activity(
    connection: sqlite3.Connection, round_id: str
) -> dict[FindingAnchor, list[dict[str, Any]]]:
    groups: dict[FindingAnchor, list[dict[str, Any]]] = {}
    if connection.execute("PRAGMA user_version").fetchone()[0] < 14:
        return groups
    for row in connection.execute(
        "SELECT * FROM review_finding_activity WHERE round_id = ? ORDER BY id",
        (round_id,),
    ):
        anchor = FindingAnchor(
            row["round_id"],
            row["snapshot_id"],
            row["seat"],
            row["attempt"],
            row["finding_id"],
        )
        item = {
            "id": row["id"],
            "kind": row["kind"],
            "created_at": row["created_at"],
            **json.loads(row["payload"]),
        }
        if row["planning_run_id"] is not None:
            item["planning_run_id"] = row["planning_run_id"]
        groups.setdefault(anchor, []).append(item)
    return groups
