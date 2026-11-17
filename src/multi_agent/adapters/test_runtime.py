"""One bounded command/exec operation, pinned to the local Codex 0.169.0 schema."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import math
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from multi_agent.core.events import Error, FailureCategory, JSONValue, freeze_json
from multi_agent.core.task_test_execution import (
    TaskTestCheckout,
    TaskTestPolicy,
    TaskTestScratch,
)
from multi_agent.core.tasks import NamedCommand

from .base import EnvironmentInfo
from .codex_isolation import CodexIsolation
from .ownership import OwnershipLedger, RuntimeOwnershipReceipt
from .probe.process import Process, SpawnFailed
from .probe.recording import Redactor
from .runtime_decode import RuntimeFailure, RuntimeJSONError
from .test_isolation import (
    VERSION,
    create_test_isolation,
    test_command_environment,
    validate_test_configuration,
)
from .test_recording import (
    OutputLimitExceeded,
    RawOutputLog,
    TestWireRecorder,
    _state,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class TestCommandResult:
    __test__ = False
    sequence: int
    request_id: int | None
    process_id: str | None
    request_attempted: bool
    request_settled: bool
    reported_exit_code: int | None
    response: JSONValue | None
    exit_code: int | None
    exit_code_provenance: str | None
    timeout_observed: bool
    cancel_accepted: bool
    environment: EnvironmentInfo | None
    observations: JSONValue
    problems: tuple[Error, ...]
    receipt: RuntimeOwnershipReceipt
    stdout_received_bytes: int
    stderr_received_bytes: int
    stdout_cap_reached: bool | None
    stderr_cap_reached: bool | None
    output_limit_reached: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "response", freeze_json(self.response))
        object.__setattr__(self, "observations", freeze_json(self.observations))


class TestProtocolError(ValueError):
    __test__ = False


class _DeferredHostFailure(Exception):
    def __init__(self, original: Exception) -> None:
        self.original = original
        super().__init__(type(original).__name__)


def _write_output(output: RawOutputLog, data: bytes) -> None:
    try:
        output.write(data)
    except OutputLimitExceeded:
        raise
    except Exception as error:
        raise _DeferredHostFailure(error) from error


def _positive(value: float, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be finite and positive")


class TaskTestRuntimeAdapter:
    """No constructor I/O; one owner and one command, with observable cleanup."""

    def __init__(
        self,
        *,
        checkout: TaskTestCheckout,
        scratch: TaskTestScratch,
        policy: TaskTestPolicy,
        log_dir: Path,
        deadline_at: float | None = None,
        on_cancel_accepted: Callable[[], None] | None = None,
    ) -> None:
        _positive(policy.timeout, "timeout")
        _positive(policy.grace, "grace")
        if type(policy.output_bytes_limit) is not int or policy.output_bytes_limit <= 0:
            raise ValueError("output byte limit must be positive")
        if deadline_at is not None and (
            isinstance(deadline_at, bool)
            or not isinstance(deadline_at, (int, float))
            or not math.isfinite(deadline_at)
        ):
            raise ValueError("deadline_at must be finite")
        self.checkout, self.scratch, self.policy = checkout, scratch, policy
        self.log_dir, self.deadline_at = Path(log_dir), deadline_at
        self._callback = on_cancel_accepted
        self._ownership = OwnershipLedger()
        self._stop = asyncio.Event()
        self._worker: asyncio.Task[TestCommandResult] | None = None
        self._finished = False
        self._cleaning = False
        self._first_host: BaseException | None = None
        self._wire: TestWireRecorder | None = None
        self._outputs: dict[str, RawOutputLog] = {}
        self._isolation: CodexIsolation | None = None
        self._process: Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._next_id = 0
        self._terminate_id: int | None = None
        self._deadline = 0.0
        self._result = TestCommandResult(
            sequence=0,
            request_id=None,
            process_id=None,
            request_attempted=False,
            request_settled=False,
            reported_exit_code=None,
            response=None,
            exit_code=None,
            exit_code_provenance=None,
            timeout_observed=False,
            cancel_accepted=False,
            environment=None,
            observations={},
            problems=(),
            receipt=self._ownership.snapshot(),
            stdout_received_bytes=0,
            stderr_received_bytes=0,
            stdout_cap_reached=None,
            stderr_cap_reached=None,
            output_limit_reached=False,
        )

    def snapshot(self) -> TestCommandResult:
        return self._result

    def ownership_receipt(self) -> RuntimeOwnershipReceipt:
        return self._ownership.snapshot()

    def _problem(self, detail: str, category: FailureCategory | None = None) -> None:
        if self._wire is not None:
            detail = str(self._wire.redactor.redact(detail))
        event = Error(detail, category, raw_type="adapter.task_test")
        self._result = replace(self._result, problems=(*self._result.problems, event))

    def _host(self, error: BaseException) -> None:
        if self._first_host is None:
            self._first_host = error
        self._problem(f"test host failure: {type(error).__name__}: {error}")

    async def cancel(self) -> bool:
        if (
            self._worker is None
            or self._finished
            or self._cleaning
            or self._result.timeout_observed
            or self._result.cancel_accepted
            or self._result.response is not None
        ):
            return False
        self._result = replace(self._result, cancel_accepted=True)
        self._stop.set()
        if self._callback is not None:
            try:
                self._callback()
            except Exception as error:
                self._host(error)
                raise
        return True

    async def run(self, command: NamedCommand, *, sequence: int) -> TestCommandResult:
        if self._worker is not None:
            raise ValueError("test runtime is single use")
        if type(sequence) is not int or not 1 <= sequence <= len(
            self.checkout.input.commands
        ):
            raise ValueError("test command sequence is outside the declared suite")
        if command != self.checkout.input.commands[sequence - 1]:
            raise ValueError("test command differs from the frozen declaration")
        self._result = replace(self._result, sequence=sequence)
        self._deadline = (
            self.deadline_at
            if self.deadline_at is not None
            else asyncio.get_running_loop().time() + self.policy.timeout
        )
        self._worker = asyncio.create_task(self._execute(command))
        cancelled: asyncio.CancelledError | None = None
        cancellation: asyncio.Task[bool] | None = None
        completion = asyncio.create_task(asyncio.wait({self._worker}))
        while not completion.done():
            try:
                await asyncio.shield(completion)
            except asyncio.CancelledError as error:
                cancelled = cancelled or error
                if cancellation is None:
                    cancellation = asyncio.create_task(self.cancel())
        completion.result()
        if cancellation is not None:
            cancellation_done = asyncio.create_task(asyncio.wait({cancellation}))
            while not cancellation_done.done():
                try:
                    await asyncio.shield(cancellation_done)
                except asyncio.CancelledError as error:
                    cancelled = cancelled or error
            cancellation_done.result()
            cancel_failure = cancellation.exception()
            if cancel_failure is not None and self._first_host is not cancel_failure:
                self._host(cancel_failure)
        worker_failure = self._worker.exception()
        final_failure = (
            self._first_host if self._first_host is not None else worker_failure
        )
        if cancelled is not None:
            if final_failure is not None:
                raise cancelled from final_failure
            raise cancelled
        if final_failure is not None:
            raise final_failure
        return self._worker.result()

    async def _watch_cancel(self, operation: asyncio.Task[Any]) -> None:
        await self._stop.wait()
        if not self._cleaning:
            operation.cancel()

    async def _execute(self, command: NamedCommand) -> TestCommandResult:
        operation = self._ownership.begin_operation("test-command")
        current = asyncio.current_task()
        assert current is not None
        monitor = asyncio.create_task(self._watch_cancel(current))
        timer = asyncio.timeout_at(self._deadline)
        try:
            async with timer:
                await self._operate(command)
        except TimeoutError as error:
            if (
                not timer.expired()
                and asyncio.get_running_loop().time() < self._deadline
            ):
                self._host(error)
                raise
            self._result = replace(self._result, timeout_observed=True)
            self._problem("host test deadline expired", FailureCategory.TIMEOUT)
        except asyncio.CancelledError:
            if not self._stop.is_set():
                raise
        except (
            TestProtocolError,
            RuntimeJSONError,
            RuntimeFailure,
            SpawnFailed,
        ) as error:
            if isinstance(error, RuntimeFailure):
                self._problem(error.event.detail, error.event.failure_category)
            else:
                self._problem(str(error))
        except Exception as error:
            self._host(error)
            raise
        finally:
            self._cleaning = True
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
            await self._cleanup()
            self._ownership.finish_operation(operation)
            self._finished = True
            await self._settle_call(self._sample_receipt)
        if self._first_host is not None:
            raise self._first_host
        return self._result

    def _sample_receipt(self) -> None:
        self._result = replace(self._result, receipt=self.ownership_receipt())

    def _check_executable(self) -> None:
        path = Path(self.policy.executable)
        before = path.lstat()
        if before.st_size > 256 * 1024 * 1024:
            raise TestProtocolError("test executable exceeds inspection byte limit")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        digest = hashlib.sha256()
        try:
            if _state(os.fstat(descriptor)) != _state(before) or not stat.S_ISREG(
                before.st_mode
            ):
                raise TestProtocolError("test executable changed during inspection")
            remaining = before.st_size
            while remaining:
                chunk = os.read(descriptor, min(remaining, 1024 * 1024))
                if not chunk:
                    raise TestProtocolError("test executable was truncated")
                remaining -= len(chunk)
                digest.update(chunk)
            if os.read(descriptor, 1) or _state(os.fstat(descriptor)) != _state(before):
                raise TestProtocolError("test executable changed during inspection")
        finally:
            os.close(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or path.resolve(strict=True) != path
            or (before.st_dev, before.st_ino) != self.policy.executable_identity
            or digest.hexdigest() != self.policy.executable_sha256
            or _state(path.lstat()) != _state(before)
        ):
            raise TestProtocolError("test executable identity or bytes changed")

    async def _operate(self, command: NamedCommand) -> None:
        if asyncio.get_running_loop().time() >= self._deadline:
            raise TimeoutError
        self._check_executable()
        self.log_dir.mkdir(mode=0o700)
        info = self.log_dir.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or self.log_dir.resolve(strict=True) != self.log_dir
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise ValueError("test logs require an owned canonical 0700 directory")
        self._wire = TestWireRecorder(
            self.log_dir / "wire.jsonl",
            Redactor(),
            limit=4 * self.policy.output_bytes_limit + 4 * 1024 * 1024,
            acquisition=self._ownership.acquire("log"),
        )
        for name in ("stdout", "stderr"):
            self._outputs[name] = RawOutputLog(
                self.log_dir / f"{name}.bin",
                self.policy.output_bytes_limit,
                self._ownership.acquire("log"),
            )
        self._isolation = self._create_isolation()
        await self._version()
        self._check_executable()
        self._process = await self._isolation.spawn(
            [self.policy.executable, "app-server", "--strict-config"],
            pipe=True,
            grace=self.policy.grace,
        )
        await self._preflight_and_command(command)

    def _create_isolation(self) -> CodexIsolation:
        assert self._wire is not None
        return create_test_isolation(
            self.checkout,
            self.scratch,
            self.policy,
            self._wire,
            log_dir=self.log_dir,
            ownership=self._ownership,
        )

    async def _preflight_and_command(self, command: NamedCommand) -> None:
        assert (
            self._process is not None
            and self._isolation is not None
            and self._wire is not None
        )
        await self._request(
            "initialize", {"clientInfo": {"name": "multi-agent-tests", "version": "1"}}
        )
        if not await self._process.send({"method": "initialized"}):
            raise TestProtocolError("test runtime closed during initialize")
        config = await self._request(
            "config/read", {"cwd": self.checkout.root, "includeLayers": True}
        )
        requirements = await self._request("configRequirements/read", None)
        try:
            observations = validate_test_configuration(
                config, requirements, self._isolation
            )
        except (TypeError, ValueError) as error:
            raise TestProtocolError(str(error)) from error
        self._wire.emit("isolation_verified", **observations)
        self._result = replace(
            self._result,
            observations={
                "version": self._result.environment.cli_version
                if self._result.environment
                else None,
                "policy_audit": {
                    "config": observations["effective_config"],
                    "requirements": observations["requirements"],
                    "layers": observations["sources"],
                },
                "isolation_root": str(self._isolation.root),
            },
        )
        remainder = self._deadline - asyncio.get_running_loop().time()
        if remainder <= 0:
            raise TimeoutError
        identifier = self._allocate_id()
        process_id = uuid4().hex
        self._result = replace(
            self._result,
            request_id=identifier,
            process_id=process_id,
            request_attempted=True,
        )
        if not await self._process.send(
            {
                "id": identifier,
                "method": "command/exec",
                "params": {
                    "command": [command.executable, *command.args],
                    "cwd": self.checkout.root,
                    "processId": process_id,
                    "tty": False,
                    "streamStdin": False,
                    "streamStdoutStderr": True,
                    "outputBytesCap": self.policy.output_bytes_limit,
                    "timeoutMs": math.ceil((remainder + self.policy.grace) * 1000) + 1,
                    "env": test_command_environment(self.scratch, self.policy),
                },
            }
        ):
            raise TestProtocolError("test runtime closed before command request")
        self._reader = asyncio.create_task(self._consume())
        await asyncio.shield(self._reader)

    async def _version(self) -> None:
        assert self._isolation is not None
        process = await self._isolation.spawn(
            [self.policy.executable, "--version"], pipe=False, grace=self.policy.grace
        )
        try:
            chunks: list[str] = []
            size = 0
            while (value := await process.read()) is not None:
                if not isinstance(value, str):
                    raise TestProtocolError("test version output is not text")
                size += len(value.encode("utf-8"))
                if size > 8192:
                    raise TestProtocolError("test version output exceeds limit")
                chunks.append(value)
            code = await process.wait()
            version = "\n".join(chunks).strip()
            self._result = replace(
                self._result, environment=EnvironmentInfo(cli_version=version or None)
            )
            if code != 0 or version != VERSION:
                raise TestProtocolError(
                    "test command protocol requires actual codex-cli 0.169.0"
                )
        finally:
            await self._isolation.terminate(process, self.policy.grace)

    def _allocate_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def _request(
        self, method: str, params: dict[str, Any] | None
    ) -> dict[str, Any]:
        assert self._process is not None
        identifier = self._allocate_id()
        if not await self._process.send(
            {"id": identifier, "method": method, "params": params}
        ):
            raise TestProtocolError("test runtime closed stdin")
        while (message := await self._process.read()) is not None:
            if not isinstance(message, dict):
                raise TestProtocolError("test runtime returned non-object JSON-RPC")
            if "method" in message:
                raise TestProtocolError("unexpected test preflight server message")
            if (
                type(message.get("id")) is not int
                or message["id"] != identifier
                or "error" in message
                or not isinstance(message.get("result"), dict)
            ):
                raise TestProtocolError("test runtime response did not match request")
            return cast(dict[str, Any], message["result"])
        raise TestProtocolError("test runtime exited before preflight response")

    def _consume_message(self, message: Any) -> bool:
        if not isinstance(message, dict):
            raise TestProtocolError("test runtime returned non-object JSON-RPC")
        if message.get("method") == "command/exec/outputDelta" and "id" not in message:
            if self._result.response is not None:
                raise TestProtocolError("test output arrived after final response")
            params = message.get("params")
            if (
                not isinstance(params, dict)
                or params.get("processId") != self._result.process_id
                or params.get("stream") not in ("stdout", "stderr")
                or type(params.get("capReached")) is not bool
                or not isinstance(params.get("deltaBase64"), str)
            ):
                raise TestProtocolError("malformed test output notification")
            stream = params["stream"]
            try:
                data = base64.b64decode(params["deltaBase64"], validate=True)
            except (ValueError, binascii.Error) as error:
                raise TestProtocolError("invalid test output base64") from error
            old_count = (
                self._result.stdout_received_bytes
                if stream == "stdout"
                else self._result.stderr_received_bytes
            )
            old_cap = (
                self._result.stdout_cap_reached
                if stream == "stdout"
                else self._result.stderr_cap_reached
            )
            if stream == "stdout":
                self._result = replace(
                    self._result,
                    stdout_received_bytes=old_count + len(data),
                    stdout_cap_reached=bool(old_cap or params["capReached"]),
                )
            else:
                self._result = replace(
                    self._result,
                    stderr_received_bytes=old_count + len(data),
                    stderr_cap_reached=bool(old_cap or params["capReached"]),
                )
            if self._first_host is None and not self._result.output_limit_reached:
                try:
                    _write_output(self._outputs[stream], data)
                except OutputLimitExceeded:
                    self._result = replace(self._result, output_limit_reached=True)
                    self._problem("test output exceeded its byte limit")
                    self._stop.set()
                except _DeferredHostFailure as error:
                    self._host(error.original)
                    self._stop.set()
                    # A host log failure must not interrupt this live reader.
                    return False
            if params["capReached"]:
                self._problem("test runtime output cap was reached")
                self._stop.set()
            return False
        if "method" in message or type(message.get("id")) is not int:
            raise TestProtocolError("unexpected test runtime message")
        if message["id"] == self._terminate_id:
            if "error" in message or message.get("result") != {}:
                raise TestProtocolError("test termination request was rejected")
            return False
        if (
            message["id"] != self._result.request_id
            or self._result.response is not None
        ):
            raise TestProtocolError("unexpected or duplicate test final response")
        value = message.get("result")
        if (
            "error" in message
            or not isinstance(value, dict)
            or type(value.get("exitCode")) is not int
            or not -(2**31) <= value["exitCode"] < 2**31
            or value.get("stdout") != ""
            or value.get("stderr") != ""
        ):
            raise TestProtocolError("invalid streamed test command result")
        code = value["exitCode"]
        mapped = code if 0 <= code <= 255 and code != 124 else None
        self._result = replace(
            self._result,
            response=value,
            reported_exit_code=code,
            request_settled=True,
            exit_code=mapped,
            exit_code_provenance="codex-v0.169.0-runtime-normalized"
            if mapped is not None
            else None,
        )
        if mapped is None:
            self._problem("test runtime reported an ambiguous command exit code")
        return True

    async def _consume(self) -> None:
        assert self._process is not None
        while (message := await self._process.read()) is not None:
            try:
                if self._consume_message(message):
                    return
            except TestProtocolError as error:
                self._problem(str(error))
                self._stop.set()
        if self._result.response is None:
            self._problem("test runtime exited without a matching final response")

    async def _settle_call(self, call: Callable[[], Any]) -> None:
        async def invoke() -> None:
            result = call()
            if hasattr(result, "__await__"):
                await result

        outcomes = await asyncio.gather(invoke(), return_exceptions=True)
        error = outcomes[0]
        if isinstance(error, BaseException):
            self._host(error)

    async def _settle_bounded(self, call: Callable[[], Any]) -> None:
        async def invoke() -> None:
            async with asyncio.timeout(self.policy.grace):
                result = call()
                if hasattr(result, "__await__"):
                    await result

        await self._settle_call(invoke)

    async def _join_reader(self) -> None:
        assert self._reader is not None
        if not self._reader.done():
            await asyncio.wait({self._reader}, timeout=self.policy.grace)
        cancelled_here = not self._reader.done()
        if cancelled_here:
            self._host(TimeoutError("test command consumer did not settle"))
            self._reader.cancel()
        outcomes = await asyncio.gather(self._reader, return_exceptions=True)
        failure = outcomes[0]
        if isinstance(failure, (RuntimeJSONError, TestProtocolError)):
            self._problem(str(failure))
        elif isinstance(failure, BaseException) and not (
            cancelled_here and isinstance(failure, asyncio.CancelledError)
        ):
            self._host(failure)

    async def _cleanup(self) -> None:
        process = self._process
        if process is not None:
            if self._result.request_attempted and self._result.response is None:
                self._terminate_id = self._allocate_id()
                await self._settle_bounded(
                    lambda: process.send(
                        {
                            "id": self._terminate_id,
                            "method": "command/exec/terminate",
                            "params": {"processId": self._result.process_id},
                        }
                    )
                )
                if self._reader is not None and not self._reader.done():
                    await asyncio.wait({self._reader}, timeout=self.policy.grace)
            await self._settle_call(process.close_stdin)
            await self._settle_call(lambda: process.wait_for_exit(self.policy.grace))
            isolation = self._isolation
            assert isolation is not None
            await self._settle_call(
                lambda: isolation.terminate(process, self.policy.grace)
            )
            if self._reader is not None:
                await self._join_reader()
            await self._settle_bounded(self._drain_tail)
        if self._isolation is not None:
            await self._settle_call(self._isolation.cleanup)
        for output in self._outputs.values():
            await self._settle_call(output.close)
        if self._wire is not None:
            if self._wire.limit_reached:
                self._result = replace(self._result, output_limit_reached=True)
            await self._settle_call(self._wire.close)

    async def _drain_tail(self) -> None:
        assert self._process is not None
        try:
            while (message := await self._process.read()) is not None:
                try:
                    self._consume_message(message)
                except TestProtocolError as error:
                    self._problem(str(error))
        except RuntimeJSONError as error:
            self._problem(str(error))
