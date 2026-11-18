"""Runtime boundary tests only execute the synthetic CLI shipped with this test."""

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from multi_agent.adapters.base import Permission, SessionConfig
from multi_agent.adapters.codex_isolation import CodexIsolation
from multi_agent.adapters.probe.process import (
    Process,
    ProcessCleanupIncomplete,
    SpawnFailed,
)
from multi_agent.adapters.probe.recording import Recorder, Redactor
from multi_agent.adapters.runtime import RuntimeAdapter, RuntimeFailure
from multi_agent.adapters.runtime_decode import CodexDecoder, claude_events
from multi_agent.core.events import (
    Completed,
    Error,
    FailureCategory,
    Message,
    ProcessExited,
    SessionStarted,
    Usage,
    thaw_json,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "runtime"


@pytest.fixture(autouse=True)
def synthetic_codex_auth_and_fake_environment(tmp_path, monkeypatch):
    auth_home = tmp_path / "synthetic-codex-auth"
    auth_home.mkdir(mode=0o700)
    auth = auth_home / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    key: "synthetic-" + key
                    for key in (
                        "id_token",
                        "access_token",
                        "refresh_token",
                        "account_id",
                    )
                },
            }
        )
    )
    auth.chmod(0o600)
    monkeypatch.setenv("CODEX_HOME", str(auth_home))
    original = RuntimeAdapter._environment

    def fake_environment(adapter):
        environment = original(adapter)
        environment.update(
            (key, value) for key, value in os.environ.items() if key.startswith("FAKE_")
        )
        return environment

    monkeypatch.setattr(RuntimeAdapter, "_environment", fake_environment)


def fake(tmp_path, provider):
    executable = tmp_path / f"{provider}-fake"
    body = (Path(__file__).parent / "runtime_fakes" / "fake_cli.py").read_text()
    executable.write_text(f"#!{sys.executable}\n" + body)
    executable.chmod(0o755)
    return str(executable)


def setup(tmp_path, monkeypatch, provider, mode="success", **options):
    monkeypatch.setenv("FAKE_RUNTIME_MODE", mode)
    capture = tmp_path / "capture.jsonl"
    monkeypatch.setenv("FAKE_RUNTIME_CAPTURE", str(capture))
    workdir = tmp_path / "input"
    workdir.mkdir(exist_ok=True)
    adapter = RuntimeAdapter(
        provider,
        executable=fake(tmp_path, provider),
        log_dir=tmp_path / "logs",
        output_schema=options.pop("output_schema", {"type": "object"}),
        timeout=options.pop("timeout", 3),
        grace=0.05,
        **options,
    )
    config = SessionConfig("requested-model", "high", Permission.READ_ONLY, workdir)
    return adapter, config, capture


async def collect(adapter, config):
    session = await adapter.start_session(config)
    turn = await adapter.submit_turn(session, "literal ; `touch nope` $(touch nope)")
    events = [event async for event in adapter.events(turn)]
    return events, await adapter.result(turn)


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_real_transport_success_and_request_evidence(tmp_path, monkeypatch, provider):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "do-not-inherit")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "do-not-inherit")
    adapter, config, capture = setup(tmp_path, monkeypatch, provider)
    events, result = asyncio.run(collect(adapter, config))
    assert isinstance(result.terminal, Completed)
    assert result.terminal.result["summary"] == "ok"
    assert result.terminal.reported_model == (
        "actual-claude" if provider == "claude" else None
    )
    session = next(e for e in events if isinstance(e, SessionStarted))
    assert session.reported_effort is None and session.reported_model is None
    assert result.process_exit.exit_code == 0
    assert isinstance(events[-2], Completed) and isinstance(events[-1], ProcessExited)
    assert not (config.cwd / "nope").exists()
    records = [json.loads(line) for line in capture.read_text().splitlines()]
    spawn = records[0]
    if provider == "claude":
        assert not spawn["api_key_present"] and not spawn["auth_token_present"]
        assert "--json-schema" in spawn["argv"]
    else:
        requests = {
            r["message"].get("method"): r["message"]
            for r in records
            if r["kind"] == "request"
        }
        assert requests["turn/start"]["params"]["outputSchema"] == {"type": "object"}
    log = next((tmp_path / "logs").glob("*.jsonl")).read_text()
    assert "fake stderr" in log
    assert ("future/unknown" if provider == "codex" else "unknown-event") in log
    if provider == "codex":
        assert "model/rerouted" in log
    assert "do-not-inherit" not in log


def test_nonplanning_claude_preserves_declared_schema_on_wire(tmp_path, monkeypatch):
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
    }
    adapter, config, capture = setup(
        tmp_path, monkeypatch, "claude", output_schema=schema
    )
    _, result = asyncio.run(collect(adapter, config))
    assert isinstance(result.terminal, Completed)
    argv = json.loads(capture.read_text().splitlines()[0])["argv"]
    assert argv[argv.index("--json-schema") + 1] == json.dumps(schema)


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_environment_is_explicit_and_preserves_catalog(tmp_path, monkeypatch, provider):
    adapter, config, capture = setup(tmp_path, monkeypatch, provider)
    assert not capture.exists()

    async def run():
        await adapter.start_session(config)
        assert not capture.exists()
        first = await adapter.check_environment()
        second = await adapter.check_environment()
        return first, second

    first, second = asyncio.run(run())
    assert first == second
    assert first.logged_in is True
    assert first.cli_version == (
        "codex-cli 0.169.0" if provider == "codex" else "fake-claude 1.0"
    )
    if provider == "codex":
        assert first.models == ("model-1", "model-2")
        assert first.model_efforts == {
            "model-1": ("high", "low"),
            "model-2": ("high", "low"),
        }
        assert '"method": "turn/start"' not in capture.read_text()
    else:
        assert first.models is None and first.model_efforts is None
    assert len(list((tmp_path / "logs").glob("*.jsonl"))) == 2


@pytest.mark.parametrize("exit_code", [0, 1])
def test_claude_logged_out_auth_preserves_false_status(
    tmp_path, monkeypatch, exit_code
):
    monkeypatch.setenv("FAKE_AUTH_JSON", '{"loggedIn": false}')
    monkeypatch.setenv("FAKE_AUTH_EXIT", str(exit_code))
    adapter, _, capture = setup(tmp_path, monkeypatch, "claude")
    environment = asyncio.run(adapter.check_environment())
    assert environment.logged_in is False
    assert environment.cli_version == "fake-claude 1.0"
    assert environment.models is None and environment.model_efforts is None
    records = [json.loads(line) for line in capture.read_text().splitlines()]
    assert [record["argv"] for record in records] == [
        ["--version"],
        ["auth", "status"],
    ]


@pytest.mark.parametrize("logged_in,exit_code", [(True, 1), (False, 2), (True, 2)])
def test_claude_auth_rejects_inconsistent_or_abnormal_exit(
    tmp_path, monkeypatch, logged_in, exit_code
):
    monkeypatch.setenv("FAKE_AUTH_JSON", json.dumps({"loggedIn": logged_in}))
    monkeypatch.setenv("FAKE_AUTH_EXIT", str(exit_code))
    adapter, _, _ = setup(tmp_path, monkeypatch, "claude")
    with pytest.raises(RuntimeFailure) as failure:
        asyncio.run(adapter.check_environment())
    assert failure.value.event.failure_category is None


@pytest.mark.parametrize("exit_code", [0, 1])
@pytest.mark.parametrize(
    "payload",
    [
        "",
        "not JSON",
        "null",
        "[]",
        "{}",
        '{"loggedIn": null}',
        '{"loggedIn": "false"}',
        '{"loggedIn": 0}',
    ],
)
def test_claude_auth_requires_boolean_json_even_for_normal_exit(
    tmp_path, monkeypatch, exit_code, payload
):
    monkeypatch.setenv("FAKE_AUTH_JSON", payload)
    monkeypatch.setenv("FAKE_AUTH_EXIT", str(exit_code))
    adapter, _, _ = setup(tmp_path, monkeypatch, "claude")
    with pytest.raises(RuntimeFailure) as failure:
        asyncio.run(adapter.check_environment())
    assert failure.value.event.failure_category is None


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize("exit_code", [1, 2])
def test_version_exit_remains_strict_before_auth(
    tmp_path, monkeypatch, provider, exit_code
):
    monkeypatch.setenv("FAKE_VERSION_EXIT", str(exit_code))
    adapter, _, capture = setup(tmp_path, monkeypatch, provider)
    with pytest.raises(RuntimeFailure) as failure:
        asyncio.run(adapter.check_environment())
    assert failure.value.event.failure_category is None
    records = [json.loads(line) for line in capture.read_text().splitlines()]
    assert [record["argv"] for record in records] == [["--version"]]


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize(
    "mode, category",
    [
        ("model-error", FailureCategory.MODEL_UNAVAILABLE),
        ("malformed", None),
        ("bad-result", None),
        ("early-exit", None),
        ("hang-start", FailureCategory.TIMEOUT),
    ],
)
def test_boundary_errors_have_terminal_and_exit(
    tmp_path, monkeypatch, provider, mode, category
):
    adapter, config, capture = setup(tmp_path, monkeypatch, provider, mode)
    if mode == "hang-start":
        ready = tmp_path / "hang-ready"
        monkeypatch.setenv("FAKE_HANG_READY", str(ready))

        async def collect_after_hang():
            turn = await adapter.submit_turn(
                await adapter.start_session(config), "wait"
            )
            state = adapter._turns[turn]
            try:
                # Startup has its own finite allowance. The short timeout must
                # exercise the actual hang, not an unfinished --version check.
                async with asyncio.timeout(3):
                    while not ready.exists() or state.process is None:
                        assert state.task is not None and not state.task.done()
                        await asyncio.sleep(0.005)
                assert state.process.child.returncode is None
                assert state.deadline is not None and not state.deadline.expired()
                records = [
                    json.loads(line) for line in capture.read_text().splitlines()
                ]
                assert [r["pid"] for r in records if r["kind"] == "hang-ready"] == [
                    state.process.child.pid
                ]
                started = asyncio.get_running_loop().time()
                state.deadline.reschedule(started + 0.4)
                async with asyncio.timeout(3):
                    events = [event async for event in adapter.events(turn)]
                    result = await adapter.result(turn)
                assert asyncio.get_running_loop().time() - started >= 0.4
                assert state.process.child.returncode is not None
                return events, result
            finally:
                if state.task is not None and not state.task.done():
                    state.task.cancel()
                    _, pending = await asyncio.wait({state.task}, timeout=3)
                    assert not pending, "runtime cleanup did not finish"
                    state.task.result()

        events, result = asyncio.run(collect_after_hang())
    else:
        events, result = asyncio.run(collect(adapter, config))
    assert isinstance(result.terminal, Error)
    assert result.terminal.failure_category == category
    assert result.process_exit is not None
    assert sum(isinstance(e, (Completed, Error)) for e in events) == 1
    assert isinstance(events[-2], Error)
    assert isinstance(events[-1], ProcessExited)


