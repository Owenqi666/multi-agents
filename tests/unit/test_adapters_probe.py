import asyncio
import hashlib
import json
import os
import signal
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from multi_agent.adapters.probe import cli
from multi_agent.adapters.probe.cli import main
from multi_agent.adapters.probe.config import Options, claude_argv
from multi_agent.adapters.probe.process import Process
from multi_agent.adapters.probe.recording import Recorder, Redactor
from multi_agent.adapters.probe.runner import run_step
from multi_agent.adapters.runtime_decode import RuntimeJSONError

KINDS = ("env", "turn", "structured", "readonly", "cancel", "bad-model")
STEPS = tuple(
    f"{provider}-{kind}" for provider in ("codex", "claude") for kind in KINDS
) + ("codex-sandbox",)
FAKE_SOURCE = Path(__file__).parent / "probe_fakes" / "fake_cli.py"


@pytest.fixture(autouse=True)
def forbid_real_cli(monkeypatch):
    original = asyncio.create_subprocess_exec

    async def guarded(program, *args, **kwargs):
        assert Path(program).name.startswith("fake-"), (
            f"unexpected executable: {program}"
        )
        return await original(program, *args, **kwargs)

    async def no_shell(*args, **kwargs):
        pytest.fail("probe used a shell")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", guarded)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", no_shell)
    monkeypatch.setenv("PATH", "")


@pytest.fixture
def setup_fake(tmp_path, monkeypatch):
    binaries = {}
    for provider in ("codex", "claude"):
        binary = tmp_path / f"fake-{provider}"
        binary.write_text(f"#!{sys.executable}\n" + FAKE_SOURCE.read_text())
        binary.chmod(0o755)
        binaries[provider] = str(binary)

    def configure(**settings):
        defaults = {
            "codex_model": "requested-codex",
            "claude_model": "requested-claude",
            "codex_effort": "medium",
            "claude_effort": "low",
        }
        monkeypatch.setenv("MULTI_AGENT_PROBE_FAKE", json.dumps(defaults | settings))
        return Options(
            out=tmp_path / "runs",
            codex_bin=binaries["codex"],
            claude_bin=binaries["claude"],
            codex_model=defaults["codex_model"],
            claude_model=defaults["claude_model"],
            codex_effort=defaults["codex_effort"],
            claude_effort=defaults["claude_effort"],
            step_timeout=3,
            cancel_after=0.05,
            settle=0.05,
            grace=0.2,
        )

    return configure


def read_records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def run_one(tmp_path, options, step):
    directory = tmp_path / step
    directory.mkdir()
    previous = os.environ["MULTI_AGENT_PROBE_FAKE"]
    settings = json.loads(previous)
    settings["expected_step"] = step
    os.environ["MULTI_AGENT_PROBE_FAKE"] = json.dumps(settings)
    try:
        result = asyncio.run(run_step(step, options, directory, Redactor()))
    finally:
        os.environ["MULTI_AGENT_PROBE_FAKE"] = previous
    records = read_records(directory / f"{step}.jsonl")
    assert result["name"] == step
    assert result["file"] == f"{step}.jsonl"
    assert result["records"] == len(records)
    assert result["seconds"] >= 0
    assert [record["seq"] for record in records] == list(range(1, len(records) + 1))
    assert [record["t"] for record in records] == sorted(
        record["t"] for record in records
    )
    return result, records


def messages(records, kind="recv"):
    return [
        record["message"]
        for record in records
        if record["kind"] == kind and "message" in record
    ]


def checked(records):
    assert any(message.get("fake") == "checked" for message in messages(records)), (
        records
    )
    assert not any("Traceback" in record.get("text", "") for record in records)


def cli_args(options, steps):
    return [
        "--out",
        str(options.out),
        "--codex-bin",
        options.codex_bin,
        "--claude-bin",
        options.claude_bin,
        "--codex-model",
        options.codex_model,
        "--claude-model",
        options.claude_model,
        "--codex-effort",
        options.codex_effort,
        "--claude-effort",
        options.claude_effort,
        "--step-timeout",
        str(options.step_timeout),
        "--cancel-after",
        str(options.cancel_after),
        "--settle",
        str(options.settle),
        "--grace",
        str(options.grace),
        *steps,
    ]


def invoke(arguments):
    try:
        return main(arguments)
    except SystemExit as error:
        return error.code


@pytest.mark.parametrize(
    "arguments", [[], ["unknown"], ["codex-turn", "codex-turn"], ["--list"]]
)
def test_cli_validation_precedes_all_side_effects(
    arguments, tmp_path, monkeypatch, capsys
):
    async def forbidden(*args, **kwargs):
        pytest.fail("validation must not start any subprocess")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    output = tmp_path / "must-not-exist"
    assert invoke(["--out", str(output), *arguments]) == (
        0 if arguments == ["--list"] else 2
    )
    assert not output.exists()
    if arguments == ["--list"]:
        lines = capsys.readouterr().out.splitlines()
        for name in STEPS:
            assert sum(name in line for line in lines) == 1
        assert len([line for line in lines if "requests_model=yes" in line]) == 10
        assert len([line for line in lines if "requests_model=no" in line]) == 3


@pytest.mark.parametrize("step", STEPS)
def test_all_steps_use_exact_requests_and_disposable_workdirs(
    step, setup_fake, tmp_path
):
    report = tmp_path / "workdir-path"
    options = setup_fake(workdir_report=str(report))
    result, records = run_one(tmp_path, options, step)
    assert result["outcome"] == "finished"
    if step == "claude-env":
        assert [
            record["returncode"] for record in records if record["kind"] == "exit"
        ] == [0, 1]
    else:
        checked(records)
        assert all(
            record["returncode"] == 0 for record in records if record["kind"] == "exit"
        )
    if report.exists():
        assert not Path(report.read_text()).exists()
    changes = next(
        record["value"] for record in records if record.get("name") == "workdir_changes"
    )
    assert changes == {"added": [], "removed": [], "changed": []}
    observations = [
        record["name"] for record in records if record["kind"] == "observation"
    ]
    assert "read_marker" in observations
    assert ("canary" in observations) == step.endswith("readonly")


def test_codex_records_all_stream_lines_and_late_notifications(setup_fake, tmp_path):
    options = setup_fake(raw_lines=True, approvals=True, late_notifications=True)
    result, records = run_one(tmp_path, options, "codex-turn")
    assert result["outcome"] == "finished"
    checked(records)
    stdout = [
        record.get("text")
        for record in records
        if record["kind"] == "recv"
        and record["stream"] == "stdout"
        and "text" in record
    ]
    assert stdout == ["not-json", '[1, "two"]', '"scalar"', "x" * (1024 * 1024 + 37)]
    stderr = [
        record["text"]
        for record in records
        if record["kind"] == "recv" and record["stream"] == "stderr"
    ]
    assert stderr == ["stderr one", "stderr two"]
    incoming = messages(records)
    methods = [message.get("method") for message in incoming]
    assert (
        methods.index("turn/completed")
        < methods.index("thread/tokenUsage/updated")
        < methods.index("model/rerouted")
    )
    closed = next(
        record["seq"] for record in records if record["kind"] == "stdin_closed"
    )
    completion = next(
        record["seq"]
        for record in records
        if record.get("message", {}).get("method") == "turn/completed"
    )
    assert closed > completion
    responses = {
        message["id"]: message
        for message in messages(records, "send")
        if isinstance(message.get("id"), str)
        and str(message["id"]).startswith("server-")
    }
    assert responses["server-0"]["result"] == {"decision": "decline"}
    assert responses["server-1"]["result"] == {"decision": "decline"}
    assert responses["server-2"]["error"]["code"] == -32601
    sent = messages(records, "send")
    initialized_response = next(
        record["seq"]
        for record in records
        if record["kind"] == "recv"
        and record.get("message", {}).get("id") == sent[0]["id"]
    )
    initialized = next(
        record["seq"]
        for record in records
        if record["kind"] == "send"
        and record.get("message", {}).get("method") == "initialized"
    )
    assert initialized_response < initialized


