"""Execute one fresh managed implementation, retaining its owned lifetime.

Runtime events keep their arrival order. Host failures defer their original
exception until the public consumer and independent physical facts are settled.
This module does not create a candidate, retry a task, or grant acceptance.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections import Counter
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from multi_agent.adapters.base import (
    EnvironmentInfo,
    Permission,
    SessionConfig,
    SessionHandle,
    TurnHandle,
    TurnResult,
)
from multi_agent.adapters.implementation_runtime import ImplementationRuntimeAdapter
from multi_agent.adapters.ownership import RuntimeOwnershipReceipt
from multi_agent.adapters.runtime_decode import RuntimeFailure
from multi_agent.core.events import (
    AdapterEvent,
    CancelRequested,
    Completed,
    Error,
    FailureCategory,
    Message,
    ProcessExited,
    SessionStarted,
    Usage,
    thaw_json,
)
from multi_agent.core.state import Disposition
from multi_agent.core.task_events import (
    TaskDiagnostic,
    TaskEnvelope,
    TaskEnvironmentObserved,
    TaskEvent,
    TaskEvidenceRejected,
    TaskReconciled,
    TaskScopeBlocked,
)
from multi_agent.core.task_execution import (
    TaskDispatchPolicy,
    TaskExecutionPolicy,
    task_execution_hash,
    task_execution_value,
)
from multi_agent.core.task_state import TaskPhase, TaskState
from multi_agent.store.planning_store import PlanningStore

from .task_context import TaskLaunch, _database, prepare_task_launch
from .task_evidence import (
    TaskExecutionBinding,
    TaskExecutionManifest,
    _light_guard,
    bind_task_execution,
    inspect_task_execution,
    seal_task_execution,
)
from .workspaces import inspect_workspace


class _DeferredHostFailure(Exception):
    """Carry an original synchronous boundary failure while its owner drains."""

    def __init__(self, original: Exception) -> None:
        super().__init__("task host boundary failed")
        self.original = original


def _boundary_call[T](operation: Callable[[], T]) -> T:
    try:
        return operation()
    except Exception as error:
        raise _DeferredHostFailure(error) from error


_HOST_ERRORS = (
    OSError,
    ValueError,
    TypeError,
    LookupError,
    RuntimeError,
    ArithmeticError,
    AssertionError,
    sqlite3.Error,
    RuntimeFailure,
    _DeferredHostFailure,
)


def _task[T](coroutine: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
    try:
        return asyncio.create_task(coroutine)
    except BaseException:
        coroutine.close()
        raise


def _now() -> str:
    return datetime.now(UTC).isoformat()


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
    envelope: TaskEnvelope
    # None means this occurrence was never submitted to the store.
    prior_count: int | None = None


@dataclass(slots=True)
class _Execution:
    launch: TaskLaunch
    run_id: str
    intent: asyncio.Event
    external_stop: asyncio.Event | None
    first_error: Exception | None = None
    problems: list[str] = field(default_factory=list)
    adapter: ImplementationRuntimeAdapter | None = None
    binding: TaskExecutionBinding | None = None
    environment: EnvironmentInfo | None = None
    turn: TurnHandle | None = None
    terminal_hashes: set[str] = field(default_factory=set)
    exit_hashes: set[str] = field(default_factory=set)
    pending: list[_Pending] = field(default_factory=list)
    confirmed: Counter[str] = field(default_factory=Counter)
    database_failed: bool = False
    cleanup_buffered: bool = False
    accepted_cancel: _Pending | None = None
    cancellation_persisted: bool = False
    cancellation_repaired: bool = False
    receipt: RuntimeOwnershipReceipt | None = None
    owner_id: str | None = None
    ownership_changed: bool = False
    operation_epoch: int = 0
    receipt_epoch: int | None = None
    final_digest: str | None = None
    manifest: TaskExecutionManifest | None = None
    waiter_error_seen: bool = False

    def wants_stop(self) -> bool:
        return self.intent.is_set() or (
            self.external_stop is not None and self.external_stop.is_set()
        )

    def envelope(self, event: TaskEvent) -> TaskEnvelope:
        return TaskEnvelope(self.launch.attempt, event, _now())

    def failure(self, error: Exception) -> None:
        if isinstance(error, _DeferredHostFailure):
            error = error.original
        if self.first_error is None:
            self.first_error = error
        elif self.first_error is not error:
            self.first_error.add_note(
                f"Additional task settlement error: {type(error).__name__}: {error}"
            )
        self.problems.append(f"{type(error).__name__}: {error}")
        self.intent.set()

    def api_failure(self, error: Exception) -> None:
        if isinstance(error, _DeferredHostFailure):
            error = error.original
        self.failure(error)
        if isinstance(error, RuntimeFailure):
            self.persist(TaskDiagnostic(error.event))

    def _write(self, envelope: TaskEnvelope, *, require_applied: bool = False) -> bool:
        try:
            disposition = _boundary_call(
                lambda: self.launch.store.apply_task(self.run_id, envelope)
            )
            if require_applied and disposition != Disposition.APPLIED:
                raise ValueError("task host control event did not apply")
        except _HOST_ERRORS as error:
            self.database_failed = True
            self.failure(error)
            return False
        self.confirmed[task_execution_hash(envelope)] += 1
        return True

    def persist(self, event: TaskEvent) -> bool:
        """One host write; remember its original occurrence if it is uncertain."""
        envelope = self.envelope(event)
        if self.database_failed or self.cleanup_buffered:
            self.pending.append(_Pending(envelope))
            return False
        prior = self.confirmed[task_execution_hash(envelope)]
        if self._write(envelope):
            return True
        self.pending.append(_Pending(envelope, prior))
        return False

    def confirm(self, item: _Pending, *, applied: bool = False) -> bool | None:
        """Read exact committed occurrences, never infer success from a phase."""
        try:
            state = _boundary_call(lambda: self.launch.store.load_task(self.run_id))
            if (
                state.current_attempt is None
                or state.current_attempt.number != self.launch.attempt
            ):
                raise ValueError("task execution is no longer the current attempt")
            count = sum(
                task_execution_hash(envelope) == task_execution_hash(item.envelope)
                and (not applied or disposition == Disposition.APPLIED)
                for envelope, disposition in _boundary_call(
                    lambda: self.launch.store.load_task_history(self.run_id)
                )
            )
            return count > (item.prior_count or 0)
        except _HOST_ERRORS as error:
            self.failure(error)
            return None

    def accept_cancel(self) -> None:
        # The adapter calls this synchronously after accepting cancellation and
        # installing its cleanup watchdog, before yielding to cancellation I/O.
        if self.accepted_cancel is not None:
            raise ValueError("task runtime accepted cancellation more than once")
        envelope = self.envelope(CancelRequested())
        prior = self.confirmed[task_execution_hash(envelope)]
        self.accepted_cancel = _Pending(envelope, prior)
        try:
            disposition = self.launch.store.apply_task(self.run_id, envelope)
            if disposition != Disposition.APPLIED:
                raise ValueError("accepted task cancellation did not apply")
        except Exception as error:
            self.database_failed = True
            self.failure(error)
            raise
        self.confirmed[task_execution_hash(envelope)] += 1
        self.cancellation_persisted = True

    def repair_cancel(self) -> bool:
        item = self.accepted_cancel
        if item is None or self.cancellation_persisted:
            return True
        if self.cancellation_repaired:
            return False
        self.cancellation_repaired = True
        committed = self.confirm(item, applied=True)
        if committed is None:
            return False
        if committed:
            self.confirmed[task_execution_hash(item.envelope)] += 1
            self.cancellation_persisted = True
            return True
        if self._write(item.envelope, require_applied=True):
            self.cancellation_persisted = True
            return True
        # One final read can recognize a repair which committed before raising.
        self.cancellation_persisted = self.confirm(item, applied=True) is True
        return self.cancellation_persisted

    def light_check(self) -> None:
        if self.binding is None:
            raise ValueError("task runtime has no fresh execution binding")
        _light_guard(self.binding)

    def observe(self, event: AdapterEvent, *, supplement: bool = False) -> None:
        if not isinstance(
            event, (SessionStarted, Message, Usage, Completed, Error, ProcessExited)
        ):
            self.failure(TypeError("runtime emitted an invalid task event object"))
            return
        digest = task_execution_hash(event)
        observed = None
        if isinstance(event, (Completed, Error)):
            observed = self.terminal_hashes
        elif isinstance(event, ProcessExited):
            observed = self.exit_hashes
        if observed is not None:
            if supplement and digest in observed:
                return
            if observed and digest not in observed:
                self.failure(
                    ValueError(
                        "runtime reports conflicting actual terminal or exit events"
                    )
                )
            observed.add(digest)
        envelope = self.envelope(event)
        if (
            isinstance(event, Error)
            and event.raw_type == "adapter.cleanup_incomplete"
            and not self.exit_hashes
        ):
            self.cleanup_buffered = True
        if (
            self.database_failed
            or self.cleanup_buffered
            or self.accepted_cancel is not None
            and not self.cancellation_persisted
        ):
            self.pending.append(_Pending(envelope))
            return
        try:
            _boundary_call(self.light_check)
        except _HOST_ERRORS as error:
            self.database_failed = True
            self.api_failure(error)
            self.pending.append(_Pending(envelope))
            return
        prior = self.confirmed[task_execution_hash(envelope)]
        if not self._write(envelope):
            self.pending.append(_Pending(envelope, prior))

    def save_pending(self) -> None:
        cancel_confirmed = self.repair_cancel()
        committed: Counter[str] | None = Counter()
        if any(item.prior_count is not None for item in self.pending):
            try:
                committed = Counter(
                    task_execution_hash(envelope)
                    for envelope, _ in _boundary_call(
                        lambda: self.launch.store.load_task_history(self.run_id)
                    )
                )
            except _HOST_ERRORS as error:
                self.failure(error)
                committed = None
        pending, self.pending = self.pending, []
        for item in pending:
            envelope = item.envelope
            event = envelope.event
            if (
                isinstance(event, Error)
                and event.raw_type == "adapter.cleanup_incomplete"
                and not self.exit_hashes
            ):
                envelope = replace(envelope, event=TaskDiagnostic(event))
            elif not cancel_confirmed and isinstance(event, (Completed, Error)):
                # The actual terminal is still preserved by the adapter and the
                # negative execution sample; it cannot bypass unrecorded intent.
                continue
            digest = task_execution_hash(envelope)
            if item.prior_count is not None:
                if committed is None:
                    continue
                if committed[digest] > item.prior_count:
                    self.confirmed[digest] += 1
                    continue
            # Every retained occurrence gets at most one repair attempt. An
            # earlier failure must not prevent trying a later actual exit.
            self._write(envelope)
        self.cleanup_buffered = False

    def sample_receipt(self) -> bool:
        if self.adapter is None:
            return False
        try:
            receipt = _boundary_call(self.adapter.ownership_receipt)
            self.remember_receipt(receipt)
        except _HOST_ERRORS as error:
            self.api_failure(error)
            return False
        return True

    def remember_receipt(self, receipt: RuntimeOwnershipReceipt) -> None:
        if not isinstance(receipt, RuntimeOwnershipReceipt):
            raise TypeError("runtime ownership must be an actual receipt")
        if self.owner_id is not None and receipt.owner_id != self.owner_id:
            self.ownership_changed = True
            self.receipt_epoch = None
            raise ValueError("task runtime ownership changed to another owner")
        if self.ownership_changed:
            raise ValueError("task runtime ownership was already invalidated")
        if not receipt.owner_id:
            raise ValueError("runtime ownership has no owner identity")
        self.owner_id = receipt.owner_id
        self.receipt = receipt
        self.receipt_epoch = self.operation_epoch

    def stopped(self) -> bool:
        return (
            self.receipt is not None
            and not self.ownership_changed
            and self.receipt_epoch == self.operation_epoch
            and self.receipt.processes_stopped
        )

    def stop_waiter_failure(self, waiter: asyncio.Task[None]) -> None:
        if self.waiter_error_seen or not waiter.done() or waiter.cancelled():
            return
        error = waiter.exception()
        if error is not None:
            self.waiter_error_seen = True
            if not isinstance(error, Exception):
                raise error
            self.api_failure(error)

    def environment_problem(self) -> Error | None:
        assert self.environment is not None
        choice = self.launch.store.load_task(self.run_id).current_attempt
        if choice is None or choice.number != self.launch.attempt:
            raise ValueError("task environment belongs to an obsolete attempt")
        request = choice.request.decision.choice
        if self.environment.logged_in is False:
            return Error(
                "runtime reports no login",
                FailureCategory.AUTH_EXPIRED,
                raw_type="environment",
            )
        if (
            self.environment.models is not None
            and request.model not in self.environment.models
        ):
            return Error(
                "selected implementation model is unavailable",
                FailureCategory.MODEL_UNAVAILABLE,
                raw_type="environment",
            )
        if (
            self.environment.model_efforts is not None
            and request.model in self.environment.model_efforts
            and request.effort not in self.environment.model_efforts[request.model]
        ):
            return Error(
                "selected implementation effort is unsupported", raw_type="environment"
            )
        return None

    async def preflight(self, waiter: asyncio.Task[None]) -> bool:
        assert self.adapter is not None and self.adapter.deadline_at is not None
        self.operation_epoch += 1
        task = _task(self.adapter.check_environment())
        await asyncio.sleep(0)
        await asyncio.wait((task, waiter), return_when=asyncio.FIRST_COMPLETED)
        self.stop_waiter_failure(waiter)
        cancelled_here = False
        if (
            not task.done()
            and self.wants_stop()
            and asyncio.get_running_loop().time() < self.adapter.deadline_at
            and not task.cancelling()
        ):
            task.cancel()
            cancelled_here = True
        try:
            environment = await asyncio.shield(task)
        except RuntimeFailure as error:
            self.persist(TaskDiagnostic(error.event))
            return False
        except asyncio.CancelledError as error:
            if cancelled_here:
                self.accept_cancel()
            else:
                unexpected_cancel = RuntimeError(
                    "preflight was cancelled without accepted controller intent"
                )
                unexpected_cancel.__cause__ = error
                self.failure(unexpected_cancel)
            return False
        except _HOST_ERRORS as error:
            self.api_failure(error)
            return False
        if not isinstance(environment, EnvironmentInfo):
            raise TypeError("preflight returned an invalid environment object")
        self.environment = environment
        if not self.persist(TaskEnvironmentObserved(task_execution_value(environment))):
            return False
        problem = self.environment_problem()
        if problem is not None:
            self.persist(TaskDiagnostic(problem))
            return False
        if not self.sample_receipt():
            return False
        if self.wants_stop():
            self.accept_cancel()
            return False
        if not self.stopped():
            self.persist(
                TaskDiagnostic(
                    Error(
                        "preflight stopping remains unconfirmed; model was not started",
                        raw_type="task.preflight",
                    )
                )
            )
            return False
        return True

    async def consume(self) -> None:
        assert self.adapter is not None and self.turn is not None
        try:
            async for event in self.adapter.events(self.turn):
                self.observe(event)
        except _HOST_ERRORS as error:
            self.api_failure(error)

    async def cancel_model(self) -> None:
        assert self.adapter is not None and self.turn is not None
        try:
            await self.adapter.cancel(self.turn, on_accepted=self.accept_cancel)
        except _HOST_ERRORS as error:
            self.api_failure(error)
        except Exception as error:
            self.failure(error)
            raise

    async def model(self, waiter: asyncio.Task[None]) -> None:
        assert self.adapter is not None
        # The just-observed preflight receipt permits a full check before any
        # write turn exists. During that turn only light metadata checks run.
        if not self.stopped():
            raise ValueError("cannot start a model with unknown preflight stopping")
        self.launch.check()
        if (
            inspect_workspace(self.launch.lease, self.launch.prepared.workspace).digest
            != self.launch.prepared.initial_workspace_digest
        ):
            raise ValueError("task workspace changed before model submission")
        current = self.launch.store.load_task(self.run_id).current_attempt
        if current is None or current.number != self.launch.attempt:
            raise ValueError("task model request is no longer current")
        choice = current.request.decision.choice
        self.light_check()
        session = await self.adapter.start_session(
            SessionConfig(
                choice.model,
                choice.effort,
                Permission.WORKSPACE_WRITE,
                Path(self.launch.prepared.workspace.root),
            )
        )
        if not isinstance(session, SessionHandle):
            raise TypeError("implementation session must be an actual SessionHandle")
        if self.wants_stop():
            self.accept_cancel()
            return
        self.light_check()
        self.operation_epoch += 1
        self.turn = await self.adapter.submit_turn(session, self.launch.prepared.prompt)
        if not isinstance(self.turn, TurnHandle):
            raise TypeError("implementation turn must be an actual TurnHandle")
        stream = self.consume()
        try:
            consumer = asyncio.create_task(stream)
        except _HOST_ERRORS as error:
            self.api_failure(error)
            try:
                await self.cancel_model()
            finally:
                await stream
        except Exception as error:
            self.failure(error)
            try:
                await self.cancel_model()
            finally:
                await stream
            raise
        else:
            try:
                await asyncio.wait(
                    (consumer, waiter), return_when=asyncio.FIRST_COMPLETED
                )
                self.stop_waiter_failure(waiter)
                if self.wants_stop():
                    await self.cancel_model()
            finally:
                await asyncio.shield(consumer)
        try:
            actual = await self.adapter.result(self.turn)
            if not isinstance(actual, TurnResult):
                raise TypeError("implementation result must be an actual TurnResult")
            for event in (actual.terminal, actual.process_exit):
                if event is not None:
                    self.observe(event, supplement=True)
        except _HOST_ERRORS as error:
            self.api_failure(error)

    def scan_final(self) -> None:
        if not self.stopped():
            self.final_digest = None
            return
        try:
            self.final_digest = _boundary_call(
                lambda: (
                    inspect_workspace(
                        self.launch.lease, self.launch.prepared.workspace
                    ).digest
                )
            )
        except _HOST_ERRORS as error:
            self.final_digest = None
            self.api_failure(error)

    def control_write(self, event: TaskEvent) -> tuple[bool, bool | None]:
        """Return clean-write and durable-write facts independently."""
        envelope = self.envelope(event)
        prior = self.confirmed[task_execution_hash(envelope)]
        if self._write(envelope, require_applied=True):
            return True, True
        committed = self.confirm(_Pending(envelope, prior), applied=True)
        if committed is True and self.first_error is not None:
            self.first_error.add_note(
                f"Task event {event.kind} committed before its write raised."
            )
        return False, committed

    def reconciliation(self, *, qualified: bool) -> TaskReconciled:
        manifest = self.manifest if qualified else None
        return TaskReconciled(
            self.final_digest,
            self.stopped(),
            "runtime ownership and task execution evidence settled"
            if qualified
            else "task execution evidence is incomplete",
            manifest.path if manifest is not None else None,
            manifest.sha256 if manifest is not None else None,
        )

    def reject_evidence(self, detail: str) -> bool:
        try:
            state = self.launch.store.load_task(self.run_id)
            event: TaskEvent = (
                TaskEvidenceRejected(detail)
                if state.phase == TaskPhase.AWAITING_DELIVERY
                else TaskDiagnostic(Error(detail, raw_type="task.evidence"))
            )
            clean, _ = self.control_write(event)
            return clean
        except _HOST_ERRORS as error:
            self.api_failure(error)
            return False

    def revoke(self) -> None:
        # PLANNING_REQUIRED also accepts a fresh negative reconciliation. Never
        # describe a failed write as a successful durable revocation.
        _, committed = self.control_write(self.reconciliation(qualified=False))
        if committed is not True and self.first_error is not None:
            self.first_error.add_note(
                "Task log-receipt revocation could not be confirmed persisted; independent recovery is required."
            )
        self.reject_evidence(
            "; ".join(self.problems) or "task execution evidence is incomplete"
        )

    async def settle(self) -> None:
        self.save_pending()
        self.sample_receipt()
        try:
            if self.binding is not None:
                try:
                    self.manifest = await seal_task_execution(
                        self.binding, turn=self.turn, environment=self.environment
                    )
                    # The returned seal includes a later successful sample from
                    # this same owner. A subsequent getter error cannot erase it
                    # or restore an older, possibly contradictory stopping fact.
                    self.remember_receipt(self.manifest.receipt)
                except _HOST_ERRORS as error:
                    self.api_failure(error)
        except Exception as error:
            self.failure(error)
            raise
        finally:
            self.finish_settlement()

    def finish_settlement(self) -> None:
        # Later getter errors retain the last successful sample for this epoch.
        self.sample_receipt()
        if (
            self.manifest is not None
            and self.receipt is not None
            and task_execution_hash(self.manifest.receipt)
            != task_execution_hash(self.receipt)
        ):
            self.failure(
                ValueError("runtime ownership changed after task execution sealing")
            )
        self.scan_final()
        if (
            self.manifest is not None
            and self.final_digest != self.manifest.final_digest
        ):
            self.failure(
                ValueError(
                    "final workspace facts differ from the single execution seal"
                )
            )
        # Receipt/seal/scan failures can add new diagnostics after the stream's
        # repairs. Each of those occurrences gets one bounded write as well.
        self.save_pending()
        qualified = (
            self.first_error is None
            and self.manifest is not None
            and self.manifest.qualified
            and self.stopped()
            and self.receipt is not None
            and self.receipt_epoch == self.operation_epoch
            and task_execution_hash(self.receipt)
            == task_execution_hash(self.manifest.receipt)
            and self.final_digest is not None
        )
        try:
            self.publish(qualified)
        except Exception as error:
            self.failure(error)
            try:
                _boundary_call(self.revoke)
            except _DeferredHostFailure as revocation_error:
                self.failure(revocation_error)
                assert self.first_error is not None
                raise self.first_error
            raise

    def publish(self, qualified: bool) -> None:
        clean, _ = self.control_write(self.reconciliation(qualified=qualified))
        if not clean:
            self.revoke()
            return
        if not qualified:
            detail = (
                "; ".join(self.problems)
                or (
                    self.manifest.validation_error
                    if self.manifest is not None
                    else None
                )
                or "task execution evidence is incomplete"
            )
            self.reject_evidence(detail)
            return
        try:
            actual = inspect_task_execution(
                self.launch.store, self.run_id, self.launch.attempt, self.launch.lease
            )
            if self.manifest is None or task_execution_hash(
                actual
            ) != task_execution_hash(self.manifest):
                raise ValueError(
                    "persisted task archive differs from the just-sealed execution"
                )
            report = actual.report
            assert report is not None
            if report.outcome == "scope_blocked":
                clean, _ = self.control_write(
                    TaskScopeBlocked(report.summary, report.required_paths)
                )
                if not clean:
                    self.revoke()
        except _HOST_ERRORS as error:
            self.api_failure(error)
            self.revoke()

    async def run(self) -> None:
        waiter: asyncio.Task[None] | None = None
        try:
            self.launch.check()
            prepared = self.launch.prepared
            schema = thaw_json(prepared.output_schema)
            if not isinstance(schema, dict):
                raise TypeError("prepared task output schema must be an object")
            policy = prepared.policy
            self.adapter = ImplementationRuntimeAdapter(
                policy.provider,
                workspace=prepared.workspace,
                executable=policy.executable,
                timeout=policy.timeout,
                deadline_at=asyncio.get_running_loop().time() + policy.timeout,
                grace=policy.grace,
                log_dir=self.launch.log_dir,
                output_schema=schema,
            )
            if not self.sample_receipt():
                return
            self.binding = bind_task_execution(self.launch, self.run_id, self.adapter)
            waiter = _task(_wait_for_stop(self.intent, self.external_stop))
            if await self.preflight(waiter):
                await self.model(waiter)
        except _HOST_ERRORS as error:
            self.api_failure(error)
        except Exception as error:
            # Unexpected programming exceptions still invalidate qualification
            # before finally settles, and remain exceptions to the caller.
            self.failure(error)
            raise
        finally:
            if waiter is not None:
                self.stop_waiter_failure(waiter)
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
                self.stop_waiter_failure(waiter)
            try:
                await self.settle()
            except _HOST_ERRORS as error:
                self.api_failure(error)


@dataclass(frozen=True, slots=True)
class _Outcome:
    state: TaskState | None
    error: Exception | None


async def _execute(
    database_path: str | Path,
    task_run_id: str,
    planning_run_id: str,
    repository: Path,
    directory: Path,
    policy: TaskExecutionPolicy | None,
    dispatch_policy: TaskDispatchPolicy | None,
    unavailable: frozenset[tuple[str, str]],
    intent: asyncio.Event,
    stop: asyncio.Event | None,
) -> _Outcome:
    execution: _Execution | None = None
    state: TaskState | None = None
    try:
        if intent.is_set() or stop is not None and stop.is_set():
            database = _database(database_path)
            with PlanningStore(database.path) as store:
                return _Outcome(store.load_task(task_run_id), None)
        with prepare_task_launch(
            database_path,
            task_run_id,
            planning_run_id,
            repository=repository,
            directory=directory,
            policy=policy,
            dispatch_policy=dispatch_policy,
            unavailable=unavailable,
        ) as launch:
            execution = _Execution(launch, task_run_id, intent, stop)
            await execution.run()
            try:
                state = launch.store.load_task(task_run_id)
            except Exception as error:
                execution.failure(error)
                execution.revoke()
                raise
    except _HOST_ERRORS as error:
        if execution is not None:
            execution.failure(error)
        return _Outcome(
            state, execution.first_error if execution is not None else error
        )
    except Exception as error:
        if execution is not None:
            execution.failure(error)
            assert execution.first_error is not None
            raise execution.first_error
        raise
    return _Outcome(state, execution.first_error if execution is not None else None)


async def execute_task_attempt(
    database_path: str | Path,
    task_run_id: str,
    planning_run_id: str,
    *,
    repository: Path,
    directory: Path,
    policy: TaskExecutionPolicy | None = None,
    dispatch_policy: TaskDispatchPolicy | None = None,
    unavailable: frozenset[tuple[str, str]] = frozenset(),
    stop: asyncio.Event | None = None,
) -> TaskState:
    """Run one fresh attempt; caller cancellation waits for the owned worker."""
    intent = asyncio.Event()
    worker = _task(
        _execute(
            database_path,
            task_run_id,
            planning_run_id,
            repository,
            directory,
            policy,
            dispatch_policy,
            unavailable,
            intent,
            stop,
        )
    )
    cancelled: asyncio.CancelledError | None = None
    while True:
        try:
            outcome = await asyncio.shield(worker)
            break
        except asyncio.CancelledError as error:
            if worker.cancelled():
                raise
            if cancelled is None:
                cancelled = error
            intent.set()
        except Exception as error:
            if cancelled is not None:
                raise cancelled from error
            raise
    if cancelled is not None:
        raise cancelled from outcome.error
    if outcome.error is not None:
        raise outcome.error
    assert outcome.state is not None
    return outcome.state
