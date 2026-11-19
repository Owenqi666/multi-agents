"""Project an exact Sol split entry and run it through independent review."""

from __future__ import annotations

import argparse
import asyncio
import re
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from multi_agent.core.dispatch import ImplementationChoice, resolve_dispatch
from multi_agent.core.planning import (
    PlanningInput,
    PlanningPhase,
    ProjectedDependency,
    ProjectedDraft,
    planning_json,
)
from multi_agent.core.planning_events import PlanningSplitValidated
from multi_agent.core.planning_execution import prepared_for
from multi_agent.core.project_plan import StageRun
from multi_agent.core.tasks import TaskCard
from multi_agent.store.project_store import ProjectStore

from .planning_evidence import (
    inspect_project_split,
    inspect_projected_draft,
    split_entry_hash,
)
from .planning_inputs import parse_planning_draft
from .planning_runner import execute_planning_attempt
from .project_integration import view_delegated_integration
from .snapshot import _resolve_commit
from .task_cli import _interruptible, _new_dispatch, _run, view_task
from .task_inputs import freeze_project_rules

_EXECUTOR_MODELS = {
    "codex": re.compile(r"gpt-[0-9]+(?:\.[0-9]+)*-terra"),
    "claude-code": re.compile(r"claude-sonnet-[0-9]+(?:-[0-9]+)*"),
}


def _leaf_view(database: Path, identifier: str) -> dict[str, Any]:
    planning = view_task(database, planning_run_id=identifier)
    task_id = planning["task_run_id"]
    implementation = view_task(database, task_run_id=task_id) if task_id else None
    candidate = implementation["candidate"] if implementation else None
    attempt = implementation["implementation_attempt"] if implementation else None
    verified = bool(
        candidate and implementation and implementation["verification"] == "verified"
    )
    reviews = planning["attempts"]["review"]
    latest = reviews[-1]["runtime"] if reviews else None
    reason = planning["details"]["validation_error"]
    if not reason and latest and planning["phase"] in ("failed", "revision_required"):
        result = latest["result"]
        reason = latest["detail"] or (
            result.get("reason") if isinstance(result, dict) else None
        )
    if not reason and planning["details"]["source_errors"]:
        reason = planning["details"]["source_errors"][0]
    if implementation and not verified and not reason:
        detail = implementation["details"]
        reason = next(iter(detail["source_errors"]), None) or implementation["phase"]
    return {
        "planning_run_id": identifier,
        "task_run_id": task_id,
        "candidate": candidate,
        "implementation_attempt": attempt,
        "candidate_verified": verified,
        "review_ready": planning["phase"] in ("ready", "bound")
        and planning["verification"] == "verified",
        "blocked_reason": reason,
        "planning": planning,
        "implementation": implementation,
    }


def delegated_view(
    store: ProjectStore, database: Path, stage: StageRun
) -> dict[str, Any]:
    parent = view_task(database, planning_run_id=stage.planning_run_id)
    state = store.load_planning(stage.planning_run_id)
    attempt = state.current("draft")
    definitions = (
        next(
            (
                event.cards
                for event in attempt.events
                if isinstance(event, PlanningSplitValidated)
            ),
            (),
        )
        if attempt
        else ()
    )
    leaf_runs = store.leaf_runs(stage.stage_run_id)
    leaves = {
        card_id: _leaf_view(database, identifier)
        for card_id, identifier in leaf_runs.items()
    }
    records = []
    for card in definitions:
        leaf = leaves.get(card.id)
        unmet = [
            item
            for item in card.depends_on
            if not leaves.get(item, {}).get("candidate_verified")
        ]
        records.append(
            {
                "card_id": card.id,
                "definition": card.to_dict(),
                "unmet_dependencies": unmet,
                "prior_planning_run_ids": store.leaf_history(
                    stage.stage_run_id, card.id
                )[:-1],
                **(
                    leaf
                    or {
                        "planning_run_id": None,
                        "task_run_id": None,
                        "candidate": None,
                        "candidate_verified": False,
                        "review_ready": False,
                        "blocked_reason": f"waiting for {', '.join(unmet)}"
                        if unmet
                        else None,
                    }
                ),
            }
        )
    split_verified = bool(definitions and parent["verification"] == "verified")
    can_integrate = bool(
        split_verified and all(item["candidate_verified"] for item in records)
    )
    goal = store.show_plan(stage.plan_id)["goal"]
    integration = (
        view_delegated_integration(
            database,
            Path(goal["git_common_dir"]),
            stage.execution_baseline,
            tuple(
                (item["task_run_id"], item["implementation_attempt"], item["candidate"])
                for item in records
            ),
        )
        if can_integrate
        else None
    )
    return {
        "kind": "project_stage",
        **asdict(stage),
        "goal_id": goal["goal_id"],
        "planning": parent,
        "split_verified": split_verified,
        "leaves": records,
        "can_integrate": can_integrate,
        "integration": integration,
        "candidate": integration["candidate"] if integration else None,
        "candidate_verified": bool(integration and integration["candidate"]),
        "tests_passed": bool(integration and integration["tests_passed"]),
        "review_satisfied": bool(integration and integration["review_satisfied"]),
        "test_attempt": integration["test_attempt"] if integration else None,
        "review_round_id": integration["review_round_id"] if integration else None,
    }


