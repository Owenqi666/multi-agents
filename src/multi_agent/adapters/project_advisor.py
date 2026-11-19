"""Produce structured project advice grounded in a frozen source snapshot."""

from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator

from multi_agent.core.planning import PlanningRequest
from multi_agent.core.project_consultation import ProjectAdvice
from multi_agent.core.snapshot import FrozenSnapshot

from .project_planner import _TEXT, _TEXTS, _object, _run_project_turn, _validate_text

ADVICE_PROMPT_VERSION = "project-advice-v1"

_ADVICE_SCHEMA = _object(
    {
        "draft_goal": _TEXT,
        "background": {
            "type": "array",
            "items": _object({"claim": _TEXT, "path": _TEXT}),
        },
        "assumptions": _TEXTS,
        "questions": _TEXTS,
        "remaining_disagreements": _TEXTS,
    }
)
_VALIDATOR = Draft202012Validator(_ADVICE_SCHEMA)


def parse_project_advice(value: object, snapshot: FrozenSnapshot) -> ProjectAdvice:
    """Validate the advice and require citations to frozen text files."""
    invalid = next(_VALIDATOR.iter_errors(value), None)
    if invalid is not None:
        raise ValueError(f"invalid advice at {invalid.json_path} ({invalid.validator})")
    advice = cast(ProjectAdvice, value)
    _validate_text(advice["draft_goal"], "draft_goal")
    for field, items in (
        ("assumptions", advice["assumptions"]),
        ("questions", advice["questions"]),
        ("remaining_disagreements", advice["remaining_disagreements"]),
    ):
        for item in items:
            _validate_text(item, field)

    frozen_paths = {
        item.path
        for item in snapshot.files
        if item.side in ("baseline", "candidate")
        and item.mode in ("100644", "100755")
        and item.text is not None
    }
    for reference in advice["background"]:
        _validate_text(reference["claim"], "background.claim")
        path = reference["path"]
        _validate_text(path, "background.path")
        if path not in frozen_paths:
            raise ValueError(f"background.path is not a frozen text file: {path!r}")
    return advice


async def run_project_advisor(
    request: PlanningRequest,
    *,
    executable: str,
    timeout: float,
    grace: float,
    cwd: Path,
    log_dir: Path,
    prompt: str,
    snapshot: FrozenSnapshot,
) -> tuple[ProjectAdvice | None, dict[str, Any] | None, dict[str, Any]]:
    return await _run_project_turn(
        request,
        executable=executable,
        timeout=timeout,
        grace=grace,
        cwd=cwd,
        log_dir=log_dir,
        prompt=prompt,
        output_schema=_ADVICE_SCHEMA,
        parse_output=lambda value: parse_project_advice(value, snapshot),
        role="advisor",
    )