def test_slow_version_reaches_model_error_after_old_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_VERSION_DELAY", "0.6")
    adapter, config, capture = setup(tmp_path, monkeypatch, "codex", "model-error")
    assert adapter.timeout == 3
    events, result = asyncio.run(collect(adapter, config))
    assert isinstance(result.terminal, Error)
    assert result.terminal.failure_category == FailureCategory.MODEL_UNAVAILABLE
    assert result.process_exit is not None
    assert sum(isinstance(e, (Completed, Error)) for e in events) == 1
    assert isinstance(events[-2], Error) and isinstance(events[-1], ProcessExited)
    records = [json.loads(line) for line in capture.read_text().splitlines()]
    assert [r for r in records if r["kind"] == "version-delay"] == [
        {"kind": "version-delay", "seconds": 0.6}
    ]
    rows = [
        json.loads(line)
        for line in next((tmp_path / "logs").glob("*.jsonl")).read_text().splitlines()
    ]
    spawns = [i for i, row in enumerate(rows) if row["kind"] == "spawn"]
    assert len(spawns) == 2
    version, app_server = (rows[i] for i in spawns)
    assert version["argv"][1:] == ["--version"]
    assert app_server["argv"][1:] == ["app-server", "--strict-config"]
    preflight = rows[spawns[0] : spawns[1]]
    exits = [row for row in preflight if row["kind"] == "exit"]
    assert len(exits) == 1 and exits[0]["returncode"] == 0
    assert exits[0]["t"] - version["t"] > 0.4
    assert not any(row["kind"] == "signal" for row in preflight)
    terminals = [
        row["message"]
        for row in rows[spawns[1] :]
        if row["kind"] == "recv"
        and row.get("message", {}).get("method") == "turn/completed"
    ]
    assert len(terminals) == 1
    assert terminals[0]["params"]["turn"]["status"] == "failed"
    assert terminals[0]["params"]["turn"]["error"]["codexErrorInfo"] == "modelNotFound"


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_fake_version_delay_is_disabled_by_default(tmp_path, monkeypatch, provider):
    monkeypatch.delenv("FAKE_VERSION_DELAY", raising=False)
    adapter, _, capture = setup(tmp_path, monkeypatch, provider)
    result = subprocess.run(
        [adapter.executable, "--version"],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
        shell=False,
    )
    assert result.returncode == 0 and result.stderr == ""
    assert result.stdout == (
        "codex-cli 0.169.0\n" if provider == "codex" else "fake-claude 1.0\n"
    )
    records = [json.loads(line) for line in capture.read_text().splitlines()]
    assert [record["kind"] for record in records] == ["spawn"]
    assert records[0]["argv"] == ["--version"]


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize("mode", ["wait", "ignore-cancel"])
def test_cancellation_is_confirmed_after_runtime_or_process_stop(
    tmp_path, monkeypatch, provider, mode
):
    adapter, config, capture = setup(tmp_path, monkeypatch, provider, mode)

    async def run():
        session = await adapter.start_session(config)
        turn = await adapter.submit_turn(session, "wait")
        events = []
        async for event in adapter.events(turn):
            events.append(event)
            if isinstance(event, Message) and event.text == "waiting":
                await adapter.cancel(turn)
        return events, await adapter.result(turn)

    events, result = asyncio.run(run())
    assert isinstance(result.terminal, Error)
    assert result.terminal.failure_category == FailureCategory.CANCELLED
    assert result.process_exit is not None
    assert isinstance(events[-1], ProcessExited)
    pid = next(
        record["pid"]
        for line in capture.read_text().splitlines()
        if (record := json.loads(line))["kind"] == "spawn"
        and record["argv"][:1] in (["app-server"], ["-p"])
    )
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    if provider == "codex":
        assert '"method": "turn/interrupt"' in capture.read_text()


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_cancel_before_launch_does_not_spawn(tmp_path, monkeypatch, provider):
    adapter, config, capture = setup(tmp_path, monkeypatch, provider)

    async def run():
        turn = await adapter.submit_turn(await adapter.start_session(config), "unused")
        await adapter.cancel(turn)
        return [e async for e in adapter.events(turn)]

    events = asyncio.run(run())
    assert not capture.exists()
    assert len(events) == 1
    assert events[0].failure_category == FailureCategory.CANCELLED


def test_codex_fast_process_exit_does_not_lose_last_stdout(tmp_path, monkeypatch):
    adapter, config, _ = setup(tmp_path, monkeypatch, "codex", "fast-exit")
    for _ in range(4):
        events, result = asyncio.run(collect(adapter, config))
        assert isinstance(result.terminal, Completed)
        assert isinstance(events[-1], ProcessExited)


def test_repeated_claude_terminal_is_rejected(tmp_path, monkeypatch):
    adapter, config, _ = setup(tmp_path, monkeypatch, "claude", "duplicate")
    events, result = asyncio.run(collect(adapter, config))
    assert isinstance(result.terminal, Error)
    assert not any(isinstance(e, Completed) for e in events)


def test_fresh_sessions_and_readonly_enforcement(tmp_path, monkeypatch):
    adapter, config, capture = setup(tmp_path, monkeypatch, "claude")

    async def run():
        with pytest.raises(ValueError, match="read-only"):
            await adapter.start_session(
                SessionConfig("m", "high", Permission.WORKSPACE_WRITE, config.cwd)
            )
        first = await adapter.start_session(config)
        turn = await adapter.submit_turn(first, "one")
        one = [e async for e in adapter.events(turn)]
        with pytest.raises(ValueError, match="fresh"):
            await adapter.submit_turn(first, "two")
        two, _ = await collect(adapter, config)
        return one, two

    one, two = asyncio.run(run())
    ids = [
        next(e.session_id for e in ev if isinstance(e, SessionStarted))
        for ev in (one, two)
    ]
    assert ids[0] != ids[1]
    assert (
        len(
            [
                json.loads(x)
                for x in capture.read_text().splitlines()
                if json.loads(x)["kind"] == "spawn"
            ]
        )
        == 2
    )


def test_missing_executable_and_environment_failure(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    adapter = RuntimeAdapter(
        "claude", executable=str(tmp_path / "missing"), log_dir=tmp_path / "logs"
    )
    config = SessionConfig("m", "high", Permission.READ_ONLY, work)
    events, result = asyncio.run(collect(adapter, config))
    assert isinstance(result.terminal, Error)
    assert result.process_exit is None
    with pytest.raises(RuntimeFailure) as failure:
        asyncio.run(adapter.check_environment())
    assert failure.value.event.failure_category is None
    assert len(events) == 1


def fixture_messages(provider, name):
    version = "0.169.0" if provider == "codex" else "2.1.325"
    rows = [
        json.loads(line)
        for line in (FIXTURES / f"{provider}-{version}" / f"{provider}-{name}.jsonl")
        .read_text()
        .splitlines()
    ]
    return [
        row["message"]
        for row in rows
        if row["kind"] == "recv"
        and row.get("stream") == "stdout"
        and isinstance(row.get("message"), dict)
    ]


@pytest.mark.parametrize(
    "name,category",
    [
        ("structured", None),
        ("cancel", FailureCategory.CANCELLED),
        ("bad-model", FailureCategory.MODEL_UNAVAILABLE),
        ("auth-expired", FailureCategory.AUTH_EXPIRED),
    ],
)
def test_claude_recordings_decode_reported_evidence(name, category):
    messages = fixture_messages("claude", name)
    events = [
        e for message in messages for e in claude_events(message, structured=True)
    ]
    terminal = next(e for e in events if isinstance(e, (Completed, Error)))
    session = next(e for e in events if isinstance(e, SessionStarted))
    assert session.reported_model is None and session.reported_effort is None
    if category is None:
        assert isinstance(terminal, Completed)
        assert terminal.result == {"verdict": "ok"}
        assert terminal.reported_model == "claude-opus-5-5"
        assert any(isinstance(e, Usage) for e in events)
    else:
        assert isinstance(terminal, Error) and terminal.failure_category == category


@pytest.mark.parametrize(
    "name,category",
    [
        ("structured", None),
        ("cancel", FailureCategory.CANCELLED),
        ("bad-model", FailureCategory.MODEL_UNAVAILABLE),
    ],
)
def test_codex_recordings_decode_terminal_and_ignore_echoes(name, category):
    messages = fixture_messages("codex", name)
    complete = next(m for m in messages if m.get("method") == "turn/completed")
    decoder = CodexDecoder(
        complete["params"]["threadId"],
        complete["params"]["turn"]["id"],
        structured=True,
    )
    events = [e for message in messages for e in decoder.decode(message)]
    terminal = next(e for e in events if isinstance(e, (Completed, Error)))
    if category is None:
        assert isinstance(terminal, Completed)
        assert terminal.result == {"verdict": "ok"}
        assert terminal.reported_model is None
        assert any(isinstance(e, Usage) for e in events)
    else:
        assert isinstance(terminal, Error) and terminal.failure_category == category


def test_wrong_turn_cannot_supply_final_answer():
    decoder = CodexDecoder("t", "correct", structured=True)
    assert (
        decoder.decode(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "t",
                    "turn": {"id": "wrong", "status": "completed", "items": []},
                },
            }
        )
        == ()
    )
    assert decoder.final_text is None


