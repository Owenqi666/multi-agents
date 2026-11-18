"""Persist one terminal integration and reopen its original frozen sources."""

import json
import sqlite3
from typing import Any

from multi_agent.core.integration import (
    IntegrationCandidate,
    IntegrationConflict,
    IntegrationMember,
    IntegrationRecord,
    IntegrationResult,
)
from multi_agent.core.planning import DependencyReceipt
from multi_agent.core.task_execution import task_execution_json
from multi_agent.core.workspaces import WorkspaceDelivery

from .task_codec import decode_task_input
from .task_review_store import TaskReviewStore


def _decode_member(item: dict[str, Any]) -> IntegrationMember:
    delivery = item["delivery"]
    return IntegrationMember(
        item["task_run_id"],
        item["planning_run_id"],
        item["attempt"],
        decode_task_input(task_execution_json(item["task_input"])),
        WorkspaceDelivery(
            delivery["baseline"],
            delivery["candidate"],
            tuple(delivery["changed_paths"]),
            delivery["digest"],
            delivery["ref"],
            delivery["tree"],
        ),
        tuple(
            DependencyReceipt(**receipt) for receipt in item["external_dependencies"]
        ),
    )


def _record(row: sqlite3.Row) -> IntegrationRecord:
    result: IntegrationResult = (
        IntegrationCandidate(
            row["candidate_ref"], row["candidate_commit"], row["candidate_tree"]
        )
        if row["conflict_paths"] is None
        else IntegrationConflict(tuple(json.loads(row["conflict_paths"])))
    )
    return IntegrationRecord(
        row["integration_id"],
        row["repository"],
        row["target_ref"],
        row["target_baseline"],
        tuple(_decode_member(item) for item in json.loads(row["members_payload"])),
        row["applied_count"],
        result,
    )


class IntegrationStore(TaskReviewStore):
    """Integration outcomes are insert-only and preserve prior member versions."""

    def create_integration(self, record: IntegrationRecord) -> None:
        result = record.result
        candidate = result if isinstance(result, IntegrationCandidate) else None
        conflicts = (
            task_execution_json(result.paths)
            if isinstance(result, IntegrationConflict)
            else None
        )
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            for member in record.members:
                self._check_current_member(member)
            self._connection.execute(
                """INSERT INTO integrations (
                       integration_id, repository, target_ref, target_baseline,
                       members_payload, applied_count, candidate_ref,
                       candidate_commit, candidate_tree, conflict_paths
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.integration_id,
                    record.repository,
                    record.target_ref,
                    record.target_baseline,
                    task_execution_json(record.members),
                    record.applied_count,
                    candidate.ref if candidate else None,
                    candidate.commit if candidate else None,
                    candidate.tree if candidate else None,
                    conflicts,
                ),
            )

    def _check_current_member(self, member: IntegrationMember) -> None:
        task = self._read_task(member.task_run_id)
        current = task.current_attempt
        candidate = task.current_candidate
        if (
            current is None
            or current.number != member.attempt
            or not task.candidate_ready
            or candidate is None
            or candidate.delivery != member.delivery
            or decode_task_input(self._task_row(member.task_run_id)["input_payload"])
            != member.task_input
            or self._planning_row(member.planning_run_id)["bound_task_run_id"]
            != member.task_run_id
        ):
            raise ValueError(
                f"integration member {member.task_run_id} differs from the current task source"
            )

    def load_integration(self, integration_id: str) -> IntegrationRecord:
        row = self._connection.execute(
            "SELECT * FROM integrations WHERE integration_id = ?", (integration_id,)
        ).fetchone()
        if row is None:
            raise KeyError(integration_id)
        return _record(row)

    def list_integration_candidates(
        self, repository: str
    ) -> tuple[IntegrationRecord, ...]:
        rows = self._connection.execute(
            """SELECT * FROM integrations
               WHERE repository = ? AND candidate_commit IS NOT NULL ORDER BY rowid""",
            (repository,),
        ).fetchall()
        return tuple(_record(row) for row in rows)
