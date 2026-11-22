"""Shared deadlines use real fake CLIs and deterministic host-side gates."""

import asyncio
import json
import math
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
import test_runtime as helpers

from multi_agent.adapters.codex_isolation import CodexIsolation
from multi_agent.adapters.probe.process import Process
from multi_agent.adapters.probe.recording import Recorder, Redactor
from multi_agent.adapters.provider_limits import budget_for_provider
from multi_agent.adapters.runtime import RuntimeAdapter, RuntimeFailure, _Rpc
from multi_agent.core.events import Completed, Error, FailureCategory, Message
from multi_agent.store.provider_limits import (
    ProviderLedger,
    ProviderLimits,
    ProviderReservation,
)

# Imported fixtures must be present in this module's namespace for pytest to
# register autouse behavior; importing only the helpers module is insufficient.
synthetic_codex_auth_and_fake_environment = (
    helpers.synthetic_codex_auth_and_fake_environment
)


@pytest.fixture(autouse=True)
def only_synthetic_processes(tmp_path, monkeypatch):
    original = asyncio.create_subprocess_exec

    async def launch(program, *arguments, **options):
        fake_program = program
        if program == sys.executable:
            assert arguments[:2] == ("-I", "-m")
            match arguments[2]:
                case "multi_agent.adapters.provider_watchdog":
                    fake_program = arguments[5]
                    assert options["pass_fds"] == (int(arguments[3]),)
                case "multi_agent.adapters.provider_limits":
                    fake_program = arguments[8]
                    assert options["pass_fds"] == (int(arguments[7]),)
                case _:
                    pytest.fail("deadline test launched an unexpected Python module")
            assert Path(fake_program).is_relative_to(tmp_path)
        assert Path(fake_program).name in {"codex-fake", "claude-fake"}
        auth_home = Path(os.environ["CODEX_HOME"])
        assert auth_home.is_relative_to(tmp_path)
        assert (
            json.loads((auth_home / "auth.json").read_text())["tokens"]["access_token"]
            == "synthetic-access_token"
        )
        return await original(program, *arguments, **options)

    async def no_shell(*args, **kwargs):
        pytest.fail("deadline test launched a shell")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", no_shell)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, True, False, "later"])
def test_shared_deadline_rejects_invalid_constructor_values(tmp_path, value):
    logs = tmp_path / "logs"
    with pytest.raises(ValueError):
        RuntimeAdapter("claude", log_dir=logs, deadline_at=value)
    assert not logs.exists()


class DeadlineClock:
    """Keep D fixed, advancing the test loop only after actual transport readiness."""

    def __init__(self, patch):
        self.loop = asyncio.get_running_loop()
        self.original_time = self.loop.time
        self.offset = 0.0
        self.deadline = self.original_time() + 30
        patch.setattr(self.loop, "time", self.now)

    def now(self):
        return self.original_time() + self.offset

    async def cross(self):
        crossed = asyncio.Event()
        self.loop.call_at(self.deadline + 0.001, crossed.set)
        self.cross_without_yield()
        # This guard is created after the forward clock jump.
        async with asyncio.timeout(5):
            await crossed.wait()

    def cross_without_yield(self):
        self.offset = max(self.offset, self.deadline + 0.01 - self.original_time())
        self.loop.call_soon(lambda: None)


def records(capture):
    return (
        [json.loads(line) for line in capture.read_text().splitlines()]
        if capture.exists()
        else []
    )


def argv_for(capture, process):
    return next(
        row["argv"]
        for row in records(capture)
        if row["kind"] == "spawn" and row["pid"] == process.child.pid
    )


async def ready_turn(adapter, config):
    turn = await adapter.submit_turn(await adapter.start_session(config), "wait")
    stream = adapter.events(turn)
    observed = []
    async with asyncio.timeout(10):
        async for event in stream:
            observed.append(event)
            if isinstance(event, Message) and event.text == "waiting":
                break
        else:
            pytest.fail("fake model did not reach its actual waiting response")
    state = adapter._turns[turn]
    assert state.process is not None and state.process.child.returncode is None
    return turn, stream, observed