def codex_agent_item(text, phase="omitted"):
    item = {"id": "agent-item", "type": "agentMessage", "text": text}
    if phase != "omitted":
        item["phase"] = phase
    return item


def codex_item_notification(item, *, thread="t", turn="u"):
    return {
        "method": "item/completed",
        "params": {
            "threadId": thread,
            "turnId": turn,
            "item": item,
            "completedAtMs": 1,
        },
    }


def codex_completed_notification(items=(), *, thread="t", turn="u", status="completed"):
    value = {"id": turn, "status": status, "items": list(items)}
    if status == "failed":
        value["error"] = {"message": "reported failure", "codexErrorInfo": "other"}
    return {"method": "turn/completed", "params": {"threadId": thread, "turn": value}}


def decode_codex_sequence(decoder, items, source):
    event_count = len(items) if source == "events" else int(source == "mixed")
    for item in items[:event_count]:
        events = decoder.decode(codex_item_notification(item))
        assert len(events) == 1 and isinstance(events[0], Message)
    return decoder.decode(codex_completed_notification(items[event_count:]))


@pytest.mark.parametrize("source", ["events", "turn-items"])
@pytest.mark.parametrize("phase", ["omitted", None])
def test_codex_unknown_phase_report_completes_from_each_source(source, phase):
    decoder = CodexDecoder("t", "u", structured=True)
    report = {"summary": "compatible report", "findings": []}
    events = decode_codex_sequence(
        decoder, [codex_agent_item(json.dumps(report), phase)], source
    )
    assert len(events) == 1 and isinstance(events[0], Completed)
    assert thaw_json(events[0].result) == report
    assert events[0].reported_model is None


@pytest.mark.parametrize("source", ["events", "mixed", "turn-items"])
@pytest.mark.parametrize("phase", ["omitted", None])
def test_codex_last_unknown_phase_wins_in_protocol_order(source, phase):
    decoder = CodexDecoder("t", "u", structured=True)
    latest = {"summary": "last report", "findings": []}
    items = [
        codex_agent_item('{"summary":"earlier","findings":[]}'),
        codex_agent_item("earlier malformed JSON", None),
        codex_agent_item(json.dumps(latest), phase),
        codex_agent_item("progress after answer", "commentary"),
        codex_agent_item("partial after answer", "partial_answer"),
    ]
    events = decode_codex_sequence(decoder, items, source)
    assert isinstance(events[0], Completed) and thaw_json(events[0].result) == latest


@pytest.mark.parametrize("source", ["events", "mixed", "turn-items"])
@pytest.mark.parametrize("phase", ["omitted", None])
@pytest.mark.parametrize("final_first", [True, False])
def test_codex_explicit_final_precedes_unknown_phase_candidates(
    source, phase, final_first
):
    decoder = CodexDecoder("t", "u", structured=True)
    report = {"summary": "explicit final", "findings": []}
    items = [
        codex_agent_item(json.dumps(report), "final_answer"),
        codex_agent_item("unknown phase is not a JSON fallback", phase),
    ]
    if not final_first:
        items.reverse()
    events = decode_codex_sequence(decoder, items, source)
    assert isinstance(events[0], Completed) and thaw_json(events[0].result) == report


@pytest.mark.parametrize("source", ["events", "mixed", "turn-items"])
@pytest.mark.parametrize("phase", ["omitted", None])
@pytest.mark.parametrize("final_first", [True, False])
def test_codex_empty_unknown_does_not_override_explicit_final(
    source, phase, final_first
):
    decoder = CodexDecoder("t", "u", structured=True)
    report = {"summary": "explicit final with empty neighbor", "findings": []}
    items = [
        codex_agent_item(json.dumps(report), "final_answer"),
        codex_agent_item("", phase),
    ]
    if not final_first:
        items.reverse()
    events = decode_codex_sequence(decoder, items, source)
    assert isinstance(events[0], Completed) and thaw_json(events[0].result) == report


@pytest.mark.parametrize("source", ["events", "mixed", "turn-items"])
@pytest.mark.parametrize("phase", ["omitted", None])
def test_codex_empty_last_unknown_does_not_fall_back_to_earlier_report(source, phase):
    decoder = CodexDecoder("t", "u", structured=True)
    items = [
        codex_agent_item('{"summary":"earlier valid","findings":[]}'),
        codex_agent_item("", phase),
    ]
    with pytest.raises(ValueError, match="completed answer must be a nonempty string"):
        decode_codex_sequence(decoder, items, source)


@pytest.mark.parametrize("source", ["events", "mixed", "turn-items"])
@pytest.mark.parametrize("final_first", [True, False])
def test_codex_empty_explicit_final_does_not_fall_back_to_unknown(source, final_first):
    decoder = CodexDecoder("t", "u", structured=True)
    items = [
        codex_agent_item("", "final_answer"),
        codex_agent_item('{"summary":"valid unknown","findings":[]}', None),
    ]
    if not final_first:
        items.reverse()
    with pytest.raises(ValueError, match="completed answer must be a nonempty string"):
        decode_codex_sequence(decoder, items, source)


@pytest.mark.parametrize("source", ["events", "mixed", "turn-items"])
@pytest.mark.parametrize("text", [None, 0, {}])
def test_codex_agent_text_requires_string_even_with_explicit_final(source, text):
    decoder = CodexDecoder("t", "u", structured=True)
    items = [
        codex_agent_item('{"summary":"explicit final","findings":[]}', "final_answer"),
        codex_agent_item(text, None),
    ]
    with pytest.raises(TypeError, match="agentMessage.text must be a string"):
        decode_codex_sequence(decoder, items, source)


@pytest.mark.parametrize("source", ["events", "mixed", "turn-items"])
@pytest.mark.parametrize("phase", ["omitted", None])
def test_codex_malformed_last_unknown_never_uses_earlier_valid_report(source, phase):
    decoder = CodexDecoder("t", "u", structured=True)
    items = [
        codex_agent_item('{"summary":"earlier valid","findings":[]}'),
        codex_agent_item("malformed final report", phase),
    ]
    with pytest.raises(json.JSONDecodeError):
        decode_codex_sequence(decoder, items, source)


@pytest.mark.parametrize("source", ["events", "mixed", "turn-items"])
def test_codex_malformed_explicit_final_never_uses_unknown_candidate(source):
    decoder = CodexDecoder("t", "u", structured=True)
    items = [
        codex_agent_item("malformed explicit final", "final_answer"),
        codex_agent_item('{"summary":"valid fallback","findings":[]}', None),
    ]
    with pytest.raises(json.JSONDecodeError):
        decode_codex_sequence(decoder, items, source)


@pytest.mark.parametrize("source", ["events", "turn-items"])
@pytest.mark.parametrize("phase", ["commentary", "partial_answer", "future-phase"])
def test_codex_nonfinal_phases_cannot_supply_report(source, phase):
    decoder = CodexDecoder("t", "u", structured=True)
    items = [
        codex_agent_item('{"summary":"valid JSON but not final","findings":[]}', phase)
    ]
    with pytest.raises(ValueError, match="no final answer"):
        decode_codex_sequence(decoder, items, source)


@pytest.mark.parametrize("source", ["events", "turn-items"])
@pytest.mark.parametrize("phase", ["omitted", None, "final_answer"])
@pytest.mark.parametrize(
    "foreign", [{"thread": "other-thread"}, {"turn": "other-turn"}]
)
def test_codex_unrelated_ids_cannot_supply_or_replace_report(source, phase, foreign):
    decoder = CodexDecoder("t", "u", structured=True)
    unrelated = codex_agent_item("malformed unrelated output", phase)
    notification = (
        codex_item_notification(unrelated, **foreign)
        if source == "events"
        else codex_completed_notification([unrelated], **foreign)
    )
    assert decoder.decode(notification) == ()
    with pytest.raises(ValueError, match="no final answer"):
        decoder.decode(codex_completed_notification())
    report = {"summary": "correct turn", "findings": []}
    decoder.decode(codex_item_notification(codex_agent_item(json.dumps(report), None)))
    assert decoder.decode(notification) == ()
    terminal = decoder.decode(codex_completed_notification())[0]
    assert isinstance(terminal, Completed) and thaw_json(terminal.result) == report


@pytest.mark.parametrize("phase", ["omitted", None])
@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_codex_unsuccessful_turn_never_promotes_unknown_report(phase, status):
    decoder = CodexDecoder("t", "u", structured=True)
    decoder.decode(
        codex_item_notification(
            codex_agent_item('{"summary":"not success","findings":[]}', phase)
        )
    )
    terminal = decoder.decode(
        codex_completed_notification(
            [codex_agent_item("malformed terminal contents", phase)], status=status
        )
    )[0]
    assert isinstance(terminal, Error)
    assert terminal.failure_category == (
        FailureCategory.CANCELLED if status == "interrupted" else None
    )


def test_claude_preserves_reported_reset_time(tmp_path, monkeypatch):
    adapter, config, _ = setup(tmp_path, monkeypatch, "claude", "rate-error")
    _, result = asyncio.run(collect(adapter, config))
    assert isinstance(result.terminal, Error)
    assert result.terminal.failure_category == FailureCategory.RATE_LIMITED
    assert result.terminal.retry_after == "12345"


def test_reported_terminal_survives_hung_process_shutdown(tmp_path, monkeypatch):
    adapter, config, _ = setup(tmp_path, monkeypatch, "claude", "hang-after-result")
    _, result = asyncio.run(collect(adapter, config))
    assert isinstance(result.terminal, Completed)
    assert result.process_exit.exit_code != 0


def test_multiple_claude_models_are_not_silently_collapsed():
    events = claude_events(
        {
            "type": "result",
            "is_error": False,
            "terminal_reason": "completed",
            "structured_output": {},
            "modelUsage": {"one": {}, "two": {}},
        },
        structured=True,
    )
    terminal = next(e for e in events if isinstance(e, Completed))
    assert terminal.reported_model is None


