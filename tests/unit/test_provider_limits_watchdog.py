"""Exercise provider deadlines across real process boundaries with local fakes."""

import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
import test_runtime as fixtures

from multi_agent.store.provider_limits import (
    ProviderLedger,
    ProviderLimits,
    ProviderWait,
)

synthetic_codex_auth_and_fake_environment = (
    fixtures.synthetic_codex_auth_and_fake_environment
)

_CONTROLLER = """
import asyncio
import json
import sys
from pathlib import Path

from multi_agent.adapters.base import Permission, SessionConfig
from multi_agent.adapters.provider_limits import budget_for_provider
from multi_agent.adapters.runtime import RuntimeAdapter
from multi_agent.core.events import Message, thaw_json
from multi_agent.store.provider_limits import ProviderLedger, ProviderLimits

provider, executable, root, operation, marker = sys.argv[1:]
root, marker = Path(root), Path(marker)
ledger = ProviderLedger(root / "data")
limits = ProviderLimits(max_concurrent=1, max_turn_seconds=2.3)
budget_for_provider.set(lambda _: (ledger, limits))

async def run():
    adapter = RuntimeAdapter(provider, executable=executable, log_dir=root / operation,
                             timeout=20, grace=0.25, output_schema={"type": "object"})
    config = SessionConfig("model-fixture", "high", Permission.READ_ONLY, root / "input")
    session = await adapter.start_session(config)
    turn = await adapter.submit_turn(session, "deterministic offline review")
    async for event in adapter.events(turn):
        if isinstance(event, Message) and event.text == "waiting":
            state = adapter._turns[turn]
            value = {
                "pid": state.process.child.pid,
                "deadline": state.provider_turn.deadline,
            }
            stage = marker.with_suffix(".tmp")
            stage.write_text(json.dumps(value))
            stage.replace(marker)
    result = await adapter.result(turn)
    receipt = adapter.ownership_receipt()
    print(json.dumps({
        "result": thaw_json(result.terminal.result),
        "reported_model": result.terminal.reported_model,
        "exit_code": result.process_exit.exit_code,
        "processes_stopped": receipt.processes_stopped,
        "logs_sealed": receipt.logs_sealed,
    }))

asyncio.run(run())
"""


def _executable(tmp_path: Path, provider: str, *, waiting: bool) -> str:
    directory = tmp_path / ("waiting" if waiting else "success")
    directory.mkdir()
    executable = Path(fixtures.fake(directory, provider))
    shebang, body = executable.read_text().split("\n", 1)
    fake = (
        "import os, signal, time\n"
        "from pathlib import Path\n"
        f"os.environ['FAKE_RUNTIME_MODE'] = {'wait' if waiting else 'success'!r}\n"
        "def wait_ignoring_term():\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"    heartbeat = Path({str(tmp_path / 'heartbeat')!r})\n"
        "    while True:\n"
        "        heartbeat.touch()\n"
        "        time.sleep(0.02)\n"
    )
    if waiting:
        # Reach the provider-specific model path first, then stay alive even
        # after controller stdin closes and a graceful group stop is sent.
        point = (
            '            continue\n        if mode == "late-contradictory":'
            if provider == "codex"
            else "        time.sleep(60)\n    result = {"
        )
        assert point in body
        replacement = (
            "            wait_ignoring_term()\n" + point
            if provider == "codex"
            else "        wait_ignoring_term()\n    result = {"
        )
        body = body.replace(point, replacement, 1)
    executable.write_text(shebang + "\n" + fake + body)
    return str(executable)


def _group_running(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # A macOS zombie-only group can still await reaping.
        return True
    return True


def _wait_for(predicate, deadline: float, detail: str) -> None:
    while not predicate():
        assert time.monotonic() < deadline, detail
        time.sleep(0.01)


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_provider_limits_watchdog_stops_orphaned_model_before_admitting_again(
    tmp_path, provider
):
    root = tmp_path / "private"
    root.mkdir()
    (root / "input").mkdir()
    (root / "data").mkdir(mode=0o700)
    env = {**os.environ, "TMPDIR": str(root)}
    marker = root / "ready.json"
    heartbeat = root / "heartbeat"
    ledger = ProviderLedger(root / "data")
    limits = ProviderLimits(max_concurrent=1, max_turn_seconds=2.3)

    def launch(operation, executable):
        return subprocess.Popen(
            [
                sys.executable,
                "-c",
                _CONTROLLER,
                provider,
                executable,
                str(root),
                operation,
                str(marker),
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    group = None
    with launch("first", _executable(root, provider, waiting=True)) as controller:
        try:
            _wait_for(
                lambda: (
                    controller.poll() is not None
                    or (marker.exists() and heartbeat.exists())
                ),
                time.monotonic() + 6,
                "fake model did not reach its protocol turn",
            )
            if controller.poll() is not None:
                stdout, stderr = controller.communicate(timeout=3)
                pytest.fail(
                    f"controller exited before the fake model began: {stdout}{stderr}"
                )
            actual = json.loads(marker.read_text())
            group = actual["pid"]
            assert os.getpgid(group) == group and group != os.getpgrp()
            assert actual["deadline"] > time.monotonic()
            stamp = heartbeat.stat().st_mtime_ns
            controller.kill()
            controller.communicate(timeout=3)
            assert controller.returncode == -signal.SIGKILL
            _wait_for(
                lambda: heartbeat.stat().st_mtime_ns > stamp,
                actual["deadline"],
                "model did not survive loss of the controller within the allowance",
            )
            assert _group_running(group)
            waiting = ledger.reserve(
                provider, limits, datetime.now(UTC), 10, os.getpid()
            )
            assert waiting == ProviderWait("max_concurrent")
            _wait_for(
                lambda: not _group_running(group),
                actual["deadline"] + 1,
                "model group outlived its provider deadline and stop grace",
            )
        finally:
            if controller.poll() is None:
                controller.kill()
                controller.communicate(timeout=3)
            if group is not None and _group_running(group):
                os.killpg(group, signal.SIGKILL)

    with launch("second", _executable(root, provider, waiting=False)) as replacement:
        stdout, stderr = replacement.communicate(timeout=10)
        assert replacement.returncode == 0, stderr
    result = json.loads(stdout)
    assert result == {
        "result": {"summary": "ok", "findings": []},
        "reported_model": "actual-claude" if provider == "claude" else None,
        "exit_code": 0,
        "processes_stopped": True,
        "logs_sealed": True,
    }
