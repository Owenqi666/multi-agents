"""Write ordered probe records after applying the task card's redaction rules."""

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from time import monotonic
from types import TracebackType
from typing import BinaryIO, Self, cast

from ..ownership import Acquisition, LogOwnership

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
# Names from ConfigReadResponse.ConfigLayerSource in the local protocol snapshot.
# Configuration values, paths, layer names and versions may contain credentials.
_CONFIG_LAYER_TYPES = {
    "packagedDefaults",
    "mdm",
    "system",
    "enterpriseManaged",
    "user",
    "project",
    "sessionFlags",
    "legacyManagedConfigTomlFromFile",
    "legacyManagedConfigTomlFromMdm",
}


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
        self._secrets: set[str] = set()
        self._add_path(Path.home() if home is None else home, "~")

    def _add_path(self, path: Path, replacement: str) -> None:
        self._paths[str(path.absolute())] = replacement
        self._paths[str(path.resolve())] = replacement

    def add_workdir(self, path: Path) -> None:
        self._add_path(path, "<WORKDIR>")

    def add_secret(self, value: str) -> None:
        """Mask a known credential in values, keys, diagnostics and JSON text.

        Register credentials before starting a process, so its earliest stdout
        or stderr cannot reach the recorder first. This is literal redaction,
        not a promise to recognize arbitrary encodings or unknown credentials.
        """
        if not isinstance(value, str):
            raise TypeError("secret must be a string")
        if not value:
            raise ValueError("secret must not be empty")
        self._secrets.add(value)
        for ensure_ascii in (False, True):
            self._secrets.add(json.dumps(value, ensure_ascii=ensure_ascii)[1:-1])

    def _secret_text(self, text: str) -> str:
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, "<REDACTED>")
        return text

    def redact_secrets(self, value: object) -> object:
        """Mask registered credentials without rewriting legitimate report text.

        Business results can contain exact source quotes, including email
        addresses, paths and private-looking keys. Only a registered credential
        is a reason to alter those values; general log privacy rules are separate.
        """
        if isinstance(value, str):
            return self._secret_text(value)
        if isinstance(value, dict):
            return {
                self._secret_text(key)
                if isinstance(key, str)
                else key: self.redact_secrets(child)
                for key, child in value.items()
            }
        if isinstance(value, list):
            return [self.redact_secrets(child) for child in value]
        if isinstance(value, tuple):
            return tuple(self.redact_secrets(child) for child in value)
        return value

    def _text(self, text: str) -> str:
        text = self._secret_text(text)
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


def _value_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, (list, tuple)):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    return "other"


def _config_read_audit(
    message: dict[object, object], identifier: str | int | None
) -> dict[str, object]:
    """Summarize an RPC envelope without retaining any arbitrary config data.

    This runs before the raw response is written. It never changes the original
    object consumed by the protocol validator. Validated policy values can be
    recorded separately as explicit observations by the caller.
    """
    result = message.get("result")
    error = message.get("error")
    summary: dict[str, object] = {
        "raw_payload_omitted": True,
        "has_result": "result" in message,
        "has_error": "error" in message,
        "result_type": _value_type(result) if "result" in message else "missing",
        "error_type": _value_type(error) if "error" in message else "missing",
    }
    if isinstance(result, dict):
        for key in ("config", "layers", "origins"):
            value = result.get(key)
            summary[key + "_type"] = _value_type(value) if key in result else "missing"
            if isinstance(value, (dict, list, tuple)):
                summary[key + "_count"] = len(value)
        layers = result.get("layers")
        if isinstance(layers, list):
            sources = []
            for layer in layers:
                source_type = None
                disabled = None
                if isinstance(layer, dict):
                    name = layer.get("name")
                    if isinstance(name, dict):
                        source_type = name.get("type")
                    reason = layer.get("disabledReason")
                    if "disabledReason" in layer and (
                        reason is None or isinstance(reason, str)
                    ):
                        disabled = reason is not None
                sources.append(
                    {
                        "source_type": source_type
                        if isinstance(source_type, str)
                        and source_type in _CONFIG_LAYER_TYPES
                        else "unrecognized",
                        "disabled": disabled,
                    }
                )
            summary["layer_sources"] = sources
    if isinstance(error, dict):
        code = error.get("code")
        summary["error_code"] = (
            code if isinstance(code, int) and not isinstance(code, bool) else None
        )
    return {"id": identifier, "config_read_audit": summary}


