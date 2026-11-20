"""Canonicalize the machine-specific values in one sequence of offline HTTP exchanges."""

import base64
import json
import re
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_IDENTIFIER = re.compile(
    r"(?<![0-9a-f])(?:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
    r"|[0-9a-f]{64}|[0-9a-f]{40}|[0-9a-f]{32})(?![0-9a-f])"
)
_TIMESTAMP = re.compile(
    r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)"
)
_FAKE_SESSION = re.compile(r"(?<![\w])(?:session|thread|turn)-[0-9]+(?![\w])")
_ENCODED_FILE = re.compile(
    r'(?P<lead>\\*"hex\\*"\s*:\s*\\*")(?P<data>(?:[0-9a-fA-F]{2})+)(?=\\*")'
)
_ARTIFACT = re.compile(r"(?<![\w-])art-[A-Za-z0-9_-]+")
_TEMP_NAME = re.compile(
    r"(/sample/(?:scenario/)?tmp/)(multi-agent-[\w-]+-)([a-z0-9_]{8})(?=/|[^\w-]|$)"
)
_GIT_INDEX_LINE = re.compile(r"(?m)^(index )([0-9a-f]{7,39})(\.\.)([0-9a-f]{7,39})")
_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)


class SampleNormalizer:
    """Keep a stable first-seen mapping for all the requests and responses in a scenario."""

    def __init__(self) -> None:
        self._groups: dict[str, dict[str | float, int]] = {}
        self._paths: dict[str, str] = {}
        self._origin = ""
        self._path(Path(tempfile.gettempdir()), "/sample/tmp")
        self._path(Path(__file__).resolve().parents[3], "/sample/workbench")
        self._path(Path(sys.base_prefix), "/sample/python")

    def _path(self, source: Path, replacement: str) -> None:
        for spelling in {str(source), str(source.resolve())}:
            if spelling != "/":
                self._paths[spelling] = replacement
                if spelling.startswith("/private/"):
                    self._paths[spelling.removeprefix("/private")] = replacement

    def bind(self, *, origin: str | None = None, repository: str | None = None) -> None:
        if origin is not None:
            self._origin = origin
        if repository is not None:
            self._path(Path(repository).parent, "/sample/scenario")

    def _index(self, group: str, raw: str | float) -> int:
        values = self._groups.setdefault(group, {})
        if raw not in values:
            values[raw] = len(values) + 1
        return values[raw]

    def _identifier(self, raw: str) -> str:
        if not raw.strip("0"):
            return raw
        size = len(raw)
        if size == 36:
            number = self._index("uuid", raw)
            return f"10000000-0000-0000-0000-{number:012x}"
        tag = {32: "1", 40: "2", 64: "3"}.get(size, "4")
        number = self._index(f"hex-{size}", raw)
        return (tag + f"{number:06x}").ljust(size, "0")

    def _timestamp(self, match: re.Match[str]) -> str:
        raw = match.group()
        instant = _EPOCH + timedelta(seconds=self._index("time", raw))
        return instant.isoformat(timespec="microseconds")

    def _text(self, value: str) -> str:
        # Handles contain the resource ID, selection, log key and digest. Keep the
        # base64url encoding and the same canonical IDs that appear in resource JSON.
        parts: list[str] = []
        end = 0
        for match in _ARTIFACT.finditer(value):
            try:
                token = match.group()[4:]
                locator = json.loads(
                    base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
                )
            except (ValueError, UnicodeError):
                continue
            if not isinstance(locator, list) or len(locator) != 5:
                continue
            parts.append(self._plain(value[end : match.start()]))
            wire = json.dumps(self.normalize(locator), separators=(",", ":")).encode()
            parts.append("art-" + base64.urlsafe_b64encode(wire).decode().rstrip("="))
            end = match.end()
        parts.append(self._plain(value[end:]))
        return "".join(parts)

    def _file_bytes(self, wire: str) -> str:
        try:
            original = bytes.fromhex(wire).decode("utf-8")
        except (UnicodeError, ValueError):
            return wire
        canonical = self._plain(original)
        return canonical.encode("utf-8").hex() if canonical != original else wire

    def _plain(self, value: str) -> str:
        if self._origin:
            value = value.replace(self._origin, "http://127.0.0.1:41001")
        for path in sorted(self._paths, key=len, reverse=True):
            value = value.replace(path + "/", self._paths[path] + "/")
            if value == path:
                value = self._paths[path]
        value = _TEMP_NAME.sub(
            lambda m: m[1] + m[2] + str(self._index("tempdir", m[0])).zfill(8), value
        )
        value = _ENCODED_FILE.sub(
            lambda m: m["lead"] + self._file_bytes(m["data"]), value
        )
        value = _FAKE_SESSION.sub(
            lambda m: m[0].rsplit("-", 1)[0] + "-" + str(self._index("session", m[0])),
            value,
        )
        value = _TIMESTAMP.sub(self._timestamp, value)
        value = _IDENTIFIER.sub(lambda m: self._identifier(m[0]), value)
        return _GIT_INDEX_LINE.sub(
            lambda m: m[1] + self._identifier(m[2]) + m[3] + self._identifier(m[4]),
            value,
        )

    def _prompt_line(self, line: str) -> str:
        # Implementation prompts carry the same frozen workspace evidence both
        # as structured fields and as a compact JSON line shown to the model.
        if line.startswith("{"):
            try:
                payload = json.loads(line)
            except ValueError:
                pass
            else:
                return (
                    json.dumps(
                        self.normalize(payload),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + line[len(line.rstrip()) :]
                )
        return self._text(line)

    def normalize(self, value: Any, *, _field: str = "") -> Any:
        if isinstance(value, dict):
            return {
                self._text(k): self.normalize(v, _field=k) for k, v in value.items()
            }
        if isinstance(value, list):
            if (
                (_field == "identity" or _field.endswith("_identity"))
                and len(value) == 2
                and all(type(n) is int for n in value)
            ):
                return [self._index("device", value[0]), self._index("inode", value[1])]
            return [self.normalize(item) for item in value]
        if isinstance(value, str):
            if _field == "prompt":
                return "".join(
                    self._prompt_line(line) for line in value.splitlines(True)
                )
            if _field == "hex":
                return self._file_bytes(value)
            return self._text(value)
        if _field == "pid" and type(value) is int:
            return self._index("pid", value)
        if _field == "elapsed_seconds" and isinstance(value, (int, float)):
            return self._index("duration", value) / 1000
        return value