@pytest.mark.parametrize("mode", ["duplicate", "contradictory"])
def test_codex_final_drain_rejects_multiple_terminals(tmp_path, monkeypatch, mode):
    adapter, config, _ = setup(tmp_path, monkeypatch, "codex", mode)
    events, result = asyncio.run(collect(adapter, config))
    assert isinstance(result.terminal, Error)
    assert result.terminal.failure_category is None
    assert "multiple terminal" in result.terminal.detail
    assert not any(isinstance(e, Completed) for e in events)
    log = next((tmp_path / "logs").glob("*.jsonl")).read_text()
    assert log.count('"method": "turn/completed"') == 2


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize(
    "mode",
    ["grandchild-hang", "grandchild-exit", "grandchild-success", "grandchild-cancel"],
)
def test_runtime_cleans_owned_process_group(tmp_path, monkeypatch, provider, mode):
    adapter, config, capture = setup(tmp_path, monkeypatch, provider, mode, timeout=0.4)

    async def run():
        if mode != "grandchild-cancel":
            return await collect(adapter, config)
        turn = await adapter.submit_turn(await adapter.start_session(config), "wait")
        async with asyncio.timeout(2):
            while (
                not capture.exists()
                or '"kind": "grandchild"' not in capture.read_text()
            ):
                await asyncio.sleep(0.005)
            await adapter.cancel(turn)
        return [event async for event in adapter.events(turn)], await adapter.result(
            turn
        )

    _, result = asyncio.run(run())
    if mode == "grandchild-success":
        assert isinstance(result.terminal, Completed)
    else:
        assert isinstance(result.terminal, Error)
    if mode == "grandchild-cancel":
        assert result.terminal.failure_category == FailureCategory.CANCELLED
    rows = [json.loads(line) for line in capture.read_text().splitlines()]
    pids = [
        row["pid"]
        for row in rows
        if row["kind"] == "grandchild"
        or row["kind"] == "spawn"
        and row["argv"][:1] in (["app-server"], ["-p"])
    ]
    assert len(pids) == 2
    # Orphan zombies may await the system reaper briefly; no running descendant
    # can survive the adapter's bounded process-group shutdown.
    for pid in pids:
        for _ in range(30):
            checked = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)],
                capture_output=True,
                text=True,
                check=False,
            )
            if checked.returncode != 0 or checked.stdout.strip().startswith("Z"):
                break
            time.sleep(0.01)
        else:
            pytest.fail(f"runtime descendant {pid} is still running")


def test_terminal_disables_model_deadline_for_final_drain(tmp_path, monkeypatch):
    adapter, config, _ = setup(
        tmp_path, monkeypatch, "codex", "late-contradictory", timeout=0.5
    )
    adapter.grace = 1
    monkeypatch.setenv("FAKE_TERMINAL_DELAY", "0.1")
    monkeypatch.setenv("FAKE_SECOND_DELAY", "0.6")
    _, result = asyncio.run(collect(adapter, config))
    assert isinstance(result.terminal, Error)
    assert "multiple terminal" in result.terminal.detail


def test_large_reported_integer_cost_remains_finite_evidence():
    amount = 10**1000
    events = claude_events(
        {
            "type": "result",
            "is_error": False,
            "terminal_reason": "completed",
            "structured_output": {},
            "total_cost_usd": amount,
        },
        structured=True,
    )
    usage = next(e for e in events if isinstance(e, Usage))
    assert usage.counts["client_estimated_cost_usd"] == amount


@pytest.mark.parametrize(
    "observed_terminal,request_cancel", [(False, True), (False, False), (True, False)]
)
def test_cleanup_failure_never_claims_unobserved_process_stop(
    tmp_path, monkeypatch, observed_terminal, request_cancel
):
    mode = "hang-after-result" if observed_terminal else "ignore-cancel"
    adapter, config, _ = setup(tmp_path, monkeypatch, "claude", mode, timeout=0.4)
    owned = []
    original_terminate = Process.terminate

    async def fail_cleanup(process, grace):
        if process not in owned:
            owned.append(process)
        raise OSError("injected process cleanup failure")

    monkeypatch.setattr(Process, "terminate", fail_cleanup)

    async def run():
        try:
            turn = await adapter.submit_turn(
                await adapter.start_session(config), "wait"
            )
            events = []
            async for event in adapter.events(turn):
                events.append(event)
                if (
                    request_cancel
                    and isinstance(event, Message)
                    and event.text == "waiting"
                ):
                    await adapter.cancel(turn)
            return events, await adapter.result(turn)
        finally:
            # The regression deliberately leaves an actual fake subprocess alive.
            # Restore cleanup and provide a live recorder, then safely reap it.
            monkeypatch.setattr(Process, "terminate", original_terminate)
            for index, process in enumerate(owned):
                with Recorder(
                    tmp_path / f"test-cleanup-{index}.jsonl", Redactor()
                ) as recorder:
                    process._recorder = recorder
                    await original_terminate(process, 0.05)

    events, result = asyncio.run(run())
    assert result.process_exit is None
    assert not any(isinstance(event, ProcessExited) for event in events)
    if observed_terminal:
        assert isinstance(result.terminal, Completed)
        assert (
            "cleanup_incomplete"
            in next((tmp_path / "logs").glob("*.jsonl")).read_text()
        )
    else:
        assert isinstance(result.terminal, Error)
        assert result.terminal.failure_category is None
        assert result.terminal.raw_type == "adapter.cleanup_incomplete"


def test_cancel_acceptance_callback_precedes_send_and_cleanup_failures(
    tmp_path, monkeypatch
):
    adapter, config, _ = setup(tmp_path, monkeypatch, "claude", "ignore-cancel")
    original_send = adapter._send_cancel
    original_terminate = Process.terminate
    order = []
    send_error = OSError("injected cancellation send failure")
    cleanup_error = OSError("injected cancellation cleanup failure")

    async def fail_send(state):
        assert state.cancelled
        order.append("send")
        raise send_error

    async def fail_cleanup(process, grace):
        assert process.child.returncode is None
        order.append("cleanup")
        raise cleanup_error

    async def run():
        turn = await adapter.submit_turn(await adapter.start_session(config), "wait")
        state = adapter._turns[turn]
        stream = adapter.events(turn)
        try:
            async with asyncio.timeout(2):
                async for event in stream:
                    if isinstance(event, Message) and event.text == "waiting":
                        break
            assert state.process is not None and state.process.child.returncode is None
            monkeypatch.setattr(adapter, "_send_cancel", fail_send)
            monkeypatch.setattr(Process, "terminate", fail_cleanup)

            def accepted():
                assert state.cancelled
                order.append("accepted")

            with pytest.raises(OSError) as failure:
                await adapter.cancel(turn, on_accepted=accepted)
            assert failure.value is cleanup_error
            assert failure.value.__context__ is send_error
            assert order == ["accepted", "send", "cleanup"]
            result = await adapter.result(turn)
            assert result.terminal is None and result.process_exit is None
            assert state.observed_terminal is None
            assert state.process.child.returncode is None
            assert state.queue.empty()
        finally:
            # The injected failures intentionally leave the fake process alive.
            # Restore the real shutdown path before consuming its actual exit.
            monkeypatch.setattr(adapter, "_send_cancel", original_send)
            monkeypatch.setattr(Process, "terminate", original_terminate)
            await original_send(state)
            async for _ in stream:
                pass
        stopped = await adapter.result(turn)
        assert stopped.process_exit is not None

    asyncio.run(run())


def test_cancel_send_failure_recovers_with_real_process_cleanup(tmp_path, monkeypatch):
    adapter, config, capture = setup(tmp_path, monkeypatch, "claude", "wait")
    order = []

    async def fail_send(state):
        assert state.cancelled
        order.append("send")
        raise OSError("injected cancellation send failure")

    async def run():
        turn = await adapter.submit_turn(await adapter.start_session(config), "wait")
        events = []
        async for event in adapter.events(turn):
            events.append(event)
            if isinstance(event, Message) and event.text == "waiting":
                monkeypatch.setattr(adapter, "_send_cancel", fail_send)
                assert (
                    await adapter.cancel(
                        turn, on_accepted=lambda: order.append("accepted")
                    )
                    is True
                )
        return events, await adapter.result(turn)

    events, result = asyncio.run(run())
    assert order == ["accepted", "send"]
    assert isinstance(result.terminal, Error)
    assert result.terminal.failure_category == FailureCategory.CANCELLED
    assert sum(isinstance(event, Error) for event in events) == 1
    assert not any(isinstance(event, Completed) for event in events)
    assert result.process_exit is not None and result.process_exit.exit_code != 0
    assert events[-1] == result.process_exit
    assert isinstance(events[-1], ProcessExited)
    pid = json.loads(capture.read_text().splitlines()[0])["pid"]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_cancel_refuses_observed_os_exit_before_exit_event(
    tmp_path, monkeypatch, provider
):
    adapter, config, _ = setup(tmp_path, monkeypatch, provider, "early-exit")
    original_terminate = Process.terminate

    async def run():
        cleaning = asyncio.Event()
        release = asyncio.Event()

        async def pause_cleanup(process, grace):
            if await process.wait_exited() == 17:
                cleaning.set()
                await release.wait()
            await original_terminate(process, grace)

        monkeypatch.setattr(Process, "terminate", pause_cleanup)
        turn = await adapter.submit_turn(await adapter.start_session(config), "unused")
        state = adapter._turns[turn]
        accepted = []
        try:
            async with asyncio.timeout(2):
                await cleaning.wait()
            assert state.process.child.returncode == 17
            assert state.exit_event is None and state.terminal is None
            assert state.observed_terminal is None
            assert (
                await adapter.cancel(turn, on_accepted=lambda: accepted.append(True))
                is False
            )
            assert accepted == []
            assert state.cancelled is False and state.cancel_sent is False
        finally:
            release.set()
            events = [event async for event in adapter.events(turn)]
        result = await adapter.result(turn)
        assert isinstance(result.terminal, Error)
        assert result.terminal.failure_category is None
        assert result.process_exit.exit_code == 17
        assert isinstance(events[-1], ProcessExited)

    asyncio.run(run())


def assert_codex_resources_removed(adapter):
    states = list(adapter._turns.values())
    assert len(states) == 1
    isolation = states[0].isolation
    assert isolation is not None
    assert not isolation.root.exists()
    assert isolation._processes
    assert all(process.child.returncode is not None for process in isolation._processes)
    assert all(isolation._processes.values())


