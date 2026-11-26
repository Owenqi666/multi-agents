"""Review a current tested candidate through the existing read-only reviewer."""

import argparse
import asyncio
import json
import signal
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from multi_agent.core import RoundState, SeatConfig, SeatState
from multi_agent.core.events import JSONValue
from multi_agent.core.review_input import ExecutedReviewInput
from multi_agent.core.task_execution import prepared_for_task, task_execution_hash
from multi_agent.store.task_review_store import TaskReviewStore

from . import runner
from .errors import report_refusal
from .review_inputs import load_review_input
from .task_context import (
    _canonical,
    _check_lease,
    _database,
    _directory,
    _identity,
    _new_root,
    _outside,
    _store_database,
    _text,
)
from .task_review_inputs import inspect_task_review_source, prepare_task_review_input
from .task_test_cli import _existing_lease
from .workspaces import ExecutorLease, executor_lock

_READ_ERRORS = (OSError, ValueError, TypeError, KeyError, sqlite3.Error)


def add_parser(commands: Any) -> None:
    parser = commands.add_parser(
        "task-review", help="review an actually tested candidate"
    )
    operations = parser.add_subparsers(dest="review_operation", required=True)
    for name in ("run", "show", "retry", "confirm"):
        operation = operations.add_parser(name)
        operation.add_argument("task_run_id" if name == "run" else "round_id")
        operation.add_argument("--database", type=Path, required=True)
        if name != "show":
            automatic = operation.add_mutually_exclusive_group()
            automatic.add_argument(
                "--auto-rework", dest="automatic_rework", action="store_true"
            )
            automatic.add_argument(
                "--no-auto-rework", dest="automatic_rework", action="store_false"
            )
            operation.set_defaults(automatic_rework=None)
        if name == "show":
            operation.add_argument("--history", action="store_true")
        elif name == "retry":
            operation.add_argument("--seat", action="append")
            operation.add_argument("--recover", action="store_true")
            operation.add_argument("--expected-attempt", type=int, action="append")
        elif name == "run":
            operation.add_argument("--implementation-attempt", type=int, required=True)
            operation.add_argument("--test-attempt", type=int, required=True)
            operation.add_argument("--repository", type=Path, required=True)
            operation.add_argument("--directory", type=Path, required=True)
            operation.add_argument(
                "--seat",
                nargs=3,
                action="append",
                required=True,
                metavar=("PROVIDER", "MODEL", "EFFORT"),
            )
            operation.add_argument("--timeout", type=float, default=180)
            operation.add_argument("--grace", type=float, default=2)
            operation.add_argument("--codex-bin", default="codex")
            operation.add_argument("--claude-bin", default="claude")


@contextmanager
def _preserve_interruption(interrupted: bool) -> Iterator[None]:
    """Keep a known interruption when subsequent checks or display fail."""
    try:
        yield
    except Exception as error:
        if interrupted:
            cancellation = KeyboardInterrupt("post-interruption verification failed")
            cancellation.add_note(
                f"Interruption was already recorded; subsequent check or result display failed: {type(error).__name__}: {error}"
            )
            raise cancellation from error
        raise


def _automatic_selection(
    metadata: dict[str, Any], override: bool | None, *, default: bool = False
) -> bool:
    stored = metadata.get("automatic_rework", default)
    if not isinstance(stored, bool):
        raise TypeError("stored automatic rework selection must be a boolean")
    if override is not None and not isinstance(override, bool):
        raise TypeError("automatic rework override must be a boolean or None")
    return stored if override is None else override


def _input(store: TaskReviewStore, identifier: str) -> ExecutedReviewInput:
    state = store.load_round(identifier)
    value = load_review_input(store, state.snapshot_id)
    if not isinstance(value, ExecutedReviewInput):
        raise TypeError("task-review requires an executed task review input")
    return value


def _origin(
    store: TaskReviewStore, value: ExecutedReviewInput, metadata: dict[str, Any]
) -> Path:
    source = value.executed_suite.prepared.checkout.input
    state = store.load_task(source.task_run_id)
    attempt = next(
        (
            item
            for item in state.attempts
            if item.number == source.implementation_attempt
        ),
        None,
    )
    prepared = prepared_for_task(attempt) if attempt is not None else None
    if prepared is None or metadata.get("repository") != prepared.workspace.source:
        raise ValueError(
            "review metadata differs from its original implementation source"
        )
    if prepared.workspace.repository != value.source.repository:
        raise ValueError("review source belongs to another repository")
    return Path(prepared.workspace.source)


