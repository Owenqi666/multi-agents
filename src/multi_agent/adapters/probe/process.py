"""Async subprocess ownership and lossless line recording for probe steps."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

from ..ownership import Acquisition, ProcessOwnership
from ..runtime_decode import RuntimeJSONError, strict_json_loads
from .recording import Recorder


class SpawnFailed(Exception):
    """Only failure of the executable launch, not subsequent recording or I/O."""


class ProcessCleanupIncomplete(RuntimeError):
    """Bounded cleanup could not confirm process stop or output settlement."""


class _GroupSignalDenied(PermissionError):
    """An OS group signal was denied, distinct from a recording I/O error."""


class Process:
    def __init__(
        self,
        name: str,
        child: asyncio.subprocess.Process,
        recorder: Recorder,
        grace: float,
        start_new_session: bool = False,
    ) -> None:
        self._owns_group = start_new_session
        self._group_stopped = False
        self.name = name
        self.child = child
        self.first_stdout = asyncio.Event()
        self._recorder = recorder
        self._grace = grace
        self._stdout: asyncio.Queue[dict[str, Any] | str | None] = asyncio.Queue()
        self._stdin_closed = False
        self._eof = {"stdout": False, "stderr": False}
        assert child.stdout is not None and child.stderr is not None
        self._readers = [
            asyncio.create_task(self._read_stream(child.stdout, "stdout")),
            asyncio.create_task(self._read_stream(child.stderr, "stderr")),
        ]
        self._exit = asyncio.create_task(self._observe_exit())
        self._watcher = asyncio.create_task(self._watch())

    @classmethod
    async def start(
        cls,
        name: str,
        argv: list[str],
        cwd: Path,
        recorder: Recorder,
        *,
        piped_stdin: bool = False,
        grace: float = 15,
        env: Mapping[str, str] | None = None,
        start_new_session: bool = False,
        acquisition: Acquisition | None = None,
        on_created: Callable[[Process], None] | None = None,
        pass_fds: tuple[int, ...] = (),
    ) -> Process:
        async def publish(child: asyncio.subprocess.Process) -> Process:
            process = cls(name, child, recorder, grace, start_new_session)
            try:
                if on_created is not None:
                    on_created(process)
            except BaseException:
                # Retain the known handle even if local registration fails.
                # A second publication failure must still clean the child.
                try:
                    if acquisition is not None:
                        acquisition.publish(process)
                finally:
                    await process.terminate(grace)
                raise
            try:
                if acquisition is not None:
                    acquisition.publish(process)
            except BaseException:
                await process.terminate(grace)
                raise
            return process

        try:
            # Before this record succeeds no launch has been attempted.
            try:
                recorder.emit("spawn", proc=name, argv=argv, cwd=str(cwd))
            except BaseException:
                if acquisition is not None:
                    acquisition.not_created()
                raise
            creation = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    *argv,
                    cwd=cwd,
                    stdin=asyncio.subprocess.PIPE
                    if piped_stdin
                    else asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                    start_new_session=start_new_session,
                    pass_fds=pass_fds,
                )
            )
            try:
                child = await asyncio.shield(creation)
            except OSError as error:
                if acquisition is not None:
                    acquisition.not_created()
                raise SpawnFailed from error
            except asyncio.CancelledError:
                # The launch still owns its child when the caller stops waiting.
                # Both paths publish before attempting any resource cleanup.
                async def finish_creation() -> None:
                    try:
                        child = await creation
                    except OSError:
                        if acquisition is not None:
                            acquisition.not_created()
                        return
                    process = await publish(child)
                    await process.terminate(grace)

                cleanup = asyncio.create_task(finish_creation())
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        continue
                cleanup.result()
                raise
            return await publish(child)
        finally:
            if acquisition is not None:
                acquisition.finish()

    def ownership_record(self, resource_id: str) -> ProcessOwnership:
        return ProcessOwnership(
            resource_id=resource_id,
            label=self.name,
            pid=self.child.pid,
            exit_code=self.child.returncode,
            owns_group=self._owns_group,
            group_gone=self._group_stopped,
            watcher_settled=self._watcher.done(),
            readers_settled=all(reader.done() for reader in self._readers),
            stdout_eof=self._eof["stdout"],
            stderr_eof=self._eof["stderr"],
        )

    def _line(self, data: bytes, stream: str) -> None:
        extra: dict[str, Any] = {}
        try:
            text = data.removesuffix(b"\r").decode("utf-8")
        except UnicodeDecodeError as error:
            if stream == "stdout":
                self._decode_error(data, "invalid_utf8")
                raise RuntimeJSONError("invalid stdout UTF-8") from error
            text = data.removesuffix(b"\r").decode("utf-8", errors="replace")
            extra["encoding_replaced"] = True
        if stream == "stderr":
            # Diagnostic JSON is still text, not an RPC reply. Passing it as a
            # message could consume a pending config/read id and prematurely
            # disable masking of the actual response's diagnostics.
            self._recorder.emit(
                "recv", proc=self.name, stream=stream, text=text, **extra
            )
            return
        try:
            parsed: object = strict_json_loads(text)
        except json.JSONDecodeError:
            # Probe recording also carries version output and non-JSON lines.
            # Runtime protocol callers still reject text where an object is due.
            parsed = None
        except RuntimeJSONError:
            self._decode_error(data, "invalid_json")
            raise
        if isinstance(parsed, dict):
            self._recorder.emit(
                "recv", proc=self.name, stream=stream, message=parsed, **extra
            )
            value: dict[str, Any] | str = parsed
        else:
            self._recorder.emit(
                "recv", proc=self.name, stream=stream, text=text, **extra
            )
            value = text
        if stream == "stdout":
            self.first_stdout.set()
            self._stdout.put_nowait(value)

    def _decode_error(self, data: bytes, category: str) -> None:
        self.first_stdout.set()
        # Do not parse an ambiguous id or record an exception/payload. In
        # particular the Recorder must keep pending config/read masking active.
        self._recorder.emit(
            "decode_error",
            proc=self.name,
            stream="stdout",
            error=category,
            byte_count=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )

    async def _read_stream(self, reader: asyncio.StreamReader, stream: str) -> None:
        buffered = bytearray()
        try:
            while chunk := await reader.read(65536):
                parts = chunk.split(b"\n")
                buffered.extend(parts[0])
                if len(parts) > 1:
                    line = bytes(buffered)
                    buffered.clear()
                    self._line(line, stream)
                    for line in parts[1:-1]:
                        self._line(line, stream)
                    buffered.extend(parts[-1])
            self._eof[stream] = True
        finally:
            # A drain deadline is not EOF, but bytes already read remain evidence.
            if buffered:
                self._line(bytes(buffered), stream)

    async def _observe_exit(self) -> int:
        # asyncio Process.wait() can wait for pipe EOF even after OS exit. The
        # returncode is set by the child watcher independently of inherited pipes.
        while self.child.returncode is None:
            await asyncio.sleep(0.005)
        return self.child.returncode

    def _close_output_pipes(self) -> None:
        # High-level Process exposes no pipe-close operation. Keep this CPython
        # bridge local; closing the public transports releases inherited pipes.
        transport = cast(asyncio.SubprocessTransport, vars(self.child)["_transport"])
        for descriptor in (1, 2):
            pipe = transport.get_pipe_transport(descriptor)
            if pipe is not None:
                pipe.close()

    async def _stop_readers(self) -> None:
        for reader in self._readers:
            reader.cancel()
        try:
            results = await asyncio.gather(*self._readers, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException) and not isinstance(
                    result, asyncio.CancelledError
                ):
                    raise result
        finally:
            self._close_output_pipes()

    def observe(self, name: str, value: object) -> None:
        self._recorder.emit("observation", name=name, value=value)

    async def _watch(self) -> int:
        output = asyncio.gather(*self._readers)
        try:
            try:
                await asyncio.wait(
                    (self._exit, output), return_when=asyncio.FIRST_COMPLETED
                )
                if output.done():
                    await output
                returncode = await self.wait_exited()
                _, pending = await asyncio.wait((output,), timeout=self._grace)
                if pending:
                    await self._stop_readers()
                    self.observe("output_not_closed", self.name)
                else:
                    await output
            except Exception:
                # A failed reader must wake consumers and still release the child.
                try:
                    await self._stop_child(self._grace)
                finally:
                    await self._stop_readers()
                raise
            self._recorder.emit("exit", proc=self.name, returncode=returncode)
            return returncode
        finally:
            await asyncio.gather(output, return_exceptions=True)
            self._stdout.put_nowait(None)

    async def read(self) -> dict[str, Any] | str | None:
        # OS exit is not pipe EOF. The watcher has its own bounded final drain;
        # waiting for it preserves queued tail output and surfaces reader errors
        # before a caller can accept a previously observed terminal result.
        if (
            self._stdout.empty()
            and self.child.returncode is not None
            and not self._watcher.done()
        ):
            await self.wait()
        if not self._stdout.empty():
            value = self._stdout.get_nowait()
        elif self._watcher.done():
            await self.wait()
            return None
        else:
            incoming = asyncio.create_task(self._stdout.get())
            try:
                done, _ = await asyncio.wait(
                    (incoming, self._exit, self._watcher),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if incoming in done:
                    value = incoming.result()
                else:
                    await self.wait()
                    value = await incoming
            finally:
                incoming.cancel()
                await asyncio.gather(incoming, return_exceptions=True)
        if value is None:
            await self.wait()
        return value

    async def wait_exited(self) -> int:
        """Wait for the OS child only; bounded output draining continues separately."""
        return await asyncio.shield(self._exit)

    async def wait(self) -> int:
        return await asyncio.shield(self._watcher)

    async def send(self, message: dict[str, Any]) -> bool:
        writer = self.child.stdin
        if writer is None:
            raise RuntimeError("this process has no piped stdin")
        if writer.is_closing() or self.child.returncode is not None:
            return False
        try:
            writer.write((json.dumps(message) + "\n").encode("utf-8"))
            self._recorder.emit("send", proc=self.name, message=message)
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            return False
        return True

    def close_stdin(self) -> None:
        if self.child.stdin is not None and not self._stdin_closed:
            self._stdin_closed = True
            self.child.stdin.close()
            self._recorder.emit("stdin_closed", proc=self.name)

    def signal(self, sig: signal.Signals) -> bool:
        if self._group_stopped or (
            not self._owns_group and self.child.returncode is not None
        ):
            return False
        try:
            if self._owns_group:
                try:
                    os.killpg(self.child.pid, sig)
                except PermissionError as error:
                    raise _GroupSignalDenied(*error.args) from error
            else:
                self.child.send_signal(sig)
        except ProcessLookupError:
            return False
        self._recorder.emit("signal", proc=self.name, name=sig.name)
        return True

    def _group_exists(self) -> bool:
        try:
            os.killpg(self.child.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # macOS reports EPERM for an unreaped zombie-only group. Treat it
            # as present, yield to the child watcher, and retry within grace.
            # This never treats an inaccessible live group as already stopped.
            return True
        return True

    async def _stop_group(self, grace: float) -> None:
        """Stop ordinary descendants even if the immediate child has exited.

        Only enabled for a process created with start_new_session. Descendants
        that deliberately leave this group are outside this ownership boundary.
        """
        recording_error: OSError | None = None
        signal_denied: _GroupSignalDenied | None = None
        try:
            self.signal(signal.SIGTERM)
        except _GroupSignalDenied as error:
            signal_denied = error
        except OSError as error:
            recording_error = error
        stopped = await self._confirm_stop(grace)
        if not stopped:
            try:
                self.signal(signal.SIGKILL)
            except _GroupSignalDenied as error:
                signal_denied = error
            except OSError as error:
                recording_error = recording_error or error
            stopped = await self._confirm_stop(grace)
        if not stopped:
            if recording_error is not None:
                raise recording_error
            if signal_denied is not None:
                raise signal_denied
            raise ProcessCleanupIncomplete("process group stop unconfirmed")
        # A transient EPERM is tolerable only after actual exit and group absence.
        self._group_stopped = True
        if recording_error is not None:
            raise recording_error

    async def _confirm_stop(self, grace: float) -> bool:
        deadline = asyncio.get_running_loop().time() + grace
        while True:
            group_gone = not self._owns_group or not self._group_exists()
            if self.child.returncode is not None and group_gone:
                return True
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.005, remaining))

    async def wait_for_exit(self, seconds: float) -> bool:
        if self.child.returncode is not None:
            return True
        try:
            await asyncio.wait_for(self.wait_exited(), seconds)
        except TimeoutError:
            return False
        return True

    async def _stop_child(self, grace: float) -> None:
        if self._owns_group:
            await self._stop_group(grace)
            return
        recording_error: OSError | None = None
        try:
            self.signal(signal.SIGTERM)
        except OSError as error:
            recording_error = error
        stopped = await self._confirm_stop(grace)
        if not stopped:
            try:
                self.signal(signal.SIGKILL)
            except OSError as error:
                recording_error = recording_error or error
            stopped = await self._confirm_stop(grace)
        if recording_error is not None:
            raise recording_error
        if not stopped:
            raise ProcessCleanupIncomplete("process stop unconfirmed")

    async def terminate(self, grace: float) -> None:
        async def cleanup() -> None:
            stop_error: BaseException | None = None
            try:
                await self._stop_child(grace)
            except BaseException as error:
                stop_error = error
                raise
            finally:
                if not self._watcher.done():
                    # A refused signal can leave a live child. Do not turn the
                    # bounded stop failure into an unbounded wait for its EOF.
                    # After a confirmed stop the watcher still owns its separate
                    # output-drain budget, then needs time to cancel readers and
                    # record unterminated tails. Do not race that same deadline.
                    # An expired wait never proves exit, EOF or successful cleanup.
                    budget = grace if stop_error is not None else self._grace + grace
                    _, pending = await asyncio.wait((self._watcher,), timeout=budget)
                    if pending:
                        if stop_error is not None:
                            raise stop_error
                        raise ProcessCleanupIncomplete(
                            "process output cleanup unconfirmed"
                        )
                try:
                    await self.wait()
                except RuntimeJSONError:
                    if stop_error is not None:
                        raise stop_error
                    # read()/wait() continue reporting the original input
                    # failure. It is not itself evidence that a completed stop
                    # failed. This catch cannot consume _stop_child's errors.
                    if (
                        self.child.returncode is None
                        or self._owns_group
                        and not self._group_stopped
                        or not self._watcher.done()
                        or any(not reader.done() for reader in self._readers)
                    ):
                        raise
                    for reader in self._readers:
                        if not reader.cancelled():
                            reader_error = reader.exception()
                            if reader_error is not None and not isinstance(
                                reader_error, RuntimeJSONError
                            ):
                                raise reader_error

        # A caller may be cancelled during the grace period itself. Ownership
        # continues until escalation and stream draining have finished.
        cleaning = asyncio.create_task(cleanup())
        cancelled = False
        while not cleaning.done():
            try:
                await asyncio.shield(cleaning)
            except asyncio.CancelledError:
                cancelled = True
        cleaning.result()
        if cancelled:
            raise asyncio.CancelledError

    async def close_finished(self, grace: float) -> None:
        self.close_stdin()
        if not await self.wait_for_exit(grace):
            await self.terminate(grace)
