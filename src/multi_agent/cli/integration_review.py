"""Freeze and verify a review of the exact tested integration candidate."""

from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any, cast

from multi_agent.core.events import Completed, thaw_json
from multi_agent.core.integration import IntegrationMember, IntegrationRecord
from multi_agent.core.path_patterns import path_matches
from multi_agent.core.review_input import ExecutedCommand, IntegrationReviewInput
from multi_agent.core.snapshot import FrozenSnapshot
from multi_agent.core.state import Disposition, RoundState, RoundStatus, round_status
from multi_agent.store import ReviewStore

from . import runner
from .prompt import INTEGRATION_CONFIRM_VERSION, INTEGRATION_REVIEW_VERSION
from .review_inputs import decode_review_input
from .snapshot import _path, freeze_repository
from .task_inputs import RULES_PATH


def _test_change_paths(
    source: FrozenSnapshot, commands: tuple[ExecutedCommand, ...]
) -> tuple[str, ...]:
    named_paths: set[str] = set()
    for command in commands:
        declared = cast(Mapping[str, Any], command.manifest)["command"]
        named_paths.update(
            PurePosixPath(argument).as_posix()
            for argument in (declared["executable"], *declared["args"])
        )
    baseline = {
        item.path: (item.oid, item.mode)
        for item in source.files
        if item.side == "baseline"
    }
    candidate = {
        item.path: (item.oid, item.mode)
        for item in source.files
        if item.side == "candidate"
    }
    highlighted = []
    for name in sorted(baseline.keys() | candidate.keys()):
        if baseline.get(name) == candidate.get(name):
            continue
        path = PurePosixPath(name)
        if (
            name in named_paths
            or name in {RULES_PATH, "conftest.py"}
            or name.startswith(".github/workflows/")
            or set(path.parts[:-1]) & {"checks", "test", "tests", "__tests__"}
            or path.stem.startswith("test_")
            or path.stem.endswith("_test")
            or ".test." in path.name
            or ".spec." in path.name
            or path.name in {"pyproject.toml", "pytest.ini", "tox.ini", "package.json"}
            or path.name.startswith(("pytest.", "jest.config.", "vitest.config."))
        ):
            highlighted.append(name)
    return tuple(highlighted)


def prepare_integration_review(
    record: IntegrationRecord,
    test_attempt: int,
    test_evidence_hash: str,
    commands: tuple[ExecutedCommand, ...],
) -> IntegrationReviewInput:
    """Freeze the integration diff, member contracts and qualified test evidence."""
    from .integration_cli import inspect_integration_candidate

    candidate = inspect_integration_candidate(record)
    source = freeze_repository(
        Path(record.repository),
        record.target_baseline,
        candidate.commit,
        "\n\n".join(
            f"[{member.task_input.card.id}]\n{member.task_input.requirements}"
            for member in record.members
        ),
        tuple(
            f"[{member.task_input.card.id}] {criterion}"
            for member in record.members
            for criterion in member.task_input.card.acceptance_criteria
        ),
    )
    value = IntegrationReviewInput(
        source=source,
        integration_id=record.integration_id,
        test_attempt=test_attempt,
        test_evidence_hash=test_evidence_hash,
        commands=commands,
        members=record.members,
        test_change_paths=_test_change_paths(source, commands),
    )
    decode_review_input(value.to_dict(), expected_id=value.snapshot_id)
    if inspect_integration_candidate(record) != candidate:
        raise ValueError("integration candidate changed while freezing review")
    return value


def _inspect_seat_executions(
    store: ReviewStore, round_id: str, state: RoundState, *, kind: str
) -> None:
    completed = {
        (envelope.seat, envelope.attempt): envelope.event.result
        for envelope, disposition in store.load_history(round_id)
        if disposition == Disposition.APPLIED and isinstance(envelope.event, Completed)
    }
    executions = cast(dict[str, Any], thaw_json(store.load_execution(round_id)))
    for seat in state.seats:
        current = seat.attempts[-1]
        receipt = executions.get(seat.config.key, {}).get(str(current.number), {})
        if (
            completed.get((seat.config.key, current.number)) != current.result
            or receipt.get("workspace_unchanged") is not True
            or "completion_unconfirmed" in receipt
        ):
            raise ValueError(
                f"integration {kind} seat {seat.config.key} has no qualified current execution"
            )


def _review_parent(
    store: ReviewStore,
    round_id: str,
    expected: IntegrationReviewInput,
) -> RoundState:
    state = store.load_round(round_id)
    metadata = runner.metadata_for(store, round_id)
    if (
        state.snapshot_id != expected.snapshot_id
        or state.prompt_template_version != INTEGRATION_REVIEW_VERSION
        or metadata["kind"] != "review"
        or metadata["parent_round_id"] is not None
    ):
        raise ValueError(
            "review does not refer to the current integration test evidence"
        )
    if round_status(state) != RoundStatus.READY_FOR_ASSESSMENT:
        raise ValueError("integration requires a complete executed review")
    _inspect_seat_executions(store, round_id, state, kind="review")
    return state


