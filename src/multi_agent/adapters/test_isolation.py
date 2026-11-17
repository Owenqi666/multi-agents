"""The fixed test checkout/scratch permission profile, never a model session."""

import asyncio
import json
import os
import stat
from pathlib import Path
from tempfile import mkdtemp
from typing import Any

from multi_agent.core.task_test_execution import (
    TaskTestCheckout,
    TaskTestPolicy,
    TaskTestScratch,
)

from .codex_isolation import (
    CodexIsolation,
    _minimal_auth,
    isolated_environment,
    policy_config,
    policy_toml,
)
from .ownership import OwnershipLedger
from .probe.process import Process, SpawnFailed
from .probe.recording import Recorder
from .runtime import _CODEX_NULL_FIELDS, _CODEX_NULL_TOP_LEVEL
from .runtime_decode import strict_json_loads
from .test_recording import BoundedTestProcess, _state

PROFILE = "multi-agent-independent-tests"
VERSION = "codex-cli 0.169.0"
SYSTEM_PATH = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")


class TestCodexIsolation(CodexIsolation):
    __test__ = False

    async def terminate(self, process: Process, grace: float) -> None:
        try:
            await super().terminate(process, grace)
        except Exception:
            # A log failure does not erase independently observed physical stop.
            if (
                process in self._processes
                and process.ownership_record("observed").stopped
            ):
                self._processes[process] = True
            raise

    async def spawn(self, argv: list[str], *, pipe: bool, grace: float) -> Process:
        if self._removed:
            raise RuntimeError("test isolation has already been removed")
        ticket = (
            self._ownership.acquire("process") if self._ownership is not None else None
        )
        created: list[Process] = []

        def publish(process: Process) -> None:
            self._processes.setdefault(process, False)
            created.append(process)

        self._pending_spawns += 1
        launch = asyncio.create_task(
            BoundedTestProcess.start(
                "codex",
                argv,
                self.cwd,
                self.recorder,
                piped_stdin=pipe,
                grace=grace,
                env=self.environment,
                start_new_session=True,
                acquisition=ticket,
                on_created=publish,
            )
        )
        owned = asyncio.create_task(self._finish_spawn(launch, created, grace, ticket))
        try:
            try:
                return await asyncio.shield(owned)
            except asyncio.CancelledError:

                async def settle() -> None:
                    try:
                        process = await owned
                    except SpawnFailed:
                        return
                    await self.terminate(process, grace)

                cleanup = asyncio.create_task(settle())
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        continue
                cleanup.result()
                raise
        finally:
            self._pending_spawns -= 1
            if ticket is not None:
                ticket.finish()


def _directory(path: str, identity: tuple[int, int]) -> Path:
    result = Path(path)
    info = result.lstat()
    if (
        not result.is_absolute()
        or result.resolve(strict=True) != result
        or not stat.S_ISDIR(info.st_mode)
        or (info.st_dev, info.st_ino) != identity
    ):
        raise ValueError("test directory identity differs from preparation")
    return result


def test_command_environment(
    scratch: TaskTestScratch, policy: TaskTestPolicy
) -> dict[str, str | None]:
    return {
        "HOME": str(Path(scratch.path) / "home"),
        "TMPDIR": str(Path(scratch.path) / "tmp"),
        "XDG_CACHE_HOME": str(Path(scratch.path) / "cache"),
        "CODEX_HOME": None,
        "PATH": ":".join((*policy.tool_directories, *SYSTEM_PATH)),
        "GIT_OPTIONAL_LOCKS": "0",
    }