def runtime_requests(capture):
    return [
        record["message"]
        for line in capture.read_text().splitlines()
        if (record := json.loads(line))["kind"] == "request"
    ]


@pytest.mark.parametrize(
    "mode",
    [
        "config-missing",
        "config-malformed",
        "config-missing-mcp-servers",
        "config-missing-instructions",
        "config-ignored-profile",
        "config-profile-extends",
        "config-extra-filesystem",
        "config-extra-profile",
        "config-network-enabled",
        "config-bool-doc-limit",
        "config-null-permission-key",
        "config-null-top-level",
        "config-system-layer",
        "config-mdm-layer",
        "config-mcp-servers",
        "config-instructions",
        "config-error",
        "config-bool-id",
        "config-float-id",
        "config-wrong-id",
        "requirements-instructions",
    ],
)
def test_codex_rejects_unsafe_effective_configuration_before_model_turn(
    tmp_path, monkeypatch, mode
):
    adapter, config, capture = setup(tmp_path, monkeypatch, "codex", mode)
    events, result = asyncio.run(collect(adapter, config))
    assert isinstance(result.terminal, Error)
    assert result.terminal.failure_category is None
    assert result.terminal.raw_type == (
        "config/read" if mode == "config-error" else "adapter.isolation_failed"
    )
    assert sum(isinstance(event, Error) for event in events) == 1
    assert not any(isinstance(event, Completed) for event in events)
    assert result.process_exit is not None
    methods = [request.get("method") for request in runtime_requests(capture)]
    assert "config/read" in methods
    assert "thread/start" not in methods and "turn/start" not in methods
    if mode == "requirements-instructions":
        assert "configRequirements/read" in methods
    assert_codex_resources_removed(adapter)
    assert all(
        "synthetic-config-poison" not in path.read_text()
        for path in (tmp_path / "logs").glob("*.jsonl")
    )


@pytest.mark.parametrize(
    "mode, turn_count",
    [("thread-instructions", 0), ("mcp-before-turn", 0), ("mcp-after-terminal", 1)],
)
def test_codex_rejects_instruction_or_mcp_contamination_including_final_drain(
    tmp_path, monkeypatch, mode, turn_count
):
    adapter, config, capture = setup(tmp_path, monkeypatch, "codex", mode)
    events, result = asyncio.run(collect(adapter, config))
    assert isinstance(result.terminal, Error)
    assert result.terminal.failure_category is None
    assert result.terminal.raw_type == "adapter.isolation_failed"
    assert not any(isinstance(event, Completed) for event in events)
    assert sum(isinstance(event, Error) for event in events) == 1
    assert result.process_exit is not None
    methods = [request.get("method") for request in runtime_requests(capture)]
    assert methods.count("thread/start") == 1
    assert methods.count("turn/start") == turn_count
    if mode == "mcp-after-terminal":
        records = [
            json.loads(line)
            for path in (tmp_path / "logs").glob("*.jsonl")
            for line in path.read_text().splitlines()
        ]
        assert any(
            record.get("message", {}).get("method") == "turn/completed"
            and record["message"]["params"]["turn"]["status"] == "completed"
            for record in records
        )
    assert_codex_resources_removed(adapter)


def test_codex_null_account_is_auth_expired_before_thread_and_cleans_isolation(
    tmp_path, monkeypatch
):
    adapter, config, capture = setup(
        tmp_path, monkeypatch, "codex", "account-logged-out"
    )
    events, result = asyncio.run(collect(adapter, config))
    assert isinstance(result.terminal, Error)
    assert result.terminal.failure_category == FailureCategory.AUTH_EXPIRED
    assert result.terminal.raw_type == "account/read"
    assert not any(isinstance(event, (SessionStarted, Completed)) for event in events)
    methods = [request.get("method") for request in runtime_requests(capture)]
    assert "account/read" in methods
    assert "thread/start" not in methods and "turn/start" not in methods
    assert result.process_exit is not None
    assert_codex_resources_removed(adapter)


def test_codex_version_mismatch_stops_before_app_server_and_cleans_isolation(
    tmp_path, monkeypatch
):
    adapter, config, capture = setup(tmp_path, monkeypatch, "codex", "wrong-version")
    events, result = asyncio.run(collect(adapter, config))
    assert isinstance(result.terminal, Error)
    assert result.terminal.raw_type == "adapter.isolation_failed"
    assert result.terminal.failure_category is None
    assert not any(isinstance(event, Completed) for event in events)
    rows = [json.loads(line) for line in capture.read_text().splitlines()]
    assert [row["argv"] for row in rows if row["kind"] == "spawn"] == [["--version"]]
    assert runtime_requests(capture) == []
    assert_codex_resources_removed(adapter)


@pytest.mark.parametrize(
    "mode",
    [
        "requirements-empty",
        "requirements-missing",
        "requirements-unknown-null",
        "requirements-login-methods",
    ],
)
def test_codex_nonnull_or_missing_requirements_do_not_attest_absence(
    tmp_path, monkeypatch, mode
):
    adapter, config, capture = setup(tmp_path, monkeypatch, "codex", mode)
    events, result = asyncio.run(collect(adapter, config))
    assert isinstance(result.terminal, Error)
    assert result.terminal.raw_type == "adapter.isolation_failed"
    assert result.terminal.failure_category is None
    assert not any(isinstance(event, Completed) for event in events)
    assert isinstance(events[-1], ProcessExited)
    methods = [request.get("method") for request in runtime_requests(capture)]
    assert methods.index("config/read") < methods.index("configRequirements/read")
    assert "thread/start" not in methods and "turn/start" not in methods
    assert_codex_resources_removed(adapter)


@pytest.mark.parametrize("unknown_stop", [False, True])
def test_codex_cancel_during_version_preflight_preserves_stop_evidence(
    tmp_path, monkeypatch, unknown_stop
):
    adapter, config, capture = setup(
        tmp_path, monkeypatch, "codex", "version-hang", timeout=10
    )
    original_start = Process.start

    async def run():
        spawned = asyncio.Event()
        handles = []

        async def capture_start(cls, name, argv, cwd, recorder, **kwargs):
            process = await original_start(name, argv, cwd, recorder, **kwargs)
            handles.append(process)
            if argv[1:] == ["--version"]:
                spawned.set()
            return process

        monkeypatch.setattr(Process, "start", classmethod(capture_start))
        turn = await adapter.submit_turn(await adapter.start_session(config), "unused")
        state = adapter._turns[turn]
        version = None
        original_terminate = None
        try:
            async with asyncio.timeout(3):
                await spawned.wait()
                version = handles[0]
                # The fake flushes the real version line, then remains alive.
                await version.first_stdout.wait()
                assert state.process is None and state.isolation is not None
                assert version.child.returncode is None
                assert state.observed_terminal is None
                original_terminate = version.terminate
                if unknown_stop:

                    async def fail_stop(grace):
                        raise OSError("injected version stop remains unknown")

                    monkeypatch.setattr(version, "terminate", fail_stop)
                accepted = []
                assert await adapter.cancel(
                    turn, on_accepted=lambda: accepted.append(True)
                )
                events = [event async for event in adapter.events(turn)]
                result = await adapter.result(turn)
            assert accepted == [True]
            assert isinstance(result.terminal, Error)
            assert not any(
                isinstance(event, (SessionStarted, Completed, ProcessExited))
                for event in events
            )
            assert result.process_exit is None
            assert state.process is None
            assert len(handles) == 1
            if unknown_stop:
                assert result.terminal.raw_type == "adapter.cleanup_incomplete"
                assert result.terminal.failure_category is None
                assert version.child.returncode is None
                assert state.isolation.root.is_dir()
                assert state.isolation.root.stat().st_mode & 0o777 == 0o700
                auth = state.isolation.codex_home / "auth.json"
                assert auth.is_file() and auth.stat().st_mode & 0o777 == 0o600
            else:
                assert result.terminal.failure_category == FailureCategory.CANCELLED
                assert version.child.returncode is not None
                assert_codex_resources_removed(adapter)
        finally:
            if version is not None and original_terminate is not None:
                monkeypatch.setattr(version, "terminate", original_terminate)
            if state.task is not None and not state.task.done():
                state.task.cancel()
                await asyncio.gather(state.task, return_exceptions=True)
            if state.isolation is not None and state.isolation.root.exists():
                # The negative case deliberately retained a live owned process
                # after its runtime recorder closed. Reap it with a live log.
                with Recorder(
                    tmp_path / "version-test-cleanup.jsonl", Redactor()
                ) as recorder:
                    state.isolation.recorder = recorder
                    for process in state.isolation._processes:
                        process._recorder = recorder
                        await state.isolation.terminate(process, 0.05)
                    assert state.isolation.cleanup()
            assert all(process.child.returncode is not None for process in handles)

    asyncio.run(run())
    rows = [json.loads(line) for line in capture.read_text().splitlines()]
    assert [row["argv"] for row in rows if row["kind"] == "spawn"] == [["--version"]]
    assert runtime_requests(capture) == []


