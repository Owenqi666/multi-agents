"""Process-local Codex review policy, pinned to the recorded 0.169.0 contract.

The official, unmodified config schema and its provenance live in
protocol/codex/config.schema.json and config-schema-source.json. Native command
execution must use this default named policy without a legacy sandbox override.
"""

import asyncio
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import mkdtemp
from typing import Any

from multi_agent.core.events import Error, FailureCategory

from .probe.process import Process, SpawnFailed
from .probe.recording import Recorder, Redactor
from .runtime_decode import RuntimeFailure

PROFILE = "multi-agent-frozen-review"
VERSION = "codex-cli 0.169.0"

# These are schema-defined switches, not arbitrary -c keys. Disabling discovery
# is additional to auditing effective configuration and instruction sources.
DISABLED_FEATURES = (
    "agent_message_board",
    "agent_message_board_mcp",
    "api_key_cyber_access_programs",
    "api_key_model_discovery",
    "apps",
    "auth_elicitation",
    "background_paginated_rollout_migration",
    "browser_use",
    "code_mode",
    "codex_apps_mcp_2026_07_28",
    "computer_use",
    "connectors",
    "context_management",
    "daybreak_program_propagation",
    "external_agent_memory_import",
    "hooks",
    "image_generation",
    "js_repl",
    "mcp_2026_07_28",
    "mcp_connection_sharing",
    "mcp_lazy_stdio_startup",
    "memories",
    "memory_tool",
    "mentions_v2",
    "multi_agent",
    "multi_agent_v2",
    "multi_env_copy_file",
    "plugins",
    "recommended_plugins",
    "remote_control",
    "remote_plugin",
    "shell_snapshot",
    "shell_snapshot_v2",
    "skill_search",
    "token_budget",
    "tool_suggest",
    "windows_sandbox_service",
    "workspace_dependencies",
)


def policy_config(cwd: Path) -> dict[str, Any]:
    """The single policy constructor used by review and native permission probes."""
    return {
        "default_permissions": PROFILE,
        "approval_policy": "never",
        "allow_login_shell": False,
        "cli_auth_credentials_store": "file",
        "project_doc_max_bytes": 0,
        "project_doc_fallback_filenames": [],
        "include_environment_context": False,
        "include_environment_context_time": False,
        "include_apps_instructions": False,
        "include_collaboration_mode_instructions": False,
        "history": {"persistence": "none"},
        "web_search": "disabled",
        "permissions": {
            PROFILE: {
                "filesystem": {":minimal": "read", str(cwd.resolve()): "read"},
                "network": {"enabled": False},
            }
        },
        "features": {
            **dict.fromkeys(DISABLED_FEATURES, False),
            "skip_host_skill_discovery": True,
        },
        "memories": {
            "use_memories": False,
            "generate_memories": False,
            "dedicated_tools": False,
        },
        "skills": {"include_instructions": False, "bundled": {"enabled": False}},
        "cloud": {"skills": {"enabled": False}},
        "plugins": {"_default": {"enabled": False}},
        "agents": {"enabled": False},
        "orchestrator": {"mcp": {"enabled": False}},
        "shell_environment_policy": {
            "inherit": "none",
            "set": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        },
    }


def policy_toml(config: dict[str, Any]) -> str:
    """Serialize only this module's JSON-shaped policy into TOML tables."""
    lines: list[str] = []

    def table(value: dict[str, Any], path: tuple[str, ...]) -> None:
        if path:
            lines.append("[" + ".".join(json.dumps(key) for key in path) + "]")
        for key, item in value.items():
            if not isinstance(item, dict):
                lines.append(f"{json.dumps(key)} = {json.dumps(item)}")
        for key, item in value.items():
            if isinstance(item, dict):
                table(item, (*path, key))

    table(config, ())
    return "\n".join(lines) + "\n"


