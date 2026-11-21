"""Compose verified task candidates and publish an immutable integration."""

import argparse
import json
import sqlite3
from contextlib import nullcontext
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
from multi_agent.core.integration_rework import IntegrationRepairDelta
from multi_agent.core.path_patterns import path_matches
from multi_agent.core.task_execution import task_execution_value
from multi_agent.core.transitions import apply_integration
from multi_agent.store.integration_store import IntegrationStore
from multi_agent.store.project_integration import (
    IntegrationMemberVersion,
    ProjectIntegrationStore,
)

from .errors import report_refusal
from .integration_git import IntegrationGit, _git, read_ref, read_tree
from .task_candidates import inspect_task_candidate
from .task_context import _database, _store_database
from .task_inputs import freeze_project_rules, validate_task_graph
from .workspaces import ExecutorLease, _check_lease, executor_lock

type _RepairLineage = tuple[
    IntegrationRecord, IntegrationRecord, IntegrationMember, IntegrationRepairDelta
]


def add_parser(commands: Any) -> None:
    from .integration_verification import add_verification_parsers

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
    show.add_argument("--history", action="store_true")
    add_verification_parsers(operations)


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


def inspect_delegated_prerequisite_integration(
    database: Path,
    repository: Path,
    stage_baseline: str,
    members: tuple[IntegrationMemberVersion, ...],
    integration_id: str,
    candidate: str,
    *,
    lease: ExecutorLease | None,
) -> None:
    """Require live test and review evidence for an exact upstream integration."""
    from .integration_review import inspect_integration_review
    from .integration_verification import _current_test, _review_input
    from .task_test_cli import _existing_lease

    with ProjectIntegrationStore.open_readonly(database) as store:
        record = store.newest_matching_integration(
            str(repository), stage_baseline, members, integration_id=integration_id
        )
        if record is None or inspect_integration_candidate(record).commit != candidate:
            raise ValueError("delegated prerequisite integration binding changed")
        member = record.members[0]
        held = (
            nullcontext(lease)
            if lease is not None
            else _existing_lease(store, member.task_run_id, member.attempt)
        )
        with held as active:
            store.require_current_integration_revision(integration_id)
            reviews = store.integration_reviews(integration_id)
            if not reviews:
                raise ValueError(
                    "delegated prerequisite integration requires a current review"
                )
            latest = reviews[-1]
            attempt = _current_test(store, record, latest.test_attempt)
            source = _review_input(record, attempt, active, _store_database(store))
            inspect_integration_review(store, latest.round_id, source)


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


def _carry_repair(
    git: IntegrationGit,
    rebuilt: IntegrationCandidate,
    target_baseline: str,
    source: IntegrationRecord,
    lineage: _RepairLineage,
) -> IntegrationCandidate:
    original, repaired, owner, delta = lineage
    card = owner.task_input.card
    if (
        original.repository != source.repository
        or repaired.repository != source.repository
        or original.target_ref != source.target_ref
        or repaired.target_ref != source.target_ref
        or original.target_baseline != repaired.target_baseline
        or original.members != source.members
        or repaired.members != source.members
        or owner not in original.members
    ):
        raise ValueError(f"repair lineage differs from the source for {card.id}")
    original_candidate = inspect_integration_candidate(original)
    repaired_candidate = inspect_integration_candidate(repaired)
    if source != repaired:
        inspect_integration_candidate(source)
    if (
        original_candidate.commit != delta.base
        or repaired_candidate.commit != delta.commit
        or repaired_candidate.tree != delta.tree
        or git.parents(delta.commit) != (delta.base,)
        or not delta.paths
        or git.changed_paths(delta.base, delta.commit) != delta.paths
    ):
        raise ValueError(
            f"fixed repair delta differs from the approved source for {card.id}"
        )

    target = freeze_project_rules(Path(source.repository), target_baseline)
    protected = (
        owner.task_input.frozen_rules.rules.protected_paths
        + target.rules.protected_paths
    )
    forbidden = tuple(
        path
        for path in delta.paths
        if not any(path_matches(path, pattern) for pattern in card.write_scope)
        or any(path_matches(path, pattern) for pattern in protected)
    )
    if forbidden:
        raise ValueError(
            f"repair for {card.id} is outside the allowed scope at {target_baseline}: {forbidden}"
        )
    tip, conflicts = git.merge(
        delta.base, rebuilt.commit, delta.commit, f"{card.id} repair"
    )
    if conflicts is not None:
        detail = repr(conflicts) if conflicts else "Git provided no path entries"
        raise ValueError(
            f"cannot rebase repair for {card.id} due to Git conflict: {detail}"
        )
    changed = git.changed_paths(rebuilt.commit, tip)
    unexpected = tuple(path for path in changed if path not in delta.paths)
    if unexpected:
        raise ValueError(
            f"rebased repair for {card.id} changes unapproved paths: {unexpected}"
        )
    return replace(rebuilt, commit=tip, tree=git.tree(tip))


