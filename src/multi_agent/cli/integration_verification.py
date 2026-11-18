"""Run and qualify verification for one fixed integration candidate."""

import argparse
import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from multi_agent.core.events import freeze_json
from multi_agent.core.integration import IntegrationRecord
from multi_agent.core.integration_testing import IntegrationTestResult
from multi_agent.core.integration_verification import (
    IntegrationAcceptance,
    IntegrationReviewLink,
    IntegrationTestAttempt,
    IntegrationTestRequest,
)
from multi_agent.core.review_input import ExecutedCommand, IntegrationReviewInput
from multi_agent.core.state import Disposition, SeatConfig
from multi_agent.core.task_execution import task_execution_value
from multi_agent.store.integration_store import IntegrationStore

from . import runner
from .integration_cli import _view, create_integration, inspect_integration_candidate
from .integration_git import read_ref, verify_integration_refs
from .integration_review import inspect_integration_review, prepare_integration_review
from .integration_tests import (
    execute_integration_tests,
    inspect_integration_test_result,
)
from .task_cli import _interruptible, _program
from .task_context import (
    _Database,
    _database,
    _directory,
    _identity,
    _new_root,
    _store_database,
    check_task_launch_owners,
)
from .task_inputs import freeze_project_rules
from .task_test_cli import _existing_lease
from .task_test_context import capture_task_test_policy, check_task_test_launch_owners
from .workspaces import ExecutorLease, executor_lock

_EVIDENCE_ERRORS = (OSError, ValueError, TypeError, KeyError)


def add_verification_parsers(operations: Any) -> None:
    for name in ("test", "review", "retry-review", "accept", "rebase"):
        parser = operations.add_parser(name)
        parser.add_argument("integration_id")
        parser.add_argument("--database", type=Path, required=True)
        parser.add_argument("--repository", type=Path, required=True)
        if name in ("test", "review"):
            parser.add_argument("--directory", type=Path, required=True)
            parser.add_argument("--codex-bin", default="codex")
            parser.add_argument("--timeout", type=float, default=180)
            parser.add_argument("--grace", type=float, default=2)
        if name == "test":
            parser.add_argument(
                "--output-bytes-limit", type=int, default=8 * 1024 * 1024
            )
            parser.add_argument("--tool-directory", action="append", default=[])
            parser.add_argument("--runtime-read-path", action="append", default=[])
        if name == "review":
            parser.add_argument(
                "--seat",
                nargs=3,
                action="append",
                required=True,
                metavar=("PROVIDER", "MODEL", "EFFORT"),
            )
            parser.add_argument("--claude-bin", default="claude")
            parser.add_argument("--test-attempt", type=int)
        if name in ("retry-review", "accept"):
            parser.add_argument("--review", required=True, metavar="ROUND_ID")
        if name == "retry-review":
            parser.add_argument("--seat", action="append", metavar="SEAT_KEY")


def _check_target(record: IntegrationRecord) -> None:
    if read_ref(Path(record.repository), record.target_ref) != record.target_baseline:
        raise ValueError(
            f"target branch changed since {record.target_baseline}; "
            f"use integration rebase {record.integration_id} with --database and --repository"
        )


@contextmanager
def _source(
    args: argparse.Namespace,
) -> Iterator[tuple[IntegrationStore, ExecutorLease, IntegrationRecord, _Database]]:
    database = _database(args.database)
    with (
        executor_lock(args.repository) as lease,
        IntegrationStore(database.path) as store,
    ):
        record = store.load_integration(args.integration_id)
        if record.repository != str(lease.repository):
            raise ValueError("integration belongs to a different Git common directory")
        _check_target(record)
        database.check()
        yield store, lease, record, database


def _test_view(attempt: IntegrationTestAttempt) -> dict[str, Any]:
    result = attempt.result
    commands = attempt.request.rules.rules.commands
    attempted = (
        {item.sequence for item in result.commands if item.request_attempted}
        if result
        else set()
    )
    unconfirmed = (
        tuple(
            item.command
            for item in result.commands
            if item.request_attempted
            and (not item.request_settled or item.reported_exit_code is None)
        )
        if result
        else ()
    )
    return {
        "attempt": attempt.number,
        "status": result.status if result else "running",
        "candidate": task_execution_value(attempt.request.candidate),
        "rules_baseline": attempt.request.rules.baseline_commit,
        "rules_blob": attempt.request.rules.blob_oid,
        "rules_hash": attempt.request.rules.rules_hash,
        "required_commands": task_execution_value(commands),
        "unexecuted_commands": task_execution_value(
            tuple(
                command
                for sequence, command in enumerate(commands, 1)
                if sequence not in attempted
            )
        ),
        "unconfirmed_commands": task_execution_value(unconfirmed),
        "directory": attempt.request.directory,
        "result": task_execution_value(result),
        "evidence_hash": attempt.evidence_hash if result else None,
    }


