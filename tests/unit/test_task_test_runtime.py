"""Independent command transport uses fake executables, never a native CLI."""

import asyncio
import hashlib
import json
import stat
import sys
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft7Validator

from multi_agent.adapters import test_isolation as isolation
from multi_agent.adapters import test_recording as recording
from multi_agent.adapters.ownership import OwnershipLedger
from multi_agent.adapters.probe.recording import Redactor
from multi_agent.adapters.test_runtime import TaskTestRuntimeAdapter
from multi_agent.core.events import FailureCategory, thaw_json
from multi_agent.core.task_events import TaskCandidateFrozen
from multi_agent.core.task_test_execution import (
    TaskTestCheckout,
    TaskTestPolicy,
    TaskTestScratch,
)
from multi_agent.core.task_test_input import TaskTestInput
from multi_agent.core.tasks import NamedCommand
from multi_agent.core.workspaces import WorkspaceDelivery

ROOT = Path(__file__).resolve().parents[2]
FAKE = Path(__file__).parent / "runtime_fakes/test_cli.py"
SCHEMAS = ROOT / "protocol/codex/schema"


def identity(path):
    info = path.stat()
    return info.st_dev, info.st_ino


@pytest.fixture
def runtime_setup(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    for name in ("checkout", "scratch", "original", "context", "logs", "host"):
        (root / name).mkdir(mode=0o700)
    (root / "checkout/.git").mkdir()
    (root / "original/.git").mkdir()
    for name in ("home", "tmp", "cache"):
        (root / "scratch" / name).mkdir(mode=0o700)
    host = root / "host"
    auth_dir = host / ".codex"
    auth_dir.mkdir(mode=0o700)
    auth = auth_dir / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "tokens": {
                    key: f"synthetic-secret-{key}"
                    for key in ("id_token", "access_token", "refresh_token")
                }
            }
        )
    )
    auth.chmod(0o600)
    monkeypatch.setenv("HOME", str(host))
    monkeypatch.setenv("CODEX_HOME", str(auth_dir))
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-command")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-reach-command")
    executable = root / "fake-codex"
    executable.write_text(f"#!{sys.executable}\n" + FAKE.read_text())
    executable.chmod(0o755)
    capture = root / "capture.jsonl"
    settings = {"mode": "success", "capture": str(capture), "execute": True}
    executable.with_suffix(".json").write_text(json.dumps(settings))
    command = NamedCommand(
        "unit",
        str(Path(sys.executable).resolve()),
        ("-c", "import os; os.write(1,b'out\\x00\\xff'); os.write(2,b'err\\xfe')"),
    )
    value = TaskTestInput(
        task_run_id="synthetic-run",
        implementation_attempt=1,
        task_id="T-0050",
        task_hash="a" * 64,
        rules_hash="b" * 64,
        task_input_hash="c" * 64,
        candidate=TaskCandidateFrozen(
            WorkspaceDelivery(
                "a" * 40, "b" * 40, (), "d" * 64, "synthetic-ref", "c" * 40
            ),
            "/synthetic/execution.json",
            "e" * 64,
        ),
        commands=(command,),
        declaration_source="Explicit fake-only test",
        task_history_length=1,
        task_history_hash="f" * 64,
    )
    database = root / "task.sqlite"
    database.write_bytes(b"synthetic-not-a-database")
    checkout = TaskTestCheckout(
        input=value,
        repository=str(root / "original/.git"),
        database_path=str(database),
        database_identity=identity(database),
        database_parent_identity=identity(root),
        root=str(root / "checkout"),
        root_identity=identity(root / "checkout"),
        git_directory=str(root / "checkout/.git"),
        git_identity=identity(root / "checkout/.git"),
        owner_token="synthetic-owner",
        object_format="sha1",
        initial_digest="d" * 64,
        controls=(),
        objects=(),
        observed_test_attempt=None,
        test_history_length=0,
        test_history_hash="f" * 64,
    )
    scratch = TaskTestScratch(
        path=str(root / "scratch"),
        identity=identity(root / "scratch"),
        home_identity=identity(root / "scratch/home"),
        tmp_identity=identity(root / "scratch/tmp"),
        cache_identity=identity(root / "scratch/cache"),
    )
    policy = TaskTestPolicy(
        executable=str(executable),
        executable_identity=identity(executable),
        executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
        timeout=4.0,
        grace=0.3,
        output_bytes_limit=8192,
    )
    original_spawn = asyncio.create_subprocess_exec

    async def only_fake(program, *args, **kwargs):
        assert program == str(executable), "attempted to start a non-fake CLI"
        return await original_spawn(program, *args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", only_fake)
    return SimpleNamespace(
        root=root,
        checkout=checkout,
        scratch=scratch,
        policy=policy,
        command=command,
        settings=settings,
        executable=executable,
        capture=capture,
        auth=auth,
    )


def configure(setup, **changes):
    setup.settings.update(changes)
    setup.executable.with_suffix(".json").write_text(json.dumps(setup.settings))


def adapter(setup, **changes):
    arguments = {
        "checkout": setup.checkout,
        "scratch": setup.scratch,
        "policy": setup.policy,
        "log_dir": setup.root / "logs/command-1",
    }
    arguments.update(changes)
    return TaskTestRuntimeAdapter(**arguments)


def captured(setup):
    return [json.loads(line) for line in setup.capture.read_text().splitlines()]


def settled(result):
    assert result.receipt.settled
    assert result.receipt.processes_stopped
    assert result.receipt.resources_released
    assert all(
        item.exit_code is not None and item.group_gone
        for item in result.receipt.processes
    )
    assert all(not Path(item.root).exists() for item in result.receipt.isolations)


def test_task_test_runtime_uses_frozen_argv_and_native_policy(runtime_setup):
    setup = runtime_setup
    runtime = adapter(setup)
    before = runtime.snapshot()
    assert not before.receipt.settled and not before.request_attempted
    assert not runtime.log_dir.exists()
    result = asyncio.run(runtime.run(setup.command, sequence=1))
    assert result.exit_code == result.reported_exit_code == 0
    assert not result.problems and not result.timeout_observed
    assert result.exit_code_provenance == "codex-v0.169.0-runtime-normalized"
    assert result.environment.cli_version == "codex-cli 0.169.0"
    assert result.environment.logged_in is None
    assert result.stdout_received_bytes == 5 and result.stderr_received_bytes == 4
    assert result.stdout_cap_reached is False and result.stderr_cap_reached is False
    assert (runtime.log_dir / "stdout.bin").read_bytes() == b"out\x00\xff"
    assert (runtime.log_dir / "stderr.bin").read_bytes() == b"err\xfe"
    assert result.receipt.logs_sealed
    assert {Path(log.path).name for log in result.receipt.logs} == {
        "stdout.bin",
        "stderr.bin",
        "wire.jsonl",
    }
    assert all(
        stat.S_IMODE(Path(log.path).stat().st_mode) == 0o400
        for log in result.receipt.logs
    )
    settled(result)
    events = captured(setup)
    methods = [
        event["message"]["method"] for event in events if event["kind"] == "request"
    ]
    assert methods == [
        "initialize",
        "initialized",
        "config/read",
        "configRequirements/read",
        "command/exec",
    ]
    request = next(
        event["message"]
        for event in events
        if event["kind"] == "request" and event["message"]["method"] == "command/exec"
    )
    params = request["params"]
    assert params["command"] == [setup.command.executable, *setup.command.args]
    assert params["cwd"] == setup.checkout.root
    assert params["tty"] is False and params["streamStdin"] is False
    assert params["streamStdoutStderr"] is True
    assert params["env"]["HOME"] == setup.scratch.home
    assert params["env"]["TMPDIR"] == setup.scratch.tmp
    assert params["env"]["XDG_CACHE_HOME"] == setup.scratch.cache
    assert params["env"]["CODEX_HOME"] is None
    child = next(event for event in events if event["kind"] == "command")
    assert "OPENAI_API_KEY" not in child["env"] and "CODEX_HOME" not in child["env"]
    configuration = next(event for event in events if event["kind"] == "configuration")
    assert configuration["home"] != setup.scratch.home
    actual = thaw_json(result.observations)
    assert actual["policy_audit"]["requirements"] is None
    assert actual["policy_audit"]["config"] == configuration["config"]
    wire = (runtime.log_dir / "wire.jsonl").read_text()
    assert "synthetic-secret" not in wire
    with pytest.raises(FrozenInstanceError):
        result.sequence = 2
    with pytest.raises(ValueError, match="single use"):
        asyncio.run(runtime.run(setup.command, sequence=1))


@pytest.mark.parametrize(
    "code,mapped", [(0, 0), (7, 7), (200, 200), (124, None), (-1, None), (256, None)]
)
def test_task_test_runtime_keeps_reported_exit_distinct(runtime_setup, code, mapped):
    configure(runtime_setup, report_code=code)
    runtime = adapter(runtime_setup)
    result = asyncio.run(runtime.run(runtime_setup.command, sequence=1))
    assert result.reported_exit_code == code and result.exit_code == mapped
    assert thaw_json(result.response) == {"exitCode": code, "stdout": "", "stderr": ""}
    assert bool(result.problems) is (mapped is None)
    assert not result.timeout_observed
    assert all(problem.failure_category is None for problem in result.problems)
    settled(result)


@pytest.mark.parametrize("mode", ["wrong-version", "bad-config", "requirements"])
def test_task_test_runtime_preflight_never_invents_command(runtime_setup, mode):
    configure(runtime_setup, mode=mode)
    result = asyncio.run(adapter(runtime_setup).run(runtime_setup.command, sequence=1))
    assert result.problems and not result.request_attempted
    assert result.sequence == 1 and result.response is None
    assert result.reported_exit_code is result.exit_code is None
    assert not any(
        event.get("message", {}).get("method") == "command/exec"
        for event in captured(runtime_setup)
    )
    settled(result)


@pytest.mark.parametrize(
    "mode", ["bad-base64", "wrong-process", "cap", "duplicate-final", "malformed"]
)
def test_task_test_runtime_protocol_stop_is_not_caller_cancel(runtime_setup, mode):
    configure(runtime_setup, mode=mode)
    result = asyncio.run(adapter(runtime_setup).run(runtime_setup.command, sequence=1))
    assert result.problems and not result.cancel_accepted
    assert not result.timeout_observed
    settled(result)


def test_task_test_runtime_actual_host_deadline_spans_version(runtime_setup):
    configure(runtime_setup, version_delay=1)
    runtime = adapter(runtime_setup, policy=replace(runtime_setup.policy, timeout=0.15))
    result = asyncio.run(runtime.run(runtime_setup.command, sequence=1))
    assert result.timeout_observed
    assert any(
        problem.failure_category == FailureCategory.TIMEOUT
        for problem in result.problems
    )
    assert not result.request_attempted and result.exit_code is None
    settled(result)


def test_task_test_runtime_missing_final_never_settles_request(runtime_setup):
    configure(runtime_setup, mode="missing-final")
    runtime = adapter(runtime_setup, policy=replace(runtime_setup.policy, timeout=0.7))
    result = asyncio.run(runtime.run(runtime_setup.command, sequence=1))
    assert result.request_attempted and not result.request_settled
    assert result.response is result.exit_code is None and result.timeout_observed
    settled(result)


async def wait_requested(runtime):
    async with asyncio.timeout(5):
        while not runtime.snapshot().request_attempted:
            await asyncio.sleep(0.005)


def test_task_test_runtime_accepted_cancel_retains_late_zero(runtime_setup):
    configure(runtime_setup, mode="cancel-late-zero")
    callbacks = []
    runtime = adapter(
        runtime_setup, on_cancel_accepted=lambda: callbacks.append("accepted")
    )

    async def run():
        pending = asyncio.create_task(runtime.run(runtime_setup.command, sequence=1))
        await wait_requested(runtime)
        assert await runtime.cancel()
        assert not await runtime.cancel()
        return await pending

    result = asyncio.run(run())
    assert callbacks == ["accepted"] and result.cancel_accepted
    assert result.reported_exit_code == result.exit_code == 0
    assert result.request_settled and not result.timeout_observed
    settled(result)


def test_task_test_runtime_caller_cancel_drains_before_reraising(runtime_setup):
    configure(runtime_setup, mode="cancel-late-zero")
    runtime = adapter(runtime_setup)

    async def run():
        pending = asyncio.create_task(runtime.run(runtime_setup.command, sequence=1))
        await wait_requested(runtime)
        pending.cancel("original caller cancellation")
        with pytest.raises(
            asyncio.CancelledError, match="original caller cancellation"
        ):
            await pending

    asyncio.run(run())
    result = runtime.snapshot()
    assert result.cancel_accepted and result.reported_exit_code == 0
    settled(result)


def test_task_test_runtime_output_write_failure_preserves_exception_and_drains(
    runtime_setup, monkeypatch
):
    problem = OSError("synthetic write failure")
    original = recording.RawOutputLog.write

    def fail_stdout(self, data):
        if self.path.name == "stdout.bin":
            raise problem
        return original(self, data)

    monkeypatch.setattr(recording.RawOutputLog, "write", fail_stdout)
    runtime = adapter(runtime_setup)
    with pytest.raises(OSError) as caught:
        asyncio.run(runtime.run(runtime_setup.command, sequence=1))
    assert caught.value is problem
    assert runtime.snapshot().problems
    settled(runtime.snapshot())


def validate(name, value):
    Draft7Validator(json.loads((SCHEMAS / name).read_text())).validate(value)


def test_task_test_fake_wire_matches_checked_in_protocol(runtime_setup):
    setup = runtime_setup
    asyncio.run(adapter(setup).run(setup.command, sequence=1))
    configure(setup, mode="cancel-late-zero")
    cancelled = adapter(setup, log_dir=setup.root / "logs/command-2")

    async def cancel_run():
        pending = asyncio.create_task(cancelled.run(setup.command, sequence=1))
        await wait_requested(cancelled)
        assert await cancelled.cancel()
        return await pending

    asyncio.run(cancel_run())
    requests = {}
    validated = set()
    for row in captured(setup):
        message = row.get("message")
        if row["kind"] == "request":
            validate(
                "ClientRequest.json" if "id" in message else "ClientNotification.json",
                message,
            )
            if "id" in message:
                requests[message["id"]] = message["method"]
            if message["method"] == "command/exec":
                validate("v2/CommandExecParams.json", message["params"])
                validated.add("params")
            if message["method"] == "command/exec/terminate":
                validate("v2/CommandExecTerminateParams.json", message["params"])
                validated.add("terminate")
        elif (
            row["kind"] == "response" and requests.get(message["id"]) == "command/exec"
        ):
            validate("v2/CommandExecResponse.json", message["result"])
            validated.add("response")
        elif (
            row["kind"] == "response"
            and requests.get(message["id"]) == "command/exec/terminate"
        ):
            validate("v2/CommandExecTerminateResponse.json", message["result"])
            validated.add("terminate-response")
        elif row["kind"] == "notification":
            validate("ServerNotification.json", message)
            validate("v2/CommandExecOutputDeltaNotification.json", message["params"])
            validated.add("output")
    assert validated == {
        "params",
        "terminate",
        "response",
        "terminate-response",
        "output",
    }


@pytest.mark.parametrize("replacement", [float("nan"), float("inf"), True, "bad"])
def test_task_test_runtime_constructor_rejects_deadline_without_io(
    runtime_setup, replacement
):
    with pytest.raises(ValueError, match="deadline"):
        adapter(runtime_setup, deadline_at=replacement)
    assert not (runtime_setup.root / "logs/command-1").exists()


def test_task_test_runtime_auth_factory_isolated_and_redacted(runtime_setup):
    setup = runtime_setup
    ledger = OwnershipLedger()
    wire = recording.TestWireRecorder(
        setup.root / "factory.jsonl",
        Redactor(),
        limit=65536,
        acquisition=ledger.acquire("log"),
    )
    owned = isolation.create_test_isolation(
        setup.checkout,
        setup.scratch,
        setup.policy,
        wire,
        log_dir=setup.root / "logs",
        ownership=ledger,
    )
    try:
        assert owned.environment["HOME"] != setup.scratch.home
        assert owned.environment["CODEX_HOME"] != str(setup.auth.parent)
        assert "OPENAI_API_KEY" not in owned.environment
        auth = json.loads((owned.codex_home / "auth.json").read_text())
        assert auth["tokens"]["access_token"] == "synthetic-secret-access_token"
        profile = owned.config["permissions"][isolation.PROFILE]
        assert profile["network"]["enabled"] is False
        assert profile["filesystem"][str(owned.root)] == "deny"
        assert profile["filesystem"][setup.checkout.git_directory] == "read"
        wire.emit("diagnostic", text="synthetic-secret-access_token")
    finally:
        owned.cleanup()
        wire.close()
    assert "synthetic-secret" not in (setup.root / "factory.jsonl").read_text()


@pytest.mark.parametrize("kind", ["limit", "append", "replace", "symlink"])
def test_task_test_runtime_binary_logs_sticky_failure(runtime_setup, kind):
    ledger = OwnershipLedger()
    path = runtime_setup.root / "binary.bin"
    output = recording.RawOutputLog(path, 4, ledger.acquire("log"))
    output.write(b"\x00\xff")
    if kind == "limit":
        with pytest.raises(recording.OutputLimitExceeded):
            output.write(b"12345")
    output.close()
    if kind == "append":
        path.chmod(0o600)
        with path.open("ab") as stream:
            stream.write(b"x")
        path.chmod(0o400)
    elif kind in ("replace", "symlink"):
        path.unlink()
        if kind == "replace":
            path.write_bytes(b"\x00\xff")
            path.chmod(0o400)
        else:
            path.symlink_to(runtime_setup.auth)
    record = output.ownership_record("log")
    assert record.closed and not record.healthy and not record.sealed
    assert record.byte_count is record.sha256 is None


@pytest.mark.parametrize("mode", ["oversized-line", "message-flood", "invalid-utf8"])
def test_task_test_runtime_bounds_transport_before_queue_and_record(
    runtime_setup, mode
):
    configure(runtime_setup, mode=mode)
    runtime = adapter(runtime_setup)
    result = asyncio.run(runtime.run(runtime_setup.command, sequence=1))
    assert result.problems and result.response is None
    assert not result.request_settled and not result.cancel_accepted
    assert not result.timeout_observed
    settled(result)
    assert (runtime.log_dir / "wire.jsonl").stat().st_size < 4 * 1024 * 1024


def test_task_test_runtime_rpc_timeout_follows_shared_host_deadline(runtime_setup):
    runtime = adapter(runtime_setup)

    async def run():
        deadline = asyncio.get_running_loop().time() + 3
        runtime.deadline_at = deadline
        result = await runtime.run(runtime_setup.command, sequence=1)
        return deadline, result

    deadline, result = asyncio.run(run())
    assert result.exit_code == 0
    params = next(
        row["message"]["params"]
        for row in captured(runtime_setup)
        if row["kind"] == "request" and row["message"]["method"] == "command/exec"
    )
    assert runtime.deadline_at == deadline
    assert 0 < params["timeoutMs"] <= 3301
    assert params["timeoutMs"] > 300


def test_task_test_runtime_auth_failure_keeps_owned_root_released(runtime_setup):
    runtime_setup.auth.write_text('{"tokens":{"access_token":"wrong-only"}}')
    runtime = adapter(runtime_setup)
    result = asyncio.run(runtime.run(runtime_setup.command, sequence=1))
    assert result.problems and not result.request_attempted
    assert not runtime_setup.capture.exists()
    assert len(result.receipt.isolations) == 1
    assert result.receipt.isolations[0].removed
    settled(result)


def test_task_test_runtime_final_receipt_error_preserves_first_host_failure(
    runtime_setup, monkeypatch
):
    first = OSError("first output failure")
    later = OSError("later receipt failure")
    original_write = recording.RawOutputLog.write
    runtime = adapter(runtime_setup)
    actual_receipt = runtime.ownership_receipt

    def write(output, data):
        if output.path.name == "stdout.bin":
            raise first
        return original_write(output, data)

    def fail_receipt():
        raise later

    monkeypatch.setattr(recording.RawOutputLog, "write", write)
    monkeypatch.setattr(runtime, "ownership_receipt", fail_receipt)
    with pytest.raises(OSError) as caught:
        asyncio.run(runtime.run(runtime_setup.command, sequence=1))
    assert caught.value is first
    assert any(
        "later receipt failure" in item.detail for item in runtime.snapshot().problems
    )
    assert actual_receipt().processes_stopped
    assert actual_receipt().resources_released
    assert not runtime.snapshot().receipt.settled


def test_task_test_runtime_repeated_caller_cancel_joins_same_callback(
    runtime_setup, monkeypatch
):
    configure(runtime_setup, mode="cancel-late-zero")
    callbacks = []
    runtime = adapter(
        runtime_setup, on_cancel_accepted=lambda: callbacks.append("accepted")
    )

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        original = runtime.cancel

        async def gated_cancel():
            accepted = await original()
            entered.set()
            await release.wait()
            return accepted

        monkeypatch.setattr(runtime, "cancel", gated_cancel)
        pending = asyncio.create_task(runtime.run(runtime_setup.command, sequence=1))
        await wait_requested(runtime)
        pending.cancel("first caller cancel")
        await entered.wait()
        pending.cancel("second caller cancel")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        still_owned = not pending.done()
        release.set()
        with pytest.raises(asyncio.CancelledError, match="first caller cancel"):
            await pending
        assert still_owned

    asyncio.run(run())
    assert callbacks == ["accepted"]
    assert runtime.snapshot().cancel_accepted
    settled(runtime.snapshot())


def test_task_test_runtime_cleanup_terminate_write_is_bounded(
    runtime_setup, monkeypatch
):
    from multi_agent.adapters.probe.process import Process

    runtime = adapter(runtime_setup, policy=replace(runtime_setup.policy, grace=0.1))
    original = Process.send

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        rescue_used = []

        async def blocked_send(process, message):
            if message.get("method") in ("command/exec", "command/exec/terminate"):
                entered.set()
                await release.wait()
                return True
            return await original(process, message)

        async def rescue():
            await asyncio.sleep(3)
            rescue_used.append(True)
            release.set()

        monkeypatch.setattr(Process, "send", blocked_send)
        pending = asyncio.create_task(runtime.run(runtime_setup.command, sequence=1))
        await entered.wait()
        rescuer = asyncio.create_task(rescue())
        try:
            assert await runtime.cancel()
            with pytest.raises(TimeoutError):
                await pending
        finally:
            release.set()
            rescuer.cancel()
            await asyncio.gather(rescuer, return_exceptions=True)
        assert not rescue_used

    asyncio.run(run())
    result = runtime.snapshot()
    assert result.cancel_accepted and not result.timeout_observed
    assert not result.request_settled and result.response is None
    settled(result)


@pytest.mark.parametrize("boundary", ["_consume", "_drain_tail"])
def test_task_test_runtime_cleanup_reader_and_tail_are_bounded(
    runtime_setup, monkeypatch, boundary
):
    runtime = adapter(runtime_setup, policy=replace(runtime_setup.policy, grace=0.1))

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        rescue_used = []

        async def blocked():
            entered.set()
            await release.wait()

        async def rescue():
            await asyncio.sleep(3)
            rescue_used.append(True)
            release.set()

        monkeypatch.setattr(runtime, boundary, blocked)
        pending = asyncio.create_task(runtime.run(runtime_setup.command, sequence=1))
        await entered.wait()
        rescuer = asyncio.create_task(rescue())
        try:
            if boundary == "_consume":
                assert await runtime.cancel()
            with pytest.raises(TimeoutError):
                await pending
        finally:
            release.set()
            rescuer.cancel()
            await asyncio.gather(rescuer, return_exceptions=True)
        assert not rescue_used

    asyncio.run(run())
    assert not runtime.snapshot().timeout_observed
    settled(runtime.snapshot())
