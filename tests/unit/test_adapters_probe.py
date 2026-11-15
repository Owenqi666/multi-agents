import asyncio
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
from multi_agent.adapters.probe.recording import Recorder, Redactor
from multi_agent.adapters.probe.runner import run_step

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


def test_cancellation_during_termination_still_kills_and_reaps(setup_fake, tmp_path):
    pid_file = tmp_path / "pid"
    options = replace(
        setup_fake(hang_at="claude", ignore_term=True, pid_file=str(pid_file)),
        step_timeout=0.3,
        grace=0.3,
    )
    directory = tmp_path / "records"
    directory.mkdir()
    log = directory / "claude-turn.jsonl"

    async def run():
        task = asyncio.create_task(
            run_step("claude-turn", options, directory, Redactor())
        )
        try:
            async with asyncio.timeout(3):
                while not log.exists() or not any(
                    record.get("name") == "SIGTERM" for record in read_records(log)
                ):
                    await asyncio.sleep(0.01)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    try:
        asyncio.run(run())
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)
        records = read_records(log)
        assert [record["name"] for record in records if record["kind"] == "signal"] == [
            "SIGTERM",
            "SIGKILL",
        ]
        assert any(
            record["kind"] == "exit" and record["returncode"] == -signal.SIGKILL
            for record in records
        )
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


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
