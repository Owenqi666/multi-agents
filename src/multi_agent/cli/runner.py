"""Phase-one orchestration at the CLI boundary; core/store remain authoritative."""

import asyncio
import fcntl
import hashlib
import json
import math
import signal
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from uuid import uuid4

from multi_agent.adapters.base import (
    Adapter,
    EnvironmentInfo,
    Permission,
    SessionConfig,
)
from multi_agent.adapters.report_validation import REPORT_SCHEMA, validate_report
from multi_agent.adapters.runtime import RuntimeAdapter
from multi_agent.adapters.runtime_decode import RuntimeFailure
from multi_agent.core import (
    AttemptStatus,
    RoundState,
    RoundStatus,
    SeatConfig,
    SeatState,
    round_status,
)
from multi_agent.core.events import (
    AttemptStarted,
    CancelRequested,
    Completed,
    Envelope,
    Error,
    FailureCategory,
    ProcessExited,
    thaw_json,
)
from multi_agent.core.reports import assess
from multi_agent.core.review_input import (
    ExecutedReviewInput,
    IntegrationReviewInput,
    ReviewInput,
    review_source,
)
from multi_agent.store import ReviewStore
from multi_agent.store.codec import encode_envelope

from .prompt import (
    CONFIRM_PROMPT,
    CONFIRM_SCHEMA,
    CONFIRM_VERSION,
    EXECUTED_CONFIRM_PROMPT,
    EXECUTED_CONFIRM_VERSION,
    EXECUTED_REVIEW_PROMPT,
    EXECUTED_REVIEW_VERSION,
    INTEGRATION_CONFIRM_PROMPT,
    INTEGRATION_CONFIRM_VERSION,
    INTEGRATION_REVIEW_PROMPT,
    INTEGRATION_REVIEW_VERSION,
    REVIEW_PROMPT,
    REVIEW_VERSION,
    targets_for,
    validate_confirmation,
)
from .review_inputs import load_review_input
from .snapshot import materialize_review_input


def now() -> str:
    return datetime.now(UTC).isoformat()


@contextmanager
def execution_lock(directory: Path) -> Iterator[None]:
    """One controller per canonical data directory; no PID-based ownership guess."""
    with (directory.resolve() / "execution.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError(
                "another review controller is running; show remains available"
            ) from error
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def metadata_for(store: ReviewStore, identifier: str) -> dict[str, Any]:
    return cast(dict[str, Any], thaw_json(store.load_review(identifier)))


def snapshot_for(store: ReviewStore, identifier: str) -> ReviewInput:
    return load_review_input(store, identifier)


def ensure_external(directory: Path, snapshot: ReviewInput) -> None:
    snapshot = review_source(snapshot)
    if snapshot.repository is not None:
        common = Path(snapshot.repository).resolve()
        root = common.parent if common.name == ".git" else common
        if directory.resolve().is_relative_to(root):
            raise ValueError("data/log directory must be outside the target repository")


def validate_review_settings(
    seats: tuple[SeatConfig, ...], *, timeout: float, grace: float
) -> None:
    if len(seats) not in (1, 2) or len({seat.model for seat in seats}) != len(seats):
        raise ValueError("select one or two distinct models")
    for seat in seats:
        if (
            seat.provider not in ("codex", "claude")
            or not seat.model.strip()
            or not seat.effort.strip()
        ):
            raise ValueError(
                "each seat needs codex/claude, a model, and an explicit effort"
            )
        if any("\0" in text for text in (seat.model, seat.effort)):
            raise ValueError("seat fields cannot contain NUL")
    if any(not math.isfinite(number) or number <= 0 for number in (timeout, grace)):
        raise ValueError("timeout and grace must be positive finite durations")


