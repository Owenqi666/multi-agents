"""Bounded wire and binary output logs for the independent test executor."""

import asyncio
import hashlib
import os
import stat
from pathlib import Path
from typing import Any

from .ownership import Acquisition, LogOwnership
from .probe.process import Process
from .probe.recording import Recorder, Redactor
from .runtime_decode import RuntimeJSONError


class OutputLimitExceeded(OSError):
    """The recorder retained a prefix, never a qualified complete output."""


def _state(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def closed_digest(
    path: Path, identity: tuple[int, int], mode: int, count: int, digest: str
) -> bool:
    before = path.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or (before.st_dev, before.st_ino) != identity
        or before.st_mode != mode
        or before.st_size != count
    ):
        return False
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if _state(os.fstat(descriptor)) != _state(before):
            return False
        observed = hashlib.sha256()
        remaining = count
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                return False
            observed.update(chunk)
            remaining -= len(chunk)
        return (
            not os.read(descriptor, 1)
            and observed.hexdigest() == digest
            and _state(os.fstat(descriptor)) == _state(before)
            and _state(path.lstat()) == _state(before)
        )
    finally:
        os.close(descriptor)


def _freeze_mode(path: Path, identity: tuple[int, int], mode: int) -> int:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or (info.st_dev, info.st_ino) != identity
            or info.st_mode != mode
        ):
            raise OSError("test log identity or mode changed before sealing")
        os.fchmod(descriptor, 0o400)
        after = os.fstat(descriptor)
        if _state(after) != _state(path.lstat()):
            raise OSError("test log was replaced while sealing")
        return after.st_mode
    finally:
        os.close(descriptor)


class TestWireRecorder(Recorder):
    """Keep the existing redaction/audit wire shape with bounded verification."""

    __test__ = False

    def __init__(
        self,
        path: Path,
        redactor: Redactor,
        *,
        limit: int,
        acquisition: Acquisition | None = None,
    ) -> None:
        self.limit = limit
        self.limit_reached = False
        super().__init__(path, redactor, acquisition=acquisition)

    def emit(self, kind: str, **fields: Any) -> None:
        if self.limit_reached:
            raise OutputLimitExceeded("test wire log byte limit exceeded")
        # The transport bounds each untrusted line before decoding it. Reserve
        # a conservative serialized-size bound before the base recorder writes.
        import json

        estimate = (
            len(json.dumps(fields, ensure_ascii=True, default=str).encode()) + 1024
        )
        if self._byte_count + estimate > self.limit:
            self._healthy = False
            self.limit_reached = True
            raise OutputLimitExceeded("test wire log byte limit exceeded")
        super().emit(kind, **fields)
        if self._byte_count > self.limit:
            self._healthy = False
            self.limit_reached = True
            raise OutputLimitExceeded("test wire log byte limit exceeded")

    def _matches_closed_file(self) -> bool:
        return (
            self._identity is not None
            and self._mode is not None
            and closed_digest(
                self._path,
                self._identity,
                self._mode,
                self._byte_count,
                self._digest.hexdigest(),
            )
        )

    def close(self) -> None:
        if self._closed:
            return
        super().close()
        if self._identity is not None and self._mode is not None:
            try:
                self._mode = _freeze_mode(self._path, self._identity, self._mode)
            except BaseException:
                self._healthy = False
                raise


class RawOutputLog:
    """Exact received bytes; write/close failures permanently revoke the seal."""

    def __init__(self, path: Path, limit: int, acquisition: Acquisition) -> None:
        self.path = path
        self.limit = limit
        self.received = 0
        self.limit_reached = False
        self._count = 0
        self._records = 0
        self._digest = hashlib.sha256()
        self._identity: tuple[int, int] | None = None
        self._mode = 0
        self._closed = False
        self._healthy = True
        self._descriptor: int | None = None
        try:
            try:
                self._descriptor = os.open(
                    path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
                )
            except OSError:
                acquisition.not_created()
                raise
            acquisition.publish(self)
            info = os.fstat(self._descriptor)
            self._identity = (info.st_dev, info.st_ino)
            self._mode = info.st_mode
        except BaseException as original:
            self._healthy = False
            try:
                self.close()
            except OSError as error:
                original.add_note(f"test log cleanup failed: {type(error).__name__}")
            raise
        finally:
            acquisition.finish()

    def write(self, data: bytes) -> None:
        self.received += len(data)
        try:
            if self._closed or self._descriptor is None:
                raise OSError("test output log is closed")
            if self.received > self.limit:
                self.limit_reached = True
                raise OutputLimitExceeded("test output byte limit exceeded")
            remaining = memoryview(data)
            while remaining:
                size = os.write(self._descriptor, remaining)
                if size <= 0:
                    raise OSError("test output write did not progress")
                self._digest.update(remaining[:size])
                self._count += size
                remaining = remaining[size:]
            self._records += 1
        except BaseException:
            self._healthy = False
            raise

    def close(self) -> None:
        if self._closed:
            return
        try:
            if self._descriptor is not None:
                try:
                    os.fsync(self._descriptor)
                finally:
                    os.close(self._descriptor)
            if self._identity is not None:
                self._mode = _freeze_mode(self.path, self._identity, self._mode)
        except BaseException:
            self._healthy = False
            raise
        finally:
            self._closed = True
            self._descriptor = None

    def ownership_record(self, resource_id: str) -> LogOwnership:
        sealed = False
        if self._closed and self._healthy and self._identity is not None:
            try:
                sealed = closed_digest(
                    self.path,
                    self._identity,
                    self._mode,
                    self._count,
                    self._digest.hexdigest(),
                )
            except OSError:
                sealed = False
            self._healthy = sealed
        return LogOwnership(
            resource_id,
            str(self.path),
            self._identity,
            self._records,
            self._closed,
            self._healthy,
            self._count if sealed else None,
            self._digest.hexdigest() if sealed else None,
            sealed,
        )


class BoundedTestProcess(Process):
    """Bound all input before JSON decoding, recording, or queue insertion."""

    MAX_LINE = 1024 * 1024
    MAX_WIRE = 64 * 1024 * 1024
    MAX_MESSAGES = 8192

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._received_wire = 0
        self._message_count = 0
        super().__init__(*args, **kwargs)

    def _line(self, data: bytes, stream: str) -> None:
        self._message_count += 1
        if self._message_count > self.MAX_MESSAGES or len(data) > self.MAX_LINE:
            self._decode_error(data[: self.MAX_LINE], "wire_limit")
            raise RuntimeJSONError("test runtime message budget exceeded")
        super()._line(data, stream)

    async def _read_stream(self, reader: asyncio.StreamReader, stream: str) -> None:
        buffered = bytearray()
        while chunk := await reader.read(8192):
            self._received_wire += len(chunk)
            if self._received_wire > self.MAX_WIRE:
                self._decode_error(chunk, "wire_limit")
                raise RuntimeJSONError("test runtime wire budget exceeded")
            for index, part in enumerate(chunk.split(b"\n")):
                if index:
                    self._line(bytes(buffered), stream)
                    buffered.clear()
                if len(buffered) + len(part) > self.MAX_LINE:
                    self._decode_error(bytes(buffered), "wire_limit")
                    raise RuntimeJSONError("test runtime line budget exceeded")
                buffered.extend(part)
        if buffered:
            self._line(bytes(buffered), stream)
        self._eof[stream] = True
