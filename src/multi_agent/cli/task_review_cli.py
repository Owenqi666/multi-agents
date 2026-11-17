"""Review a current tested candidate through the existing read-only reviewer."""

import argparse
import asyncio
import json
import signal
import sqlite3
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

from multi_agent.core import RoundState, SeatConfig, SeatState
from multi_agent.core.review_input import ExecutedReviewInput
from multi_agent.core.task_execution import prepared_for_task, task_execution_hash
from multi_agent.store.task_review_store import TaskReviewStore

from . import runner
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
        if name == "show":
            operation.add_argument("--history", action="store_true")
        elif name == "retry":
            operation.add_argument("--seat", action="append")
            operation.add_argument("--recover", action="store_true")
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


def _run(args: argparse.Namespace) -> tuple[dict[str, Any], bool]:
    database = _database(args.database)
    seats = tuple(
        SeatConfig(f"reviewer-{index}", *fields)
        for index, fields in enumerate(args.seat, 1)
    )
    runner.validate_review_settings(seats, timeout=args.timeout, grace=args.grace)
    for executable in (args.codex_bin, args.claude_bin):
        _text(executable, "review executable")
    with (
        TaskReviewStore(database.path) as store,
        executor_lock(args.repository) as lease,
    ):
        database.check()
        if _store_database(store) != database:
            raise ValueError("review database connection identity changed")
        directory = _new_root(args.directory, database, lease)
        identifier = uuid4().hex
        directory.mkdir(mode=0o700)
        try:
            with runner.execution_lock(directory):
                value = prepare_task_review_input(
                    store,
                    args.task_run_id,
                    args.implementation_attempt,
                    args.test_attempt,
                    lease,
                )
                metadata = {
                    "kind": "review",
                    "created_at": runner.now(),
                    "timeout": args.timeout,
                    "grace": args.grace,
                    "executables": {"codex": args.codex_bin, "claude": args.claude_bin},
                    "parent_round_id": None,
                    "repository": str(lease.source),
                    "directory": str(directory),
                    "directory_identity": _identity(_directory(directory)),
                }
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
                database.check()
                _check_lease(lease)
                return _view(store, identifier, lease=lease), interrupted
        except BaseException as error:
            error.add_note(
                f"Review directory retained: {directory}; inspect round {identifier} for any committed binding."
            )
            raise


def _continue(args: argparse.Namespace) -> tuple[dict[str, Any], bool]:
    database = _database(args.database)
    with TaskReviewStore(database.path) as store:
        value = _input(store, args.round_id)
        metadata = runner.metadata_for(store, args.round_id)
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
                        )
                    interrupted = asyncio.run(
                        _execute(store, identifier, directory, seat_keys)
                    )
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
            code = 130 if interrupted else 0 if result["current_eligible"] else 1
    except KeyboardInterrupt:
        print("multi-agent: task review interrupted after settlement", file=sys.stderr)
        return 130
    except (*_READ_ERRORS, RuntimeError) as error:
        print(f"multi-agent: {error}", file=sys.stderr)
        for note in getattr(error, "__notes__", ()):
            print(note, file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return code
