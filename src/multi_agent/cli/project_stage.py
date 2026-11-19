"""Start one direct stage and read its existing formal task evidence."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from multi_agent.core.dispatch import ImplementationChoice, resolve_dispatch
from multi_agent.core.integration import IntegrationCandidate
from multi_agent.core.planning import PlanningInput, PlanningPhase, PlanningRequest
from multi_agent.core.planning_execution import (
    PlanningExecutionPolicy,
    prepared_attempts,
)
from multi_agent.core.project_plan import (
    ProjectPlan,
    StageDependency,
    StageIntegrationProof,
    StagePlan,
    StageRun,
)
from multi_agent.store.integration_store import IntegrationStore
from multi_agent.store.project_store import ProjectStore

from .integration_cli import inspect_integration_candidate
from .integration_git import read_ref
from .integration_verification import verification_view
from .planning_context import capture_planning_policy
from .planning_inputs import validate_planning_input
from .planning_runner import execute_planning_attempt
from .snapshot import _git, _resolve_commit
from .task_cli import _PLAN_INPUTS, _interruptible, _plan, _program, _run, view_task
from .task_inputs import RULES_PATH, freeze_project_rules


def add_stage_parser(
    actions: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    stage = actions.add_parser(
        "stage", help="claim or read stages of a saved project plan"
    )
    operations = stage.add_subparsers(dest="stage_action", required=True)
    start = operations.add_parser(
        "start", help="have Sol plan a direct or delegated stage"
    )
    start.add_argument("plan_id")
    start.add_argument("stage_id")
    start.add_argument("--mode", choices=("direct", "delegate"), default="direct")
    start.add_argument(
        "--baseline", help="choose a commit containing all verified prerequisites"
    )
    start.add_argument(
        "--sol", nargs=2, metavar=("MODEL", "EFFORT"), default=("gpt-6.2-sol", "high")
    )
    start.add_argument(
        "--card-review",
        nargs=3,
        metavar=("PROVIDER", "MODEL", "EFFORT"),
        default=("claude", "claude-opus-5-5", "high"),
    )
    start.add_argument("--codex-bin")
    start.add_argument("--claude-bin")
    start.add_argument("--timeout", type=float, default=300)
    start.add_argument("--grace", type=float, default=2)
    resume = operations.add_parser("resume", help="explicitly continue a claimed stage")
    resume.add_argument("stage_run_id")
    resume.add_argument(
        "--prepare",
        action="store_true",
        help="capture execution policy if the first attempt was never prepared",
    )
    resume.add_argument("--codex-bin")
    resume.add_argument("--claude-bin")
    resume.add_argument("--timeout", type=float)
    resume.add_argument("--grace", type=float)
    leaf = operations.add_parser("leaf", help="review or implement one delegated card")
    leaf.add_argument("stage_run_id")
    leaf.add_argument("card_id")
    leaf.add_argument(
        "--implementation", nargs=3, metavar=("PROVIDER", "MODEL", "EFFORT")
    )
    leaf.add_argument("--baseline")
    leaf.add_argument("--review-only", action="store_true")
    leaf.add_argument(
        "--refreeze",
        action="store_true",
        help="review a previously unbound leaf against changed upstream candidates",
    )
    operations.add_parser("show").add_argument("stage_run_id")
    operations.add_parser("list").add_argument("plan_id")


def _test_and_review(
    task: dict[str, Any], required_names: set[str]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if task["verification"] != "verified":
        return None, None
    tests = next(
        (
            item
            for item in task["tests"]
            if item["implementation_attempt"] == task["implementation_attempt"]
            and item["candidate"] == task["candidate"]
            and item["phase"] == "succeeded"
            and item["verification"] == "verified"
            and required_names <= {command["name"] for command in item["commands"]}
        ),
        None,
    )
    if tests is None:
        return None, None
    review = next(
        (
            item
            for item in reversed(task["reviews"])
            if item["metadata"]["kind"] == "review"
            and item["implementation_attempt"] == task["implementation_attempt"]
            and item["candidate"] == task["candidate"]
        ),
        None,
    )
    if review is not None and (
        not review["current_eligible"]
        or review["test_attempt"] != tests["test_attempt"]
    ):
        review = None
    return tests, review


def _matching_implementation(task: dict[str, Any], sol: PlanningRequest) -> bool:
    attempt = next(
        (
            item
            for item in task["attempts"]
            if item["runtime"]["number"] == task["implementation_attempt"]
        ),
        None,
    )
    if attempt is None:
        return False
    request = attempt["request"]["decision"]["choice"]
    return bool(request == asdict(sol))


def stage_view(store: ProjectStore, database: Path, stage: StageRun) -> dict[str, Any]:
    if stage.mode == "delegate":
        from .project_delegation import delegated_view

        return delegated_view(store, database, stage)
    goal_id = store.show_plan(stage.plan_id)["goal"]["goal_id"]
    planning = view_task(database, planning_run_id=stage.planning_run_id)
    task_run_id = planning["task_run_id"]
    task = view_task(database, task_run_id=task_run_id) if task_run_id else None
    candidate = task["candidate"] if task else None
    tests, review = (None, None)
    candidate_verified = bool(
        task
        and task["verification"] == "verified"
        and _matching_implementation(
            task, store.load_planning_input(stage.planning_run_id).draft_request
        )
    )
    if task is not None and candidate_verified:
        tests, review = _test_and_review(task, set(planning["card"]["test_refs"]))
    return {
        "kind": "project_stage",
        **asdict(stage),
        "goal_id": goal_id,
        "task_run_id": task_run_id,
        "candidate": candidate,
        "candidate_verified": candidate_verified,
        "tests_passed": tests is not None,
        "review_satisfied": review is not None,
        "test_attempt": tests["test_attempt"] if tests is not None else None,
        "review_round_id": review["round_id"] if review is not None else None,
        "planning": planning,
        "implementation": task,
    }


def _prerequisite_run(
    store: ProjectStore, plan_id: str, stage_id: str
) -> StageRun | None:
    current = store.find_stage(plan_id, stage_id)
    if current is not None:
        return current
    plan = store.show_plan(plan_id)
    document = plan["document"]
    needed = {stage_id}
    definitions = {}
    for item in reversed(document["stages"]):
        if item["stage_id"] in needed:
            needed.update(item["depends_on"])
            definitions[item["stage_id"]] = item
    while source_id := plan.get("source_plan_id"):
        if any(
            store.find_stage(plan["plan_id"], predecessor) is not None
            for predecessor in needed - {stage_id}
        ):
            break
        source = store.show_plan(source_id)
        previous = source["document"]
        previous_stages = {item["stage_id"]: item for item in previous["stages"]}
        if (
            source["goal"]["goal_id"] != plan["goal"]["goal_id"]
            or previous["architecture"] != document["architecture"]
            or previous["shared_interfaces"] != document["shared_interfaces"]
            or any(
                previous_stages.get(key) != value for key, value in definitions.items()
            )
        ):
            break
        inherited = store.find_stage(source_id, stage_id)
        if inherited is not None:
            return inherited
        plan = source
    return None


def _prerequisites(
    store: ProjectStore, database: Path, plan_id: str, stage: StagePlan
) -> tuple[StageDependency, ...]:
    dependencies: list[StageDependency] = []
    for identifier in stage["depends_on"]:
        run = _prerequisite_run(store, plan_id, identifier)
        if run is None:
            raise ValueError(
                f"stage {identifier} has no verified delivery for plan {plan_id}"
            )
        result = stage_view(store, database, run)
        if not result["review_satisfied"]:
            raise ValueError(
                f"stage {identifier} requires an exact candidate with verified tests and review"
            )
        dependency = StageDependency(
            stage_id=identifier,
            stage_run_id=run.stage_run_id,
            candidate=result["candidate"],
            test_attempt=result["test_attempt"],
            review_round_id=result["review_round_id"],
        )
        if run.plan_id != plan_id:
            dependency["plan_id"] = run.plan_id
        if run.mode == "delegate":
            integration = result["integration"]
            dependency["integration"] = StageIntegrationProof(
                integration_id=integration["integration_id"],
                candidate=integration["candidate"],
                test_attempt=integration["test_attempt"],
                test_evidence_hash=integration["test_evidence_hash"],
                review_round_id=integration["review_round_id"],
                review_snapshot_id=integration["review_snapshot_id"],
            )
        else:
            dependency["task_run_id"] = result["task_run_id"]
            dependency["implementation_attempt"] = result["implementation"][
                "implementation_attempt"
            ]
        dependencies.append(dependency)
    return tuple(dependencies)


def _contains(repo: Path, baseline: str, prerequisite: str) -> bool:
    if baseline == prerequisite:
        return True
    try:
        common = (
            _git(repo, "merge-base", baseline, prerequisite).decode("ascii").strip()
        )
    except ValueError:
        return False
    return common == prerequisite


def _integrated_prerequisites(
    repo: Path,
    common: Path,
    database: Path,
    chosen: str,
    missing: tuple[StageDependency, ...],
) -> dict[str, StageIntegrationProof]:
    needed = {
        (item["task_run_id"], item["implementation_attempt"], item["candidate"]): item[
            "stage_id"
        ]
        for item in missing
        if "task_run_id" in item and "implementation_attempt" in item
    }
    covered: dict[str, StageIntegrationProof] = {}
    with IntegrationStore.open_readonly(database) as store:
        for record in reversed(store.list_integration_candidates(str(common))):
            result = record.result
            if not isinstance(result, IntegrationCandidate) or not _contains(
                repo, chosen, result.commit
            ):
                continue
            matches = [
                needed[key]
                for member in record.members
                if (
                    key := (
                        member.task_run_id,
                        member.attempt,
                        member.delivery.candidate,
                    )
                )
                in needed
                and needed[key] not in covered
            ]
            if not matches:
                continue
            inspect_integration_candidate(record)
            evidence = verification_view(
                store,
                record,
                target_current=read_ref(common, record.target_ref)
                == record.target_baseline,
            )
            reviews = evidence["reviews"]
            if (
                evidence["superseded_by"] is None
                and evidence["test_evidence_verification"] == "verified"
                and reviews
                and reviews[-1]["verification"] == "verified"
                and reviews[-1]["assessment"]["status"] == "ready"
            ):
                suite = evidence["test"]
                review = reviews[-1]
                proof = StageIntegrationProof(
                    integration_id=record.integration_id,
                    candidate=result.commit,
                    test_attempt=suite["attempt"],
                    test_evidence_hash=suite["evidence_hash"],
                    review_round_id=review["round_id"],
                    review_snapshot_id=review["snapshot_id"],
                )
                covered.update({stage_id: proof for stage_id in matches})
            if len(covered) == len(needed):
                break
    return covered


def _execution_baseline(
    repo: Path,
    common: Path,
    database: Path,
    plan_base: str,
    dependencies: tuple[StageDependency, ...],
    revision: str | None,
) -> tuple[str, tuple[StageDependency, ...]]:
    candidates = tuple(dict.fromkeys(item["candidate"] for item in dependencies))
    choices = (
        (_resolve_commit(repo, revision),)
        if revision is not None
        else candidates or (plan_base,)
    )
    for chosen in choices:
        if not _contains(repo, chosen, plan_base):
            continue
        missing = tuple(
            item
            for item in dependencies
            if not _contains(repo, chosen, item["candidate"])
        )
        if not missing:
            return chosen, dependencies
        if revision is not None:
            covered = _integrated_prerequisites(repo, common, database, chosen, missing)
            if len(covered) == len(missing):
                return chosen, tuple(
                    {**item, "integration": covered[item["stage_id"]]}
                    if item["stage_id"] in covered
                    else item
                    for item in dependencies
                )
    raise ValueError(
        "the stage baseline must contain the plan and every verified prerequisite candidate "
        "by ancestry or verified integration"
    )


def _handoff(
    plan_id: str,
    goal: dict[str, Any],
    document: ProjectPlan,
    stage: StagePlan,
    baseline: str,
    dependencies: tuple[StageDependency, ...],
    *,
    delegate: bool = False,
) -> str:
    context = {
        "plan_id": plan_id,
        "goal_id": goal["goal_id"],
        "original_goal": goal["original_goal"],
        "confirmed_goal": goal["confirmed_goal"],
        "planning_baseline": goal["baseline_commit"],
        "execution_baseline": baseline,
        "architecture": document["architecture"],
        "shared_interfaces": document["shared_interfaces"],
        "selected_stage": stage,
        "verified_prerequisites": dependencies,
        "other_stages": [
            {"stage_id": item["stage_id"], "title": item["title"]}
            for item in document["stages"]
            if item["stage_id"] != stage["stage_id"]
        ],
    }
    instruction = (
        "Split only selected_stage into executable task cards from the confirmed project plan below. "
        if delegate
        else "Implement only selected_stage from the confirmed project plan below. "
    )
    return (
        instruction
        + (
            "Other stages and the original user wording provide context. "
            "The workbench has checked the project-level prerequisites. "
        )
        + (
            "Use the selected stage's scope, acceptance and named tests across the split.\n"
            if delegate
            else "Use the selected stage's scope, acceptance and named tests for this one formal card.\n"
        )
        + json.dumps(context, ensure_ascii=False, sort_keys=True)
    )


def _start(
    args: argparse.Namespace, store: ProjectStore, database: Path
) -> dict[str, Any]:
    from .project_cli import _repository

    plan = store.show_plan(args.plan_id)
    if plan["document"] is None:
        raise ValueError("a successful project plan version is required")
    document: ProjectPlan = plan["document"]
    selected = next(
        (item for item in document["stages"] if item["stage_id"] == args.stage_id), None
    )
    if selected is None:
        raise ValueError(f"unknown stage {args.stage_id} in plan {args.plan_id}")
    existing = store.find_stage(args.plan_id, args.stage_id)
    if existing is not None:
        if existing.mode != args.mode:
            raise ValueError(f"stage has already been claimed in {existing.mode} mode")
        return stage_view(store, database, existing)
    goal = plan["goal"]
    repo, common = _repository(Path(goal["repository"]), database.parent)
    if str(common) != goal["git_common_dir"]:
        raise ValueError("repository Git metadata differs from the confirmed goal")
    dependencies = _prerequisites(store, database, args.plan_id, selected)
    baseline, dependencies = _execution_baseline(
        repo, common, database, goal["baseline_commit"], dependencies, args.baseline
    )
    sol = PlanningRequest("codex", *args.sol)
    if re.fullmatch(r"gpt-\d+(?:\.\d+)*-sol", sol.model) is None:
        raise ValueError("the stage planner must be an explicit GPT Sol model")
    reviewer = PlanningRequest(*args.card_review)
    if (sol.provider, sol.model) == (reviewer.provider, reviewer.model):
        raise ValueError("the independent card reviewer must use a distinct model")
    frozen = freeze_project_rules(repo, baseline)
    value = PlanningInput(
        _handoff(
            args.plan_id,
            goal,
            document,
            selected,
            baseline,
            dependencies,
            delegate=args.mode == "delegate",
        ),
        frozen,
        sol,
        reviewer,
    )
    validate_planning_input(value, configured_roles=True)
    policy = _capture_policy(args)
    record = StageRun(
        f"stage-{uuid4().hex}",
        args.plan_id,
        args.stage_id,
        uuid4().hex,
        args.mode,
        baseline,
        dependencies,
        datetime.now(UTC).isoformat(),
    )
    store.reserve_stage(record, value)

    return _prepare_stage(store, database, record, repo, policy)


def _capture_policy(args: argparse.Namespace) -> PlanningExecutionPolicy:
    return capture_planning_policy(
        {p: _program(getattr(args, p + "_bin"), p) for p in ("codex", "claude")},
        {"codex": 2, "claude": 2},
        args.timeout if args.timeout is not None else 300,
        args.grace if args.grace is not None else 2,
    )


def _prepare_stage(
    store: ProjectStore,
    database: Path,
    record: StageRun,
    repo: Path,
    policy: PlanningExecutionPolicy,
) -> dict[str, Any]:
    async def plan_card(stop: asyncio.Event) -> None:
        state = await execute_planning_attempt(
            database,
            record.planning_run_id,
            "draft",
            repository=repo,
            directory=database.parent / f"{record.stage_run_id}-planning-{uuid4().hex}",
            policy=policy,
            stop=stop,
        )
        if state.phase == PlanningPhase.AWAITING_REVIEW and not stop.is_set():
            await execute_planning_attempt(
                database, record.planning_run_id, "review", stop=stop
            )

    _, interrupted = asyncio.run(_interruptible(plan_card))
    if interrupted:
        return {**stage_view(store, database, record), "interrupted": True}
    return _implement(store, database, record, repo)


def _implement(
    store: ProjectStore, database: Path, record: StageRun, repo: Path
) -> dict[str, Any]:
    state = store.load_planning(record.planning_run_id)
    if (
        state.phase != PlanningPhase.READY
        or state.card is None
        or record.dispatch_error
    ):
        return stage_view(store, database, record)
    policy = prepared_attempts(state)[0].policy
    sol = state.planning_input.draft_request
    choice = ImplementationChoice(sol.provider, sol.model, sol.effort)
    try:
        resolve_dispatch(
            state.card, state.planning_input.frozen_rules, requested=choice
        )
    except ValueError as error:
        paths = ", ".join(state.card.write_scope)
        store.block_stage_dispatch(
            record.stage_run_id, f"{RULES_PATH} ({paths}): {error}"
        )
        return stage_view(store, database, store.load_stage(record.stage_run_id))

    _, status = _run(
        argparse.Namespace(
            database=database,
            repository=repo,
            directory=database.parent / f"{record.stage_run_id}-implementation",
            planning_run_id=record.planning_run_id,
            retry=False,
            task_type=None,
            implementation=(choice.provider, choice.model, choice.effort),
            fallback=None,
            timeout=policy.timeout,
            grace=policy.grace,
            max_attempts=None,
            codex_bin=policy.for_provider("codex").executable,
            claude_bin=policy.for_provider("claude").executable,
        )
    )
    result = stage_view(store, database, record)
    if status == 130:
        result["interrupted"] = True
    return result


def _resume(
    args: argparse.Namespace, store: ProjectStore, database: Path, record: StageRun
) -> dict[str, Any]:
    from .project_cli import _repository

    state = store.load_planning(record.planning_run_id)
    if args.prepare and (
        state.phase != PlanningPhase.PENDING
        or state.draft_attempts
        or state.review_attempts
    ):
        raise ValueError(
            "an existing planning attempt retains its original execution policy"
        )
    supplied = (args.codex_bin, args.claude_bin, args.timeout, args.grace)
    if any(item is not None for item in supplied) and not args.prepare:
        raise ValueError("execution settings require an unprepared stage and --prepare")
    if state.bound_task_run_id or record.dispatch_error:
        return stage_view(store, database, record)
    goal = store.show_plan(record.plan_id)["goal"]
    repo, common = _repository(Path(goal["repository"]), database.parent)
    if str(common) != goal["git_common_dir"]:
        raise ValueError("repository Git metadata differs from the confirmed goal")
    if state.phase == PlanningPhase.PENDING and not state.draft_attempts:
        if not args.prepare:
            raise ValueError(
                "the first attempt was never prepared; resume using --prepare"
            )
        return _prepare_stage(store, database, record, repo, _capture_policy(args))
    if state.phase != PlanningPhase.READY:
        _, status = _plan(
            argparse.Namespace(
                database=database,
                retry=record.planning_run_id,
                **dict.fromkeys(_PLAN_INPUTS),
            )
        )
        if status == 130:
            return {**stage_view(store, database, record), "interrupted": True}
        if status != 0:
            return stage_view(store, database, record)
    return _implement(store, database, record, repo)


def dispatch_stage(
    args: argparse.Namespace, store: ProjectStore, database: Path
) -> dict[str, Any]:
    if args.stage_action == "start":
        return _start(args, store, database)
    if args.stage_action == "leaf":
        from .project_delegation import launch_leaf

        return launch_leaf(args, store, database)
    if args.stage_action == "resume":
        return _resume(args, store, database, store.load_stage(args.stage_run_id))
    if args.stage_action == "show":
        return stage_view(store, database, store.load_stage(args.stage_run_id))
    return {
        "kind": "project_stages",
        "plan_id": args.plan_id,
        "stages": [asdict(item) for item in store.list_stages(args.plan_id)],
    }
