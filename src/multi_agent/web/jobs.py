"""Persist loopback jobs and own their controller processes for one service."""

import asyncio
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock
from typing import Any, BinaryIO
from uuid import uuid4

from multi_agent.adapters.probe.process import ProcessCleanupIncomplete
from multi_agent.adapters.runtime import RuntimeAdapter
from multi_agent.adapters.runtime_decode import RuntimeFailure, strict_json_loads
from multi_agent.cli.runner import _environment_value

from .gate import ReadGate
from .resources import references
from .workspace import private_directory

PROVIDERS = ("codex", "claude")
ACTION = "environment.refresh"
_ACTIVE_STATES = {"queued", "running", "cancelling"}
_FIELDS = ("cli_version", "logged_in", "models", "model_efforts")
_ARTIFACT = re.compile(r"([0-9a-f]{32})\.(stdout|stderr)")
_CLI_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_PREVIEW_BYTES = 64 * 1024
_STDERR_TAIL_BYTES = 8 * 1024
_RECENT_JOBS = 50
_RUNS_VERSION = 1
_RECORD_FIELDS = (
    "args",
    "created_at",
    "started_at",
    "finished_at",
    "cancel_requested_at",
    "stderr_tail",
)


class BusyJob(ValueError):
    pass


class BusyEnvironment(BusyJob):
    pass


class BusyCLI(BusyJob):
    def __init__(self, active_job_id: str) -> None:
        super().__init__("A CLI job is already active.")
        self.active_job_id = active_job_id


def _private_file(path: str, flags: int) -> int:
    return os.open(path, flags | os.O_NOFOLLOW, 0o600)


def _record_args(args: dict[str, Any] | None) -> dict[str, Any] | None:
    if args is None:
        return None
    stored = args.copy()
    for field in ("original_goal", "confirmed_goal"):
        if field in args:
            stored[field] = {"length": len(args[field])}
    if "document" in args:
        compact = json.dumps(
            args["document"], ensure_ascii=False, separators=(",", ":")
        )
        stored["document"] = {"length": len(compact)}
    return stored


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    public = dict.fromkeys(_RECORD_FIELDS) | job
    if job["state"] != "interrupted":
        public.pop("recovery_consultation_id", None)
    return public