def _inspect_test(
    record: IntegrationRecord,
    attempt: IntegrationTestAttempt,
    lease: ExecutorLease,
    database: _Database,
) -> tuple[ExecutedCommand, ...]:
    if attempt.result is None or attempt.result.status != "succeeded":
        raise ValueError("integration requires the current complete passing test suite")
    request = attempt.request
    candidate = inspect_integration_candidate(record)
    if (
        candidate != request.candidate
        or freeze_project_rules(lease.source, record.target_baseline) != request.rules
    ):
        raise ValueError(
            "integration test request differs from the frozen candidate or rules"
        )
    database.check()
    return inspect_integration_test_result(
        attempt.result,
        integration_id=record.integration_id,
        attempt=attempt.number,
        lease=lease,
        database=database.path,
        root=Path(request.directory),
        candidate=request.candidate,
        rules=request.rules,
        policy=request.policy,
    )


def _review_input(
    record: IntegrationRecord,
    attempt: IntegrationTestAttempt,
    lease: ExecutorLease,
    database: _Database,
) -> IntegrationReviewInput:
    commands = _inspect_test(record, attempt, lease, database)
    return prepare_integration_review(
        record, attempt.number, attempt.evidence_hash, commands
    )


def _current_test(
    store: IntegrationStore, record: IntegrationRecord, expected: int | None = None
) -> IntegrationTestAttempt:
    attempt = store.current_integration_test(record.integration_id)
    if attempt is None or expected is not None and expected != attempt.number:
        raise ValueError("integration has no matching current test attempt")
    return attempt


def _review_directory(link: IntegrationReviewLink) -> Path:
    path = Path(link.directory)
    _directory(path, identity=link.directory_identity, mode=0o700)
    return path


def _current_review(
    store: IntegrationStore, record: IntegrationRecord, round_id: str
) -> IntegrationReviewLink:
    links = store.integration_reviews(record.integration_id)
    if not links or links[-1].round_id != round_id:
        raise ValueError("integration requires its current review round")
    return links[-1]


def _review_view(
    store: IntegrationStore,
    link: IntegrationReviewLink,
    source: IntegrationReviewInput | None,
    *,
    target_current: bool,
    accepted: bool,
    history: bool = False,
) -> dict[str, Any]:
    view = runner.view_round(store, link.round_id, history=history)
    exact_source = source is not None and link.snapshot_id == source.snapshot_id
    verified = False
    errors = []
    if exact_source:
        assert source is not None
        try:
            inspect_integration_review(
                store, link.round_id, source, require_ready=False
            )
            verified = True
        except _EVIDENCE_ERRORS as error:
            errors.append(str(error))
    source_current = exact_source and target_current
    return view | {
        "test_attempt": link.test_attempt,
        "source_current": source_current,
        "verification": "verified" if verified else "unverified",
        "source_errors": errors,
        "current_eligible": source_current
        and verified
        and not accepted
        and view["assessment"]["status"] == "ready",
    }


def _test(
    args: argparse.Namespace,
    store: IntegrationStore,
    lease: ExecutorLease,
    record: IntegrationRecord,
    database: _Database,
) -> tuple[dict[str, Any], int]:
    candidate = inspect_integration_candidate(record)
    rules = freeze_project_rules(lease.source, record.target_baseline)
    if not rules.rules.commands:
        raise ValueError("target baseline registers no test commands")
    root = _new_root(args.directory, database, lease)
    policy = capture_task_test_policy(
        args.codex_bin,
        timeout=args.timeout,
        grace=args.grace,
        output_bytes_limit=args.output_bytes_limit,
        tool_directories=tuple(args.tool_directory),
        runtime_read_paths=tuple(args.runtime_read_path),
    )
    check_task_launch_owners(store, lease)
    check_task_test_launch_owners(store, lease)
    attempt = store.reserve_integration_test(
        record, IntegrationTestRequest(candidate, rules, policy, str(root))
    )

    async def execute(stop: asyncio.Event) -> IntegrationTestResult:
        return await execute_integration_tests(
            integration_id=record.integration_id,
            attempt=attempt.number,
            lease=lease,
            database=database.path,
            root=root,
            candidate=candidate,
            rules=rules,
            policy=policy,
            stop=stop,
        )

    result, interrupted = asyncio.run(_interruptible(execute))
    database.check()
    disposition = store.finish_integration_test(
        record.integration_id, attempt.number, result
    )
    if disposition != Disposition.APPLIED:
        raise ValueError(
            "integration test completion did not apply to the current attempt"
        )
    current = _current_test(store, record, attempt.number)
    code = (
        130
        if interrupted or result.status == "cancelled"
        else 0
        if result.status == "succeeded"
        else 1
    )
    return {
        "operation": "test",
        "integration_id": record.integration_id,
        "test": _test_view(current),
    }, code


