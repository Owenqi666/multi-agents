"""Serve the workspace API on an loopback endpoint with Host and Origin checks."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

from multi_agent.adapters.runtime_decode import strict_json_loads

from .jobs import ACTION, PROVIDERS, BusyEnvironment, EnvironmentJobs
from .workspace import Workspace

_BODY_LIMIT = 4096
_JOB_PATH = "/api/jobs/"


class RequestError(ValueError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class WorkspaceServer(ThreadingHTTPServer):
    def __init__(
        self, port: int, workspace: Workspace, executables: dict[str, str]
    ) -> None:
        self.workspace = workspace
        super().__init__(("127.0.0.1", port), WorkspaceHandler)
        self.authority = f"127.0.0.1:{self.server_port}"
        self.origin = f"http://{self.authority}"
        try:
            self.jobs = EnvironmentJobs(
                workspace.data_dir, workspace.repository, executables
            )
        except BaseException:
            super().server_close()
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
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
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

    def _provider(self) -> str:
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
            body = strict_json_loads(encoded.decode("utf-8"))
        except (UnicodeError, ValueError, TimeoutError):
            raise RequestError(
                400, "invalid_request", "Malformed UTF-8 JSON body."
            ) from None
        if not isinstance(body, dict) or body.keys() != {"action", "args"}:
            raise RequestError(
                400, "invalid_request", "Expected action and args fields."
            )
        if body["action"] != ACTION:
            raise RequestError(
                400, "unknown_action", "The requested action is unavailable."
            )
        args = body["args"]
        if not isinstance(args, dict) or args.keys() != {"provider"}:
            raise RequestError(
                400, "invalid_request", "Only the provider argument is accepted."
            )
        provider = args["provider"]
        if not isinstance(provider, str) or provider not in PROVIDERS:
            raise RequestError(
                400, "invalid_provider", "Provider must be codex or claude."
            )
        return provider

    def _dispatch(self, server: WorkspaceServer) -> tuple[int, dict[str, Any]]:
        if self.command == "GET" and self.path == "/api/workspace":
            return 200, server.workspace.view(server.jobs.view())
        if self.command == "GET" and self.path.startswith(_JOB_PATH):
            job = server.jobs.get(self.path.removeprefix(_JOB_PATH))
            if job is not None:
                return 200, job
        if self.command == "POST" and self.path == "/api/jobs":
            try:
                return 202, server.jobs.start(self._provider())
            except BusyEnvironment as error:
                raise RequestError(409, "environment_busy", str(error)) from None
        raise RequestError(404, "not_found", "The requested resource does not exist.")

    def _serve(self) -> None:
        server = cast(WorkspaceServer, self.server)
        try:
            self._authorize(server)
            status, body = self._dispatch(server)
        except RequestError as error:
            status = error.status
            body = {"error": {"code": error.code, "message": str(error)}}
        except (OSError, ValueError):
            status = 500
            body = {
                "error": {
                    "code": "workspace_unavailable",
                    "message": "The workspace could not be read or updated.",
                }
            }
        self._reply(status, body)

    do_GET = _serve
    do_POST = _serve
