"""A real provider subprocess can finish cleanly after the model deadline."""

import asyncio
import sys
from pathlib import Path

import pytest
import test_runtime as fixtures

from multi_agent.adapters.base import Permission, SessionConfig
from multi_agent.adapters.provider_limits import budget_for_provider
from multi_agent.adapters.runtime import RuntimeAdapter
from multi_agent.core.events import Completed, Error, FailureCategory, Message
from multi_agent.store.provider_limits import ProviderLedger, ProviderLimits

synthetic_codex_auth_and_fake_environment = (
    fixtures.synthetic_codex_auth_and_fake_environment
)

_FAKE = """
import json
import os
import signal
import sys
import time
from pathlib import Path

assert sys.argv[1] == "-p"

def emit(value):
    os.write(1, (json.dumps(value) + "\\n").encode())

def leave():
    exit_at = float(Path("exit-at").read_text())
    time.sleep(max(0, exit_at - time.monotonic()))
    sys.exit(0)

def cancel(*_):
    emit({"type": "result", "is_error": True, "subtype": "error_during_execution",
          "terminal_reason": "aborted_streaming"})
    leave()

signal.signal(signal.SIGINT, cancel)
emit({"type": "system", "subtype": "init", "session_id": "fake-cleanup"})
emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "ready"}]}})
while not Path("finish").exists():
    time.sleep(0.005)
emit({"type": "result", "is_error": False, "subtype": "success",
      "terminal_reason": "completed", "structured_output": {"review": "accepted"},
      "modelUsage": {"fixture-claude": {"canonicalModel": "fixture-claude"}}})
leave()
"""


@pytest.mark.parametrize("cancel_turn", [False, True], ids=["terminal", "cancel"])
def test_provider_limits_keep_normal_cleanup_after_model_deadline(
    tmp_path: Path, cancel_turn: bool
):
    workspace = tmp_path / "input"
    workspace.mkdir()
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    program = tmp_path / "claude-fake"
    program.write_text(f"#!{sys.executable}\n" + _FAKE)
    program.chmod(0o700)
    config = SessionConfig("fixture", "high", Permission.READ_ONLY, workspace)
    adapter = RuntimeAdapter(
        "claude",
        executable=str(program),
        log_dir=tmp_path / "logs",
        timeout=10,
        grace=1.2,
        output_schema={"type": "object"},
    )

    async def run():
        loop = asyncio.get_running_loop()
        ledger = ProviderLedger(data)
        limits = ProviderLimits(max_turn_seconds=2.0)
        token = budget_for_provider.set(lambda _: (ledger, limits))
        try:
            turn = await adapter.submit_turn(
                await adapter.start_session(config), "exercise bounded cleanup"
            )
            stream = adapter.events(turn)
            async with asyncio.timeout(8):
                async for event in stream:
                    if isinstance(event, Message) and event.text == "ready":
                        break
                else:
                    pytest.fail("the fake provider did not reach its model session")
                state = adapter._turns[turn]
                assert state.provider_turn is not None
                deadline = state.provider_turn.deadline
                assert loop.time() < deadline - 0.4
                (workspace / "exit-at").write_text(str(deadline + 0.2))
                await asyncio.sleep(max(0, deadline - loop.time() - 0.4))
                if cancel_turn:
                    assert await adapter.cancel(turn)
                else:
                    (workspace / "finish").touch()
                events = [event async for event in stream]
                result = await adapter.result(turn)
            if cancel_turn:
                assert isinstance(result.terminal, Error)
                assert result.terminal.failure_category == FailureCategory.CANCELLED
            else:
                assert isinstance(result.terminal, Completed)
                assert result.terminal.reported_model == "fixture-claude"
            assert result.terminal in events
            assert state.provider_turn.terminal_at is not None
            assert state.provider_turn.terminal_at < deadline < loop.time()
            assert (
                result.process_exit is not None and result.process_exit.exit_code == 0
            )
            assert adapter.ownership_receipt().processes_stopped
        finally:
            budget_for_provider.reset(token)

    asyncio.run(run())