def _dependencies(
    store: ProjectStore, database: Path, stage: StageRun, card: TaskCard
) -> tuple[ProjectedDependency, ...]:
    leaves = store.leaf_runs(stage.stage_run_id)
    ready = []
    for card_id in card.depends_on:
        identifier = leaves.get(card_id)
        if identifier is None:
            raise ValueError(f"leaf {card_id} has no verified candidate")
        result = _leaf_view(database, identifier)
        if not result["candidate_verified"]:
            raise ValueError(f"leaf {card_id} has no verified candidate")
        ready.append(
            ProjectedDependency(
                card_id,
                identifier,
                result["task_run_id"],
                result["implementation_attempt"],
                result["candidate"],
            )
        )
    return tuple(ready)


def _leaf_baseline(
    repo: Path,
    common: Path,
    database: Path,
    stage: StageRun,
    dependencies: tuple[ProjectedDependency, ...],
    revision: str | None,
) -> tuple[str, dict[str, Any] | None]:
    from .project_stage import _contains

    candidates = tuple(dict.fromkeys(item.candidate for item in dependencies))
    choices = (
        (_resolve_commit(repo, revision),)
        if revision is not None
        else candidates or (stage.execution_baseline,)
    )
    for choice in choices:
        if not _contains(repo, choice, stage.execution_baseline):
            continue
        if all(_contains(repo, choice, candidate) for candidate in candidates):
            return choice, None
        integration = view_delegated_integration(
            database,
            common,
            stage.execution_baseline,
            tuple(
                (item.task_run_id, item.implementation_attempt, item.candidate)
                for item in dependencies
            ),
        )
        if (
            integration
            and integration["review_satisfied"]
            and _contains(repo, choice, integration["candidate"])
        ):
            return choice, integration
    raise ValueError(
        "the leaf baseline must contain the stage and all exact upstream candidates"
    )


def _executor(fields: list[str]) -> ImplementationChoice:
    choice = _new_dispatch(
        argparse.Namespace(implementation=fields, fallback=None, task_type=None)
    ).requested
    assert choice is not None
    pattern = _EXECUTOR_MODELS.get(choice.provider)
    if pattern is None or pattern.fullmatch(choice.model) is None:
        raise ValueError(
            "delegated execution requires a Codex Terra or Claude Code Sonnet model"
        )
    return choice


def _claim(
    args: argparse.Namespace,
    store: ProjectStore,
    database: Path,
    stage: StageRun,
    repo: Path,
    common: Path,
    *,
    previous_id: str | None = None,
) -> str:
    previous = store.load_planning(previous_id) if previous_id is not None else None
    prior = previous.projection if previous is not None else None
    if args.implementation is not None:
        executor = _executor(args.implementation)
    elif prior is not None:
        executor = prior.executor
    else:
        raise ValueError("first claim requires --implementation PROVIDER MODEL EFFORT")
    attempt, cards, manifest = inspect_project_split(store, stage.planning_run_id)
    selected = next(
        ((index, card) for index, card in enumerate(cards) if card.id == args.card_id),
        None,
    )
    if selected is None:
        raise ValueError(f"unknown leaf {args.card_id} in {stage.stage_run_id}")
    index, definition = selected
    dependencies = _dependencies(store, database, stage, definition)
    if prior is not None and dependencies == prior.dependencies:
        raise ValueError("refreezing requires a changed verified upstream candidate")
    baseline, integration = _leaf_baseline(
        repo, common, database, stage, dependencies, args.baseline
    )
    rules = freeze_project_rules(repo, baseline)
    card = replace(definition, baseline_commit=baseline)
    resolve_dispatch(card, rules, requested=executor)
    parent = store.load_planning_input(stage.planning_run_id)
    projection = ProjectedDraft(
        stage.stage_run_id,
        stage.planning_run_id,
        attempt.number,
        manifest.sha256,
        index,
        split_entry_hash(attempt, index),
        card.task_hash,
        executor,
        dependencies,
    )
    handoff = {
        "selected_leaf": card.id,
        "parent_projection": projection,
        "project_stage": parent.requirements.partition("\n")[2],
        "upstream_integration": (
            {
                key: integration[key]
                for key in (
                    "integration_id",
                    "candidate",
                    "test_attempt",
                    "test_evidence_hash",
                    "review_round_id",
                    "review_snapshot_id",
                )
            }
            if integration is not None
            else None
        ),
    }
    value = PlanningInput(
        "Implement and review only the exact selected leaf card, using the stage below as context.\n"
        + planning_json(handoff),
        rules,
        parent.draft_request,
        parent.review_request,
    )
    parse_planning_draft(
        planning_json(card.to_dict()),
        value,
        configured_roles=True,
        verified_dependency_ids=projection.dependency_ids,
    )
    identifier = uuid4().hex
    store.reserve_leaf(
        identifier,
        projection,
        card,
        value,
        datetime.now(UTC).isoformat(),
        replaces_run_id=previous_id,
    )
    return identifier


