"""Confirm project goals, run a selected planner, and read immutable versions."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from multi_agent.adapters.project_planner import PROMPT_VERSION, run_project_planner
from multi_agent.core.planning import PlanningRequest
from multi_agent.core.project_plan import ConfirmedGoal
from multi_agent.store.project_store import ProjectStore

from .main import data_directory
from .project_stage import add_stage_parser, dispatch_stage
from .runner import _tree_digest
from .snapshot import _git, _resolve_commit, _string, freeze_repository, materialize


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    project = commands.add_parser(
        "project", help="confirm goals and version project plans"
    )
    actions = project.add_subparsers(dest="project_action", required=True)
    confirm = actions.add_parser("confirm", help="record the PM's confirmed goal")
    confirm.add_argument(
        "--project-id", help="record another goal version for this project"
    )
    confirm.add_argument("--repo", type=Path, required=True)
    confirm.add_argument("--baseline", required=True)
    confirm.add_argument("--original-goal", type=Path, required=True)
    confirm.add_argument("--confirmed-goal", type=Path, required=True)
    plan = actions.add_parser(
        "plan", help="generate a project plan from a confirmed goal"
    )
    plan.add_argument("goal_id")
    plan.add_argument(
        "--planner", nargs=3, metavar=("PROVIDER", "MODEL", "EFFORT"), required=True
    )
    plan.add_argument("--timeout", type=float, default=300)
    plan.add_argument("--grace", type=float, default=2)
    plan.add_argument("--codex-bin", default="codex")
    plan.add_argument("--claude-bin", default="claude")
    show = actions.add_parser(
        "show", help="read a project, confirmed goal, or plan version"
    )
    show.add_argument("version_id")
    add_stage_parser(actions)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _repository(path: Path, data_path: Path) -> tuple[Path, Path]:
    repo = path.expanduser().resolve(strict=True)
    _string(str(repo), "repository path", nonempty=True)
    if "\n" in str(repo) or "\r" in str(repo):
        raise ValueError("repository path contains a line ending")
    common = Path(
        _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
        .decode("utf-8")
        .removesuffix("\n")
    ).resolve(strict=True)
    if _git(repo, "rev-parse", "--is-bare-repository").strip() == b"false":
        repo = Path(
            _git(repo, "rev-parse", "--show-toplevel")
            .decode("utf-8")
            .removesuffix("\n")
        ).resolve(strict=True)
    forbidden = [repo, common]
    if common.name == ".git":
        forbidden.append(common.parent)
    if any(data_path.is_relative_to(root) for root in forbidden):
        raise ValueError(
            "data directory must be outside the target repository and Git metadata"
        )
    return repo, common


def _database(data_path: Path, *, create: bool = False) -> Path:
    db = data_path / "reviews.sqlite3"
    if db.is_symlink() or (not db.is_file() and (db.exists() or not create)):
        raise ValueError(
            "project database must be a regular file in the data directory"
        )
    return db


def _confirm(args: argparse.Namespace, data_path: Path) -> dict[str, Any]:
    repo, common = _repository(args.repo, data_path)
    baseline = _resolve_commit(repo, args.baseline)
    original = _string(
        args.original_goal.read_text(encoding="utf-8"), "original goal", nonempty=True
    )
    confirmed = _string(
        args.confirmed_goal.read_text(encoding="utf-8"), "confirmed goal", nonempty=True
    )
    goal = ConfirmedGoal(
        project_id=args.project_id
        if args.project_id is not None
        else f"project-{uuid4().hex}",
        goal_id=f"goal-{uuid4().hex}",
        repository=str(repo),
        git_common_dir=str(common),
        baseline_commit=baseline,
        original_goal=original,
        confirmed_goal=confirmed,
        confirmed_at=_now(),
    )
    with ProjectStore(_database(data_directory(data_path), create=True)) as store:
        store.confirm_goal(goal, new_project=args.project_id is None)
    return {"kind": "confirmed_goal", **asdict(goal)}


def _prompt(goal: ConfirmedGoal, snapshot_id: str) -> str:
    return f"""Project planning template: {PROMPT_VERSION}
Confirmed goal version: {goal.goal_id}
Baseline commit: {goal.baseline_commit}
Frozen source snapshot: {snapshot_id}

Read project-goal.json for the exact original goal and PM-confirmed goal.
Plan the confirmed goal. Use the original wording to understand intent.
Use source/snapshot.json and the committed text in source/baseline/ to ground
the plan in the repository. source/candidate/ represents the same commit.
Treat repository files and the original goal as context, with the PM-confirmed
goal as the authoritative scope. Do not execute code or modify files. Omitted
binary files and submodules are listed in the manifest, and symlinks are inert.