async def completed_stream(adapter, turn, stream):
    async with asyncio.timeout(5):
        events = [event async for event in stream]
        result = await adapter.result(turn)
    return events, result


@pytest.mark.parametrize(
    ("maximum", "reported_limit"), [(10, "max_turn_seconds"), (60, None)]
)
def test_provider_turn_uses_the_earlier_deadline_and_releases_the_slot(
    tmp_path, monkeypatch, maximum, reported_limit
):
    async def run():
        clock = DeadlineClock(monkeypatch)
        ledger = ProviderLedger(tmp_path)
        limits = ProviderLimits(
            max_concurrent=1, max_calls_per_day=2, max_turn_seconds=maximum
        )
        scope = budget_for_provider.set(lambda _: (ledger, limits))
        adapter, config, _ = helpers.setup(
            tmp_path, monkeypatch, "codex", mode="wait", deadline_at=clock.deadline
        )
        try:
            turn, stream, _ = await ready_turn(adapter, config)
            state = adapter._turns[turn]
            assert state.provider_turn is not None
            assert state.provider_turn.limit_field == reported_limit
            if reported_limit is not None:
                assert state.provider_turn.deadline < clock.deadline
                clock.deadline = state.provider_turn.deadline
            assert state.deadline.when() == clock.deadline
            await clock.cross()
            _, result = await completed_stream(adapter, turn, stream)
            assert isinstance(result.terminal, Error)
            assert result.terminal.failure_category == FailureCategory.TIMEOUT
            if reported_limit is None:
                assert result.terminal.detail == "runtime deadline exceeded"
            else:
                assert result.terminal.raw_type == "adapter.provider_limits"
                assert reported_limit in result.terminal.detail
            assert_released(adapter)
            next_slot = ledger.reserve(
                "codex", limits, datetime.now(UTC), 20, os.getpid()
            )
            assert isinstance(next_slot, ProviderReservation)
            ledger.finish(next_slot.reservation_id, 0, True)
            next_slot.close()
        finally:
            budget_for_provider.reset(scope)
            await clean_owned(adapter, tmp_path)

    asyncio.run(run())


def test_provider_turn_expiring_at_launch_fence_spends_no_call(tmp_path, monkeypatch):
    async def run():
        clock = DeadlineClock(monkeypatch)
        ledger = ProviderLedger(tmp_path)
        limits = ProviderLimits(max_calls_per_day=1, max_turn_seconds=10)
        scope = budget_for_provider.set(lambda _: (ledger, limits))
        original_start = ledger.start
        launch_fenced = asyncio.Event()

        def start_after_deadline(identifier, group):
            started = original_start(identifier, group)
            clock.cross_without_yield()
            launch_fenced.set()
            return started

        monkeypatch.setattr(ledger, "start", start_after_deadline)
        adapter, config, capture = helpers.setup(
            tmp_path, monkeypatch, "codex", deadline_at=clock.deadline
        )
        try:
            turn = await adapter.submit_turn(
                await adapter.start_session(config), "offline"
            )
            async with asyncio.timeout_at(clock.deadline + 5):
                await launch_fenced.wait()
            _, result = await completed_stream(adapter, turn, adapter.events(turn))
            assert isinstance(result.terminal, Error)
            assert result.terminal.failure_category == FailureCategory.TIMEOUT
            assert result.terminal.raw_type == "adapter.provider_limits"
            sent_methods = {
                row["message"].get("method")
                for row in records(capture)
                if row["kind"] == "request"
            }
            assert "thread/start" in sent_methods and "turn/start" not in sent_methods
            assert_released(adapter)
            next_slot = ledger.reserve(
                "codex", limits, datetime.now(UTC), 20, os.getpid()
            )
            assert isinstance(next_slot, ProviderReservation)
            ledger.finish(next_slot.reservation_id, 0, True)
            next_slot.close()
        finally:
            budget_for_provider.reset(scope)
            await clean_owned(adapter, tmp_path)

    asyncio.run(run())


