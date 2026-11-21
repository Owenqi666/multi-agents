"""Structured CLI diagnostics alongside the existing terminal stderr."""

import json
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_structured: ContextVar[bool] = ContextVar("structured_cli_errors", default=False)


@contextmanager
def structured_errors(enabled: bool) -> Iterator[None]:
    token = _structured.set(enabled)
    try:
        yield
    finally:
        _structured.reset(token)


class CLIRefusal(ValueError):
    """A business refusal with a stable machine-readable reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _write_error(code: str, error: Exception) -> None:
    if not _structured.get():
        return
    message = (str(error) or type(error).__name__).encode("utf-8", "replace").decode()
    print(
        json.dumps({"kind": "cli_error", "error": {"code": code, "message": message}})
    )


def report_refusal(
    error: Exception,
    *,
    code: str = "cli_refused",
    prefix: str = "multi-agent: ",
    notes: bool = True,
) -> int:
    print(f"{prefix}{error}", file=sys.stderr)
    if notes:
        for note in getattr(error, "__notes__", ()):
            print(note, file=sys.stderr)
    if isinstance(error, CLIRefusal):
        code = error.code
    elif isinstance(error, (OSError, sqlite3.Error, RuntimeError)):
        code = "cli_runtime_failed"
    _write_error(code, error)
    return 2


def report_unexpected_error(error: Exception) -> None:
    _write_error("cli_runtime_failed", error)
