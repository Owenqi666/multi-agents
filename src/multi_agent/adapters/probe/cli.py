"""Command-line validation happens before filesystem or process side effects."""

import argparse
import asyncio
import math
from pathlib import Path

from .config import STEPS, Options
from .runner import run


def _duration(value: str) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or seconds < 0:
        raise argparse.ArgumentTypeError("duration must be finite and non-negative")
    return seconds


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Record local CLI protocol probes (user-run only)."
    )
    parser.add_argument("steps", nargs="*")
    parser.add_argument("--list", action="store_true", dest="list_steps")
    parser.add_argument("--out", type=Path, default=Path("probe-runs"))
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--claude-bin", default="claude")
    parser.add_argument("--codex-model", default="gpt-6.1-sol")
    parser.add_argument("--codex-effort", default="high")
    parser.add_argument("--claude-model", default="claude-opus-5-5")
    parser.add_argument("--claude-effort", default="high")
    parser.add_argument(
        "--claude-session-arg", action="append", dest="claude_session_args"
    )
    for name, default in (
        ("step-timeout", 180),
        ("cancel-after", 3),
        ("settle", 1),
        ("grace", 15),
    ):
        parser.add_argument(f"--{name}", type=_duration, default=default)
    args = parser.parse_args(argv)
    if args.list_steps:
        for name in STEPS:
            print(
                f"{name} provider={name.split('-', 1)[0]} requests_model={'no' if name.endswith('-env') else 'yes'}"
            )
        return 0
    if not args.steps:
        parser.error("at least one step is required (see --list)")
    if any(step not in STEPS for step in args.steps):
        parser.error("unknown step (see --list)")
    if len(args.steps) != len(set(args.steps)):
        parser.error("duplicate steps are not allowed")
    values = vars(args).copy()
    steps = values.pop("steps")
    values.pop("list_steps")
    session_args = values["claude_session_args"]
    if session_args is not None:
        values["claude_session_args"] = tuple(session_args)
    try:
        return asyncio.run(run(steps, Options(**values)))
    except FileExistsError as error:
        print(f"Output directory already exists: {error.filename}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