async def clean_owned(adapter, tmp_path):
    """Failure cleanup never contributes to assertions about product receipts."""
    for state in adapter._turns.values():
        if state.task is not None and not state.task.done():
            state.task.cancel()
            done, _ = await asyncio.wait((state.task,), timeout=5)
            assert state.task in done, "fake runtime did not settle for test cleanup"
        if state.task is not None:
            await asyncio.gather(state.task, return_exceptions=True)
    resources = [value for _, value in adapter._ownership._resources]
    with Recorder(tmp_path / "test-final-cleanup.jsonl", Redactor()) as recorder:
        for resource in resources:
            if (
                isinstance(resource, Process)
                and not resource.ownership_record("test").stopped
            ):
                resource._recorder = recorder
                await resource.terminate(0.2)
        for resource in resources:
            if isinstance(resource, CodexIsolation) and not resource._removed:
                resource.recorder = recorder
                for process in resource._processes:
                    process._recorder = recorder
                    await resource.terminate(process, 0.2)
                assert resource.cleanup()


def assert_released(adapter):
    receipt = adapter.ownership_receipt()
    assert receipt.settled
    assert receipt.processes_stopped and receipt.resources_released
    assert receipt.logs_sealed and receipt.outputs_complete
    return receipt


def cleanup_gate(patch, provider, capture, predicate, *, failure=None):
    """Pause real process cleanup after the matching fake has produced output."""
    entered, release = asyncio.Event(), asyncio.Event()
    cancelled = []
    used = False
    original_process = Process.terminate
    original_isolation = CodexIsolation.terminate

    async def wait_at_gate(process):
        nonlocal used
        if not used and predicate(argv_for(capture, process)):
            used = True
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
            if failure is not None:
                raise failure

    async def process_stop(process, grace):
        await wait_at_gate(process)
        await original_process(process, grace)

    async def isolation_stop(isolation, process, grace):
        await wait_at_gate(process)
        await original_isolation(isolation, process, grace)

    if provider == "claude":
        patch.setattr(Process, "terminate", process_stop)
    else:
        patch.setattr(CodexIsolation, "terminate", isolation_stop)
    return entered, release, cancelled


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_shared_deadline_expired_preflight_and_turn_never_spawn(
    tmp_path, monkeypatch, provider
):
    async def run():
        deadline = asyncio.get_running_loop().time() - 1
        adapter, config, capture = helpers.setup(
            tmp_path, monkeypatch, provider, deadline_at=deadline
        )
        try:
            with pytest.raises(RuntimeFailure) as caught:
                await adapter.check_environment()
            assert caught.value.event.failure_category == FailureCategory.TIMEOUT
            assert "cleanup_incomplete" != caught.value.event.raw_type
            assert not capture.exists()
            receipt = assert_released(adapter)
            assert len(receipt.operations) == 1 and len(receipt.logs) == 1
            assert receipt.processes == ()
            turn = await adapter.submit_turn(
                await adapter.start_session(config), "expired"
            )
            events, result = await completed_stream(adapter, turn, adapter.events(turn))
            assert isinstance(result.terminal, Error)
            assert result.terminal.failure_category == FailureCategory.TIMEOUT
            assert result.process_exit is None
            assert events == [result.terminal]
            assert not capture.exists()
            receipt = assert_released(adapter)
            assert len(receipt.operations) == 2 and len(receipt.logs) == 2
            assert receipt.processes == ()
            assert adapter.deadline_at == deadline
        finally:
            await clean_owned(adapter, tmp_path)

    asyncio.run(run())


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_shared_deadline_preflight_and_model_use_same_absolute_limit(
    tmp_path, monkeypatch, provider
):
    # This integration deliberately retains the real monotonic clock. Startup
    # and preflight have a five-second allowance, not a tiny timing race.
    async def run():
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5
        adapter, config, capture = helpers.setup(
            tmp_path, monkeypatch, provider, "wait", timeout=30, deadline_at=deadline
        )
        try:
            async with asyncio.timeout(12):
                environment = await adapter.check_environment()
                assert environment.logged_in is True
                turn, stream, _ = await ready_turn(adapter, config)
                state = adapter._turns[turn]
                assert state.deadline.when() == deadline
                assert adapter.deadline_at == deadline and loop.time() < deadline
                _, result = await completed_stream(adapter, turn, stream)
                assert result.terminal.failure_category == FailureCategory.TIMEOUT
                assert result.process_exit is not None
                assert loop.time() >= deadline
                assert adapter.deadline_at == deadline
                receipt = assert_released(adapter)
                assert len(receipt.operations) == 2
                assert len(receipt.processes) == (4 if provider == "codex" else 3)
                assert all(
                    row["argv"] for row in records(capture) if row["kind"] == "spawn"
                )
        finally:
            await clean_owned(adapter, tmp_path)

    asyncio.run(run())


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize("final_cleanup", [False, True])
def test_preflight_cleanup_crossing_deadline_preserves_environment(
    tmp_path, monkeypatch, provider, final_cleanup
):
    async def run():
        with monkeypatch.context() as clock_patch:
            clock = DeadlineClock(clock_patch)
            adapter, config, capture = helpers.setup(
                tmp_path, monkeypatch, provider, deadline_at=clock.deadline
            )
            expected_argv = (
                (
                    ["auth", "status"]
                    if provider == "claude"
                    else ["app-server", "--strict-config"]
                )
                if final_cleanup
                else ["--version"]
            )
            checking = None
            try:
                with monkeypatch.context() as patch:
                    entered, release, cancelled = cleanup_gate(
                        patch, provider, capture, lambda argv: argv == expected_argv
                    )
                    checking = asyncio.create_task(adapter.check_environment())
                    try:
                        async with asyncio.timeout(10):
                            await entered.wait()
                        await clock.cross()
                        release.set()
                        async with asyncio.timeout(5):
                            if final_cleanup:
                                environment = await checking
                                assert environment.logged_in is True
                                assert environment.cli_version == (
                                    "codex-cli 0.169.0"
                                    if provider == "codex"
                                    else "fake-claude 1.0"
                                )
                            else:
                                with pytest.raises(RuntimeFailure) as caught:
                                    await checking
                                assert (
                                    caught.value.event.failure_category
                                    == FailureCategory.TIMEOUT
                                )
                                assert (
                                    caught.value.event.raw_type
                                    != "adapter.cleanup_incomplete"
                                )
                        assert cancelled == [], "execution deadline interrupted cleanup"
                        assert adapter.deadline_at == clock.deadline
                        receipt = assert_released(adapter)
                        assert all(
                            item.exit_code is not None for item in receipt.processes
                        )
                        launches = [
                            row for row in records(capture) if row["kind"] == "spawn"
                        ]
                        assert len(launches) == (2 if final_cleanup else 1)
                        if not final_cleanup:
                            assert launches[0]["argv"] == ["--version"]
                    finally:
                        release.set()
                        if checking is not None:
                            await asyncio.gather(checking, return_exceptions=True)
                before = records(capture)
                turn = await adapter.submit_turn(
                    await adapter.start_session(config), "after preflight deadline"
                )
                _, result = await completed_stream(adapter, turn, adapter.events(turn))
                assert result.terminal.failure_category == FailureCategory.TIMEOUT
                assert result.process_exit is None
                assert records(capture) == before
                assert_released(adapter)
            finally:
                await clean_owned(adapter, tmp_path)

    asyncio.run(run())


