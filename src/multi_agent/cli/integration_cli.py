"""Compose verified task candidates and publish an immutable integration."""

import argparse
import json
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from multi_agent.core.events import FailureCategory
from multi_agent.core.integration import (
    IntegrationCandidate,
    IntegrationConflict,
    IntegrationMember,
    IntegrationMemberApplied,
    IntegrationMemberConflicted,
    IntegrationProgress,
    IntegrationRecord,
)
from multi_agent.core.task_execution import task_execution_value
from multi_agent.core.transitions import apply_integration
from multi_agent.store.integration_store import IntegrationStore

from .integration_git import IntegrationGit, read_ref, read_tree
from .task_candidates import inspect_task_candidate
from .task_context import _database
from .task_inputs import validate_task_graph
from .workspaces import ExecutorLease, _check_lease, executor_lock


def add_parser(commands: Any) -> None:
    integration = commands.add_parser(
        "integration", help="integrate and inspect verified task candidates"
    )
    operations = integration.add_subparsers(dest="integration_operation", required=True)
    create = operations.add_parser("create")
    create.add_argument("task_run_ids", nargs="+", metavar="TASK_RUN_ID")
    create.add_argument("--database", type=Path, required=True)
    create.add_argument("--repository", type=Path, required=True)
    create.add_argument("--target", required=True, metavar="LOCAL_BRANCH")
    show = operations.add_parser("show")
    show.add_argument("integration_id")
    show.add_argument("--database", type=Path, required=True)


def _candidate_ref(integration_id: str) -> str:
    return f"refs/multi-agent/integrations/{integration_id}"


def inspect_integration_candidate(record: IntegrationRecord) -> IntegrationCandidate:
    """Read back the fixed candidate independently of the live target branch."""
    candidate = record.result
    if not isinstance(candidate, IntegrationCandidate):
        raise TypeError(f"integration {record.integration_id} has no final candidate")
    repository = Path(record.repository)
    if (
        candidate.ref != _candidate_ref(record.integration_id)
        or read_ref(repository, candidate.ref) != candidate.commit
        or read_tree(repository, candidate.commit) != candidate.tree
    ):
        raise ValueError(
            f"integration {record.integration_id} candidate binding changed"
        )
    return candidate


def _ordered_members(
    store: IntegrationStore, lease: ExecutorLease, task_run_ids: list[str]
) -> tuple[IntegrationMember, ...]:
    if len(task_run_ids) != len(set(task_run_ids)):
        raise ValueError("duplicate task run ID")
    members = []
    for task_run_id in task_run_ids:
        task = store.load_task(task_run_id)
        attempt = task.current_attempt
        if attempt is None:
            raise ValueError(f"{task.task_id} ({task_run_id}) has no implementation")
        delivery = inspect_task_candidate(
            store, task_run_id, attempt.number, lease
        ).delivery
        task_input = store.load_task_input(task_run_id)
        members.append(
            IntegrationMember(
                task_run_id,
                store.planning_run_for_task(task_run_id),
                attempt.number,
                task_input,
                delivery,
                store.load_task_provenance(task_run_id).planning_input.dependencies,
            )
        )

    selected = {member.task_input.card.id for member in members}
    external_ids: set[str] = set()
    validated = []
    for member in members:
        card = member.task_input.card
        external = set(card.depends_on) - selected
        receipts = tuple(
            item for item in member.external_dependencies if item.task_id in external
        )
        missing = external - {item.task_id for item in receipts}
        if missing:
            raise ValueError(
                f"{card.id} has undeclared dependencies: {', '.join(sorted(missing))}"
            )
        external_ids.update(external)
        validated.append(replace(member, external_dependencies=receipts))
    validate_task_graph(
        tuple(member.task_input.card for member in validated), frozenset(external_ids)
    )
    pending = {member.task_input.card.id: member for member in validated}
    ordered = []
    while pending:
        available = next(
            key
            for key, member in pending.items()
            if pending.keys().isdisjoint(member.task_input.card.depends_on)
        )
        ordered.append(pending.pop(available))
    return tuple(ordered)


def _validate_lineage(
    store: IntegrationStore,
    git: IntegrationGit,
    repository: str,
    members: tuple[IntegrationMember, ...],
) -> None:
    selected = {member.task_input.card.id: member for member in members}
    earlier: tuple[IntegrationRecord, ...] | None = None
    for member in members:
        for dependency in member.task_input.card.depends_on:
            upstream = selected.get(dependency)
            if upstream is None:
                continue
            source = upstream.delivery.candidate
            baseline = member.delivery.baseline
            if git.is_ancestor(source, baseline):
                continue
            if earlier is None:
                earlier = store.list_integration_candidates(repository)
            for record in earlier:
                if (
                    isinstance(record.result, IntegrationCandidate)
                    and any(
                        previous.task_input.card.id == dependency
                        and previous.delivery.candidate == source
                        for previous in record.members
                    )
                    and git.is_ancestor(record.result.commit, baseline)
                ):
                    inspect_integration_candidate(record)
                    break
            else:
                raise ValueError(
                    f"{member.task_input.card.id} must rebase: its baseline {baseline} "
                    f"does not include {dependency} candidate {source}"
                )


