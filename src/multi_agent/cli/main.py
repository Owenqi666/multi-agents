"""User-input boundary for the phase-one review CLI."""

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, cast

from multi_agent.core import SeatConfig
from multi_agent.core.snapshot import FrozenSnapshot, TestEvidence
from multi_agent.store import ReviewStore

from .review_inputs import load_review_input
from .snapshot import _git, freeze_diff, freeze_repository


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(read_text(path))


def object_fields(value: object, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{name} must contain exactly {sorted(fields)}")
    return cast(dict[str, Any], value)


def argv_value(value: object) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(part, str) or "\0" in part for part in value)
        or not value[0].strip()
    ):
        raise ValueError("test commands must be nonempty argv arrays")
    return tuple(value)


def test_input(
    path: Path | None,
) -> tuple[tuple[TestEvidence, ...], tuple[tuple[str, ...], ...]]:
    if path is None:
        return (), ()
    data = object_fields(read_json(path), {"required_commands", "results"}, "tests")
    if not isinstance(data["required_commands"], list) or not isinstance(
        data["results"], list
    ):
        raise TypeError("required_commands and results must be arrays")
    required = tuple(argv_value(value) for value in data["required_commands"])
    results = []
    for raw in data["results"]:
        item = object_fields(
            raw, {"commit", "command", "exit_code", "log"}, "test result"
        )
        if not isinstance(item["commit"], str) or not isinstance(item["log"], str):
            raise TypeError("test commit and log must be strings")
        if type(item["exit_code"]) is not int:
            raise TypeError("test exit_code must be an integer")
        results.append(
            TestEvidence(
                item["commit"],
                argv_value(item["command"]),
                item["exit_code"],
                item["log"],
            )
        )
    return tuple(results), required


def data_directory(path: Path, repository: Path | None = None) -> Path:
    directory = path.expanduser().resolve()
    if repository is not None and directory.is_relative_to(repository.resolve()):
        raise ValueError("data directory must be outside the target repository")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def load_snapshot(store: ReviewStore, identifier: str) -> FrozenSnapshot:
    value = load_review_input(store, identifier)
    if not isinstance(value, FrozenSnapshot):
        raise TypeError("executed task reviews must use task-review commands")
    return value


def parser() -> argparse.ArgumentParser:
    from .task_review_cli import add_parser as add_task_review_parser
    from .task_test_cli import add_parser as add_task_test_parser

    result = argparse.ArgumentParser(
        description="Freeze and independently review committed code."
    )
    result.add_argument(
        "--data-dir", type=Path, default=Path.home() / ".local/share/multi-agent"
    )
    commands = result.add_subparsers(dest="command", required=True)
    freeze = commands.add_parser(
        "freeze", help="freeze committed inputs without running models or tests"
    )
    source = freeze.add_mutually_exclusive_group(required=True)
    source.add_argument("--repo", type=Path)
    source.add_argument("--diff", type=Path)
    freeze.add_argument("--baseline")
    freeze.add_argument("--candidate")
    freeze.add_argument("--requirements", type=Path, required=True)
    freeze.add_argument("--acceptance", action="append", required=True)
    freeze.add_argument("--tests", type=Path)
    snapshot = commands.add_parser("snapshot", help="print a stored frozen input")
    snapshot.add_argument("snapshot_id")
    run = commands.add_parser(
        "run", help="start selected real read-only reviewer sessions"
    )
    run.add_argument("snapshot_id")
    run.add_argument(
        "--seat",
        nargs=3,
        action="append",
        required=True,
        metavar=("PROVIDER", "MODEL", "EFFORT"),
    )
    run.add_argument("--timeout", type=float, default=180)
    run.add_argument("--grace", type=float, default=2)
    run.add_argument("--codex-bin", default="codex")
    run.add_argument("--claude-bin", default="claude")
    show = commands.add_parser(
        "show", help="show current reports, recovery state and evidence"
    )
    show.add_argument("round_id", nargs="?")
    show.add_argument("--history", action="store_true")
    retry = commands.add_parser(
        "retry", help="retry failed seats with their original configuration"
    )
    retry.add_argument("round_id")
    retry.add_argument("--seat", action="append")
    retry.add_argument("--recover", action="store_true")
    confirm = commands.add_parser(
        "confirm", help="one cross-confirmation round for blocking findings"
    )
    confirm.add_argument("round_id")
    benchmark = commands.add_parser(
        "benchmark", help="prepare fixed samples or score imported human judgments"
    )
    benchmark.add_argument("operation", choices=("prepare", "template", "score"))
    benchmark.add_argument("arguments", nargs=argparse.REMAINDER)
    add_task_test_parser(commands)
    add_task_review_parser(commands)
    return result


