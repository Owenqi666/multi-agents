"""Read-only, one-turn CLI adapters using the recorded local protocols.

Constructing an adapter and start_session do not launch a CLI. submit_turn owns
one fresh process until it exits; callers consume events and may call cancel.
check_environment is explicit and raises RuntimeFailure (with .event) on a
boundary failure. Its model catalog is availability evidence, not model-use
proof. All raw traffic, including unknown events/reroutes, is retained in a new
redacted JSONL file under log_dir; callers must place logs outside the target
repository. Completed.result is JSON decoded here; the application validates its
report schema before persisting a successful attempt.

Sources: protocol/codex/schema/{ClientRequest,ServerRequest}.json, v1/
InitializeParams.json, v2/{ThreadStartParams,TurnStartParams,TurnInterruptParams,
ModelListParams,ModelListResponse}.json, protocol/claude/help.txt, and the recorded
fixtures under tests/fixtures/runtime/. No runtime config overrides are invented.
Codex's read-only sandbox is verified, but user MCP/skills/config isolation is
NOT verified: logs preserve instructionSources and MCP notifications. The caller
supplies a neutral review directory, never the mutable target checkout. Claude
uses the verified safe-mode tool restriction and removes both API credential
variables to retain the user's subscription login.
"""

import asyncio
import json
import math
import os
import signal
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from multi_agent.core.events import (
    AdapterEvent,
    Completed,
    Error,
    FailureCategory,
    ProcessExited,
    SessionStarted,
)

from .base import (
    Adapter,
    EnvironmentInfo,
    Permission,
    SessionConfig,
    SessionHandle,
    TurnHandle,
    TurnResult,
)
from .probe.process import Process, SpawnFailed
from .probe.recording import Recorder, Redactor
from .runtime_decode import (
    CodexDecoder,
    RuntimeFailure,
    claude_events,
    object_value,
    string_value,
)


async def _read(process: Process) -> dict[str, Any] | str | None:
    value = await process.read()
    if value is None:
        # The transport can observe OS exit before its reader has drained the
        # last stdout bytes. Always finish its bounded drain before declaring EOF.
        await process.wait()
        value = await process.read()
    return value


class _Rpc:
    def __init__(self, process: Process) -> None:
        self.process = process
        self.next_id = 0
        self.deferred: deque[dict[str, Any]] = deque()

    async def receive(self) -> dict[str, Any] | None:
        while True:
            value = await _read(self.process)
            if value is None:
                return None
            message = object_value(value, "JSON-RPC message")
            if "method" in message and "id" in message:
                # Never grant tool or filesystem permissions. Known approvals
                # have a schema-defined decline; all other requests are rejected.
                if message["method"] in (
                    "item/commandExecution/requestApproval",
                    "item/fileChange/requestApproval",
                ):
                    response = {"id": message["id"], "result": {"decision": "decline"}}
                else:
                    response = {
                        "id": message["id"],
                        "error": {
                            "code": -32601,
                            "message": "Read-only review client does not support this request",
                        },
                    }
                await self.process.send(response)
                continue
            return message

    async def send(self, method: str, params: dict[str, Any]) -> int:
        self.next_id += 1
        if not await self.process.send(
            {"id": self.next_id, "method": method, "params": params}
        ):
            raise ValueError("runtime closed stdin before request")
        return self.next_id

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        identifier = await self.send(method, params)
        while (message := await self.receive()) is not None:
            if "method" not in message and message.get("id") == identifier:
                if "error" in message:
                    error = object_value(message["error"], "RPC error")
                    raise RuntimeFailure(
                        Error(
                            string_value(error.get("message"), "RPC error.message"),
                            raw_type=method,
                        )
                    )
                return object_value(message.get("result"), "RPC result")
            self.deferred.append(message)
        raise ValueError(f"runtime exited before {method} response")

    async def initialize(self) -> None:
        await self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "multi-agent-review",
                    "title": "Multi-agent review",
                    "version": "0.1.0",
                }
            },
        )
        if not await self.process.send({"method": "initialized"}):
            raise ValueError("runtime exited during initialization")


