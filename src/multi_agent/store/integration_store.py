"""Persist one terminal integration and reopen its original frozen sources."""

import json
import sqlite3
from typing import Any

from multi_agent.core import Disposition, transitions
from multi_agent.core.events import thaw_json
from multi_agent.core.integration import (
    IntegrationCandidate,
    IntegrationConflict,
    IntegrationMember,
    IntegrationRecord,
    IntegrationResult,
)
from multi_agent.core.integration_testing import (
    IntegrationTestResult,
    decode_integration_test_result,
)
from multi_agent.core.integration_verification import (
    IntegrationAcceptance,
    IntegrationReviewLink,
    IntegrationTestAttempt,
    IntegrationTestRequest,
)
from multi_agent.core.planning import DependencyReceipt
from multi_agent.core.task_execution import task_execution_hash, task_execution_json
from multi_agent.core.task_test_execution import TaskTestPolicy
from multi_agent.core.tasks import FrozenProjectRules
from multi_agent.core.workspaces import WorkspaceDelivery

from .task_codec import _rules, decode_task_input
from .task_review_store import TaskReviewStore
from .task_test_store import TaskTestStore

_VERIFICATION_SCHEMA = 7


def check_integration_test_launch_owners(store: TaskTestStore, repository: str) -> None:
    if (
        store._connection.execute("PRAGMA user_version").fetchone()[0]
        < _VERIFICATION_SCHEMA
    ):
        return
    running = store._connection.execute(
        """SELECT t.integration_id, t.attempt
           FROM integration_test_attempts t JOIN integrations i USING (integration_id)
           WHERE i.repository = ? AND t.process_stopped IS NOT 1 LIMIT 1""",
        (repository,),
    ).fetchone()
    if running is not None:
        raise ValueError(
            f"integration test {running['integration_id']}/{running['attempt']} "
            "has no confirmed stopping facts"
        )


def _attempt(row: sqlite3.Row) -> IntegrationTestAttempt:
    value = json.loads(row["request_payload"])
    rules = value["rules"]
    policy = value["policy"]
    for key in ("executable_identity", "tool_directories", "runtime_read_paths"):
        policy[key] = tuple(policy[key])
    request = IntegrationTestRequest(
        IntegrationCandidate(**value["candidate"]),
        FrozenProjectRules(
            rules["repository"],
            rules["baseline_commit"],
            rules["blob_oid"],
            rules["raw_text"],
            _rules(rules["rules"]),
        ),
        TaskTestPolicy(**policy),
        value["directory"],
    )
    raw = row["result_payload"]
    return IntegrationTestAttempt(
        row["attempt"],
        request,
        decode_integration_test_result(json.loads(raw)) if raw is not None else None,
    )


