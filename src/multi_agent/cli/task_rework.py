"""Authorize one bounded rework from actual, current executed-review evidence."""

from typing import Any, cast

from multi_agent.core.events import thaw_json
from multi_agent.core.path_patterns import path_matches
from multi_agent.core.review_input import ExecutedReviewInput
from multi_agent.core.task_events import TaskReworkStarted
from multi_agent.core.task_execution import prepared_for_task, task_execution_hash
from multi_agent.core.task_rework import TaskReworkSource
from multi_agent.core.task_state import TaskInput
from multi_agent.store.planning_store import PlanningStore
from multi_agent.store.task_review_store import TaskReviewStore

from . import runner
from .review_inputs import load_review_input
from .snapshot import _path
from .task_review_inputs import inspect_task_review_source
from .workspaces import ExecutorLease


class ReworkRefused(ValueError):
    """A known policy decision, distinct from failed source verification."""

    def __init__(self, kind: str, detail: str) -> None:
        if kind not in ("user_decision", "budget_exhausted"):
            raise ValueError("unknown rework refusal kind")
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


def _frozen_input(
    store: PlanningStore, source: TaskReworkSource
) -> ExecutedReviewInput:
    """Read the bound immutable input; never re-authorize its old current source."""
    value = load_review_input(store, source.review_snapshot_id)
    if not isinstance(value, ExecutedReviewInput):
        raise TypeError("rework requires an executed review input")
    suite = value.executed_suite
    declared = suite.prepared.checkout.input
    if (
        declared.task_run_id != source.task_run_id
        or declared.implementation_attempt != source.implementation_attempt
        or task_execution_hash(suite.task_input) != source.task_input_hash
        or declared.candidate.delivery.candidate != source.candidate_commit
        or task_execution_hash(declared.candidate) != source.candidate_hash
        or suite.test_attempt != source.test_attempt
    ):
        raise ValueError("rework source differs from its frozen review input")
    return value


def _actionable(assessment: dict[str, Any], value: TaskInput) -> None:
    if assessment["status"] != "needs_rework":
        raise ReworkRefused(
            "user_decision", "review does not identify undisputed actionable rework"
        )
    blockers = [
        finding
        for report in assessment["reports"].values()
        for finding in report["findings"]
        if finding["severity"] in ("P0", "P1")
    ]
    if not blockers:
        raise ReworkRefused(
            "user_decision", "rework requires a located, verified blocking finding"
        )
    for finding in blockers:
        if finding["reference_status"] != "verified":
            raise ReworkRefused("user_decision", "blocking reference is unverified")
        try:
            path = _path(finding["path"])
        except ValueError as error:
            raise ReworkRefused(
                "user_decision", "blocking finding has no safe literal scope"
            ) from error
        if not any(
            path_matches(path, pattern) for pattern in value.card.write_scope
        ) or any(
            path_matches(path, pattern)
            for pattern in value.frozen_rules.rules.protected_paths
        ):
            raise ReworkRefused(
                "user_decision",
                "blocking finding is outside the original writable scope",
            )


def prepare_task_rework_source(
    store: TaskReviewStore, review_round_id: str, lease: ExecutorLease
) -> TaskReworkSource:
    """Freeze one exact authorization while the caller holds both controller locks."""
    from .task_context import _check_lease, _store_database

    _check_lease(lease)
    database = _store_database(store)
    checkpoint = store.load_task_rework_checkpoint(review_round_id)
    checkpoint_value = cast(dict[str, Any], thaw_json(checkpoint))
    record = checkpoint_value["review"]
    confirmation = checkpoint_value["confirmation"]
    state = store.load_round(review_round_id)
    metadata = runner.metadata_for(store, review_round_id)
    if (
        metadata["kind"] != "review"
        or metadata["parent_round_id"] is not None
        or record["parent_round_id"] is not None
        or record["input_snapshot_id"] != state.snapshot_id
        or record["state"]["snapshot_id"] != state.snapshot_id
        or confirmation is not None
        and (
            confirmation["parent_round_id"] != review_round_id
            or confirmation["input_snapshot_id"] != state.snapshot_id
            or confirmation["state"]["snapshot_id"] != state.snapshot_id
            or confirmation["metadata"]["kind"] != "confirmation"
        )
    ):
        raise ValueError("rework source must be the exact parent review round")
    value = load_review_input(store, state.snapshot_id)
    if not isinstance(value, ExecutedReviewInput):
        raise TypeError("rework requires an executed review input")
    declared = value.executed_suite.prepared.checkout.input
    run_id = declared.task_run_id
    implementation = store.load_task(run_id)
    task_input = store.load_task_input(run_id)
    history = store.load_task_history(run_id)
    tests, test_history = store.load_task_test_checkpoint(
        run_id, declared.implementation_attempt
    )
    if any(
        isinstance(item.request, TaskReworkStarted) for item in implementation.attempts
    ):
        raise ReworkRefused(
            "user_decision", "the task's one rework reservation is already used"
        )
    current = implementation.current_attempt
    prepared = prepared_for_task(current) if current is not None else None
    if current is None or prepared is None:
        raise ValueError("rework requires the current managed implementation")
    if len(implementation.attempts) >= prepared.policy.max_attempts:
        raise ReworkRefused(
            "budget_exhausted", "the frozen implementation attempt budget is exhausted"
        )
    inspect_task_review_source(store, value, lease)
    assessment = runner.view_round(store, review_round_id)["assessment"]
    if not isinstance(assessment, dict):
        raise TypeError("parent review has no assessment")
    _actionable(assessment, task_input)
    if (
        current.number != declared.implementation_attempt
        or not implementation.candidate_ready
        or implementation.current_candidate != declared.candidate
        or task_execution_hash(task_input)
        != task_execution_hash(value.executed_suite.task_input)
        or tests is None
        or tests.current_attempt is None
        or tests.current_attempt.number != value.executed_suite.test_attempt
    ):
        raise ValueError("rework source does not identify the current task and test")
    # Do not refresh any captured checkpoint after the filesystem/assessment reads.
    if (
        task_execution_hash(store.load_task(run_id))
        != task_execution_hash(implementation)
        or task_execution_hash(store.load_task_input(run_id))
        != task_execution_hash(task_input)
        or task_execution_hash(store.load_task_history(run_id))
        != task_execution_hash(history)
        or task_execution_hash(store.load_task_test_checkpoint(run_id, current.number))
        != task_execution_hash((tests, test_history))
        or task_execution_hash(store.load_task_rework_checkpoint(review_round_id))
        != task_execution_hash(checkpoint)
    ):
        raise ValueError("rework source changed during authorization")
    _check_lease(lease)
    database.check()
    return TaskReworkSource(
        task_run_id=run_id,
        implementation_attempt=current.number,
        task_input_hash=task_execution_hash(task_input),
        candidate_commit=declared.candidate.delivery.candidate,
        candidate_hash=task_execution_hash(declared.candidate),
        task_history_length=len(history),
        task_history_hash=task_execution_hash(history),
        test_attempt=value.executed_suite.test_attempt,
        test_history_length=len(test_history),
        test_history_hash=task_execution_hash(test_history),
        review_round_id=review_round_id,
        review_snapshot_id=value.snapshot_id,
        review_checkpoint_hash=task_execution_hash(checkpoint),
        assessment=assessment,
    )
