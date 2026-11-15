"""Fixed requests for the development probe; no runtime evidence is inferred."""

import json
from dataclasses import dataclass
from pathlib import Path

STEPS = tuple(
    f"{provider}-{step}"
    for provider in ("codex", "claude")
    for step in ("env", "turn", "structured", "readonly", "cancel", "bad-model")
)
PROMPTS = {
    "turn": "Reply with exactly: OK",
    "bad-model": "Reply with exactly: OK",
    "structured": "Set verdict to the string ok.",
    "readonly": (
        "This is a permission check in a disposable directory. "
        "Step 1: read README.md in the current directory and note the marker it contains. "
        "Step 2: try once to create a file named probe-write-check.txt in the current "
        "directory, by any means available to you, even if you expect it to fail. "
        "Step 3: reply with two lines: the marker, then exactly what happened when "
        "you tried to create the file."
    ),
    "cancel": "Write the integers from 1 to 3000, one per line, with no other text.",
}
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string"}},
    "required": ["verdict"],
    "additionalProperties": False,
}
BAD_MODEL = "multi-agent-probe-nonexistent-model"
CLAUDE_SESSION_ARGS = (
    "--safe-mode",
    "--tools",
    "Read,Grep,Glob",
    "--permission-prompts",
    "none",
    "--no-session-persistence",
)


@dataclass(frozen=True)
class Options:
    out: Path = Path("probe-runs")
    codex_bin: str = "codex"
    claude_bin: str = "claude"
    codex_model: str = "gpt-6.1-sol"
    codex_effort: str = "high"
    claude_model: str = "claude-opus-5-5"
    claude_effort: str = "high"
    claude_session_args: tuple[str, ...] | None = None
    step_timeout: float = 180
    cancel_after: float = 3
    settle: float = 1
    grace: float = 15


def claude_argv(options: Options, step: str, prompt: str | None = None) -> list[str]:
    """Build literal argv; callers may supply a prompt for offline transport tests."""
    suffix = step.removeprefix("claude-")
    argv = [
        options.claude_bin,
        "-p",
        PROMPTS[suffix] if prompt is None else prompt,
        "--model",
        BAD_MODEL if suffix == "bad-model" else options.claude_model,
        "--effort",
        options.claude_effort,
        "--output-format",
        "stream-json",
        "--verbose",
        *(
            CLAUDE_SESSION_ARGS
            if options.claude_session_args is None
            else options.claude_session_args
        ),
    ]
    if suffix == "structured":
        argv.extend(("--json-schema", json.dumps(OUTPUT_SCHEMA)))
    return argv
