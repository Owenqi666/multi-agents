"""Runtime boundary tests only execute the synthetic CLI shipped with this test."""

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from multi_agent.adapters.base import Permission, SessionConfig
from multi_agent.adapters.probe.process import Process
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
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "runtime"


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
        output_schema={"type": "object"},
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
    assert first.cli_version == "fake-" + provider + " 1.0"
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
    adapter, config, _ = setup(tmp_path, monkeypatch, provider, mode, timeout=0.4)
    events, result = asyncio.run(collect(adapter, config))
    assert isinstance(result.terminal, Error)
    assert result.terminal.failure_category == category
    assert result.process_exit is not None
    assert sum(isinstance(e, (Completed, Error)) for e in events) == 1
    assert isinstance(events[-1], ProcessExited)


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
    pid = json.loads(capture.read_text().splitlines()[0])["pid"]
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
    pids = [row["pid"] for row in rows if row["kind"] in ("spawn", "grandchild")]
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
