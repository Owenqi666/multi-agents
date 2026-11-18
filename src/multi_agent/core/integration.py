"""Frozen sources and ordered outcomes for one integration."""

from dataclasses import dataclass

from .planning import DependencyReceipt
from .task_state import TaskInput
from .workspaces import WorkspaceDelivery


@dataclass(frozen=True, slots=True)
class IntegrationMember:
    task_run_id: str
    planning_run_id: str
    attempt: int
    task_input: TaskInput
    delivery: WorkspaceDelivery
    external_dependencies: tuple[DependencyReceipt, ...]


@dataclass(frozen=True, slots=True)
class IntegrationCandidate:
    ref: str
    commit: str
    tree: str


@dataclass(frozen=True, slots=True)
class IntegrationConflict:
    paths: tuple[str, ...]


type IntegrationResult = IntegrationCandidate | IntegrationConflict


@dataclass(frozen=True, slots=True)
class IntegrationRecord:
    integration_id: str
    repository: str
    target_ref: str
    target_baseline: str
    members: tuple[IntegrationMember, ...]
    applied_count: int
    result: IntegrationResult

    def __post_init__(self) -> None:
        if (
            isinstance(self.result, IntegrationCandidate)
            and not 0 < self.applied_count == len(self.members)
        ) or (
            isinstance(self.result, IntegrationConflict)
            and not 0 <= self.applied_count < len(self.members)
        ):
            raise ValueError(
                "integration outcome differs from its applied member count"
            )


@dataclass(frozen=True, slots=True)
class IntegrationProgress:
    member_count: int
    applied_count: int = 0
    result: IntegrationResult | None = None


@dataclass(frozen=True, slots=True)
class IntegrationMemberApplied:
    index: int


@dataclass(frozen=True, slots=True)
class IntegrationMemberConflicted:
    index: int
    conflict: IntegrationConflict


type IntegrationEvent = (
    IntegrationMemberApplied | IntegrationMemberConflicted | IntegrationCandidate
)
