"""A deterministic, in-memory implementation of the adapter protocol."""

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

from multi_agent.core.events import AdapterEvent, Completed, Error, ProcessExited

from .base import (
    Adapter,
    EnvironmentInfo,
    SessionConfig,
    SessionHandle,
    TurnHandle,
    TurnResult,
)
from .validation import validate_event_sequence


@dataclass(frozen=True, slots=True)
class TurnScript:
    """Normal events and the remaining events after a cancellation request."""

    events: tuple[AdapterEvent, ...]
    cancelled_events: tuple[AdapterEvent, ...]

    def __post_init__(self) -> None:
        validate_event_sequence(self.events, cancelled_before_exit=False)
        validate_event_sequence(self.cancelled_events, cancelled_before_exit=True)


@dataclass
class _Turn:
    script: TurnScript
    cancellation_requested: bool = False
    stream_started: bool = False
    terminal: Completed | Error | None = None
    process_exit: ProcessExited | None = None
    observed: list[AdapterEvent] = field(default_factory=list)


class ScriptedAdapter(Adapter):
    """Consume one script per submission and allow one event reader per turn.

    Cancellation replaces the unconsumed events with cancelled_events. After an
    observed terminal or process exit, cancellation has no further effect.
    Results are snapshots of emitted events and never wait for a reader.
    """

    def __init__(
        self,
        scripts: Sequence[TurnScript],
        *,
        environment: EnvironmentInfo | None = None,
    ) -> None:
        self._scripts = tuple(scripts)
        self._environment = (
            environment if environment is not None else EnvironmentInfo()
        )
        self._sessions: dict[SessionHandle, SessionConfig] = {}
        self._turns: dict[TurnHandle, _Turn] = {}

    async def check_environment(self) -> EnvironmentInfo:
        return self._environment

    async def start_session(self, config: SessionConfig) -> SessionHandle:
        session = SessionHandle(f"script-session-{len(self._sessions) + 1}")
        self._sessions[session] = config
        return session

    async def submit_turn(self, session: SessionHandle, text: str) -> TurnHandle:
        if session not in self._sessions:
            raise ValueError("unknown session")
        index = len(self._turns)
        if index >= len(self._scripts):
            raise ValueError("no turn script remains")
        handle = TurnHandle(f"script-turn-{index + 1}", session)
        self._turns[handle] = _Turn(self._scripts[index])
        return handle

    async def events(self, turn: TurnHandle) -> AsyncIterator[AdapterEvent]:
        state = self._turns[turn]
        if state.stream_started:
            raise RuntimeError("this turn already has an event reader")
        state.stream_started = True
        events = state.script.events
        index = 0
        cancellation_applied = False
        while True:
            await asyncio.sleep(0)
            if state.cancellation_requested and not cancellation_applied:
                events = state.script.cancelled_events
                index = 0
                cancellation_applied = True
            if index == len(events):
                break
            event = events[index]
            index += 1
            state.observed.append(event)
            if isinstance(event, (Completed, Error)):
                state.terminal = event
            if isinstance(event, ProcessExited):
                state.process_exit = event
            yield event
        validate_event_sequence(
            state.observed, cancelled_before_exit=state.cancellation_requested
        )

    async def cancel(self, turn: TurnHandle) -> None:
        state = self._turns[turn]
        if state.terminal is None and state.process_exit is None:
            state.cancellation_requested = True

    async def result(self, turn: TurnHandle) -> TurnResult:
        state = self._turns[turn]
        return TurnResult(state.terminal, state.process_exit)
