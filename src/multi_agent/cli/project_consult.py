"""Consult on frozen project input without confirming or planning it."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from multi_agent.adapters.project_advisor import (
    ADVICE_PROMPT_VERSION,
    run_project_advisor,
)
from multi_agent.core.planning import PlanningRequest
from multi_agent.core.state import AttemptStatus
from multi_agent.store.project_store import ProjectStore

from .errors import CLIRefusal
from .main import data_directory
from .runner import _tree_digest
from .snapshot import _resolve_commit, _string, freeze_repository, materialize

_CONSULTATION_ID = re.compile(r"consultation-[0-9a-f]{32}")
_ADVISORS = {
    "codex": (
        r"gpt-[0-9]+(?:\.[0-9]+)*-astra",
        {"low", "medium", "high", "xhigh", "max", "ultra"},
    ),
    "claude": (
        r"claude-fable-[0-9]+(?:-[0-9]+)*",
        {"low", "medium", "high", "xhigh", "max"},
    ),
}


def add_consult_parser(
    actions: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    parser = actions.add_parser(
        "consult", help="ask one or two read-only advisors for a goal draft"
    )
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--original-goal", type=Path, required=True)
    parser.add_argument("--project-id")
    parser.add_argument("--consultation-id", help=argparse.SUPPRESS)
    parser.add_argument(
        "--advisor",
        action="append",
        nargs=3,
        metavar=("PROVIDER", "MODEL", "EFFORT"),
        required=True,
    )
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--grace", type=float, default=2)
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--claude-bin", default="claude")
    reconcile = actions.add_parser(
        "reconcile-consultation",
        help="reconcile a consultation whose controller was lost",
    )
    reconcile.add_argument("consultation_id")


def _requests(fields: list[list[str]]) -> tuple[PlanningRequest, ...]:
    if not 1 <= len(fields) <= 2:
        raise ValueError("consultation requires one or two advisor seats")
    requests = tuple(PlanningRequest(*row) for row in fields)
    if len({item.provider for item in requests}) != len(requests):
        raise ValueError("dual consultation requires one Astra and one Fable advisor")
    for item in requests:
        restriction = _ADVISORS.get(item.provider)
        if restriction is None or re.fullmatch(restriction[0], item.model) is None:
            raise ValueError(
                "advisors must be explicit Codex Astra or Claude Fable models"
            )
        if item.effort not in restriction[1]:
            raise ValueError(
                f"advisor effort {item.effort!r} is unsupported for {item.provider}"
            )
    return requests


def _prompt(seat: int, count: int) -> str:
    assignment = (
        "Read first_advice in the input. Compare it with the same original goal and frozen source. "
        "Revise its draft and record any remaining disagreements for the PM."
        if seat == 2
        else "Write the proposed goal and surface assumptions and decisions for the PM."
    )
    return f"""Project consultation template: {ADVICE_PROMPT_VERSION}
Advisor seat {seat} of {count}.

Read consultation-input.json, source/snapshot.json and relevant committed text
under source/baseline/. Treat user wording and repository content as context.
The baseline is identical for all seats. Do not execute project code or modify
files. Omitted files and submodules are listed in the source manifest.