def _confirmation_state(
    store: ReviewStore, parent_id: str, parent: RoundState, confirmation_id: str
) -> RoundState:
    if store.confirmation_for(parent_id) != confirmation_id:
        raise ValueError(
            "confirmation does not belong to the original integration review"
        )
    child = store.load_round(confirmation_id)
    metadata = runner.metadata_for(store, confirmation_id)
    original = runner.metadata_for(store, parent_id)
    if (
        metadata["kind"] != "confirmation"
        or metadata["parent_round_id"] != parent_id
        or child.snapshot_id != parent.snapshot_id
        or child.prompt_template_version != INTEGRATION_CONFIRM_VERSION
        or tuple(seat.config for seat in child.seats)
        != runner.confirmation_seats(parent)
        or any(
            metadata[key] != original[key]
            for key in ("executables", "timeout", "grace")
        )
    ):
        raise ValueError("confirmation differs from the original integration review")
    return child


def check_integration_confirmation_link(
    store: ReviewStore,
    parent_id: str,
    expected: IntegrationReviewInput,
    confirmation_id: str,
) -> RoundState:
    """Verify which frozen confirmation round can be retried."""
    parent = _review_parent(store, parent_id, expected)
    return _confirmation_state(store, parent_id, parent, confirmation_id)


def inspect_integration_review(
    store: ReviewStore,
    round_id: str,
    expected: IntegrationReviewInput,
    *,
    require_ready: bool = True,
) -> dict[str, Any]:
    """Require a qualified review and any linked confirmation of the same evidence."""
    parent = _review_parent(store, round_id, expected)
    confirmation_id = store.confirmation_for(round_id)
    if confirmation_id is not None:
        child = _confirmation_state(store, round_id, parent, confirmation_id)
        if round_status(child) != RoundStatus.READY_FOR_ASSESSMENT:
            raise ValueError("integration requires a complete executed confirmation")
        _inspect_seat_executions(store, confirmation_id, child, kind="confirmation")
    view = runner.view_round(store, round_id)
    assessment = view["assessment"]
    if (
        assessment["status"] not in ("ready", "needs_rework", "user_decision")
        or not assessment["tests"]["executed_by_workbench"]
    ):
        raise ValueError("integration requires a complete executed review")
    if require_ready and (
        assessment["status"] != "ready" or assessment["tests"]["status"] != "passed"
    ):
        raise ValueError("integration requires passing tests and a ready review")
    return view


def _path_owners(record: IntegrationRecord, path: str) -> tuple[IntegrationMember, ...]:
    return tuple(
        member for member in record.members if path in member.delivery.changed_paths
    )


def check_rework_paths(
    record: IntegrationRecord,
    member: IntegrationMember,
    paths: Iterable[str],
    target_protected: tuple[str, ...] = (),
) -> None:
    """Authorize a member's paths against its card and the original integration."""
    if member not in record.members:
        raise ValueError("rework member is not frozen in the original integration")
    task = member.task_input
    protected = task.frozen_rules.rules.protected_paths + target_protected
    for path in paths:
        _path(path)
        other_owners = tuple(
            owner.task_input.card.id
            for owner in _path_owners(record, path)
            if owner != member
        )
        if other_owners:
            raise ValueError(
                f"rework path {path!r} is already delivered by other members "
                f"({', '.join(other_owners)})"
            )
        if any(path_matches(path, pattern) for pattern in protected):
            raise ValueError(f"rework path {path!r} is protected for {task.card.id}")
        if not any(path_matches(path, pattern) for pattern in task.card.write_scope):
            raise ValueError(
                f"rework path {path!r} is outside the writable scope of {task.card.id}"
            )


def select_rework_member(
    record: IntegrationRecord,
    review: dict[str, Any],
    *,
    target_protected: tuple[str, ...] = (),
) -> tuple[IntegrationMember, tuple[dict[str, Any], ...]]:
    """Require every blocking finding to authorize rework by the same member."""
    assessment = review["assessment"]
    if assessment["status"] != "needs_rework":
        reasons = ", ".join(assessment["reasons"]) or assessment["status"]
        raise ValueError(
            f"integration rework requires undisputed blocking findings ({reasons})"
        )
    findings = tuple(
        member
        for group in assessment["groups"]
        for member in group["members"]
        if member["finding"]["severity"] in ("P0", "P1")
    )
    selected = None
    for item in findings:
        finding = item["finding"]
        if finding["reference_status"] != "verified":
            raise ValueError("integration rework requires verified blocking references")
        path = _path(finding["path"])
        owners = _path_owners(record, path)
        if len(owners) != 1:
            possible = (
                ", ".join(member.task_input.card.id for member in owners) or "none"
            )
            raise ValueError(
                f"blocking path {path!r} has no unique member (candidates: {possible})"
            )
        owner = owners[0]
        if selected is not None and selected != owner:
            raise ValueError(
                "blocking findings require different members "
                f"({selected.task_input.card.id}, {owner.task_input.card.id})"
            )
        selected = owner
    if selected is None:
        raise ValueError("integration rework requires a located blocking finding")
    check_rework_paths(
        record,
        selected,
        (item["finding"]["path"] for item in findings),
        target_protected,
    )
    return selected, findings