def isolated_environment() -> dict[str, str]:
    """Do not let inherited credentials, config switches or loader hooks in."""
    return {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8"}


def _auth_error(detail: str) -> RuntimeFailure:
    return RuntimeFailure(
        Error(detail, FailureCategory.AUTH_EXPIRED, raw_type="adapter.isolation_auth")
    )


def _minimal_auth(auth: object, redactor: Redactor) -> dict[str, Any]:
    # Same-tag login/src/auth/manager.rs::AuthDotJson::resolved_mode defaults to
    # Chatgpt only in the absence of other credential modes. Never turn another
    # login mode into a subscription login by stripping its discriminator.
    if (
        not isinstance(auth, dict)
        or auth.get("auth_mode") not in (None, "chatgpt")
        or any(
            auth.get(key) is not None
            for key in (
                "OPENAI_API_KEY",
                "personal_access_token",
                "bedrock_api_key",
                "bedrock_access_keys",
                "agent_identity",
            )
        )
    ):
        raise _auth_error("Codex isolation requires official ChatGPT file login")
    tokens = auth.get("tokens")
    if not isinstance(tokens, dict) or any(
        not isinstance(tokens.get(key), str) or not tokens[key]
        for key in ("id_token", "access_token", "refresh_token")
    ):
        raise _auth_error("Codex subscription auth.json has invalid token fields")
    selected: dict[str, Any] = {
        key: tokens[key] for key in ("id_token", "access_token", "refresh_token")
    }
    # TokenData has optional account_id/login_owner_id and OAuth provenance.
    # Preserve these rather than silently changing refresh authority/ownership.
    for key in ("account_id", "login_owner_id"):
        if key in tokens:
            value = tokens[key]
            if value is not None and not isinstance(value, str):
                raise _auth_error(
                    "Codex subscription auth.json has invalid token metadata"
                )
            selected[key] = value
    if "oauth_provenance" in tokens:
        provenance = tokens["oauth_provenance"]
        if provenance is None:
            selected["oauth_provenance"] = None
        elif isinstance(provenance, dict) and all(
            isinstance(provenance.get(key), str) for key in ("issuer", "client_id")
        ):
            selected["oauth_provenance"] = {
                key: provenance[key] for key in ("issuer", "client_id")
            }
        else:
            raise _auth_error(
                "Codex subscription auth.json has invalid OAuth provenance"
            )
    minimal: dict[str, Any] = {"tokens": selected}
    if "auth_mode" in auth:
        minimal["auth_mode"] = auth["auth_mode"]
    if "last_refresh" in auth:
        refresh = auth["last_refresh"]
        if refresh is not None and not isinstance(refresh, str):
            raise _auth_error(
                "Codex subscription auth.json has invalid refresh metadata"
            )
        minimal["last_refresh"] = refresh

    def register(value: object) -> None:
        if isinstance(value, str) and value:
            redactor.add_secret(value)
        elif isinstance(value, dict):
            for child in value.values():
                register(child)

    register(selected)
    return minimal


def _overlaps(left: Path, right: Path) -> bool:
    return left.is_relative_to(right) or right.is_relative_to(left)


@dataclass
class CodexIsolation:
    cwd: Path
    root: Path
    config: dict[str, Any]
    environment: dict[str, str]
    recorder: Recorder
    _processes: dict[Process, bool] = field(
        default_factory=dict, init=False, repr=False
    )
    _pending_spawns: int = field(default=0, init=False, repr=False)
    _unknown_spawns: int = field(default=0, init=False, repr=False)
    _removed: bool = field(default=False, init=False, repr=False)

    @property
    def codex_home(self) -> Path:
        return self.root / "codex"

    @classmethod
    def create(
        cls,
        cwd: Path,
        recorder: Recorder,
        environment: dict[str, str],
        *,
        log_dir: Path,
    ) -> "CodexIsolation":
        cwd = cwd.resolve(strict=True)
        log_dir = log_dir.resolve()
        root = Path(mkdtemp(prefix="multi-agent-codex-")).resolve()
        instance = cls(cwd, root, policy_config(cwd), dict(environment), recorder)
        try:
            root.chmod(0o700)
            if not cwd.is_dir() or any(
                _overlaps(left, right)
                for left, right in ((root, cwd), (root, log_dir), (cwd, log_dir))
            ):
                raise RuntimeFailure(
                    Error(
                        "Codex isolation, frozen input and log directories must be separate",
                        raw_type="adapter.isolation_path",
                    )
                )
            for name in ("home", "codex", "tmp"):
                (root / name).mkdir(mode=0o700)
            instance.environment.update(
                HOME=str(root / "home"),
                CODEX_HOME=str(instance.codex_home),
                TMPDIR=str(root / "tmp"),
            )
            source = (
                Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
                / "auth.json"
            )
            try:
                auth = json.loads(source.read_bytes())
            except (OSError, ValueError) as error:
                raise _auth_error(
                    "Codex subscription auth.json is missing or unreadable"
                ) from error
            minimal = _minimal_auth(auth, recorder.redactor)
            for name, content in (
                ("auth.json", json.dumps(minimal)),
                ("config.toml", policy_toml(instance.config)),
            ):
                target = instance.codex_home / name
                with target.open("x", encoding="utf-8") as stream:
                    target.chmod(0o600)
                    stream.write(content)
            recorder.emit("isolation_created", owner=str(root), profile=PROFILE)
            return instance
        except BaseException as original:
            try:
                instance.cleanup()
            except (OSError, ValueError) as cleanup_error:
                # The authentication/path error remains the actionable failure;
                # cleanup() records the retained owner before it propagates.
                original.add_note(
                    "Codex isolation cleanup also failed: "
                    + type(cleanup_error).__name__
                )
            raise

    def _incomplete(self, reason: str) -> None:
        self.recorder.emit(
            "cleanup_incomplete",
            owner=str(self.root),
            resources_removed=False,
            reason=reason,
            pending_spawns=self._pending_spawns,
            unknown_spawns=self._unknown_spawns,
            unstopped_processes=sum(
                not stopped for stopped in self._processes.values()
            ),
        )

    async def _finish_spawn(self, launch: asyncio.Task[Process]) -> Process:
        try:
            process = await launch
        except SpawnFailed:
            self.recorder.emit(
                "isolation_spawn_failed", owner=str(self.root), child_created=False
            )
            raise
        except BaseException:
            self._unknown_spawns += 1
            self._incomplete("spawn_outcome_unknown")
            raise
        self._processes[process] = False
        return process

    async def spawn(self, argv: list[str], *, pipe: bool, grace: float) -> Process:
        """Own creation through cancellation; never release auth for a live child."""
        if self._removed:
            raise RuntimeError("Codex isolation resources have already been removed")
        self._pending_spawns += 1
        launch = asyncio.create_task(
            Process.start(
                "codex",
                argv,
                self.cwd,
                self.recorder,
                piped_stdin=pipe,
                grace=grace,
                env=self.environment,
                start_new_session=True,
            )
        )
        owned = asyncio.create_task(self._finish_spawn(launch))
        try:
            try:
                return await asyncio.shield(owned)
            except asyncio.CancelledError:

                async def stop_after_creation() -> None:
                    try:
                        process = await owned
                    except SpawnFailed:
                        return
                    await self.terminate(process, grace)

                cleanup = asyncio.create_task(stop_after_creation())
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        continue
                cleanup.result()
                raise
        finally:
            self._pending_spawns -= 1

    async def terminate(self, process: Process, grace: float) -> None:
        if process not in self._processes:
            raise ValueError("process is not owned by this Codex isolation")
        if self._processes[process]:
            return
        stopping = asyncio.create_task(process.terminate(grace))
        cancelled = False
        try:
            while not stopping.done():
                try:
                    await asyncio.shield(stopping)
                except asyncio.CancelledError:
                    cancelled = True
            stopping.result()
            if process.child.returncode is None:
                raise RuntimeError(
                    "Codex process stop returned without an observed OS exit"
                )
        except BaseException:
            self._incomplete("process_stop_not_confirmed")
            raise
        self._processes[process] = True
        if cancelled:
            raise asyncio.CancelledError

    def cleanup(self) -> bool:
        """No finalizer: unknown exit retains private resources and their locator."""
        if self._removed:
            return True
        if (
            self._pending_spawns
            or self._unknown_spawns
            or not all(self._processes.values())
        ):
            self._incomplete("owned_processes_not_confirmed_stopped")
            return False
        try:
            shutil.rmtree(self.root)
        except OSError:
            self._incomplete("resource_removal_failed")
            raise
        self._removed = True
        self.recorder.emit("isolation_removed", owner=str(self.root))
        return True