def _review_protocol(
    snapshot: ReviewInput, *, confirmation: bool = False
) -> tuple[str, str]:
    if isinstance(snapshot, IntegrationReviewInput):
        return (
            (INTEGRATION_CONFIRM_VERSION, INTEGRATION_CONFIRM_PROMPT)
            if confirmation
            else (INTEGRATION_REVIEW_VERSION, INTEGRATION_REVIEW_PROMPT)
        )
    if isinstance(snapshot, ExecutedReviewInput):
        return (
            (EXECUTED_CONFIRM_VERSION, EXECUTED_CONFIRM_PROMPT)
            if confirmation
            else (EXECUTED_REVIEW_VERSION, EXECUTED_REVIEW_PROMPT)
        )
    return (
        (CONFIRM_VERSION, CONFIRM_PROMPT)
        if confirmation
        else (REVIEW_VERSION, REVIEW_PROMPT)
    )


def new_review(
    store: ReviewStore,
    snapshot_id: str,
    seats: tuple[SeatConfig, ...],
    *,
    timeout: float,
    grace: float,
    executables: dict[str, str],
) -> str:
    validate_review_settings(seats, timeout=timeout, grace=grace)
    snapshot = snapshot_for(store, snapshot_id)
    identifier = uuid4().hex
    store.create_review(
        identifier,
        RoundState(
            snapshot_id,
            _review_protocol(snapshot)[0],
            tuple(SeatState(seat) for seat in seats),
        ),
        {
            "kind": "review",
            "created_at": now(),
            "timeout": timeout,
            "grace": grace,
            "executables": executables,
            "parent_round_id": None,
        },
    )
    return identifier


def current_reports(state: RoundState) -> dict[str, dict[str, Any]]:
    return {
        seat.config.key: validate_report(seat.attempts[-1].result)
        for seat in state.seats
        if seat.attempts and seat.attempts[-1].status == AttemptStatus.SUCCEEDED
    }


def confirmation_seats(parent: RoundState) -> tuple[SeatConfig, ...]:
    reports = current_reports(parent)
    return tuple(
        seat.config for seat in parent.seats if targets_for(reports, seat.config.key)
    )


def new_confirmation(store: ReviewStore, parent_id: str) -> str:
    parent = store.load_round(parent_id)
    metadata = metadata_for(store, parent_id)
    if (
        metadata["kind"] != "review"
        or len(parent.seats) != 2
        or round_status(parent) != RoundStatus.READY_FOR_ASSESSMENT
    ):
        raise ValueError(
            "confirmation requires two complete independent review reports"
        )
    if store.confirmation_for(parent_id) is not None:
        raise ValueError("a confirmation round already exists; retry its failed seats")
    selected = tuple(SeatState(config) for config in confirmation_seats(parent))
    if not selected:
        raise ValueError("there are no P0/P1 findings to cross-confirm")
    identifier = uuid4().hex
    template_version, _ = _review_protocol(
        snapshot_for(store, parent.snapshot_id), confirmation=True
    )
    store.create_review(
        identifier,
        RoundState(
            parent.snapshot_id,
            template_version,
            selected,
        ),
        {
            **metadata,
            "kind": "confirmation",
            "parent_round_id": parent_id,
            "created_at": now(),
        },
        parent_id,
    )
    return identifier


