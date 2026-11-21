"""Minimal test run and read-only result views for a verified task candidate."""

import argparse
import asyncio
import fcntl
import json
import os
import sqlite3
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from multi_agent.core.task_execution import prepared_for_task, task_execution_hash
from multi_agent.core.task_test_events import TaskTestCommandReported
from multi_agent.core.task_test_execution import prepared_for_test
from multi_agent.core.task_test_state import TaskTestPhase
from multi_agent.core.transitions import task_test_phase
from multi_agent.store.task_test_store import TaskTestStore

from .errors import report_refusal
from .task_context import _check_lease
from .task_test_context import capture_task_test_policy
from .task_test_evidence import inspect_task_test_command
from .task_test_inputs import prepare_task_test_input
from .task_test_runner import execute_task_tests
from .workspaces import ExecutorLease


def add_parser(commands: Any) -> None:
    command = commands.add_parser(
        "task-test",
        help="run approved tests for a fixed task candidate or show results",
    )
    operations = command.add_subparsers(dest="test_operation", required=True)
    for operation in ("run", "show"):
        parser = operations.add_parser(operation)
        parser.add_argument("task_run_id")
        parser.add_argument("--database", type=Path, required=True)
        parser.add_argument("--implementation-attempt", type=int, required=True)
        if operation == "show":
            parser.add_argument("--test-attempt", type=int)
            continue
        parser.add_argument("--repository", type=Path, required=True)
        parser.add_argument("--directory", type=Path, required=True)
        parser.add_argument(
            "--command", dest="required_commands", action="append", required=True
        )
        parser.add_argument("--declaration-source", required=True)
        parser.add_argument("--codex-bin", default="codex")
        parser.add_argument("--timeout", type=float, default=180)
        parser.add_argument("--grace", type=float, default=2)
        parser.add_argument("--output-bytes-limit", type=int, default=8 * 1024 * 1024)
        parser.add_argument("--tool-directory", action="append", default=[])
        parser.add_argument("--runtime-read-path", action="append", default=[])


