"""Async subprocess ownership and lossless line recording for probe steps."""

from __future__ import annotations

import asyncio
import json
import os
import signal
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from .recording import Recorder


class SpawnFailed(Exception):
    """Only failure of the executable launch, not subsequent recording or I/O."""


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
    ) -> Process:
        # Record the launch attempt before acquiring an OS process, so a failed
        # recording cannot leave an unregistered child behind.
        recorder.emit("spawn", proc=name, argv=argv, cwd=str(cwd))
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
            )
        )
        try:
            child = await asyncio.shield(creation)
        except OSError as error:
            raise SpawnFailed from error
        except asyncio.CancelledError:
            # Creation owns the OS process even if the step stops awaiting it.
            async def finish_creation() -> None:
                try:
                    child = await creation
                except OSError:
                    return
                process = cls(name, child, recorder, grace, start_new_session)
                await process.terminate(grace)

            cleanup = asyncio.create_task(finish_creation())
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            cleanup.result()
            raise
        process = cls(name, child, recorder, grace, start_new_session)
        return process

    def _line(self, data: bytes, stream: str) -> None:
        text = data.removesuffix(b"\r").decode("utf-8", errors="replace")
        try:
            parsed: object = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            self._recorder.emit("recv", proc=self.name, stream=stream, message=parsed)
            value: dict[str, Any] | str = parsed
        else:
            self._recorder.emit("recv", proc=self.name, stream=stream, text=text)
            value = text
        if stream == "stdout":
            self.first_stdout.set()
            self._stdout.put_nowait(value)

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
        if not self._stdout.empty():
            value = self._stdout.get_nowait()
        elif self._watcher.done():
            await self.wait()
            return None
        elif self.child.returncode is not None:
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
                    if self._watcher in done:
                        await self.wait()
                    return None
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
                os.killpg(self.child.pid, sig)
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
        try:
            self.signal(signal.SIGTERM)
        except OSError as error:
            recording_error = error
        deadline = asyncio.get_running_loop().time() + grace
        while self._group_exists() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.005)
        if self._group_exists():
            try:
                self.signal(signal.SIGKILL)
            except OSError as error:
                recording_error = recording_error or error
        await asyncio.shield(self._exit)
        self._group_stopped = True
        if recording_error is not None:
            raise recording_error

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
        try:
            await asyncio.wait_for(asyncio.shield(self._exit), grace)
        except TimeoutError:
            try:
                self.signal(signal.SIGKILL)
            except OSError as error:
                recording_error = recording_error or error
            await asyncio.shield(self._exit)
        if recording_error is not None:
            raise recording_error

    async def terminate(self, grace: float) -> None:
        async def cleanup() -> None:
            try:
                await self._stop_child(grace)
            finally:
                await self.wait()

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
