"""Informational messages cross the real fake transport without granting trust."""

import asyncio
import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
import test_task_test_runtime as helpers
from jsonschema import Draft7Validator

from multi_agent.adapters import test_probe as probe
from multi_agent.core.events import FailureCategory

runtime_setup = helpers.runtime_setup
ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "protocol/codex/schema"


def warning(**changes):
    return {
        "method": "configWarning",
        "params": {"summary": "Synthetic warning"},
        **changes,
    }


def remote(**changes):
    return {
        "method": "remoteControl/status/changed",
        "params": {
            "installationId": "synthetic-installation",
            "serverName": "synthetic-server",
            "status": "disabled",
            **changes,
        },
    }


def notices(phase):
    first = warning()
    first["params"]["summary"] = f"Synthetic {phase} warning"
    second = remote()
    if phase == "command":
        first["params"].update(
            details="Synthetic details",
            path="synthetic-config.toml",
            range={
                "start": {"line": 0, "column": 0},
                "end": {"line": 2, "column": 3},
            },
            futureParameter={"enabled": True},
        )
        second["params"]["environmentId"] = "synthetic-environment"
        for message in (first, second):
            message["emittedAtMs"] = 2**63 - 1
            message["futureEnvelopeField"] = ["preserved", 7]
    elif phase == "after-final":
        first["params"].update(details=None, path=None, range=None)
        second["params"]["environmentId"] = None
        for message in (first, second):
            message["emittedAtMs"] = -(2**63)
    return [first, second]


def wire_rows(runtime):
    return [
        json.loads(line)
        for line in (runtime.log_dir / "wire.jsonl").read_text().splitlines()
    ]


def redacted_notice(message):
    expected = deepcopy(message)
    if expected["method"] == "remoteControl/status/changed":
        for name in ("installationId", "serverName"):
            if isinstance(expected["params"].get(name), str):
                expected["params"][name] = "<REDACTED>"
    return expected


def assert_notices_recorded(runtime, messages):
    rows = wire_rows(runtime)
    found = []
    for message in messages:
        expected = redacted_notice(message)
        matching = [
            row
            for row in rows
            if row["kind"] == "recv" and row.get("message") == expected
        ]
        assert len(matching) == 1
        found.append(matching[0]["seq"])
    return rows, found


def command_requests(setup):
    return [
        row["message"]
        for row in helpers.captured(setup)
        if row["kind"] == "request" and row["message"]["method"] == "command/exec"
    ]


@pytest.mark.parametrize("phase", ["preflight", "command", "after-final"])
def test_task_test_notifications_accept_across_phases(runtime_setup, phase):
    messages = notices(phase)
    validator = Draft7Validator(
        json.loads((SCHEMAS / "ServerNotification.json").read_text())
    )
    for message in messages:
        validator.validate(message)
    helpers.configure(runtime_setup, notifications={phase: messages})
    runtime = helpers.adapter(runtime_setup)

    result = asyncio.run(runtime.run(runtime_setup.command, sequence=1))

    assert not result.problems and not result.timeout_observed
    assert result.request_attempted and result.request_settled
    assert result.exit_code == result.reported_exit_code == 0
    assert result.stdout_received_bytes == 5 and result.stderr_received_bytes == 4
    assert (runtime.log_dir / "stdout.bin").read_bytes() == b"out\x00\xff"
    assert (runtime.log_dir / "stderr.bin").read_bytes() == b"err\xfe"
    assert result.receipt.logs_sealed
    helpers.settled(result)
    requests = command_requests(runtime_setup)
    assert len(requests) == 1
    assert requests[0]["params"]["command"] == list(runtime_setup.command.argv)
    rows, positions = assert_notices_recorded(runtime, messages)
    sent = next(
        row["seq"]
        for row in rows
        if row["kind"] == "send"
        and row.get("message", {}).get("method") == "command/exec"
    )
    final = next(
        row["seq"]
        for row in rows
        if row["kind"] == "recv"
        and row.get("message", {}).get("id") == requests[0]["id"]
        and "result" in row["message"]
    )
    assert positions[0] < positions[1]
    if phase == "preflight":
        assert max(positions) < sent
    elif phase == "command":
        assert sent < min(positions) < max(positions) < final
    else:
        assert final < min(positions)


INVALID = [
    pytest.param(warning(params={}), id="missing-summary"),
    pytest.param(
        warning(params={"summary": "x", "details": 1}), id="invalid-optional-details"
    ),
    pytest.param(
        warning(
            params={
                "summary": "x",
                "range": {
                    "start": {"line": True, "column": 0},
                    "end": {"line": 0, "column": 0},
                },
            }
        ),
        id="range-bool-is-not-uint",
    ),
    pytest.param(warning(emittedAtMs=True), id="timestamp-bool"),
    pytest.param(warning(emittedAtMs=1.5), id="timestamp-noninteger"),
    pytest.param(warning(emittedAtMs=2**63), id="timestamp-int64-overflow"),
    pytest.param(warning(emittedAtMs=-(2**63) - 1), id="timestamp-int64-underflow"),
    pytest.param(warning(id=73), id="server-request"),
    pytest.param(warning(result={}), id="notification-response-conflict"),
    pytest.param(warning(error={"code": -1}), id="notification-error-conflict"),
    pytest.param({"method": "future/unknown", "params": {}}, id="unknown"),
    pytest.param(
        {
            "method": "mcpServer/startupStatus/updated",
            "params": {"name": "synthetic-mcp", "status": "starting"},
        },
        id="mcp",
    ),
    pytest.param(remote(status="connected"), id="remote-enabled"),
    pytest.param(remote(installationId=None), id="remote-invalid-identity"),
]