def _cli_result(
    path: Path, exit_code: int
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    raw = path.read_bytes()
    if not raw.strip() and exit_code:
        if exit_code in (130, -signal.SIGINT):
            return None, {
                "code": "cli_cancelled",
                "message": "The CLI was interrupted.",
            }
        return None, {"code": "cli_failed", "message": "The CLI exited unsuccessfully."}
    try:
        value = strict_json_loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise TypeError("CLI output must be an object")
        if value.get("kind") == "cli_error":
            error = value.get("error")
            if (
                exit_code == 0
                or value.keys() != {"kind", "error"}
                or not isinstance(error, dict)
                or error.keys() != {"code", "message"}
                or not isinstance(code := error["code"], str)
                or not _CLI_ERROR_CODE.fullmatch(code)
                or not isinstance(message := error["message"], str)
                or not message.strip()
            ):
                raise ValueError("CLI error envelope is malformed")
            return None, {"code": code, "message": message}
    except (TypeError, ValueError):
        return None, {
            "code": "invalid_cli_output",
            "message": "The CLI did not return a valid JSON object.",
        }
    return value, None


def _stderr_tail(path: Path) -> str:
    with path.open("rb") as stderr:
        size = os.fstat(stderr.fileno()).st_size
        stderr.seek(max(0, size - _STDERR_TAIL_BYTES))
        return stderr.read(_STDERR_TAIL_BYTES).decode("utf-8", errors="replace")


def _seal_artifact(path: Path, artifact_id: str, stream: str) -> dict[str, Any]:
    with path.open("rb") as artifact:
        return {
            "artifact_id": artifact_id,
            "stream": stream,
            "byte_count": os.fstat(artifact.fileno()).st_size,
            "sha256": hashlib.file_digest(artifact, "sha256").hexdigest(),
        }


def _page(query: dict[str, list[str]]) -> tuple[int, int]:
    options = {"offset": 0, "limit": _PREVIEW_BYTES}
    for name, values in query.items():
        if (
            name not in options
            or len(values) != 1
            or not (values[0].isascii() and values[0].isdecimal())
        ):
            raise ValueError("Expected a single byte offset and page limit.")
        options[name] = int(values[0])
    if not 1 <= options["limit"] <= _PREVIEW_BYTES:
        raise ValueError("Artifact page limit must be between 1 and 65536 bytes.")
    return options["offset"], options["limit"]


def _preview_artifact(
    artifact: BinaryIO, registered: dict[str, Any], offset: int, limit: int
) -> dict[str, Any]:
    digest = hashlib.sha256()
    preview = bytearray()
    size = 0
    for chunk in iter(lambda: artifact.read(_PREVIEW_BYTES), b""):
        digest.update(chunk)
        end = size + len(chunk)
        if size < offset + limit and end > offset:
            preview.extend(chunk[max(0, offset - size) : offset + limit - size])
        size = end
    if registered["byte_count"] != size or registered["sha256"] != digest.hexdigest():
        raise KeyError(registered["artifact_id"])
    next_offset = offset + len(preview)
    return {
        "stream": registered["stream"],
        "text": preview.decode("utf-8", errors="replace"),
        "offset": offset,
        "next_offset": next_offset if next_offset < size else None,
        "byte_count": size,
        "sha256": registered["sha256"],
        "truncated": next_offset < size,
    }


class EnvironmentJobs:
    def __init__(
        self, data_dir: Path, repository: Path, executables: dict[str, str]
    ) -> None:
        self.repository = str(repository)
        self.executables = executables
        self.directory = data_dir / "web-environment"
        private_directory(self.directory)
        self.log_dir = self.directory / "logs"
        private_directory(self.log_dir)
        self.job_root = data_dir / "web-jobs"
        self.state_path = self.directory / "state.json"
        self.lock = Lock()
        self._submission = Lock()
        self.read_gate = ReadGate()
        self.closed = False
        self.active: set[str] = set()
        self.controllers: dict[str, subprocess.Popen[bytes]] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.navigation: dict[tuple[str, str], dict[str, Any]] = {}
        self.environments: dict[str, Any] = dict.fromkeys(PROVIDERS)
        owner = self._lock_directory()
        self.owner: int | None = owner
        try:
            self._restore()
            self.pool = ThreadPoolExecutor(max_workers=len(PROVIDERS))
        except BaseException:
            os.close(owner)
            raise

    def _lock_directory(self) -> int:
        lock_fd = os.open(
            self.directory / "owner.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
        )
        try:
            if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                raise ValueError("environment owner lock must be a regular file")
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError(
                    "environment data directory already has a service owner"
                ) from None
        except BaseException:
            os.close(lock_fd)
            raise
        return lock_fd

    def _restore(self) -> None:
        if self.state_path.is_symlink() or (
            self.state_path.exists() and not self.state_path.is_file()
        ):
            raise ValueError("environment state must be a regular file")
        if not self.state_path.exists():
            self._save()
            return
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if state["version"] != 1 or state["repository"] != self.repository:
                raise ValueError(
                    "environment data belongs to a different repository or format"
                )
            self.jobs = state["jobs"]
            self.environments = state["environments"]
            self.navigation = {
                (item["kind"], item["id"]): item for item in state.get("runs", [])
            }
            upgrade_runs = state.get("runs_version", 0) < _RUNS_VERSION
            if upgrade_runs:
                self._upgrade_runs()
        except (KeyError, TypeError, UnicodeError, json.JSONDecodeError):
            raise ValueError("environment state is unreadable") from None
        interrupted = False
        for job in self.jobs.values():
            if job["state"] in _ACTIVE_STATES:
                interrupted = True
                job.update(
                    state="interrupted",
                    error={
                        "code": "service_interrupted",
                        "message": "The previous service stopped before observing a result.",
                    },
                )
        if interrupted or upgrade_runs:
            self._save()

    def _upgrade_runs(self) -> None:
        timestamps = {key: row["updated_at"] for key, row in self.navigation.items()}
        for key, timestamp in timestamps.items():
            self._register_run(self.navigation[key], timestamp)

        for job in sorted(
            self.jobs.values(), key=lambda job: job.get("finished_at") or ""
        ):
            if job["result"] is None:
                continue
            finished_at = job.get("finished_at")
            if finished_at is None:
                # Older job records only kept their time on registered runs.
                related = (
                    timestamps.get((item["kind"], item["id"]))
                    for item in job.get("resources", ())
                )
                finished_at = min(
                    (timestamp for timestamp in related if timestamp is not None),
                    default=None,
                )
            for item in references(job["action"], job["result"]):
                timestamp = timestamps.get((item["kind"], item["id"]), finished_at)
                if timestamp is not None:
                    self._register_run(item, timestamp)

    def _save(self) -> None:
        state = {
            "version": 1,
            "repository": self.repository,
            "jobs": self.jobs,
            "environments": self.environments,
            "runs": list(self.navigation.values()),
            "runs_version": _RUNS_VERSION,
        }
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.directory, delete=False
        ) as output:
            temporary = Path(output.name)
            try:
                json.dump(state, output, allow_nan=False)
                output.flush()
                os.fsync(output.fileno())
                os.replace(temporary, self.state_path)
            finally:
                temporary.unlink(missing_ok=True)

    def close(self) -> None:
        with self.lock:
            self.closed = True
            self.read_gate.stop_reads()
            for identifier, controller in self.controllers.items():
                if (
                    self.jobs[identifier]["state"] == "running"
                    and controller.poll() is None
                ):
                    self._signal_cancel(identifier, controller)
        # Retain ownership until workers and admitted reads finish and any
        # submission can clean up its private inputs, even if close is retried.
        self.pool.shutdown(wait=True)
        self.read_gate.wait_for_read()
        with self._submission, self.lock:
            if self.active or self.controllers:
                raise RuntimeError("job workers are still active")
            owner, self.owner = self.owner, None
            if owner is not None:
                os.close(owner)

    def view(self) -> dict[str, Any]:
        with self.lock:
            return self.environments.copy()

    def get(self, identifier: str) -> dict[str, Any] | None:
        with self.lock:
            job = self.jobs.get(identifier)
            return _public_job(job) if job is not None else None

    def list_jobs(self) -> dict[str, Any]:
        with self.lock:
            # The persisted insertion order breaks ties and orders legacy jobs.
            ordered = sorted(
                self.jobs.values(),
                key=lambda job: job.get("created_at") or "",
            )
            return {
                "items": [
                    _public_job(job)
                    for index, job in enumerate(reversed(ordered))
                    if index < _RECENT_JOBS or job["state"] in _ACTIVE_STATES
                ]
            }

    def runs(self, kind: str | None) -> dict[str, Any]:
        with self.lock:
            items = [
                item.copy()
                for (record_kind, _), item in self.navigation.items()
                if kind is None or record_kind == kind
            ]
        return {
            "items": sorted(items, key=lambda item: item["updated_at"], reverse=True)
        }

    def _register_run(self, item: dict[str, str], updated_at: str) -> None:
        key = item["kind"], item["id"]
        previous = self.navigation.get(key, {})
        fallback_label = key[0].replace("-", " ").capitalize()
        label = previous.get("label")
        if not label or label in (key[1], fallback_label):
            label = item.get("label")
        if not label or label == key[1]:
            label = fallback_label
        self.navigation[key] = {
            "kind": key[0],
            "id": key[1],
            "label": label,
            "parent_id": item.get("parent_id", previous.get("parent_id")),
            "updated_at": updated_at,
        }

    def _accept(
        self, identifier: str, action: str, args: dict[str, Any] | None
    ) -> None:
        job: dict[str, Any] = {
            **dict.fromkeys(_RECORD_FIELDS),
            "job_id": identifier,
            "action": action,
            "args": _record_args(args),
            "state": "queued",
            "created_at": datetime.now(UTC).isoformat(),
            "exit_code": None,
            "result": None,
            "error": None,
            "resources": [],
        }
        if action == "project.consult":
            job["recovery_consultation_id"] = f"consultation-{identifier}"
        self.jobs[identifier] = job
        try:
            self._save()
        except OSError:
            del self.jobs[identifier]
            raise

    def start(self, provider: str) -> dict[str, str]:
        with self.lock:
            if self.closed or provider in self.active:
                raise BusyEnvironment(
                    "An environment check cannot start for this provider now."
                )
            identifier = uuid4().hex
            self._accept(identifier, ACTION, {"provider": provider})
            self.active.add(provider)
            self.pool.submit(self._run, provider, identifier)
        return {"job_id": identifier, "state": "queued"}

    def start_cli(
        self,
        action: str,
        prepare: Callable[[Path], list[str]],
        *,
        args: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        with self._submission:
            active = self.read_gate.active_job_id
            if active is not None:
                raise BusyCLI(active)
            with self.lock:
                if self.closed:
                    raise BusyJob("The service is closing.")
            private_directory(self.job_root)
            identifier = uuid4().hex
            directory = self.job_root / identifier
            directory.mkdir(mode=0o700)
            try:
                input_dir = directory / "input"
                input_dir.mkdir(mode=0o700)
                with self.read_gate.read(
                    guarded=action == "project.stage.integrate"
                ) as allowed:
                    if not allowed:
                        raise BusyJob("The service is closing.")
                    argv = prepare(input_dir)
                with self.lock:
                    if self.closed:
                        raise BusyJob("The service is closing.")
                    self._accept(identifier, action, args)
                    self.read_gate.begin_job(identifier)
                    self.pool.submit(self._run_cli, identifier, argv, directory)
            except BaseException:
                shutil.rmtree(directory)
                raise
        return {"job_id": identifier, "state": "queued"}

    def _signal_cancel(
        self, identifier: str, controller: subprocess.Popen[bytes]
    ) -> None:
        job = self.jobs[identifier]
        job.update(
            state="cancelling", cancel_requested_at=datetime.now(UTC).isoformat()
        )
        try:
            self._save()
        except OSError:
            job.update(state="running", cancel_requested_at=None)
            raise
        controller.send_signal(signal.SIGINT)

    def cancel(self, identifier: str) -> dict[str, str]:
        with self.lock:
            job = self.jobs[identifier]
            controller = self.controllers.get(identifier)
            if (
                job["state"] != "running"
                or controller is None
                or controller.poll() is not None
            ):
                raise BusyJob(
                    "The service does not control a cancellable process for this job."
                )
            self._signal_cancel(identifier, controller)
        return {"job_id": identifier, "state": "cancelling"}

    def _run_cli(self, identifier: str, argv: list[str], directory: Path) -> None:
        controller = None
        exit_code = None
        result = None
        error = None
        stderr_tail = None
        artifacts: list[dict[str, Any]] = []
        try:
            self.read_gate.wait_for_read()
            with self.lock:
                if self.closed:
                    self._interrupt(
                        identifier,
                        "service_interrupted",
                        "The service stopped before launching the job.",
                    )
                    self._save()
                    return
                self.jobs[identifier].update(
                    state="running", started_at=datetime.now(UTC).isoformat()
                )
                self._save()
                with (
                    open(directory / "stdout", "xb", opener=_private_file) as stdout,
                    open(directory / "stderr", "xb", opener=_private_file) as stderr,
                ):
                    controller = subprocess.Popen(
                        argv,
                        cwd=directory / "input",
                        stdin=subprocess.DEVNULL,
                        stdout=stdout,
                        stderr=stderr,
                        start_new_session=True,
                    )
                self.controllers[identifier] = controller
            exit_code = controller.wait()
            stderr_tail = _stderr_tail(directory / "stderr")
            result, error = _cli_result(directory / "stdout", exit_code)
            artifacts = [
                _seal_artifact(directory / stream, f"{identifier}.{stream}", stream)
                for stream in ("stdout", "stderr")
            ]
        except OSError:
            error = (
                {"code": "cli_launch_failed", "message": "The CLI did not start."}
                if controller is None
                else {
                    "code": "cli_io_failed",
                    "message": "The CLI output could not be saved.",
                }
            )
        finally:
            if controller is not None and exit_code is None:
                exit_code = controller.wait()
            with self.lock:
                self.controllers.pop(identifier, None)
                if self.jobs[identifier]["state"] != "interrupted":
                    self._finish_cli(
                        identifier, exit_code, result, error, artifacts, stderr_tail
                    )
                self.read_gate.finish_job()

    def _finish_cli(
        self,
        identifier: str,
        exit_code: int | None,
        result: dict[str, Any] | None,
        error: dict[str, str] | None,
        artifacts: list[dict[str, Any]],
        stderr_tail: str | None,
    ) -> None:
        job = self.jobs[identifier]
        records = references(job["action"], result) if result is not None else []
        now = datetime.now(UTC).isoformat()
        job.update(
            state="finished",
            finished_at=now,
            exit_code=exit_code,
            result=result,
            error=error,
            resources=[{"kind": item["kind"], "id": item["id"]} for item in records],
            artifacts=artifacts,
            stderr_tail=stderr_tail,
        )
        before = self.navigation.copy()
        for item in records:
            self._register_run(item, now)
        try:
            self._save()
        except OSError:
            self.navigation = before
            job["artifacts"] = []
            job["resources"] = []
            self._interrupt(
                identifier, "state_write_failed", "The CLI result could not be saved."
            )

    def read_artifact(
        self, artifact_id: str, query: dict[str, list[str]]
    ) -> dict[str, Any] | None:
        match = _ARTIFACT.fullmatch(artifact_id)
        if match is None:
            return None
        identifier, stream = match.groups()
        with self.lock:
            job = self.jobs.get(identifier)
            registered = next(
                (
                    item
                    for item in (job or {}).get("artifacts", [])
                    if item["artifact_id"] == artifact_id
                ),
                None,
            )
        if registered is None:
            raise KeyError(artifact_id)
        offset, limit = _page(query)
        path = self.job_root / identifier / stream
        try:
            if path.resolve(strict=True) != path:
                raise KeyError(artifact_id)
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as artifact:
                if not stat.S_ISREG(os.fstat(artifact.fileno()).st_mode):
                    raise KeyError(artifact_id)
                preview = _preview_artifact(artifact, registered, offset, limit)
        except OSError:
            raise KeyError(artifact_id) from None
        return preview

    def _interrupt(self, identifier: str, code: str, message: str) -> None:
        self.jobs[identifier].update(
            state="interrupted", result=None, error={"code": code, "message": message}
        )

    def _run(self, provider: str, identifier: str) -> None:
        result = None
        try:
            with self.lock:
                self.jobs[identifier].update(
                    state="running", started_at=datetime.now(UTC).isoformat()
                )
                self._save()
            fields, error = self._observe(provider, identifier)
            result = {
                "provider": provider,
                "observed_at": datetime.now(UTC).isoformat(),
                "source": "runtime_environment_check",
                **fields,
                "error": error,
            }
        finally:
            with self.lock:
                previous = self.environments[provider]
                try:
                    if result is None:
                        self._interrupt(
                            identifier,
                            "job_interrupted",
                            "The environment job stopped without a result.",
                        )
                    else:
                        self.jobs[identifier].update(
                            state="finished",
                            finished_at=datetime.now(UTC).isoformat(),
                            result=result,
                            error=error,
                        )
                        if error is None:
                            self.environments[provider] = result
                    self._save()
                except OSError:
                    self.environments[provider] = previous
                    self._interrupt(
                        identifier,
                        "state_write_failed",
                        "The environment job could not be saved.",
                    )
                finally:
                    self.active.remove(provider)

    def _observe(
        self, provider: str, identifier: str
    ) -> tuple[dict[str, Any], dict[str, str] | None]:
        try:
            adapter = RuntimeAdapter(
                provider,
                executable=self.executables[provider],
                timeout=30,
                log_dir=self.log_dir / identifier,
            )
            fields = _environment_value(asyncio.run(adapter.check_environment()))
        except RuntimeFailure as failure:
            return dict.fromkeys(_FIELDS), {
                "code": failure.event.failure_category or "environment_check_failed",
                "message": "Runtime environment check failed.",
            }
        except ProcessCleanupIncomplete:
            return dict.fromkeys(_FIELDS), {
                "code": "environment_cleanup_incomplete",
                "message": "Runtime environment check did not confirm process cleanup.",
            }
        except (
            RuntimeError,
            OSError,
            ValueError,
            TypeError,
            KeyError,
            asyncio.CancelledError,
        ):
            # This is the runtime boundary of a submitted HTTP job. Even an
            # unexpected adapter failure must produce a result the client can read.
            return dict.fromkeys(_FIELDS), {
                "code": "environment_check_failed",
                "message": "Runtime environment check failed.",
            }
        return fields, None
