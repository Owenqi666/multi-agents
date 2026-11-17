"""Continue a settled task review through at most one existing rework."""

import argparse
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from multi_agent.core import SeatConfig
from multi_agent.core.events import thaw_json
from multi_agent.core.review_input import ExecutedReviewInput
from multi_agent.core.task_events import TaskReworkStarted
from multi_agent.core.task_execution import prepared_for_task, task_execution_hash
from multi_agent.core.task_test_state import TaskTestPhase, TaskTestState
from multi_agent.store.task_review_store import TaskReviewStore

from . import runner
from .task_cli import _interruptible, _rework
from .task_context import _database
from .task_review_cli import (
    _automatic_selection,
    _create_review,
    _preserve_interruption,
    view_task_review,
)
from .task_rework import _frozen_input
from .task_test_cli import view_task_test
from .task_test_runner import execute_task_tests


@dataclass(frozen=True, slots=True)
class _Configuration:
    value: ExecutedReviewInput
    repository: Path
    seats: tuple[SeatConfig, ...]
    timeout: float
    grace: float
    executables: dict[str, str]


def _configuration(
    database: Path,
    parent_id: str,
    task_run_id: str,
    implementation_attempt: int,
) -> _Configuration:
    """Inherit from the checkpoint actually reserved by this rework."""
    with TaskReviewStore.open_readonly(database) as store:
        state = store.load_task(task_run_id)
        current = state.current_attempt
        if (
            current is None
            or current.number != implementation_attempt
            or not isinstance(current.request, TaskReworkStarted)
            or not state.candidate_ready
        ):
            raise ValueError(
                "automatic continuation requires its current rework candidate"
            )
        source = current.request.source
        if source.task_run_id != task_run_id or source.review_round_id != parent_id:
            raise ValueError("automatic continuation reserved another review source")
        checkpoint = store.load_task_rework_checkpoint(parent_id)
        if task_execution_hash(checkpoint) != source.review_checkpoint_hash:
            raise ValueError(
                "reserved review configuration changed before continuation"
            )
        record = cast(dict[str, Any], thaw_json(checkpoint))["review"]
        metadata = record["metadata"]
        _automatic_selection(metadata, None)
        value = _frozen_input(store, source)
        prepared = prepared_for_task(current)
        if (
            prepared is None
            or metadata["repository"] != prepared.workspace.source
            or prepared.workspace.repository != value.source.repository
            or record["input_snapshot_id"] != source.review_snapshot_id
            or record["state"]["snapshot_id"] != source.review_snapshot_id
            or record["parent_round_id"] is not None
            or metadata["kind"] != "review"
        ):
            raise ValueError(
                "reserved review configuration has inconsistent provenance"
            )
        seats = tuple(SeatConfig(**seat["config"]) for seat in record["state"]["seats"])
        executables = dict(metadata["executables"])
        runner.validate_review_settings(
            seats, timeout=metadata["timeout"], grace=metadata["grace"]
        )
        return _Configuration(
            value,
            Path(prepared.workspace.source),
            seats,
            metadata["timeout"],
            metadata["grace"],
            executables,
        )


def _enabled(database: Path, parent_id: str, override: bool | None) -> bool:
    with TaskReviewStore.open_readonly(database) as store:
        metadata = runner.metadata_for(store, parent_id)
    return _automatic_selection(metadata, override)


def _complete_tests(state: TaskTestState, result: dict[str, Any]) -> bool:
    current = state.current_attempt
    return bool(
        current is not None
        and current.finished
        and not current.cancel_requested
        and not current.problems
        and not any(command.problems for command in current.commands)
        and state.phase in (TaskTestPhase.SUCCEEDED, TaskTestPhase.FAILED)
        and result["task_run_id"] == state.task_run_id
        and result["implementation_attempt"] == state.implementation_attempt
        and result["test_attempt"] == current.number
        and result["candidate"] == current.input.candidate.delivery.candidate
        and result["source_current"]
        and result["verification"] == "verified"
        and result["details"]["is_current"]
    )


def _finish(
    result: dict[str, Any],
    workflow: dict[str, Any],
    outcome: str,
    reason: str,
    code: int,
) -> tuple[dict[str, Any], int]:
    return {
        **result,
        "workflow": {
            **workflow,
            "outcome": outcome,
            "reason": reason,
        },
    }, code