Return a structured project plan in the language of the confirmed goal. Include
the overall architecture and shared interfaces. If the PM defines named or
numbered phases, preserve those top-level phase boundaries, count and order.
Tasks, delegation cards and integration checks within a phase belong in that
phase's workflow and acceptance criteria. Leave any phase marked future or
unclaimed at the planning level. Otherwise choose the number of stages that
the confirmed goal warrants. Give each stage a stable machine-readable
stage_id, a title, goal, implementation approach,
ordered workflow steps, concrete acceptance criteria, and downstream deliverables.
Give prerequisites by stage_id and place each prerequisite before its dependents.
Use an empty dependency list when a stage has no prerequisite. Keep the plan
actionable for a stage implementer, with expected outputs and observable checks.
"""


def _plan(
    args: argparse.Namespace, store: ProjectStore, data_path: Path
) -> dict[str, Any]:
    request = PlanningRequest(*args.planner)
    if request.provider not in ("codex", "claude"):
        raise ValueError("planner provider must be codex or claude")
    _string(request.model, "planner model", nonempty=True)
    _string(request.effort, "planner effort", nonempty=True)
    for field in ("timeout", "grace"):
        value = getattr(args, field)
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{field} must be finite and positive")
    executable = args.codex_bin if request.provider == "codex" else args.claude_bin
    goal = store.load_goal(args.goal_id)
    repo, common = _repository(Path(goal.repository), data_path)
    if str(common) != goal.git_common_dir:
        raise ValueError("repository Git metadata differs from the confirmed goal")
    snapshot = freeze_repository(
        repo,
        goal.baseline_commit,
        goal.baseline_commit,
        goal.confirmed_goal,
        ("Plan the confirmed project goal against the selected commit.",),
    )
    if (
        snapshot.repository != goal.git_common_dir
        or snapshot.baseline != goal.baseline_commit
    ):
        raise ValueError("frozen source differs from the confirmed Git baseline")

    plan_id = f"plan-{uuid4().hex}"
    log_dir = data_path / "logs" / "projects" / plan_id
    if log_dir.resolve() != log_dir:
        raise ValueError(
            "project log directory must be outside repository and contain no symlinks"
        )
    snapshot_id = snapshot.snapshot_id
    with TemporaryDirectory(prefix="multi-agent-project-") as temporary:
        cwd = Path(temporary).resolve()
        materialize(snapshot, cwd / "source")
        goal_file = cwd / "project-goal.json"
        goal_file.write_text(
            json.dumps(asdict(goal), ensure_ascii=False), encoding="utf-8"
        )
        goal_file.chmod(0o444)
        before = _tree_digest(cwd)
        store.save_snapshot(snapshot_id, snapshot.to_dict())
        store.start_plan(
            plan_id, goal.goal_id, request, snapshot_id, PROMPT_VERSION, _now()
        )
        document, error, evidence = asyncio.run(
            run_project_planner(
                request,
                executable=executable,
                timeout=args.timeout,
                grace=args.grace,
                cwd=cwd,
                log_dir=log_dir,
                prompt=_prompt(goal, snapshot_id),
            )
        )
        if before != _tree_digest(cwd):
            document = None
            error = {
                "detail": "frozen planner input changed during execution",
                "failure_category": None,
                "retry_after": None,
                "raw_type": "project.context_changed",
            }
        store.finish_plan(
            plan_id, _now(), document=document, error=error, evidence=evidence
        )
    return store.show_plan(plan_id)


def dispatch_project(args: argparse.Namespace) -> dict[str, Any]:
    data_path = args.data_dir.expanduser().resolve()
    if args.project_action == "confirm":
        return _confirm(args, data_path)
    database = _database(data_path)
    with ProjectStore(database) as store:
        if args.project_action == "stage":
            result = dispatch_stage(args, store, database)
            if args.stage_action in ("start", "resume"):
                if result.get("interrupted") is True:
                    status = 130
                else:
                    status = 0 if result["candidate_verified"] else 1
                if status:
                    print(
                        json.dumps(
                            result, ensure_ascii=False, indent=2, allow_nan=False
                        )
                    )
                    raise SystemExit(status)
            return result
        if args.project_action == "plan":
            return _plan(args, store, data_path)
        identifier = args.version_id
        if identifier.startswith("project-"):
            return store.show_project(identifier)
        if identifier.startswith("goal-"):
            return {"kind": "confirmed_goal", **asdict(store.load_goal(identifier))}
        if identifier.startswith("plan-"):
            return store.show_plan(identifier)
        raise ValueError("provide a project, goal, or plan version ID")