@pytest.mark.parametrize("trigger", ["success", "cancel", "preflight-failure"])
@pytest.mark.parametrize("cleanup_outcome", ["false", "rmtree-error"])
def test_codex_environment_resource_cleanup_failure_keeps_unknown_marker(
    tmp_path, monkeypatch, trigger, cleanup_outcome
):
    adapter, _, capture = setup(
        tmp_path,
        monkeypatch,
        "codex",
        {
            "success": "success",
            "cancel": "version-hang",
            "preflight-failure": "config-error",
        }[trigger],
        timeout=10,
    )
    original_create = CodexIsolation.create
    original_cleanup = CodexIsolation.cleanup
    original_start = Process.start
    original_rmtree = shutil.rmtree
    owners = []
    handles = []

    def capture_create(cls, *args, **kwargs):
        owner = original_create(*args, **kwargs)
        owners.append(owner)
        return owner

    monkeypatch.setattr(CodexIsolation, "create", classmethod(capture_create))
    if cleanup_outcome == "false":

        def fail_cleanup(owner):
            owner._incomplete("injected_cleanup_false")
            return False

        monkeypatch.setattr(CodexIsolation, "cleanup", fail_cleanup)
    else:
        # Exercise the real cleanup() OSError branch, including its retained
        # resource locator; other temporary-directory cleanup must still work.
        def fail_owned_rmtree(path, *args, **kwargs):
            if any(Path(path) == owner.root for owner in owners):
                raise OSError("injected isolated auth directory removal failure")
            return original_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(shutil, "rmtree", fail_owned_rmtree)

    async def run():
        spawned = asyncio.Event()

        async def capture_start(cls, name, argv, cwd, recorder, **kwargs):
            process = await original_start(name, argv, cwd, recorder, **kwargs)
            handles.append(process)
            spawned.set()
            return process

        monkeypatch.setattr(Process, "start", classmethod(capture_start))
        task = asyncio.create_task(adapter.check_environment())
        try:
            async with asyncio.timeout(5):
                if trigger == "cancel":
                    await spawned.wait()
                    await handles[0].first_stdout.wait()
                    assert handles[0].child.returncode is None
                    assert task.cancel()
                with pytest.raises(RuntimeFailure) as failure:
                    await task
            assert failure.value.event.raw_type == "adapter.cleanup_incomplete"
            assert failure.value.event.failure_category is None
            assert len(owners) == 1
            owner = owners[0]
            assert owner.root.is_dir()
            assert owner.root.stat().st_mode & 0o777 == 0o700
            auth = owner.codex_home / "auth.json"
            assert auth.is_file() and auth.stat().st_mode & 0o777 == 0o600
            assert owner._processes and all(owner._processes.values())
            assert all(p.child.returncode is not None for p in handles)
            assert not adapter._sessions and not adapter._turns
            log = next((tmp_path / "logs").glob("*.jsonl")).read_text()
            assert '"kind": "cleanup_incomplete"' in log
            assert '"kind": "isolation_removed"' not in log
            assert (
                "injected_cleanup_false"
                if cleanup_outcome == "false"
                else "resource_removal_failed"
            ) in log
            if trigger == "preflight-failure":
                assert '"has_error": true' in log
                assert "synthetic-config-poison" not in log
        finally:
            monkeypatch.setattr(CodexIsolation, "cleanup", original_cleanup)
            if cleanup_outcome == "rmtree-error":
                monkeypatch.setattr(shutil, "rmtree", original_rmtree)
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            for index, owner in enumerate(owners):
                with Recorder(
                    tmp_path / f"environment-resource-cleanup-{index}.jsonl", Redactor()
                ) as recorder:
                    owner.recorder = recorder
                    for process in owner._processes:
                        process._recorder = recorder
                        await owner.terminate(process, 0.05)
                    assert owner.cleanup()
                assert not owner.root.exists()
            assert all(p.child.returncode is not None for p in handles)

    asyncio.run(run())
    methods = [request.get("method") for request in runtime_requests(capture)]
    assert "thread/start" not in methods and "turn/start" not in methods


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("stop_error", [None, OSError, RuntimeError])
def test_claude_environment_collect_cleanup_preserves_failure_evidence(
    tmp_path, monkeypatch, cancelled, stop_error
):
    adapter, _, capture = setup(
        tmp_path, monkeypatch, "claude", "version-hang", timeout=10
    )
    original_start = Process.start
    original_read = Process.read
    original_terminate = Process.terminate
    handles = []

    async def fail_read(process):
        await original_read(process)
        raise ValueError("injected environment collection failure")

    async def fail_stop(process, grace):
        raise stop_error("injected environment process stop failure")

    if not cancelled:
        monkeypatch.setattr(Process, "read", fail_read)
    if stop_error is not None:
        monkeypatch.setattr(Process, "terminate", fail_stop)

    async def run():
        spawned = asyncio.Event()

        async def capture_start(cls, name, argv, cwd, recorder, **kwargs):
            process = await original_start(name, argv, cwd, recorder, **kwargs)
            handles.append(process)
            spawned.set()
            return process

        monkeypatch.setattr(Process, "start", classmethod(capture_start))
        task = asyncio.create_task(adapter.check_environment())
        try:
            async with asyncio.timeout(5):
                await spawned.wait()
                if cancelled:
                    await handles[0].first_stdout.wait()
                    assert handles[0].child.returncode is None
                    assert task.cancel()
                if cancelled and stop_error is None:
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    with pytest.raises(RuntimeFailure) as failure:
                        await task
                    event = failure.value.event
                    assert event.failure_category is None
                    if stop_error is not None:
                        assert event.raw_type == "adapter.cleanup_incomplete"
                        assert (
                            "injected environment process stop failure" in event.detail
                        )
                    else:
                        assert event.raw_type is None
                        assert "injected environment collection failure" in event.detail
            assert len(handles) == 1
            assert (handles[0].child.returncode is None) == (stop_error is not None)
            assert not adapter._sessions and not adapter._turns
        finally:
            monkeypatch.setattr(Process, "read", original_read)
            monkeypatch.setattr(Process, "terminate", original_terminate)
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            for index, process in enumerate(handles):
                with Recorder(
                    tmp_path / f"environment-process-cleanup-{index}.jsonl", Redactor()
                ) as recorder:
                    process._recorder = recorder
                    await original_terminate(process, 0.05)
            assert all(p.child.returncode is not None for p in handles)

    asyncio.run(run())
    rows = [json.loads(line) for line in capture.read_text().splitlines()]
    assert [row["argv"] for row in rows if row["kind"] == "spawn"] == [["--version"]]


@pytest.mark.parametrize("stop_error", [None, OSError, RuntimeError, LookupError])
def test_environment_creation_cancel_waits_for_real_process_start_cleanup(
    tmp_path, monkeypatch, stop_error
):
    adapter = RuntimeAdapter(
        "claude",
        executable="synthetic-claude-unused",
        log_dir=tmp_path / "logs",
        timeout=10,
        grace=0.05,
    )

    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()
        handles = []
        launches = []
        returned_spawn = []
        original_spawn = adapter._spawn
        injected = (
            stop_error("injected creation-time process stop failure")
            if stop_error is not None
            else None
        )

        class FakeChild:
            def __init__(self):
                self.stdout = asyncio.StreamReader()
                self.stderr = asyncio.StreamReader()
                self.returncode = None

            def finish(self):
                self.returncode = 0
                self.stdout.feed_eof()
                self.stderr.feed_eof()

        child = FakeChild()

        async def create_child(*argv, **kwargs):
            launches.append(argv)
            entered.set()
            await release.wait()
            return child

        async def observe_spawn(*args, **kwargs):
            process = await original_spawn(*args, **kwargs)
            returned_spawn.append(process)
            return process

        async def stop_child(process, grace):
            # Only Process.start.finish_creation can reach cleanup before the
            # _spawn await returns a handle to _collect's own finally block.
            assert not returned_spawn
            handles.append(process)
            if injected is not None:
                raise injected
            child.finish()
            await process.wait()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", create_child)
        monkeypatch.setattr(adapter, "_spawn", observe_spawn)
        monkeypatch.setattr(Process, "terminate", stop_child)
        task = asyncio.create_task(adapter.check_environment())
        try:
            async with asyncio.timeout(5):
                await entered.wait()
                assert not release.is_set() and child.returncode is None
                assert task.cancel()
                release.set()
                if injected is None:
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    with pytest.raises(RuntimeFailure) as failure:
                        await task
                    assert failure.value.event.raw_type == "adapter.cleanup_incomplete"
                    assert failure.value.event.failure_category is None
                    assert (
                        "creation-time process stop failure"
                        in failure.value.event.detail
                    )
                    assert failure.value.__cause__ is injected
            assert len(handles) == 1 and not returned_spawn
            assert launches == [("synthetic-claude-unused", "--version")]
            assert (child.returncode is None) == (injected is not None)
            assert not adapter._sessions and not adapter._turns
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            with Recorder(
                tmp_path / "creation-test-cleanup.jsonl", Redactor()
            ) as recorder:
                for process in handles:
                    process._recorder = recorder
                if child.returncode is None:
                    child.finish()
                for process in handles:
                    await process.wait()
                    assert all(reader.done() for reader in process._readers)
                    assert process._exit.done() and process._watcher.done()

    asyncio.run(run())


def test_environment_known_spawn_failure_is_not_cleanup_unknown(tmp_path, monkeypatch):
    adapter = RuntimeAdapter(
        "claude",
        executable="synthetic-missing-claude",
        log_dir=tmp_path / "logs",
    )
    creation_error = FileNotFoundError("injected executable was not created")
    launches = []

    async def fail_creation(*argv, **kwargs):
        launches.append(argv)
        raise creation_error

    async def unexpected_cleanup(process, grace):
        pytest.fail("SpawnFailed must not invent an owned child")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_creation)
    monkeypatch.setattr(Process, "terminate", unexpected_cleanup)
    with pytest.raises(RuntimeFailure) as failure:
        asyncio.run(adapter.check_environment())
    assert failure.value.event.raw_type is None
    assert failure.value.event.failure_category is None
    assert isinstance(failure.value.__cause__, SpawnFailed)
    assert failure.value.__cause__.__cause__ is creation_error
    assert launches == [("synthetic-missing-claude", "--version")]
    assert not adapter._sessions and not adapter._turns