def _assemble(
    git: IntegrationGit,
    integration_id: str,
    baseline: str,
    members: tuple[IntegrationMember, ...],
) -> IntegrationProgress:
    progress = IntegrationProgress(len(members))
    tip = baseline
    for index, member in enumerate(members):
        delivery = member.delivery
        tip, paths = git.merge(
            delivery.baseline, tip, delivery.candidate, member.task_input.card.id
        )
        if paths is not None:
            return apply_integration(
                progress, IntegrationMemberConflicted(index, IntegrationConflict(paths))
            )
        progress = apply_integration(progress, IntegrationMemberApplied(index))
    candidate = IntegrationCandidate(_candidate_ref(integration_id), tip, git.tree(tip))
    return apply_integration(progress, candidate)


def create_integration(
    database_path: Path, repository: Path, target: str, task_run_ids: list[str]
) -> IntegrationRecord:
    database = _database(database_path)
    with (
        executor_lock(repository) as lease,
        IntegrationStore(database.path) as store,
        IntegrationGit(lease.repository) as git,
    ):
        target_ref = git.branch_ref(target)
        baseline = git.ref_commit(target_ref)
        if baseline is None:
            raise ValueError(f"local branch {target_ref} does not exist")
        members = _ordered_members(store, lease, task_run_ids)
        _validate_lineage(store, git, str(lease.repository), members)
        integration_id = uuid4().hex
        progress = _assemble(git, integration_id, baseline, members)
        assert progress.result is not None
        record = IntegrationRecord(
            integration_id,
            str(lease.repository),
            target_ref,
            baseline,
            members,
            progress.applied_count,
            progress.result,
        )
        _check_lease(lease)
        database.check()
        if isinstance(record.result, IntegrationCandidate):
            git.pin(record.result.ref, record.result.commit)
        try:
            store.create_integration(record)
        except Exception as error:
            if isinstance(record.result, IntegrationCandidate):
                error.add_note(f"Integration ref retained: {record.result.ref}")
            raise
        return record


def _view(record: IntegrationRecord, operation: str) -> dict[str, Any]:
    candidate = None
    conflict = None
    errors = []
    verified = False
    if isinstance(record.result, IntegrationCandidate):
        status = "ready"
        candidate = task_execution_value(record.result)
        try:
            inspect_integration_candidate(record)
            verified = True
        except (OSError, ValueError) as error:
            errors.append(str(error))
    else:
        status = "conflicted"
        member = record.members[record.applied_count]
        conflict = {
            "category": FailureCategory.INTEGRATION_CONFLICT,
            "task_id": member.task_input.card.id,
            "task_run_id": member.task_run_id,
            "attempt": member.attempt,
            "candidate": member.delivery.candidate,
            "paths": record.result.paths,
        }
    try:
        target_head = read_ref(Path(record.repository), record.target_ref)
    except (OSError, ValueError) as error:
        target_head = None
        errors.append(str(error))
    return {
        "operation": operation,
        "integration_id": record.integration_id,
        "status": status,
        "verification": "verified" if verified else "unverified",
        "repository": record.repository,
        "target_ref": record.target_ref,
        "target_baseline": record.target_baseline,
        "target_head": target_head,
        "target_is_current": target_head == record.target_baseline,
        "members": task_execution_value(record.members),
        "applied": [
            member.task_run_id for member in record.members[: record.applied_count]
        ],
        "candidate": candidate,
        "conflict": conflict,
        "source_errors": errors,
    }


def main(args: argparse.Namespace) -> int:
    operation = args.integration_operation
    try:
        if operation == "create":
            record = create_integration(
                args.database, args.repository, args.target, args.task_run_ids
            )
        else:
            database = _database(args.database)
            with IntegrationStore.open_readonly(database.path) as store:
                record = store.load_integration(args.integration_id)
        result = _view(record, operation)
    except (
        OSError,
        UnicodeError,
        ValueError,
        TypeError,
        KeyError,
        sqlite3.Error,
    ) as error:
        print(f"multi-agent: {error}", file=sys.stderr)
        for note in getattr(error, "__notes__", ()):
            print(note, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    print(json.dumps(result, ensure_ascii=False))
    return 1 if operation == "create" and result["verification"] != "verified" else 0
