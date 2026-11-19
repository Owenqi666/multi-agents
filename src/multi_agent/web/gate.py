"""Coordinate lock-taking HTTP reads with the active CLI job."""

from collections.abc import Iterator
from contextlib import contextmanager
from threading import Condition


class ReadGate:
    def __init__(self) -> None:
        self._changed = Condition()
        self._reading = False
        self._closed = False
        self._active_job_id: str | None = None

    @property
    def active_job_id(self) -> str | None:
        with self._changed:
            return self._active_job_id

    @contextmanager
    def read(self, *, guarded: bool = True) -> Iterator[bool]:
        if not guarded:
            yield True
            return
        with self._changed:
            self._changed.wait_for(
                lambda: (
                    self._closed or self._active_job_id is not None or not self._reading
                )
            )
            allowed = not self._closed and self._active_job_id is None
            if allowed:
                self._reading = True
        try:
            yield allowed
        finally:
            if allowed:
                with self._changed:
                    self._reading = False
                    self._changed.notify_all()

    def begin_job(self, identifier: str) -> None:
        with self._changed:
            self._active_job_id = identifier
            self._changed.notify_all()

    def wait_for_read(self) -> None:
        with self._changed:
            self._changed.wait_for(lambda: not self._reading)

    def stop_reads(self) -> None:
        with self._changed:
            self._closed = True
            self._changed.notify_all()

    def finish_job(self) -> None:
        with self._changed:
            self._active_job_id = None
            self._changed.notify_all()
