"""Keep a provider's deadline in its own dedicated process group."""

import argparse
import asyncio
import math
import os
import select
import signal
import socket
import struct
import sys
import time

_BOUND = struct.Struct("!cd")
_ARM = b"A"
_CLEANUP = b"C"
_ARMED = b"1"
_POLL_SECONDS = 0.02


def system_deadline(deadline: float) -> float:
    """Translate a loop clock value for a different interpreter."""
    return time.monotonic() + (deadline - asyncio.get_running_loop().time())


class ProviderWatchdog:
    """Send the work bound and first cleanup bound over a private channel."""

    def __init__(self, grace: float, *, prearmed: bool = False) -> None:
        self.grace = grace
        self._armed = prearmed
        self._cleanup_sent = False
        self._owner, self._launch = socket.socketpair()
        self._owner.setblocking(False)

    @property
    def launch_fd(self) -> int:
        return self._launch.fileno()

    def codex_command(self, argv: list[str]) -> list[str]:
        return [
            sys.executable,
            "-I",
            "-m",
            __name__,
            str(self.launch_fd),
            str(self.grace),
            *argv,
        ]

    def close_launch_end(self) -> None:
        self._launch.close()

    async def arm(self, deadline: float) -> None:
        loop = asyncio.get_running_loop()
        await loop.sock_sendall(
            self._owner, _BOUND.pack(_ARM, system_deadline(deadline))
        )
        if await loop.sock_recv(self._owner, 1) != _ARMED:
            raise OSError("provider watchdog did not adopt the turn deadline")
        self._armed = True

    def begin_cleanup(self) -> None:
        if not self._armed or self._cleanup_sent:
            return
        self._cleanup_sent = True
        try:
            self._owner.sendall(_BOUND.pack(_CLEANUP, time.monotonic() + self.grace))
        except OSError:
            # The group may already be stopped. The watchdog otherwise
            # retains its previous bound, and the controller still cleans up.
            pass

    def close(self) -> None:
        self._launch.close()
        self._owner.close()


def _watch(
    leader: int,
    ready: int,
    grace: float,
    control: socket.socket,
    deadline: float | None,
) -> None:
    # Both signals may be delivered to the provider for ordinary graceful
    # shutdown. Keep the deadline protected until the provider really exits.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    for fd in (0, 1, 2):
        os.close(fd)
    os.write(ready, _ARMED)
    os.close(ready)
    channel: socket.socket | None = control
    frame = bytearray()
    force_at: float | None = None
    cleaning = False
    while os.getppid() == leader:
        now = time.monotonic()
        until = force_at if force_at is not None else deadline
        delay = (
            _POLL_SECONDS
            if until is None
            else max(0.0, min(_POLL_SECONDS, until - now))
        )
        if channel is None:
            time.sleep(delay)
        else:
            readable, _, _ = select.select([channel], [], [], delay)
            if readable:
                part = channel.recv(_BOUND.size - len(frame))
                if not part:
                    channel.close()
                    channel = None
                    # An unarmed app-server cannot have an authorized turn.
                    if deadline is None:
                        break
                    continue
                frame.extend(part)
                if len(frame) != _BOUND.size:
                    continue
                operation, bound = _BOUND.unpack(frame)
                frame.clear()
                if not math.isfinite(bound):
                    break
                if operation == _ARM and deadline is None:
                    try:
                        channel.sendall(_ARMED)
                    except BrokenPipeError:
                        break
                    deadline = bound
                elif (
                    operation == _CLEANUP
                    and deadline is not None
                    and not cleaning
                    and force_at is None
                ):
                    deadline = bound
                    cleaning = True
                # Give a queued control frame priority over the timer.
                continue
        now = time.monotonic()
        if force_at is not None and now >= force_at:
            break
        if deadline is not None and force_at is None and now >= deadline:
            os.killpg(os.getpgrp(), signal.SIGTERM)
            force_at = deadline + grace


def start_watchdog(
    grace: float, *, control_fd: int, deadline: float | None = None
) -> None:
    """Start the watcher while the original executable is still gated."""
    leader = os.getpid()
    if os.getpgrp() != leader or os.getsid(0) != leader:
        raise ValueError("provider watchdog requires its own process group and session")
    if control_fd < 3:
        raise ValueError("provider watchdog requires a separate control descriptor")
    ready_reader, ready_writer = os.pipe()
    try:
        if os.fork() == 0:
            try:
                os.close(ready_reader)
                channel = socket.socket(fileno=control_fd)
                _watch(leader, ready_writer, grace, channel, deadline)
            finally:
                # This process still belongs to the group it is terminating.
                # It never signals a numeric group from an archived receipt.
                os.killpg(os.getpgrp(), signal.SIGKILL)
                os._exit(125)
        os.close(ready_writer)
    except BaseException:
        os.close(ready_reader)
        os.close(ready_writer)
        raise
    try:
        if os.read(ready_reader, 1) != _ARMED:
            raise OSError("provider watchdog did not start")
    finally:
        os.close(ready_reader)


def _launch(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("control_fd", type=int)
    parser.add_argument("grace", type=float)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if not args.command:
        parser.error("a CLI executable is required")
    try:
        start_watchdog(args.grace, control_fd=args.control_fd)
    finally:
        os.close(args.control_fd)
    os.execvp(args.command[0], args.command)


if __name__ == "__main__":
    _launch(sys.argv[1:])
