"""Admit a product turn against the workbench's local provider budget."""

import argparse
import asyncio
import os
import sys
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from multi_agent.core.events import Error, FailureCategory
from multi_agent.job_activity import observe
from multi_agent.store.provider_limits import (
    ProviderExhausted,
    ProviderLedger,
    ProviderLimits,
    ProviderReservation,
    ProviderWait,
)

from .provider_watchdog import start_watchdog, system_deadline
from .runtime_decode import RuntimeFailure

type ProviderBudget = tuple[ProviderLedger, ProviderLimits]
budget_for_provider: ContextVar[Callable[[str], ProviderBudget | None] | None] = (
    ContextVar("budget_for_provider", default=None)
)

_SOURCE = "adapter.provider_limits"
_WAIT_SECONDS = 0.05


def limit_failure(
    detail: str, category: FailureCategory | None = None
) -> RuntimeFailure:
    return RuntimeFailure(Error(detail, category, raw_type=_SOURCE))


def _start_before_deadline(
    ledger: ProviderLedger,
    reservation_id: str,
    process_group: int,
    deadline: float,
    clock: Callable[[], float],
) -> bool:
    if clock() >= deadline:
        raise TimeoutError("expired before launching the model")
    started = ledger.start(reservation_id, process_group)
    if started and clock() >= deadline:
        ledger.withdraw_unlaunched(reservation_id)
        raise TimeoutError("expired while registering the model launch")
    return started


@dataclass(slots=True)
class ProviderTurn:
    provider: str
    ledger: ProviderLedger
    reservation: ProviderReservation
    admitted_at: float
    terminal_at: float | None = None

    @property
    def deadline(self) -> float:
        return self.admitted_at + self.reservation.run_seconds

    @property
    def limit_field(self) -> str | None:
        return self.reservation.limit_field

    def start(self, process_group: int) -> None:
        if not _start_before_deadline(
            self.ledger,
            self.reservation.reservation_id,
            process_group,
            self.deadline,
            asyncio.get_running_loop().time,
        ):
            raise limit_failure(
                f"{self.provider}: local turn reservation is unavailable"
            )

    def claude_command(
        self, argv: list[str], *, grace: float, control_fd: int
    ) -> list[str]:
        # A controller can exit between spawning a child and receiving its PID.
        # Register the new group inside that child before it can launch Claude.
        return [
            sys.executable,
            "-I",
            "-m",
            __name__,
            str(self.ledger.path.parent),
            self.reservation.reservation_id,
            str(system_deadline(self.deadline)),
            str(grace),
            str(control_fd),
            *argv,
        ]

    def finish(self, now: float, *, stopped: bool) -> None:
        stopped_at = self.terminal_at if self.terminal_at is not None else now
        try:
            self.ledger.settle(
                self.reservation,
                elapsed=max(0.0, stopped_at - self.admitted_at),
                stopped=stopped,
            )
        finally:
            self.reservation.close()


def current_provider_budget(provider: str) -> ProviderBudget | None:
    lookup = budget_for_provider.get()
    if lookup is None:
        return None
    try:
        return lookup(provider)
    except (OSError, TypeError, ValueError) as exc:
        raise limit_failure(str(exc)) from exc


async def admit_turn(
    provider: str,
    deadline: float,
    cancelled: Callable[[], bool],
    budget: ProviderBudget,
    *,
    process_group: int | None = None,
) -> ProviderTurn:
    ledger, limits = budget
    clock = asyncio.get_running_loop().time
    waiting = False
    while True:
        if cancelled():
            raise limit_failure(
                f"{provider}: cancelled while waiting for a local provider limit",
                FailureCategory.CANCELLED,
            )
        now = clock()
        remaining = deadline - now
        if remaining <= 0:
            raise limit_failure(
                f"{provider}: execution deadline exceeded while waiting for a local provider limit",
                FailureCategory.TIMEOUT,
            )
        decision = ledger.reserve(
            provider,
            limits,
            datetime.now(UTC),
            remaining,
            os.getpid(),
            process_group,
        )
        if isinstance(decision, ProviderReservation):
            if waiting:
                observe("provider_admitted")
            return ProviderTurn(provider, ledger, decision, now)
        if isinstance(decision, ProviderExhausted):
            raise limit_failure(
                f"{provider}: local provider limit {decision.field} is exhausted for this UTC day",
                FailureCategory.RATE_LIMITED,
            )
        assert isinstance(decision, ProviderWait)
        if not waiting:
            observe("provider_wait")
            waiting = True
        await asyncio.sleep(min(_WAIT_SECONDS, remaining))


def _launch_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_dir", type=Path)
    parser.add_argument("reservation_id")
    parser.add_argument("deadline", type=float)
    parser.add_argument("grace", type=float)
    parser.add_argument("control_fd", type=int)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if not args.command:
        parser.error("a CLI executable is required")
    ledger = ProviderLedger(args.data_dir)
    try:
        started = _start_before_deadline(
            ledger, args.reservation_id, os.getpgrp(), args.deadline, time.monotonic
        )
    except TimeoutError:
        print("Claude: local model deadline expired before launch", file=sys.stderr)
        return 124
    if not started:
        print("Claude: local turn reservation is unavailable", file=sys.stderr)
        return 125
    try:
        try:
            start_watchdog(
                args.grace, control_fd=args.control_fd, deadline=args.deadline
            )
        finally:
            os.close(args.control_fd)
    except OSError:
        ledger.withdraw_unlaunched(args.reservation_id)
        raise
    if time.monotonic() >= args.deadline:
        ledger.withdraw_unlaunched(args.reservation_id)
        print("Claude: local model deadline expired before launch", file=sys.stderr)
        return 124
    os.execvp(args.command[0], args.command)


if __name__ == "__main__":
    sys.exit(_launch_cli(sys.argv[1:]))