def test_codex_env_continues_after_error_and_pages_both_lists(setup_fake, tmp_path):
    result, records = run_one(tmp_path, setup_fake(env_error=True), "codex-env")
    assert result["outcome"] == "finished"
    checked(records)
    sent = messages(records, "send")
    pages = [
        message.get("params", {})
        for message in sent
        if message.get("method") == "model/list"
    ]
    assert [
        (page.get("includeHidden", False), page.get("cursor")) for page in pages
    ] == [(False, None), (False, "page-two"), (True, None), (True, "page-two")]
    assert sent[-1]["method"] == "account/rateLimits/read"


@pytest.mark.parametrize("error_at", ["thread/start", "turn/start"])
def test_codex_error_stops_further_requests_but_finishes(
    error_at, setup_fake, tmp_path
):
    result, records = run_one(tmp_path, setup_fake(error_at=error_at), "codex-turn")
    assert result["outcome"] == "finished"
    checked(records)
    requests = [
        message["method"]
        for message in messages(records, "send")
        if "method" in message
    ]
    assert requests[-1] == error_at


@pytest.mark.parametrize("exit_at", ["before-request", "initialize", "turn/start"])
def test_codex_early_process_exit_is_finished(exit_at, setup_fake, tmp_path):
    result, records = run_one(tmp_path, setup_fake(exit_at=exit_at), "codex-turn")
    assert result["outcome"] == "finished"
    assert any(
        record["kind"] == "exit" and record["returncode"] == 0 for record in records
    )
    assert not any(record["kind"] == "timeout" for record in records)


def test_claude_session_options_replace_defaults(setup_fake, tmp_path):
    replacement = ("--safe-mode", "--tools", "Read")
    options = setup_fake(session_args=list(replacement))
    arguments = cli_args(options, ["claude-turn"])
    arguments += [f"--claude-session-arg={item}" for item in replacement]
    assert invoke(arguments) == 0
    (directory,) = options.out.iterdir()
    checked(read_records(directory / "claude-turn.jsonl"))


