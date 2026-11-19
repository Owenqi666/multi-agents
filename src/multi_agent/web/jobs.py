"""Persist loopback jobs and execute only explicit runtime environment checks."""

import asyncio
import fcntl
import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock
from typing import Any
from uuid import uuid4

from multi_agent.adapters.probe.process import ProcessCleanupIncomplete
from multi_agent.adapters.runtime import RuntimeAdapter
from multi_agent.adapters.runtime_decode import RuntimeFailure
from multi_agent.cli.runner import _environment_value

from .workspace import private_directory

PROVIDERS = ("codex", "claude")
ACTION = "environment.refresh"
_ACTIVE_STATES = {"queued", "running", "cancelling"}
_FIELDS = ("cli_version", "logged_in", "models", "model_efforts")


class BusyEnvironment(ValueError):
    pass


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
        self.state_path = self.directory / "state.json"
        self.lock = Lock()
        self.closed = False
        self.active: set[str] = set()
        self.jobs: dict[str, dict[str, Any]] = {}
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
        if interrupted:
            self._save()

    def _save(self) -> None:
        state = {
            "version": 1,
            "repository": self.repository,
            "jobs": self.jobs,
            "environments": self.environments,
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
        # If waiting is interrupted, retain ownership until a later drain
        # or process exit. Only those workers may still commit results.
        self.pool.shutdown(wait=True)
        with self.lock:
            if self.active:
                raise RuntimeError("environment workers are still active")
            owner, self.owner = self.owner, None
            if owner is not None:
                os.close(owner)

    def view(self) -> dict[str, Any]:
        with self.lock:
            return self.environments.copy()

    def get(self, identifier: str) -> dict[str, Any] | None:
        with self.lock:
            job = self.jobs.get(identifier)
            return job.copy() if job is not None else None

    def start(self, provider: str) -> dict[str, str]:
        with self.lock:
            if self.closed or provider in self.active:
                raise BusyEnvironment(
                    "An environment check cannot start for this provider now."
                )
            identifier = uuid4().hex
            self.jobs[identifier] = {
                "job_id": identifier,
                "action": ACTION,
                "state": "queued",
                "exit_code": None,
                "result": None,
                "error": None,
                "resources": [],
            }
            try:
                self._save()
            except OSError:
                del self.jobs[identifier]
                raise
            self.active.add(provider)
            self.pool.submit(self._run, provider, identifier)
        return {"job_id": identifier, "state": "queued"}

    def _interrupt(self, identifier: str, code: str, message: str) -> None:
        self.jobs[identifier].update(
            state="interrupted", result=None, error={"code": code, "message": message}
        )

    def _run(self, provider: str, identifier: str) -> None:
        result = None
        try:
            with self.lock:
                self.jobs[identifier]["state"] = "running"
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
                            state="finished", result=result, error=error
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
