"""Publish bounded host observations to the owning web job, when there is one."""

import fcntl
import json
import os
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime

FD_ENV = "MULTI_AGENT_JOB_ACTIVITY_FD"
_writer: ContextVar[int | None] = ContextVar("job_activity_writer", default=None)


@dataclass
class _Actor:
    kind: str
    identifier: str
    seat: str
    provider: str | None
    attempt: int
    last_message: float | None = None


_actor: ContextVar[_Actor | None] = ContextVar("job_activity_actor", default=None)


@contextmanager
def capture_activity() -> Iterator[None]:
    raw = os.environ.pop(FD_ENV, None)
    if raw is None:
        yield
        return
    descriptor = int(raw)
    if descriptor < 3 or not stat.S_ISREG(os.fstat(descriptor).st_mode):
        raise ValueError("invalid job activity descriptor")
    os.set_inheritable(descriptor, False)
    if not (fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_APPEND):
        raise ValueError("job activity descriptor must be append-only")
    token = _writer.set(descriptor)
    scope = _actor.set(None)
    try:
        yield
    finally:
        _actor.reset(scope)
        _writer.reset(token)
        os.close(descriptor)


def start_actor(
    kind: str, identifier: str, seat: str, provider: str | None, attempt: int
) -> None:
    if _writer.get() is not None:
        _actor.set(_Actor(kind, identifier, seat, provider, attempt))
        observe("attempt_started")


def observe(event: str, *, state: str | None = None) -> None:
    """Record host-controlled metadata only; failed telemetry cannot change a run."""
    descriptor, actor = _writer.get(), _actor.get()
    if descriptor is None or actor is None:
        return
    if event == "message":
        clock = time.monotonic()
        if actor.last_message is not None and clock - actor.last_message < 15:
            return
        actor.last_message = clock
    record = {
        "at": datetime.now(UTC).isoformat(),
        "event": event,
        "state": state,
        "resource": {"kind": actor.kind, "id": actor.identifier},
        "seat": actor.seat,
        "provider": actor.provider,
        "attempt": actor.attempt,
    }
    try:
        os.write(
            descriptor, (json.dumps(record, separators=(",", ":")) + "\n").encode()
        )
    except OSError:
        pass
