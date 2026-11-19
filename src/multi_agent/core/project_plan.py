"""Versioned project inputs and a single representation of the plan document."""

from dataclasses import dataclass
from typing import NotRequired, TypedDict


@dataclass(frozen=True, slots=True)
class ConfirmedGoal:
    project_id: str
    goal_id: str
    repository: str
    git_common_dir: str
    baseline_commit: str
    original_goal: str
    confirmed_goal: str
    confirmed_at: str


class StagePlan(TypedDict):
    stage_id: str
    title: str
    goal: str
    approach: str
    workflow: list[str]
    depends_on: list[str]
    acceptance_criteria: list[str]
    deliverables: list[str]


class ProjectPlan(TypedDict):
    title: str
    architecture: str
    shared_interfaces: list[str]
    stages: list[StagePlan]


def _items(values: list[str]) -> str:
    return "\n".join("- " + value.replace("\n", "\n  ") for value in values)


def render_markdown(plan: ProjectPlan) -> str:
    """Render the same ordered stage records consumed by downstream commands."""
    sections = [
        "# " + " ".join(plan["title"].split()),
        "## 整体架构\n\n" + plan["architecture"],
    ]
    if plan["shared_interfaces"]:
        sections.append("## 共用接口\n\n" + _items(plan["shared_interfaces"]))
    for stage in plan["stages"]:
        identifier = stage["stage_id"]
        title = " ".join(stage["title"].split())
        sections.append(
            "\n\n".join(
                (
                    f"## {identifier} · {title}",
                    "### 目标\n\n" + stage["goal"],
                    "### 实现方向\n\n" + stage["approach"],
                    "### 流程\n\n" + _items(stage["workflow"]),
                    "### 依赖\n\n" + (_items(stage["depends_on"]) or "无"),
                    "### 验收\n\n" + _items(stage["acceptance_criteria"]),
                    "### 交付物\n\n" + _items(stage["deliverables"]),
                )
            )
        )
    return "\n\n".join(sections) + "\n"


class StageIntegrationProof(TypedDict):
    integration_id: str
    candidate: str
    test_attempt: int
    test_evidence_hash: str
    review_round_id: str
    review_snapshot_id: str


class StageDependency(TypedDict):
    stage_id: str
    stage_run_id: str
    task_run_id: NotRequired[str]
    implementation_attempt: NotRequired[int]
    candidate: str
    test_attempt: int
    review_round_id: str
    integration: NotRequired[StageIntegrationProof]


@dataclass(frozen=True, slots=True)
class StageRun:
    stage_run_id: str
    plan_id: str
    stage_id: str
    planning_run_id: str
    mode: str
    execution_baseline: str
    dependencies: tuple[StageDependency, ...]
    created_at: str
    dispatch_error: str | None = None
