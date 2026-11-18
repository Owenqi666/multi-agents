"""Explicit planning, implementation and one-rework CLI operations."""

import argparse
import asyncio
import json
import shutil
import signal
import sqlite3
import sys
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from multi_agent.adapters.runtime_decode import RuntimeFailure
from multi_agent.core.dispatch import ImplementationChoice, resolve_dispatch
from multi_agent.core.events import FailureCategory, thaw_json
from multi_agent.core.path_patterns import patterns_intersect
from multi_agent.core.planning import (
    DependencyReceipt,
    PlanningInput,
    PlanningPhase,
    PlanningRequest,
    PlanningState,
    UserDecision,
)
from multi_agent.core.planning_execution import prepared_attempts
from multi_agent.core.task_events import TaskReworkStarted
from multi_agent.core.task_execution import (
    TaskDispatchPolicy,
    TaskExecutionPolicy,
    prepared_for_task,
    task_execution_value,
)
from multi_agent.core.task_state import TaskPhase, TaskState
from multi_agent.core.tasks import TaskCard
from multi_agent.store.task_review_store import TaskReviewStore

from .planning_context import capture_planning_policy
from .planning_evidence import bind_planning_task, inspect_planning_execution
from .planning_inputs import validate_planning_input
from .planning_runner import execute_planning_attempt
from .review_inputs import load_review_input
from .task_candidates import freeze_task_candidate, inspect_task_candidate
from .task_context import (
    _canonical,
    _database,
    _directory,
    _dispatch,
    _new_root,
    _outside,
    capture_task_policy,
    prepare_task_batch,
)
from .task_inputs import (
    _input_text,
    _unique_json,
    freeze_project_rules,
    validate_task_graph,
)
from .task_review_cli import _preserve_interruption, view_task_review
from .task_rework import ReworkRefused
from .task_runner import execute_task_attempt
from .task_test_cli import _existing_lease, view_task_test
from .workspaces import ExecutorLease, executor_lock

_STRATEGY = (
    "implementation",
    "fallback",
    "task_type",
    "max_attempts",
    "timeout",
    "grace",
    "codex_bin",
    "claude_bin",
)
_PLAN_INPUTS = (
    "repository",
    "baseline",
    "requirements",
    "directory",
    "declarations",
    "draft",
    "plan_review",
    "max_attempts",
    "timeout",
    "grace",
    "codex_bin",
    "claude_bin",
)
_ERRORS = (OSError, ValueError, TypeError, KeyError, sqlite3.Error, RuntimeError)


