"""Write ordered probe records after applying the task card's redaction rules."""

import json
import re
from pathlib import Path
from time import monotonic
from types import TracebackType
from typing import Self

_PRIVATE_KEYS = {
    "email",
    "userid",
    "accountid",
    "orgid",
    "organizationid",
    "orgname",
    "organizationname",
    "workspacename",
    "plantype",
    "subscriptiontype",
}
_PRIVATE_SUFFIXES = ("token", "secret", "password", "apikey")
_EMAIL = re.compile(
    r"(?<![\w.!#$%&'*+=?^`{|}~+-])"
    r"[\w.!#$%&'*+=?^`{|}~+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"
)


class Redactor:
    """Redact JSON-shaped data without changing counters or other scalar values."""

    def __init__(self, home: Path | None = None) -> None:
        self._paths: dict[str, str] = {}
        self._add_path(Path.home() if home is None else home, "~")

    def _add_path(self, path: Path, replacement: str) -> None:
        self._paths[str(path.absolute())] = replacement
        self._paths[str(path.resolve())] = replacement

    def add_workdir(self, path: Path) -> None:
        self._add_path(path, "<WORKDIR>")

    def _text(self, text: str) -> str:
        for path in sorted(self._paths, key=len, reverse=True):
            text = text.replace(path, self._paths[path])
        return _EMAIL.sub("<EMAIL>", text) if "@" in text else text

    def redact(self, value: object) -> object:
        if isinstance(value, str):
            return self._text(value)
        if isinstance(value, dict):
            result = {}
            for key, child in value.items():
                if isinstance(key, str):
                    normalized = key.lower().replace("_", "").replace("-", "")
                    private = normalized in _PRIVATE_KEYS or normalized.endswith(
                        _PRIVATE_SUFFIXES
                    )
                    result[self._text(key)] = (
                        "<REDACTED>"
                        if private and isinstance(child, str)
                        else self.redact(child)
                    )
                else:
                    result[key] = self.redact(child)
            return result
        if isinstance(value, list):
            return [self.redact(child) for child in value]
        if isinstance(value, tuple):
            return tuple(self.redact(child) for child in value)
        return value


class Recorder:
    """Flush each redacted JSONL record before returning to the event loop."""

    def __init__(self, path: Path, redactor: Redactor) -> None:
        self.redactor = redactor
        self._started = monotonic()
        self._count = 0
        self._stream = path.open("x", encoding="utf-8")

    @property
    def count(self) -> int:
        return self._count

    @property
    def seconds(self) -> float:
        return monotonic() - self._started

    def emit(self, kind: str, **fields: object) -> None:
        record = {
            **fields,
            "seq": self._count + 1,
            "t": self.seconds,
            "kind": kind,
        }
        self._stream.write(
            json.dumps(self.redactor.redact(record), ensure_ascii=False) + "\n"
        )
        self._stream.flush()
        self._count += 1

    def close(self) -> None:
        self._stream.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
