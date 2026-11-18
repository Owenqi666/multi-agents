"""Freeze and verify a review of the exact tested integration candidate."""

from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any, cast

from multi_agent.core.events import Completed
from multi_agent.core.integration import IntegrationRecord
from multi_agent.core.review_input import ExecutedCommand, IntegrationReviewInput
from multi_agent.core.snapshot import FrozenSnapshot
from multi_agent.core.state import Disposition, RoundStatus
from multi_agent.store import ReviewStore

from . import runner
from .prompt import INTEGRATION_REVIEW_VERSION
from .review_inputs import decode_review_input
from .snapshot import freeze_repository
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


def inspect_integration_review(
    store: ReviewStore,
    round_id: str,
    expected: IntegrationReviewInput,
    *,
    require_ready: bool = True,
) -> dict[str, Any]:
    """Require a ready review of the current integration test evidence."""
    state = store.load_round(round_id)
    if (
        state.snapshot_id != expected.snapshot_id
        or state.prompt_template_version != INTEGRATION_REVIEW_VERSION
        or runner.metadata_for(store, round_id)["kind"] != "review"
    ):
        raise ValueError(
            "review does not refer to the current integration test evidence"
        )
    view = runner.view_round(store, round_id)
    assessment = view["assessment"]
    if (
        view["status"] != RoundStatus.READY_FOR_ASSESSMENT
        or assessment["status"] not in ("ready", "needs_rework", "user_decision")
        or not assessment["tests"]["executed_by_workbench"]
    ):
        raise ValueError("integration requires a complete executed review")
    if require_ready and (
        assessment["status"] != "ready" or assessment["tests"]["status"] != "passed"
    ):
        raise ValueError("integration requires passing tests and a ready review")
    completed = {
        (envelope.seat, envelope.attempt): envelope.event.result
        for envelope, disposition in store.load_history(round_id)
        if disposition == Disposition.APPLIED and isinstance(envelope.event, Completed)
    }
    executions = cast(dict[str, Any], view["execution"])
    for seat in state.seats:
        current = seat.attempts[-1]
        receipt = executions.get(seat.config.key, {}).get(str(current.number), {})
        if (
            completed.get((seat.config.key, current.number)) != current.result
            or receipt.get("workspace_unchanged") is not True
            or "completion_unconfirmed" in receipt
        ):
            raise ValueError(
                f"integration review seat {seat.config.key} has no qualified current execution"
            )
    return view
