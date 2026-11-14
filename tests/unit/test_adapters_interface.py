import asyncio
from pathlib import Path

import pytest

from multi_agent.adapters import (
    EnvironmentInfo,
    Permission,
    ScriptedAdapter,
    SessionConfig,
    SessionHandle,
    TurnResult,
    TurnScript,
    validate_event_sequence,
)
from multi_agent.core.events import (
    Completed,
    Error,
    FailureCategory,
    Message,
    ProcessExited,
    SessionStarted,
    Usage,
)

CONFIG = SessionConfig("requested-model", "high", Permission.READ_ONLY, Path("/repo"))
EXIT = ProcessExited(130)


def test_normal_lifecycle_preserves_only_reported_evidence() -> None:
    events = (
        SessionStarted("observed-session", raw_type="synthetic.init"),
        Message("partial report", raw_type="synthetic.message"),
        Usage({"output_tokens": 7}, raw_type="synthetic.usage"),
        Completed(
            {"findings": []},
            reported_model="reported-model",
            raw_type="synthetic.result",
        ),
    )

    async def run() -> None:
        adapter = ScriptedAdapter([TurnScript(events, (EXIT,))])
        assert await adapter.check_environment() == EnvironmentInfo()
        session = await adapter.start_session(CONFIG)
        turn = await adapter.submit_turn(session, "review this change")
        assert await adapter.result(turn) == TurnResult()
        observed = tuple([event async for event in adapter.events(turn)])
        assert observed == events
        assert observed[0].reported_model is None
        assert observed[0].reported_effort is None
        assert await adapter.result(turn) == TurnResult(terminal=events[-1])

    asyncio.run(run())


def test_cancellation_is_a_request_until_exit_is_observed() -> None:
    async def run() -> None:
        adapter = ScriptedAdapter([TurnScript((Completed("normal"),), (EXIT,))])
        session = await adapter.start_session(CONFIG)
        turn = await adapter.submit_turn(session, "review")
        await adapter.cancel(turn)
        assert await adapter.result(turn) == TurnResult()
        assert [event async for event in adapter.events(turn)] == [EXIT]
        assert await adapter.result(turn) == TurnResult(process_exit=EXIT)

    asyncio.run(run())


def test_cancellation_during_stream_keeps_prior_events() -> None:
    initial = SessionStarted(
        "session", "observed-model", "medium", raw_type="synthetic.init"
    )
    activity = Message("already received", raw_type="synthetic.message")

    async def run() -> None:
        adapter = ScriptedAdapter(
            [TurnScript((initial, activity, Completed("unused")), (EXIT,))]
        )
        session = await adapter.start_session(CONFIG)
        turn = await adapter.submit_turn(session, "review")
        stream = adapter.events(turn)
        assert await anext(stream) == initial
        assert await anext(stream) == activity
        await adapter.cancel(turn)
        await adapter.cancel(turn)
        assert await adapter.result(turn) == TurnResult()
        assert [event async for event in stream] == [EXIT]
        assert await adapter.result(turn) == TurnResult(process_exit=EXIT)

    asyncio.run(run())


def test_cancellation_may_be_followed_by_a_reported_terminal() -> None:
    completed = Completed("arrived after cancel", reported_model="observed-model")

    async def run() -> None:
        adapter = ScriptedAdapter([TurnScript((Completed("normal"),), (completed,))])
        session = await adapter.start_session(CONFIG)
        turn = await adapter.submit_turn(session, "review")
        await adapter.cancel(turn)
        assert [event async for event in adapter.events(turn)] == [completed]
        assert await adapter.result(turn) == TurnResult(terminal=completed)

    asyncio.run(run())


def test_terminal_and_exit_remain_separate_when_cancel_arrives_late() -> None:
    completed = Completed("report")
    exited = ProcessExited(0)

    async def run() -> None:
        adapter = ScriptedAdapter([TurnScript((completed, exited), (EXIT,))])
        session = await adapter.start_session(CONFIG)
        turn = await adapter.submit_turn(session, "review")
        stream = adapter.events(turn)
        assert await anext(stream) == completed
        assert await adapter.result(turn) == TurnResult(terminal=completed)
        await adapter.cancel(turn)
        assert [event async for event in stream] == [exited]
        assert await adapter.result(turn) == TurnResult(completed, exited)

    asyncio.run(run())


def test_environment_keeps_unavailable_fields_empty() -> None:
    async def run() -> None:
        adapter = ScriptedAdapter(
            [], environment=EnvironmentInfo("reported-version", False)
        )
        await adapter.start_session(CONFIG)
        assert await adapter.check_environment() == EnvironmentInfo(
            "reported-version", False, None
        )
        reported = EnvironmentInfo("version", True, ("model-a", "model-b"))
        assert (
            await ScriptedAdapter([], environment=reported).check_environment()
            == reported
        )

    asyncio.run(run())


def test_turns_are_independent_and_each_stream_has_one_reader() -> None:
    first = Completed("first")
    second = Error("limited", FailureCategory.RATE_LIMITED, "2026-11-14T20:00:00Z")

    async def run() -> None:
        adapter = ScriptedAdapter(
            [TurnScript((first,), (EXIT,)), TurnScript((second,), (EXIT,))]
        )
        session = await adapter.start_session(CONFIG)
        first_turn = await adapter.submit_turn(session, "first")
        second_turn = await adapter.submit_turn(session, "second")
        await adapter.cancel(first_turn)
        assert [event async for event in adapter.events(second_turn)] == [second]
        assert [event async for event in adapter.events(first_turn)] == [EXIT]
        assert await adapter.result(second_turn) == TurnResult(terminal=second)
        assert await adapter.result(first_turn) == TurnResult(process_exit=EXIT)
        with pytest.raises(RuntimeError, match="reader"):
            await anext(adapter.events(first_turn))
        with pytest.raises(ValueError, match="no turn script"):
            await adapter.submit_turn(session, "third")
        with pytest.raises(ValueError, match="unknown session"):
            await adapter.submit_turn(SessionHandle(session.identifier), "foreign")

    asyncio.run(run())


@pytest.mark.parametrize(
    ("events", "cancelled"),
    [
        ((EXIT,), True),
        ((SessionStarted("session"), EXIT), True),
        ((Completed("done"),), False),
        ((Error("unexpected exit"), ProcessExited(1)), False),
        ((Completed("done"), ProcessExited(0)), False),
        ((Message("partial"), Completed("done")), True),
    ],
)
def test_valid_event_sequences(events: tuple, cancelled: bool) -> None:
    validate_event_sequence(events, cancelled_before_exit=cancelled)


@pytest.mark.parametrize(
    ("events", "cancelled"),
    [
        ((), False),
        ((), True),
        ((Message("partial"), Usage({"tokens": 2})), False),
        ((EXIT,), False),
        ((Completed("first"), Completed("second")), False),
        ((Error("failed"), Completed("later")), True),
        ((EXIT, Message("too late")), True),
        ((EXIT, ProcessExited(1)), True),
    ],
)
def test_invalid_event_sequences(events: tuple, cancelled: bool) -> None:
    with pytest.raises(ValueError):
        validate_event_sequence(events, cancelled_before_exit=cancelled)


def test_script_rejects_invalid_sequences_before_consumption() -> None:
    with pytest.raises(ValueError):
        TurnScript((), (EXIT,))
    with pytest.raises(ValueError):
        TurnScript((Completed("done"),), ())