def track_strict_json_processes(monkeypatch):
    """Track real fake-CLI handles without replacing their lifecycle behavior."""
    processes = []
    original = Process.start.__func__

    async def tracked(cls, *args, **kwargs):
        process = await original(cls, *args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(Process, "start", classmethod(tracked))
    return processes


def strict_json_raw_wires(capture, stream="stdout"):
    return [
        bytes.fromhex(record["wire_hex"])
        for line in capture.read_text().splitlines()
        if (record := json.loads(line))["kind"] == "raw-wire"
        and record["stream"] == stream
    ]


def assert_strict_json_owned_processes_stopped(processes):
    assert processes
    for process in processes:
        assert process.child.returncode is not None
        assert process._owns_group and process._group_stopped
        assert not process._group_exists(), "the owned fake process group is still live"
        assert process._watcher.done()
        assert process._exit.done()
        assert all(reader.done() for reader in process._readers)


def strict_json_log_rows(tmp_path):
    return [
        json.loads(line)
        for path in (tmp_path / "logs").glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]


def assert_strict_json_safe_decode_record(rows, wire, error):
    records = [row for row in rows if row["kind"] == "decode_error"]
    assert len(records) == 1
    record = records[0]
    data = wire.removesuffix(b"\n")
    assert record["stream"] == "stdout"
    assert record["error"] == error
    assert record["byte_count"] == len(data)
    assert record["sha256"] == hashlib.sha256(data).hexdigest()
    assert not {"text", "message", "payload"} & record.keys()


@pytest.mark.parametrize(
    "provider,mode",
    [
        ("codex", "json-outer-duplicate"),
        ("claude", "json-outer-duplicate"),
        ("codex", "json-result-duplicate"),
        ("claude", "json-result-duplicate"),
        ("claude", "json-structured-duplicate"),
    ],
)
def test_runtime_rejects_duplicate_outer_and_structured_results(
    tmp_path, monkeypatch, provider, mode
):
    processes = track_strict_json_processes(monkeypatch)
    adapter, config, capture = setup(tmp_path, monkeypatch, provider, mode)
    events, result = asyncio.run(collect(adapter, config))
    assert isinstance(result.terminal, Error)
    assert result.terminal.failure_category is None
    assert result.process_exit is not None
    assert not any(isinstance(event, Completed) for event in events)
    assert isinstance(events[-2], Error)
    assert isinstance(events[-1], ProcessExited)
    assert_strict_json_owned_processes_stopped(processes)
    if provider == "codex":
        assert_codex_resources_removed(adapter)
    rows = strict_json_log_rows(tmp_path)
    assert not any(row["kind"] == "cleanup_incomplete" for row in rows)
    wires = strict_json_raw_wires(capture)
    assert wires, "the fixture must send actual wire bytes, not an already-decoded dict"
    if mode == "json-result-duplicate":
        # The outer frame is valid JSON. Repetition survives in the nested
        # string and must be rejected by structured-artifact decoding.
        messages = [json.loads(wire) for wire in wires]
        if provider == "codex":
            terminal = next(
                item for item in messages if item.get("method") == "turn/completed"
            )
            embedded = terminal["params"]["turn"]["items"][0]["text"]
        else:
            terminal = next(item for item in messages if item.get("type") == "result")
            assert "structured_output" not in terminal
            embedded = terminal["result"]
        assert embedded.count('"summary"') == 2
        assert not any(row["kind"] == "decode_error" for row in rows)
    else:
        assert len(wires) == 1
        repeated = (
            b'"method"'
            if provider == "codex"
            else b'"is_error"'
            if mode == "json-outer-duplicate"
            else b'"summary"'
        )
        assert wires[0].count(repeated) >= 2
        assert_strict_json_safe_decode_record(rows, wires[0], "invalid_json")


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_runtime_bad_stdout_utf8_fails_with_real_exit_and_owned_cleanup(
    tmp_path, monkeypatch, provider
):
    processes = track_strict_json_processes(monkeypatch)
    adapter, config, capture = setup(tmp_path, monkeypatch, provider, "json-bad-utf8")
    events, result = asyncio.run(collect(adapter, config))
    assert isinstance(result.terminal, Error)
    assert result.terminal.failure_category is None
    assert result.process_exit is not None
    assert not any(isinstance(event, Completed) for event in events)
    assert isinstance(events[-1], ProcessExited)
    assert_strict_json_owned_processes_stopped(processes)
    if provider == "codex":
        assert_codex_resources_removed(adapter)
    wires = strict_json_raw_wires(capture)
    assert len(wires) == 1 and b"\xff" in wires[0]
    rows = strict_json_log_rows(tmp_path)
    assert_strict_json_safe_decode_record(rows, wires[0], "invalid_utf8")
    assert not any(row["kind"] == "cleanup_incomplete" for row in rows)


@pytest.mark.parametrize(
    "mode",
    ["auth-json-duplicate", "auth-json-duplicate-pretty", "auth-json-bad-utf8"],
)
def test_environment_rejects_ambiguous_auth_status(tmp_path, monkeypatch, mode):
    processes = track_strict_json_processes(monkeypatch)
    adapter, _, capture = setup(tmp_path, monkeypatch, "claude", mode)
    with pytest.raises(RuntimeFailure) as failure:
        asyncio.run(adapter.check_environment())
    assert failure.value.event.failure_category is None
    assert failure.value.event.raw_type != "adapter.cleanup_incomplete"
    assert not adapter._sessions and not adapter._turns
    records = [json.loads(line) for line in capture.read_text().splitlines()]
    spawns = [record for record in records if record["kind"] == "spawn"]
    assert [record["argv"] for record in spawns] == [["--version"], ["auth", "status"]]
    assert len(processes) == 2
    assert_strict_json_owned_processes_stopped(processes)
    assert all(not Path(record["cwd"]).exists() for record in spawns)
    wires = strict_json_raw_wires(capture)
    assert len(wires) == 1
    rows = strict_json_log_rows(tmp_path)
    assert not any(row["kind"] == "cleanup_incomplete" for row in rows)
    if mode == "auth-json-duplicate-pretty":
        assert wires[0].count(b'"loggedIn"') == 2 and wires[0].count(b"\n") > 1
        assert not any(row["kind"] == "decode_error" for row in rows)
        # Each line was ordinary transport text; check_environment must reject
        # the combined object instead of accepting its last loggedIn field.
        assert sum("loggedIn" in row.get("text", "") for row in rows) == 2
    else:
        assert_strict_json_safe_decode_record(
            rows, wires[0], "invalid_utf8" if mode.endswith("utf8") else "invalid_json"
        )


@pytest.mark.parametrize(
    "mode,marker,error",
    [
        ("config-json-duplicate", b'"ordinary"', "invalid_json"),
        ("config-json-nan", b"NaN", "invalid_json"),
        ("config-json-overflow", b"1e9999", "invalid_json"),
        ("config-json-bad-utf8", b"\xff", "invalid_utf8"),
    ],
)
def test_pending_config_bad_json_never_exposes_unregistered_payload_or_stderr(
    tmp_path, monkeypatch, mode, marker, error
):
    sentinel = "unregistered-config-sentinel-4567"
    processes = track_strict_json_processes(monkeypatch)
    adapter, config, capture = setup(tmp_path, monkeypatch, "codex", mode)
    events, result = asyncio.run(collect(adapter, config))
    assert isinstance(result.terminal, Error)
    assert result.terminal.failure_category is None
    assert result.process_exit is not None
    assert not any(isinstance(event, Completed) for event in events)
    assert sentinel not in result.terminal.detail
    methods = [request.get("method") for request in runtime_requests(capture)]
    assert "config/read" in methods
    assert "thread/start" not in methods and "turn/start" not in methods
    assert_strict_json_owned_processes_stopped(processes)
    assert_codex_resources_removed(adapter)
    rows = strict_json_log_rows(tmp_path)
    assert not any(row["kind"] == "cleanup_incomplete" for row in rows)
    assert sentinel not in json.dumps(rows)
    stdout = strict_json_raw_wires(capture)
    stderr = strict_json_raw_wires(capture, "stderr")
    assert len(stdout) == 1 and marker in stdout[0]
    assert sentinel.encode() in stdout[0]
    if mode == "config-json-duplicate":
        assert stdout[0].count(marker) == 2
    assert len(stderr) == 2 and all(sentinel.encode() in wire for wire in stderr)
    request_id = next(
        request["id"]
        for request in runtime_requests(capture)
        if request.get("method") == "config/read"
    )
    assert json.loads(stderr[0])["id"] == request_id
    diagnostics = [
        row for row in rows if row["kind"] == "recv" and row.get("stream") == "stderr"
    ]
    assert len(diagnostics) == 2
    assert all(
        row.get("text") == "<CONFIG_READ_PAYLOAD_OMITTED>" for row in diagnostics
    )
    assert all("message" not in row for row in diagnostics)
    assert_strict_json_safe_decode_record(rows, stdout[0], error)


@pytest.mark.parametrize(
    "provider,process_count,isolation_count", [("claude", 3, 0), ("codex", 4, 2)]
)
def test_runtime_ownership_tracks_preflight_and_turn_resources(
    tmp_path, monkeypatch, provider, process_count, isolation_count
):
    adapter, config, _ = setup(tmp_path, monkeypatch, provider)
    empty = adapter.ownership_receipt()
    assert not empty.settled and not empty.logs_sealed

    async def run():
        await adapter.check_environment()
        preflight = adapter.ownership_receipt()
        assert preflight.resources_released and preflight.logs_sealed
        session = await adapter.start_session(config)
        turn = await adapter.submit_turn(session, "synthetic ownership integration")
        active = adapter.ownership_receipt()
        assert active.active_operations == 1
        assert active.pending_acquisitions == 1
        assert not active.processes_stopped and not active.logs_sealed
        events = [event async for event in adapter.events(turn)]
        return preflight, active, events, await adapter.result(turn)

    preflight, active, events, result = asyncio.run(run())
    receipt = adapter.ownership_receipt()
    assert isinstance(result.terminal, Completed)
    assert result.process_exit.exit_code == 0
    assert isinstance(events[-1], ProcessExited)
    assert len(receipt.processes) == process_count
    assert len(receipt.isolations) == isolation_count
    assert len(receipt.directories) == 1
    assert receipt.directories[0].removed
    assert len(receipt.logs) == 2
    assert receipt.processes_stopped and receipt.resources_released
    assert receipt.logs_sealed and receipt.outputs_complete
    assert all(item.exit_code == 0 for item in receipt.processes)
    assert all(item.group_gone for item in receipt.processes)
    for log in receipt.logs:
        path = Path(log.path)
        data = path.read_bytes()
        assert log.byte_count == len(data)
        assert log.sha256 == hashlib.sha256(data).hexdigest()
        assert log.records == len(data.splitlines())
        assert log.identity == (path.stat().st_dev, path.stat().st_ino)
    assert len(preflight.logs) == 1 and len(preflight.operations) == 1
    assert active.active_operations == 1 and not active.logs_sealed
    assert empty.operations == () and empty.processes == ()
    another = RuntimeAdapter(provider, log_dir=tmp_path / "unused-logs")
    other = another.ownership_receipt()
    assert other.owner_id != receipt.owner_id
    assert not other.processes_stopped and not other.resources_released


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_runtime_ownership_missing_turn_log_cannot_borrow_preflight(
    tmp_path, monkeypatch, provider
):
    adapter, config, _ = setup(tmp_path, monkeypatch, provider)

    def reject_workspace(state):
        raise ValueError("injected changed workspace before log creation")

    async def run():
        await adapter.check_environment()
        assert adapter.ownership_receipt().logs_sealed
        monkeypatch.setattr(adapter, "_validate_turn_workspace", reject_workspace)
        return await collect(adapter, config)

    _, result = asyncio.run(run())
    receipt = adapter.ownership_receipt()
    assert isinstance(result.terminal, Error)
    assert result.process_exit is None
    assert len(receipt.logs) == 1
    assert receipt.processes_stopped and receipt.resources_released
    assert not receipt.logs_sealed
    assert [item.outcome for item in receipt.acquisitions if item.kind == "log"] == [
        "published",
        "not_created",
    ]


def test_runtime_ownership_missing_environment_log_cannot_borrow_prior_check(
    tmp_path, monkeypatch
):
    from multi_agent.adapters import runtime

    adapter, _, _ = setup(tmp_path, monkeypatch, "claude")
    asyncio.run(adapter.check_environment())
    assert adapter.ownership_receipt().logs_sealed

    def fail_directory(*args, **kwargs):
        raise OSError("injected directory allocation failure")

    monkeypatch.setattr(runtime, "OwnedTemporaryDirectory", fail_directory)
    with pytest.raises(RuntimeFailure, match="directory allocation failure"):
        asyncio.run(adapter.check_environment())
    receipt = adapter.ownership_receipt()
    assert len(receipt.operations) == 2 and receipt.active_operations == 0
    assert len(receipt.logs) == 1 and not receipt.logs_sealed


def test_runtime_ownership_preflight_second_spawn_failure_keeps_first_process(
    tmp_path, monkeypatch
):
    adapter, _, _ = setup(tmp_path, monkeypatch, "claude")
    original_spawn = asyncio.create_subprocess_exec
    spawn_calls = 0

    async def fail_second(*args, **kwargs):
        nonlocal spawn_calls
        spawn_calls += 1
        if spawn_calls == 2:
            raise FileNotFoundError("injected executable missing for auth")
        return await original_spawn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_second)
    with pytest.raises(RuntimeFailure, match="SpawnFailed") as failure:
        asyncio.run(adapter.check_environment())
    launch_error = failure.value.__cause__
    assert isinstance(launch_error, SpawnFailed)
    assert isinstance(launch_error.__cause__, FileNotFoundError)
    assert str(launch_error.__cause__) == "injected executable missing for auth"
    receipt = adapter.ownership_receipt()
    assert spawn_calls == 2 and len(receipt.processes) == 1
    assert receipt.processes[0].exit_code == 0
    assert receipt.processes_stopped and receipt.resources_released
    assert receipt.outputs_complete and receipt.logs_sealed
    assert [
        item.outcome for item in receipt.acquisitions if item.kind == "process"
    ] == ["published", "not_created"]


