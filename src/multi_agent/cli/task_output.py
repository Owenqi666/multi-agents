"""Parse an implementation report without interpreting it as execution proof."""

import unicodedata
from dataclasses import dataclass

from jsonschema import Draft202012Validator, ValidationError

from multi_agent.adapters.runtime_decode import strict_json_loads
from multi_agent.core.path_patterns import normalize_path
from multi_agent.core.tasks import TaskCard

from .snapshot import _path
from .task_prompt import task_output_schema

MAX_OUTPUT_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class TaskImplementationReport:
    task_hash: str
    outcome: str
    summary: str
    required_paths: tuple[str, ...]


def _concrete_path(value: str) -> str:
    """Validate literal POSIX syntax; do not resolve or authorize a path."""
    _path(value)
    if (
        any(char in value for char in "*?")
        or any(unicodedata.category(char) == "Cc" for char in value)
        or any(
            part.rstrip(" .") != part or normalize_path(part) == ".git"
            for part in value.split("/")
        )
    ):
        raise ValueError("implementation report requires safe concrete paths")
    return value


def parse_task_output(text: str, card: TaskCard) -> TaskImplementationReport:
    """Validate the exact task's four-field report, preserving its original text."""
    if not isinstance(text, str):
        raise TypeError("implementation report input must be text")
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise ValueError("implementation report must be valid UTF-8") from error
    if size > MAX_OUTPUT_BYTES:
        raise ValueError("implementation report exceeds the 1 MiB byte limit")
    value = strict_json_loads(text)
    try:
        Draft202012Validator(task_output_schema()).validate(value)
    except ValidationError as error:
        raise ValueError("invalid implementation report schema") from error
    if value["task_hash"] != card.task_hash:
        raise ValueError("implementation report task hash does not match the task")
    paths = tuple(value["required_paths"])
    if value["outcome"] == "completed":
        if paths:
            raise ValueError("completed implementation report cannot require paths")
    elif not value["summary"].strip() or not paths:
        raise ValueError("scope-blocked report requires an explanation and paths")
    seen: set[str] = set()
    for path in paths:
        normalized = normalize_path(_concrete_path(path))
        if normalized in seen:
            raise ValueError("implementation report contains duplicate path aliases")
        seen.add(normalized)
    return TaskImplementationReport(
        value["task_hash"], value["outcome"], value["summary"], paths
    )