def _settings(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--max-attempts", type=int)
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--grace", type=float)
    parser.add_argument("--codex-bin")
    parser.add_argument("--claude-bin")


def _implementation_settings(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument(
        "--implementation", nargs=3, metavar=("PROVIDER", "MODEL", "EFFORT")
    )
    parser.add_argument(
        "--fallback", nargs=3, action="append", metavar=("PROVIDER", "MODEL", "EFFORT")
    )
    parser.add_argument("--task-type")
    _settings(parser)


def add_parser(commands: Any) -> None:
    command = commands.add_parser("task", help="plan, implement and rework a task")
    operations = command.add_subparsers(dest="task_operation", required=True)
    plan = operations.add_parser("plan")
    plan.add_argument("--database", type=Path, required=True)
    plan.add_argument("--repository", type=Path)
    plan.add_argument("--baseline")
    plan.add_argument("--requirements", type=Path)
    plan.add_argument("--directory", type=Path)
    plan.add_argument("--declarations", type=Path)
    plan.add_argument("--draft", nargs=2, metavar=("MODEL", "EFFORT"))
    plan.add_argument("--plan-review", nargs=2, metavar=("MODEL", "EFFORT"))
    plan.add_argument("--retry", metavar="PLAN_RUN_ID")
    _settings(plan)
    run = operations.add_parser("run")
    run.add_argument("planning_run_id")
    _implementation_settings(run)
    run.add_argument("--retry", action="store_true")
    batch = operations.add_parser(
        "run-batch",
        help="run independent ready plans together",
        description=(
            "Run two or more ready plans from the same baseline. "
            "Each task gets a fresh path of the form DIRECTORY-TASK_ID "
            "next to the database."
        ),
    )
    batch.add_argument("planning_run_ids", nargs="+", metavar="PLAN_RUN_ID")
    _implementation_settings(batch)
    rework = operations.add_parser("rework")
    rework.add_argument("review_round_id")
    rework.add_argument("--database", type=Path, required=True)
    rework.add_argument("--directory", type=Path, required=True)
    show = operations.add_parser("show")
    show.add_argument("--database", type=Path, required=True)
    selected = show.add_mutually_exclusive_group(required=True)
    selected.add_argument("--plan", dest="planning_run_id")
    selected.add_argument("--task", dest="task_run_id")
    show.add_argument("--history", action="store_true")


def _next(kind: str, detail: str, **coordinates: Any) -> dict[str, Any]:
    return {
        "kind": kind,
        "planning_run_id": None,
        "task_run_id": None,
        "implementation_attempt": None,
        "test_attempt": None,
        "review_round_id": None,
        "detail": detail,
        **coordinates,
    }


def _base(operation: str, **values: Any) -> dict[str, Any]:
    return {
        "operation": operation,
        "planning_run_id": None,
        "task_run_id": None,
        "phase": None,
        "implementation_attempt": None,
        "candidate": None,
        "verification": "unverified",
        "next_action": _next("user_decision", "Inspect the recorded evidence."),
        "details": {},
        **values,
    }


def _refusal(
    value: dict[str, Any], operation: str, kind: str, detail: str, **source: Any
) -> tuple[dict[str, Any], int]:
    value.update(
        operation=operation,
        next_action=_next(
            kind,
            detail,
            planning_run_id=value["planning_run_id"],
            task_run_id=value["task_run_id"],
            implementation_attempt=value["implementation_attempt"],
            review_round_id=source.get("source_review_round_id"),
        ),
        **source,
    )
    return value, 1


def _plan_view(
    store: TaskReviewStore, identifier: str, history: bool
) -> dict[str, Any]:
    state = store.load_planning(identifier)
    records = prepared_attempts(state)
    budget = None
    if records:
        budget = {
            provider: {
                "limit": records[0].policy.for_provider(provider).max_attempts,
                "used": len(state.attempts(stage)),
                "remaining": max(
                    0,
                    records[0].policy.for_provider(provider).max_attempts
                    - len(state.attempts(stage)),
                ),
            }
            for provider, stage in (("claude", "draft"), ("codex", "review"))
        }
    errors = []
    qualified = True
    for stage in ("draft", "review"):
        current = state.current(stage)
        if current is None:
            qualified = False
            continue
        try:
            if not inspect_planning_execution(
                store, identifier, stage, current.number
            ).qualified:
                qualified = False
        except _ERRORS as error:
            errors.append(str(error))
            qualified = False
    if state.phase in (PlanningPhase.READY, PlanningPhase.BOUND):
        next_kind = "run" if state.bound_task_run_id is None else "user_decision"
        detail = (
            "The reviewed plan is ready for implementation."
            if qualified
            else "Planning execution evidence is unverified."
        )
        if not qualified:
            next_kind = "user_decision"
    elif state.phase in (
        PlanningPhase.AWAITING_REVIEW,
        PlanningPhase.REVISION_REQUIRED,
        PlanningPhase.FAILED,
        PlanningPhase.CANCELLED,
    ):
        next_kind, detail = (
            "plan_retry",
            "An explicit retry rechecks stopping facts and the frozen budget.",
        )
    else:
        next_kind, detail = (
            "reconciliation_required",
            "No automatic recovery or adoption of a previous runtime.",
        )
    result = _base(
        "show",
        planning_run_id=identifier,
        task_run_id=state.bound_task_run_id,
        phase=state.phase,
        verification="verified" if qualified else "unverified",
        card=task_execution_value(state.card),
        questions=task_execution_value(state.card.open_questions) if state.card else [],
        attempts={
            stage: task_execution_value(state.attempts(stage))
            for stage in ("draft", "review")
        },
        budget=budget,
        details={
            "input": task_execution_value(state.planning_input),
            "validation_error": state.validation_error,
            "source_errors": errors,
        },
        next_action=_next(
            next_kind,
            detail,
            planning_run_id=identifier,
            task_run_id=state.bound_task_run_id,
        ),
    )
    if history:
        result["history"] = task_execution_value(
            store.load_planning_history(identifier)
        )
    return result


def _task_view(
    store: TaskReviewStore, database: Path, identifier: str, history: bool
) -> dict[str, Any]:
    state = store.load_task(identifier)
    planning_id = store.planning_run_for_task(identifier)
    current = state.current_attempt
    prepared = prepared_for_task(current) if current is not None else None
    rework_used = any(
        isinstance(attempt.request, TaskReworkStarted) for attempt in state.attempts
    )
    budget = (
        None
        if prepared is None
        else {
            "limit": prepared.policy.max_attempts,
            "used": len(state.attempts),
            "remaining": max(0, prepared.policy.max_attempts - len(state.attempts)),
        }
    )
    candidate = state.current_candidate
    errors: list[str] = []
    verified = False
    if current is not None and candidate is not None:
        try:
            with _existing_lease(store, identifier, current.number) as lease:
                inspect_task_candidate(store, identifier, current.number, lease)
            verified = True
        except _ERRORS as error:
            errors.append(str(error))
    tests = [
        view_task_test(database, run_id, attempt)
        for run_id, attempt in store.list_task_test_stages()
        if run_id == identifier
    ]
    reviews = []
    for round_id in store.list_reviews():
        review = store.load_round(round_id)
        raw = thaw_json(store.load_snapshot(review.snapshot_id))
        if not isinstance(raw, dict) or raw.get("kind") != "executed_task_review":
            continue
        payload = cast(dict[str, Any], raw)
        if (
            payload["executed_suite"]["prepared"]["checkout"]["input"]["task_run_id"]
            == identifier
        ):
            reviews.append(view_task_review(database, round_id, history=history))
    coordinates = {
        "planning_run_id": planning_id,
        "task_run_id": identifier,
        "implementation_attempt": current.number if current is not None else None,
    }
    if state.phase == TaskPhase.READY:
        action = _next("run", "Start the first implementation attempt.", **coordinates)
    elif state.phase in (TaskPhase.FAILED, TaskPhase.CANCELLED):
        if rework_used:
            action = _next(
                "user_decision",
                "The one rework was consumed; no further implementation retry.",
                **coordinates,
            )
        elif budget is not None and budget["remaining"] == 0:
            action = _next(
                "budget_exhausted",
                "The original implementation budget is exhausted.",
                **coordinates,
            )
        else:
            action = _next(
                "run",
                "Explicit --retry needs actual stopping evidence and a fresh directory.",
                **coordinates,
            )
    elif verified:
        assert current is not None
        current_reviews = [
            item
            for item in reviews
            if item["implementation_attempt"] == current.number
            and item["metadata"]["kind"] == "review"
        ]
        current_tests = [
            item for item in tests if item["implementation_attempt"] == current.number
        ]
        if current_reviews:
            action = _next(
                "user_decision",
                "Inspect the explicitly selected review round; acceptance and merge remain user actions.",
                **coordinates,
            )
            if len(current_reviews) == 1 and current_reviews[0]["source_current"]:
                reviewed = current_reviews[0]
                if (
                    reviewed["assessment"]["status"] == "needs_rework"
                    and not rework_used
                ):
                    kind = (
                        "budget_exhausted"
                        if budget is not None and budget["remaining"] == 0
                        else "rework"
                    )
                    action = _next(
                        kind,
                        "Rework rechecks the original scope and complete review evidence.",
                        review_round_id=reviewed["round_id"],
                        **coordinates,
                    )
        elif current_tests:
            tested = current_tests[-1]
            complete = (
                tested["verification"] == "verified"
                and tested["details"]["finished"]
                and not tested["details"]["cancel_requested"]
                and tested["phase"] in ("succeeded", "failed")
            )
            action = _next(
                "task_review" if complete else "reconciliation_required",
                "Select reviewers for this candidate and its complete current test suite."
                if complete
                else "Current testing is incomplete or unverified; inspect its stopping facts before an explicit retry.",
                test_attempt=tested["test_attempt"],
                **coordinates,
            )
        else:
            action = _next(
                "task_test",
                "Explicitly declare and execute the complete named test suite for this candidate.",
                **coordinates,
            )
    else:
        action = _next(
            "user_decision"
            if state.phase == TaskPhase.PLANNING_REQUIRED
            else "reconciliation_required",
            "Inspect retained evidence; do not replay an old prepared runtime or adopt a partial candidate.",
            **coordinates,
        )
    result = _base(
        "show",
        **coordinates,
        phase=state.phase,
        candidate=candidate.delivery.candidate if candidate else None,
        verification="verified" if verified else "unverified",
        attempts=task_execution_value(state.attempts),
        tests=tests,
        reviews=reviews,
        budget=budget,
        rework_used=rework_used,
        next_action=action,
        details={
            "input": task_execution_value(store.load_task_input(identifier)),
            "scope_block": task_execution_value(state.scope_block),
            "source_errors": errors,
        },
    )
    if history:
        result["history"] = task_execution_value(store.load_task_history(identifier))
    return result


def view_task(
    database_path: str | Path,
    *,
    planning_run_id: str | None = None,
    task_run_id: str | None = None,
    history: bool = False,
) -> dict[str, Any]:
    if (planning_run_id is None) == (task_run_id is None):
        raise ValueError("select exactly one planning or task run")
    database = Path(database_path).expanduser().absolute()
    with TaskReviewStore.open_readonly(database) as store:
        if planning_run_id is not None:
            return _plan_view(store, planning_run_id, history)
        assert task_run_id is not None
        return _task_view(store, database, task_run_id, history)


def _program(value: str | None, default: str) -> str:
    selected = default if value is None else value
    if not selected or "\0" in selected:
        raise ValueError("executable must be a nonempty program or path")
    path = shutil.which(selected)
    if path is None:
        raise ValueError(f"executable not found: {selected}")
    return str(Path(path).resolve(strict=True))


def _required(args: argparse.Namespace, fields: tuple[str, ...]) -> None:
    missing = [field for field in fields if getattr(args, field) is None]
    if missing:
        raise ValueError(f"this operation requires: {', '.join(missing)}")


def _unchanged(args: argparse.Namespace, fields: tuple[str, ...]) -> None:
    if any(getattr(args, field) is not None for field in fields):
        raise ValueError("retry cannot change frozen inputs, models or policy")


def _declarations(
    path: Path | None,
) -> tuple[tuple[DependencyReceipt, ...], tuple[UserDecision, ...]]:
    if path is None:
        return (), ()
    value = json.loads(
        _input_text(path.read_bytes().decode("utf-8")), object_pairs_hook=_unique_json
    )
    if not isinstance(value, dict) or set(value) != {"dependencies", "decisions"}:
        raise ValueError("declarations require exactly dependencies and decisions")
    expected = {
        "dependencies": {"task_id", "baseline_commit", "source", "detail"},
        "decisions": {"question", "resolution", "source", "detail"},
    }
    for name, fields in expected.items():
        if not isinstance(value[name], list) or any(
            not isinstance(item, dict) or set(item) != fields for item in value[name]
        ):
            raise ValueError(f"invalid {name} declaration fields")
    return (
        tuple(DependencyReceipt(**item) for item in value["dependencies"]),
        tuple(UserDecision(**item) for item in value["decisions"]),
    )


async def _interruptible[T](
    operation: Callable[[asyncio.Event], Awaitable[T]],
) -> tuple[T, bool]:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous = signal.getsignal(signal.SIGINT)
    loop.add_signal_handler(signal.SIGINT, stop.set)
    try:
        value = await operation(stop)
        return value, stop.is_set()
    finally:
        loop.remove_signal_handler(signal.SIGINT)
        signal.signal(signal.SIGINT, previous)


def _plan(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    first = args.retry is None
    policy = None
    if first:
        _required(args, ("repository", "baseline", "requirements", "directory"))
        dependencies, decisions = _declarations(args.declarations)
        value = PlanningInput(
            args.requirements.read_bytes().decode("utf-8"),
            freeze_project_rules(args.repository, args.baseline),
            PlanningRequest("claude", *(args.draft or ("claude-opus-5-5", "high"))),
            PlanningRequest("codex", *(args.plan_review or ("gpt-6.1-sol", "high"))),
            dependencies,
            decisions,
        )
        validate_planning_input(value)
        limit = 2 if args.max_attempts is None else args.max_attempts
        policy = capture_planning_policy(
            {
                provider: _program(getattr(args, provider + "_bin"), provider)
                for provider in ("claude", "codex")
            },
            {"claude": limit, "codex": limit},
            180 if args.timeout is None else args.timeout,
            2 if args.grace is None else args.grace,
        )
        database = _canonical(args.database, existing=False)
        _directory(database.parent)
        with executor_lock(args.repository) as lease:
            _outside(database, lease)
            with TaskReviewStore(database) as store:
                _new_root(args.directory, _database(database), lease)
                identifier = uuid4().hex
                store.create_planning(identifier, value, datetime.now(UTC).isoformat())
        stage = "draft"
    else:
        _unchanged(args, _PLAN_INPUTS)
        database = _database(args.database).path
        identifier = args.retry
        with TaskReviewStore.open_readonly(database) as store:
            state = store.load_planning(identifier)
            records = prepared_attempts(state)
        if state.phase in (PlanningPhase.READY, PlanningPhase.BOUND):
            result = view_task(database, planning_run_id=identifier)
            result["operation"] = "plan"
            return result, 0 if result["verification"] == "verified" else 1
        if not records:
            raise ValueError(
                "first planning preparation is incomplete; create a new planning run"
            )
        if state.phase == PlanningPhase.AWAITING_REVIEW:
            stage = "review"
        elif state.phase == PlanningPhase.REVISION_REQUIRED:
            stage = "draft"
        elif state.phase in (PlanningPhase.FAILED, PlanningPhase.CANCELLED):
            draft = state.current("draft")
            stage = (
                "review"
                if state.stage == "review" and draft is not None and draft.usable
                else "draft"
            )
        else:
            raise ValueError(
                "planning execution is incomplete; stopping facts must be reconciled"
            )
        provider = "claude" if stage == "draft" else "codex"
        if (
            len(state.attempts(stage))
            >= records[0].policy.for_provider(provider).max_attempts
        ):
            result = view_task(database, planning_run_id=identifier)
            result.update(
                operation="plan",
                next_action=_next(
                    "budget_exhausted",
                    "The original planning budget is exhausted.",
                    planning_run_id=identifier,
                ),
            )
            return result, 1

    async def execute(stop: asyncio.Event) -> tuple[PlanningState, bool]:
        state = await execute_planning_attempt(
            database,
            identifier,
            stage,
            repository=args.repository if first else None,
            directory=args.directory if first else None,
            policy=policy,
            stop=stop,
        )
        if (
            stage == "draft"
            and state.phase == PlanningPhase.AWAITING_REVIEW
            and not stop.is_set()
        ):
            records = prepared_attempts(state)
            if (
                len(state.review_attempts)
                >= records[0].policy.for_provider("codex").max_attempts
            ):
                return state, True
            state = await execute_planning_attempt(
                database, identifier, "review", stop=stop
            )
        return state, False

    try:
        (state, budget_exhausted), interrupted = asyncio.run(_interruptible(execute))
    except BaseException as error:
        error.add_note(f"Planning run retained: {identifier}; database: {database}")
        raise
    result = view_task(database, planning_run_id=identifier)
    result["operation"] = "plan"
    if budget_exhausted:
        result["next_action"] = _next(
            "budget_exhausted",
            "The frozen review budget is exhausted; the completed draft is retained.",
            planning_run_id=identifier,
        )
    return (
        result,
        130
        if interrupted
        else 0
        if state.phase == PlanningPhase.READY and result["verification"] == "verified"
        else 1,
    )


def _unavailable(state: TaskState) -> frozenset[tuple[str, str]]:
    result: set[tuple[str, str]] = set()
    for attempt in state.attempts:
        result.update(attempt.request.decision.unavailable_models)
        if attempt.runtime.failure_category == FailureCategory.MODEL_UNAVAILABLE:
            choice = attempt.request.decision.choice
            result.add((choice.provider, choice.model))
    return frozenset(result)


def _freeze(
    database: Path,
    identifier: str,
    repository: Path,
    state: TaskState,
    *,
    held_lease: ExecutorLease | None = None,
) -> None:
    if not state.delivery_ready:
        return
    assert state.current_attempt is not None
    owner = executor_lock(repository) if held_lease is None else nullcontext(held_lease)
    with TaskReviewStore(database) as store, owner as lease:
        if store.load_task(identifier).current_candidate is None:
            freeze_task_candidate(
                store, identifier, state.current_attempt.number, lease
            )
        else:
            inspect_task_candidate(
                store, identifier, state.current_attempt.number, lease
            )


def _new_dispatch(args: argparse.Namespace) -> TaskDispatchPolicy:
    dispatch = TaskDispatchPolicy(
        args.task_type,
        ImplementationChoice(*args.implementation) if args.implementation else None,
        tuple(ImplementationChoice(*item) for item in (args.fallback or ())),
    )
    _dispatch(dispatch, frozenset())
    return dispatch


def _execution_policy(
    args: argparse.Namespace, planning: PlanningState, dispatch: TaskDispatchPolicy
) -> TaskExecutionPolicy:
    assert planning.card is not None
    decision = resolve_dispatch(
        planning.card,
        planning.planning_input.frozen_rules,
        task_type=dispatch.task_type,
        requested=dispatch.requested,
        fallbacks=dispatch.fallbacks,
    )
    provider = {"codex": "codex", "claude-code": "claude"}[decision.choice.provider]
    return capture_task_policy(
        provider,
        _program(getattr(args, provider + "_bin"), provider),
        max_attempts=2 if args.max_attempts is None else args.max_attempts,
        timeout=180 if args.timeout is None else args.timeout,
        grace=2 if args.grace is None else args.grace,
    )


def _run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    database = _database(args.database).path
    policy = None
    dispatch = None
    unavailable: frozenset[tuple[str, str]] = frozenset()
    with TaskReviewStore(database) as store:
        planning = store.load_planning(args.planning_run_id)
        if (
            planning.phase not in (PlanningPhase.READY, PlanningPhase.BOUND)
            or planning.card is None
        ):
            result = view_task(database, planning_run_id=args.planning_run_id)
            result.update(
                operation="run",
                next_action=_next(
                    "user_decision",
                    "A verified ready plan is required.",
                    planning_run_id=args.planning_run_id,
                ),
            )
            return result, 1
        identifier = planning.bound_task_run_id
        state = store.load_task(identifier) if identifier is not None else None
        if args.retry:
            _unchanged(args, _STRATEGY)
            if state is None or state.phase not in (
                TaskPhase.FAILED,
                TaskPhase.CANCELLED,
            ):
                raise ValueError(
                    "ordinary retry requires an existing failed or cancelled implementation"
                )
            if any(
                isinstance(item.request, TaskReworkStarted) for item in state.attempts
            ):
                assert identifier is not None
                return _refusal(
                    view_task(database, task_run_id=identifier),
                    "run",
                    "user_decision",
                    "The single rework was already consumed; ordinary retry is unavailable.",
                )
            prepared = (
                prepared_for_task(state.current_attempt)
                if state.current_attempt
                else None
            )
            if prepared is None:
                raise ValueError("previous implementation has no managed preparation")
            if len(state.attempts) >= prepared.policy.max_attempts:
                assert identifier is not None
                return _refusal(
                    view_task(database, task_run_id=identifier),
                    "run",
                    "budget_exhausted",
                    "The original implementation attempt budget is exhausted.",
                )
            unavailable = _unavailable(state)
        else:
            if state is not None and (state.phase != TaskPhase.READY or state.attempts):
                raise ValueError(
                    "existing implementation requires explicit retry or inspection"
                )
            dispatch = _new_dispatch(args)
            policy = _execution_policy(args, planning, dispatch)
            with executor_lock(args.repository) as lease:
                _new_root(args.directory, _database(database), lease)
            if identifier is None:
                identifier = uuid4().hex
                bind_planning_task(
                    store,
                    args.planning_run_id,
                    identifier,
                    datetime.now(UTC).isoformat(),
                )
    assert identifier is not None

    async def execute(stop: asyncio.Event) -> TaskState:
        return await execute_task_attempt(
            database,
            identifier,
            args.planning_run_id,
            repository=args.repository,
            directory=args.directory,
            policy=policy,
            dispatch_policy=dispatch,
            unavailable=unavailable,
            stop=stop,
        )

    try:
        state, interrupted = asyncio.run(_interruptible(execute))
        if not interrupted:
            _freeze(database, identifier, args.repository, state)
    except BaseException as error:
        error.add_note(
            f"Task run retained: {identifier}; database: {database}; directory: {args.directory}"
        )
        raise
    result = view_task(database, task_run_id=identifier)
    result["operation"] = "run"
    return result, 130 if interrupted else 0 if result[
        "verification"
    ] == "verified" else 1


def _batch_plans(
    store: TaskReviewStore, identifiers: list[str], lease: ExecutorLease
) -> list[tuple[str, PlanningState, TaskCard]]:
    selections = []
    for identifier in identifiers:
        planning = store.load_planning(identifier)
        card = planning.card
        if (
            planning.phase not in (PlanningPhase.READY, PlanningPhase.BOUND)
            or not planning.evidence_ready
            or card is None
        ):
            raise ValueError(f"{identifier} requires a verified ready plan")
        if planning.bound_task_run_id is not None:
            state = store.load_task(planning.bound_task_run_id)
            if state.phase != TaskPhase.READY or state.attempts:
                raise ValueError(f"{card.id} already has an implementation attempt")
        if planning.planning_input.frozen_rules.repository != str(lease.repository):
            raise ValueError(f"{card.id} belongs to a different repository")
        selections.append((identifier, planning, card))

    cards = tuple(card for _, _, card in selections)
    for left, right in combinations(cards, 2):
        pair = f"{left.id} and {right.id}"
        if left.baseline_commit != right.baseline_commit:
            raise ValueError(f"{pair} have different baselines")
        if left.id in right.depends_on or right.id in left.depends_on:
            raise ValueError(f"{pair} have an implementation dependency")
        for a in left.write_scope:
            for b in right.write_scope:
                if patterns_intersect(a, b):
                    raise ValueError(
                        f"{pair} have overlapping write scopes: {a!r}, {b!r}"
                    )
    validate_task_graph(
        cards,
        frozenset(
            receipt.task_id
            for _, planning, _ in selections
            for receipt in planning.planning_input.dependencies
        ),
    )
    return selections


@dataclass(frozen=True, slots=True)
class _BatchMember:
    planning_id: str
    task_run_id: str
    directory: Path
    policy: TaskExecutionPolicy


def _run_batch(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    identifiers = args.planning_run_ids
    if len(identifiers) < 2 or len(set(identifiers)) != len(identifiers):
        raise ValueError("run-batch requires at least two distinct planning run IDs")
    database = _database(args.database)
    dispatch = _new_dispatch(args)
    with (
        TaskReviewStore(database.path) as store,
        executor_lock(args.repository) as lease,
    ):
        selections = _batch_plans(store, identifiers, lease)
        members = [
            _BatchMember(
                identifier,
                planning.bound_task_run_id or uuid4().hex,
                _new_root(
                    args.directory.with_name(f"{args.directory.name}-{card.id}"),
                    database,
                    lease,
                ),
                _execution_policy(args, planning, dispatch),
            )
            for identifier, planning, card in selections
        ]
        batch = prepare_task_batch(
            store, lease, frozenset(member.task_run_id for member in members)
        )
        for member, (_, planning, _) in zip(members, selections, strict=True):
            if planning.bound_task_run_id is None:
                bind_planning_task(
                    store,
                    member.planning_id,
                    member.task_run_id,
                    datetime.now(UTC).isoformat(),
                )
            else:
                provenance = planning.provenance()
                for stage, attempt in (
                    ("draft", provenance.draft),
                    ("review", provenance.review),
                ):
                    inspect_planning_execution(
                        store, member.planning_id, stage, attempt.number
                    )

        async def execute(stop: asyncio.Event) -> list[TaskState | BaseException]:
            return await asyncio.gather(
                *(
                    execute_task_attempt(
                        database.path,
                        member.task_run_id,
                        member.planning_id,
                        repository=lease.source,
                        directory=member.directory,
                        policy=member.policy,
                        dispatch_policy=dispatch,
                        stop=stop,
                        batch=batch,
                    )
                    for member in members
                ),
                return_exceptions=True,
            )

        outcomes, interrupted = asyncio.run(_interruptible(execute))
        failures: dict[str, list[str]] = {}
        for member, outcome in zip(members, outcomes, strict=True):
            try:
                if isinstance(outcome, BaseException):
                    raise outcome
                if not interrupted:
                    _freeze(
                        database.path,
                        member.task_run_id,
                        lease.source,
                        outcome,
                        held_lease=lease,
                    )
            except (*_ERRORS, RuntimeFailure) as error:
                failures[member.task_run_id] = [
                    str(error),
                    *getattr(error, "__notes__", ()),
                ]

    tasks = []
    for member in members:
        task = view_task(database.path, task_run_id=member.task_run_id)
        task["details"]["source_errors"].extend(failures.get(member.task_run_id, ()))
        tasks.append(task)
    verified = all(task["verification"] == "verified" for task in tasks)
    return {
        "operation": "run-batch",
        "tasks": tasks,
    }, 130 if interrupted else 0 if verified else 1


def _rework(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    from multi_agent.core.review_input import ExecutedReviewInput

    database = _database(args.database).path
    with TaskReviewStore.open_readonly(database) as store:
        round_state = store.load_round(args.review_round_id)
        value = load_review_input(store, round_state.snapshot_id)
        if not isinstance(value, ExecutedReviewInput):
            raise TypeError("rework requires executed task-review evidence")
        source = value.executed_suite.prepared.checkout.input
        identifier = source.task_run_id
        planning_id = store.planning_run_for_task(identifier)
        state = store.load_task(identifier)
        parent = next(
            (
                item
                for item in state.attempts
                if item.number == source.implementation_attempt
            ),
            None,
        )
        prepared = prepared_for_task(parent) if parent is not None else None
        if prepared is None:
            raise ValueError("rework source has no original managed implementation")
        repository = Path(prepared.workspace.source)

    async def execute(stop: asyncio.Event) -> TaskState:
        return await execute_task_attempt(
            database,
            identifier,
            planning_id,
            repository=repository,
            directory=args.directory,
            stop=stop,
            rework_from=args.review_round_id,
        )

    try:
        state, interrupted = asyncio.run(_interruptible(execute))
        if not interrupted:
            _freeze(database, identifier, repository, state)
    except ReworkRefused as error:
        return _refusal(
            view_task(database, task_run_id=identifier),
            "rework",
            error.kind,
            str(error),
            rework_refusal_kind=error.kind,
            source_review_round_id=args.review_round_id,
            parent_candidate=source.candidate.delivery.candidate,
        )
    except BaseException as error:
        error.add_note(
            f"Rework task retained: {identifier}; source review: {args.review_round_id}; directory: {args.directory}"
        )
        raise
    with _preserve_interruption(interrupted):
        result = view_task(database, task_run_id=identifier)
    result.update(
        operation="rework",
        source_review_round_id=args.review_round_id,
        parent_candidate=source.candidate.delivery.candidate,
    )
    return result, 130 if interrupted else 0 if result[
        "verification"
    ] == "verified" else 1


def main(args: argparse.Namespace) -> int:
    try:
        if args.task_operation == "show":
            value = view_task(
                args.database,
                planning_run_id=args.planning_run_id,
                task_run_id=args.task_run_id,
                history=args.history,
            )
            code = 0
        else:
            value, code = {
                "plan": _plan,
                "run": _run,
                "run-batch": _run_batch,
                "rework": _rework,
            }[args.task_operation](args)
        print(json.dumps(value, ensure_ascii=False))
        return code
    except ReworkRefused as error:
        value = _base(
            args.task_operation,
            next_action=_next(
                error.kind,
                str(error),
                planning_run_id=getattr(args, "planning_run_id", None),
                review_round_id=getattr(args, "review_round_id", None),
            ),
            details={"notes": getattr(error, "__notes__", [])},
        )
        print(json.dumps(value, ensure_ascii=False))
        return 1
    except _ERRORS as error:
        print(str(error), file=sys.stderr)
        for note in getattr(error, "__notes__", ()):
            print(note, file=sys.stderr)
        return 2
    except KeyboardInterrupt as error:
        if args.task_operation == "rework":
            for note in getattr(error, "__notes__", ()):
                print(note, file=sys.stderr)
        return 130