def test_runtime_ownership_success_does_not_hide_cleanup_failure(tmp_path, monkeypatch):
    adapter, config, _ = setup(tmp_path, monkeypatch, "codex")
    retained = []
    original = CodexIsolation.cleanup

    def retain(owner):
        retained.append(owner)
        return False

    monkeypatch.setattr(CodexIsolation, "cleanup", retain)
    try:
        _, result = asyncio.run(collect(adapter, config))
        receipt = adapter.ownership_receipt()
        assert isinstance(result.terminal, Completed)
        assert result.process_exit.exit_code == 0
        assert receipt.processes_stopped and receipt.logs_sealed
        assert receipt.outputs_complete and not receipt.resources_released
        assert retained and all(not item.removed for item in receipt.isolations)
    finally:
        monkeypatch.setattr(CodexIsolation, "cleanup", original)
        for index, owner in enumerate(retained):
            with Recorder(
                tmp_path / f"retained-owner-cleanup-{index}.jsonl", Redactor()
            ) as recorder:
                owner.recorder = recorder
                assert owner.cleanup()
    assert adapter.ownership_receipt().resources_released
    assert not receipt.resources_released


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_runtime_ownership_terminal_survives_unconfirmed_group_cleanup(
    tmp_path, monkeypatch, provider
):
    mode = "hang-after-result" if provider == "claude" else "success"
    adapter, config, _ = setup(tmp_path, monkeypatch, provider, mode)
    original_spawn = adapter._spawn
    original_group_exists = Process._group_exists
    original_close_finished = Process.close_finished
    main_processes = []

    async def track_main(argv, *args, **kwargs):
        process = await original_spawn(argv, *args, **kwargs)
        if "--version" not in argv:
            main_processes.append(process)
        return process

    def group_remains_unconfirmed(process):
        if process in main_processes:
            return True
        return original_group_exists(process)

    async def stop_waiting_codex(process, grace):
        if provider == "codex" and process in main_processes:
            # Keep the actual synthetic app-server waiting on stdin after its
            # terminal. Exercise real signal/exit cleanup, not a mocked error.
            assert process.child.returncode is None
            assert isinstance(
                next(iter(adapter._turns.values())).observed_terminal, Completed
            )
            await process.terminate(grace)
        else:
            await original_close_finished(process, grace)

    monkeypatch.setattr(adapter, "_spawn", track_main)
    monkeypatch.setattr(Process, "_group_exists", group_remains_unconfirmed)
    monkeypatch.setattr(Process, "close_finished", stop_waiting_codex)

    async def run():
        try:
            events, result = await collect(adapter, config)
            receipt = adapter.ownership_receipt()
            assert len(main_processes) == 1
            process = main_processes[0]
            owned = next(
                item for item in receipt.processes if item.pid == process.child.pid
            )
            assert isinstance(result.terminal, Completed)
            assert (
                result.terminal is next(iter(adapter._turns.values())).observed_terminal
            )
            assert result.process_exit is not None
            assert result.process_exit.exit_code == process.child.returncode
            assert result.process_exit.exit_code != 0
            assert (
                isinstance(events[-2], Completed) and events[-1] == result.process_exit
            )
            assert owned.exit_code == result.process_exit.exit_code
            assert not owned.group_gone and not owned.stopped
            assert owned.watcher_settled and owned.readers_settled
            assert receipt.outputs_complete and receipt.logs_sealed
            assert not receipt.processes_stopped and not receipt.resources_released
            assert any(
                row["kind"] == "cleanup_incomplete"
                for row in strict_json_log_rows(tmp_path)
            )
            if provider == "codex":
                assert len(receipt.isolations) == 1
                assert receipt.isolations[0].unstopped_processes == 1
                assert not receipt.isolations[0].removed
            return receipt
        finally:
            monkeypatch.setattr(Process, "_group_exists", original_group_exists)
            monkeypatch.setattr(Process, "close_finished", original_close_finished)
            # The child has really exited but the injected probe withheld group
            # confirmation. Restore that observation and release retained owners.
            with Recorder(tmp_path / "group-cleanup.jsonl", Redactor()) as recorder:
                for state in adapter._turns.values():
                    if state.isolation is not None:
                        state.isolation.recorder = recorder
                    if state.process is not None:
                        state.process._recorder = recorder
                        if state.isolation is not None:
                            await state.isolation.terminate(
                                state.process, adapter.grace
                            )
                        else:
                            await state.process.terminate(adapter.grace)
                    if state.isolation is not None:
                        assert state.isolation.cleanup()

    receipt = asyncio.run(run())
    assert adapter.ownership_receipt().resources_released
    assert not receipt.processes_stopped and not receipt.resources_released


@pytest.mark.parametrize(
    "reported_terminal,error_type",
    [(False, ProcessCleanupIncomplete), (True, RuntimeError)],
)
def test_runtime_ownership_cleanup_exception_does_not_hide_boundary_failure(
    tmp_path, monkeypatch, reported_terminal, error_type
):
    adapter, config, _ = setup(tmp_path, monkeypatch, "claude")

    async def boundary_failure(state, recorder):
        if reported_terminal:
            adapter._observe(state, Completed({"summary": "ok"}))
        raise error_type("injected boundary failure")

    monkeypatch.setattr(adapter, "_claude", boundary_failure)
    events, result = asyncio.run(collect(adapter, config))
    assert isinstance(result.terminal, Error)
    assert result.terminal.detail == (
        f"runtime boundary failure: {error_type.__name__}: injected boundary failure"
    )
    assert result.terminal.raw_type is None
    assert result.process_exit is None
    assert events == [result.terminal]


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_runtime_ownership_environment_directory_cleanup_failure(
    tmp_path, monkeypatch, provider
):
    from tempfile import TemporaryDirectory

    from multi_agent.adapters.ownership import OwnedTemporaryDirectory

    adapter, _, _ = setup(tmp_path, monkeypatch, provider)
    original = TemporaryDirectory.cleanup
    owner_cleanup = OwnedTemporaryDirectory.cleanup
    retained = []

    def fail_cleanup(directory):
        if Path(directory.name).name.startswith("multi-agent-environment-"):
            raise PermissionError("injected preflight directory removal failure")
        original(directory)

    def capture_cleanup(owner):
        retained.append(owner)
        owner_cleanup(owner)

    monkeypatch.setattr(TemporaryDirectory, "cleanup", fail_cleanup)
    monkeypatch.setattr(OwnedTemporaryDirectory, "cleanup", capture_cleanup)
    try:
        with pytest.raises(RuntimeFailure, match="preflight directory removal failure"):
            asyncio.run(adapter.check_environment())
        receipt = adapter.ownership_receipt()
        assert receipt.processes_stopped and receipt.logs_sealed
        assert receipt.outputs_complete and not receipt.resources_released
        assert len(receipt.directories) == 1 and not receipt.directories[0].removed
    finally:
        monkeypatch.setattr(TemporaryDirectory, "cleanup", original)
        for owner in retained:
            owner_cleanup(owner)
    assert adapter.ownership_receipt().resources_released