def create_integration(
    database_path: Path,
    repository: Path,
    target: str,
    task_run_ids: list[str],
    *,
    rebased_from: str | None = None,
    expected_baseline: str | None = None,
    expected_members: tuple[IntegrationMemberVersion, ...] | None = None,
    prepare_target: bool = False,
) -> IntegrationRecord:
    database = _database(database_path)
    with (
        executor_lock(repository) as lease,
        IntegrationStore(database.path) as store,
        IntegrationGit(lease.repository) as git,
    ):
        target_ref = git.branch_ref(target)
        baseline = git.ref_commit(target_ref)
        create_target = baseline is None and prepare_target
        if baseline is None:
            if not create_target or expected_baseline is None:
                raise ValueError(f"local branch {target_ref} does not exist")
            baseline = expected_baseline
        if expected_baseline is not None and baseline != expected_baseline:
            raise ValueError(
                f"local branch {target_ref} is at {baseline}, expected stage baseline {expected_baseline}"
            )
        members = _ordered_members(store, lease, task_run_ids)
        if expected_members is not None and sorted(expected_members) != sorted(
            (item.task_run_id, item.attempt, item.delivery.candidate)
            for item in members
        ):
            raise ValueError("stage leaf candidates changed before integration")
        source = None
        repair = None
        if rebased_from is not None:
            source = store.load_integration(rebased_from)
            if (
                source.repository != str(lease.repository)
                or source.target_ref != target_ref
                or baseline == source.target_baseline
            ):
                raise ValueError(
                    "rebase requires a changed target from the original integration"
                )
            if source.members != members:
                raise ValueError(
                    "member candidates changed; use integration create with current task IDs"
                )
            repair = store.integration_repair_for_rebase(rebased_from)
        _validate_lineage(store, git, str(lease.repository), members)
        integration_id = uuid4().hex
        progress = _assemble(git, integration_id, baseline, members)
        result = progress.result
        assert result is not None
        if repair is not None:
            assert source is not None
            if isinstance(result, IntegrationConflict):
                blocked = members[progress.applied_count].task_input.card.id
                owner = repair[2].task_input.card.id
                raise ValueError(
                    f"cannot carry repair for {owner}: rebuilding {blocked} conflicts at {result.paths}"
                )
            result = _carry_repair(git, result, baseline, source, repair)
        record = IntegrationRecord(
            integration_id,
            str(lease.repository),
            target_ref,
            baseline,
            members,
            progress.applied_count,
            result,
        )
        _check_lease(lease)
        database.check()
        if create_target:
            _git(
                lease.repository,
                "update-ref",
                "--no-deref",
                target_ref,
                baseline,
                "0" * len(baseline),
            )
        if (
            expected_baseline is not None
            and git.ref_commit(target_ref) != expected_baseline
        ):
            raise ValueError("stage target changed before integration publication")
        if isinstance(record.result, IntegrationCandidate):
            git.pin(record.result.ref, record.result.commit)
        try:
            store.create_integration(record, rebased_from=rebased_from)
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
    from .integration_verification import run_operation, verification_view

    operation = args.integration_operation
    try:
        if operation == "create":
            record = create_integration(
                args.database, args.repository, args.target, args.task_run_ids
            )
            result = _view(record, operation)
            code = 0 if result["verification"] == "verified" else 1
        elif operation == "show":
            database = _database(args.database)
            with IntegrationStore.open_readonly(database.path) as store:
                record = store.load_integration(args.integration_id)
                result = _view(record, operation)
                if store.has_integration_verification():
                    result.update(
                        verification_view(
                            store,
                            record,
                            target_current=result["target_is_current"],
                            history=args.history,
                        )
                    )
            code = 0
        else:
            result, code = run_operation(args)
    except (
        OSError,
        UnicodeError,
        ValueError,
        TypeError,
        KeyError,
        sqlite3.Error,
    ) as error:
        return report_refusal(
            error,
            code="integration_accept_refused"
            if operation == "accept"
            else "cli_refused",
        )
    except KeyboardInterrupt:
        return 130
    print(json.dumps(result, ensure_ascii=False))
    return code
