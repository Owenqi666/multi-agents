"""Execute one newly reserved planning attempt through the public runtime APIs.

The controller lease serializes cooperative callers. It is never evidence that
an older process stopped. Only the adapter which owned an operation can provide
that fact, and an interrupted database write deliberately leaves it unknown.
"""

import asyncio
import fcntl
import json
import os
import sqlite3
import stat
from collections import Counter
from collections.abc import Coroutine, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from multi_agent.adapters.base import (
    EnvironmentInfo,
    Permission,
    SessionConfig,
    TurnHandle,
)
from multi_agent.adapters.ownership import RuntimeOwnershipReceipt
from multi_agent.adapters.runtime import RuntimeAdapter
from multi_agent.adapters.runtime_decode import RuntimeFailure
from multi_agent.core.events import (
    AdapterEvent,
    CancelRequested,
    Completed,
    Error,
    FailureCategory,
    ProcessExited,
)
from multi_agent.core.planning import (
    PlanningAttempt,
    PlanningPhase,
    PlanningState,
    planning_hash,
    planning_json,
    planning_value,
)
from multi_agent.core.planning_events import (
    PlanningDiagnostic,
    PlanningDraftValidated,
    PlanningEnvelope,
    PlanningEnvironmentObserved,
    PlanningEvent,
    PlanningEvidenceRejected,
    PlanningExecutionPrepared,
    PlanningReconciled,
    PlanningReviewValidated,
    PlanningValidationRejected,
)
from multi_agent.core.planning_execution import PlanningExecutionPolicy, prepared_for
from multi_agent.store.planning_store import PlanningStore

from .planning_context import inspect_planning_context, prepare_planning_attempt
from .planning_evidence import (
    PlanningExecutionManifest,
    PlanningLogDirectory,
    inspect_planning_execution,
    prepare_planning_logs,
    seal_planning_execution,
)
from .planning_inputs import parse_planning_draft, parse_planning_review

_BOUNDARY_ERRORS = (
    OSError,
    ValueError,
    TypeError,
    LookupError,
    RuntimeError,
    sqlite3.Error,
)


def _task[T](coroutine: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
    try:
        return asyncio.create_task(coroutine)
    except BaseException:
        coroutine.close()
        raise


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _database_path(value: str | Path) -> Path:
    path = Path(value).expanduser().absolute()
    if path.resolve(strict=True) != path or not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("planning database must be an existing canonical regular file")
    return path


@dataclass(frozen=True, slots=True)
class _Lease:
    database: Path
    database_identity: tuple[int, int]
    parent_identity: tuple[int, int]
    path: Path
    fd: int

    def check(self) -> None:
        held = os.fstat(self.fd)
        current = self.path.lstat()
        database = self.database.lstat()
        parent = self.database.parent.lstat()
        if (
            not stat.S_ISREG(current.st_mode)
            or _identity(current) != _identity(held)
            or not stat.S_ISREG(database.st_mode)
            or _identity(database) != self.database_identity
            or not stat.S_ISDIR(parent.st_mode)
            or _identity(parent) != self.parent_identity
            or self.database.resolve(strict=True) != self.database
        ):
            raise ValueError("planning controller lease or database identity changed")


@contextmanager
def _controller_lease(database: Path) -> Iterator[_Lease]:
    path = database.parent / ".planning-controller.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("planning controller lock must be a regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError(
                "another planning controller holds this data parent"
            ) from error
        lease = _Lease(
            database,
            _identity(database.lstat()),
            _identity(database.parent.lstat()),
            path,
            fd,
        )
        lease.check()
        yield lease
    finally:
        # Closing the held fd releases only this lease; never unlink its name.
        os.close(fd)


def _root_in_parent(
    state: PlanningState, database: Path, directory: Path | None
) -> None:
    prepared = [
        item
        for stage in ("draft", "review")
        for attempt in state.attempts(stage)
        if (item := prepared_for(attempt)) is not None
    ]
    root = Path(prepared[0].run_owner.root) if prepared else directory
    if root is None:
        raise ValueError("first planning execution requires its new run directory")
    root = root.expanduser().absolute()
    if (
        root.parent.resolve(strict=True) != database.parent
        or root.parent != database.parent
        or root == database
        or root.name == ".planning-controller.lock"
    ):
        raise ValueError(
            "planning root and database must be siblings in the data parent"
        )


def _ensure_no_unknown(store: PlanningStore) -> None:
    for run_id in store.list_recoverable_planning():
        state = store.load_planning(run_id)
        for stage in ("draft", "review"):
            for attempt in state.attempts(stage):
                if prepared_for(attempt) is not None and not attempt.stopped:
                    raise ValueError(
                        f"planning execution has unknown stopping: {run_id}/{stage}/{attempt.number}"
                    )


def _review_prerequisite(store: PlanningStore, run_id: str, stage: str) -> None:
    if stage != "review":
        return
    draft = store.load_planning(run_id).current("draft")
    if (
        draft is None
        or not inspect_planning_execution(
            store, run_id, "draft", draft.number
        ).qualified
    ):
        raise ValueError("review requires the current draft's verified execution")


def _output_schema(prepared: PlanningExecutionPrepared) -> dict[str, Any]:
    path = Path(prepared.attempt_context.path) / "output-schema.json"
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "r", encoding="utf-8") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("frozen output schema must be a regular file")
        value: Any = json.load(source)
    if not isinstance(value, dict):
        raise TypeError("frozen output schema must be an object")
    return value