def _link(row: sqlite3.Row) -> IntegrationReviewLink:
    device, inode = json.loads(row["directory_identity"])
    return IntegrationReviewLink(
        row["round_id"],
        row["test_attempt"],
        row["snapshot_id"],
        row["directory"],
        (device, inode),
    )


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

    def create_integration(
        self, record: IntegrationRecord, *, rebased_from: str | None = None
    ) -> None:
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
            if rebased_from is not None:
                self._connection.execute(
                    "INSERT INTO integration_rebases VALUES (?, ?)",
                    (record.integration_id, rebased_from),
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

    def has_integration_verification(self) -> bool:
        return (
            int(self._connection.execute("PRAGMA user_version").fetchone()[0])
            >= _VERIFICATION_SCHEMA
        )

    def integration_parent(self, integration_id: str) -> str | None:
        row = self._connection.execute(
            "SELECT source_id FROM integration_rebases WHERE integration_id = ?",
            (integration_id,),
        ).fetchone()
        return str(row["source_id"]) if row is not None else None

    def _current_integration_test(
        self, integration_id: str
    ) -> IntegrationTestAttempt | None:
        row = self._connection.execute(
            """SELECT * FROM integration_test_attempts WHERE integration_id = ?
               ORDER BY attempt DESC LIMIT 1""",
            (integration_id,),
        ).fetchone()
        return _attempt(row) if row is not None else None

    def current_integration_test(
        self, integration_id: str
    ) -> IntegrationTestAttempt | None:
        return self._current_integration_test(integration_id)

    def integration_test_history(
        self, integration_id: str
    ) -> tuple[tuple[IntegrationTestAttempt, tuple[IntegrationTestResult, ...]], ...]:
        rows = self._connection.execute(
            "SELECT * FROM integration_test_attempts WHERE integration_id = ? ORDER BY attempt",
            (integration_id,),
        ).fetchall()
        return tuple(
            (
                _attempt(row),
                tuple(
                    decode_integration_test_result(item)
                    for item in json.loads(row["late_results"])
                ),
            )
            for row in rows
        )

    def reserve_integration_test(
        self, record: IntegrationRecord, request: IntegrationTestRequest
    ) -> IntegrationTestAttempt:
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            self._require_unaccepted(record.integration_id)
            if (
                self.load_integration(record.integration_id) != record
                or request.candidate != record.result
                or request.rules.repository != record.repository
                or request.rules.baseline_commit != record.target_baseline
            ):
                raise ValueError(
                    "integration test does not match the fixed integration"
                )
            current = self._current_integration_test(record.integration_id)
            attempt = transitions.reserve_integration_test(current, request)
            self._connection.execute(
                """INSERT INTO integration_test_attempts (integration_id, attempt, request_payload)
                   VALUES (?, ?, ?)""",
                (record.integration_id, attempt.number, task_execution_json(request)),
            )
            return attempt

    def finish_integration_test(
        self, integration_id: str, attempt: int, result: IntegrationTestResult
    ) -> Disposition:
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            current = self._current_integration_test(integration_id)
            if current is None:
                raise ValueError("integration has no reserved test attempt")
            _, disposition = transitions.finish_integration_test(
                current, attempt, result
            )
            if disposition == Disposition.APPLIED:
                changed = self._connection.execute(
                    """UPDATE integration_test_attempts SET result_payload = ?, process_stopped = ?
                       WHERE integration_id = ? AND attempt = ? AND result_payload IS NULL""",
                    (
                        task_execution_json(result),
                        result.process_stopped,
                        integration_id,
                        attempt,
                    ),
                ).rowcount
            else:
                changed = self._connection.execute(
                    """UPDATE integration_test_attempts
                       SET late_results = json_insert(late_results, '$[#]', json(?))
                       WHERE integration_id = ? AND attempt = ?""",
                    (task_execution_json(result), integration_id, attempt),
                ).rowcount
            if changed != 1:
                raise ValueError("integration test is no longer a matching reservation")
            return disposition

    def link_integration_review(
        self,
        integration_id: str,
        link: IntegrationReviewLink,
        test_evidence_hash: str,
    ) -> None:
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            self._require_unaccepted(integration_id)
            self._require_current_test(
                integration_id, link.test_attempt, test_evidence_hash
            )
            if self._read_round(link.round_id).snapshot_id != link.snapshot_id:
                raise ValueError(
                    "integration review round has a different frozen snapshot"
                )
            self._connection.execute(
                "INSERT INTO integration_review_links VALUES (?, ?, ?, ?, ?)",
                (
                    link.round_id,
                    integration_id,
                    link.test_attempt,
                    link.directory,
                    task_execution_json(link.directory_identity),
                ),
            )

    def integration_reviews(
        self, integration_id: str
    ) -> tuple[IntegrationReviewLink, ...]:
        rows = self._connection.execute(
            """SELECT i.*, r.snapshot_id FROM integration_review_links i
               JOIN round_inputs r USING (round_id)
               WHERE i.integration_id = ? ORDER BY i.rowid""",
            (integration_id,),
        ).fetchall()
        return tuple(_link(row) for row in rows)

    def load_integration_acceptance(
        self, integration_id: str
    ) -> IntegrationAcceptance | None:
        row = self._connection.execute(
            "SELECT payload FROM integration_acceptances WHERE integration_id = ?",
            (integration_id,),
        ).fetchone()
        if row is None:
            return None
        value = json.loads(row["payload"])
        value["candidate"] = IntegrationCandidate(**value["candidate"])
        return IntegrationAcceptance(**value)

    def _require_unaccepted(self, integration_id: str) -> None:
        if self.load_integration_acceptance(integration_id) is not None:
            raise ValueError("integration already has an immutable acceptance receipt")

    def _require_current_test(
        self, integration_id: str, attempt: int, evidence_hash: str
    ) -> None:
        current = self._current_integration_test(integration_id)
        if (
            current is None
            or current.number != attempt
            or current.result is None
            or current.result.status != "succeeded"
            or current.result.process_stopped is not True
            or current.evidence_hash != evidence_hash
        ):
            raise ValueError("integration requires the current passed test evidence")

    def _integration_review_checkpoint(self, round_id: str) -> str:
        checkpoint = self._read_task_rework_checkpoint(round_id)
        snapshot = self.load_snapshot(checkpoint["review"]["input_snapshot_id"])
        return task_execution_hash((checkpoint, thaw_json(snapshot)))

    def integration_review_checkpoint(self, round_id: str) -> str:
        with self._connection:
            self._connection.execute("BEGIN")
            return self._integration_review_checkpoint(round_id)

    def accept_integration(
        self, record: IntegrationRecord, receipt: IntegrationAcceptance
    ) -> None:
        integration_id = record.integration_id
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            self._require_unaccepted(integration_id)
            if (
                self.load_integration(integration_id) != record
                or receipt.integration_id != integration_id
                or receipt.target_ref != record.target_ref
                or receipt.target_baseline != record.target_baseline
                or receipt.candidate != record.result
            ):
                raise ValueError("acceptance does not identify the fixed integration")
            self._require_current_test(
                integration_id, receipt.test_attempt, receipt.test_evidence_hash
            )
            reviews = self.integration_reviews(integration_id)
            if not reviews or (
                reviews[-1].round_id,
                reviews[-1].test_attempt,
                reviews[-1].snapshot_id,
            ) != (
                receipt.review_round_id,
                receipt.test_attempt,
                receipt.snapshot_id,
            ):
                raise ValueError("acceptance requires the current integration review")
            if (
                self._integration_review_checkpoint(receipt.review_round_id)
                != receipt.review_checkpoint_hash
            ):
                raise ValueError(
                    "integration review evidence changed before acceptance"
                )
            self._connection.execute(
                "INSERT INTO integration_acceptances VALUES (?, ?, ?, ?)",
                (
                    integration_id,
                    receipt.acceptance_id,
                    receipt.review_round_id,
                    task_execution_json(receipt),
                ),
            )
