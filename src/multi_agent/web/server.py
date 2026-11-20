"""Serve the workspace API on a loopback endpoint with Host and Origin checks."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from sqlite3 import Error as SQLiteError
from typing import Any, cast
from urllib.parse import parse_qs, unquote, urlsplit

from multi_agent.adapters.runtime_decode import strict_json_loads
from multi_agent.store.project_store import ProjectStore

from .actions import ActionConflict, UnknownAction, prepare_action
from .jobs import ACTION, PROVIDERS, BusyCLI, BusyEnvironment, BusyJob, EnvironmentJobs
from .resources import (
    LOCKED_DIFF_KINDS,
    LOCKED_RESOURCE_KINDS,
    lock_busy_diff,
    lock_busy_resource,
    parent_diff_requested,
    read_artifact,
    read_diff,
    read_resource,
    read_snapshot_file,
)
from .static import FrontendResponse, read_frontend
from .workspace import Workspace

_BODY_LIMIT = 8 * 1024 * 1024


class RequestError(ValueError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class WorkspaceServer(ThreadingHTTPServer):
    def __init__(
        self,
        port: int,
        workspace: Workspace,
        executables: dict[str, str],
        *,
        tool_directories: tuple[str, ...] = (),
        runtime_read_paths: tuple[str, ...] = (),
        frontend_dist: Path | None = None,
    ) -> None:
        self.workspace = workspace
        self.executables = executables
        self.tool_directories = tool_directories
        self.runtime_read_paths = runtime_read_paths
        self.frontend_dist = (
            frontend_dist.expanduser().resolve() if frontend_dist is not None else None
        )
        super().__init__(("127.0.0.1", port), WorkspaceHandler)
        self.authority = f"127.0.0.1:{self.server_port}"
        self.origin = f"http://{self.authority}"
        try:
            self.jobs = EnvironmentJobs(
                workspace.data_dir, workspace.repository, executables
            )
            database = workspace.data_dir / "reviews.sqlite3"
            if database.is_symlink() or database.exists() and not database.is_file():
                raise ValueError("workspace database must be a regular file")
            if database.is_file():
                try:
                    with ProjectStore(database):
                        pass
                except SQLiteError as error:
                    raise ValueError(
                        f"workspace database could not be opened: {error}"
                    ) from error
        except BaseException:
            self.server_close()
            raise

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            if hasattr(self, "jobs"):
                self.jobs.close()


class WorkspaceHandler(BaseHTTPRequestHandler):
    server_version = "MultiAgentWorkspace"
    timeout = 10
    sys_version = ""

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        # Request paths and payloads are not operational logs.
        return

    def _reply(self, status: int, value: dict[str, Any]) -> None:
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self._reply_bytes(status, payload, "application/json; charset=utf-8")

    def _reply_bytes(
        self,
        status: int,
        payload: bytes,
        content_type: str,
        cache_control: str = "no-store",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def send_error(
        self, code: int, message: str | None = None, explain: str | None = None
    ) -> None:
        self._reply(
            code,
            {
                "error": {
                    "code": "invalid_request",
                    "message": "Unsupported or malformed HTTP request.",
                }
            },
        )

    def _authorize(self, server: WorkspaceServer) -> None:
        if self.headers.get_all("Host") != [server.authority]:
            raise RequestError(
                403, "invalid_host", "Host must equal the printed loopback authority."
            )
        origin = self.headers.get_all("Origin")
        if self.command == "POST" and origin is not None and origin != [server.origin]:
            raise RequestError(
                403, "invalid_origin", "Origin must equal the printed loopback URL."
            )

    def _body(self) -> Any:
        lengths = self.headers.get_all("Content-Length", [])
        if (
            self.headers.get_all("Transfer-Encoding") is not None
            or self.headers.get_content_type() != "application/json"
            or len(lengths) != 1
            or not lengths[0].isascii()
            or not lengths[0].isdigit()
            or len(lengths[0]) > len(str(_BODY_LIMIT))
        ):
            raise RequestError(
                400, "invalid_request", "Use a fixed-length application/json body."
            )
        length = int(lengths[0])
        if not 0 < length <= _BODY_LIMIT:
            raise RequestError(
                400, "invalid_request", "JSON body has an invalid length."
            )
        try:
            encoded = self.rfile.read(length)
            if len(encoded) != length:
                raise ValueError("Incomplete JSON body")
            return strict_json_loads(encoded.decode("utf-8"))
        except (UnicodeError, ValueError, TimeoutError):
            raise RequestError(
                400, "invalid_request", "Malformed UTF-8 JSON body."
            ) from None

    def _start(self, server: WorkspaceServer) -> dict[str, str]:
        body = self._body()
        if (
            not isinstance(body, dict)
            or body.keys() != {"action", "args"}
            or not isinstance(body["action"], str)
            or not isinstance(body["args"], dict)
        ):
            raise RequestError(
                400, "invalid_request", "Expected action and args fields."
            )
        action, args = body["action"], body["args"]
        if action != ACTION:
            return server.jobs.start_cli(
                action,
                lambda directory: prepare_action(
                    action,
                    args,
                    workspace=server.workspace,
                    executables=server.executables,
                    directory=directory,
                    tool_directories=server.tool_directories,
                    runtime_read_paths=server.runtime_read_paths,
                ),
                args=args,
            )
        if args.keys() != {"provider"}:
            raise RequestError(
                400, "invalid_request", "Only the provider argument is accepted."
            )
        provider = args["provider"]
        if not isinstance(provider, str) or provider not in PROVIDERS:
            raise RequestError(
                400, "invalid_provider", "Provider must be codex or claude."
            )
        return server.jobs.start(provider)

    def _read(
        self, server: WorkspaceServer, parts: list[str], query: dict[str, list[str]]
    ) -> dict[str, Any]:
        match parts:
            case ["api", "workspace"]:
                _no_query(query)
                return server.workspace.view(server.jobs.view())
            case ["api", "jobs"]:
                _no_query(query)
                return server.jobs.list_jobs()
            case ["api", "jobs", identifier]:
                _no_query(query)
                job = server.jobs.get(identifier)
                if job is not None:
                    return job
            case ["api", "runs"]:
                if query.keys() - {"kind"}:
                    raise ValueError("Only a kind filter is accepted.")
                kinds = query.get("kind")
                if kinds is not None and (len(kinds) != 1 or not kinds[0]):
                    raise ValueError("Expected a single kind filter.")
                return server.jobs.runs(kinds[0] if kinds else None)
            case ["api", "resources", kind, identifier]:
                with server.jobs.read_gate.read(
                    guarded=kind in LOCKED_RESOURCE_KINDS
                ) as allowed:
                    if not allowed:
                        return lock_busy_resource(kind, identifier, query)
                    return read_resource(server.workspace, kind, identifier, query)
            case ["api", "diffs", kind, identifier]:
                against_parent = parent_diff_requested(kind, query)
                with server.jobs.read_gate.read(
                    guarded=kind in LOCKED_DIFF_KINDS
                ) as allowed:
                    if not allowed:
                        return lock_busy_diff(
                            kind, identifier, against_parent=against_parent
                        )
                    return read_diff(
                        server.workspace,
                        kind,
                        identifier,
                        against_parent=against_parent,
                    )
            case ["api", "snapshots", identifier, "file"]:
                return read_snapshot_file(
                    server.workspace, "snapshot", identifier, query
                )
            case ["api", "artifacts", identifier]:
                artifact = server.jobs.read_artifact(identifier, query)
                if artifact is not None:
                    return artifact
                return read_artifact(
                    server.workspace, identifier, query, gate=server.jobs.read_gate
                )
        raise KeyError("resource")

    def _dispatch(
        self, server: WorkspaceServer
    ) -> tuple[int, dict[str, Any]] | FrontendResponse:
        target = urlsplit(self.path)
        if (
            target.scheme
            or target.netloc
            or target.fragment
            or not target.path.startswith("/")
        ):
            raise ValueError("Expected a local API path.")
        parts = [unquote(part, errors="strict") for part in target.path[1:].split("/")]
        if (
            self.command == "GET"
            and parts[0] != "api"
            and server.frontend_dist is not None
        ):
            return read_frontend(server.frontend_dist, parts)
        query = parse_qs(
            target.query, keep_blank_values=True, strict_parsing=True, errors="strict"
        )
        if self.command == "GET":
            return 200, self._read(server, parts, query)
        _no_query(query)
        match parts:
            case ["api", "jobs"]:
                return 202, self._start(server)
            case ["api", "jobs", identifier, "cancel"]:
                if self._body() != {}:
                    raise ValueError("Job cancellation requires an empty JSON object.")
                return 202, server.jobs.cancel(identifier)
        raise KeyError("resource")

    def _serve(self) -> None:
        server = cast(WorkspaceServer, self.server)
        try:
            self._authorize(server)
            response = self._dispatch(server)
        except RequestError as error:
            status = error.status
            body = _error(error.code, str(error))
        except BusyEnvironment as error:
            status, body = 409, _error("environment_busy", str(error))
        except BusyCLI as error:
            status, body = (
                409,
                _error(
                    "busy", str(error), details={"active_job_id": error.active_job_id}
                ),
            )
        except (ActionConflict, BusyJob) as error:
            status, body = 409, _error("job_unavailable", str(error))
        except UnknownAction as error:
            status, body = 400, _error("unknown_action", str(error))
        except KeyError:
            status, body = (
                404,
                _error("not_found", "The requested resource does not exist."),
            )
        except UnicodeError:
            status, body = (
                400,
                _error("invalid_request", "Malformed UTF-8 request target."),
            )
        except ValueError as error:
            status, body = 400, _error("invalid_request", str(error))
        except (OSError, SQLiteError):
            status = 500
            body = _error(
                "workspace_unavailable", "The workspace could not be read or updated."
            )
        else:
            if isinstance(response, FrontendResponse):
                self._reply_bytes(
                    response.status,
                    response.payload,
                    response.content_type,
                    response.cache_control,
                )
                return
            status, body = response
        self._reply(status, body)

    do_GET = _serve
    do_POST = _serve


def _no_query(query: dict[str, list[str]]) -> None:
    if query:
        raise ValueError("This endpoint does not accept query parameters.")


def _error(
    code: str, message: str, *, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        body["details"] = details
    return {"error": body}