def test_policy_config(
    checkout: TaskTestCheckout, scratch: TaskTestScratch, policy: TaskTestPolicy
) -> dict[str, Any]:
    root = _directory(checkout.root, checkout.root_identity)
    git = _directory(checkout.git_directory, checkout.git_identity)
    scratch_root = _directory(scratch.path, scratch.identity)
    if git != root / ".git" or scratch_root != root.parent / "scratch":
        raise ValueError("test checkout/scratch layout differs from preparation")
    for name, identity in (
        ("home", scratch.home_identity),
        ("tmp", scratch.tmp_identity),
        ("cache", scratch.cache_identity),
    ):
        path = _directory(str(scratch_root / name), identity)
        if stat.S_IMODE(path.lstat().st_mode) != 0o700:
            raise ValueError("test scratch permissions changed")
    config = policy_config(root)
    config["default_permissions"] = PROFILE
    filesystem = {
        ":minimal": "read",
        str(root): "write",
        str(git): "read",
        str(scratch_root): "write",
        checkout.repository: "deny",
        checkout.database_path: "deny",
        str(root.parent / "context"): "deny",
        str(root.parent / "logs"): "deny",
    }
    home = Path.home().resolve()
    credential_roots = (
        home / ".codex",
        home / ".claude",
        home / ".ssh",
        home / ".aws",
        home / ".config",
        Path(os.environ.get("CODEX_HOME", str(home / ".codex"))).resolve(),
    )
    forbidden = (
        root.parent,
        Path(checkout.repository),
        Path(checkout.database_path),
        *credential_roots,
    )
    for raw in (*policy.tool_directories, *policy.runtime_read_paths):
        path = Path(raw)
        if (
            not path.is_absolute()
            or path.resolve(strict=True) != path
            or path == home
            or home.is_relative_to(path)
            or any(
                path.is_relative_to(item) or item.is_relative_to(path)
                for item in forbidden
            )
        ):
            raise ValueError("test runtime read path overlaps protected storage")
        filesystem[str(path)] = "read"
    for path in credential_roots:
        filesystem[str(path)] = "deny"
    config["permissions"] = {
        PROFILE: {"filesystem": filesystem, "network": {"enabled": False}}
    }
    config["shell_environment_policy"]["set"]["PATH"] = ":".join(
        (*policy.tool_directories, *SYSTEM_PATH)
    )
    return config