def test_internal_claude_prompt_preserves_shell_metacharacters(setup_fake, tmp_path):
    prompt = "space \"double\" 'single' $HOME; $(must-not-run)"
    options = setup_fake(special_prompt=prompt)
    (tmp_path / "README.md").write_text("marker")

    async def run():
        process = await asyncio.create_subprocess_exec(
            *claude_argv(options, "claude-turn", prompt=prompt),
            cwd=tmp_path,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        assert process.returncode == 0, stderr
        assert b'"fake": "checked"' in stdout

    asyncio.run(run())


@pytest.mark.parametrize("cancel_mode", ["complete", "exit", "hang"])
def test_codex_cancel_outcomes(cancel_mode, setup_fake, tmp_path):
    result, records = run_one(
        tmp_path, setup_fake(cancel_mode=cancel_mode), "codex-cancel"
    )
    assert result["outcome"] == ("timed_out" if cancel_mode == "hang" else "finished")
    interrupts = [
        message
        for message in messages(records, "send")
        if message.get("method") == "turn/interrupt"
    ]
    assert len(interrupts) == 1
    assert interrupts[0]["params"] == {
        "threadId": "thread-from-server",
        "turnId": "turn-from-server",
    }
    assert sum(record["kind"] == "timeout" for record in records) == (
        1 if cancel_mode == "hang" else 0
    )
    assert not any(record.get("name") == "cancel_skipped" for record in records)


def test_codex_completion_before_start_response_skips_cancel(setup_fake, tmp_path):
    options = replace(setup_fake(complete_before_response=True), cancel_after=0)
    result, records = run_one(tmp_path, options, "codex-cancel")
    assert result["outcome"] == "finished"
    checked(records)
    assert not any(
        message.get("method") == "turn/interrupt"
        for message in messages(records, "send")
    )
    assert any(record.get("name") == "cancel_skipped" for record in records)
    turn_request = next(
        message
        for message in messages(records, "send")
        if message.get("method") == "turn/start"
    )
    completion = next(
        record["seq"]
        for record in records
        if record.get("message", {}).get("method") == "turn/completed"
    )
    response = next(
        record["seq"]
        for record in records
        if record["kind"] == "recv"
        and record.get("message", {}).get("id") == turn_request["id"]
    )
    assert completion < response


def test_unterminated_stdout_and_stderr_tails_are_preserved(setup_fake, tmp_path):
    result, records = run_one(
        tmp_path, setup_fake(unterminated_tail=True), "claude-turn"
    )
    assert result["outcome"] == "finished"
    checked(records)
    tails = {
        record["stream"]: record["text"]
        for record in records
        if record["kind"] == "recv" and "text" in record
    }
    assert tails == {
        "stdout": "stdout tail without newline",
        "stderr": "stderr tail without newline",
    }


@pytest.mark.parametrize("cancel_mode", ["complete", "early"])
def test_claude_cancel_waits_for_output_or_skips_exited_process(
    cancel_mode, setup_fake, tmp_path
):
    options = replace(setup_fake(cancel_mode=cancel_mode), cancel_after=0)
    result, records = run_one(tmp_path, options, "claude-cancel")
    assert result["outcome"] == "finished"
    signals = [record for record in records if record["kind"] == "signal"]
    if cancel_mode == "early":
        assert signals == []
        assert any(record.get("name") == "cancel_skipped" for record in records)
    else:
        first_line = next(
            record["seq"]
            for record in records
            if record["kind"] == "recv" and record["stream"] == "stdout"
        )
        assert [record["name"] for record in signals] == ["SIGINT"]
        assert first_line < signals[0]["seq"]
        checked(records)


def test_claude_ignored_cancel_escalates_and_times_out(setup_fake, tmp_path):
    result, records = run_one(
        tmp_path, setup_fake(ignore_int=True, ignore_term=True), "claude-cancel"
    )
    assert result["outcome"] == "timed_out"
    lifecycle = [
        record.get("name", record["kind"])
        for record in records
        if record["kind"] in {"signal", "timeout", "exit"}
    ]
    assert lifecycle == ["SIGINT", "timeout", "SIGTERM", "SIGKILL", "exit"]
    assert any(record.get("returncode") == -signal.SIGKILL for record in records)


def test_codex_eof_escalation_preserves_finished_outcome(setup_fake, tmp_path):
    result, records = run_one(
        tmp_path, setup_fake(ignore_eof=True, ignore_term=True), "codex-turn"
    )
    assert result["outcome"] == "finished"
    checked(records)
    assert [record["name"] for record in records if record["kind"] == "signal"] == [
        "SIGTERM",
        "SIGKILL",
    ]
    assert not any(record["kind"] == "timeout" for record in records)


@pytest.mark.parametrize("failure", ["timeout", "spawn_failed"])
def test_main_continues_after_failed_step(failure, setup_fake):
    options = setup_fake(hang_at="initialize")
    if failure == "timeout":
        options = replace(options, step_timeout=0.5)
    else:
        options = replace(
            options, codex_bin=str(options.out.parent / "fake-missing-codex")
        )
    assert invoke(cli_args(options, ["codex-turn", "claude-turn"])) == 1
    (directory,) = options.out.iterdir()
    summary = json.loads((directory / "summary.json").read_text())
    assert [step["name"] for step in summary["steps"]] == ["codex-turn", "claude-turn"]
    assert [step["outcome"] for step in summary["steps"]] == [
        "timed_out" if failure == "timeout" else "spawn_failed",
        "finished",
    ]
    checked(read_records(directory / "claude-turn.jsonl"))


def test_cancelled_coroutine_reaps_its_process(setup_fake, tmp_path):
    pid_file = tmp_path / "pid"
    workdir_file = tmp_path / "workdir"
    options = setup_fake(
        hang_at="claude", pid_file=str(pid_file), workdir_report=str(workdir_file)
    )
    directory = tmp_path / "records"
    directory.mkdir()

    async def run():
        task = asyncio.create_task(
            run_step("claude-turn", options, directory, Redactor())
        )
        try:
            async with asyncio.timeout(3):
                while not workdir_file.exists():
                    await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
    assert not Path(workdir_file.read_text()).exists()


def test_workdir_changes_and_markers_are_recorded(setup_fake, tmp_path):
    result, records = run_one(
        tmp_path, setup_fake(change_files=True), "claude-readonly"
    )
    assert result["outcome"] == "finished"
    checked(records)
    changes = next(
        record["value"] for record in records if record.get("name") == "workdir_changes"
    )
    assert changes == {
        "added": ["added.txt"],
        "removed": ["AGENTS.md"],
        "changed": ["README.md"],
    }


def test_spawn_recording_error_propagates_before_launch(
    setup_fake, tmp_path, monkeypatch
):
    options = setup_fake()
    directory = tmp_path / "records"
    directory.mkdir()
    emit = Recorder.emit

    def fail_spawn(self, kind, **fields):
        if kind == "spawn":
            raise OSError("recording unavailable")
        emit(self, kind, **fields)

    async def forbidden(*args, **kwargs):
        pytest.fail("failed recording must not leave an unregistered child")

    monkeypatch.setattr(Recorder, "emit", fail_spawn)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    with pytest.raises(OSError, match="recording unavailable"):
        asyncio.run(run_step("claude-turn", options, directory, Redactor()))


def test_signal_recording_error_still_reaps_and_drains(
    setup_fake, tmp_path, monkeypatch
):
    pid_file = tmp_path / "pid"
    options = setup_fake(
        ignore_int=True,
        ignore_term=True,
        pid_file=str(pid_file),
        expected_step="claude-cancel",
    )
    directory = tmp_path / "records"
    directory.mkdir()
    emit = Recorder.emit

    def fail_signal(self, kind, **fields):
        if kind == "signal":
            raise OSError("recording unavailable")
        emit(self, kind, **fields)

    monkeypatch.setattr(Recorder, "emit", fail_signal)
    with pytest.raises(OSError, match="recording unavailable"):
        asyncio.run(run_step("claude-cancel", options, directory, Redactor()))
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
    records = read_records(directory / "claude-cancel.jsonl")
    assert any(record.get("returncode") == -signal.SIGKILL for record in records)


def test_cancellation_during_spawn_reaps_created_child(
    setup_fake, tmp_path, monkeypatch
):
    options = setup_fake(hang_at="claude")
    directory = tmp_path / "records"
    directory.mkdir()
    original_spawn = asyncio.create_subprocess_exec

    async def run():
        spawned = asyncio.Event()
        release = asyncio.Event()
        children = []

        async def delayed_spawn(*args, **kwargs):
            child = await original_spawn(*args, **kwargs)
            children.append(child)
            spawned.set()
            await release.wait()
            return child

        monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
        task = asyncio.create_task(
            run_step("claude-turn", options, directory, Redactor())
        )
        try:
            async with asyncio.timeout(3):
                await spawned.wait()
                task.cancel()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert len(children) == 1
        assert children[0].returncode is not None
        with pytest.raises(ProcessLookupError):
            os.kill(children[0].pid, 0)

    asyncio.run(run())


def assert_cancellation_during_termination(
    setup_fake, tmp_path, monkeypatch, *, startup_delay
):
    pid_file = tmp_path / "pid"
    options = replace(
        setup_fake(
            hang_at="claude",
            ignore_term=True,
            pid_file=str(pid_file),
            startup_delay=startup_delay,
        ),
        # The outer synchronization budget expires first. Cleanup is triggered
        # by readiness below, never by a deadline racing interpreter startup.
        step_timeout=30,
        grace=0.3,
    )
    directory = tmp_path / "records"
    directory.mkdir()
    log = directory / "claude-turn.jsonl"

    async def run():
        ready = asyncio.Event()
        validated_ready = asyncio.Event()
        owned = []
        cancellations = []
        fallback_kills = []
        original_start = Process.start
        original_wait_exited = Process.wait_exited
        original_signal = Process.signal
        original_emit = Recorder.emit

        async def capture_start(cls, *args, **kwargs):
            process = await original_start(*args, **kwargs)
            owned.append(process)
            return process

        def observe_ready(recorder, kind, **fields):
            original_emit(recorder, kind, **fields)
            if kind == "recv" and fields.get("message") == {"fake": "ready"}:
                ready.set()

        async def trigger_cleanup_after_ready(process):
            if asyncio.current_task() is task:
                # Only replace run_step's initial wait with a controlled trigger.
                # Watchers and termination still await the actual OS process.
                await ready.wait()
                assert owned == [process]
                assert int(pid_file.read_text()) == process.child.pid, (
                    "PID file does not identify the owned child"
                )
                assert process.child.returncode is None
                validated_ready.set()
                raise TimeoutError("test cleanup trigger after fake ready")
            return await original_wait_exited(process)

        def cancel_after_term(process, signum):
            sent = original_signal(process, signum)
            if (
                sent
                and signum == signal.SIGTERM
                and validated_ready.is_set()
                and process in owned
                and not cancellations
            ):
                # signal() has already sent and recorded SIGTERM. The cleanup
                # task now owns the grace/KILL path; cancel its waiting caller
                # synchronously rather than racing a polling coroutine.
                cancellations.append(task.cancel())
            return sent

        monkeypatch.setattr(Process, "start", classmethod(capture_start))
        monkeypatch.setattr(Recorder, "emit", observe_ready)
        monkeypatch.setattr(Process, "wait_exited", trigger_cleanup_after_ready)
        monkeypatch.setattr(Process, "signal", cancel_after_term)
        task = asyncio.create_task(
            run_step("claude-turn", options, directory, Redactor())
        )
        try:
            async with asyncio.timeout(5):
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.shield(task)
            assert cancellations == [True]
            assert validated_ready.is_set()
            assert len(owned) == 1
            assert owned[0].child.returncode == -signal.SIGKILL
        finally:
            # Restore real waits/signals before failure cleanup. A missing ready
            # or PID file must not prevent us from killing/reaping our own child.
            monkeypatch.setattr(Process, "wait_exited", original_wait_exited)
            monkeypatch.setattr(Process, "signal", original_signal)
            monkeypatch.setattr(Recorder, "emit", original_emit)
            if not task.done():
                task.cancel()
            for process in owned:
                if process.child.returncode is None:
                    try:
                        process.child.kill()
                        fallback_kills.append(process.child.pid)
                    except ProcessLookupError:
                        pass
            async with asyncio.timeout(3):
                await asyncio.gather(task, return_exceptions=True)
                await asyncio.gather(
                    *(process.wait() for process in owned), return_exceptions=True
                )
        assert not fallback_kills, "emergency cleanup must not count as test success"

    asyncio.run(run())
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
    records = read_records(log)
    (ready_record,) = [
        record for record in records if record.get("message") == {"fake": "ready"}
    ]
    (timeout_record,) = [record for record in records if record["kind"] == "timeout"]
    signals = [record for record in records if record["kind"] == "signal"]
    assert [record["name"] for record in signals] == ["SIGTERM", "SIGKILL"]
    (exited,) = [record for record in records if record["kind"] == "exit"]
    assert exited["returncode"] == -signal.SIGKILL
    assert ready_record["t"] >= startup_delay
    assert (
        ready_record["seq"]
        < timeout_record["seq"]
        < signals[0]["seq"]
        < signals[1]["seq"]
        < exited["seq"]
    )


def test_cancellation_during_termination_still_kills_and_reaps(
    setup_fake, tmp_path, monkeypatch
):
    assert_cancellation_during_termination(
        setup_fake, tmp_path, monkeypatch, startup_delay=0
    )


def test_cancellation_during_termination_after_slow_start_still_kills_and_reaps(
    setup_fake, tmp_path, monkeypatch
):
    assert_cancellation_during_termination(
        setup_fake, tmp_path, monkeypatch, startup_delay=0.5
    )


def test_invalid_ready_pid_is_not_masked_by_cleanup_cancellation(
    setup_fake, tmp_path, monkeypatch
):
    original_read = Path.read_text
    pid_file = tmp_path / "pid"

    def wrong_pid(path, *args, **kwargs):
        if path == pid_file:
            return "0"
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", wrong_pid)
    with pytest.raises(AssertionError, match="PID file does not identify"):
        assert_cancellation_during_termination(
            setup_fake, tmp_path, monkeypatch, startup_delay=0
        )
    # A readiness validation failure stays visible and cleanup still targets
    # the actual child handle, never the deliberately incorrect PID read.
    with pytest.raises(ProcessLookupError):
        os.kill(int(original_read(pid_file)), 0)
    records = read_records(tmp_path / "records" / "claude-turn.jsonl")
    assert [record["name"] for record in records if record["kind"] == "signal"] == [
        "SIGTERM",
        "SIGKILL",
    ]
    assert any(
        record["kind"] == "exit" and record["returncode"] == -signal.SIGKILL
        for record in records
    )


def test_summary_and_recordings_are_redacted_before_delivery(setup_fake, tmp_path):
    workdir_report = tmp_path / "workdir"
    options = setup_fake(secrets=True, workdir_report=str(workdir_report))
    options = replace(options, out=tmp_path / "contact-probe@example.test")
    assert invoke(cli_args(options, ["claude-turn"])) == 0
    (directory,) = options.out.iterdir()
    text = (directory / "summary.json").read_text() + (
        directory / "claude-turn.jsonl"
    ).read_text()
    for forbidden in (
        str(Path.home()),
        workdir_report.read_text(),
        "probe@example.test",
        "sensitive-token-value",
        "sensitive-user-value",
        "sensitive-org-value",
    ):
        assert forbidden not in text
    records = read_records(directory / "claude-turn.jsonl")
    report = next(message for message in messages(records) if "inputTokens" in message)
    assert report["inputTokens"] == 12
    assert report["outputTokens"] == 5
    assert report["threadId"] == "thread-from-server"
    assert report["session_id"] == "session-from-fake"


def test_output_drain_has_its_own_deadline_and_preserves_unterminated_tails(
    setup_fake, tmp_path
):
    holder_report = tmp_path / "holder.json"
    options = replace(
        setup_fake(hold_output=True, holder_report=str(holder_report)),
        step_timeout=1,
        grace=1.5,
    )
    directory = tmp_path / "records"
    directory.mkdir()

    def kill_holder():
        if holder_report.exists():
            try:
                os.kill(json.loads(holder_report.read_text())["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass

    async def run():
        task = asyncio.create_task(
            run_step("claude-turn", options, directory, Redactor())
        )
        try:
            async with asyncio.timeout(4):
                return await asyncio.shield(task)
        finally:
            if not task.done():
                kill_holder()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    try:
        result = asyncio.run(run())
        holder = json.loads(holder_report.read_text())
        elapsed = time.monotonic() - holder["parent_exits_at"]
        assert options.grace * 0.8 <= elapsed < options.grace + 1.5
        os.kill(holder["pid"], 0)
        records = read_records(directory / "claude-turn.jsonl")
        assert result["outcome"] == "finished"
        assert not any(record["kind"] == "timeout" for record in records)
        tails = [
            record
            for record in records
            if "held tail without newline" in record.get("text", "")
        ]
        assert {record["text"] for record in tails} == {
            "stdout held tail without newline",
            "stderr held tail without newline",
        }
        (cutoff,) = [
            record for record in records if record.get("name") == "output_not_closed"
        ]
        (exited,) = [record for record in records if record["kind"] == "exit"]
        assert cutoff["value"] == "claude"
        assert max(record["seq"] for record in tails) < cutoff["seq"] < exited["seq"]
        assert exited["returncode"] == 0
    finally:
        kill_holder()


def test_multiline_auth_json_redacts_whole_escaped_values(setup_fake, tmp_path):
    result, records = run_one(tmp_path, setup_fake(multiline_auth=True), "claude-env")
    assert result["outcome"] == "finished"
    lines = [
        record["text"]
        for record in records
        if record.get("proc") == "auth-status" and "text" in record
    ]
    text = "\n".join(lines)
    for key in ("email", "orgId", "orgName", "subscriptionType", "planTier"):
        assert f'"{key}": "<REDACTED>"' in text
    assert "private" not in text
    assert "probe@example.test" not in text
    assert '"loggedIn": false' in text
    assert '"authMethod": "oauth"' in text
    assert '"apiKeySource": "none"' in text
    assert (
        '  "orgName": "<REDACTED>", "planTier": "<REDACTED>", "authMethod": "oauth"'
        in lines
    )


@pytest.mark.parametrize(
    "present",
    [
        (),
        ("ANTHROPIC_API_KEY",),
        ("ANTHROPIC_AUTH_TOKEN",),
        ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
    ],
)
def test_claude_removes_only_auth_overrides_and_codex_preserves_them(
    present, setup_fake, monkeypatch
):
    values = {name: f"private-{name.lower()}-value" for name in present}
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("MULTI_AGENT_PROBE_KEEP", "keep-me")
    options = setup_fake(
        check_environment=values | {"MULTI_AGENT_PROBE_KEEP": "keep-me"},
        expected_by_provider={"claude": "claude-env", "codex": "codex-env"},
    )
    assert invoke(cli_args(options, ["claude-env", "codex-env"])) == 0
    (directory,) = options.out.iterdir()
    claude_records = read_records(directory / "claude-env.jsonl")
    codex_records = read_records(directory / "codex-env.jsonl")
    removed = [
        record["value"]
        for record in claude_records
        if record.get("name") == "env_removed"
    ]
    assert len(removed) == 1 and set(removed[0]) == set(present)
    assert not any(record.get("name") == "env_removed" for record in codex_records)
    checked(claude_records)
    checked(codex_records)
    assert [
        record["returncode"] for record in claude_records if record["kind"] == "exit"
    ] == [0, 1]
    recorded = "".join(path.read_text() for path in directory.iterdir())
    for value in values.values():
        assert value not in recorded


@pytest.mark.parametrize("error_at", [None, 0, 1])
def test_codex_sandbox_sends_all_three_commands_without_a_turn(
    error_at, setup_fake, tmp_path
):
    result, records = run_one(
        tmp_path, setup_fake(sandbox_error_at=error_at), "codex-sandbox"
    )
    assert result["outcome"] == "finished"
    checked(records)
    requests = messages(records, "send")
    assert [request["method"] for request in requests] == [
        "initialize",
        "initialized",
        "command/exec",
        "command/exec",
        "command/exec",
    ]
    commands = [
        request["params"] for request in requests if request["method"] == "command/exec"
    ]
    assert [command["command"] for command in commands] == [
        ["cat", "README.md"],
        ["touch", "probe-write-check.txt"],
        ["touch", "probe-write-control.txt"],
    ]
    assert (
        commands[0]["sandboxPolicy"]
        == commands[1]["sandboxPolicy"]
        == {"type": "readOnly"}
    )
    assert commands[2]["sandboxPolicy"] == {
        "type": "workspaceWrite",
        "writableRoots": ["<WORKDIR>"],
    }


@pytest.mark.parametrize("items_error", [False, True])
def test_codex_items_pages_finish_before_settle_and_stdin_close(
    items_error, setup_fake, tmp_path
):
    options = replace(setup_fake(items_error=items_error, items_delay=0.15), settle=0.1)
    result, records = run_one(tmp_path, options, "codex-turn")
    assert result["outcome"] == "finished"
    checked(records)
    requests = [
        record
        for record in records
        if record.get("message", {}).get("method") == "thread/items/list"
    ]
    assert [record["message"]["params"] for record in requests] == (
        [{"threadId": "thread-from-server"}]
        if items_error
        else [
            {"threadId": "thread-from-server"},
            {"threadId": "thread-from-server", "cursor": "items-page-two"},
        ]
    )
    completed = next(
        record
        for record in records
        if record.get("message", {}).get("method") == "turn/completed"
    )
    response = next(
        record
        for record in records
        if record["kind"] == "recv"
        and record.get("message", {}).get("id") == requests[-1]["message"]["id"]
    )
    closed = next(record for record in records if record["kind"] == "stdin_closed")
    assert completed["seq"] < requests[0]["seq"] < response["seq"] < closed["seq"]
    assert closed["t"] - response["t"] >= options.settle * 0.8


@pytest.mark.parametrize("early", [False, True])
def test_items_delay_does_not_extend_cancellation_wait(early, setup_fake, tmp_path):
    options = replace(
        setup_fake(items_delay=0.4, complete_before_response=early), grace=0.15
    )
    result, records = run_one(tmp_path, options, "codex-cancel")
    assert result["outcome"] == "finished"
    checked(records)
    assert not any(record["kind"] == "timeout" for record in records)
    assert sum(
        message.get("method") == "turn/interrupt"
        for message in messages(records, "send")
    ) == (0 if early else 1)
    assert any(record.get("name") == "cancel_skipped" for record in records) is early


def test_items_without_response_use_step_timeout(setup_fake, tmp_path):
    options = replace(
        setup_fake(items_hang=True, complete_before_response=True),
        step_timeout=0.8,
        grace=0.1,
    )
    result, records = run_one(tmp_path, options, "codex-cancel")
    assert result["outcome"] == "timed_out"
    (timeout,) = [record for record in records if record["kind"] == "timeout"]
    spawned = next(record for record in records if record["kind"] == "spawn")
    assert 0.7 <= timeout["t"] - spawned["t"] < 1.8
    assert any(
        message.get("method") == "thread/items/list"
        for message in messages(records, "send")
    )
    assert not any(
        message.get("method") == "turn/interrupt"
        for message in messages(records, "send")
    )


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_bad_model_is_checked_against_independent_step_expectation(
    provider, setup_fake, tmp_path
):
    result, records = run_one(tmp_path, setup_fake(), f"{provider}-bad-model")
    assert result["outcome"] == "finished"
    checked(records)
    assert all(
        record["returncode"] == 0 for record in records if record["kind"] == "exit"
    )


def test_foreign_thread_completion_does_not_end_turn(setup_fake, tmp_path):
    result, records = run_one(
        tmp_path, setup_fake(foreign_completion=True), "codex-turn"
    )
    assert result["outcome"] == "finished"
    checked(records)
    foreign = next(
        record
        for record in records
        if record.get("message", {}).get("method") == "turn/completed"
        and record["message"]["params"]["threadId"] == "other-thread"
    )
    own = next(
        record
        for record in records
        if record.get("message", {}).get("method") == "turn/completed"
        and record["message"]["params"]["threadId"] == "thread-from-server"
    )
    item_request = next(
        record
        for record in records
        if record.get("message", {}).get("method") == "thread/items/list"
    )
    assert foreign["seq"] < own["seq"] < item_request["seq"]


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_cancel_timeout_uses_grace_instead_of_step_timeout(
    provider, setup_fake, tmp_path
):
    options = replace(
        setup_fake(cancel_mode="hang", ignore_int=True), step_timeout=4, grace=0.15
    )
    result, records = run_one(tmp_path, options, f"{provider}-cancel")
    assert result["outcome"] == "timed_out"
    cancellation = next(
        record
        for record in records
        if record.get("name") == "SIGINT"
        or record.get("message", {}).get("method") == "turn/interrupt"
    )
    (timeout,) = [record for record in records if record["kind"] == "timeout"]
    elapsed = timeout["t"] - cancellation["t"]
    assert options.grace * 0.8 <= elapsed < options.grace + 0.75


def test_settle_records_notifications_and_answers_server_requests_before_close(
    setup_fake, tmp_path
):
    result, records = run_one(
        tmp_path, setup_fake(settle_request=True, late_notifications=True), "codex-turn"
    )
    assert result["outcome"] == "finished"
    checked(records)
    closed = next(
        record["seq"] for record in records if record["kind"] == "stdin_closed"
    )
    answer = next(
        record
        for record in records
        if record["kind"] == "send"
        and record.get("message", {}).get("id") == "server-settle"
    )
    assert answer["message"]["result"] == {"decision": "decline"}
    for method in ("thread/tokenUsage/updated", "model/rerouted"):
        notice = next(
            record
            for record in records
            if record.get("message", {}).get("method") == method
        )
        assert answer["seq"] < notice["seq"] < closed


def test_recorded_markers_match_actual_workdir_file_contents(setup_fake, tmp_path):
    contents = tmp_path / "contents.json"
    result, records = run_one(
        tmp_path, setup_fake(contents_report=str(contents)), "codex-readonly"
    )
    assert result["outcome"] == "finished"
    checked(records)
    files = json.loads(contents.read_text())
    marker = next(
        record["value"] for record in records if record.get("name") == "read_marker"
    )
    canary = next(
        record["value"] for record in records if record.get("name") == "canary"
    )
    assert marker in files["README.md"]
    assert canary in files["AGENTS.md"] and canary in files["CLAUDE.md"]


def test_codex_env_runs_version_before_app_server(setup_fake, tmp_path):
    result, records = run_one(tmp_path, setup_fake(), "codex-env")
    assert result["outcome"] == "finished"
    checked(records)
    launches = [record for record in records if record["kind"] == "spawn"]
    assert [record["proc"] for record in launches] == ["version", "app-server"]
    assert launches[0]["argv"][1:] == ["--version"]
    version_exit = next(
        record
        for record in records
        if record["kind"] == "exit" and record["proc"] == "version"
    )
    assert version_exit["seq"] < launches[1]["seq"]
    assert any(record.get("text") == "fake-codex 0.0" for record in records)


def test_cli_passes_the_documented_defaults_to_runner(monkeypatch):
    captured = []

    async def capture(steps, options):
        captured.append((steps, asdict(options)))
        return 0

    monkeypatch.setattr(cli, "run", capture)
    assert main(["codex-turn"]) == 0
    assert captured == [
        (
            ["codex-turn"],
            {
                "out": Path("probe-runs"),
                "codex_bin": "codex",
                "claude_bin": "claude",
                "codex_model": "gpt-6.1-sol",
                "codex_effort": "high",
                "claude_model": "claude-opus-5-5",
                "claude_effort": "high",
                "claude_session_args": None,
                "step_timeout": 180,
                "cancel_after": 3,
                "settle": 1,
                "grace": 15,
            },
        )
    ]


def raw_process_program(tmp_path, source):
    executable = tmp_path / "fake-raw-process"
    executable.write_text(f"#!{sys.executable}\n" + source)
    executable.chmod(0o755)
    return [str(executable)]


async def cleanup_raw_process(process):
    # Failure cleanup is separate from the assertions about production cleanup.
    # It cannot make a failed stop/reader assertion count as success.
    if process.child.returncode is None:
        try:
            process.child.kill()
        except ProcessLookupError:
            pass
    async with asyncio.timeout(3):
        await process.wait_exited()
        await asyncio.gather(process._watcher, return_exceptions=True)


@pytest.mark.parametrize(
    ("payload", "newline", "error_kind"),
    [
        (
            b'{"unregistered-boundary-secret":0,"unregistered-boundary-secret":1}',
            True,
            "invalid_json",
        ),
        (
            b'{"nested":{"value":"unregistered-boundary-secret","value":2}}',
            True,
            "invalid_json",
        ),
        (b'{"value":NaN,"note":"unregistered-boundary-secret"}', True, "invalid_json"),
        (
            b'{"value":Infinity,"note":"unregistered-boundary-secret"}',
            True,
            "invalid_json",
        ),
        (
            b'{"value":-Infinity,"note":"unregistered-boundary-secret"}',
            True,
            "invalid_json",
        ),
        (
            b'{"value":1e309,"note":"unregistered-boundary-secret"}',
            True,
            "invalid_json",
        ),
        (
            b'{"value":"\\ud800","note":"unregistered-boundary-secret"}',
            True,
            "invalid_json",
        ),
        (b'{"\\udfff":"unregistered-boundary-secret"}', True, "invalid_json"),
        (
            b"[" * 129 + b'"unregistered-boundary-secret"' + b"]" * 129,
            True,
            "invalid_json",
        ),
        (b'{"value":"unregistered-boundary-secret\xff"}', True, "invalid_utf8"),
        (b"unregistered-boundary-secret\xff", True, "invalid_utf8"),
        (
            b"x" * (1024 * 1024 + 37) + b"unregistered-boundary-secret\xff",
            True,
            "invalid_utf8",
        ),
        (b"unregistered-boundary-secret\xff", False, "invalid_utf8"),
        (
            b'{"value":NaN,"note":"unregistered-boundary-secret"}\r',
            True,
            "invalid_json",
        ),
    ],
    ids=[
        "duplicate-secret-key",
        "nested-duplicate",
        "nan",
        "infinity",
        "negative-infinity",
        "overflow",
        "surrogate-value",
        "surrogate-key",
        "depth",
        "utf8-object",
        "utf8-text",
        "utf8-large-text",
        "utf8-tail",
        "raw-carriage-return-hash",
    ],
)
def test_process_rejects_invalid_stdout_without_recording_payload(
    tmp_path, monkeypatch, payload, newline, error_kind
):
    wire = payload + (b"\n" if newline else b"")
    argv = raw_process_program(
        tmp_path,
        "import os\n"
        f"os.write(1, {wire!r})\n"
        "os.write(2, b'unregistered-stderr-secret\\n')\n"
        "os._exit(23)\n",
    )
    path = tmp_path / "raw.jsonl"

    async def run():
        stderr_done = asyncio.Event()
        original_read_stream = Process._read_stream

        async def read_after_actual_exit(process, reader, stream):
            if len(payload) > 65536:
                # Large writes need concurrent draining to avoid blocking the
                # child on pipe capacity; its observed exit may be signal-based.
                await original_read_stream(process, reader, stream)
                return
            # Deterministically preserve the child's chosen OS exit, then read
            # diagnostics before the invalid stdout can cancel the other reader.
            await process.wait_exited()
            if stream == "stdout":
                await stderr_done.wait()
                await original_read_stream(process, reader, stream)
            else:
                try:
                    await original_read_stream(process, reader, stream)
                finally:
                    stderr_done.set()

        monkeypatch.setattr(Process, "_read_stream", read_after_actual_exit)
        with Recorder(path, Redactor()) as recorder:
            recorder.emit(
                "send",
                proc="raw",
                message={"id": 17, "method": "config/read", "params": {}},
            )
            process = await Process.start("raw", argv, tmp_path, recorder, grace=0.1)
            try:
                async with asyncio.timeout(3):
                    with pytest.raises(RuntimeJSONError) as caught:
                        await process.wait()
                    assert "unregistered-boundary-secret" not in str(caught.value)
                    assert len(str(caught.value)) < 200
                    assert process.child.returncode is not None
                    if len(payload) <= 65536:
                        assert process.child.returncode == 23
                    assert await process.wait_exited() == process.child.returncode
                    assert all(reader.done() for reader in process._readers)
                    await process.terminate(0.1)
                    await process.terminate(0.1)
                    with pytest.raises(RuntimeJSONError) as again:
                        await process.read()
                    assert again.value is caught.value
                    with pytest.raises(RuntimeJSONError) as waited:
                        await process.wait()
                    assert waited.value is caught.value
            finally:
                await cleanup_raw_process(process)

    asyncio.run(run())
    records = read_records(path)
    (failure,) = [record for record in records if record["kind"] == "decode_error"]
    assert set(failure) == {
        "seq",
        "t",
        "kind",
        "proc",
        "stream",
        "error",
        "byte_count",
        "sha256",
    }
    assert failure["proc"] == "raw"
    assert failure["stream"] == "stdout"
    assert failure["error"] == error_kind
    assert failure["byte_count"] == len(payload)
    assert failure["sha256"] == hashlib.sha256(payload).hexdigest()
    assert not any(
        record["kind"] == "recv" and record.get("stream") == "stdout"
        for record in records
    )
    diagnostics = [record for record in records if record.get("stream") == "stderr"]
    if len(payload) <= 65536:
        assert len(diagnostics) == 1
    assert all(
        record["text"] == "<CONFIG_READ_PAYLOAD_OMITTED>" for record in diagnostics
    )
    assert "unregistered-boundary-secret" not in path.read_text()
    assert "unregistered-stderr-secret" not in path.read_text()


@pytest.mark.parametrize("owned_group", [False, True])
def test_process_invalid_stdout_stops_sleeping_child_and_keeps_read_failure(
    tmp_path, owned_group
):
    argv = raw_process_program(
        tmp_path,
        'import os, time\nos.write(1, b\'{"same":1,"same":2}\\n\')\ntime.sleep(60)\n',
    )

    async def run():
        with Recorder(tmp_path / "sleeping.jsonl", Redactor()) as recorder:
            process = await Process.start(
                "raw",
                argv,
                tmp_path,
                recorder,
                grace=0.1,
                start_new_session=owned_group,
            )
            try:
                async with asyncio.timeout(3):
                    with pytest.raises(RuntimeJSONError) as caught:
                        first = await process.read()
                        if first is None:
                            # read() can observe OS exit before final pipe drain;
                            # wait() must still surface the failed reader.
                            await process.wait()
                    assert process.child.returncode == -signal.SIGTERM
                    assert await process.wait_exited() == -signal.SIGTERM
                    assert process._watcher.done()
                    assert all(reader.done() for reader in process._readers)
                    if owned_group:
                        assert process._group_stopped
                    with pytest.raises(ProcessLookupError):
                        os.kill(process.child.pid, 0)
                    await process.terminate(0.1)
                    await process.terminate(0.1)
                    for operation in (process.read, process.wait):
                        with pytest.raises(RuntimeJSONError) as repeated:
                            await operation()
                        assert repeated.value is caught.value
            finally:
                await cleanup_raw_process(process)

    asyncio.run(run())


def test_process_preserves_plain_text_and_trailing_lines(tmp_path):
    lines = [
        "not-json",
        '{"unfinished":',
        '[1, "two"]',
        '"scalar"',
        "true",
        "42",
        "null",
        "x" * (1024 * 1024 + 37),
        "tail without newline",
    ]
    stdout = b'{"valid":"caf\xc3\xa9"}\n' + "\n".join(lines).encode()
    stderr = b'diagnostic\xff\n{"repeated":1,"repeated":2}\nstderr tail'
    argv = raw_process_program(
        tmp_path,
        f"import os\nos.write(1, {stdout!r})\nos.write(2, {stderr!r})\nos._exit(7)\n",
    )
    path = tmp_path / "valid.jsonl"

    async def run():
        with Recorder(path, Redactor()) as recorder:
            process = await Process.start("raw", argv, tmp_path, recorder, grace=0.1)
            try:
                async with asyncio.timeout(3):
                    assert await process.wait() == 7
                    values = []
                    while (value := await process.read()) is not None:
                        values.append(value)
                    assert values == [{"valid": "café"}, *lines]
                    assert all(reader.done() for reader in process._readers)
                    await process.terminate(0.1)
            finally:
                await cleanup_raw_process(process)

    asyncio.run(run())
    records = read_records(path)
    assert not any(record["kind"] == "decode_error" for record in records)
    assert [
        record["text"]
        for record in records
        if record.get("stream") == "stdout" and "text" in record
    ] == lines
    diagnostics = [record for record in records if record.get("stream") == "stderr"]
    assert diagnostics[0]["text"] == "diagnostic�"
    assert diagnostics[0]["encoding_replaced"] is True
    assert diagnostics[1]["text"] == '{"repeated":1,"repeated":2}'
    assert diagnostics[2]["text"] == "stderr tail"
    assert all("encoding_replaced" not in record for record in diagnostics[1:])
    assert [record["returncode"] for record in records if record["kind"] == "exit"] == [
        7
    ]


@pytest.mark.parametrize("error_type", [OSError, RuntimeError])
def test_process_json_failure_does_not_hide_stop_failure(
    tmp_path, monkeypatch, error_type
):
    argv = raw_process_program(
        tmp_path,
        'import os\nos.write(1, b\'{"same":1,"same":2}\\n\')\n',
    )

    async def run():
        with Recorder(tmp_path / "stop-error.jsonl", Redactor()) as recorder:
            process = await Process.start("raw", argv, tmp_path, recorder, grace=0.1)
            try:
                async with asyncio.timeout(3):
                    with pytest.raises(RuntimeJSONError):
                        await process.wait()
                    assert process.child.returncode is not None
                    failure = error_type("injected stop failure")

                    async def cannot_confirm_stop(grace):
                        raise failure

                    with monkeypatch.context() as patcher:
                        patcher.setattr(process, "_stop_child", cannot_confirm_stop)
                        with pytest.raises(error_type) as caught:
                            await process.terminate(0.1)
                    assert caught.value is failure
            finally:
                await cleanup_raw_process(process)

    asyncio.run(run())


def test_process_json_failure_does_not_hide_other_reader_recording_failure(
    tmp_path, monkeypatch
):
    argv = raw_process_program(
        tmp_path,
        "import os, time\n"
        "os.write(2, b'stderr recording fails\\n')\n"
        'os.write(1, b\'{"same":1,"same":2}\\n\')\n'
        "time.sleep(60)\n",
    )

    async def run():
        stdout_failed = asyncio.Event()
        stderr_ready = asyncio.Event()
        original_read_stream = Process._read_stream
        original_emit = Recorder.emit
        failure = OSError("injected stderr recording failure")

        async def order_readers(process, reader, stream):
            if stream == "stdout":
                await stderr_ready.wait()
                try:
                    await original_read_stream(process, reader, stream)
                finally:
                    stdout_failed.set()
            else:
                original_read = reader.read

                async def read_when_stdout_failed(size):
                    data = await original_read(size)
                    # Consume actual stderr bytes before releasing stdout; do
                    # not rely on the child winning a scheduling race to write.
                    stderr_ready.set()
                    await stdout_failed.wait()
                    return data

                monkeypatch.setattr(reader, "read", read_when_stdout_failed)
                await original_read_stream(process, reader, stream)

        def fail_stderr_recording(recorder, kind, **fields):
            if kind == "recv" and fields.get("stream") == "stderr":
                raise failure
            return original_emit(recorder, kind, **fields)

        monkeypatch.setattr(Process, "_read_stream", order_readers)
        monkeypatch.setattr(Recorder, "emit", fail_stderr_recording)
        with Recorder(tmp_path / "reader-errors.jsonl", Redactor()) as recorder:
            process = await Process.start("raw", argv, tmp_path, recorder, grace=0.1)
            try:
                async with asyncio.timeout(3):
                    with pytest.raises((RuntimeJSONError, OSError)):
                        await process.wait()
                    assert isinstance(process._readers[0].exception(), RuntimeJSONError)
                    assert process._readers[1].exception() is failure
                    assert process.child.returncode is not None
                    with pytest.raises(OSError) as caught:
                        await process.terminate(0.1)
                    assert caught.value is failure
            finally:
                await cleanup_raw_process(process)

    asyncio.run(run())


def test_process_stderr_cannot_consume_pending_config_read(tmp_path):
    argv = raw_process_program(
        tmp_path,
        "import os\n"
        'os.write(2, b\'{"id":17,"result":{"value":"unregistered-json-secret"}}\\n\')\n'
        "os.write(2, b'unregistered-plain-secret\\n')\n"
        "os.write(2, b'unregistered-encoding-secret\\xff\\n')\n",
    )
    path = tmp_path / "stderr-config.jsonl"

    async def run():
        with Recorder(path, Redactor()) as recorder:
            recorder.emit(
                "send",
                proc="raw",
                message={"id": 17, "method": "config/read", "params": {}},
            )
            process = await Process.start("raw", argv, tmp_path, recorder, grace=0.1)
            try:
                async with asyncio.timeout(3):
                    assert await process.wait() == 0
                    assert await process.read() is None
                    assert recorder._pending_config_reads["raw"] == {17}
                    await process.terminate(0.1)
            finally:
                await cleanup_raw_process(process)

    asyncio.run(run())
    diagnostics = [
        record for record in read_records(path) if record.get("stream") == "stderr"
    ]
    assert len(diagnostics) == 3
    assert all(
        record["text"] == "<CONFIG_READ_PAYLOAD_OMITTED>" for record in diagnostics
    )
    assert all("message" not in record for record in diagnostics)
    assert all(
        record["config_read_text_audit"]["raw_payload_omitted"]
        for record in diagnostics
    )
    assert diagnostics[2]["encoding_replaced"] is True
    assert "unregistered-" not in path.read_text()


def test_process_group_signal_denied_after_actual_group_disappearance(
    tmp_path, monkeypatch
):
    argv = raw_process_program(tmp_path, "import os\nos._exit(7)\n")

    async def run():
        with Recorder(tmp_path / "gone-group.jsonl", Redactor()) as recorder:
            process = await Process.start(
                "raw", argv, tmp_path, recorder, grace=0.02, start_new_session=True
            )
            try:
                async with asyncio.timeout(3):
                    assert await process.wait() == 7
                    actual_killpg = os.killpg
                    denied = PermissionError(1, "injected transient signal denial")
                    signals = []

                    def deny_term_only(group, sig):
                        assert group == process.child.pid
                        if sig == signal.SIGTERM:
                            signals.append(sig)
                            raise denied
                        # Group disappearance must be real, not supplied by a
                        # mocked success return or by the leader's exit alone.
                        return actual_killpg(group, sig)

                    with monkeypatch.context() as patcher:
                        patcher.setattr(os, "killpg", deny_term_only)
                        await process.terminate(0.02)
                    assert signals == [signal.SIGTERM]
                    assert process._group_stopped
                    assert await process.wait_exited() == 7
                    with pytest.raises(ProcessLookupError):
                        actual_killpg(process.child.pid, 0)
            finally:
                await cleanup_raw_process(process)

    asyncio.run(run())


def test_process_group_signal_denied_with_unknown_group_stays_unconfirmed(
    tmp_path, monkeypatch
):
    argv = raw_process_program(tmp_path, "import os\nos._exit(7)\n")

    async def run():
        with Recorder(tmp_path / "unknown-group.jsonl", Redactor()) as recorder:
            process = await Process.start(
                "raw", argv, tmp_path, recorder, grace=0.02, start_new_session=True
            )
            try:
                async with asyncio.timeout(3):
                    assert await process.wait() == 7
                    denied = PermissionError(1, "injected inaccessible group")
                    signals = []

                    def inaccessible_group(group, sig):
                        assert group == process.child.pid
                        signals.append(sig)
                        raise denied

                    with monkeypatch.context() as patcher:
                        patcher.setattr(os, "killpg", inaccessible_group)
                        with pytest.raises(PermissionError) as caught:
                            await process.terminate(0.02)
                    assert caught.value.__cause__ is denied
                    assert signal.SIGTERM in signals and signal.SIGKILL in signals
                    assert 0 in signals
                    assert not process._group_stopped
                    assert process.child.returncode == 7
            finally:
                await cleanup_raw_process(process)

    asyncio.run(run())


def test_process_group_does_not_discard_recorder_permission_error(
    tmp_path, monkeypatch
):
    argv = raw_process_program(tmp_path, "import os\nos._exit(7)\n")

    async def run():
        with Recorder(tmp_path / "recording-permission.jsonl", Redactor()) as recorder:
            process = await Process.start(
                "raw", argv, tmp_path, recorder, grace=0.02, start_new_session=True
            )
            try:
                async with asyncio.timeout(3):
                    assert await process.wait() == 7
                    actual_killpg = os.killpg
                    actual_emit = recorder.emit
                    denied = PermissionError(1, "injected log write denial")

                    def successful_signal(group, sig):
                        assert group == process.child.pid
                        if sig != 0:
                            return None
                        return actual_killpg(group, sig)

                    def deny_signal_record(kind, **fields):
                        if kind == "signal":
                            raise denied
                        return actual_emit(kind, **fields)

                    with monkeypatch.context() as patcher:
                        patcher.setattr(os, "killpg", successful_signal)
                        patcher.setattr(recorder, "emit", deny_signal_record)
                        with pytest.raises(PermissionError) as caught:
                            await process.terminate(0.02)
                    assert caught.value is denied
                    assert await process.wait_exited() == 7
                    with pytest.raises(ProcessLookupError):
                        actual_killpg(process.child.pid, 0)
            finally:
                await cleanup_raw_process(process)

    asyncio.run(run())


def test_process_unknown_group_stop_failure_precedes_json_reader_error(
    tmp_path, monkeypatch
):
    argv = raw_process_program(
        tmp_path,
        'import os\nos.write(1, b\'{"same":1,"same":2}\\n\')\nos._exit(7)\n',
    )

    async def run():
        original_read_stream = Process._read_stream

        async def read_after_exit(process, reader, stream):
            await process.wait_exited()
            await original_read_stream(process, reader, stream)

        monkeypatch.setattr(Process, "_read_stream", read_after_exit)
        with Recorder(tmp_path / "stop-priority.jsonl", Redactor()) as recorder:
            process = await Process.start(
                "raw", argv, tmp_path, recorder, grace=0.02, start_new_session=True
            )
            try:
                async with asyncio.timeout(3):
                    denied = PermissionError(1, "injected inaccessible group")

                    def inaccessible_group(group, sig):
                        assert group == process.child.pid
                        raise denied

                    with monkeypatch.context() as patcher:
                        patcher.setattr(os, "killpg", inaccessible_group)
                        with pytest.raises(RuntimeJSONError) as parse_error:
                            await process.wait()
                        assert process.child.returncode == 7
                        assert not process._group_stopped
                        stop_error = OSError("injected unconfirmed stop")

                        async def cannot_stop(grace):
                            raise stop_error

                        patcher.setattr(process, "_stop_child", cannot_stop)
                        with pytest.raises(OSError) as caught:
                            await process.terminate(0.02)
                        assert caught.value is stop_error
                        assert not process._group_stopped
                        with pytest.raises(RuntimeJSONError) as repeated:
                            await process.wait()
                        assert repeated.value is parse_error.value
            finally:
                await cleanup_raw_process(process)

    asyncio.run(run())


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param(b'{"same":1,"same":2}', None, id="invalid-json-tail"),
        pytest.param(b'{"tail":"caf\xc3\xa9"}', {"tail": "café"}, id="valid-json-tail"),
    ],
)
def test_process_read_after_os_exit_waits_for_unparsed_stdout_tail(
    tmp_path, monkeypatch, payload, expected
):
    argv = raw_process_program(
        tmp_path, f"import os\nos.write(1, {payload!r})\nos._exit(7)\n"
    )

    async def run():
        stdout_buffered = asyncio.Event()
        release_stdout = asyncio.Event()
        read_started = asyncio.Event()
        original_read_stream = Process._read_stream

        async def hold_stdout_before_line(process, reader, stream):
            if stream == "stdout":
                original_read = reader.read

                async def gated_read(size):
                    data = await original_read(size)
                    if data:
                        stdout_buffered.set()
                        await release_stdout.wait()
                    return data

                monkeypatch.setattr(reader, "read", gated_read)
            await original_read_stream(process, reader, stream)

        monkeypatch.setattr(Process, "_read_stream", hold_stdout_before_line)
        with Recorder(tmp_path / "exit-before-drain.jsonl", Redactor()) as recorder:
            process = await Process.start("raw", argv, tmp_path, recorder, grace=1)
            reading = None
            try:
                async with asyncio.timeout(3):
                    await stdout_buffered.wait()
                    assert await process.wait_exited() == 7
                    assert not process._watcher.done()

                    async def read_after_exit():
                        read_started.set()
                        return await process.read()

                    reading = asyncio.create_task(read_after_exit())
                    await read_started.wait()
                    # The child has exited and its bytes are already in the
                    # host, but _line has not run. OS exit alone is not EOF.
                    assert not reading.done()
                    release_stdout.set()
                    if expected is None:
                        with pytest.raises(RuntimeJSONError) as caught:
                            await reading
                        with pytest.raises(RuntimeJSONError) as waited:
                            await process.wait()
                        assert waited.value is caught.value
                    else:
                        assert await reading == expected
                        assert await process.read() is None
                        assert await process.wait() == 7
                    assert process.child.returncode == 7
                    assert all(reader.done() for reader in process._readers)
                    await process.terminate(0.1)
            finally:
                release_stdout.set()
                await cleanup_raw_process(process)
                if reading is not None:
                    await asyncio.gather(reading, return_exceptions=True)

    asyncio.run(run())


def test_process_live_group_signal_denial_fails_without_waiting_for_exit(
    tmp_path, monkeypatch
):
    argv = raw_process_program(
        tmp_path,
        "import os, time\nos.write(1, b'{\"ready\":true}\\n')\ntime.sleep(60)\n",
    )

    async def run():
        with Recorder(tmp_path / "live-group-denied.jsonl", Redactor()) as recorder:
            process = await Process.start(
                "raw", argv, tmp_path, recorder, grace=0.02, start_new_session=True
            )
            terminating = None
            try:
                async with asyncio.timeout(3):
                    assert await process.read() == {"ready": True}
                assert process.child.returncode is None
                denied = PermissionError(1, "injected live group denial")
                calls = []

                def inaccessible_group(group, sig):
                    assert group == process.child.pid
                    calls.append(sig)
                    raise denied

                with monkeypatch.context() as patcher:
                    patcher.setattr(os, "killpg", inaccessible_group)
                    terminating = asyncio.create_task(process.terminate(0.02))
                    # wait_for/timeout would cancel the caller, which terminate
                    # deliberately shields. Observe the deadline without asking
                    # the still-owned cleanup task to abandon its work.
                    done, _ = await asyncio.wait((terminating,), timeout=1)
                    assert terminating in done, "stop denial waited forever for OS exit"
                    with pytest.raises(PermissionError) as caught:
                        terminating.result()
                    assert caught.value.__cause__ is denied
                    assert signal.SIGTERM in calls and signal.SIGKILL in calls
                    assert 0 in calls
                    assert process.child.returncode is None
                    assert not process._group_stopped
                    assert not process._exit.done()
                    assert not process._watcher.done()
                    assert all(not reader.done() for reader in process._readers)
                    assert process._stdout.empty(), "unconfirmed stop fabricated EOF"
            finally:
                # Restore real signals before clearing the deliberately live
                # owner. This external cleanup is never proof of terminate()
                # succeeding, and the assertions above require its failure.
                await cleanup_raw_process(process)
                if terminating is not None:
                    await asyncio.gather(terminating, return_exceptions=True)
            assert process.child.returncode == -signal.SIGKILL
            assert process._exit.done() and process._watcher.done()
            assert all(reader.done() for reader in process._readers)
            with pytest.raises(ProcessLookupError):
                os.killpg(process.child.pid, 0)

    asyncio.run(run())
