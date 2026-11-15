"""Async subprocess ownership and lossless line recording for probe steps."""

from __future__ import annotations

import asyncio
import json
import signal
from pathlib import Path
from typing import Any

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
    ) -> None:
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
        self._exit = asyncio.create_task(child.wait())
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
                process = cls(name, child, recorder, grace)
                await process.terminate(grace)

            cleanup = asyncio.create_task(finish_creation())
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            cleanup.result()
            raise
        process = cls(name, child, recorder, grace)
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
        while chunk := await reader.read(65536):
            parts = chunk.split(b"\n")
            buffered.extend(parts[0])
            if len(parts) > 1:
                self._line(bytes(buffered), stream)
                buffered.clear()
                for line in parts[1:-1]:
                    self._line(line, stream)
                buffered.extend(parts[-1])
        if buffered:
            self._line(bytes(buffered), stream)

    async def _watch(self) -> int:
        try:
            try:
                await asyncio.gather(self._exit, *self._readers)
            except Exception:
                # A failed reader must wake consumers and still release the child.
                try:
                    await self._stop_child(self._grace)
                finally:
                    await asyncio.gather(*self._readers, return_exceptions=True)
                raise
            returncode = self._exit.result()
            self._recorder.emit("exit", proc=self.name, returncode=returncode)
            return returncode
        finally:
            self._stdout.put_nowait(None)

    async def read(self) -> dict[str, Any] | str | None:
        value = await self._stdout.get()
        if value is None:
            await self.wait()
        return value

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
        if self.child.returncode is not None:
            return False
        try:
            self.child.send_signal(sig)
        except ProcessLookupError:
            return False
        self._recorder.emit("signal", proc=self.name, name=sig.name)
        return True

    async def wait_for_exit(self, seconds: float) -> bool:
        if self._watcher.done():
            await self.wait()
            return True
        try:
            await asyncio.wait_for(self.wait(), seconds)
        except TimeoutError:
            return False
        return True

    async def _stop_child(self, grace: float) -> None:
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