def prepare_retry(
    store: ReviewStore,
    identifier: str,
    seat_keys: tuple[str, ...] | None,
    *,
    recover: bool,
) -> tuple[str, ...]:
    state = store.load_round(identifier)
    keys = {seat.config.key for seat in state.seats}
    if seat_keys is not None and (
        len(set(seat_keys)) != len(seat_keys) or not set(seat_keys) <= keys
    ):
        raise ValueError("retry seat keys must be unique existing seats")
    selected = [
        seat
        for seat in state.seats
        if (
            seat_keys is None
            and (
                not seat.attempts or seat.attempts[-1].status != AttemptStatus.SUCCEEDED
            )
        )
        or (seat_keys is not None and seat.config.key in seat_keys)
    ]
    if not selected:
        raise ValueError("no failed or unstarted seats to retry")
    executions = cast(dict[str, Any], thaw_json(store.load_execution(identifier)))
    # Validate every selected seat before changing any attempt.
    for seat in selected:
        if not seat.attempts:
            continue
        execution = executions.get(seat.config.key, {}).get(
            str(seat.attempts[-1].number), {}
        )
        if "completion_unconfirmed" in execution:
            raise ValueError(
                "attempt completion remains unconfirmed; retry and recovery are blocked; "
                "inspect its execution evidence and logs"
            )
        status = seat.attempts[-1].status
        if status == AttemptStatus.SUCCEEDED:
            raise ValueError("successful seats cannot be rerun in the same round")
        if status == AttemptStatus.CANCELLING:
            raise ValueError(
                "cancelling attempt lacks terminal evidence; recovery cannot claim cancellation"
            )
        if status == AttemptStatus.RUNNING and not recover:
            raise ValueError(
                "abandoned running attempt requires explicit --recover; orphan CLI status is unknown"
            )
    for seat in selected:
        if seat.attempts and seat.attempts[-1].status == AttemptStatus.RUNNING:
            store.apply(
                identifier,
                Envelope(
                    seat.config.key,
                    seat.attempts[-1].number,
                    Error(
                        "controller interrupted; explicit recovery; prior process exit remains unknown",
                        raw_type="cli.recovery",
                    ),
                    now(),
                ),
            )
    return tuple(seat.config.key for seat in selected)


def make_adapter(
    config: SeatConfig, log_dir: Path, metadata: dict[str, Any], schema: dict[str, Any]
) -> Adapter:
    return RuntimeAdapter(
        config.provider,
        executable=metadata["executables"][config.provider],
        timeout=metadata["timeout"],
        grace=metadata["grace"],
        log_dir=log_dir,
        output_schema=schema,
    )


def _environment_value(info: EnvironmentInfo) -> dict[str, Any]:
    return {
        "cli_version": info.cli_version,
        "logged_in": info.logged_in,
        "models": list(info.models) if info.models is not None else None,
        "model_efforts": {key: list(value) for key, value in info.model_efforts.items()}
        if info.model_efforts is not None
        else None,
    }


def _check_environment(info: EnvironmentInfo, config: SeatConfig) -> Error | None:
    if info.logged_in is False:
        return Error(
            "runtime reports no login",
            FailureCategory.AUTH_EXPIRED,
            raw_type="environment",
        )
    if info.models is not None and config.model not in info.models:
        return Error(
            "selected model is unavailable; select a new review configuration",
            FailureCategory.MODEL_UNAVAILABLE,
            raw_type="environment",
        )
    if (
        info.model_efforts is not None
        and config.model in info.model_efforts
        and config.effort not in info.model_efforts[config.model]
    ):
        return Error(
            "selected effort is unsupported for this model", raw_type="environment"
        )
    return None


