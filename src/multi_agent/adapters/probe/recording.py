"""Write ordered probe records after applying the task card's redaction rules."""

import json
import re
from pathlib import Path
from time import monotonic
from types import TracebackType
from typing import Self

_PRIVATE_KEYS = {
    "email",
    "orgname",
    "organizationname",
    "workspacename",
    "plantype",
    "plantier",
    "subscriptiontype",
    "servername",
    "balance",
}
_PRIVATE_SUFFIXES = (
    "userid",
    "accountid",
    "orgid",
    "organizationid",
    "installationid",
    "token",
    "secret",
    "password",
    "apikey",
)
_EMAIL = re.compile(
    r"(?<![\w.!#$%&'*+=?^`{|}~+-])"
    r"[\w.!#$%&'*+=?^`{|}~+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"
)
_JSON_STRING = r'"(?:[^"\\\x00-\x1f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*"'
_TEXT_FIELD = re.compile(
    rf"(?P<key>{_JSON_STRING})(?P<separator>\s*:\s*)(?P<value>{_JSON_STRING})"
)


def _private(key: str) -> bool:
    normalized = key.lower().replace("_", "").replace("-", "")
    return normalized in _PRIVATE_KEYS or normalized.endswith(_PRIVATE_SUFFIXES)


def _redact_field(match: re.Match[str]) -> str:
    if _private(json.loads(match["key"])):
        return match["key"] + match["separator"] + '"<REDACTED>"'
    return match[0]


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
        text = _EMAIL.sub("<EMAIL>", text) if "@" in text else text
        return _TEXT_FIELD.sub(_redact_field, text)

    def redact(self, value: object) -> object:
        if isinstance(value, str):
            return self._text(value)
        if isinstance(value, dict):
            result = {}
            for key, child in value.items():
                if isinstance(key, str):
                    result[self._text(key)] = (
                        "<REDACTED>"
                        if _private(key) and isinstance(child, str)
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
