"""Provider-independent events and their routing information."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import ClassVar

type JSONScalar = str | int | float | bool | None
type JSONValue = JSONScalar | Mapping[str, JSONValue] | Sequence[JSONValue]
type JSONPayload = JSONScalar | dict[str, JSONPayload] | list[JSONPayload]


def freeze_json(value: JSONValue) -> JSONValue:
    """Snapshot a validated JSON value. Use thaw_json before JSON encoding."""
    if value is None or isinstance(value, (str, int, float)):
        return value
    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze_json(item) for key, item in value.items()})
    return tuple(freeze_json(item) for item in value)


def thaw_json(value: JSONValue) -> JSONPayload:
    """Copy an immutable payload into plain JSON-serializable containers."""
    if value is None or isinstance(value, (str, int, float)):
        return value
    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    return [thaw_json(item) for item in value]


class FailureCategory(StrEnum):
    AUTH_EXPIRED = "auth_expired"
    RATE_LIMITED = "rate_limited"
    MODEL_UNAVAILABLE = "model_unavailable"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    OUT_OF_SCOPE_PATCH = "out_of_scope_patch"
    TESTS_FAILED = "tests_failed"
    INTEGRATION_CONFLICT = "integration_conflict"


@dataclass(frozen=True, slots=True)
class AttemptStarted:
    kind: ClassVar[str] = "attempt_started"


@dataclass(frozen=True, slots=True)
class CancelRequested:
    kind: ClassVar[str] = "cancel_requested"


@dataclass(frozen=True, slots=True, kw_only=True)
class _RuntimeEvent:
    raw_type: str | None = None


@dataclass(frozen=True, slots=True)
class SessionStarted(_RuntimeEvent):
    kind: ClassVar[str] = "session_started"
    session_id: str
    reported_model: str | None = None
    reported_effort: str | None = None


@dataclass(frozen=True, slots=True)
class Message(_RuntimeEvent):
    kind: ClassVar[str] = "message"
    text: str


@dataclass(frozen=True, slots=True)
class Usage(_RuntimeEvent):
    kind: ClassVar[str] = "usage"
    counts: Mapping[str, int | float]

    def __post_init__(self) -> None:
        object.__setattr__(self, "counts", MappingProxyType(dict(self.counts)))


@dataclass(frozen=True, slots=True)
class Completed(_RuntimeEvent):
    kind: ClassVar[str] = "completed"
    result: JSONValue
    reported_model: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "result", freeze_json(self.result))


@dataclass(frozen=True, slots=True)
class Error(_RuntimeEvent):
    kind: ClassVar[str] = "error"
    detail: str
    failure_category: FailureCategory | None = None
    retry_after: str | None = None


@dataclass(frozen=True, slots=True)
class ProcessExited(_RuntimeEvent):
    kind: ClassVar[str] = "process_exited"
    exit_code: int


type AdapterEvent = SessionStarted | Message | Usage | Completed | Error | ProcessExited
type ReviewEvent = AttemptStarted | CancelRequested | AdapterEvent


@dataclass(frozen=True, slots=True)
class Envelope:
    seat: str
    attempt: int
    event: ReviewEvent
    occurred_at: str | None = None