def freeze_input(args: argparse.Namespace, store: ReviewStore) -> dict[str, Any]:
    requirements = read_text(args.requirements)
    acceptance = tuple(args.acceptance)
    if args.repo is not None:
        if args.baseline is None or args.candidate is None:
            raise ValueError("--repo requires both --baseline and --candidate")
        evidence, required = test_input(args.tests)
        snapshot = freeze_repository(
            args.repo,
            args.baseline,
            args.candidate,
            requirements,
            acceptance,
            evidence,
            required_tests=required,
        )
    else:
        if (
            args.baseline is not None
            or args.candidate is not None
            or args.tests is not None
        ):
            raise ValueError("diff-only input cannot include commit or test evidence")
        snapshot = freeze_diff(read_text(args.diff), requirements, acceptance)
    store.save_snapshot(snapshot.snapshot_id, snapshot.to_dict())
    return {
        "snapshot_id": snapshot.snapshot_id,
        "repository": snapshot.repository,
        "baseline": snapshot.baseline,
        "candidate": snapshot.candidate,
        "report_only": snapshot.repository is None,
        "test_evidence_source": "user_supplied",
        "test_records": len(snapshot.tests),
        "required_commands": snapshot.required_tests,
    }


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    repository = getattr(args, "repo", None)
    if repository is not None:
        common = Path(
            _git(repository, "rev-parse", "--path-format=absolute", "--git-common-dir")
            .decode()
            .rstrip("\n")
        ).resolve()
        if args.data_dir.expanduser().resolve().is_relative_to(common):
            raise ValueError(
                "data directory must be outside the repository Git metadata"
            )
    if (
        repository is not None
        and _git(repository, "rev-parse", "--is-bare-repository").strip() == b"false"
    ):
        repository = Path(
            _git(repository, "rev-parse", "--show-toplevel").decode().rstrip("\n")
        )
    directory = data_directory(args.data_dir, repository)
    with ReviewStore(directory / "reviews.sqlite3") as store:
        if args.command == "freeze":
            return freeze_input(args, store)
        if args.command == "snapshot":
            snapshot = load_review_input(store, args.snapshot_id)
            return {"snapshot_id": snapshot.snapshot_id, "input": snapshot.to_dict()}
        from . import runner

        if args.command == "show":
            if args.round_id is None:
                return {
                    "rounds": [
                        runner.view_round(store, key) for key in store.list_reviews()
                    ]
                }
            return runner.view_round(store, args.round_id, history=args.history)
        with runner.execution_lock(directory):
            seat_keys = None
            if args.command == "run":
                runner.ensure_external(
                    directory, load_snapshot(store, args.snapshot_id)
                )
                seats = tuple(
                    SeatConfig(f"reviewer-{index}", *fields)
                    for index, fields in enumerate(args.seat, 1)
                )
                identifier = runner.new_review(
                    store,
                    args.snapshot_id,
                    seats,
                    timeout=args.timeout,
                    grace=args.grace,
                    executables={"codex": args.codex_bin, "claude": args.claude_bin},
                )
            else:
                state = store.load_round(args.round_id)
                runner.ensure_external(
                    directory, load_snapshot(store, state.snapshot_id)
                )
                if args.command == "confirm":
                    identifier = runner.new_confirmation(store, args.round_id)
                else:
                    identifier = args.round_id
                    seat_keys = runner.prepare_retry(
                        store,
                        identifier,
                        tuple(args.seat) if args.seat else None,
                        recover=args.recover,
                    )
            asyncio.run(
                runner.execute_round(store, identifier, directory, seat_keys=seat_keys)
            )
            return runner.view_round(store, identifier)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "task-review":
        from .task_review_cli import main as task_review_main

        return task_review_main(args)
    if args.command == "task-test":
        from .task_test_cli import main as task_test_main

        return task_test_main(args)
    if args.command == "benchmark":
        from .benchmark import main as benchmark_main

        return benchmark_main(
            ["--data-dir", str(args.data_dir), args.operation, *args.arguments]
        )
    try:
        output = dispatch(args)
    except (
        OSError,
        UnicodeError,
        ValueError,
        TypeError,
        KeyError,
        sqlite3.Error,
    ) as error:
        print(f"multi-agent: {error}", file=sys.stderr)
        return 2
    print(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False))
    return 0