def _environment_problem(
    environment: EnvironmentInfo, attempt: PlanningAttempt
) -> Error | None:
    request = attempt.request.request
    if environment.logged_in is False:
        return Error(
            "runtime reports no login",
            FailureCategory.AUTH_EXPIRED,
            raw_type="environment",
        )
    if environment.models is not None and request.model not in environment.models:
        return Error(
            "selected planning model is unavailable",
            FailureCategory.MODEL_UNAVAILABLE,
            raw_type="environment",
        )
    if (
        environment.model_efforts is not None
        and request.model in environment.model_efforts
        and request.effort not in environment.model_efforts[request.model]
    ):
        return Error("selected planning effort is unsupported", raw_type="environment")
    return None


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


@dataclass(slots=True)
class _AttemptExecution:
    store: PlanningStore
    run_id: str
    stage: str
    attempt: PlanningAttempt
    prepared: PlanningExecutionPrepared
    lease: _Lease
    intent: asyncio.Event
    external_stop: asyncio.Event | None
    first_error: Exception | None = None
    problems: list[str] = field(default_factory=list)
    adapter: RuntimeAdapter | None = None
    logs: PlanningLogDirectory | None = None
    environment: EnvironmentInfo | None = None
    turn: TurnHandle | None = None
    terminal: Completed | Error | None = None
    process_exit: ProcessExited | None = None
    accepted_cancel: PlanningEnvelope | None = None
    cancellation_persisted: bool = False
    database_failed: bool = False
    pending: list[tuple[PlanningEnvelope, int | None]] = field(default_factory=list)
    confirmed_reports: Counter[str] = field(default_factory=Counter)

    def wants_stop(self) -> bool:
        return self.intent.is_set() or (
            self.external_stop is not None and self.external_stop.is_set()
        )

    def envelope(self, event: PlanningEvent) -> PlanningEnvelope:
        return PlanningEnvelope(self.stage, self.attempt.number, event, _now())

    def failure(self, error: Exception) -> None:
        if self.first_error is None:
            self.first_error = error
        self.problems.append(f"{type(error).__name__}: {error}")
        self.intent.set()

    def persist(self, event: PlanningEvent) -> bool:
        try:
            self.store.apply_planning(self.run_id, self.envelope(event))
        except _BOUNDARY_ERRORS as error:
            self.database_failed = True
            self.failure(error)
            return False
        return True

    def inspect(self) -> None:
        self.lease.check()
        actual = inspect_planning_context(
            self.store, self.run_id, self.stage, self.attempt.number
        )
        if planning_hash(actual) != planning_hash(self.prepared):
            raise ValueError("newly reserved planning context changed")

    def accept_cancel(self) -> None:
        # RuntimeAdapter invokes this synchronously after acceptance, before I/O.
        self.accepted_cancel = self.envelope(CancelRequested())
        try:
            self.store.apply_planning(self.run_id, self.accepted_cancel)
        except _BOUNDARY_ERRORS as error:
            self.database_failed = True
            self.failure(error)
            raise
        self.cancellation_persisted = True

    def repair_cancel(self) -> bool:
        if self.accepted_cancel is None or self.cancellation_persisted:
            return True
        try:
            current = self.store.load_planning(self.run_id).current(self.stage)
            if current is None or current.number != self.attempt.number:
                raise ValueError("cancelled planning attempt is no longer current")
            if not any(isinstance(event, CancelRequested) for event in current.events):
                self.store.apply_planning(self.run_id, self.accepted_cancel)
            self.cancellation_persisted = True
        except _BOUNDARY_ERRORS as error:
            self.failure(error)
            return False
        return True

    def stop_waiter_failure(self, task: asyncio.Task[None]) -> None:
        if task.done() and not task.cancelled():
            try:
                task.result()
            except _BOUNDARY_ERRORS as error:
                self.failure(error)

    async def preflight(self, stop_waiter: asyncio.Task[None]) -> bool:
        assert self.adapter is not None
        deadline = self.adapter.deadline_at
        assert deadline is not None
        task = _task(self.adapter.check_environment())
        # check_environment registers its operation before its first await. A
        # stop after reservation must not cancel a never-started coroutine.
        await asyncio.sleep(0)
        await asyncio.wait((task, stop_waiter), return_when=asyncio.FIRST_COMPLETED)
        self.stop_waiter_failure(stop_waiter)
        if (
            not task.done()
            and self.wants_stop()
            and asyncio.get_running_loop().time() < deadline
            and not task.cancelling()
        ):
            task.cancel()
        try:
            self.environment = await asyncio.shield(task)
        except RuntimeFailure as error:
            self.persist(PlanningDiagnostic(error.event))
            return False
        except asyncio.CancelledError:
            self.accept_cancel()
            return False
        except _BOUNDARY_ERRORS as error:
            self.failure(error)
            return False
        if not self.persist(
            PlanningEnvironmentObserved(planning_value(self.environment))
        ):
            return False
        problem = _environment_problem(self.environment, self.attempt)
        if problem is not None:
            self.persist(PlanningDiagnostic(problem))
            return False
        if self.wants_stop():
            self.accept_cancel()
            return False
        return True

    def observe(self, event: AdapterEvent) -> None:
        if isinstance(event, (Completed, Error)):
            if self.terminal is not None and planning_hash(
                self.terminal
            ) != planning_hash(event):
                self.failure(ValueError("runtime stream reports conflicting terminals"))
            else:
                self.terminal = event
        elif isinstance(event, ProcessExited):
            if self.process_exit is not None and planning_hash(
                self.process_exit
            ) != planning_hash(event):
                self.failure(ValueError("runtime stream reports conflicting exits"))
            else:
                self.process_exit = event
        else:
            envelope = self.envelope(event)
            if self.database_failed:
                self.pending.append((envelope, None))
                return
            digest = planning_hash(envelope)
            try:
                self.store.apply_planning(self.run_id, envelope)
            except _BOUNDARY_ERRORS as error:
                self.pending.append((envelope, self.confirmed_reports[digest]))
                self.database_failed = True
                self.failure(error)
            else:
                self.confirmed_reports[digest] += 1

    async def consume(self) -> None:
        assert self.adapter is not None and self.turn is not None
        try:
            async for event in self.adapter.events(self.turn):
                self.observe(event)
        except _BOUNDARY_ERRORS as error:
            # For RuntimeAdapter the iterator's final await has already joined
            # its task, including a Recorder.close exception after final events.
            self.failure(error)

    async def cancel_model(self) -> None:
        assert self.adapter is not None and self.turn is not None
        try:
            await self.adapter.cancel(self.turn, on_accepted=self.accept_cancel)
        except _BOUNDARY_ERRORS as error:
            self.failure(error)

    async def model(self, stop_waiter: asyncio.Task[None]) -> None:
        assert self.adapter is not None
        self.inspect()
        request = self.attempt.request.request
        session = await self.adapter.start_session(
            SessionConfig(
                request.model,
                request.effort,
                Permission.READ_ONLY,
                Path(self.prepared.attempt_context.path),
            )
        )
        if self.wants_stop():
            self.accept_cancel()
            return
        self.inspect()
        self.turn = await self.adapter.submit_turn(session, self.attempt.request.prompt)
        stream = self.consume()
        try:
            consumer = asyncio.create_task(stream)
        except _BOUNDARY_ERRORS as error:
            self.failure(error)
            await self.cancel_model()
            # submit_turn already owns a live operation. Join this same public
            # stream even when a custom task factory refuses our consumer task.
            await stream
        else:
            await asyncio.wait(
                (consumer, stop_waiter), return_when=asyncio.FIRST_COMPLETED
            )
            self.stop_waiter_failure(stop_waiter)
            if self.wants_stop():
                await self.cancel_model()
            await asyncio.shield(consumer)
        result = await self.adapter.result(self.turn)
        for event in (result.terminal, result.process_exit):
            if event is not None:
                self.observe(event)

    def save_runtime(self) -> None:
        if not self.repair_cancel():
            if self.process_exit is not None:
                self.persist(self.process_exit)
            return
        # Preserve runtime order for nonterminal events retained after a database
        # failure, then the terminal and actual model process exit independently.
        self.save_pending()
        for event in (self.terminal, self.process_exit):
            if event is not None:
                if (
                    isinstance(event, Error)
                    and event.raw_type == "adapter.cleanup_incomplete"
                    and self.process_exit is None
                ):
                    self.persist(PlanningDiagnostic(event))
                else:
                    self.persist(event)

    def save_pending(self) -> None:
        if not self.pending:
            return
        committed: Counter[str] = Counter()
        if any(prior_count is not None for _, prior_count in self.pending):
            try:
                committed = Counter(
                    planning_hash(envelope)
                    for envelope, _ in self.store.load_planning_history(self.run_id)
                )
            except _BOUNDARY_ERRORS as error:
                self.failure(error)
                return
        for envelope, prior_count in self.pending:
            # An apply may commit and then raise. Compare the entire original
            # envelope's occurrence count to the writes already confirmed here.
            # Fresh reservation means no model reports predate this controller.
            # Unattempted reports stay distinct even with equal clock readings.
            if (
                prior_count is not None
                and committed[planning_hash(envelope)] > prior_count
            ):
                continue
            try:
                self.store.apply_planning(self.run_id, envelope)
            except _BOUNDARY_ERRORS as error:
                self.failure(error)
                return

    def reject_evidence(self, detail: str) -> None:
        try:
            state = self.store.load_planning(self.run_id)
            if state.phase == PlanningPhase.AWAITING_VALIDATION:
                self.persist(PlanningEvidenceRejected(detail))
            else:
                self.persist(
                    PlanningDiagnostic(Error(detail, raw_type="planning.evidence"))
                )
        except _BOUNDARY_ERRORS as error:
            self.failure(error)

    def validate(self) -> None:
        assert isinstance(self.terminal, Completed)
        state = self.store.load_planning(self.run_id)
        if state.phase != PlanningPhase.AWAITING_VALIDATION:
            return
        try:
            text = planning_json(self.terminal.result)
            if self.stage == "draft":
                event: PlanningEvent = PlanningDraftValidated(
                    parse_planning_draft(text, state.planning_input)
                )
            else:
                if state.card is None:
                    raise ValueError("review has no current validated draft")
                event = PlanningReviewValidated(parse_planning_review(text, state.card))
        except (ValueError, TypeError, KeyError) as error:
            self.persist(PlanningValidationRejected(str(error)))
        else:
            self.persist(event)

    async def settle(self) -> None:
        assert self.adapter is not None
        self.save_runtime()
        receipt: RuntimeOwnershipReceipt | None = None
        manifest: PlanningExecutionManifest | None = None
        try:
            receipt = self.adapter.ownership_receipt()
        except _BOUNDARY_ERRORS as error:
            self.failure(error)
        try:
            self.inspect()
            if self.logs is not None:
                manifest = await seal_planning_execution(
                    self.store,
                    self.run_id,
                    self.stage,
                    self.attempt.number,
                    logs=self.logs,
                    adapter=self.adapter,
                    turn=self.turn,
                    environment=self.environment,
                )
                self.inspect()
        except _BOUNDARY_ERRORS as error:
            self.failure(error)
        try:
            receipt = self.adapter.ownership_receipt()
        except _BOUNDARY_ERRORS as error:
            receipt = None
            self.failure(error)
        if (
            manifest is not None
            and receipt is not None
            and planning_hash(receipt) != planning_hash(manifest.receipt)
        ):
            self.failure(
                ValueError("runtime ownership changed after execution sealing")
            )
        qualified = (
            not self.problems
            and receipt is not None
            and receipt.processes_stopped
            and receipt.resources_released
            and receipt.logs_sealed
            and receipt.outputs_complete
            and manifest is not None
            and manifest.qualified
        )
        self.persist(
            PlanningReconciled(
                receipt.processes_stopped if receipt is not None else False,
                "runtime ownership and execution evidence settled"
                if qualified
                else "planning execution evidence is incomplete",
                manifest.path if qualified and manifest is not None else None,
                manifest.sha256 if qualified and manifest is not None else None,
            )
        )
        if qualified and not self.problems:
            try:
                inspect_planning_execution(
                    self.store, self.run_id, self.stage, self.attempt.number
                )
                self.validate()
            except _BOUNDARY_ERRORS as error:
                self.failure(error)
                self.reject_evidence("; ".join(self.problems))
        else:
            self.reject_evidence(
                "; ".join(self.problems) or "planning execution evidence is incomplete"
            )

    async def run(self) -> None:
        stop_waiter = _task(_wait_for_stop(self.intent, self.external_stop))
        try:
            self.inspect()
            self.logs = prepare_planning_logs(
                self.store, self.run_id, self.stage, self.attempt.number
            )
            schema = _output_schema(self.prepared)
            self.inspect()
            request = self.attempt.request.request
            policy = self.prepared.policy
            self.adapter = RuntimeAdapter(
                request.provider,
                executable=policy.for_provider(request.provider).executable,
                timeout=policy.timeout,
                deadline_at=asyncio.get_running_loop().time() + policy.timeout,
                grace=policy.grace,
                log_dir=Path(self.logs.path),
                output_schema=schema,
                claude_planning_draft=self.stage == "draft",
            )
            if await self.preflight(stop_waiter):
                await self.model(stop_waiter)
        except _BOUNDARY_ERRORS as error:
            self.failure(error)
        finally:
            self.stop_waiter_failure(stop_waiter)
            stop_waiter.cancel()
            await asyncio.gather(stop_waiter, return_exceptions=True)
            self.stop_waiter_failure(stop_waiter)
            if self.adapter is not None:
                try:
                    await self.settle()
                except _BOUNDARY_ERRORS as error:
                    self.failure(error)