def _review_directory(metadata: dict[str, Any], lease: ExecutorLease) -> Path:
    directory = _canonical(_text(metadata.get("directory"), "review directory"))
    info = _directory(directory)
    if task_execution_hash(_identity(info)) != task_execution_hash(
        metadata.get("directory_identity")
    ):
        raise ValueError("review directory identity changed")
    _outside(directory, lease)
    return directory


def _view(
    store: TaskReviewStore,
    identifier: str,
    *,
    history: bool = False,
    lease: ExecutorLease | None = None,
) -> dict[str, Any]:
    value = _input(store, identifier)
    metadata = runner.metadata_for(store, identifier)
    result = runner.view_round(store, identifier, history=history)
    assessment_round = identifier
    if metadata["kind"] == "confirmation":
        assessment_round = metadata["parent_round_id"]
        if _input(store, assessment_round).snapshot_id != value.snapshot_id:
            raise ValueError(
                "confirmation and parent review have different frozen input"
            )
        result["assessment"] = runner.view_round(store, assessment_round)["assessment"]
    elif metadata["kind"] != "review":
        raise ValueError("task-review round metadata has an unknown kind")
    source = value.executed_suite.prepared.checkout.input
    errors: list[str] = []
    verified = False

    def inspect(held: ExecutorLease) -> None:
        if _origin(store, value, metadata) != held.source:
            raise ValueError("held review lease differs from its original source")
        _review_directory(metadata, held)
        inspect_task_review_source(store, value, held)

    try:
        if lease is None:
            with _existing_lease(
                store, source.task_run_id, source.implementation_attempt
            ) as held:
                inspect(held)
        else:
            inspect(lease)
        verified = True
    except _READ_ERRORS as error:
        errors.append(str(error))
    result.update(
        task_run_id=source.task_run_id,
        implementation_attempt=source.implementation_attempt,
        test_attempt=value.executed_suite.test_attempt,
        candidate=source.candidate.delivery.candidate,
        assessment_round_id=assessment_round,
        source_current=verified,
        verification="verified" if verified else "unverified",
        current_eligible=bool(verified and result["assessment"]["status"] == "ready"),
        source_errors=errors,
    )
    return result


def view_task_review(
    database_path: str | Path, round_id: str, *, history: bool = False
) -> dict[str, Any]:
    with TaskReviewStore.open_readonly(
        Path(database_path).expanduser().absolute()
    ) as store:
        return _view(store, round_id, history=history)


async def _execute(
    store: TaskReviewStore,
    identifier: str,
    directory: Path,
    seat_keys: tuple[str, ...] | None = None,
) -> bool:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous = signal.getsignal(signal.SIGINT)
    loop.add_signal_handler(signal.SIGINT, stop.set)
    try:
        await runner.execute_round(
            store, identifier, directory, stop=stop, seat_keys=seat_keys
        )
        return stop.is_set()
    finally:
        loop.remove_signal_handler(signal.SIGINT)
        signal.signal(signal.SIGINT, previous)