@pytest.mark.parametrize("after_method", ["account/read", "thread/start"])
def test_deadline_between_codex_audit_and_model_prevents_submission(
    tmp_path, monkeypatch, after_method
):
    async def run():
        with monkeypatch.context() as clock_patch:
            clock = DeadlineClock(clock_patch)
            adapter, config, _capture = helpers.setup(
                tmp_path, monkeypatch, "codex", deadline_at=clock.deadline
            )
            original_request = _Rpc.request
            responses = []

            async def cross_after_actual_response(rpc, method, params):
                response = await original_request(rpc, method, params)
                if method == after_method:
                    responses.append(response)
                    # No yield before the next launch guard: timeout_at alone
                    # would otherwise allow the next request to be written.
                    clock.cross_without_yield()
                return response

            try:
                with monkeypatch.context() as patch:
                    patch.setattr(_Rpc, "request", cross_after_actual_response)
                    turn = await adapter.submit_turn(
                        await adapter.start_session(config), "cross during audit"
                    )
                    # This absolute outer guard remains ahead of the deliberate
                    # clock jump and still bounds a broken handshake/reader.
                    async with asyncio.timeout_at(clock.deadline + 10):
                        events = [event async for event in adapter.events(turn)]
                        result = await adapter.result(turn)
                assert len(responses) == 1
                assert result.terminal.failure_category == FailureCategory.TIMEOUT
                assert result.process_exit is not None
                assert not any(isinstance(event, Completed) for event in events)
                methods = [
                    row["message"]["method"]
                    for path in adapter.log_dir.glob("*.jsonl")
                    for row in records(path)
                    if row["kind"] == "send" and "method" in row["message"]
                ]
                assert "account/read" in methods
                assert "turn/start" not in methods
                assert ("thread/start" in methods) is (after_method == "thread/start")
                assert adapter.deadline_at == clock.deadline
                assert_released(adapter)
            finally:
                await clean_owned(adapter, tmp_path)

    asyncio.run(run())


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_observed_terminal_survives_absolute_deadline_during_cleanup(
    tmp_path, monkeypatch, provider
):
    async def run():
        with monkeypatch.context() as clock_patch:
            clock = DeadlineClock(clock_patch)
            adapter, config, capture = helpers.setup(
                tmp_path, monkeypatch, provider, deadline_at=clock.deadline
            )
            try:
                with monkeypatch.context() as patch:
                    entered, release, cancelled = cleanup_gate(
                        patch,
                        provider,
                        capture,
                        lambda argv: argv[:1] in (["-p"], ["app-server"]),
                    )
                    turn = await adapter.submit_turn(
                        await adapter.start_session(config), "finish"
                    )
                    state = adapter._turns[turn]
                    try:
                        async with asyncio.timeout(10):
                            await entered.wait()
                        assert isinstance(state.observed_terminal, Completed)
                        original_terminal = state.observed_terminal
                        assert state.deadline.when() is None
                        await clock.cross()
                        release.set()
                        _, result = await completed_stream(
                            adapter, turn, adapter.events(turn)
                        )
                        assert result.terminal == original_terminal
                        assert result.process_exit is not None
                        assert cancelled == []
                        assert adapter.deadline_at == clock.deadline
                        assert_released(adapter)
                    finally:
                        release.set()
            finally:
                await clean_owned(adapter, tmp_path)

    asyncio.run(run())


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_timeout_then_cancel_keeps_timeout(tmp_path, monkeypatch, provider):
    async def run():
        with monkeypatch.context() as clock_patch:
            clock = DeadlineClock(clock_patch)
            adapter, config, capture = helpers.setup(
                tmp_path,
                monkeypatch,
                provider,
                "ignore-cancel",
                deadline_at=clock.deadline,
            )
            try:
                turn, stream, _ = await ready_turn(adapter, config)
                state = adapter._turns[turn]
                with monkeypatch.context() as patch:
                    entered, release, _ = cleanup_gate(
                        patch,
                        provider,
                        capture,
                        lambda argv: argv[:1] in (["-p"], ["app-server"]),
                    )
                    try:
                        assert state.deadline.when() == clock.deadline
                        await clock.cross()
                        async with asyncio.timeout(5):
                            await entered.wait()
                        assert state.deadline.expired() or state.timed_out
                        accepted = []
                        assert not await adapter.cancel(
                            turn, on_accepted=lambda: accepted.append(True)
                        )
                        assert accepted == [] and not state.cancelled
                        release.set()
                        _, result = await completed_stream(adapter, turn, stream)
                        assert (
                            result.terminal.failure_category == FailureCategory.TIMEOUT
                        )
                        assert result.process_exit is not None
                        assert_released(adapter)
                    finally:
                        release.set()
            finally:
                await clean_owned(adapter, tmp_path)

    asyncio.run(run())


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_cancel_then_deadline_keeps_cancellation(tmp_path, monkeypatch, provider):
    async def run():
        with monkeypatch.context() as clock_patch:
            clock = DeadlineClock(clock_patch)
            adapter, config, _ = helpers.setup(
                tmp_path, monkeypatch, provider, "wait", deadline_at=clock.deadline
            )
            release = asyncio.Event()
            cancellation = None
            try:
                turn, stream, _ = await ready_turn(adapter, config)
                state = adapter._turns[turn]
                original_send = adapter._send_cancel
                entered = asyncio.Event()
                accepted = []

                async def gated_send(current):
                    assert current is state and accepted == [True]
                    entered.set()
                    await release.wait()
                    await original_send(current)

                with monkeypatch.context() as patch:
                    patch.setattr(adapter, "_send_cancel", gated_send)
                    cancellation = asyncio.create_task(
                        adapter.cancel(turn, on_accepted=lambda: accepted.append(True))
                    )
                    try:
                        async with asyncio.timeout(10):
                            await entered.wait()
                        assert state.cancelled and state.deadline.when() is None
                        assert not await adapter.cancel(
                            turn, on_accepted=lambda: accepted.append(False)
                        )
                        await clock.cross()
                        release.set()
                        async with asyncio.timeout(5):
                            assert await cancellation
                        _, result = await completed_stream(adapter, turn, stream)
                        assert (
                            result.terminal.failure_category
                            == FailureCategory.CANCELLED
                        )
                        assert result.process_exit is not None
                        assert accepted == [True]
                        assert adapter.deadline_at == clock.deadline
                        assert_released(adapter)
                    finally:
                        release.set()
            finally:
                if cancellation is not None:
                    await asyncio.gather(cancellation, return_exceptions=True)
                await clean_owned(adapter, tmp_path)

    asyncio.run(run())


