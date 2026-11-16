"""Write-session transport bound to one host-owned implementation workspace.

Protocol structure is pinned to Codex 0.169.0 / Claude Code 2.1.325. The write
policies have offline contract coverage only; init/config reports are not proof
of OS enforcement. Native/model permission probes are a separate acceptance.
"""

import stat
from pathlib import Path
from typing import Any

from multi_agent.core.events import Error
from multi_agent.core.workspaces import WorkspaceRecord

from .base import Permission, SessionConfig, SessionHandle
from .codex_isolation import CodexIsolation
from .probe.recording import Recorder
from .runtime import RuntimeAdapter, _Turn
from .runtime_decode import RuntimeFailure

CLAUDE_VERSION = "2.1.325"
CLAUDE_FILE_TOOLS = ("Read", "Grep", "Glob", "Edit", "Write")


def _policy_failure(detail: str) -> RuntimeFailure:
    return RuntimeFailure(Error(detail, raw_type="adapter.implementation_policy"))


class ImplementationRuntimeAdapter(RuntimeAdapter):
    """A separate explicit write entry; the review adapter continues to reject it."""

    def __init__(
        self,
        provider: str,
        *,
        workspace: WorkspaceRecord,
        executable: str | None = None,
        timeout: float = 180,
        deadline_at: float | None = None,
        grace: float = 2,
        log_dir: Path,
        output_schema: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            provider,
            executable=executable,
            timeout=timeout,
            deadline_at=deadline_at,
            grace=grace,
            log_dir=log_dir,
            output_schema=output_schema,
        )
        self.workspace = workspace

    def _check_workspace(self, config: SessionConfig) -> None:
        root = Path(self.workspace.root)
        try:
            info = root.lstat()
            if (
                not root.is_absolute()
                or not stat.S_ISDIR(info.st_mode)
                or root.resolve(strict=True) != root
                or (info.st_dev, info.st_ino) != self.workspace.root_identity
                or config.cwd.resolve(strict=True) != root
                or config.permission != Permission.WORKSPACE_WRITE
            ):
                raise ValueError("implementation session does not match the owned root")
            if (
                self.log_dir.resolve() != self.log_dir
                or self.log_dir.is_relative_to(root)
                or root.is_relative_to(self.log_dir)
                or self.log_dir.is_relative_to(Path(self.workspace.repository))
                or self.log_dir.is_relative_to(Path(self.workspace.git_directory))
            ):
                raise ValueError(
                    "implementation logs must be separate from the workspace and Git metadata"
                )
        except (OSError, RuntimeError) as error:
            raise ValueError(
                f"implementation workspace cannot be opened: {error}"
            ) from error

    async def start_session(self, config: SessionConfig) -> SessionHandle:
        if config.permission != Permission.WORKSPACE_WRITE:
            raise ValueError(
                "implementation adapters only support workspace-write sessions"
            )
        self._check_workspace(config)
        return self._register_session(config)

    def _validate_turn_workspace(self, state: _Turn) -> None:
        try:
            self._check_workspace(state.config)
        except ValueError as error:
            raise _policy_failure(str(error)) from error

    def _create_isolation(self, state: _Turn, recorder: Recorder) -> CodexIsolation:
        self._validate_turn_workspace(state)
        return CodexIsolation.create(
            state.config.cwd,
            recorder,
            self._environment(),
            log_dir=self.log_dir,
            workspace=self.workspace,
            ownership=self._ownership,
        )

    def _claude_argv(self, state: _Turn) -> list[str]:
        self._validate_turn_workspace(state)
        argv = super()._claude_argv(state)
        argv[argv.index("--tools") + 1] = ",".join(CLAUDE_FILE_TOOLS)
        argv.extend(
            ("--restricted", "--strict-mcp-config", "--permission-mode", "acceptEdits")
        )
        return argv

    async def _claude(self, state: _Turn, recorder: Recorder) -> None:
        await super()._claude(state, recorder)
        if not state.implementation_policy_reported:
            raise _policy_failure("Claude exited without an implementation init report")

    def _validate_claude_message(
        self, state: _Turn, recorder: Recorder, message: dict[str, Any]
    ) -> None:
        is_init = message.get("type") == "system" and message.get("subtype") == "init"
        if state.implementation_policy_reported:
            if is_init:
                raise _policy_failure("Claude returned duplicate implementation init")
            return
        expected = set(CLAUDE_FILE_TOOLS)
        if self.output_schema is not None:
            expected.add("StructuredOutput")
        tools = message.get("tools")
        if (
            not is_init
            or not isinstance(tools, list)
            or not all(isinstance(tool, str) for tool in tools)
            or len(tools) != len(expected)
            or set(tools) != expected
            or message.get("cwd") != str(state.config.cwd)
            or message.get("permissionMode") != "acceptEdits"
            or message.get("claude_code_version") != CLAUDE_VERSION
            or message.get("mcp_servers") != []
            or message.get("additional_directories") != []
            or message.get("apiKeySource") != "none"
        ):
            raise _policy_failure(
                "Claude init does not report the required implementation policy"
            )
        state.implementation_policy_reported = True
        # Only runtime-reported values are recorded here. This does not assert
        # that native permissions, managed policy, or every hidden tool is tested.
        recorder.emit(
            "implementation_policy_reported",
            cwd=message["cwd"],
            tools=tools,
            permission_mode=message["permissionMode"],
            cli_version=message["claude_code_version"],
            mcp_servers=message["mcp_servers"],
            additional_directories=message["additional_directories"],
            api_key_source=message["apiKeySource"],
        )
