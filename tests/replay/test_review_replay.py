"""Run the same review scenarios against an interchangeable state backend."""

import json
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pytest

from multi_agent.core import (
    Disposition,
    RoundState,
    SeatConfig,
    SeatState,
    apply,
    round_status,
)
from multi_agent.core.events import (
    AttemptStarted,
    CancelRequested,
    Completed,
    Envelope,
    Error,
    FailureCategory,
    Message,
    ProcessExited,
    SessionStarted,
    Usage,
    thaw_json,
)
from multi_agent.store import ReviewStore

SCENARIOS = Path(__file__).parent / "scenarios"
BASELINE_SCENARIOS = {
    "parallel-seats",
    "rerun-same-snapshot",
    "late-old-attempt",
    "cancel-turn-terminal",
    "cancel-process-exit",
}
EVENT_TYPES = {
    event.kind: event
    for event in (
        AttemptStarted,
        CancelRequested,
        SessionStarted,
        Message,
        Usage,
        Completed,
        Error,
        ProcessExited,
    )
}


class ReplayBackend(Protocol):
    def apply(self, envelope: Envelope) -> Disposition: ...

    def load_round(self) -> RoundState: ...

    def load_history(self) -> tuple[tuple[Envelope, Disposition], ...]: ...


class CoreBackend:
    def __init__(self, state: RoundState) -> None:
        self._state = state
        self._history: list[tuple[Envelope, Disposition]] = []

    def apply(self, envelope: Envelope) -> Disposition:
        self._state, disposition = apply(self._state, envelope)
        self._history.append((envelope, disposition))
        return disposition

    def load_round(self) -> RoundState:
        return self._state

    def load_history(self) -> tuple[tuple[Envelope, Disposition], ...]:
        return tuple(self._history)


class StoreBackend:
    def __init__(self, path: Path, state: RoundState) -> None:
        self.path = path
        self.store = ReviewStore(path)
        try:
            self.store.create_round("replay", state)
        except BaseException:
            self.store.close()
            raise

    def apply(self, envelope: Envelope) -> Disposition:
        return self.store.apply("replay", envelope)

    def load_round(self) -> RoundState:
        return self.store.load_round("replay")

    def load_history(self) -> tuple[tuple[Envelope, Disposition], ...]:
        return self.store.load_history("replay")

    def reopen(self) -> None:
        self.store.close()
        self.store = ReviewStore(self.path)

    def close(self) -> None:
        self.store.close()


@dataclass(frozen=True)
class StepExpectation:
    round_status: str
    disposition: str
    attempt_status: str | None = None


def read_envelope(*, event: dict, **fields: object) -> Envelope:
    event_fields = dict(event)
    event_type = EVENT_TYPES[event_fields.pop("type")]
    if event_type is Error and event_fields.get("failure_category") is not None:
        event_fields["failure_category"] = FailureCategory(
            event_fields["failure_category"]
        )
    return Envelope(event=event_type(**event_fields), **fields)


def read_round(
    *, snapshot_id: str, prompt_template_version: str, seats: list[dict]
) -> RoundState:
    return RoundState(
        snapshot_id,
        prompt_template_version,
        tuple(SeatState(SeatConfig(**seat)) for seat in seats),
    )


def check_final(state: RoundState, expected: dict) -> None:
    actual = {
        "round_status": round_status(state),
        "snapshot_id": state.snapshot_id,
        "prompt_template_version": state.prompt_template_version,
        "seats": {},
    }
    for seat in state.seats:
        attempts = {}
        for attempt in seat.attempts:
            number = str(attempt.number)
            expectation = expected["seats"][seat.config.key]["attempts"][number]
            fields = {
                "status": attempt.status,
                "failure_category": attempt.failure_category,
                "result": thaw_json(attempt.result),
                "exit_code": attempt.exit_code,
            }
            optional = {
                "retry_after": attempt.retry_after,
                "detail": attempt.detail,
                "session_id": attempt.session_id,
                "reported_model": attempt.reported_model,
                "reported_effort": attempt.reported_effort,
            }
            fields.update(
                {key: optional[key] for key in optional.keys() & expectation.keys()}
            )
            attempts[number] = fields
        actual["seats"][seat.config.key] = {
            "provider": seat.config.provider,
            "model": seat.config.model,
            "effort": seat.config.effort,
            "current_attempt": seat.current_attempt,
            "attempts": attempts,
        }
    assert actual == expected


def replay(
    backend: Callable[[RoundState], ReplayBackend],
    *,
    initial: dict,
    steps: list[dict],
    final: dict,
    before_final: Callable[[ReplayBackend], None] | None = None,
) -> tuple[tuple[RoundState, Disposition], ...]:
    runner = backend(read_round(**initial))
    trace = []
    expected_history = []
    for index, step in enumerate(steps, 1):
        assert step.keys() == {"envelope", "expect"}, f"step {index}: unexpected fields"
        envelope = read_envelope(**step["envelope"])
        expectation = StepExpectation(**step["expect"])
        disposition = runner.apply(envelope)
        state = runner.load_round()
        assert disposition == expectation.disposition, f"step {index}: {envelope}"
        assert round_status(state) == expectation.round_status, (
            f"step {index}: {envelope}"
        )
        if expectation.attempt_status is not None:
            seat = next(
                seat for seat in state.seats if seat.config.key == envelope.seat
            )
            attempt = next(
                attempt
                for attempt in seat.attempts
                if attempt.number == envelope.attempt
            )
            assert attempt.status == expectation.attempt_status, (
                f"step {index}: {envelope}"
            )
        trace.append((state, disposition))
        expected_history.append((envelope, Disposition(expectation.disposition)))
    final_state = runner.load_round()
    if before_final is not None:
        before_final(runner)
    assert runner.load_round() == final_state
    check_final(runner.load_round(), final)
    assert runner.load_history() == tuple(expected_history)
    return tuple(trace)


@pytest.mark.parametrize(
    "name",
    sorted(BASELINE_SCENARIOS | {path.stem for path in SCENARIOS.glob("*.json")}),
)
def test_review_scenario(name: str, tmp_path: Path) -> None:
    scenario = json.loads((SCENARIOS / f"{name}.json").read_text(encoding="utf-8"))
    core_trace = replay(CoreBackend, **scenario)
    assert core_trace == replay(CoreBackend, **scenario)
    for run in range(2):
        with ExitStack() as resources:

            def store_backend(
                state: RoundState,
                database_path: Path = tmp_path / f"replay-{run}.sqlite",
            ) -> StoreBackend:
                backend = StoreBackend(database_path, state)
                resources.callback(backend.close)
                return backend

            assert (
                replay(store_backend, before_final=StoreBackend.reopen, **scenario)
                == core_trace
            )


@pytest.mark.parametrize(
    "location", ["root", "seat", "step", "envelope", "event", "expect", "final"]
)
def test_replay_rejects_unknown_keys(location: str) -> None:
    scenario = json.loads(
        (SCENARIOS / "parallel-seats.json").read_text(encoding="utf-8")
    )
    step = scenario["steps"][0]
    target = {
        "root": scenario,
        "seat": scenario["initial"]["seats"][0],
        "step": step,
        "envelope": step["envelope"],
        "event": step["envelope"]["event"],
        "expect": step["expect"],
        "final": scenario["final"],
    }[location]
    target["unknown"] = True
    with pytest.raises((AssertionError, TypeError)):
        replay(CoreBackend, **scenario)
