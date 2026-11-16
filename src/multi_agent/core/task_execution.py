"""Trusted managed-task preparation values; no filesystem or runtime access."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from typing import TYPE_CHECKING, Any

from .dispatch import ImplementationChoice

if TYPE_CHECKING:
    from .task_events import TaskEnvelope, TaskExecutionPrepared
    from .task_state import TaskAttempt, TaskState


@dataclass(frozen=True, slots=True)
class TaskExecutionPolicy:
    provider: str
    executable: str
    executable_identity: tuple[int, int]
    executable_sha256: str
    max_attempts: int
    timeout: float
    grace: float


@dataclass(frozen=True, slots=True)
class TaskDispatchPolicy:
    task_type: str | None = None
    requested: ImplementationChoice | None = None
    fallbacks: tuple[ImplementationChoice, ...] = ()


@dataclass(frozen=True, slots=True)
class TaskExecutionContext:
    path: str
    identity: tuple[int, int]
    token: str
    manifest_sha256: str


def task_execution_value(value: Any) -> Any:
    """Encode owned control bytes explicitly, without changing legacy hashes."""
    if isinstance(value, bytes):
        return {"hex": value.hex()}
    if is_dataclass(value) and not isinstance(value, type):
        result = {
            field.name: task_execution_value(getattr(value, field.name))
            for field in fields(value)
        }
        if hasattr(value, "kind"):
            result["kind"] = value.kind
        return result
    if isinstance(value, Mapping):
        return {key: task_execution_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [task_execution_value(item) for item in value]
    return value


def task_execution_json(value: Any) -> str:
    return json.dumps(
        task_execution_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def task_execution_hash(value: Any) -> str:
    return hashlib.sha256(task_execution_json(value).encode("utf-8")).hexdigest()


def prepared_for_task(attempt: TaskAttempt) -> TaskExecutionPrepared | None:
    from .task_events import TaskExecutionPrepared

    return next(
        (event for event in attempt.events if isinstance(event, TaskExecutionPrepared)),
        None,
    )


def reserve_task_execution(
    state: TaskState, started: TaskEnvelope, prepared: TaskEnvelope
) -> TaskState:
    """Fold both reservation events locally; publish no intermediate state."""
    from .state import Disposition
    from .task_events import TaskAttemptStarted, TaskExecutionPrepared
    from .transitions import apply_task

    if not isinstance(started.event, TaskAttemptStarted) or not isinstance(
        prepared.event, TaskExecutionPrepared
    ):
        raise TypeError("task reservation requires Started and Prepared envelopes")
    if not started.event.managed or started.attempt != prepared.attempt:
        raise ValueError("reservation must identify one managed task attempt")
    updated, start_disposition = apply_task(state, started)
    reserved, prepare_disposition = apply_task(updated, prepared)
    if (
        start_disposition != Disposition.APPLIED
        or prepare_disposition != Disposition.APPLIED
    ):
        raise ValueError("reservation must apply both current task events")
    return reserved