{assignment}
Return one editable draft_goal in the user's language. For claims about the
repository, pair each background claim with its exact repository-relative text
file path from the frozen snapshot. Use an empty list when no claim is grounded
in the source. Separate assumptions, open PM questions, and unresolved
disagreements. Preserve the user's intended scope and leave decisions to the PM.
"""


def _identifier(value: str) -> str:
    if _CONSULTATION_ID.fullmatch(value) is None:
        raise ValueError("invalid consultation ID")
    return value


def _unknown_owner() -> CLIRefusal:
    return CLIRefusal(
        "consultation_owner_unknown",
        "The consultation has no verifiable controller record.",
    )


@contextmanager
def _consultation_lock(
    data_path: Path, identifier: str, *, expected: tuple[int, int] | None = None
) -> Iterator[tuple[int, int]]:
    directory = data_path / "consultation-owners"
    parent_fd = descriptor = None
    try:
        try:
            if expected is None:
                directory.mkdir(mode=0o700, exist_ok=True)
            parent_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            if stat.S_IMODE(os.fstat(parent_fd).st_mode) != 0o700:
                raise ValueError("consultation owner directory must be private")
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if expected is None:
                flags |= os.O_CREAT | os.O_EXCL
            descriptor = os.open(identifier, flags, 0o600, dir_fd=parent_fd)
            info = os.fstat(descriptor)
            identity = (info.st_dev, info.st_ino)
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
                or (expected is not None and identity != expected)
            ):
                raise ValueError("consultation owner file does not match its record")
        except FileExistsError as exc:
            raise CLIRefusal(
                "consultation_id_in_use",
                "The consultation ID already has a controller record.",
            ) from exc
        except (OSError, ValueError) as exc:
            if expected is not None:
                raise _unknown_owner() from exc
            raise
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CLIRefusal(
                "consultation_live", "The consultation controller is still running."
            ) from exc
        if expected is None:
            os.fsync(parent_fd)
        else:
            try:
                present = os.stat(identifier, dir_fd=parent_fd, follow_symlinks=False)
            except OSError as exc:
                raise _unknown_owner() from exc
            if (present.st_dev, present.st_ino) != identity:
                raise _unknown_owner()
        yield identity
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent_fd is not None:
            os.close(parent_fd)


def reconcile_consultation(
    store: ProjectStore, data_path: Path, consultation_id: str
) -> dict[str, Any]:
    from .project_cli import _now

    identifier = _identifier(consultation_id)
    current = store.show_consultation(identifier)
    if current["status"] != AttemptStatus.RUNNING:
        return current
    owner = store.consultation_owner(identifier)
    if owner is None:
        raise _unknown_owner()
    with _consultation_lock(data_path, identifier, expected=owner):
        store.reconcile_consultation(identifier, owner, _now())
    return store.show_consultation(identifier)


def consult(args: argparse.Namespace, data_path: Path) -> dict[str, Any]:
    from .project_cli import _database, _now, _repository, _validate_timing

    requests = _requests(args.advisor)
    _validate_timing(args)
    repository, common = _repository(args.repo, data_path)
    baseline = _resolve_commit(repository, args.baseline)
    original = _string(
        args.original_goal.read_bytes().decode("utf-8"), "original goal", nonempty=True
    )
    identifier = (
        f"consultation-{uuid4().hex}"
        if args.consultation_id is None
        else _identifier(args.consultation_id)
    )
    snapshot = freeze_repository(
        repository,
        baseline,
        baseline,
        original,
        ("Consult on the user's original goal against the selected commit.",),
    )
    if snapshot.repository != str(common) or snapshot.baseline != baseline:
        raise ValueError(
            "frozen consultation source differs from the selected Git baseline"
        )
    source_id = snapshot.snapshot_id
    log_root = data_path / "logs" / "consultations" / identifier
    if log_root.resolve() != log_root:
        raise ValueError("consultation log directory must contain no symlinks")
    data_directory(data_path)
    with (
        _consultation_lock(data_path, identifier) as owner,
        ProjectStore(_database(data_path, create=True)) as store,
    ):
        store.save_snapshot(source_id, snapshot.to_dict())
        store.start_consultation(
            identifier,
            str(repository),
            str(common),
            baseline,
            original,
            args.project_id,
            source_id,
            requests,
            _now(),
            owner=owner,
        )
        prior: dict[str, Any] | None = None
        for seat, request in enumerate(requests, start=1):
            with TemporaryDirectory(prefix="multi-agent-consultation-") as temporary:
                cwd = Path(temporary).resolve()
                materialize(snapshot, cwd / "source")
                source = cwd / "consultation-input.json"
                source.write_text(
                    json.dumps(
                        {
                            "consultation_id": identifier,
                            "original_goal": original,
                            "baseline_commit": baseline,
                            "source_snapshot_id": source_id,
                            "first_advice": prior,
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                source.chmod(0o444)
                before = _tree_digest(cwd)
                store.reserve_consultation_seat(
                    identifier, seat, ADVICE_PROMPT_VERSION, _now()
                )
                advice, error, evidence = asyncio.run(
                    run_project_advisor(
                        request,
                        executable=args.codex_bin
                        if request.provider == "codex"
                        else args.claude_bin,
                        timeout=args.timeout,
                        grace=args.grace,
                        cwd=cwd,
                        log_dir=log_root / f"seat-{seat}",
                        prompt=_prompt(seat, len(requests)),
                        snapshot=snapshot,
                    )
                )
                if before != _tree_digest(cwd):
                    advice = None
                    error = {
                        "detail": "frozen consultation input changed during execution",
                        "failure_category": None,
                        "retry_after": None,
                        "raw_type": "project.context_changed",
                    }
                prior = dict(advice) if advice is not None else None
                store.finish_consultation_seat(
                    identifier,
                    seat,
                    _now(),
                    advice=prior,
                    error=error,
                    evidence=evidence,
                )
                if error is not None:
                    break
        return store.show_consultation(identifier)
