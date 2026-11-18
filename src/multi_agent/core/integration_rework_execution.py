"""Frozen actor policy and observed outcome of an integration repair."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from .dispatch import DispatchDecision
from .events import JSONValue, freeze_json
from .task_execution import TaskDispatchPolicy, TaskExecutionPolicy

if TYPE_CHECKING:
    from .integration_rework import IntegrationRepairDelta


@dataclass(frozen=True, slots=True)
class IntegrationReworkActor:
    policy: TaskExecutionPolicy
    dispatch_policy: TaskDispatchPolicy
    decision: DispatchDecision


@dataclass(frozen=True, slots=True, kw_only=True)
class IntegrationReworkOutcome:
    status: Literal["succeeded", "failed", "cancelled"]
    process_stopped: bool | None
    delta: IntegrationRepairDelta | None
    evidence: JSONValue
    reason: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", freeze_json(self.evidence))
