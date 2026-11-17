"""Run one declared suite in its fresh checkout and retain actual settlement."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from multi_agent.adapters.test_runtime import TaskTestRuntimeAdapter, TestCommandResult
from multi_agent.core import Disposition
from multi_agent.core.task_execution import task_execution_hash
from multi_agent.core.task_test_events import (
    TaskTestAttemptFinished,
    TaskTestCancelRequested,
    TaskTestCommandExited,
    TaskTestCommandReported,
    TaskTestCommandStarted,
    TaskTestEnvelope,
    TaskTestEvent,
    TaskTestProblem,
    TaskTestReconciled,
)
from multi_agent.core.task_test_execution import TaskTestPolicy
from multi_agent.core.task_test_state import TaskTestState
from multi_agent.core.tasks import NamedCommand

from .task_test_context import TaskTestLaunch, prepare_task_test_launch
from .task_test_evidence import command_process_stopped, seal_task_test_command


class _DeferredHostFailure(Exception):
    """Retain the original API exception until its operation is settled."""

    def __init__(self, original: BaseException) -> None:
        super().__init__("test host boundary failed")
        self.original = original


def _boundary_call[T](operation: Callable[[], T]) -> T:
    try:
        return operation()
    except BaseException as error:
        raise _DeferredHostFailure(error) from error


async def _boundary_await[T](operation: Callable[[], Awaitable[T]]) -> T:
    try:
        return await operation()
    except BaseException as error:
        raise _DeferredHostFailure(error) from error


_HOST_FAILURES = (
    _DeferredHostFailure,
    ValueError,
    TypeError,
    AssertionError,
    RuntimeError,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _task[T](coroutine: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
    try:
        return asyncio.create_task(coroutine)
    except BaseException:
        coroutine.close()
        raise


async def _wait_for_stop(intent: asyncio.Event, external: asyncio.Event | None) -> None:
    if external is None:
        await intent.wait()
        return
    waiters: list[asyncio.Task[bool]] = []
    try:
        waiters.append(_task(intent.wait()))
        waiters.append(_task(external.wait()))
        done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        for waiter in done:
            waiter.result()
    finally:
        for waiter in waiters:
            waiter.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)


@dataclass(frozen=True, slots=True)
class _Pending:
    envelope: TaskTestEnvelope
    prior_count: int | None = None
    require_applied: bool = False


@dataclass(slots=True)
class _Suite:
    launch: TaskTestLaunch
    intent: asyncio.Event
    external: asyncio.Event | None
    first_error: BaseException | None = None
    database_failed: bool = False
    pending: list[_Pending] = field(default_factory=list)
    confirmed: Counter[str] = field(default_factory=Counter)
    accepted_cancel: bool = False
    sequence: int | None = None
    stopped: bool | None = True
    clean_intercommand_stop: bool = False

    @property
    def run_id(self) -> str:
        return self.launch.prepared.checkout.input.task_run_id

    @property
    def implementation_attempt(self) -> int:
        return self.launch.prepared.checkout.input.implementation_attempt

    def wants_stop(self) -> bool:
        return (
            self.intent.is_set() or self.external is not None and self.external.is_set()
        )

    def failure(self, error: BaseException) -> None:
        if isinstance(error, _DeferredHostFailure):
            error = error.original
        if self.first_error is None:
            self.first_error = error
        elif self.first_error is not error:
            self.first_error.add_note(
                f"Additional test settlement failure: {type(error).__name__}: {error}"
            )
        self.intent.set()

    def envelope(self, event: TaskTestEvent) -> TaskTestEnvelope:
        return TaskTestEnvelope(
            self.implementation_attempt, self.launch.test_attempt, event, _now()
        )

    def persist(self, event: TaskTestEvent, *, cancellation: bool = False) -> bool:
        envelope = self.envelope(event)
        if self.database_failed and not cancellation:
            self.pending.append(_Pending(envelope))
            return False
        digest = task_execution_hash(envelope)
        prior = self.confirmed[digest]
        try:
            disposition = self.launch.store.apply_task_test(self.run_id, envelope)
            if cancellation and disposition != Disposition.APPLIED:
                raise ValueError("accepted test cancellation did not apply")
        except BaseException as error:
            self.database_failed = True
            self.pending.append(_Pending(envelope, prior, cancellation))
            self.failure(error)
            if cancellation:
                raise
            return False
        self.confirmed[digest] += 1
        return True

    def accept_cancel(self) -> None:
        if self.accepted_cancel:
            raise ValueError("test cancellation was accepted more than once")
        self.accepted_cancel = True
        self.persist(TaskTestCancelRequested(), cancellation=True)

    def repair_pending(self) -> None:
        pending, self.pending = self.pending, []
        for item in pending:
            digest = task_execution_hash(item.envelope)
            try:
                if item.prior_count is not None:
                    history = _boundary_call(
                        lambda: self.launch.store.load_task_test_history(
                            self.run_id, self.implementation_attempt
                        )
                    )
                    count = sum(
                        task_execution_hash(envelope) == digest
                        and (
                            not item.require_applied
                            or disposition == Disposition.APPLIED
                        )
                        for envelope, disposition in history
                    )
                    if count > item.prior_count:
                        self.confirmed[digest] += 1
                        continue

                def write_pending(item: _Pending = item) -> Disposition:
                    return self.launch.store.apply_task_test(self.run_id, item.envelope)

                disposition = _boundary_call(write_pending)
                if item.require_applied and disposition != Disposition.APPLIED:
                    raise ValueError("accepted test cancellation could not be repaired")
                self.confirmed[digest] += 1
            except _HOST_FAILURES as error:
                # Each uncertain occurrence receives one readback and at most
                # one repair; no rerun or repeated publication is inferred.
                self.failure(error)

    def host_problem(self) -> None:
        if self.first_error is not None:
            self.persist(
                TaskTestProblem(
                    self.sequence,
                    None,
                    f"Test host failed: {type(self.first_error).__name__}: {self.first_error}",
                )
            )

    def reject_after_finalization_failure(self) -> None:
        """A last host failure cannot leave a qualified successful conclusion."""
        self.host_problem()
        self.persist(
            TaskTestReconciled(
                self.sequence,
                self.stopped,
                "Test finalization failed; actual stop retained without qualified evidence",
            )
        )
        self.repair_pending()

    async def cancel(self, adapter: TaskTestRuntimeAdapter) -> None:
        try:
            await _boundary_await(adapter.cancel)
        except _HOST_FAILURES as error:
            self.failure(error)

    async def run_command(self, command: NamedCommand, sequence: int) -> bool:
        self.clean_intercommand_stop = False
        adapter: TaskTestRuntimeAdapter | None = None
        operation: asyncio.Task[TestCommandResult] | None = None
        monitor: asyncio.Task[None] | None = None
        result: TestCommandResult | None = None
        try:
            _boundary_call(self.launch.check)
            state, history = _boundary_call(
                lambda: self.launch.store.load_task_test_checkpoint(
                    self.run_id, self.implementation_attempt
                )
            )
            assert state is not None and state.current_attempt is not None
            current = state.current_command
            _boundary_call(
                lambda: self.launch.store.reserve_task_test_command(
                    self.run_id,
                    self.envelope(TaskTestCommandStarted(sequence)),
                    expected_test_attempt=self.launch.test_attempt,
                    expected_command_sequence=current.sequence if current else None,
                    expected_history_length=len(history),
                    expected_history_hash=task_execution_hash(history),
                )
            )
            self.sequence = sequence
            _boundary_call(lambda: self.launch.claim(sequence))
            prepared = self.launch.prepared
            adapter = _boundary_call(
                lambda: TaskTestRuntimeAdapter(
                    checkout=prepared.checkout,
                    scratch=prepared.scratch,
                    policy=prepared.policy,
                    log_dir=self.launch.log_dir / f"command-{sequence}",
                    deadline_at=asyncio.get_running_loop().time()
                    + prepared.policy.timeout,
                    on_cancel_accepted=self.accept_cancel,
                )
            )
            running_adapter = adapter
            operation = _boundary_call(
                lambda: _task(running_adapter.run(command, sequence=sequence))
            )
            self.stopped = None
            monitor = _boundary_call(
                lambda: _task(_wait_for_stop(self.intent, self.external))
            )
            active_operation, active_monitor = operation, monitor
            done, _ = await _boundary_await(
                lambda: asyncio.wait(
                    (active_operation, active_monitor),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            )
            if monitor in done:
                _boundary_call(monitor.result)
                if not operation.done():
                    await self.cancel(adapter)
        except _HOST_FAILURES as error:
            self.failure(error)
        finally:
            if operation is not None and adapter is not None:
                if self.first_error is not None and not operation.done():
                    # run() must enter before cancel(): its public contract has
                    # no operation until the coroutine has started.
                    await asyncio.sleep(0)
                    await self.cancel(adapter)
                try:
                    finishing_operation = operation
                    result = await _boundary_await(lambda: finishing_operation)
                except _HOST_FAILURES as error:
                    self.failure(error)
                try:
                    snapshot = _boundary_call(adapter.snapshot)
                    if result is None:
                        result = snapshot
                except _HOST_FAILURES as error:
                    self.failure(error)
            if monitor is not None:
                if not monitor.done():
                    monitor.cancel()
                outcomes = await asyncio.gather(monitor, return_exceptions=True)
                if isinstance(outcomes[0], BaseException) and not isinstance(
                    outcomes[0], asyncio.CancelledError
                ):
                    self.failure(outcomes[0])
            if result is not None:
                self.save_result(sequence, result)
            elif operation is None:
                # No runtime coroutine was started. A reservation may have
                # committed before its caller failed, so recover the epoch.
                try:
                    state = _boundary_call(
                        lambda: self.launch.store.load_task_test(
                            self.run_id, self.implementation_attempt
                        )
                    )
                    current = state.current_command
                    self.sequence = current.sequence if current else None
                except _HOST_FAILURES as error:
                    self.failure(error)
                self.stopped = True
                self.host_problem()
                self.persist(
                    TaskTestReconciled(
                        self.sequence, True, "No test runtime operation was started"
                    )
                )
                self.repair_pending()
        self.clean_intercommand_stop = bool(
            result is not None
            and result.request_settled
            and self.stopped is True
            and self.first_error is None
            and not result.cancel_accepted
            and not result.timeout_observed
            and not result.output_limit_reached
            and result.stdout_cap_reached is not True
            and result.stderr_cap_reached is not True
            and not result.problems
        )
        return bool(
            result is not None
            and result.request_settled
            and self.stopped is True
            and self.first_error is None
            and not self.wants_stop()
            and not result.cancel_accepted
            and not result.timeout_observed
            and not result.output_limit_reached
            and result.stdout_cap_reached is not True
            and result.stderr_cap_reached is not True
        )

    def save_result(self, sequence: int, result: TestCommandResult) -> None:
        try:
            if result.sequence != sequence:
                raise ValueError("test runtime snapshot belongs to another command")
            self.stopped = _boundary_call(lambda: command_process_stopped(result))
            if result.cancel_accepted and not self.accepted_cancel:
                raise ValueError(
                    "test runtime cancellation lacks its accepted callback"
                )
            if result.response is not None and result.reported_exit_code is not None:
                self.persist(
                    TaskTestCommandReported(
                        sequence, result.reported_exit_code, result.response
                    )
                )
            if result.exit_code is not None:
                self.persist(TaskTestCommandExited(sequence, result.exit_code))
            for problem in result.problems:
                self.persist(
                    TaskTestProblem(sequence, problem.failure_category, problem.detail)
                )
            self.host_problem()
            self.persist(
                TaskTestReconciled(
                    sequence,
                    self.stopped,
                    "Actual command request and owned runtime scope settlement",
                )
            )
            self.repair_pending()
            if self.first_error is None:
                manifest = _boundary_call(
                    lambda: seal_task_test_command(self.launch, sequence, result)
                )
                self.persist(
                    TaskTestReconciled(
                        sequence,
                        self.stopped,
                        manifest.validation_error or "Command evidence sealed",
                        manifest.path,
                        manifest.sha256,
                        manifest.qualified,
                    )
                )
                self.repair_pending()
        except _HOST_FAILURES as error:
            self.failure(error)
        finally:
            if self.first_error is not None:
                self.host_problem()
                self.persist(
                    TaskTestReconciled(
                        sequence,
                        self.stopped,
                        "Host failure; independent stop retained without qualified evidence",
                    )
                )
                self.repair_pending()

    async def run(self) -> TaskTestState:
        try:
            for sequence, command in enumerate(
                self.launch.prepared.checkout.input.commands, 1
            ):
                if self.wants_stop():
                    _boundary_call(self.accept_cancel)
                    self.persist(
                        TaskTestReconciled(
                            self.sequence,
                            self.stopped,
                            "Stopped between test runtime operations",
                        )
                    )
                    break
                if not await self.run_command(command, sequence):
                    if (
                        self.wants_stop()
                        and self.first_error is None
                        and self.clean_intercommand_stop
                        and not self.accepted_cancel
                        and sequence < len(self.launch.prepared.checkout.input.commands)
                    ):
                        _boundary_call(self.accept_cancel)
                    break
        except _HOST_FAILURES as error:
            self.failure(error)
        finally:
            self.host_problem()
            self.repair_pending()
            finish = self.first_error is None
            if not finish:
                try:
                    current = _boundary_call(
                        lambda: self.launch.store.load_task_test(
                            self.run_id, self.implementation_attempt
                        )
                    ).current_attempt
                    finish = current is not None and (
                        current.cancel_requested
                        or any(
                            isinstance(event, TaskTestProblem)
                            for event in current.events
                        )
                    )
                except _HOST_FAILURES as error:
                    self.failure(error)
            if finish:
                error_before_finish = self.first_error
                self.persist(TaskTestAttemptFinished())
                self.repair_pending()
                if error_before_finish is None and self.first_error is not None:
                    self.reject_after_finalization_failure()
        if self.first_error is not None:
            raise self.first_error
        try:
            return _boundary_call(
                lambda: self.launch.store.load_task_test(
                    self.run_id, self.implementation_attempt
                )
            )
        except _HOST_FAILURES as error:
            self.failure(error)
            self.reject_after_finalization_failure()
            assert self.first_error is not None
            raise self.first_error


async def execute_task_tests(
    database_path: str | Path,
    task_run_id: str,
    implementation_attempt: int,
    *,
    repository: Path,
    directory: Path,
    required_commands: tuple[str, ...],
    declaration_source: str,
    policy: TaskTestPolicy,
    stop: asyncio.Event | None = None,
) -> TaskTestState:
    """One new attempt; caller cancellation waits for the same owned worker."""
    intent = asyncio.Event()

    async def execute() -> TaskTestState:
        with prepare_task_test_launch(
            database_path,
            task_run_id,
            implementation_attempt,
            repository=repository,
            directory=directory,
            required_commands=required_commands,
            declaration_source=declaration_source,
            policy=policy,
        ) as launch:
            return await _Suite(launch, intent, stop).run()

    worker = _task(execute())
    cancelled: asyncio.CancelledError | None = None
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError as error:
            cancelled = cancelled or error
            intent.set()
        except BaseException:
            if cancelled is None:
                raise
            break
    if cancelled is not None:
        if worker.cancelled():
            raise cancelled
        result_failure = worker.exception()
        if result_failure is not None:
            raise cancelled from result_failure
        raise cancelled
    return worker.result()
