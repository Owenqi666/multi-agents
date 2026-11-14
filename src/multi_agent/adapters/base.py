"""Async adapter contract and the recorded runtime operation mappings.

The mappings below use the runtime-protocols reference tables and the checked-in
protocol snapshots (Codex 0.169.0, Claude Code 2.1.325). Status labels describe the
available evidence. Runtime event ordering and effective permissions remain
未确认 until live verification resumes.

| Operation | Codex App Server | Claude headless |
| --- | --- | --- |
| check_environment | CLI version recorded 已验证; model/list and its model entries 快照; login-check mapping 未确认. | CLI version recorded 已验证; login-check and model-catalog mappings 未确认. |
| start_session | thread/start with model, cwd and sandbox; response reports model and reasoningEffort 快照. | No separate start operation is established; -p, --model, --effort and --tools 快照; system/init reports session metadata 文档; read-only enforcement 未确认. |
| submit_turn | turn/start with threadId and input, plus explicit model, effort and sandboxPolicy 快照. | -p/--print and --input-format 快照; short print-mode connectivity 已验证; streaming turn submission behavior 未确认. |
| events | turn/started, item notifications, thread/tokenUsage/updated and turn/completed 快照; live ordering 未确认. | --output-format=stream-json 快照; system/init and system/api_retry 文档; full event fields and ordering 未确认. |
| cancel | turn/interrupt(threadId, turnId) 快照; wait for the turn terminal 文档; local behavior 未确认. | SIGINT followed by process termination after a grace period 文档; terminal output and exit codes 未确认. |
| result | turn/completed carries the turn status and error 快照; record observed process exit separately. | --output-format=json 快照; modelUsage and total_cost_usd 文档; full result decoding 未确认; record observed process exit separately. |

Sources within this repository are
.agents/skills/runtime-protocols/references/codex-app-server.md and
.agents/skills/runtime-protocols/references/claude-headless.md. Method names were
checked against protocol/codex/schema/{ClientRequest,ServerNotification}.json;
Claude option names were checked against protocol/claude/help.txt.

Handles identify adapter-owned resources. A SessionStarted event carries the
runtime-reported session identifier. A result is the latest observed terminal
and process exit, with either field empty until its event has arrived.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from multi_agent.core.events import AdapterEvent, Completed, Error, ProcessExited


@dataclass(frozen=True, slots=True)
class EnvironmentInfo:
    cli_version: str | None = None
    logged_in: bool | None = None
    models: tuple[str, ...] | None = None


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

    async def cancel(self, turn: TurnHandle) -> None: ...

    async def result(self, turn: TurnHandle) -> TurnResult: ...