def test_codex_cancel_send_hang_is_bounded(tmp_path, monkeypatch):
    # Keep a real clock here: the product must bound actual interrupt sending
    # after accepting cancellation, even when the model budget is much longer.
    async def run():
        adapter, config, _ = helpers.setup(
            tmp_path, monkeypatch, "codex", "wait", timeout=30
        )
        release = asyncio.Event()
        cancellation = None
        try:
            turn, stream, _ = await ready_turn(adapter, config)
            state = adapter._turns[turn]
            original_send = state.rpc.send
            entered = asyncio.Event()
            accepted = []

            async def hung_interrupt(method, params):
                assert method == "turn/interrupt"
                assert accepted == [True]
                entered.set()
                await release.wait()
                return await original_send(method, params)

            with monkeypatch.context() as patch:
                patch.setattr(state.rpc, "send", hung_interrupt)
                cancellation = asyncio.create_task(
                    adapter.cancel(turn, on_accepted=lambda: accepted.append(True))
                )
                try:
                    async with asyncio.timeout(10):
                        await entered.wait()
                    started = time.perf_counter()
                    done, _ = await asyncio.wait((cancellation,), timeout=2)
                    assert cancellation in done, "accepted cancel hung in rpc.send"
                    assert cancellation.result() is True
                    assert time.perf_counter() - started < 2
                    _, result = await completed_stream(adapter, turn, stream)
                    assert result.terminal.failure_category == FailureCategory.CANCELLED
                    assert result.process_exit is not None
                    assert accepted == [True]
                    assert_released(adapter)
                finally:
                    release.set()
            if cancellation is not None:
                await asyncio.gather(cancellation, return_exceptions=True)
        finally:
            release.set()
            if cancellation is not None:
                await asyncio.gather(cancellation, return_exceptions=True)
            await clean_owned(adapter, tmp_path)

    asyncio.run(run())


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_terminal_before_deadline_does_not_hide_late_contradictory_wire(
    tmp_path, monkeypatch, provider
):
    release = tmp_path / "release-late-terminal"
    monkeypatch.setenv("FAKE_DEADLINE_RELEASE", str(release))

    async def run():
        with monkeypatch.context() as clock_patch:
            clock = DeadlineClock(clock_patch)
            adapter, config, _ = helpers.setup(
                tmp_path,
                monkeypatch,
                provider,
                "deadline-late-contradictory",
                deadline_at=clock.deadline,
            )
            first_terminal = asyncio.Event()
            cleanup_held = asyncio.Event()
            release_cleanup = asyncio.Event()
            late_wire = asyncio.Event()
            original_observe = adapter._observe
            original_close_finished = Process.close_finished
            original_wait_for_exit = Process.wait_for_exit
            original_emit = Recorder.emit

            def observe(state, event):
                original_observe(state, event)
                if isinstance(event, Completed):
                    first_terminal.set()

            async def hold_before_grace(process):
                if first_terminal.is_set():
                    cleanup_held.set()
                    await release_cleanup.wait()

            async def close_finished(process, grace):
                await hold_before_grace(process)
                await original_close_finished(process, grace)

            async def wait_for_exit(process, grace):
                await hold_before_grace(process)
                return await original_wait_for_exit(process, grace)

            def record_wire(recorder, kind, **fields):
                original_emit(recorder, kind, **fields)
                message = fields.get("message", {})
                if kind == "recv" and (
                    message.get("type") == "result"
                    and message.get("is_error") is True
                    or message.get("method") == "turn/completed"
                    and message.get("params", {}).get("turn", {}).get("status")
                    == "failed"
                ):
                    late_wire.set()

            try:
                with monkeypatch.context() as patch:
                    patch.setattr(adapter, "_observe", observe)
                    patch.setattr(Recorder, "emit", record_wire)
                    if provider == "claude":
                        patch.setattr(Process, "wait_for_exit", wait_for_exit)
                    else:
                        patch.setattr(Process, "close_finished", close_finished)
                    turn = await adapter.submit_turn(
                        await adapter.start_session(config), "first then contradictory"
                    )
                    state = adapter._turns[turn]
                    async with asyncio.timeout(10):
                        await first_terminal.wait()
                        await cleanup_held.wait()
                    assert isinstance(state.observed_terminal, Completed)
                    assert state.deadline.when() is None
                    await clock.cross()
                    release.touch()
                    # No low-level grace timer existed during the clock jump.
                    # Wait for real contradictory bytes before beginning drain.
                    async with asyncio.timeout(5):
                        await late_wire.wait()
                    release_cleanup.set()
                    events, result = await completed_stream(
                        adapter, turn, adapter.events(turn)
                    )
                assert isinstance(result.terminal, Error)
                assert result.terminal.failure_category is None
                assert "multiple terminal" in result.terminal.detail
                assert not any(isinstance(event, Completed) for event in events)
                assert result.process_exit is not None
                wire = [
                    row["message"]
                    for path in adapter.log_dir.glob("*.jsonl")
                    for row in records(path)
                    if row["kind"] == "recv"
                    and "message" in row
                    and (
                        row["message"].get("type") == "result"
                        if provider == "claude"
                        else row["message"].get("method") == "turn/completed"
                    )
                ]
                assert len(wire) == 2
                assert adapter.deadline_at == clock.deadline
                assert_released(adapter)
            finally:
                release.touch()
                release_cleanup.set()
                await clean_owned(adapter, tmp_path)

    asyncio.run(run())


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_cancel_callback_failure_still_stops_owned_process(
    tmp_path, monkeypatch, provider
):
    async def run():
        adapter, config, _ = helpers.setup(
            tmp_path,
            monkeypatch,
            provider,
            "ignore-cancel",
            timeout=30,
            deadline_at=asyncio.get_running_loop().time() + 30,
        )
        original_signal = Process.signal
        failure = RuntimeError("callback persistence failed")
        order = []

        def accepted():
            order.append("callback")
            raise failure

        def observe_signal(process, signum):
            order.append(signum.name)
            return original_signal(process, signum)

        try:
            turn, stream, _ = await ready_turn(adapter, config)
            state = adapter._turns[turn]
            with monkeypatch.context() as patch:
                patch.setattr(Process, "signal", observe_signal)
                with pytest.raises(RuntimeError) as caught:
                    await adapter.cancel(turn, on_accepted=accepted)
                assert caught.value is failure
                assert order == ["callback"], "callback must precede actual signal I/O"
                assert state.cancelled and state.deadline.when() is None
                assert not await adapter.cancel(turn, on_accepted=accepted)
                _, result = await completed_stream(adapter, turn, stream)
            assert order[0] == "callback" and len(order) > 1
            assert order.count("callback") == 1
            assert result.terminal.failure_category == FailureCategory.CANCELLED
            assert result.process_exit is not None
            assert_released(adapter)
        finally:
            await clean_owned(adapter, tmp_path)

    asyncio.run(run())


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_cancel_watchdog_stop_failure_reports_unknown_owned_resources(
    tmp_path, monkeypatch, provider
):
    async def run():
        adapter, config, _ = helpers.setup(
            tmp_path,
            monkeypatch,
            provider,
            "ignore-cancel",
            timeout=30,
            deadline_at=asyncio.get_running_loop().time() + 30,
        )
        failures = []

        async def cannot_stop(process, grace):
            failures.append(process)
            raise OSError("injected actual stop failure")

        try:
            turn, stream, _ = await ready_turn(adapter, config)
            with monkeypatch.context() as patch:
                patch.setattr(Process, "terminate", cannot_stop)
                accepted = []
                assert await adapter.cancel(
                    turn, on_accepted=lambda: accepted.append(True)
                )
                _, result = await completed_stream(adapter, turn, stream)
                assert accepted == [True]
                assert failures
                assert result.terminal.raw_type == "adapter.cleanup_incomplete"
                assert result.process_exit is None
                receipt = adapter.ownership_receipt()
                assert not receipt.processes_stopped
                assert not receipt.resources_released
                assert any(item.exit_code is None for item in receipt.processes)
                assert not await adapter.cancel(turn)
        finally:
            await clean_owned(adapter, tmp_path)

    asyncio.run(run())