@contextmanager
def _existing_lease(
    store: TaskTestStore, run_id: str, implementation_attempt: int
) -> Iterator[ExecutorLease]:
    """Read-only show never creates a lock or discovers repositories through Git."""
    implementation = store.load_task(run_id)
    attempt = next(
        item
        for item in implementation.attempts
        if item.number == implementation_attempt
    )
    prepared = prepared_for_task(attempt)
    if prepared is None:
        raise ValueError("test source has no managed implementation")
    repository = Path(prepared.workspace.repository)
    source = Path(prepared.workspace.source)
    fd = os.open(
        repository / "multi-agent-executor.lock",
        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
    )
    lease = ExecutorLease(source, repository, fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("test executor lock is not a regular file")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _check_lease(lease)
        yield lease
    finally:
        lease.active = False
        os.close(fd)


def view_task_test(
    database_path: str | Path,
    task_run_id: str,
    implementation_attempt: int,
    *,
    test_attempt: int | None = None,
) -> dict[str, Any]:
    with TaskTestStore.open_readonly(
        Path(database_path).expanduser().absolute()
    ) as store:
        state = store.load_task_test(task_run_id, implementation_attempt)
        selected = (
            state.current_attempt
            if test_attempt is None
            else next(
                (item for item in state.attempts if item.number == test_attempt), None
            )
        )
        if selected is None:
            raise ValueError("test attempt does not exist")
        prepared = prepared_for_test(selected)
        value = selected.input
        commands: list[dict[str, Any]] = []
        errors: list[str] = []
        source_current = False
        verified: set[int] = set()
        if prepared is not None:
            try:
                with _existing_lease(
                    store, task_run_id, implementation_attempt
                ) as lease:
                    actual = prepare_task_test_input(
                        store,
                        task_run_id,
                        implementation_attempt,
                        lease,
                        required_commands=tuple(item.name for item in value.commands),
                        declaration_source=value.declaration_source,
                    )
                    source_current = task_execution_hash(actual) == task_execution_hash(
                        value
                    )
                    for observed_command in selected.commands:
                        try:
                            archive = inspect_task_test_command(
                                store,
                                task_run_id,
                                implementation_attempt,
                                selected.number,
                                observed_command.sequence,
                                lease,
                            )
                            if archive.qualified and source_current:
                                verified.add(observed_command.sequence)
                            elif archive.validation_error:
                                errors.append(
                                    f"command {observed_command.sequence}: {archive.validation_error}"
                                )
                        except (
                            OSError,
                            ValueError,
                            TypeError,
                            KeyError,
                            sqlite3.Error,
                        ) as error:
                            errors.append(
                                f"command {observed_command.sequence}: {error}"
                            )
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as error:
                errors.append(str(error))
        for sequence, command in enumerate(value.commands, 1):
            stored = next(
                (item for item in selected.commands if item.sequence == sequence), None
            )
            reports = [
                event
                for event in selected.events
                if isinstance(event, TaskTestCommandReported)
                and event.sequence == sequence
            ]
            report = reports[-1] if reports else None
            status = "not_run"
            if stored is not None:
                if selected.cancel_requested and sequence == len(selected.commands):
                    status = "cancelled"
                elif (
                    stored.exit_code is not None
                    and stored.exit_code != 0
                    or stored.problems
                ):
                    status = "failed"
                elif stored.exit_code == 0 and sequence in verified:
                    status = "passed"
                else:
                    status = "incomplete"
            logs: dict[str, str | None] = {"stdout": None, "stderr": None, "wire": None}
            if prepared is not None and stored is not None:
                directory = (
                    Path(prepared.context.path).parent / "logs" / f"command-{sequence}"
                )
                logs = {
                    "stdout": _visible_log(directory / "stdout.bin"),
                    "stderr": _visible_log(directory / "stderr.bin"),
                    "wire": _visible_log(directory / "wire.jsonl"),
                }
            commands.append(
                {
                    "sequence": sequence,
                    "name": command.name,
                    "status": status,
                    "reported_exit_code": report.reported_exit_code
                    if report is not None
                    else None,
                    "exit_code": stored.exit_code if stored is not None else None,
                    "verification": "verified"
                    if sequence in verified
                    else "unverified",
                    "logs": logs,
                }
            )
        return {
            "task_run_id": task_run_id,
            "implementation_attempt": implementation_attempt,
            "test_attempt": selected.number,
            "candidate": value.candidate.delivery.candidate,
            "phase": task_test_phase(selected).value,
            "verification": "verified"
            if source_current and len(verified) == len(value.commands)
            else "unverified",
            "source_current": source_current,
            "commands": commands,
            "details": {
                "is_current": selected == state.current_attempt,
                "finished": selected.finished,
                "cancel_requested": selected.cancel_requested,
                "declaration_source": value.declaration_source,
                "input_hash": value.input_hash,
                "retained_root": str(Path(prepared.context.path).parent)
                if prepared is not None
                else None,
                "stop_scope": prepared.policy.required_stop_scope
                if prepared is not None
                else None,
                "output_scope": prepared.policy.output_scope
                if prepared is not None
                else None,
                "command_group_gone": None,
                "all_descendants_stopped": None,
                "producer_stdout_eof": None,
                "producer_stderr_eof": None,
                "verification_errors": errors,
            },
        }


def _visible_log(path: Path) -> str | None:
    try:
        if path.parent.resolve(strict=True) != path.parent or not stat.S_ISREG(
            path.lstat().st_mode
        ):
            return None
        return str(path)
    except OSError:
        return None


def main(args: argparse.Namespace) -> int:
    try:
        if args.test_operation == "show":
            output = view_task_test(
                args.database,
                args.task_run_id,
                args.implementation_attempt,
                test_attempt=args.test_attempt,
            )
            code = 0
        else:
            policy = capture_task_test_policy(
                args.codex_bin,
                timeout=args.timeout,
                grace=args.grace,
                output_bytes_limit=args.output_bytes_limit,
                tool_directories=tuple(args.tool_directory),
                runtime_read_paths=tuple(args.runtime_read_path),
            )
            state = asyncio.run(
                execute_task_tests(
                    args.database,
                    args.task_run_id,
                    args.implementation_attempt,
                    repository=args.repository,
                    directory=args.directory,
                    required_commands=tuple(args.required_commands),
                    declaration_source=args.declaration_source,
                    policy=policy,
                )
            )
            output = view_task_test(
                args.database, args.task_run_id, args.implementation_attempt
            )
            code = (
                0
                if state.phase == TaskTestPhase.SUCCEEDED
                and output["verification"] == "verified"
                else 1
            )
    except KeyboardInterrupt:
        print("multi-agent: test cancelled after runtime settlement", file=sys.stderr)
        return 130
    except (
        OSError,
        UnicodeError,
        ValueError,
        TypeError,
        KeyError,
        RuntimeError,
        sqlite3.Error,
    ) as error:
        return report_refusal(error)
    print(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False))
    return code