def _create_review(
    database_path: str | Path,
    task_run_id: str,
    implementation_attempt: int,
    test_attempt: int,
    repository: Path,
    directory: Path,
    seats: tuple[SeatConfig, ...],
    *,
    timeout: float,
    grace: float,
    executables: dict[str, str],
    automatic_rework: bool,
    rework_from_round_id: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Create one independent review; continuation is owned by the caller."""
    automatic_rework = _automatic_selection({}, automatic_rework)
    database = _database(database_path)
    runner.validate_review_settings(seats, timeout=timeout, grace=grace)
    for executable in executables.values():
        _text(executable, "review executable")
    with (
        TaskReviewStore(database.path) as store,
        executor_lock(repository) as lease,
    ):
        database.check()
        if _store_database(store) != database:
            raise ValueError("review database connection identity changed")
        directory = _new_root(directory, database, lease)
        identifier = uuid4().hex
        directory.mkdir(mode=0o700)
        try:
            with runner.execution_lock(directory):
                value = prepare_task_review_input(
                    store, task_run_id, implementation_attempt, test_attempt, lease
                )
                metadata: dict[str, JSONValue] = {
                    "kind": "review",
                    "created_at": runner.now(),
                    "timeout": timeout,
                    "grace": grace,
                    "executables": executables,
                    "parent_round_id": None,
                    "repository": str(lease.source),
                    "directory": str(directory),
                    "directory_identity": _identity(_directory(directory)),
                    "automatic_rework": automatic_rework,
                }
                if rework_from_round_id is not None:
                    metadata["rework_from_round_id"] = rework_from_round_id
                database.check()
                store.create_task_review(
                    identifier,
                    RoundState(
                        value.snapshot_id,
                        "executed-review-v2",
                        tuple(SeatState(seat) for seat in seats),
                    ),
                    value,
                    metadata,
                )
                inspect_task_review_source(store, value, lease)
                interrupted = asyncio.run(_execute(store, identifier, directory))
                with _preserve_interruption(interrupted):
                    database.check()
                    _check_lease(lease)
                    return _view(store, identifier, lease=lease), interrupted
        except BaseException as error:
            error.add_note(
                f"Review directory retained: {directory}; inspect round {identifier} for any committed binding."
            )
            raise


def _run(args: argparse.Namespace) -> tuple[dict[str, Any], bool]:
    seats = tuple(
        SeatConfig(f"reviewer-{index}", *fields)
        for index, fields in enumerate(args.seat, 1)
    )
    automatic = _automatic_selection(
        {}, getattr(args, "automatic_rework", None), default=True
    )
    return _create_review(
        args.database,
        args.task_run_id,
        args.implementation_attempt,
        args.test_attempt,
        args.repository,
        args.directory,
        seats,
        timeout=args.timeout,
        grace=args.grace,
        executables={"codex": args.codex_bin, "claude": args.claude_bin},
        automatic_rework=automatic,
    )


def _continue(args: argparse.Namespace) -> tuple[dict[str, Any], bool]:
    database = _database(args.database)
    with TaskReviewStore(database.path) as store:
        value = _input(store, args.round_id)
        metadata = runner.metadata_for(store, args.round_id)
        parent_metadata = (
            runner.metadata_for(store, metadata["parent_round_id"])
            if metadata["kind"] == "confirmation"
            else metadata
        )
        _automatic_selection(parent_metadata, getattr(args, "automatic_rework", None))
        repository = _origin(store, value, metadata)
        with executor_lock(repository) as lease:
            database.check()
            if _store_database(store) != database:
                raise ValueError("review database connection identity changed")
            directory = _review_directory(metadata, lease)
            with runner.execution_lock(directory):
                inspect_task_review_source(store, value, lease)
                identifier = args.round_id
                seat_keys = None
                try:
                    if args.review_operation == "confirm":
                        identifier = runner.new_confirmation(store, args.round_id)
                    else:
                        seat_keys = runner.prepare_retry(
                            store,
                            identifier,
                            tuple(args.seat) if args.seat else None,
                            recover=args.recover,
                            expected_attempts=getattr(args, "expected_attempt", None),
                        )
                    interrupted = asyncio.run(
                        _execute(store, identifier, directory, seat_keys)
                    )
                    with _preserve_interruption(interrupted):
                        database.check()
                        _check_lease(lease)
                        return _view(store, identifier, lease=lease), interrupted
                except BaseException as error:
                    error.add_note(
                        f"Review records and directory retained: {directory}; round {identifier}."
                    )
                    raise


def main(args: argparse.Namespace) -> int:
    try:
        if args.review_operation == "show":
            result = view_task_review(
                args.database, args.round_id, history=args.history
            )
            code = 0
        else:
            result, interrupted = (
                _run(args) if args.review_operation == "run" else _continue(args)
            )
            from .task_review_workflow import continue_task_review

            result, code = continue_task_review(
                args.database,
                result,
                interrupted,
                override=getattr(args, "automatic_rework", None),
            )
    except KeyboardInterrupt as error:
        print("multi-agent: task review interrupted after settlement", file=sys.stderr)
        for note in getattr(error, "__notes__", ()):
            print(note, file=sys.stderr)
        return 130
    except (*_READ_ERRORS, RuntimeError) as error:
        return report_refusal(error)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return code
