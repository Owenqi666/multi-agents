"""Consult on frozen project input without confirming or planning it."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from multi_agent.adapters.project_advisor import (
    ADVICE_PROMPT_VERSION,
    run_project_advisor,
)
from multi_agent.core.planning import PlanningRequest
from multi_agent.store.project_store import ProjectStore

from .main import data_directory
from .runner import _tree_digest
from .snapshot import _resolve_commit, _string, freeze_repository, materialize

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


def consult(args: argparse.Namespace, data_path: Path) -> dict[str, Any]:
    from .project_cli import _database, _now, _repository, _validate_timing

    requests = _requests(args.advisor)
    _validate_timing(args)
    repository, common = _repository(args.repo, data_path)
    baseline = _resolve_commit(repository, args.baseline)
    original = _string(
        args.original_goal.read_bytes().decode("utf-8"), "original goal", nonempty=True
    )
    identifier = f"consultation-{uuid4().hex}"
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
    with ProjectStore(_database(data_directory(data_path), create=True)) as store:
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