def _dispatch_review(
    store: IntegrationStore,
    record: IntegrationRecord,
    link: IntegrationReviewLink,
    directory: Path,
    operation: str,
    source: IntegrationReviewInput,
    seats: tuple[str, ...] | None = None,
) -> tuple[dict[str, Any], int]:
    async def execute(stop: asyncio.Event) -> None:
        await runner.execute_round(
            store, link.round_id, directory, stop=stop, seat_keys=seats
        )

    _, interrupted = asyncio.run(_interruptible(execute))
    target_current = (
        read_ref(Path(record.repository), record.target_ref) == record.target_baseline
    )
    view = _review_view(
        store, link, source, target_current=target_current, accepted=False
    )
    return {
        "operation": operation,
        "integration_id": record.integration_id,
        "test_attempt": link.test_attempt,
        "review": view,
    }, 130 if interrupted else 0 if view["current_eligible"] else 1


def _review(
    args: argparse.Namespace,
    store: IntegrationStore,
    lease: ExecutorLease,
    record: IntegrationRecord,
    database: _Database,
) -> tuple[dict[str, Any], int]:
    if store.load_integration_acceptance(record.integration_id) is not None:
        raise ValueError("integration already has an immutable acceptance receipt")
    attempt = _current_test(store, record, args.test_attempt)
    source = _review_input(record, attempt, lease, database)
    seats = tuple(
        SeatConfig(f"seat-{index}", *values)
        for index, values in enumerate(args.seat, 1)
    )
    runner.validate_review_settings(seats, timeout=args.timeout, grace=args.grace)
    executables = {
        provider: _program(getattr(args, f"{provider}_bin"), provider)
        for provider in {seat.provider for seat in seats}
    }
    directory = _new_root(args.directory, database, lease)
    directory.mkdir(mode=0o700)
    with runner.execution_lock(directory):
        store.save_snapshot(source.snapshot_id, freeze_json(source.to_dict()))
        round_id = runner.new_review(
            store,
            source.snapshot_id,
            seats,
            timeout=args.timeout,
            grace=args.grace,
            executables=executables,
        )
        link = IntegrationReviewLink(
            round_id,
            attempt.number,
            source.snapshot_id,
            str(directory),
            _identity(directory.lstat()),
        )
        store.link_integration_review(
            record.integration_id, link, attempt.evidence_hash
        )
        return _dispatch_review(store, record, link, directory, "review", source)


def _retry_review(
    args: argparse.Namespace,
    store: IntegrationStore,
    lease: ExecutorLease,
    record: IntegrationRecord,
    database: _Database,
) -> tuple[dict[str, Any], int]:
    link = _current_review(store, record, args.review)
    attempt = _current_test(store, record, link.test_attempt)
    source = _review_input(record, attempt, lease, database)
    directory = _review_directory(link)
    with runner.execution_lock(directory):
        if runner.snapshot_for(store, link.snapshot_id) != source:
            raise ValueError("review retry differs from the original frozen snapshot")
        seats = runner.prepare_retry(
            store, link.round_id, tuple(args.seat) if args.seat else None, recover=False
        )
        return _dispatch_review(
            store, record, link, directory, "retry-review", source, seats
        )


def _accept(
    args: argparse.Namespace,
    store: IntegrationStore,
    lease: ExecutorLease,
    record: IntegrationRecord,
    database: _Database,
) -> tuple[dict[str, Any], int]:
    link = _current_review(store, record, args.review)
    attempt = _current_test(store, record, link.test_attempt)
    candidate = inspect_integration_candidate(record)
    directory = _review_directory(link)
    with runner.execution_lock(directory):
        try:
            with verify_integration_refs(
                lease.repository,
                record.target_ref,
                record.target_baseline,
                candidate.ref,
                candidate.commit,
            ):
                source = _review_input(record, attempt, lease, database)
                checkpoint = store.integration_review_checkpoint(link.round_id)
                inspect_integration_review(store, link.round_id, source)
                database.check()
                receipt = IntegrationAcceptance(
                    uuid4().hex,
                    record.integration_id,
                    record.target_ref,
                    record.target_baseline,
                    candidate,
                    attempt.number,
                    attempt.evidence_hash,
                    link.round_id,
                    source.snapshot_id,
                    checkpoint,
                    runner.now(),
                )
                store.accept_integration(record, receipt)
        except ValueError:
            _check_target(record)
            raise
    return {"operation": "accept", "acceptance": task_execution_value(receipt)}, 0


