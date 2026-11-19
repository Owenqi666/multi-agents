"""Project the current delegated integration and its independently verified evidence."""

from dataclasses import asdict
from pathlib import Path
from typing import Any

from multi_agent.core.integration import IntegrationConflict
from multi_agent.store.project_integration import (
    IntegrationMemberVersion,
    ProjectIntegrationStore,
)

from .integration_cli import inspect_integration_candidate
from .integration_git import read_ref
from .integration_verification import verification_view


def view_delegated_integration(
    database: Path,
    git_common_dir: Path,
    execution_baseline: str,
    members: tuple[IntegrationMemberVersion, ...],
    *,
    integration_id: str | None = None,
) -> dict[str, Any] | None:
    """Read evidence for the newest exact integration, even when it is blocked."""
    if not members:
        return None
    with ProjectIntegrationStore.open_readonly(database) as store:
        record = store.newest_matching_integration(
            str(git_common_dir),
            execution_baseline,
            members,
            integration_id=integration_id,
        )
        if record is None:
            return None
        result: dict[str, Any] = {
            "integration_id": record.integration_id,
            "candidate": None,
            "test_attempt": None,
            "test_evidence_hash": None,
            "review_round_id": None,
            "review_snapshot_id": None,
            "tests_passed": False,
            "review_satisfied": False,
            "evidence": None,
            "source_error": None,
        }
        if record.target_baseline != execution_baseline:
            result["source_error"] = (
                f"integration baseline {record.target_baseline} differs from "
                f"stage execution baseline {execution_baseline}"
            )
            return result
        if isinstance(record.result, IntegrationConflict):
            member = record.members[record.applied_count]
            paths = ", ".join(record.result.paths)
            result["source_error"] = (
                f"integration member {member.task_run_id} conflicts at {paths}"
            )
            return result
        try:
            candidate = inspect_integration_candidate(record)
        except (OSError, ValueError) as error:
            result["source_error"] = str(error)
            return result

        result["candidate"] = candidate.commit
        try:
            target_current = (
                read_ref(git_common_dir, record.target_ref) == execution_baseline
            )
        except (OSError, ValueError) as error:
            target_current = False
            result["source_error"] = str(error)
        evidence = verification_view(store, record, target_current=target_current)
        result["evidence"] = evidence
        test = evidence["test"]
        if test is None or test["candidate"] != asdict(candidate):
            return result

        result["test_attempt"] = test["attempt"]
        result["test_evidence_hash"] = test["evidence_hash"]
        tests_passed = (
            test["status"] == "succeeded"
            and evidence["test_evidence_verification"] == "verified"
            and evidence["superseded_by"] is None
        )
        result["tests_passed"] = tests_passed
        reviews = evidence["reviews"]
        if reviews and reviews[-1]["test_attempt"] == test["attempt"]:
            latest = reviews[-1]
            result["review_round_id"] = latest["round_id"]
            result["review_snapshot_id"] = latest["snapshot_id"]
            # Acceptance closes the target's accept action. The stage still
            # needs a verified review bound to this exact candidate and suite.
            result["review_satisfied"] = (
                tests_passed
                and latest["verification"] == "verified"
                and latest["assessment"]["status"] == "ready"
            )
        return result