@dataclass(frozen=True, slots=True)
class _Outcome:
    state: PlanningState | None
    error: Exception | None


async def _execute(
    database_path: str | Path,
    run_id: str,
    stage: str,
    repository: Path | None,
    directory: Path | None,
    policy: PlanningExecutionPolicy | None,
    intent: asyncio.Event,
    stop: asyncio.Event | None,
) -> _Outcome:
    execution: _AttemptExecution | None = None
    result: PlanningState | None = None
    try:
        database = _database_path(database_path)
        with _controller_lease(database) as lease, PlanningStore(database) as store:
            state = store.load_planning(run_id)
            if intent.is_set() or stop is not None and stop.is_set():
                return _Outcome(state, None)
            _root_in_parent(state, database, directory)
            _ensure_no_unknown(store)
            _review_prerequisite(store, run_id, stage)
            lease.check()
            prepared = prepare_planning_attempt(
                store,
                run_id,
                stage,
                repository=repository,
                directory=directory,
                policy=policy,
            )
            attempt = store.load_planning(run_id).current(stage)
            if attempt is None or prepared_for(attempt) != prepared:
                raise ValueError("planning reservation is no longer current")
            execution = _AttemptExecution(
                store, run_id, stage, attempt, prepared, lease, intent, stop
            )
            await execution.run()
            lease.check()
            result = store.load_planning(run_id)
    except _BOUNDARY_ERRORS as error:
        return _Outcome(
            result,
            execution.first_error
            if execution is not None and execution.first_error is not None
            else error,
        )
    return _Outcome(result, execution.first_error if execution is not None else None)


async def execute_planning_attempt(
    database_path: str | Path,
    run_id: str,
    stage: str,
    *,
    repository: Path | None = None,
    directory: Path | None = None,
    policy: PlanningExecutionPolicy | None = None,
    stop: asyncio.Event | None = None,
) -> PlanningState:
    """Execute one fresh attempt; defer caller cancellation until real settlement.

    Only this outer waiter receives caller cancellations. Repeated cancellation
    never reaches the worker, its preflight task, or its single event consumer.
    """
    intent = asyncio.Event()
    worker = _task(
        _execute(
            database_path, run_id, stage, repository, directory, policy, intent, stop
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
    if cancelled is not None:
        raise cancelled from outcome.error
    if outcome.error is not None:
        raise outcome.error
    assert outcome.state is not None
    return outcome.state