def _blocking_findings(
    record: IntegrationRecord, review: dict[str, Any]
) -> list[dict[str, Any]]:
    findings = []
    for group in (review["assessment"] or {}).get("groups", ()):
        if not group["blocking"]:
            continue
        for item in group["members"]:
            finding = item["finding"]
            if finding["severity"] not in ("P0", "P1"):
                continue
            eligible = [
                member
                for member in record.members
                if finding["path"] in member.delivery.changed_paths
            ]
            findings.append(
                {
                    "finding": finding,
                    "candidate": task_execution_value(record.result),
                    "round_id": review["round_id"],
                    "owner": task_execution_value(eligible[0])
                    if len(eligible) == 1 and finding["reference_status"] == "verified"
                    else None,
                }
            )
    return findings


def prepare_integration_rework_source(
    store: IntegrationStore,
    record: IntegrationRecord,
    lease: ExecutorLease,
    database: _Database,
) -> dict[str, Any]:
    """Prepare the verified source for a later bounded implementation attempt."""
    _check_target(record)
    links = store.integration_reviews(record.integration_id)
    if not links:
        raise ValueError("integration has no review for rework")
    link = links[-1]
    attempt = _current_test(store, record, link.test_attempt)
    source = _review_input(record, attempt, lease, database)
    checkpoint = store.integration_review_checkpoint(link.round_id)
    review = inspect_integration_review(
        store, link.round_id, source, require_ready=False
    )
    if review["assessment"]["status"] != "needs_rework":
        raise ValueError("integration review has no verified rework finding")
    return {
        "integration": task_execution_value(record),
        "test_attempt": attempt.number,
        "test_evidence_hash": attempt.evidence_hash,
        "review_round_id": link.round_id,
        "review_snapshot_id": source.snapshot_id,
        "review_checkpoint_hash": checkpoint,
        "blocking_findings": _blocking_findings(record, review),
    }


def verification_view(
    store: IntegrationStore,
    record: IntegrationRecord,
    *,
    target_current: bool,
    history: bool = False,
) -> dict[str, Any]:
    identifier = record.integration_id
    attempt = store.current_integration_test(identifier)
    links = store.integration_reviews(identifier)
    acceptance = store.load_integration_acceptance(identifier)
    source = None
    verified = False
    errors = []
    if attempt and attempt.result and attempt.result.status == "succeeded":
        member = record.members[0]
        try:
            with _existing_lease(store, member.task_run_id, member.attempt) as lease:
                commands = _inspect_test(record, attempt, lease, _store_database(store))
                verified = True
                if links and links[-1].test_attempt == attempt.number:
                    source = prepare_integration_review(
                        record, attempt.number, attempt.evidence_hash, commands
                    )
        except _EVIDENCE_ERRORS as error:
            errors.append(str(error))
    reviews = [
        _review_view(
            store,
            link,
            source if link == links[-1] else None,
            target_current=target_current,
            accepted=acceptance is not None,
            history=history,
        )
        for link in links
    ]
    current_review = reviews[-1] if reviews and source is not None else None
    value = {
        "rebased_from": store.integration_parent(identifier),
        "test": _test_view(attempt) if attempt else None,
        "test_evidence_verification": "verified" if verified else "unverified",
        "test_source_errors": errors,
        "reviews": reviews,
        "current_eligible": current_review is not None
        and current_review["current_eligible"],
        "blocking_findings": _blocking_findings(record, current_review)
        if current_review
        else [],
        "acceptance": task_execution_value(acceptance),
    }
    if history:
        value["test_history"] = [
            _test_view(prior) | {"late_results": task_execution_value(late)}
            for prior, late in store.integration_test_history(identifier)
        ]
    return value


def run_operation(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    operation = args.integration_operation
    if operation == "rebase":
        database_path = _database(args.database).path
        with IntegrationStore.open_readonly(database_path) as store:
            source = store.load_integration(args.integration_id)
        record = create_integration(
            database_path,
            args.repository,
            source.target_ref,
            [member.task_run_id for member in source.members],
            rebased_from=source.integration_id,
        )
        result = _view(record, operation) | {"rebased_from": source.integration_id}
        return result, 0 if result["verification"] == "verified" else 1
    with _source(args) as (store, lease, record, database):
        return {
            "test": _test,
            "review": _review,
            "retry-review": _retry_review,
            "accept": _accept,
        }[operation](args, store, lease, record, database)
