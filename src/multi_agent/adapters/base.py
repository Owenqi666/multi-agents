"""Async adapter contract and verified runtime mappings.

Protocol snapshots: Codex 0.169.0 and Claude Code 2.1.325. Local recordings from
2026-11-15 live under tests/fixtures/runtime/. Their precise scope and remaining
unknowns are in .agents/skills/runtime-protocols/references/.

Codex uses initialize, account/read, model/list, thread/start, turn/start and
turn/interrupt. The recorded sandbox rejected writes. turn/completed.turn.status
is the terminal; thread/start model/effort fields are request echoes, NOT actual
configuration evidence. Runtime effective model and effort remain unconfirmed.
Claude uses -p with --safe-mode, Read,Grep,Glob, explicit --model and --effort,
stream-json, and optional --json-schema. Recorded result.is_error and
terminal_reason determine success, including API errors with subtype success
and cancellation with exit code zero. modelUsage reports actual model evidence;
actual effort remains unconfirmed. No Claude model catalog is established.

check_environment is separate from model execution; auth status does not prove
credentials will remain valid for the next turn. Runtime adapters make each
session single-use. SessionStarted carries only observed runtime metadata;
result returns the latest normalized terminal and process exit separately.
"""

from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from multi_agent.core.events import AdapterEvent, Completed, Error, ProcessExited


@dataclass(frozen=True, slots=True)
class EnvironmentInfo:
    cli_version: str | None = None
    logged_in: bool | None = None
    models: tuple[str, ...] | None = None
    model_efforts: Mapping[str, tuple[str, ...]] | None = None

    def __post_init__(self) -> None:
        if self.model_efforts is not None:
            object.__setattr__(
                self, "model_efforts", MappingProxyType(dict(self.model_efforts))
            )


class Permission(StrEnum):
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"


@dataclass(frozen=True, slots=True)
class SessionConfig:
    model: str
    effort: str
    permission: Permission
    cwd: Path


@dataclass(frozen=True, slots=True, eq=False)
class SessionHandle:
    identifier: str


@dataclass(frozen=True, slots=True, eq=False)
class TurnHandle:
    identifier: str
    session: SessionHandle


@dataclass(frozen=True, slots=True)
class TurnResult:
    terminal: Completed | Error | None = None
    process_exit: ProcessExited | None = None


class Adapter(Protocol):
    async def check_environment(self) -> EnvironmentInfo: ...

    async def start_session(self, config: SessionConfig) -> SessionHandle: ...

    async def submit_turn(self, session: SessionHandle, text: str) -> TurnHandle: ...

    def events(self, turn: TurnHandle) -> AsyncIterator[AdapterEvent]: ...

    async def cancel(
        self, turn: TurnHandle, *, on_accepted: Callable[[], None] | None = None
    ) -> bool:
        """Accept a new cancellation before any observed terminal or exit.

        The decision and optional synchronous on_accepted callback run before
        yielding to I/O, so callers can persist intent even if later I/O raises.
        True reports acceptance, not confirmed stopping; False includes repeated
        or late requests, which never invoke the callback.
        Callback exceptions and unrecovered I/O failures propagate. Runtime
        adapters may recover a failed send by terminating the owned process;
        neither acceptance nor the callback is evidence of that process exit.
        Callers must await this decision before persisting a terminal, and must
        still consume the complete event stream and validate the report.
        """
        ...

    async def result(self, turn: TurnHandle) -> TurnResult: ...