@pytest.mark.parametrize("message", INVALID)
def test_task_test_notifications_reject_invalid(runtime_setup, message):
    helpers.configure(runtime_setup, notifications={"preflight": [message]})
    runtime = helpers.adapter(runtime_setup)

    result = asyncio.run(runtime.run(runtime_setup.command, sequence=1))

    assert result.problems and not result.timeout_observed
    assert not result.request_attempted and not result.request_settled
    assert result.response is result.exit_code is result.reported_exit_code is None
    assert not command_requests(runtime_setup)
    if "result" in message or "error" in message:
        assert (
            sum(
                row["kind"] == "notification" and row.get("message") == message
                for row in helpers.captured(runtime_setup)
            )
            == 1
        )
        # Response-like envelopes during config/read retain only an audit
        # projection; the fake capture independently proves the sent payload.
        audit = {
            "raw_payload_omitted": True,
            "has_result": "result" in message,
            "has_error": "error" in message,
            "result_type": "object" if "result" in message else "missing",
            "error_type": "object" if "error" in message else "missing",
        }
        if "result" in message:
            audit.update(
                config_type="missing", layers_type="missing", origins_type="missing"
            )
        else:
            audit["error_code"] = -1
        assert (
            sum(
                row["kind"] == "recv"
                and row.get("message") == {"id": None, "config_read_audit": audit}
                for row in wire_rows(runtime)
            )
            == 1
        )
    else:
        assert any(
            row["kind"] == "recv" and row.get("message") == redacted_notice(message)
            for row in wire_rows(runtime)
        )
    helpers.settled(result)


@pytest.mark.parametrize("mode", ["bad-config", "requirements"])
def test_task_test_notifications_preserve_configuration_audit(runtime_setup, mode):
    messages = notices("preflight")
    helpers.configure(runtime_setup, mode=mode, notifications={"preflight": messages})
    runtime = helpers.adapter(runtime_setup)

    result = asyncio.run(runtime.run(runtime_setup.command, sequence=1))

    assert result.problems and not result.request_attempted
    assert not result.observations and result.response is None
    assert not command_requests(runtime_setup)
    assert_notices_recorded(runtime, messages)
    methods = [
        row["message"]["method"]
        for row in helpers.captured(runtime_setup)
        if row["kind"] == "request"
    ]
    assert methods == [
        "initialize",
        "initialized",
        "config/read",
        "configRequirements/read",
    ]
    helpers.settled(result)


def test_task_test_notifications_preserve_deadline(runtime_setup, monkeypatch):
    command_messages = notices("command")
    helpers.configure(
        runtime_setup,
        mode="wait",
        notifications={"preflight": notices("preflight"), "command": command_messages},
    )
    runtime = helpers.adapter(
        runtime_setup, policy=replace(runtime_setup.policy, timeout=10)
    )
    original = runtime._consume_message

    async def run():
        entered = asyncio.Event()
        seen_deadlines = []
        rescue_used = []
        deadline = asyncio.get_running_loop().time() + 6
        runtime.deadline_at = deadline

        def observe(message):
            outcome = original(message)
            if message in command_messages:
                seen_deadlines.append(runtime._deadline)
                if message == command_messages[-1]:
                    entered.set()
            return outcome

        async def rescue():
            await asyncio.sleep(8)
            rescue_used.append(True)
            await runtime.cancel()

        monkeypatch.setattr(runtime, "_consume_message", observe)
        pending = asyncio.create_task(runtime.run(runtime_setup.command, sequence=1))
        reached = asyncio.create_task(entered.wait())
        rescuer = asyncio.create_task(rescue())
        try:
            await asyncio.wait({pending, reached}, return_when=asyncio.FIRST_COMPLETED)
            assert entered.is_set(), "command notifications were never accepted"
            assert seen_deadlines == [deadline, deadline]
            result = await pending
            assert not rescue_used
            assert result.timeout_observed
            assert any(
                p.failure_category == FailureCategory.TIMEOUT for p in result.problems
            )
            assert result.request_attempted and not result.cancel_accepted
            assert runtime.deadline_at == deadline
            return result
        finally:
            if not pending.done():
                await runtime.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            reached.cancel()
            rescuer.cancel()
            await asyncio.gather(reached, rescuer, return_exceptions=True)

    result = asyncio.run(run())
    assert_notices_recorded(runtime, command_messages)
    assert len(command_requests(runtime_setup)) == 1
    helpers.settled(result)


def test_task_test_notifications_bind_notification_schemas(tmp_path):
    path = probe.prepare_plan(tmp_path / "unexecuted-plan")
    plan = json.loads(path.read_text())
    expected = (
        "protocol/codex/schema/ServerNotification.json",
        "protocol/codex/schema/v2/ConfigWarningNotification.json",
        "protocol/codex/schema/v2/RemoteControlStatusChangedNotification.json",
    )
    for relative in expected:
        assert (
            plan["source_hashes"].get(relative)
            == hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
        )
    assert not (path.parent / "native-test-boundary").exists()
