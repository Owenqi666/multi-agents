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
Codex uses a private HOME/CODEX_HOME and a pinned named permissions profile.
Effective configuration and instruction sources are checked before a model turn;
native no-model permission evidence is recorded separately from model acceptance.
The caller supplies a frozen review directory, never the mutable target checkout. Claude
uses the verified safe-mode tool restriction and removes both API credential
variables to retain the user's subscription login.
"""

import asyncio
import copy
import json
import math
import os
import shutil
import signal
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from multi_agent.core.events import (
    AdapterEvent,
    Completed,
    Error,
    FailureCategory,
    Message,
    ProcessExited,
    SessionStarted,
    thaw_json,
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
from .codex_isolation import VERSION, CodexIsolation, isolated_environment
from .ownership import (
    Acquisition,
    OwnedTemporaryDirectory,
    OwnershipLedger,
    RuntimeOwnershipReceipt,
)
from .probe.process import Process, ProcessCleanupIncomplete, SpawnFailed
from .probe.recording import Recorder, Redactor
from .runtime_decode import (
    CodexDecoder,
    RuntimeFailure,
    claude_events,
    object_value,
    result_value,
    strict_json_loads,
    string_value,
)

# Optional nulls emitted by the pinned ConfigToml serializer; unknown keys are rejected.
_CODEX_NULL_TOP_LEVEL = frozenset(
    [
        "allow_symlinked_codex_home",
        "analytics",
        "approvals_reviewer",
        "apps",
        "apps_mcp_product_sku",
        "audio",
        "auto_review",
        "browser_use",
        "check_for_update_on_startup",
        "client_search_provider",
        "compact_prompt",
        "computer_use",
        "daybreak",
        "desktop",
        "developer_instructions",
        "disable_paste_burst",
        "experimental_compact_prompt_file",
        "experimental_realtime_start_instructions",
        "experimental_realtime_webrtc_call_base_url",
        "experimental_realtime_ws_backend_prompt",
        "experimental_realtime_ws_base_url",
        "experimental_realtime_ws_model",
        "experimental_realtime_ws_startup_context",
        "experimental_thread_store",
        "experimental_thread_store_endpoint",
        "experimental_use_unified_exec_tool",
        "feedback",
        "forced_chatgpt_workspace_id",
        "forced_login_method",
        "ghost_snapshot",
        "goals",
        "hooks",
        "instructions",
        "js_repl_node_module_dirs",
        "js_repl_node_path",
        "log_dir",
        "mcp_enterprise_managed_auth",
        "mcp_oauth_callback_port",
        "mcp_oauth_callback_url",
        "mcp_optional_startup_grace_ms",
        "model",
        "model_auto_compact_token_limit",
        "model_auto_compact_token_limit_scope",
        "model_catalog_json",
        "model_context_window",
        "model_instructions_file",
        "model_post_turn_compact_threshold_percent",
        "model_provider",
        "model_reasoning_effort",
        "model_reasoning_summary",
        "model_verbosity",
        "notice",
        "notify",
        "openai_base_url",
        "oss_provider",
        "otel",
        "personality",
        "plan_mode_reasoning_effort",
        "profile",
        "projects",
        "realtime",
        "responses_api_metadata",
        "review_model",
        "sandbox_mode",
        "sandbox_workspace_write",
        "service_tier",
        "show_raw_agent_reasoning",
        "sqlite_home",
        "suppress_unstable_features_warning",
        "thread_unload_delay_secs",
        "tool_output_token_limit",
        "tool_suggest",
        "tools",
        "tui",
        "windows",
    ]
)
_CODEX_NULL_FIELDS = {
    ("permissions", "multi-agent-frozen-review"): frozenset(
        ["description", "extends", "workspace_roots"]
    ),
    ("permissions", "multi-agent-frozen-review", "filesystem"): frozenset(
        ["glob_scan_max_depth"]
    ),
    ("permissions", "multi-agent-frozen-review", "network"): frozenset(
        [
            "allow_local_binding",
            "allow_upstream_proxy",
            "dangerously_allow_all_unix_sockets",
            "dangerously_allow_non_loopback_proxy",
            "domains",
            "enable_socks5",
            "enable_socks5_udp",
            "mitm",
            "mode",
            "proxy_url",
            "socks_url",
            "unix_sockets",
        ]
    ),
    ("features",): frozenset(["network_proxy"]),
    ("memories",): frozenset(
        [
            "consolidation_model",
            "consolidation_service_tier",
            "disable_on_external_context",
            "dual_write",
            "extract_model",
            "max_raw_memories_for_consolidation",
            "max_rollout_age_days",
            "max_rollouts_per_startup",
            "max_unused_days",
            "min_rate_limit_remaining_percent",
            "min_rollout_idle_hours",
            "version",
        ]
    ),
    ("agents",): frozenset(
        [
            "default_subagent_model",
            "default_subagent_reasoning_effort",
            "interrupt_message",
            "job_max_runtime_seconds",
            "max_concurrent_threads_per_session",
            "max_depth",
        ]
    ),
    ("orchestrator",): frozenset(["skills"]),
    ("shell_environment_policy",): frozenset(
        [
            "exclude",
            "experimental_use_profile",
            "filters",
            "ignore_default_excludes",
            "include_only",
        ]
    ),
    ("history",): frozenset(["max_bytes"]),
}


async def _read(process: Process) -> dict[str, Any] | str | None:
    value = await process.read()
    if value is None:
        # The transport can observe OS exit before its reader has drained the
        # last stdout bytes. Always finish its bounded drain before declaring EOF.
        await process.wait()
        value = await process.read()
    return value


class _ExecutionDeadlineExpired(TimeoutError):
    """The host rejected new I/O before attempting any process creation."""


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
            method = message.get("method")
            if "method" in message and (
                not isinstance(method, str)
                or not method
                or "id" in message
                and type(message["id"]) not in (str, int)
            ):
                raise RuntimeFailure(
                    Error(
                        "runtime sent a malformed JSON-RPC server request",
                        raw_type="adapter.isolation_failed",
                    )
                )
            if isinstance(method, str) and method.startswith("mcpServer/"):
                raise RuntimeFailure(
                    Error(
                        "unexpected MCP activity in isolated review",
                        raw_type="adapter.isolation_failed",
                    )
                )
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
            if "method" not in message:
                if type(message.get("id")) is not int or message["id"] != identifier:
                    raise RuntimeFailure(
                        Error(
                            "runtime returned an unexpected JSON-RPC response id",
                            raw_type="adapter.isolation_failed"
                            if method in ("config/read", "configRequirements/read")
                            else method,
                        )
                    )
                if "error" in message:
                    error = object_value(message["error"], "RPC error")
                    detail = (
                        "runtime rejected configuration audit"
                        if method in ("config/read", "configRequirements/read")
                        else string_value(error.get("message"), "RPC error.message")
                    )
                    raise RuntimeFailure(
                        Error(
                            str(self.process._recorder.redactor.redact(detail)),
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
    timed_out: bool = False
    cancel_sent: bool = False
    reader_started: bool = False
    observed_terminal: Completed | Error | None = None
    terminal: Completed | Error | None = None
    exit_event: ProcessExited | None = None
    cancel_task: asyncio.Task[None] | None = None
    deadline: asyncio.Timeout | None = None
    deadline_active: bool = False
    isolation: CodexIsolation | None = None
    redactor: Redactor | None = None
    implementation_policy_reported: bool = False
    ownership_operation: int | None = None
    log_acquisition: Acquisition | None = None


class RuntimeAdapter(Adapter):
    def __init__(
        self,
        provider: str,
        *,
        executable: str | None = None,
        timeout: float = 180,
        deadline_at: float | None = None,
        grace: float = 2,
        log_dir: Path,
        output_schema: dict[str, Any] | None = None,
        claude_planning_draft: bool = False,
        codex_planning_review: bool = False,
    ) -> None:
        if provider not in ("codex", "claude"):
            raise ValueError("provider must be codex or claude")
        if deadline_at is not None:
            if isinstance(deadline_at, bool) or not isinstance(
                deadline_at, (int, float)
            ):
                raise ValueError("deadline_at must be a finite non-bool number")
            try:
                finite_deadline = math.isfinite(deadline_at)
            except OverflowError as error:
                raise ValueError("deadline_at must be finite") from error
            if not finite_deadline:
                raise ValueError("deadline_at must be finite")
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
        self.deadline_at = deadline_at
        self.grace = grace
        self.log_dir = log_dir.resolve()
        self._claude_planning_draft = claude_planning_draft
        self._codex_planning_review = codex_planning_review
        self.output_schema = (
            json.loads(json.dumps(output_schema, allow_nan=False))
            if output_schema is not None
            else None
        )
        self._sessions: dict[SessionHandle, SessionConfig] = {}
        self._used: set[SessionHandle] = set()
        self._turns: dict[TurnHandle, _Turn] = {}
        self._ownership = OwnershipLedger()

    def _execution_timer(self) -> asyncio.Timeout:
        return (
            asyncio.timeout(self.timeout)
            if self.deadline_at is None
            else asyncio.timeout_at(self.deadline_at)
        )

    def _check_execution_deadline(self) -> None:
        if (
            self.deadline_at is not None
            and asyncio.get_running_loop().time() >= self.deadline_at
        ):
            raise _ExecutionDeadlineExpired("execution deadline already passed")

    def _resume_execution_timer(self, timer: asyncio.Timeout) -> None:
        # Explicit deadlines remain fixed while cleanup is allowed to finish.
        # A later I/O phase cannot borrow time from a new relative timeout.
        self._check_execution_deadline()
        if self.deadline_at is not None:
            timer.reschedule(self.deadline_at)

    def ownership_receipt(self) -> RuntimeOwnershipReceipt:
        """Snapshot this instance's owned resources; never adopt a prior runtime."""
        return self._ownership.snapshot()

    def _environment(self) -> dict[str, str]:
        if self.provider == "codex":
            return isolated_environment()
        env = dict(os.environ)
        if self.provider == "claude":
            env.pop("ANTHROPIC_API_KEY", None)
            env.pop("ANTHROPIC_AUTH_TOKEN", None)
        return env

    def _recorder(
        self, identifier: str, cwd: Path, *, acquisition: Acquisition
    ) -> Recorder:
        opening = False
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            redactor = Redactor()
            redactor.add_workdir(cwd)
            opening = True
            return Recorder(
                self.log_dir / f"{self.provider}-{identifier}.jsonl",
                redactor,
                acquisition=acquisition,
            )
        except BaseException:
            if not opening:
                acquisition.not_created()
            raise
        finally:
            acquisition.finish()

    async def start_session(self, config: SessionConfig) -> SessionHandle:
        if config.permission != Permission.READ_ONLY:
            raise ValueError("review adapters only support read-only sessions")
        return self._register_session(config)

    def _register_session(self, config: SessionConfig) -> SessionHandle:
        config = replace(config, cwd=config.cwd.resolve())
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
        operation = self._ownership.begin_operation("turn")
        state.ownership_operation = operation
        state.log_acquisition = self._ownership.acquire("log")
        execution = self._run(handle, state)
        try:
            state.task = asyncio.create_task(execution)
        except BaseException:
            execution.close()
            state.log_acquisition.not_created()
            self._ownership.finish_operation(operation)
            raise
        return handle

    def _observe(self, state: _Turn, event: AdapterEvent) -> None:
        if state.redactor is not None:
            if isinstance(event, Error):
                event = replace(event, detail=str(state.redactor.redact(event.detail)))
            elif isinstance(event, Message):
                event = replace(
                    event, text=str(state.redactor.redact_secrets(event.text))
                )
            elif isinstance(event, Completed):
                event = replace(
                    event,
                    result=result_value(
                        state.redactor.redact_secrets(thaw_json(event.result)),
                        structured=False,
                    ),
                )
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
        self,
        argv: list[str],
        cwd: Path,
        recorder: Recorder,
        *,
        pipe: bool = False,
        isolation: CodexIsolation | None = None,
    ) -> Process:
        self._check_execution_deadline()
        if isolation is not None:
            executable = shutil.which(argv[0])
            if executable is None:
                acquisition = self._ownership.acquire("process")
                acquisition.not_created()
                raise SpawnFailed("executable unavailable")
            self._check_execution_deadline()
            return await isolation.spawn(
                [str(Path(executable).resolve()), *argv[1:]],
                pipe=pipe,
                grace=self.grace,
            )
        acquisition = self._ownership.acquire("process")
        try:
            environment = self._environment()
            try:
                self._check_execution_deadline()
            except _ExecutionDeadlineExpired:
                acquisition.not_created()
                raise
            return await Process.start(
                self.provider,
                argv,
                cwd,
                recorder,
                piped_stdin=pipe,
                grace=self.grace,
                env=environment,
                start_new_session=True,
                acquisition=acquisition,
            )
        finally:
            acquisition.finish()

    async def _run(self, handle: TurnHandle, state: _Turn) -> None:
        assert state.ownership_operation is not None
        assert state.log_acquisition is not None
        try:
            await self._run_owned(handle, state)
        finally:
            if state.log_acquisition.outcome == "pending":
                state.log_acquisition.not_created()
            state.log_acquisition.finish()
            self._ownership.finish_operation(state.ownership_operation)

    async def _run_owned(self, handle: TurnHandle, state: _Turn) -> None:
        recorder: Recorder | None = None
        failure: Error | None = None
        cleanup_problem: Error | None = None
        isolation_incomplete = False
        try:
            self._validate_turn_workspace(state)
            assert state.log_acquisition is not None
            recorder = self._recorder(
                handle.identifier, state.config.cwd, acquisition=state.log_acquisition
            )
            state.redactor = recorder.redactor
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
                async with self._execution_timer() as deadline:
                    state.deadline = deadline
                    state.deadline_active = True
                    try:
                        self._check_execution_deadline()
                        if self.provider == "codex":
                            await self._codex(state, recorder)
                        else:
                            await self._claude(state, recorder)
                    finally:
                        state.deadline_active = False
        except TimeoutError:
            if state.cancelled:
                failure = Error("runtime task cancelled", FailureCategory.CANCELLED)
            else:
                state.timed_out = True
                failure = Error("runtime deadline exceeded", FailureCategory.TIMEOUT)
        except RuntimeFailure as error:
            failure = error.event
        except asyncio.CancelledError:
            if (
                not state.cancelled
                and state.deadline is not None
                and state.deadline.expired()
            ):
                state.timed_out = True
                failure = Error("runtime deadline exceeded", FailureCategory.TIMEOUT)
            else:
                state.cancelled = True
                failure = Error("runtime task cancelled", FailureCategory.CANCELLED)
        except (OSError, ProcessCleanupIncomplete) as error:
            if state.observed_terminal is not None:
                cleanup_problem = Error(
                    f"runtime cleanup failure: {error}",
                    raw_type="adapter.cleanup_incomplete",
                )
            else:
                failure = Error(
                    f"runtime boundary failure: {type(error).__name__}: {error}"
                )
        except (SpawnFailed, ValueError, TypeError, KeyError, RuntimeError) as error:
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
                        if state.isolation is not None:
                            await state.isolation.terminate(state.process, self.grace)
                        else:
                            await state.process.terminate(self.grace)
                    finally:
                        code = state.process.child.returncode
                        if code is not None:
                            state.exit_event = ProcessExited(
                                code, raw_type="process_exit"
                            )
            except (OSError, ValueError, RuntimeError) as error:
                cleanup_problem = Error(
                    f"runtime cleanup failure: {error}",
                    raw_type="adapter.cleanup_incomplete",
                )
            finally:
                if state.isolation is not None:
                    try:
                        if not state.isolation.cleanup():
                            isolation_incomplete = True
                            cleanup_problem = Error(
                                "isolated runtime resources remain owned; cleanup unconfirmed",
                                raw_type="adapter.cleanup_incomplete",
                            )
                    except OSError:
                        isolation_incomplete = True
                        cleanup_problem = Error(
                            "isolated runtime resources could not be removed",
                            raw_type="adapter.cleanup_incomplete",
                        )
                # A cancellation request is not proof of process termination.
                # The runner keeps CANCELLING when this diagnostic has no exit
                # or runtime terminal, instead of feeding it to the state machine.
                if state.observed_terminal is None:
                    if isolation_incomplete or (
                        state.process is not None and state.exit_event is None
                    ):
                        failure = Error(
                            "runtime process stop remains unconfirmed",
                            raw_type="adapter.cleanup_incomplete",
                        )
                    elif state.cancelled:
                        failure = Error(
                            "process stopped after cancellation"
                            if state.process is not None
                            else "cancelled before process launch",
                            FailureCategory.CANCELLED,
                        )
                terminal = (
                    failure
                    or state.observed_terminal
                    or cleanup_problem
                    or Error("runtime exited without a terminal result")
                )
                if isinstance(terminal, Error) and state.redactor is not None:
                    terminal = replace(
                        terminal, detail=str(state.redactor.redact(terminal.detail))
                    )
                state.terminal = terminal
                state.queue.put_nowait(terminal)
                if state.exit_event is not None:
                    state.queue.put_nowait(state.exit_event)
                state.queue.put_nowait(None)
                if recorder is not None:
                    try:
                        if cleanup_problem is not None:
                            recorder.emit(
                                "cleanup_incomplete", detail=cleanup_problem.detail
                            )
                    finally:
                        recorder.close()

    def _validate_turn_workspace(self, state: _Turn) -> None:
        """Implementation sessions recheck their owned filesystem at I/O boundaries."""

    def _create_isolation(self, state: _Turn, recorder: Recorder) -> CodexIsolation:
        return CodexIsolation.create(
            state.config.cwd,
            recorder,
            self._environment(),
            log_dir=self.log_dir,
            ownership=self._ownership,
        )

    async def _codex(self, state: _Turn, recorder: Recorder) -> None:
        state.isolation = isolation = self._create_isolation(state, recorder)
        version, code = await self._collect(
            [self.executable, "--version"],
            state.config.cwd,
            recorder,
            isolation=isolation,
            cleanup_timer=state.deadline,
        )
        if code != 0 or version.strip() != VERSION:
            raise RuntimeFailure(
                Error(
                    "Codex isolation requires the recorded 0.169.0 runtime",
                    raw_type="adapter.isolation_failed",
                )
            )
        if state.cancelled:
            raise RuntimeFailure(
                Error(
                    "cancelled before review process launch", FailureCategory.CANCELLED
                )
            )
        assert state.deadline is not None
        self._resume_execution_timer(state.deadline)
        self._validate_turn_workspace(state)
        process = await self._spawn(
            [self.executable, "app-server", "--strict-config"],
            state.config.cwd,
            recorder,
            pipe=True,
            isolation=isolation,
        )
        state.process = process
        state.rpc = rpc = _Rpc(process)
        if state.cancelled:
            await self._send_cancel(state)
        await rpc.initialize()
        await self._audit_codex(rpc, isolation)
        auth = await rpc.request("account/read", {"refreshToken": False})
        if auth.get("account") is None:
            raise RuntimeFailure(
                Error(
                    "isolated Codex reports no subscription login",
                    FailureCategory.AUTH_EXPIRED,
                    raw_type="account/read",
                )
            )
        if (
            not isinstance(auth["account"], dict)
            or auth["account"].get("type") != "chatgpt"
        ):
            raise RuntimeFailure(
                Error(
                    "isolated Codex returned unsupported account evidence",
                    raw_type="adapter.isolation_failed",
                )
            )
        if state.cancelled:
            raise RuntimeFailure(
                Error("cancelled before thread submission", FailureCategory.CANCELLED)
            )
        self._validate_turn_workspace(state)
        self._check_execution_deadline()
        response = await rpc.request(
            "thread/start",
            {
                "model": state.config.model,
                "cwd": str(state.config.cwd),
                "approvalPolicy": "never",
                "ephemeral": True,
            },
        )
        if (
            response.get("instructionSources") != []
            or response.get("approvalPolicy") != "never"
            or response.get("cwd") != str(state.config.cwd)
        ):
            raise RuntimeFailure(
                Error(
                    "isolated thread returned unexpected instruction sources or policy",
                    raw_type="adapter.isolation_failed",
                )
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
            "approvalPolicy": "never",
            "cwd": str(state.config.cwd),
        }
        if self.output_schema is not None:
            schema = self.output_schema
            if self._codex_planning_review:
                # Strict response formats reject uniqueItems. The frozen
                # planning schema still enforces it on the returned decision.
                schema = copy.deepcopy(schema)
                schema["properties"]["blocking_issues"].pop("uniqueItems", None)
            params["outputSchema"] = schema
        self._validate_turn_workspace(state)
        self._check_execution_deadline()
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

    def _claude_argv(self, state: _Turn) -> list[str]:
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
            schema = self.output_schema
            if (
                self._claude_planning_draft
                and schema.get("$schema")
                == "https://json-schema.org/draft/2020-12/schema"
            ):
                # Claude Code 2.1.325 compiles the task card's local refs and
                # constraints but has no registered 2020-12 meta-schema.
                schema = {
                    key: value for key, value in schema.items() if key != "$schema"
                }
            argv += ["--json-schema", json.dumps(schema)]
        return argv

    def _validate_claude_message(
        self, state: _Turn, recorder: Recorder, message: dict[str, Any]
    ) -> None:
        """The restricted implementation adapter adds its init-report gate here."""

    async def _claude(self, state: _Turn, recorder: Recorder) -> None:
        argv = self._claude_argv(state)
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
            self._validate_claude_message(state, recorder, message)
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

    def _ensure_cancel_cleanup(self, state: _Turn) -> None:
        process = state.process
        if process is None:
            # An executing turn can own a creation/version subprocess before
            # its model handle is returned. Keep Process.start's shielded
            # creation cleanup responsible for it. Do not cancel an _run task
            # that has not started: it still owes logs, events and settlement.
            if (
                state.deadline_active
                and state.task is not None
                and not state.task.cancelling()
            ):
                state.task.cancel()
            return
        if state.cancel_task is None:

            async def stop_after_grace() -> None:
                await asyncio.sleep(self.grace)
                try:
                    await process.terminate(self.grace)
                except (OSError, ValueError, RuntimeError):
                    # The model timer was disarmed when cancellation was
                    # accepted. A failed stop must still wake a reader waiting
                    # on a live child, so the normal finally can report the
                    # unconfirmed cleanup without inventing an OS exit.
                    if (
                        state.observed_terminal is None
                        and state.task is not None
                        and not state.task.done()
                    ):
                        state.task.cancel()
                    raise

            # Merely scheduling this task performs no signal/write before the
            # synchronous acceptance callback. It also survives callback error.
            state.cancel_task = asyncio.create_task(stop_after_grace())

    async def _send_cancel(self, state: _Turn) -> None:
        self._ensure_cancel_cleanup(state)
        process = state.process
        if process is None or state.cancel_sent or state.observed_terminal is not None:
            return
        if self.provider == "codex":
            if (
                state.rpc is not None
                and state.thread_id is not None
                and state.turn_id is not None
            ):
                async with asyncio.timeout(self.grace):
                    await state.rpc.send(
                        "turn/interrupt",
                        {"threadId": state.thread_id, "turnId": state.turn_id},
                    )
                state.cancel_sent = True
        else:
            process.signal(signal.SIGINT)
            state.cancel_sent = True

    async def cancel(
        self, turn: TurnHandle, *, on_accepted: Callable[[], None] | None = None
    ) -> bool:
        state = self._turns[turn]
        if (
            state.terminal is not None
            or state.observed_terminal is not None
            or state.exit_event is not None
            or state.cancelled
            or state.timed_out
            or state.deadline is not None
            and state.deadline.expired()
            or state.process is not None
            and state.process.child.returncode is not None
        ):
            return False
        # Decide before the first await: a subsequent runtime terminal must not
        # turn an already accepted request into a rejected late cancellation.
        state.cancelled = True
        self._ensure_cancel_cleanup(state)
        if state.deadline is not None and state.deadline_active:
            state.deadline.reschedule(None)
        if on_accepted is not None:
            on_accepted()
        try:
            await self._send_cancel(state)
        except (ValueError, OSError):
            # The reader records the exit/failure; cancellation still owns cleanup.
            if state.process is not None:
                await state.process.terminate(self.grace)
        return True

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

    async def _collect(
        self,
        argv: list[str],
        cwd: Path,
        recorder: Recorder,
        *,
        isolation: CodexIsolation | None = None,
        cleanup_timer: asyncio.Timeout | None = None,
    ) -> tuple[str, int]:
        try:
            process = await self._spawn(argv, cwd, recorder, isolation=isolation)
        except (SpawnFailed, _ExecutionDeadlineExpired):
            # Process.start distinguishes a confirmed executable-launch failure
            # from failure while owning or cleaning up a newly created child.
            raise
        except Exception as error:
            # No returned handle does not prove no child exists. In particular,
            # creation-time cancellation can fail inside Process.start cleanup.
            raise RuntimeFailure(
                Error(
                    str(
                        recorder.redactor.redact(
                            f"environment process creation or cleanup unconfirmed: "
                            f"{type(error).__name__}: {error}"
                        )
                    ),
                    raw_type="adapter.cleanup_incomplete",
                )
            ) from error
        try:
            lines: list[str] = []
            while (value := await _read(process)) is not None:
                lines.append(json.dumps(value) if isinstance(value, dict) else value)
            return "\n".join(lines), await process.wait_exited()
        finally:
            if (
                self.deadline_at is not None
                and cleanup_timer is not None
                and not cleanup_timer.expired()
            ):
                # The completed read or its failure is already known. Cleanup
                # has the transport's bounded grace, never the execution timer.
                # The caller rearms the original D only before its next I/O.
                cleanup_timer.reschedule(None)
            try:
                if isolation is not None:
                    await isolation.terminate(process, self.grace)
                else:
                    await process.terminate(self.grace)
            except (OSError, ValueError, RuntimeError) as error:
                raise RuntimeFailure(
                    Error(
                        str(
                            recorder.redactor.redact(
                                f"environment process stop unconfirmed: "
                                f"{type(error).__name__}: {error}"
                            )
                        ),
                        raw_type="adapter.cleanup_incomplete",
                    )
                ) from error

    async def _audit_codex(self, rpc: _Rpc, isolation: CodexIsolation) -> None:
        """Check the runtime's merged values and the sources before any turn.

        ConfigReadResponse serializes optional fields as null. Non-null defaults
        below are pinned to rust-v0.169.0/codex-rs/config/defaults.toml; all owned
        overrides must survive normalization exactly, including their types.
        """
        response = await rpc.request(
            "config/read", {"includeLayers": True, "cwd": str(isolation.cwd)}
        )

        def reject() -> None:
            raise RuntimeFailure(
                Error(
                    "Codex effective configuration does not satisfy the owned isolation policy",
                    raw_type="adapter.isolation_failed",
                )
            )

        def equal(left: object, right: object) -> bool:
            return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)

        def effective_equal(
            actual: object, expected: object, path: tuple[str, ...]
        ) -> bool:
            if not isinstance(expected, dict):
                return equal(actual, expected)
            if not isinstance(actual, dict) or not expected.keys() <= actual.keys():
                return False
            # Null defaults are schema-defined and identical for our two fixed
            # profiles. Select by the host-owned expectation, never the reply.
            schema_path = path
            if len(path) >= 2 and path[:2] == (
                "permissions",
                isolation.config["default_permissions"],
            ):
                schema_path = ("permissions", "multi-agent-frozen-review", *path[2:])
            optional_nulls = _CODEX_NULL_FIELDS.get(schema_path, frozenset())
            for key, value in actual.items():
                if key not in expected:
                    if key not in optional_nulls or value is not None:
                        return False
                elif not effective_equal(value, expected[key], (*path, key)):
                    return False
            return True

        config = response.get("config")
        layers = response.get("layers")
        origins = response.get("origins")
        if (
            not isinstance(config, dict)
            or not isinstance(layers, list)
            or not isinstance(origins, dict)
        ):
            reject()
        assert isinstance(config, dict) and isinstance(layers, list)
        own_layers = 0
        for layer in layers:
            if not isinstance(layer, dict) or not isinstance(layer.get("name"), dict):
                reject()
            assert isinstance(layer, dict)
            name = layer["name"]
            if not isinstance(layer.get("version"), str) or not layer["version"]:
                reject()
            if name.get("type") == "user":
                if (
                    name.get("file") != str(isolation.codex_home / "config.toml")
                    or name.get("profile") is not None
                    or layer.get("disabledReason") is not None
                    or not equal(layer.get("config"), isolation.config)
                ):
                    reject()
                own_layers += 1
            elif not (
                name.get("type") == "system"
                and name.get("file") == "/etc/codex/config.toml"
                and layer.get("config") == {}
                and layer.get("disabledReason") is None
            ):
                reject()
        if own_layers != 1:
            reject()
        for key, expected in isolation.config.items():
            if key not in config or not effective_equal(config[key], expected, (key,)):
                reject()
        defaults: dict[str, object] = {
            "background_terminal_max_timeout": 300000,
            "file_opener": "vscode",
            "include_permissions_instructions": True,
            "chatgpt_base_url": "https://chatgpt.com/backend-api/",
            "project_root_markers": [".git"],
            "mcp_oauth_credentials_store": "auto",
            "hide_agent_reasoning": False,
        }
        empty_maps = {"profiles", "model_providers", "mcp_servers", "marketplaces"}
        if (
            config.keys()
            != isolation.config.keys()
            | _CODEX_NULL_TOP_LEVEL
            | defaults.keys()
            | empty_maps
        ):
            reject()
        for key, value in config.items():
            if key in isolation.config or (
                key in _CODEX_NULL_TOP_LEVEL and value is None
            ):
                continue
            if key in empty_maps and type(value) is dict and not value:
                continue
            if key not in defaults or not equal(value, defaults[key]):
                reject()
        requirements = await rpc.request("configRequirements/read", {})
        if (
            "requirements" not in requirements
            or requirements["requirements"] is not None
        ):
            reject()
        isolation.recorder.emit(
            "isolation_verified",
            profile=config["default_permissions"],
            effective_config={key: config[key] for key in isolation.config},
            sources=[layer["name"]["type"] for layer in layers],
            requirements=requirements["requirements"],
        )

    async def _check_codex_environment(
        self, log_acquisition: Acquisition
    ) -> EnvironmentInfo:
        with OwnedTemporaryDirectory(
            self._ownership, prefix="multi-agent-environment-"
        ) as directory:
            cwd = Path(directory).resolve()
            with self._recorder(
                "environment-" + uuid4().hex, cwd, acquisition=log_acquisition
            ) as recorder:
                self._check_execution_deadline()
                isolation = CodexIsolation.create(
                    cwd,
                    recorder,
                    self._environment(),
                    log_dir=self.log_dir,
                    ownership=self._ownership,
                )
                process = None
                try:
                    async with self._execution_timer() as deadline:
                        version, code = await self._collect(
                            [self.executable, "--version"],
                            cwd,
                            recorder,
                            isolation=isolation,
                            cleanup_timer=deadline,
                        )
                        if code != 0 or version.strip() != VERSION:
                            raise RuntimeFailure(
                                Error(
                                    "Codex isolation requires the recorded 0.169.0 runtime",
                                    raw_type="adapter.isolation_failed",
                                )
                            )
                        self._resume_execution_timer(deadline)
                        process = await self._spawn(
                            [self.executable, "app-server", "--strict-config"],
                            cwd,
                            recorder,
                            pipe=True,
                            isolation=isolation,
                        )
                        rpc = _Rpc(process)
                        await rpc.initialize()
                        await self._audit_codex(rpc, isolation)
                        auth = await rpc.request(
                            "account/read", {"refreshToken": False}
                        )
                        if "account" not in auth or (
                            auth["account"] is not None
                            and not isinstance(auth["account"], dict)
                        ):
                            raise ValueError("account/read has no account evidence")
                        if (
                            auth["account"] is not None
                            and auth["account"].get("type") != "chatgpt"
                        ):
                            raise RuntimeFailure(
                                Error(
                                    "isolated Codex returned unsupported account evidence",
                                    raw_type="adapter.isolation_failed",
                                )
                            )
                        models, efforts = await self._codex_catalog(rpc)
                        return EnvironmentInfo(
                            VERSION, auth["account"] is not None, models, efforts
                        )
                finally:
                    try:
                        if process is not None:
                            try:
                                await process.close_finished(self.grace)
                            finally:
                                await isolation.terminate(process, self.grace)
                    finally:
                        try:
                            removed = isolation.cleanup()
                        except OSError as error:
                            raise RuntimeFailure(
                                Error(
                                    str(
                                        recorder.redactor.redact(
                                            f"isolated environment resource removal "
                                            f"unconfirmed: {type(error).__name__}: {error}"
                                        )
                                    ),
                                    raw_type="adapter.cleanup_incomplete",
                                )
                            ) from error
                        if not removed:
                            raise RuntimeFailure(
                                Error(
                                    "isolated environment resources remain owned; cleanup unconfirmed",
                                    raw_type="adapter.cleanup_incomplete",
                                )
                            )

    async def _codex_catalog(
        self, rpc: _Rpc
    ) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
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
                    raise TypeError("supportedReasoningEfforts must be an array")
                if model.get("hidden") is not True:
                    models.append(name)
                    efforts[name] = tuple(
                        string_value(
                            object_value(level, "effort").get("reasoningEffort"),
                            "reasoningEffort",
                        )
                        for level in levels
                    )
            cursor = page.get("nextCursor")
            if cursor is None:
                break
            cursor = string_value(cursor, "model cursor")
            if cursor in cursors or len(cursors) >= 100:
                raise ValueError("model/list pagination did not terminate")
            cursors.add(cursor)
            params["cursor"] = cursor
        return tuple(models), efforts

    async def check_environment(self) -> EnvironmentInfo:
        """Record version/login/catalog separately; never starts a model turn."""
        operation = self._ownership.begin_operation("environment")
        log_acquisition = self._ownership.acquire("log")
        try:
            return await self._check_environment_owned(log_acquisition)
        finally:
            if log_acquisition.outcome == "pending":
                log_acquisition.not_created()
            log_acquisition.finish()
            self._ownership.finish_operation(operation)

    async def _check_environment_owned(
        self, log_acquisition: Acquisition
    ) -> EnvironmentInfo:
        try:
            if self.provider == "codex":
                return await self._check_codex_environment(log_acquisition)
            with OwnedTemporaryDirectory(
                self._ownership, prefix="multi-agent-environment-"
            ) as directory:
                cwd = Path(directory)
                with self._recorder(
                    "environment-" + uuid4().hex, cwd, acquisition=log_acquisition
                ) as recorder:
                    async with self._execution_timer() as deadline:
                        version_text, version_exit = await self._collect(
                            [self.executable, "--version"],
                            cwd,
                            recorder,
                            cleanup_timer=deadline,
                        )
                        if version_exit != 0:
                            raise ValueError("version check exited unsuccessfully")
                        version = string_value(version_text.strip(), "CLI version")
                        self._resume_execution_timer(deadline)
                        auth_text, auth_exit = await self._collect(
                            [self.executable, "auth", "status"],
                            cwd,
                            recorder,
                            cleanup_timer=deadline,
                        )
                        auth = object_value(
                            strict_json_loads(auth_text),
                            "auth status",
                        )
                        logged_in = auth.get("loggedIn")
                        if not isinstance(logged_in, bool):
                            raise TypeError("auth loggedIn must be a boolean")
                        # CLI reference: auth status exits 1 when logged out.
                        # No other nonzero status is successful preflight.
                        if auth_exit != 0 and not (
                            auth_exit == 1 and logged_in is False
                        ):
                            raise ValueError("auth status exited unsuccessfully")
                        return EnvironmentInfo(version, logged_in, None)
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
