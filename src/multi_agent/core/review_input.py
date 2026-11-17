"""Immutable executed review inputs, separate from legacy supplied evidence."""

import hashlib
from dataclasses import dataclass
from typing import Any, cast

from .events import JSONValue, freeze_json
from .snapshot import FrozenSnapshot
from .task_execution import task_execution_hash, task_execution_value
from .task_state import TaskInput
from .task_test_events import TaskTestExecutionPrepared


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutedLog:
    content: bytes

    @property
    def byte_count(self) -> int:
        return len(self.content)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutedCommand:
    manifest: JSONValue
    stdout: ExecutedLog
    stderr: ExecutedLog

    def __post_init__(self) -> None:
        object.__setattr__(self, "manifest", freeze_json(self.manifest))


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutedSuite:
    task_input: TaskInput
    prepared: TaskTestExecutionPrepared
    test_attempt: int
    test_history_length: int
    test_history_hash: str
    commands: tuple[ExecutedCommand, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutedReviewInput:
    source: FrozenSnapshot
    executed_suite: ExecutedSuite
    version: int = 2
    kind: str = "executed_task_review"

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], task_execution_value(self))

    @property
    def snapshot_id(self) -> str:
        return task_execution_hash(self.to_dict())


type ReviewInput = FrozenSnapshot | ExecutedReviewInput


def review_source(value: ReviewInput) -> FrozenSnapshot:
    return value.source if isinstance(value, ExecutedReviewInput) else value