def continue_task_review(
    database_path: str | Path,
    result: dict[str, Any],
    interrupted: bool,
    override: bool | None = None,
) -> tuple[dict[str, Any], int]:
    """Called only after an execution entry has released its review locks.

    Existing controllers retain ownership, cancellation and atomic reservation.
    A new review runs once through the single-round helper, never this function.
    """
    parent_id = result["assessment_round_id"]
    workflow: dict[str, Any] = {
        "source_review_round_id": parent_id,
        "enabled": override
        if override is not None
        else result["metadata"].get("automatic_rework", False),
        "stage": "initial_review",
        "outcome": "interrupted" if interrupted else "not_needed",
        "reason": "",
        "rework": None,
        "tests": None,
        "final_review_round_id": None,
    }
    if interrupted:
        return _finish(
            result, workflow, "interrupted", "Review execution was interrupted.", 130
        )
    database = Path(database_path)
    directory: Path | None = None
    try:
        database = _database(database).path
        workflow["enabled"] = _enabled(database, parent_id, override)
        if not workflow["enabled"]:
            return _finish(
                result,
                workflow,
                "disabled",
                "Automatic rework is disabled for this invocation.",
                0 if result["current_eligible"] else 1,
            )
        if result["current_eligible"]:
            return _finish(
                result, workflow, "not_needed", "The current candidate is ready.", 0
            )
        if (
            not result["source_current"]
            or result["verification"] != "verified"
            or result["assessment"]["status"] != "needs_rework"
        ):
            return _finish(
                result,
                workflow,
                "refused",
                "The current review has no complete verified rework assessment; inspect its source errors and assessment reasons.",
                1,
            )
        workflow["stage"] = "rework"
        directory = database.parent / f"automatic-rework-{uuid4().hex}"
        rework, code = _rework(
            argparse.Namespace(
                database=database, review_round_id=parent_id, directory=directory
            )
        )
        workflow["rework"] = rework
        # The original candidate is now historical if a new attempt was reserved.
        with _preserve_interruption(code == 130):
            original = view_task_review(database, result["round_id"])
        if code == 130:
            return _finish(
                original,
                workflow,
                "interrupted",
                "Rework was interrupted after settlement.",
                130,
            )
        if code != 0:
            outcome = (
                "refused"
                if rework["implementation_attempt"] == result["implementation_attempt"]
                else "stopped"
            )
            return _finish(
                original, workflow, outcome, rework["next_action"]["detail"], 1
            )
        task_run_id = rework["task_run_id"]
        implementation_attempt = rework["implementation_attempt"]
        configuration = _configuration(
            database, parent_id, task_run_id, implementation_attempt
        )
        declared = configuration.value.executed_suite.prepared.checkout.input
        workflow["stage"] = "tests"
        test_directory = database.parent / f"automatic-tests-{uuid4().hex}"
        directory = test_directory

        async def execute(stop: asyncio.Event) -> TaskTestState:
            return await execute_task_tests(
                database,
                task_run_id,
                implementation_attempt,
                repository=configuration.repository,
                directory=test_directory,
                required_commands=tuple(command.name for command in declared.commands),
                declaration_source=declared.declaration_source,
                policy=configuration.value.executed_suite.prepared.policy,
                stop=stop,
            )

        tests, tests_interrupted = asyncio.run(_interruptible(execute))
        with _preserve_interruption(tests_interrupted):
            tested = view_task_test(database, task_run_id, implementation_attempt)
        workflow["tests"] = tested
        if tests_interrupted:
            return _finish(
                original,
                workflow,
                "interrupted",
                "Testing was interrupted after settlement.",
                130,
            )
        if not _complete_tests(tests, tested):
            return _finish(
                original,
                workflow,
                "stopped",
                "The new test suite is incomplete, cancelled or unverified; no new review was started.",
                1,
            )
        workflow["stage"] = "review"
        directory = database.parent / f"automatic-review-{uuid4().hex}"
        reviewed, review_interrupted = _create_review(
            database,
            task_run_id,
            implementation_attempt,
            tested["test_attempt"],
            configuration.repository,
            directory,
            configuration.seats,
            timeout=configuration.timeout,
            grace=configuration.grace,
            executables=configuration.executables,
            automatic_rework=False,
            rework_from_round_id=parent_id,
        )
        workflow["final_review_round_id"] = reviewed["round_id"]
        if review_interrupted:
            return _finish(
                reviewed,
                workflow,
                "interrupted",
                "The new review was interrupted after settlement.",
                130,
            )
        if (
            reviewed["assessment"]["status"] == "incomplete"
            or not reviewed["source_current"]
        ):
            return _finish(
                reviewed,
                workflow,
                "stopped",
                "The new review is incomplete or its source is unverified.",
                1,
            )
        return _finish(
            reviewed,
            workflow,
            "completed",
            "One rework and complete retesting and review finished; no further automatic rework is allowed.",
            0 if reviewed["current_eligible"] else 1,
        )
    except BaseException as error:
        error.add_note(
            f"Automatic review continuation retained: source review {parent_id}; stage {workflow['stage']}; database {database}; directory {directory}."
        )
        raise