@dataclass
class _Turn:
    config: SessionConfig
    prompt: str
    queue: asyncio.Queue[AdapterEvent | None] = field(default_factory=asyncio.Queue)
    task: asyncio.Task[None] | None = None
    process: Process | None = None
    rpc: _Rpc | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    cancelled: bool = False
    cancel_sent: bool = False
    reader_started: bool = False
    observed_terminal: Completed | Error | None = None
    terminal: Completed | Error | None = None
    exit_event: ProcessExited | None = None
    cancel_task: asyncio.Task[None] | None = None
    deadline: asyncio.Timeout | None = None


class RuntimeAdapter(Adapter):
    def __init__(
        self,
        provider: str,
        *,
        executable: str | None = None,
        timeout: float = 180,
        grace: float = 2,
        log_dir: Path,
        output_schema: dict[str, Any] | None = None,
    ) -> None:
        if provider not in ("codex", "claude"):
            raise ValueError("provider must be codex or claude")
        if (
            not math.isfinite(timeout)
            or timeout <= 0
            or not math.isfinite(grace)
            or grace <= 0
        ):
            raise ValueError("timeout and grace must be finite positive durations")
        self.provider = provider
        self.executable = executable or provider
        self.timeout = timeout
        self.grace = grace
        self.log_dir = log_dir.resolve()
        self.output_schema = (
            json.loads(json.dumps(output_schema, allow_nan=False))
            if output_schema is not None
            else None
        )
        self._sessions: dict[SessionHandle, SessionConfig] = {}
        self._used: set[SessionHandle] = set()
        self._turns: dict[TurnHandle, _Turn] = {}

    def _environment(self) -> dict[str, str]:
        env = dict(os.environ)
        if self.provider == "claude":
            env.pop("ANTHROPIC_API_KEY", None)
            env.pop("ANTHROPIC_AUTH_TOKEN", None)
        return env

    def _recorder(self, identifier: str, cwd: Path) -> Recorder:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        redactor = Redactor()
        redactor.add_workdir(cwd)
        return Recorder(self.log_dir / f"{self.provider}-{identifier}.jsonl", redactor)

    async def start_session(self, config: SessionConfig) -> SessionHandle:
        if config.permission != Permission.READ_ONLY:
            raise ValueError("review adapters only support read-only sessions")
        if not config.cwd.is_dir():
            raise ValueError("review working directory does not exist")
        if self.log_dir.is_relative_to(config.cwd.resolve()):
            raise ValueError("raw logs must be outside the review working directory")
        string_value(config.model, "requested model")
        string_value(config.effort, "requested effort")
        session = SessionHandle(uuid4().hex)
        self._sessions[session] = config
        return session

    async def submit_turn(self, session: SessionHandle, text: str) -> TurnHandle:
        config = self._sessions[session]
        if session in self._used:
            raise ValueError("review sessions accept one turn; start a fresh session")
        self._used.add(session)
        handle = TurnHandle(uuid4().hex, session)
        state = _Turn(config, text)
        self._turns[handle] = state
        state.task = asyncio.create_task(self._run(handle, state))
        return handle

    def _observe(self, state: _Turn, event: AdapterEvent) -> None:
        if isinstance(event, (Completed, Error)):
            if state.observed_terminal is not None:
                raise ValueError("runtime emitted multiple terminal results")
            state.observed_terminal = event
            # Only model execution uses the turn deadline. Shutdown and final
            # decoding have the transport's own bounded grace period.
            if state.deadline is not None:
                state.deadline.reschedule(None)
        else:
            state.queue.put_nowait(event)

    async def _spawn(
        self, argv: list[str], cwd: Path, recorder: Recorder, *, pipe: bool = False
    ) -> Process:
        return await Process.start(
            self.provider,
            argv,
            cwd,
            recorder,
            piped_stdin=pipe,
            grace=self.grace,
            env=self._environment(),
            start_new_session=True,
        )

    async def _run(self, handle: TurnHandle, state: _Turn) -> None:
        recorder: Recorder | None = None
        failure: Error | None = None
        try:
            recorder = self._recorder(handle.identifier, state.config.cwd)
            recorder.emit(
                "requested",
                model=state.config.model,
                effort=state.config.effort,
                permission=state.config.permission,
            )
            if state.cancelled:
                failure = Error(
                    "cancelled before process launch", FailureCategory.CANCELLED
                )
            else:
                async with asyncio.timeout(self.timeout) as deadline:
                    state.deadline = deadline
                    if self.provider == "codex":
                        await self._codex(state, recorder)
                    else:
                        await self._claude(state, recorder)
        except TimeoutError:
            failure = Error("runtime deadline exceeded", FailureCategory.TIMEOUT)
        except RuntimeFailure as error:
            failure = error.event
        except asyncio.CancelledError:
            state.cancelled = True
            failure = Error("runtime task cancelled", FailureCategory.CANCELLED)
        except (OSError, SpawnFailed, ValueError, TypeError, KeyError) as error:
            failure = Error(
                f"runtime boundary failure: {type(error).__name__}: {error}"
            )
        finally:
            try:
                if state.cancel_task is not None:
                    state.cancel_task.cancel()
                    await asyncio.gather(state.cancel_task, return_exceptions=True)
                if state.process is not None:
                    try:
                        await state.process.terminate(self.grace)
                    finally:
                        code = state.process.child.returncode
                        if code is not None:
                            state.exit_event = ProcessExited(
                                code, raw_type="process_exit"
                            )
            except (OSError, ValueError, RuntimeError) as error:
                failure = Error(f"runtime cleanup failure: {error}")
            finally:
                # Cancellation is only confirmed here, after process termination,
                # unless the runtime itself reported a cancellation terminal.
                if state.cancelled and state.observed_terminal is None:
                    failure = Error(
                        "process stopped after cancellation", FailureCategory.CANCELLED
                    )
                terminal = (
                    failure
                    or state.observed_terminal
                    or Error("runtime exited without a terminal result")
                )
                state.terminal = terminal
                state.queue.put_nowait(terminal)
                if state.exit_event is not None:
                    state.queue.put_nowait(state.exit_event)
                state.queue.put_nowait(None)
                if recorder is not None:
                    recorder.close()

    async def _codex(self, state: _Turn, recorder: Recorder) -> None:
        process = await self._spawn(
            [self.executable, "app-server"], state.config.cwd, recorder, pipe=True
        )
        state.process = process
        state.rpc = rpc = _Rpc(process)
        if state.cancelled:
            await self._send_cancel(state)
        await rpc.initialize()
        response = await rpc.request(
            "thread/start",
            {
                "model": state.config.model,
                "cwd": str(state.config.cwd),
                "sandbox": "read-only",
                "approvalPolicy": "never",
                "ephemeral": True,
            },
        )
        state.thread_id = string_value(
            object_value(response.get("thread"), "thread").get("id"), "thread.id"
        )
        self._observe(state, SessionStarted(state.thread_id, raw_type="thread/start"))
        if state.cancelled:
            raise RuntimeFailure(
                Error("cancelled before turn submission", FailureCategory.CANCELLED)
            )
        params: dict[str, Any] = {
            "threadId": state.thread_id,
            "input": [{"type": "text", "text": state.prompt}],
            "model": state.config.model,
            "effort": state.config.effort,
            "sandboxPolicy": {"type": "readOnly"},
            "approvalPolicy": "never",
            "cwd": str(state.config.cwd),
        }
        if self.output_schema is not None:
            params["outputSchema"] = self.output_schema
        response = await rpc.request("turn/start", params)
        state.turn_id = string_value(
            object_value(response.get("turn"), "turn").get("id"), "turn.id"
        )
        decoder = CodexDecoder(
            state.thread_id, state.turn_id, structured=self.output_schema is not None
        )
        if state.cancelled:
            await self._send_cancel(state)
        while state.observed_terminal is None:
            message = rpc.deferred.popleft() if rpc.deferred else await rpc.receive()
            if message is None:
                break
            for event in decoder.decode(message):
                self._observe(state, event)
        # Stop after this turn, then decode the bounded final drain as well.
        # A contradictory second terminal must not be hidden by the first one.
        await process.close_finished(self.grace)
        while True:
            message = rpc.deferred.popleft() if rpc.deferred else await rpc.receive()
            if message is None:
                break
            for event in decoder.decode(message):
                self._observe(state, event)

    async def _claude(self, state: _Turn, recorder: Recorder) -> None:
        argv = [
            self.executable,
            "-p",
            state.prompt,
            "--model",
            state.config.model,
            "--effort",
            state.config.effort,
            "--output-format",
            "stream-json",
            "--verbose",
            "--safe-mode",
            "--tools",
            "Read,Grep,Glob",
            "--permission-prompts",
            "none",
            "--no-session-persistence",
        ]
        if self.output_schema is not None:
            argv += ["--json-schema", json.dumps(self.output_schema)]
        recorder.emit(
            "environment",
            removed_variables=["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"],
        )
        state.process = process = await self._spawn(argv, state.config.cwd, recorder)
        if state.cancelled:
            await self._send_cancel(state)
        retry_after: str | None = None
        while (value := await _read(process)) is not None:
            message = object_value(value, "stream message")
            if message.get("type") == "rate_limit_event":
                info = object_value(message.get("rate_limit_info"), "rate_limit_info")
                reset = info.get("resetsAt")
                if isinstance(reset, float) and not math.isfinite(reset):
                    raise ValueError("rate-limit reset must be finite")
                if isinstance(reset, (str, int, float)) and not isinstance(reset, bool):
                    retry_after = str(reset)
            for event in claude_events(
                message,
                structured=self.output_schema is not None,
            ):
                if (
                    isinstance(event, Error)
                    and event.failure_category == FailureCategory.RATE_LIMITED
                ):
                    event = replace(event, retry_after=retry_after)
                self._observe(state, event)
            # A reported terminal is independent of a hung process shutdown.
            if state.observed_terminal is not None and not await process.wait_for_exit(
                self.grace
            ):
                await process.terminate(self.grace)

    async def _send_cancel(self, state: _Turn) -> None:
        process = state.process
        if process is None or state.cancel_sent or state.observed_terminal is not None:
            return
        if self.provider == "codex":
            if (
                state.rpc is not None
                and state.thread_id is not None
                and state.turn_id is not None
            ):
                await state.rpc.send(
                    "turn/interrupt",
                    {"threadId": state.thread_id, "turnId": state.turn_id},
                )
                state.cancel_sent = True
        else:
            process.signal(signal.SIGINT)
            state.cancel_sent = True
        if state.cancel_task is None:

            async def stop_after_grace() -> None:
                await asyncio.sleep(self.grace)
                await process.terminate(self.grace)

            state.cancel_task = asyncio.create_task(stop_after_grace())

    async def cancel(self, turn: TurnHandle) -> None:
        state = self._turns[turn]
        if state.terminal is not None or state.observed_terminal is not None:
            return
        state.cancelled = True
        try:
            await self._send_cancel(state)
        except (ValueError, OSError):
            # The reader records the exit/failure; cancellation still owns cleanup.
            if state.process is not None:
                await state.process.terminate(self.grace)

    async def events(self, turn: TurnHandle) -> AsyncIterator[AdapterEvent]:
        state = self._turns[turn]
        if state.reader_started:
            raise RuntimeError("this turn already has an event reader")
        state.reader_started = True
        while (event := await state.queue.get()) is not None:
            yield event
        if state.task is not None:
            await state.task

    async def result(self, turn: TurnHandle) -> TurnResult:
        state = self._turns[turn]
        return TurnResult(state.terminal, state.exit_event)

    async def _collect(self, argv: list[str], cwd: Path, recorder: Recorder) -> str:
        process = await self._spawn(argv, cwd, recorder)
        try:
            lines: list[str] = []
            while (value := await _read(process)) is not None:
                lines.append(json.dumps(value) if isinstance(value, dict) else value)
            if await process.wait_exited() != 0:
                raise ValueError("environment check exited unsuccessfully")
            return "\n".join(lines)
        finally:
            await process.terminate(self.grace)

    async def check_environment(self) -> EnvironmentInfo:
        """Record version/login/catalog separately; never starts a model turn."""
        try:
            with TemporaryDirectory(prefix="multi-agent-environment-") as directory:
                cwd = Path(directory)
                with self._recorder("environment-" + uuid4().hex, cwd) as recorder:
                    async with asyncio.timeout(self.timeout):
                        version = string_value(
                            (
                                await self._collect(
                                    [self.executable, "--version"], cwd, recorder
                                )
                            ).strip(),
                            "CLI version",
                        )
                        if self.provider == "claude":
                            auth = object_value(
                                json.loads(
                                    await self._collect(
                                        [self.executable, "auth", "status"],
                                        cwd,
                                        recorder,
                                    )
                                ),
                                "auth status",
                            )
                            logged_in = auth.get("loggedIn")
                            if not isinstance(logged_in, bool):
                                raise ValueError("auth loggedIn must be a boolean")
                            return EnvironmentInfo(version, logged_in, None)
                        process = await self._spawn(
                            [self.executable, "app-server"], cwd, recorder, pipe=True
                        )
                        try:
                            rpc = _Rpc(process)
                            await rpc.initialize()
                            auth = await rpc.request(
                                "account/read", {"refreshToken": False}
                            )
                            if "account" not in auth or (
                                auth["account"] is not None
                                and not isinstance(auth["account"], dict)
                            ):
                                raise ValueError("account/read has no account evidence")
                            models: list[str] = []
                            efforts: dict[str, tuple[str, ...]] = {}
                            params: dict[str, Any] = {"includeHidden": False}
                            cursors: set[str] = set()
                            while True:
                                page = await rpc.request("model/list", params)
                                if not isinstance(page.get("data"), list):
                                    raise TypeError("model/list.data must be an array")
                                for value in page["data"]:
                                    model = object_value(value, "model entry")
                                    name = string_value(model.get("id"), "model.id")
                                    levels = model.get("supportedReasoningEfforts")
                                    if not isinstance(levels, list):
                                        raise TypeError(
                                            "supportedReasoningEfforts must be an array"
                                        )
                                    if model.get("hidden") is not True:
                                        models.append(name)
                                        efforts[name] = tuple(
                                            string_value(
                                                object_value(level, "effort").get(
                                                    "reasoningEffort"
                                                ),
                                                "reasoningEffort",
                                            )
                                            for level in levels
                                        )
                                cursor = page.get("nextCursor")
                                if cursor is None:
                                    break
                                cursor = string_value(cursor, "model cursor")
                                if cursor in cursors or len(cursors) >= 100:
                                    raise ValueError(
                                        "model/list pagination did not terminate"
                                    )
                                cursors.add(cursor)
                                params["cursor"] = cursor
                            return EnvironmentInfo(
                                version,
                                auth["account"] is not None,
                                tuple(models),
                                efforts,
                            )
                        finally:
                            await process.close_finished(self.grace)
                            await process.terminate(self.grace)
        except RuntimeFailure:
            raise
        except TimeoutError as error:
            raise RuntimeFailure(
                Error("environment check timed out", FailureCategory.TIMEOUT)
            ) from error
        except (OSError, SpawnFailed, ValueError, TypeError, KeyError) as error:
            raise RuntimeFailure(
                Error(f"environment check failed: {type(error).__name__}: {error}")
            ) from error
