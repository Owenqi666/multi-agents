"""One repair from the current verified integration, followed by full verification."""

import argparse
import asyncio
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from multi_agent.core.events import freeze_json
from multi_agent.core.integration import IntegrationCandidate, IntegrationRecord
from multi_agent.core.integration_rework import IntegrationReworkRequest
from multi_agent.core.integration_rework_execution import IntegrationReworkOutcome
from multi_agent.core.integration_verification import IntegrationReviewLink
from multi_agent.core.state import Disposition
from multi_agent.core.task_execution import task_execution_value
from multi_agent.store.integration_store import IntegrationStore

from . import runner
from .integration_cli import _candidate_ref, _view, inspect_integration_candidate
from .integration_git import IntegrationGit, verify_integration_refs
from .integration_review import (
    check_rework_paths,
    inspect_integration_review,
    select_rework_member,
)
from .integration_rework_runtime import (
    execute_integration_rework,
    prepare_integration_rework_actor,
)
from .integration_verification import (
    _check_target,
    _current_review,
    _current_test,
    _review_directory,
    _review_input,
    run_integration_review_cycle,
    run_integration_test_cycle,
)
from .task_cli import _interruptible
from .task_context import _Database, _new_root, check_task_launch_owners
from .task_test_context import check_task_test_launch_owners
from .workspaces import ExecutorLease

_REFUSALS = (ValueError, TypeError, OSError, KeyError, sqlite3.Error)
_UNRESOLVED = {
    "confirmation_round_incomplete",
    "confirmation_targets_unanswered",
    "blocking_confirmation_unresolved",
}


def check_confirmation_decisions(
    store: IntegrationStore, record: IntegrationRecord
) -> None:
    for round_id in store.candidate_confirmed_reviews(record):
        review = runner.view_round(store, round_id)
        if _UNRESOLVED.intersection(review["assessment"]["reasons"]):
            raise ValueError(
                f"integration confirmation for {round_id} still requires resolution"
            )


def _stop_result(status: str, detail: str) -> tuple[dict[str, Any], int]:
    return {"rework": {"status": status, "reason": detail}}, 1


def rework_directories(
    link: IntegrationReviewLink, database: _Database, lease: ExecutorLease
) -> tuple[Path, Path, Path, Path]:
    """Check the original review directory and the three fresh execution paths."""
    directory = _review_directory(link)
    runtime, test, review = (
        _new_root(directory.with_name(directory.name + suffix), database, lease)
        for suffix in ("-rework", "-retest", "-rereview")
    )
    return directory, runtime, test, review


