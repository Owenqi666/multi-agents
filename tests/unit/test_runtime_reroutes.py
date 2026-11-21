"""Check the recorded source of a Codex reroute without invoking a model."""

import json
from pathlib import Path

import pytest

from multi_agent.adapters.runtime_decode import CodexDecoder, codex_reroutes


def _transcript(*rows):
    return b"".join((json.dumps(row) + "\n").encode() for row in rows)


def _send(message, *, process="codex"):
    return {"kind": "send", "proc": process, "message": message}


def _recv(message, *, process="codex", stream="stdout"):
    return {"kind": "recv", "proc": process, "stream": stream, "message": message}


def _thread():
    return (
        _send({"id": 4, "method": "thread/start", "params": {}}),
        _recv({"id": 4, "result": {"thread": {"id": "session"}}}),
    )


def _turn_request(identifier=5):
    return _send(
        {"id": identifier, "method": "turn/start", "params": {"threadId": "session"}}
    )


def _turn_response(identifier=5, *, process="codex"):
    return _recv(
        {"id": identifier, "result": {"turn": {"id": "turn"}}}, process=process
    )


def _reroute(**overrides):
    return {
        "method": "model/rerouted",
        "params": {
            "threadId": "session",
            "turnId": "turn",
            "fromModel": "reported-original",
            "toModel": "reported-destination",
            "reason": "highRiskCyberActivity",
            **overrides,
        },
    }


def test_recorded_codex_turn_without_reroutes_is_confirmed_empty():
    log = (
        Path(__file__).parents[1] / "fixtures/runtime/codex-0.169.0/codex-turn.jsonl"
    ).read_bytes()
    assert codex_reroutes(log, "01a1e035-a4b6-7300-9f82-922c09c88aa3") == []
    assert codex_reroutes(log, "another-attempt") is None


def test_only_server_notifications_for_the_recorded_turn_are_projected():
    valid = _reroute()
    raw = _transcript(
        *_thread(),
        _recv(_reroute(fromModel="before-turn")),
        _turn_request(),
        _recv(_reroute(threadId="another-thread")),
        _recv(_reroute(turnId="another-turn")),
        _recv(_reroute(fromModel="another-process"), process="version"),
        _recv(_reroute(fromModel="stderr"), stream="stderr"),
        _recv({**_reroute(fromModel="server-request"), "id": 5}),
        _send(_reroute(fromModel="client")),
        _recv(valid),  # App Server can notify while the request is awaiting its reply.
        _turn_response(),
        _recv(_reroute(fromModel="reported-destination", toModel="reported-later")),
    )
    assert codex_reroutes(raw, "session") == [
        {
            "source": "model/rerouted",
            "from_model": "reported-original",
            "to_model": "reported-destination",
            "reason": "highRiskCyberActivity",
        },
        {
            "source": "model/rerouted",
            "from_model": "reported-destination",
            "to_model": "reported-later",
            "reason": "highRiskCyberActivity",
        },
    ]
    assert CodexDecoder("session", "turn", structured=True).decode(valid) == ()


@pytest.mark.parametrize(
    "suffix",
    [
        [_turn_response(99)],
        [_turn_response(process="other-process")],
        [_turn_response(), _turn_response()],
        [_turn_response(), _turn_request(6)],
        [_turn_response(), _recv(_reroute(reason=None))],
    ],
    ids=[
        "mismatched-reply-id",
        "mismatched-process",
        "duplicate-reply",
        "ambiguous-turn",
        "malformed-reroute",
    ],
)
def test_unverifiable_reroutes_remain_unknown(suffix):
    assert (
        codex_reroutes(_transcript(*_thread(), _turn_request(), *suffix), "session")
        is None
    )


def test_malformed_transcript_remains_unknown():
    raw = _transcript(*_thread(), _turn_request(), _turn_response())
    assert codex_reroutes(raw + b"{invalid JSON line\n", "session") is None