def test_cancel_during_claude_creation_keeps_owned_shutdown(tmp_path, monkeypatch):
    async def run():
        adapter, config, _ = helpers.setup(
            tmp_path,
            monkeypatch,
            "claude",
            "ignore-cancel",
            timeout=30,
            deadline_at=asyncio.get_running_loop().time() + 30,
        )
        created, release = asyncio.Event(), asyncio.Event()
        original_spawn = asyncio.create_subprocess_exec
        children = []

        async def delayed_return(*args, **kwargs):
            child = await original_spawn(*args, **kwargs)
            children.append(child)
            created.set()
            await release.wait()
            return child

        try:
            with monkeypatch.context() as patch:
                patch.setattr(asyncio, "create_subprocess_exec", delayed_return)
                turn = await adapter.submit_turn(
                    await adapter.start_session(config), "creation"
                )
                state = adapter._turns[turn]
                async with asyncio.timeout(10):
                    await created.wait()
                assert state.process is None and children[0].returncode is None
                assert await adapter.cancel(turn)
                assert state.deadline.when() is None
                release.set()
                _, result = await completed_stream(adapter, turn, adapter.events(turn))
            assert result.terminal.failure_category == FailureCategory.CANCELLED
            # The Process.start owner observed the real exit; _run never received
            # a model process handle and must not synthesize its ProcessExited.
            assert result.process_exit is None
            receipt = assert_released(adapter)
            assert len(receipt.processes) == 1
            assert receipt.processes[0].pid == children[0].pid
            assert receipt.processes[0].exit_code is not None
        finally:
            release.set()
            await clean_owned(adapter, tmp_path)

    asyncio.run(run())