def launch_leaf(
    args: argparse.Namespace, store: ProjectStore, database: Path
) -> dict[str, Any]:
    from .project_cli import _repository

    stage = store.load_stage(args.stage_run_id)
    if stage.mode != "delegate":
        raise ValueError("leaf operations require a delegated stage")
    goal = store.show_plan(stage.plan_id)["goal"]
    repo, common = _repository(Path(goal["repository"]), database.parent)
    if str(common) != goal["git_common_dir"]:
        raise ValueError("repository Git metadata differs from the confirmed goal")
    identifier = store.leaf_runs(stage.stage_run_id).get(args.card_id)
    if args.refreeze:
        if identifier is None:
            raise ValueError("refreezing requires an existing leaf")
        identifier = _claim(
            args, store, database, stage, repo, common, previous_id=identifier
        )
    elif identifier is None:
        identifier = _claim(args, store, database, stage, repo, common)
    state = store.load_planning(identifier)
    projection = state.projection
    assert projection is not None
    executor = projection.executor
    if (
        args.implementation is not None
        and _executor(args.implementation) != executor
        or (
            args.baseline is not None
            and _resolve_commit(repo, args.baseline)
            != state.planning_input.frozen_rules.baseline_commit
        )
    ):
        raise ValueError("an existing leaf retains its exact executor and baseline")
    inspect_projected_draft(store, identifier, state)
    parent = store.load_planning(stage.planning_run_id).current("draft")
    assert parent is not None
    prepared = prepared_for(parent)
    assert prepared is not None
    policy = prepared.policy

    def result() -> dict[str, Any]:
        return {
            "kind": "project_leaf",
            "plan_id": stage.plan_id,
            "stage_id": stage.stage_id,
            "stage_run_id": stage.stage_run_id,
            "card_id": args.card_id,
            **_leaf_view(database, identifier),
        }

    if state.phase in (
        PlanningPhase.AWAITING_REVIEW,
        PlanningPhase.FAILED,
        PlanningPhase.CANCELLED,
    ):
        first = not state.review_attempts
        if (
            len(state.review_attempts)
            >= policy.for_provider(
                state.planning_input.review_request.provider
            ).max_attempts
        ):
            return result()

        async def review(stop: asyncio.Event) -> None:
            await execute_planning_attempt(
                database,
                identifier,
                "review",
                repository=repo if first else None,
                directory=database.parent / f"leaf-review-{uuid4().hex}"
                if first
                else None,
                policy=policy if first else None,
                stop=stop,
            )

        _, interrupted = asyncio.run(_interruptible(review))
        if interrupted:
            return {**result(), "interrupted": True}
        state = store.load_planning(identifier)
    if args.review_only or state.phase != PlanningPhase.READY:
        return result()
    _, status = _run(
        argparse.Namespace(
            database=database,
            repository=repo,
            directory=database.parent / f"leaf-work-{identifier}",
            planning_run_id=identifier,
            retry=False,
            task_type=None,
            fallback=None,
            implementation=(executor.provider, executor.model, executor.effort),
            max_attempts=None,
            timeout=policy.timeout,
            grace=policy.grace,
            codex_bin=policy.for_provider("codex").executable,
            claude_bin=policy.for_provider("claude").executable,
        )
    )
    return {**result(), "interrupted": True} if status == 130 else result()
