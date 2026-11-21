"""Shared local admission accounting, separate from workflow and review evidence."""

import fcntl
import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4


@dataclass(frozen=True, slots=True)
class ProviderLimits:
    max_concurrent: int | None = None
    max_calls_per_day: int | None = None
    max_turn_seconds: float | None = None
    max_seconds_per_day: float | None = None


@dataclass(slots=True)
class ProviderReservation:
    reservation_id: str
    run_seconds: float
    limit_field: str | None
    _guard_fd: int | None = field(repr=False, compare=False)

    def close(self) -> None:
        """Drop this controller's ownership. The ledger still owns its accounting."""
        fd, self._guard_fd = self._guard_fd, None
        if fd is not None:
            os.close(fd)


@dataclass(frozen=True, slots=True)
class ProviderWait:
    field: str


@dataclass(frozen=True, slots=True)
class ProviderExhausted:
    field: str


@dataclass(frozen=True, slots=True)
class _Allowance:
    seconds: float
    limit_field: str | None


_SCHEMA_VERSION = 1
_GUARD_OPEN = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_RDWR


def _group_alive(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class ProviderLedger:
    """Share an independent SQLite sidecar among controllers of one workbench."""

    def __init__(self, data_dir: Path) -> None:
        directory = data_dir.resolve()
        self.path = directory / "provider-limits.sqlite3"
        self._guards = directory / "provider-limit-owners"

    @contextmanager
    def _write(self, *, timeout: float) -> Iterator[sqlite3.Connection]:
        fd = os.open(self.path, _GUARD_OPEN | os.O_CREAT, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("provider limits ledger must be a regular file")
        finally:
            os.close(fd)
        connection = sqlite3.connect(self.path, timeout=timeout, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version == 0:
                    connection.execute(
                        """CREATE TABLE provider_reservations (
                            reservation_id TEXT PRIMARY KEY,
                            provider TEXT NOT NULL CHECK (provider IN ('codex', 'claude')),
                            utc_day TEXT NOT NULL,
                            controller_pid INTEGER NOT NULL CHECK (controller_pid > 0),
                            process_group INTEGER CHECK (process_group > 0),
                            reserved_seconds REAL NOT NULL CHECK (reserved_seconds > 0),
                            elapsed_seconds REAL NOT NULL DEFAULT 0
                                CHECK (elapsed_seconds >= 0 AND elapsed_seconds <= reserved_seconds),
                            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                            started INTEGER NOT NULL DEFAULT 0 CHECK (started IN (0, 1)),
                            CHECK (started = 0 OR process_group IS NOT NULL)
                        )"""
                    )
                    connection.execute(
                        "CREATE INDEX active_provider_reservations "
                        "ON provider_reservations(provider) WHERE active = 1"
                    )
                    connection.execute(
                        "CREATE INDEX daily_provider_reservations "
                        "ON provider_reservations(provider, utc_day)"
                    )
                    connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                elif version != _SCHEMA_VERSION:
                    raise ValueError(
                        f"unsupported provider limits ledger version {version}"
                    )
                yield connection
        finally:
            connection.close()

    def _guard_path(self, reservation_id: str) -> Path:
        # Reservation IDs are generated before publishing the row and can be
        # passed back by the gated subprocess. Canonicalize before opening a file.
        if UUID(reservation_id).hex != reservation_id:
            raise ValueError("invalid provider reservation id")
        return self._guards / reservation_id

    def _guard_held(self, reservation_id: str) -> bool:
        try:
            fd = os.open(self._guard_path(reservation_id), _GUARD_OPEN)
        except FileNotFoundError:
            return False
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError(
                    "provider reservation owner guard must be a regular file"
                )
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            return False
        finally:
            os.close(fd)

    def _sweep(self, connection: sqlite3.Connection, provider: str) -> None:
        rows = connection.execute(
            """SELECT reservation_id, process_group, started
               FROM provider_reservations WHERE provider = ? AND active = 1""",
            (provider,),
        ).fetchall()
        for row in rows:
            identifier = row["reservation_id"]
            if self._guard_held(identifier):
                continue
            if row["started"] and _group_alive(row["process_group"]):
                continue
            # A dead controller cannot start a pending reservation. An orphaned
            # started turn has no measured end, so settle its reserved allowance.
            connection.execute(
                """UPDATE provider_reservations SET active = 0,
                        elapsed_seconds = CASE WHEN started = 1 THEN reserved_seconds ELSE 0 END
                   WHERE reservation_id = ?""",
                (identifier,),
            )
            # Only remove a guard with no holder. If the transaction rolls
            # back, the next sweep will still see that the controller is gone.
            self._guard_path(identifier).unlink(missing_ok=True)

    def reserve(
        self,
        provider: str,
        limits: ProviderLimits,
        now: datetime,
        run_seconds: float,
        controller_pid: int,
        process_group: int | None = None,
    ) -> ProviderReservation | ProviderWait | ProviderExhausted:
        """Check and reserve all dimensions in one transaction."""
        if now.utcoffset() is None:
            raise ValueError("provider reservation time must include a time zone")
        day = now.astimezone(UTC).date().isoformat()
        reservation: ProviderReservation | None = None
        try:
            with self._write(timeout=0.0) as connection:
                self._sweep(connection, provider)
                allowance = _allowance(connection, provider, day, limits, run_seconds)
                if isinstance(allowance, _Allowance):
                    self._guards.mkdir(mode=0o700, exist_ok=True)
                    identifier = uuid4().hex
                    fd = os.open(
                        self._guard_path(identifier),
                        _GUARD_OPEN | os.O_CREAT | os.O_EXCL,
                        0o600,
                    )
                    reservation = ProviderReservation(
                        identifier, allowance.seconds, allowance.limit_field, fd
                    )
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    connection.execute(
                        """INSERT INTO provider_reservations
                           (reservation_id, provider, utc_day, controller_pid, process_group, reserved_seconds)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            identifier,
                            provider,
                            day,
                            controller_pid,
                            process_group,
                            allowance.seconds,
                        ),
                    )
        except BaseException as error:
            if reservation is not None:
                reservation.close()
                self._guard_path(reservation.reservation_id).unlink(missing_ok=True)
            if isinstance(error, sqlite3.OperationalError) and (
                getattr(error, "sqlite_errorcode", 0) & 0xFF
            ) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                return ProviderWait("ledger_busy")
            raise
        if reservation is not None:
            return reservation
        assert not isinstance(allowance, _Allowance)
        return allowance

    def start(self, reservation_id: str, process_group: int) -> bool:
        """Commit the launch fence before the authorized child can execute."""
        with self._write(timeout=0.25) as connection:
            if not self._guard_held(reservation_id):
                return False
            updated = connection.execute(
                """UPDATE provider_reservations SET started = 1, process_group = ?
                   WHERE reservation_id = ? AND active = 1 AND started = 0
                     AND (process_group IS NULL OR process_group = ?)""",
                (process_group, reservation_id, process_group),
            )
            return updated.rowcount == 1

    def withdraw_unlaunched(self, reservation_id: str) -> None:
        """Undo the start fence when the owner can still prevent launch."""
        with self._write(timeout=0.25) as connection:
            if self._guard_held(reservation_id):
                connection.execute(
                    """UPDATE provider_reservations SET started = 0
                       WHERE reservation_id = ? AND active = 1""",
                    (reservation_id,),
                )

    def finish(self, reservation_id: str, elapsed: float, stopped: bool) -> None:
        """Settle a stopped turn once, leaving an unconfirmed stop reserved."""
        if not stopped:
            return
        guard = self._guard_path(reservation_id)
        with self._write(timeout=0.25) as connection:
            updated = connection.execute(
                """UPDATE provider_reservations SET active = 0,
                        elapsed_seconds = CASE WHEN started = 1 THEN MIN(reserved_seconds, ?) ELSE 0 END
                   WHERE reservation_id = ? AND active = 1""",
                (elapsed, reservation_id),
            ).rowcount
        if updated == 1:
            guard.unlink(missing_ok=True)


def _allowance(
    connection: sqlite3.Connection,
    provider: str,
    day: str,
    limits: ProviderLimits,
    run_seconds: float,
) -> _Allowance | ProviderWait | ProviderExhausted:
    counts = connection.execute(
        """SELECT COUNT(CASE WHEN started = 1 THEN 1 END) AS used_calls,
                  COUNT(CASE WHEN active = 1 AND started = 0 THEN 1 END) AS held_calls,
                  COALESCE(SUM(elapsed_seconds), 0) AS used_seconds,
                  COALESCE(SUM(CASE WHEN active = 1 THEN reserved_seconds END), 0) AS held_seconds
           FROM provider_reservations WHERE provider = ? AND utc_day = ?""",
        (provider, day),
    ).fetchone()
    calls, held_calls, used, held = counts
    if limits.max_calls_per_day is not None and calls >= limits.max_calls_per_day:
        return ProviderExhausted("max_calls_per_day")
    if limits.max_seconds_per_day is not None and used >= limits.max_seconds_per_day:
        return ProviderExhausted("max_seconds_per_day")
    if limits.max_concurrent is not None:
        active = connection.execute(
            "SELECT COUNT(*) FROM provider_reservations WHERE provider = ? AND active = 1",
            (provider,),
        ).fetchone()[0]
        if active >= limits.max_concurrent:
            return ProviderWait("max_concurrent")
    if (
        limits.max_calls_per_day is not None
        and calls + held_calls >= limits.max_calls_per_day
    ):
        return ProviderWait("max_calls_per_day")
    limit_field = None
    if limits.max_turn_seconds is not None and limits.max_turn_seconds < run_seconds:
        run_seconds = limits.max_turn_seconds
        limit_field = "max_turn_seconds"
    if limits.max_seconds_per_day is not None:
        available = limits.max_seconds_per_day - used - held
        if available <= 0:
            return ProviderWait("max_seconds_per_day")
        if available < run_seconds:
            run_seconds = available
            limit_field = "max_seconds_per_day"
    return _Allowance(run_seconds, limit_field)