class Recorder:
    """Flush each redacted JSONL record before returning to the event loop."""

    def __init__(
        self,
        path: Path,
        redactor: Redactor,
        *,
        acquisition: Acquisition | None = None,
    ) -> None:
        self.redactor = redactor
        self._started = monotonic()
        self._count = 0
        self._rpc_methods: dict[tuple[str | None, str | int], str] = {}
        self._pending_config_reads: dict[str | None, set[str | int]] = {}
        self._path = path.absolute()
        self._identity: tuple[int, int] | None = None
        self._mode: int | None = None
        self._fd: int | None = None
        self._stream: BinaryIO | None = None
        self._closed = False
        self._healthy = True
        self._byte_count = 0
        self._digest = hashlib.sha256()
        try:
            try:
                self._fd = os.open(
                    self._path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o666,
                )
            except OSError:
                if acquisition is not None:
                    acquisition.not_created()
                raise
            # Local ownership precedes publication, fstat and stream wrapping.
            # A failing publisher still leaves a real descriptor to close.
            if acquisition is not None:
                acquisition.publish(self)
            info = os.fstat(self._fd)
            self._identity = info.st_dev, info.st_ino
            self._mode = info.st_mode
            self._stream = os.fdopen(self._fd, "wb")
            self._fd = None  # The binary stream now owns the descriptor.
        except BaseException:
            self._healthy = False
            self.close()
            raise
        finally:
            if acquisition is not None:
                acquisition.finish()

    @property
    def count(self) -> int:
        return self._count

    @property
    def seconds(self) -> float:
        return monotonic() - self._started

    def emit(self, kind: str, **fields: object) -> None:
        try:
            self._emit(kind, **fields)
        except BaseException:
            self._healthy = False
            raise

    def _emit(self, kind: str, **fields: object) -> None:
        message = fields.get("message")
        proc = fields.get("proc")
        if isinstance(message, dict) and (proc is None or isinstance(proc, str)):
            identifier = message.get("id")
            valid_id = isinstance(identifier, (str, int)) and not isinstance(
                identifier, bool
            )
            key = (proc, cast(str | int, identifier)) if valid_id else None
            method = message.get("method")
            pending = self._pending_config_reads.setdefault(proc, set())
            if kind == "send" and isinstance(method, str) and key is not None:
                self._rpc_methods[key] = method
                if method in ("config/read", "configRequirements/read"):
                    pending.add(key[1])
                else:
                    pending.discard(key[1])
            elif kind == "recv":
                known_method = self._rpc_methods.get(key) if key is not None else None
                known_config = (
                    known_method == "config/read"
                    or known_method == "configRequirements/read"
                )
                server_request = (
                    isinstance(method, str)
                    and ("id" not in message or valid_id)
                    and "result" not in message
                    and "error" not in message
                )
                if not server_request and (pending or known_config):
                    # Unexpected IDs are rejected by the RPC boundary only
                    # after recording. Omit their payload and untrusted ID now.
                    # Keep correct-ID associations for duplicate/late replies.
                    fields = {
                        **fields,
                        "message": _config_read_audit(
                            message, identifier if known_config else None
                        ),
                    }
                    if known_config:
                        pending.discard(identifier)
        if (
            kind == "recv"
            and (proc is None or isinstance(proc, str))
            and self._pending_config_reads.get(proc)
            and "text" in fields
        ):
            # Non-object JSON and parse errors enter the recorder as text.
            # Config parsing diagnostics can echo arbitrary values on stderr.
            # The process reader retains the original for boundary validation.
            raw_text = fields["text"]
            fields = {
                **fields,
                "text": "<CONFIG_READ_PAYLOAD_OMITTED>",
                "config_read_text_audit": {
                    "raw_payload_omitted": True,
                    "value_type": _value_type(raw_text),
                    "character_count": len(raw_text)
                    if isinstance(raw_text, str)
                    else None,
                },
            }
        record = {
            **fields,
            "seq": self._count + 1,
            "t": self.seconds,
            "kind": kind,
        }
        encoded = (
            json.dumps(self.redactor.redact(record), ensure_ascii=False) + "\n"
        ).encode("utf-8")
        assert self._stream is not None
        if self._stream.write(encoded) != len(encoded):
            raise OSError("log record write was incomplete")
        self._stream.flush()
        self._digest.update(encoded)
        self._byte_count += len(encoded)
        self._count += 1

    def close(self) -> None:
        try:
            if self._stream is not None:
                self._stream.close()
            elif self._fd is not None:
                os.close(self._fd)
                self._fd = None
            self._closed = True
        except BaseException:
            self._healthy = False
            if self._stream is not None:
                self._closed = self._stream.closed
            raise

    @staticmethod
    def _file_state(info: os.stat_result) -> tuple[int, ...]:
        return (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )

    def _matches_closed_file(self) -> bool:
        """Read a stable original file only after successful recording closes."""
        before = self._path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or (before.st_dev, before.st_ino) != self._identity
            or before.st_mode != self._mode
            or before.st_size != self._byte_count
        ):
            return False
        descriptor = os.open(self._path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if self._file_state(os.fstat(descriptor)) != self._file_state(before):
                return False
            digest = hashlib.sha256()
            count = 0
            while block := os.read(descriptor, 1024 * 1024):
                digest.update(block)
                count += len(block)
            return (
                count == self._byte_count
                and digest.digest() == self._digest.digest()
                and self._file_state(os.fstat(descriptor)) == self._file_state(before)
                and self._file_state(self._path.lstat()) == self._file_state(before)
            )
        finally:
            os.close(descriptor)

    def ownership_record(self, resource_id: str) -> LogOwnership:
        sealed = False
        if self._closed and self._healthy:
            try:
                sealed = self._matches_closed_file()
            except OSError:
                sealed = False
            if not sealed:
                self._healthy = False
        return LogOwnership(
            resource_id,
            str(self._path),
            self._identity,
            self._count,
            self._closed,
            self._healthy,
            self._byte_count if sealed else None,
            self._digest.hexdigest() if sealed else None,
            sealed,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