def _tree_digest(directory: Path) -> str:
    values = []
    for path in sorted(directory.rglob("*")):
        name = str(path.relative_to(directory))
        if path.is_symlink():
            values.append(
                (name, "symlink", str(path.lstat().st_mode), str(path.readlink()))
            )
        elif path.is_file():
            values.append(
                (
                    name,
                    "file",
                    str(path.stat().st_mode),
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
            )
        else:
            values.append((name, "directory", str(path.stat().st_mode), ""))
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


async def execute_round(
    store: ReviewStore,
    round_id: str,
    directory: Path,
    *,
    stop: asyncio.Event | None = None,
    seat_keys: tuple[str, ...] | None = None,
) -> None:
    """Caller holds execution_lock; only selected fresh/failed attempts launch."""
    state = store.load_round(round_id)
    metadata = metadata_for(store, round_id)
    snapshot = snapshot_for(store, state.snapshot_id)
    ensure_external(directory, snapshot)
    confirmation = metadata["kind"] == "confirmation"
    template_version, prompt = _review_protocol(snapshot, confirmation=confirmation)
    if state.prompt_template_version != template_version:
        raise ValueError("unsupported frozen prompt template version")
    parent_reports = (
        current_reports(store.load_round(metadata["parent_round_id"]))
        if confirmation
        else {}
    )
    selected = [
        seat
        for seat in state.seats
        if (seat_keys is None or seat.config.key in seat_keys)
        and (not seat.attempts or seat.attempts[-1].status == AttemptStatus.FAILED)
    ]
    if not selected:
        raise ValueError("no dispatchable seats")
    signal_owned = stop is None
    stop = stop if stop is not None else asyncio.Event()
    stop_event = stop
    loop = asyncio.get_running_loop()
    previous = signal.getsignal(signal.SIGINT)
    attempts = {seat.config.key: (seat.current_attempt or 0) + 1 for seat in selected}
    for seat in selected:
        store.apply(
            round_id,
            Envelope(
                seat.config.key, attempts[seat.config.key], AttemptStarted(), now()
            ),
        )
    if signal_owned:
        loop.add_signal_handler(signal.SIGINT, stop_event.set)

    async def run_seat(seat: SeatState) -> None:
        key, number = seat.config.key, attempts[seat.config.key]
        log_dir = directory / "logs" / round_id / key / str(number)
        evidence: dict[str, Any] = {
            "started_at": now(),
            "log_dir": str(log_dir),
            "environment": None,
        }
        started = loop.time()

        def emit(event: Any) -> None:
            store.apply(round_id, Envelope(key, number, event, now()))

        try:
            with TemporaryDirectory(prefix="multi-agent-review-") as temporary:
                cwd = Path(temporary)
                materialize_review_input(snapshot, cwd)
                targets = targets_for(parent_reports, key) if confirmation else []
                if confirmation:
                    (cwd / "confirmation-targets.json").write_text(
                        json.dumps(targets, ensure_ascii=False), encoding="utf-8"
                    )
                    (cwd / "confirmation-targets.json").chmod(0o444)
                before = _tree_digest(cwd)
                adapter = make_adapter(
                    seat.config,
                    log_dir,
                    metadata,
                    CONFIRM_SCHEMA if confirmation else REPORT_SCHEMA,
                )
                environment_task = asyncio.create_task(adapter.check_environment())
                stopped = asyncio.create_task(stop_event.wait())
                cancellation_requested = False
                environment_cancelled = False
                interrupted: asyncio.CancelledError | None = None

                def cancel_environment() -> None:
                    nonlocal cancellation_requested, environment_cancelled
                    if not cancellation_requested:
                        emit(CancelRequested())
                        cancellation_requested = True
                    if (
                        not environment_task.done()
                        and not environment_task.cancelling()
                    ):
                        environment_cancelled = environment_task.cancel()

                try:
                    await asyncio.wait(
                        (environment_task, stopped), return_when=asyncio.FIRST_COMPLETED
                    )
                except asyncio.CancelledError as error:
                    interrupted = error
                if stop_event.is_set() or interrupted is not None:
                    cancel_environment()
                stopped.cancel()
                # Own one settlement future. Repeated controller cancellation
                # must not cancel the adapter again while its finally cleans up.
                settled = asyncio.gather(
                    environment_task, stopped, return_exceptions=True
                )
                while True:
                    try:
                        preflight_outcome, _ = await asyncio.shield(settled)
                        break
                    except asyncio.CancelledError as error:
                        if interrupted is None:
                            interrupted = error
                        cancel_environment()
                if stop_event.is_set():
                    cancel_environment()

                if isinstance(preflight_outcome, RuntimeFailure):
                    if preflight_outcome.event.raw_type == "adapter.cleanup_incomplete":
                        evidence["completion_unconfirmed"] = (
                            preflight_outcome.event.detail
                        )
                    else:
                        emit(preflight_outcome.event)
                        if interrupted is not None:
                            raise interrupted from preflight_outcome
                        return
                elif isinstance(preflight_outcome, BaseException):
                    if not (
                        isinstance(preflight_outcome, asyncio.CancelledError)
                        and environment_cancelled
                    ):
                        evidence["completion_unconfirmed"] = (
                            f"environment preflight {type(preflight_outcome).__name__}: "
                            f"{preflight_outcome}"
                        )
                else:
                    evidence["environment"] = _environment_value(preflight_outcome)

                if "completion_unconfirmed" in evidence:
                    if interrupted is not None:
                        raise interrupted from cast(BaseException, preflight_outcome)
                    raise ValueError(
                        "environment preflight completion remains unconfirmed; "
                        "attempt stays incomplete"
                    ) from cast(BaseException, preflight_outcome)
                if cancellation_requested:
                    emit(
                        Error(
                            "cancelled before model session launch",
                            FailureCategory.CANCELLED,
                            raw_type="cli.before_session",
                        )
                    )
                    if interrupted is not None:
                        raise interrupted
                    return
                assert isinstance(preflight_outcome, EnvironmentInfo)
                info = preflight_outcome
                if environment_error := _check_environment(info, seat.config):
                    emit(environment_error)
                    return
                session = await adapter.start_session(
                    SessionConfig(
                        seat.config.model, seat.config.effort, Permission.READ_ONLY, cwd
                    )
                )
                turn = await adapter.submit_turn(session, prompt)
                terminal: Completed | Error | None = None
                process_exit: ProcessExited | None = None

                async def cancel_when_requested() -> None:
                    await stop_event.wait()
                    await adapter.cancel(
                        turn, on_accepted=lambda: emit(CancelRequested())
                    )

                cancelling = asyncio.create_task(cancel_when_requested())
                stream = adapter.events(turn)
                try:
                    async for event in stream:
                        if isinstance(event, (Completed, Error)):
                            terminal = event
                        elif isinstance(event, ProcessExited):
                            process_exit = event
                        else:
                            emit(event)
                except BaseException:
                    await adapter.cancel(
                        turn, on_accepted=lambda: emit(CancelRequested())
                    )
                    async for _ in stream:
                        pass
                    raise
                finally:
                    # An accepted cancellation may still be sending/draining
                    # while the stream ends. Persist its decision before the
                    # terminal; only cancel a monitor still waiting for SIGINT.
                    if not stop_event.is_set():
                        cancelling.cancel()
                    outcomes = await asyncio.gather(cancelling, return_exceptions=True)
                    for outcome in outcomes:
                        if isinstance(outcome, BaseException) and not isinstance(
                            outcome, asyncio.CancelledError
                        ):
                            raise outcome
                if process_exit is None and (
                    terminal is None
                    or isinstance(terminal, Error)
                    and terminal.raw_type == "adapter.cleanup_incomplete"
                ):
                    evidence["completion_unconfirmed"] = (
                        terminal.detail
                        if isinstance(terminal, Error)
                        else "no terminal or process-exit evidence"
                    )
                    raise ValueError(
                        "runtime termination remains unconfirmed; attempt stays incomplete"
                    )
                if terminal is None:
                    terminal = Error(
                        "adapter stream ended without terminal evidence",
                        raw_type="cli.invalid_stream",
                    )
                if isinstance(terminal, Completed):
                    try:
                        normalized = (
                            validate_confirmation(terminal.result, targets)
                            if confirmation
                            else validate_report(terminal.result)
                        )
                    except (ValueError, TypeError) as error:
                        terminal = Error(
                            f"invalid structured report: {error}",
                            raw_type=terminal.raw_type,
                        )
                    else:
                        terminal = replace(terminal, result=normalized)
                evidence["workspace_unchanged"] = before == _tree_digest(cwd)
                if not evidence["workspace_unchanged"]:
                    terminal = Error(
                        "read-only review workspace changed",
                        FailureCategory.OUT_OF_SCOPE_PATCH,
                        raw_type="cli.workspace_check",
                    )
                emit(terminal)
                if process_exit is not None:
                    emit(process_exit)
        finally:
            evidence.update(finished_at=now(), elapsed_seconds=loop.time() - started)
            store.set_execution(round_id, key, number, evidence)

    tasks = [asyncio.create_task(run_seat(seat)) for seat in selected]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        stop_event.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    finally:
        if signal_owned:
            loop.remove_signal_handler(signal.SIGINT)
            signal.signal(signal.SIGINT, previous)


def view_round(
    store: ReviewStore, identifier: str, *, history: bool = False
) -> dict[str, Any]:
    state = store.load_round(identifier)
    metadata = metadata_for(store, identifier)
    seats = []
    for seat in state.seats:
        attempts = [
            {
                "number": attempt.number,
                "status": attempt.status,
                "failure_category": attempt.failure_category,
                "detail": attempt.detail,
                "retry_after": attempt.retry_after,
                "session_id": attempt.session_id,
                "reported_model": attempt.reported_model,
                "reported_effort": attempt.reported_effort,
                "reported_model_source": {
                    "event_type": attempt.reported_model_source.event_type,
                    "raw_type": attempt.reported_model_source.raw_type,
                }
                if attempt.reported_model_source is not None
                else None,
                "reported_effort_source": {
                    "event_type": attempt.reported_effort_source.event_type,
                    "raw_type": attempt.reported_effort_source.raw_type,
                }
                if attempt.reported_effort_source is not None
                else None,
                "reported_model_status": "reported"
                if attempt.reported_model is not None
                else "未确认",
                "reported_effort_status": "reported"
                if attempt.reported_effort is not None
                else "未确认",
                "result": thaw_json(attempt.result),
                "exit_code": attempt.exit_code,
            }
            for attempt in seat.attempts
        ]
        seats.append(
            {
                "key": seat.config.key,
                "provider": seat.config.provider,
                "requested_model": seat.config.model,
                "requested_effort": seat.config.effort,
                "current_attempt": seat.current_attempt,
                "attempts": attempts,
            }
        )
    result: dict[str, Any] = {
        "round_id": identifier,
        "snapshot_id": state.snapshot_id,
        "prompt_template_version": state.prompt_template_version,
        "metadata": metadata,
        "status": round_status(state),
        "seats": seats,
        "execution": thaw_json(store.load_execution(identifier)),
        "assessment": None,
    }
    if metadata["kind"] == "review":
        confirmations: list[dict[str, Any]] = []
        child_id = store.confirmation_for(identifier)
        missing_targets: list[dict[str, Any]] = []
        child_complete = True
        reports = current_reports(state)
        if child_id is not None:
            child = store.load_round(child_id)
            child_complete = round_status(child) == RoundStatus.READY_FOR_ASSESSMENT
            for seat in child.seats:
                targets = targets_for(reports, seat.config.key)
                answers = []
                if (
                    seat.attempts
                    and seat.attempts[-1].status == AttemptStatus.SUCCEEDED
                ):
                    answers = validate_confirmation(seat.attempts[-1].result, targets)[
                        "confirmations"
                    ]
                confirmations.extend(
                    {"seat": seat.config.key, **item} for item in answers
                )
                answered = {
                    (item["target_seat"], item["finding_id"]) for item in answers
                }
                missing_targets.extend(
                    {
                        "seat": seat.config.key,
                        "target_seat": item["target_seat"],
                        "finding_id": item["finding_id"],
                    }
                    for item in targets
                    if (item["target_seat"], item["finding_id"]) not in answered
                )
        assessment = assess(
            snapshot_for(store, state.snapshot_id),
            reports,
            tuple(seat.config.key for seat in state.seats),
            tuple(confirmations),
        )
        if child_id is not None and not child_complete:
            assessment["status"] = "incomplete"
            assessment["reasons"].append("confirmation_round_incomplete")
        elif missing_targets:
            assessment["status"] = "user_decision"
            assessment["reasons"].append("confirmation_targets_unanswered")
        result.update(
            assessment=assessment,
            confirmation_round_id=child_id,
            confirmations=confirmations,
            unanswered_confirmations=missing_targets,
        )
    if state.prompt_template_version in (
        EXECUTED_REVIEW_VERSION,
        EXECUTED_CONFIRM_VERSION,
        INTEGRATION_REVIEW_VERSION,
        INTEGRATION_CONFIRM_VERSION,
    ):
        result.update(
            assessment_scope="historical_frozen_input",
            source_current=None,
            verification="unverified",
            current_eligible=False,
        )
    if history:
        result["history"] = [
            {
                "envelope": json.loads(encode_envelope(envelope)),
                "disposition": disposition,
            }
            for envelope, disposition in store.load_history(identifier)
        ]
    return result