def _run_integration_rework(
    store: IntegrationStore,
    lease: ExecutorLease,
    record: IntegrationRecord,
    database: _Database,
    round_id: str,
) -> tuple[dict[str, Any], int]:
    prior = store.integration_rework(record.integration_id)
    if prior is not None:
        return _stop_result(
            "budget_exhausted",
            f"the lineage already reserved rework {prior['rework_id']}",
        )

    try:
        _check_target(record)
        check_confirmation_decisions(store, record)
        link = _current_review(store, record, round_id)
        directory, runtime_dir, test_dir, review_dir = rework_directories(
            link, database, lease
        )
        attempt = _current_test(store, record, link.test_attempt)
        source = _review_input(record, attempt, lease, database)
        parent = inspect_integration_candidate(record)
        with runner.execution_lock(directory):
            checkpoint = store.integration_review_checkpoint(link.round_id)
            reviewed = inspect_integration_review(
                store, link.round_id, source, require_ready=False
            )
            protected = attempt.request.rules.rules.protected_paths
            member, findings = select_rework_member(
                record, reviewed, target_protected=protected
            )
            actor = prepare_integration_rework_actor(store, member)
            review_settings = runner.metadata_for(store, link.round_id)
            seats = tuple(seat.config for seat in store.load_round(link.round_id).seats)
            check_task_launch_owners(store, lease)
            check_task_test_launch_owners(store, lease)
            request = IntegrationReworkRequest(
                uuid4().hex,
                record.integration_id,
                link.round_id,
                source.snapshot_id,
                checkpoint,
                attempt.number,
                attempt.evidence_hash,
                member,
                str(runtime_dir),
                freeze_json(task_execution_value(actor)),
                tuple(freeze_json(item) for item in findings),
            )
            store.reserve_integration_rework(record, request)

            async def execute(stop: asyncio.Event) -> IntegrationReworkOutcome:
                return await execute_integration_rework(
                    rework_id=request.rework_id,
                    lease=lease,
                    database=database.path,
                    directory=runtime_dir,
                    record=record,
                    member=member,
                    actor=actor,
                    source=source,
                    findings=findings,
                    stop=stop,
                )

            outcome, interrupted = asyncio.run(_interruptible(execute))
            report = task_execution_value(outcome)
            child = None
            delta = outcome.delta
            if (
                outcome.status == "succeeded"
                and outcome.process_stopped is True
                and delta is not None
            ):
                try:
                    check_rework_paths(record, member, delta.paths, protected)
                    identifier = uuid4().hex
                    candidate = IntegrationCandidate(
                        _candidate_ref(identifier), delta.commit, delta.tree
                    )
                    revised = replace(
                        record, integration_id=identifier, result=candidate
                    )
                    with (
                        IntegrationGit(lease.repository) as git,
                        verify_integration_refs(
                            lease.repository,
                            record.target_ref,
                            record.target_baseline,
                            parent.ref,
                            parent.commit,
                        ),
                    ):
                        database.check()
                        if (
                            git.parents(delta.commit) != (parent.commit,)
                            or git.tree(delta.commit) != delta.tree
                        ):
                            raise ValueError(
                                "repair delivery is not based on the fixed integration"
                            )
                        git.pin(candidate.ref, candidate.commit)
                        disposition = store.finish_integration_rework(
                            record, request, report, revised
                        )
                        if disposition != Disposition.APPLIED:
                            raise ValueError(
                                "integration repair completion remains historical"
                            )
                    child = revised
                except _REFUSALS as error:
                    report = report | {
                        "status": "delivery_blocked",
                        "runtime_status": outcome.status,
                        "reason": str(error),
                    }
            if (
                child is None
                and store.finish_integration_rework(record, request, report)
                != Disposition.APPLIED
            ):
                raise ValueError("integration rework completion remains historical")
    except _REFUSALS as error:
        return _stop_result("user_decision", str(error))

    result = {"rework": store.integration_rework(record.integration_id)}
    if child is not None:
        result["revision"] = _view(child, "integrated-rework")
    if interrupted or outcome.status == "cancelled":
        return result, 130
    if child is None:
        return result, 1
    try:
        tested, code = run_integration_test_cycle(
            store,
            lease,
            child,
            database,
            test_dir,
            attempt.request.rules,
            attempt.request.policy,
        )
        result["retest"] = tested
        if code:
            return result, code
        new_attempt = _current_test(store, child)
        new_source = _review_input(child, new_attempt, lease, database)
        _, rereviewed, code = run_integration_review_cycle(
            store,
            child,
            new_attempt,
            new_source,
            review_dir,
            seats=seats,
            timeout=review_settings["timeout"],
            grace=review_settings["grace"],
            executables=review_settings["executables"],
            auto_rework=False,
        )
        result["rereview"] = rereviewed
        return result, code
    except _REFUSALS as error:
        return result | {"verification_stopped": str(error)}, 1


def maybe_rework_integration(
    store: IntegrationStore,
    lease: ExecutorLease,
    record: IntegrationRecord,
    database: _Database,
    link: IntegrationReviewLink,
    result: dict[str, Any],
    code: int,
) -> tuple[dict[str, Any], int]:
    assessment = result["review"]["assessment"]
    if code == 130 or not link.auto_rework or assessment["status"] != "needs_rework":
        return result, code
    reworked, code = _run_integration_rework(
        store, lease, record, database, link.round_id
    )
    return result | reworked, code


def manual_rework(
    args: argparse.Namespace,
    store: IntegrationStore,
    lease: ExecutorLease,
    record: IntegrationRecord,
    database: _Database,
) -> tuple[dict[str, Any], int]:
    result, code = _run_integration_rework(store, lease, record, database, args.review)
    return {
        "operation": "rework",
        "integration_id": record.integration_id,
    } | result, code