def create_test_isolation(
    checkout: TaskTestCheckout,
    scratch: TaskTestScratch,
    policy: TaskTestPolicy,
    recorder: Recorder,
    *,
    log_dir: Path,
    ownership: OwnershipLedger,
) -> CodexIsolation:
    config = test_policy_config(checkout, scratch, policy)
    ticket = ownership.acquire("isolation")
    instance: CodexIsolation | None = None
    try:
        root = Path(mkdtemp(prefix="multi-agent-test-codex-"))
        instance = TestCodexIsolation(
            Path(checkout.root),
            root,
            config,
            isolated_environment(),
            recorder,
            ownership,
        )
        ticket.publish(instance)
        root = root.resolve(strict=True)
        instance.root = root
        root.chmod(0o700)
        if root.is_relative_to(log_dir) or log_dir.is_relative_to(root):
            raise ValueError("test isolation overlaps logs")
        for name in ("home", "codex", "tmp"):
            (root / name).mkdir(mode=0o700)
        instance.environment.update(
            HOME=str(root / "home"),
            CODEX_HOME=str(root / "codex"),
            TMPDIR=str(root / "tmp"),
        )
        config["permissions"][PROFILE]["filesystem"][str(root)] = "deny"
        source = (
            Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
            / "auth.json"
        )
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size > 1024 * 1024:
                raise ValueError("test authentication input is not a bounded file")
            raw = bytearray()
            while len(raw) <= before.st_size:
                chunk = os.read(descriptor, min(8192, before.st_size + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            if (
                len(raw) != before.st_size
                or _state(os.fstat(descriptor)) != _state(before)
                or _state(source.lstat()) != _state(before)
            ):
                raise ValueError("test authentication input changed while reading")
        finally:
            os.close(descriptor)
        auth = _minimal_auth(strict_json_loads(raw.decode("utf-8")), recorder.redactor)
        for name, contents in (
            ("auth.json", json.dumps(auth)),
            ("config.toml", policy_toml(config)),
        ):
            path = instance.codex_home / name
            with path.open("x", encoding="utf-8") as stream:
                path.chmod(0o600)
                stream.write(contents)
        recorder.emit("isolation_created", owner=str(root), profile=PROFILE)
        return instance
    except BaseException as original:
        if instance is not None:
            try:
                instance.cleanup()
            except (OSError, RuntimeError, ValueError) as cleanup_error:
                original.add_note(
                    f"test isolation retained at {instance.root}: {type(cleanup_error).__name__}"
                )
        raise
    finally:
        ticket.finish()


def validate_test_configuration(
    response: dict[str, Any], requirements: dict[str, Any], isolation: CodexIsolation
) -> dict[str, Any]:
    """Same pinned normalization as the existing fixed-profile audit."""

    def equal(actual: Any, expected: Any, path: tuple[str, ...] = ()) -> bool:
        if not isinstance(expected, dict):
            return json.dumps(actual, sort_keys=True) == json.dumps(
                expected, sort_keys=True
            )
        if not isinstance(actual, dict) or not expected.keys() <= actual.keys():
            return False
        schema_path = path
        if len(path) >= 2 and path[:2] == ("permissions", PROFILE):
            schema_path = ("permissions", "multi-agent-frozen-review", *path[2:])
        optional = _CODEX_NULL_FIELDS.get(schema_path, frozenset())
        return all(
            equal(value, expected[key], (*path, key))
            if key in expected
            else key in optional and value is None
            for key, value in actual.items()
        )

    config, layers, origins = (
        response.get(key) for key in ("config", "layers", "origins")
    )
    if (
        not isinstance(config, dict)
        or not isinstance(layers, list)
        or not isinstance(origins, dict)
    ):
        raise TypeError("test configuration response is incomplete")
    own = 0
    for layer in layers:
        if not isinstance(layer, dict) or not isinstance(layer.get("name"), dict):
            raise TypeError("test configuration layer is malformed")
        name = layer["name"]
        if not isinstance(layer.get("version"), str) or not layer["version"]:
            raise ValueError("test configuration layer has no observed version")
        if name.get("type") == "user":
            if (
                name.get("file") != str(isolation.codex_home / "config.toml")
                or name.get("profile") is not None
                or layer.get("disabledReason") is not None
                or not equal(layer.get("config"), isolation.config)
            ):
                raise ValueError("test configuration user layer differs")
            own += 1
        elif not (
            name.get("type") == "system"
            and name.get("file") == "/etc/codex/config.toml"
            and layer.get("config") == {}
            and layer.get("disabledReason") is None
        ):
            raise ValueError("test configuration has an unapproved layer")
    defaults = {
        "background_terminal_max_timeout": 300000,
        "file_opener": "vscode",
        "include_permissions_instructions": True,
        "chatgpt_base_url": "https://chatgpt.com/backend-api/",
        "project_root_markers": [".git"],
        "mcp_oauth_credentials_store": "auto",
        "hide_agent_reasoning": False,
    }
    empty = {"profiles", "model_providers", "mcp_servers", "marketplaces"}
    if own != 1 or not all(
        key in config and equal(config[key], value, (key,))
        for key, value in isolation.config.items()
    ):
        raise ValueError("test effective configuration differs")
    for key, value in config.items():
        if key in isolation.config or key in _CODEX_NULL_TOP_LEVEL and value is None:
            continue
        if key in empty and type(value) is dict and not value:
            continue
        if key not in defaults or not equal(value, defaults[key]):
            raise ValueError("test effective configuration includes unknown settings")
    if "requirements" not in requirements or requirements["requirements"] is not None:
        raise ValueError("test runtime requirements are not confirmed empty")
    return {
        "effective_config": {key: config[key] for key in isolation.config},
        "sources": [layer["name"] for layer in layers],
        "requirements": None,
    }
