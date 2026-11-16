"""Synthetic writer contracts; these tests do not establish OS sandbox behavior."""

import asyncio
import json
import os
import signal
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from multi_agent.adapters import codex_isolation
from multi_agent.adapters import runtime as runtime_module
from multi_agent.adapters.base import Permission, SessionConfig
from multi_agent.adapters.codex_isolation import CodexIsolation
from multi_agent.adapters.implementation_runtime import ImplementationRuntimeAdapter
from multi_agent.adapters.probe.process import Process
from multi_agent.adapters.probe.recording import Recorder, Redactor
from multi_agent.adapters.runtime import RuntimeAdapter, RuntimeFailure, _Rpc
from multi_agent.cli.workspaces import create_workspace, executor_lock, remove_workspace
from multi_agent.core.events import (
    Completed,
    Error,
    FailureCategory,
    Message,
    ProcessExited,
    SessionStarted,
)

PROFILE = "multi-agent-owned-implementation"
TOOLS = ["Read", "Grep", "Glob", "Edit", "Write"]


def _git(repository, *arguments):
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_TERMINAL_PROMPT="0",
    )
    result = subprocess.run(
        [
            "git",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "commit.gpgSign=false",
            "-C",
            str(repository),
            *arguments,
        ],
        env=environment,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result.stdout.decode().strip()


@pytest.fixture
def owned_workspace(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "Synthetic")
    _git(repository, "config", "user.email", "fixture@example.invalid")
    (repository / "input.txt").write_text("baseline\n")
    _git(repository, "add", "input.txt")
    _git(repository, "commit", "-qm", "baseline")
    baseline = _git(repository, "rev-parse", "HEAD")
    with executor_lock(repository) as lease:
        record = create_workspace(lease, baseline, tmp_path / "owned-workspace")
        try:
            yield record
        finally:
            remove_workspace(lease, record)


@pytest.fixture(autouse=True)
def synthetic_environment(tmp_path, monkeypatch):
    auth_home = tmp_path / "synthetic-auth"
    auth_home.mkdir(mode=0o700)
    auth = auth_home / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    key: "synthetic-" + key
                    for key in ("id_token", "access_token", "refresh_token")
                },
            }
        )
    )
    auth.chmod(0o600)
    monkeypatch.setenv("CODEX_HOME", str(auth_home))
    monkeypatch.setenv("FAKE_IMPLEMENTATION", "1")
    original = RuntimeAdapter._environment

    def fake_environment(adapter):
        environment = original(adapter)
        environment.update(
            (key, value) for key, value in os.environ.items() if key.startswith("FAKE_")
        )
        return environment

    monkeypatch.setattr(RuntimeAdapter, "_environment", fake_environment)


def _runtime(
    tmp_path,
    monkeypatch,
    workspace,
    provider,
    mode="success",
    *,
    schema=True,
    deadline_at=None,
):
    monkeypatch.setenv("FAKE_RUNTIME_MODE", mode)
    capture = tmp_path / "capture.jsonl"
    monkeypatch.setenv("FAKE_RUNTIME_CAPTURE", str(capture))
    executable = tmp_path / f"{provider}-fake"
    executable.write_text(
        f"#!{sys.executable}\n"
        + (Path(__file__).parent / "runtime_fakes/fake_cli.py").read_text()
    )
    executable.chmod(0o755)
    adapter = ImplementationRuntimeAdapter(
        provider,
        workspace=workspace,
        executable=str(executable),
        log_dir=tmp_path / "logs",
        output_schema={"type": "object"} if schema else None,
        timeout=3,
        grace=0.05,
        deadline_at=deadline_at,
    )
    config = SessionConfig(
        "requested-model", "high", Permission.WORKSPACE_WRITE, Path(workspace.root)
    )
    return adapter, config, capture


async def _collect(adapter, config):
    session = await adapter.start_session(config)
    turn = await adapter.submit_turn(session, "literal ; `touch nope` $(touch nope)")
    return [event async for event in adapter.events(turn)], await adapter.result(turn)


def _records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def _log_records(tmp_path):
    return _records(next((tmp_path / "logs").glob("*.jsonl")))


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_default_runtime_still_rejects_write_and_implementation_requires_owned_root(
    owned_workspace, tmp_path, monkeypatch, provider
):
    adapter, config, capture = _runtime(
        tmp_path, monkeypatch, owned_workspace, provider
    )
    review = RuntimeAdapter(provider, log_dir=tmp_path / "review-logs")

    async def run():
        with pytest.raises(ValueError):
            await review.start_session(config)
        with pytest.raises(ValueError):
            await adapter.start_session(
                replace(config, permission=Permission.READ_ONLY)
            )
        with pytest.raises(ValueError):
            await adapter.start_session(
                replace(config, cwd=Path(owned_workspace.source))
            )
        nested = config.cwd / "nested"
        nested.mkdir()
        with pytest.raises(ValueError):
            await adapter.start_session(replace(config, cwd=nested))
        # Lexical normalization of the same root is allowed; descendants are not.
        await adapter.start_session(replace(config, cwd=nested / ".."))
        assert not capture.exists()

    asyncio.run(run())


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize("after_start", [False, True])
@pytest.mark.parametrize("replacement", ["directory", "symlink"])
def test_implementation_runtime_rejects_replaced_root(
    owned_workspace, tmp_path, monkeypatch, provider, after_start, replacement
):
    adapter, config, capture = _runtime(
        tmp_path, monkeypatch, owned_workspace, provider
    )
    parked = tmp_path / "parked-original-root"

    async def run():
        session = await adapter.start_session(config) if after_start else None
        config.cwd.rename(parked)
        if replacement == "symlink":
            config.cwd.symlink_to(parked, target_is_directory=True)
        else:
            config.cwd.mkdir()
        try:
            if session is None:
                with pytest.raises(ValueError):
                    await adapter.start_session(config)
            else:
                turn = await adapter.submit_turn(session, "must not launch")
                events = [event async for event in adapter.events(turn)]
                result = await adapter.result(turn)
                assert isinstance(result.terminal, Error)
                assert result.terminal.raw_type == "adapter.implementation_policy"
                assert result.process_exit is None
                assert not any(isinstance(event, SessionStarted) for event in events)
            assert not capture.exists()
        finally:
            if replacement == "symlink":
                config.cwd.unlink()
            else:
                config.cwd.rmdir()
            parked.rename(config.cwd)

    asyncio.run(run())


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize("location", ["root", "repository", "git_directory"])
def test_implementation_runtime_rejects_logs_in_owned_or_git_directories(
    owned_workspace, tmp_path, monkeypatch, provider, location
):
    adapter, config, capture = _runtime(
        tmp_path, monkeypatch, owned_workspace, provider
    )
    adapter.log_dir = Path(getattr(owned_workspace, location)) / "unsafe-runtime-logs"
    with pytest.raises(ValueError):
        asyncio.run(adapter.start_session(config))
    assert not capture.exists()
    assert not adapter.log_dir.exists()


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_implementation_runtime_checks_log_symlink_before_opening_recorder(
    owned_workspace, tmp_path, monkeypatch, provider
):
    adapter, config, capture = _runtime(
        tmp_path, monkeypatch, owned_workspace, provider
    )

    async def run():
        session = await adapter.start_session(config)
        adapter.log_dir.symlink_to(config.cwd, target_is_directory=True)
        try:
            turn = await adapter.submit_turn(session, "must not create logs")
            events = [event async for event in adapter.events(turn)]
            result = await adapter.result(turn)
            assert isinstance(result.terminal, Error)
            assert result.terminal.raw_type == "adapter.implementation_policy"
            assert result.process_exit is None
            assert not any(isinstance(event, SessionStarted) for event in events)
            assert not list(config.cwd.glob("*.jsonl"))
            assert not capture.exists()
        finally:
            adapter.log_dir.unlink()

    asyncio.run(run())


@pytest.mark.parametrize("boundary", ["version", "account"])
def test_implementation_runtime_rechecks_root_after_codex_awaits(
    owned_workspace, tmp_path, monkeypatch, boundary
):
    adapter, config, capture = _runtime(tmp_path, monkeypatch, owned_workspace, "codex")
    parked = tmp_path / "parked-root-after-response"

    def replace_root():
        config.cwd.rename(parked)
        config.cwd.mkdir()

    if boundary == "version":
        original_collect = RuntimeAdapter._collect

        async def replace_after_version(self, argv, cwd, recorder, **kwargs):
            result = await original_collect(self, argv, cwd, recorder, **kwargs)
            if argv[1:] == ["--version"]:
                replace_root()
            return result

        monkeypatch.setattr(RuntimeAdapter, "_collect", replace_after_version)
    else:
        original_request = _Rpc.request

        async def replace_after_account(self, method, params):
            result = await original_request(self, method, params)
            if method == "account/read":
                replace_root()
            return result

        monkeypatch.setattr(_Rpc, "request", replace_after_account)
    try:
        events, result = asyncio.run(_collect(adapter, config))
        assert parked.is_dir(), "the real fake response must precede root replacement"
        assert isinstance(result.terminal, Error)
        assert result.terminal.raw_type == "adapter.implementation_policy"
        assert not any(
            isinstance(event, (SessionStarted, Completed)) for event in events
        )
        records = _records(capture)
        if boundary == "version":
            assert [row["argv"] for row in records if row["kind"] == "spawn"] == [
                ["--version"]
            ]
        else:
            methods = [
                row["message"].get("method")
                for row in records
                if row["kind"] == "request"
            ]
            assert "account/read" in methods
            assert "thread/start" not in methods and "turn/start" not in methods
        owner = next(
            row["owner"]
            for row in _log_records(tmp_path)
            if row["kind"] == "isolation_created"
        )
        assert not Path(owner).exists()
    finally:
        if parked.exists():
            config.cwd.rmdir()
            parked.rename(config.cwd)


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize("schema", [False, True])
def test_implementation_permission_contract(
    owned_workspace, tmp_path, monkeypatch, provider, schema
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-api-secret")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "synthetic-auth-secret")
    adapter, config, capture = _runtime(
        tmp_path, monkeypatch, owned_workspace, provider, schema=schema
    )
    events, result = asyncio.run(_collect(adapter, config))
    assert isinstance(result.terminal, Completed)
    assert result.process_exit is not None
    assert isinstance(events[-1], ProcessExited)
    assert not (config.cwd / "nope").exists()
    records = _records(capture)
    spawn = next(
        row
        for row in records
        if row["kind"] == "spawn" and row["argv"][:1] in (["-p"], ["app-server"])
    )
    assert spawn["cwd"] == owned_workspace.root
    if provider == "claude":
        expected = {
            "--model": "requested-model",
            "--effort": "high",
            "--output-format": "stream-json",
            "--tools": "Read,Grep,Glob,Edit,Write",
            "--permission-mode": "acceptEdits",
            "--permission-prompts": "none",
        }
        argv = spawn["argv"]
        assert argv[:2] == ["-p", "literal ; `touch nope` $(touch nope)"]
        remaining = argv[2:]
        for flag, value in expected.items():
            position = remaining.index(flag)
            assert remaining[position + 1] == value
            del remaining[position : position + 2]
        if schema:
            position = remaining.index("--json-schema")
            assert json.loads(remaining[position + 1]) == {"type": "object"}
            del remaining[position : position + 2]
        assert sorted(remaining) == sorted(
            [
                "--restricted",
                "--safe-mode",
                "--strict-mcp-config",
                "--verbose",
                "--no-session-persistence",
            ]
        )
        assert not spawn["api_key_present"] and not spawn["auth_token_present"]
        init = next(
            row["message"] for row in records if row["kind"] == "implementation-init"
        )
        assert set(init["tools"]) == set(
            TOOLS + (["StructuredOutput"] if schema else [])
        )
        assert init["plugins"] and init["skills"]
    else:
        assert spawn["argv"] == ["app-server", "--strict-config"]
        policy = next(
            row["config"] for row in records if row["kind"] == "implementation-config"
        )
        assert policy["default_permissions"] == PROFILE
        assert policy["permissions"] == {
            PROFILE: {
                "filesystem": {
                    ":minimal": "read",
                    owned_workspace.root: "write",
                    str(Path(owned_workspace.root) / ".git"): "deny",
                    owned_workspace.git_directory: "deny",
                    owned_workspace.repository: "deny",
                },
                "network": {"enabled": False},
            }
        }
        requests = {
            row["message"].get("method"): row["message"].get("params", {})
            for row in records
            if row["kind"] == "request"
        }
        for method in ("thread/start", "turn/start"):
            assert requests[method]["approvalPolicy"] == "never"
            assert not {"sandbox", "sandboxPolicy"} & requests[method].keys()
        assert requests["thread/start"]["cwd"] == owned_workspace.root
        assert requests["turn/start"]["effort"] == "high"
        assert requests["turn/start"].get("outputSchema") == (
            {"type": "object"} if schema else None
        )
        logs = _log_records(tmp_path)
        for kind in ("isolation_created", "isolation_verified"):
            assert (
                next(row for row in logs if row["kind"] == kind)["profile"] == PROFILE
            )
        owner = next(row["owner"] for row in logs if row["kind"] == "isolation_created")
        assert not Path(owner).exists()
    log = next((tmp_path / "logs").glob("*.jsonl")).read_text()
    assert "synthetic-api-secret" not in log and "synthetic-auth-secret" not in log


def test_implementation_runtime_environment_check_keeps_review_policy(
    owned_workspace, tmp_path, monkeypatch
):
    adapter, _, capture = _runtime(tmp_path, monkeypatch, owned_workspace, "codex")
    environment = asyncio.run(adapter.check_environment())
    assert environment.logged_in is True
    assert environment.models == ("model-1", "model-2")
    records = _records(capture)
    policy = next(
        row["config"] for row in records if row["kind"] == "implementation-config"
    )
    assert policy["default_permissions"] == "multi-agent-frozen-review"
    assert set(policy["permissions"]) == {"multi-agent-frozen-review"}
    assert set(
        policy["permissions"]["multi-agent-frozen-review"]["filesystem"].values()
    ) == {"read"}
    assert not any(
        row["kind"] == "request" and row["message"].get("method") == "turn/start"
        for row in records
    )


@pytest.mark.parametrize(
    "mode",
    [
        "implementation-root-readonly",
        "implementation-missing-git-deny",
        "config-ignored-profile",
        "config-profile-extends",
        "config-extra-filesystem",
        "config-extra-profile",
        "config-network-enabled",
        "config-null-permission-key",
        "config-null-top-level",
        "config-system-layer",
        "config-mdm-layer",
        "config-mcp-servers",
        "config-instructions",
        "requirements-empty",
        "requirements-unknown-null",
        "requirements-missing",
        "account-logged-out",
        "implementation-account-api-key",
    ],
)
def test_implementation_policy_drift_is_rejected(
    owned_workspace, tmp_path, monkeypatch, mode
):
    adapter, config, capture = _runtime(
        tmp_path, monkeypatch, owned_workspace, "codex", mode
    )
    events, result = asyncio.run(_collect(adapter, config))
    assert isinstance(result.terminal, Error)
    assert result.terminal.raw_type == (
        "account/read" if mode == "account-logged-out" else "adapter.isolation_failed"
    )
    assert not any(isinstance(event, Completed) for event in events)
    assert not any(
        row["kind"] == "request" and row["message"].get("method") == "turn/start"
        for row in _records(capture)
    )
    logs = _log_records(tmp_path)
    owner = next(row["owner"] for row in logs if row["kind"] == "isolation_created")
    assert not Path(owner).exists()
    assert any(row["kind"] == "isolation_removed" for row in logs)


@pytest.mark.parametrize(
    "field,value",
    [
        ("cwd", "/unowned"),
        ("tools", TOOLS + ["Bash"]),
        ("tools", TOOLS[:-1]),
        ("tools", TOOLS + ["StructuredOutput", "Write"]),
        ("tools", "Read,Grep,Glob,Edit,Write"),
        ("permissionMode", "bypassPermissions"),
        ("claude_code_version", "2.1.324"),
        ("mcp_servers", [{"name": "foreign"}]),
        ("mcp_servers", {}),
        ("additional_directories", ["/unowned"]),
        ("apiKeySource", "ANTHROPIC_API_KEY"),
    ],
)
def test_implementation_runtime_rejects_claude_init_drift(
    owned_workspace, tmp_path, monkeypatch, field, value
):
    monkeypatch.setenv("FAKE_IMPLEMENTATION_INIT_PATCH", json.dumps({field: value}))
    adapter, config, _ = _runtime(tmp_path, monkeypatch, owned_workspace, "claude")
    events, result = asyncio.run(_collect(adapter, config))
    assert isinstance(result.terminal, Error)
    assert result.terminal.raw_type == "adapter.implementation_policy"
    assert not any(isinstance(event, (Completed, SessionStarted)) for event in events)


@pytest.mark.parametrize(
    "field",
    [
        "cwd",
        "tools",
        "permissionMode",
        "claude_code_version",
        "mcp_servers",
        "additional_directories",
        "apiKeySource",
    ],
)
def test_implementation_runtime_requires_all_claude_init_fields(
    owned_workspace, tmp_path, monkeypatch, field
):
    monkeypatch.setenv("FAKE_IMPLEMENTATION_INIT_DROP", json.dumps([field]))
    adapter, config, _ = _runtime(tmp_path, monkeypatch, owned_workspace, "claude")
    events, result = asyncio.run(_collect(adapter, config))
    assert isinstance(result.terminal, Error)
    assert result.terminal.raw_type == "adapter.implementation_policy"
    assert not any(isinstance(event, (Completed, SessionStarted)) for event in events)


@pytest.mark.parametrize(
    "mode",
    [
        "implementation-missing-init",
        "implementation-before-init",
        "implementation-unknown-before-init",
        "implementation-duplicate-init",
        "early-exit",
    ],
)
def test_implementation_runtime_requires_first_and_single_claude_init(
    owned_workspace, tmp_path, monkeypatch, mode
):
    adapter, config, _ = _runtime(
        tmp_path, monkeypatch, owned_workspace, "claude", mode
    )
    events, result = asyncio.run(_collect(adapter, config))
    assert isinstance(result.terminal, Error)
    assert result.terminal.raw_type == "adapter.implementation_policy"
    assert not any(isinstance(event, Completed) for event in events)


def test_implementation_runtime_structured_tool_requires_requested_schema(
    owned_workspace, tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "FAKE_IMPLEMENTATION_INIT_PATCH",
        json.dumps({"tools": TOOLS + ["StructuredOutput"]}),
    )
    adapter, config, _ = _runtime(
        tmp_path, monkeypatch, owned_workspace, "claude", schema=False
    )
    _, result = asyncio.run(_collect(adapter, config))
    assert isinstance(result.terminal, Error)
    assert result.terminal.raw_type == "adapter.implementation_policy"


def test_implementation_runtime_schema_requires_reported_structured_tool(
    owned_workspace, tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_IMPLEMENTATION_INIT_PATCH", json.dumps({"tools": TOOLS}))
    adapter, config, _ = _runtime(tmp_path, monkeypatch, owned_workspace, "claude")
    _, result = asyncio.run(_collect(adapter, config))
    assert isinstance(result.terminal, Error)
    assert result.terminal.raw_type == "adapter.implementation_policy"


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_implementation_preserves_terminal_exit_and_unknown_evidence(
    owned_workspace, tmp_path, monkeypatch, provider
):
    adapter, config, _ = _runtime(
        tmp_path, monkeypatch, owned_workspace, provider, "implementation-signal-exit"
    )
    events, result = asyncio.run(_collect(adapter, config))
    assert isinstance(result.terminal, Completed)
    assert result.terminal.reported_model == (
        "actual-claude" if provider == "claude" else None
    )
    started = next(event for event in events if isinstance(event, SessionStarted))
    assert started.reported_model is None and started.reported_effort is None
    assert result.process_exit is not None
    assert result.process_exit.exit_code == -signal.SIGTERM
    assert isinstance(events[-2], Completed) and isinstance(events[-1], ProcessExited)


def test_implementation_runtime_does_not_fill_unreported_claude_model(
    owned_workspace, tmp_path, monkeypatch
):
    adapter, config, _ = _runtime(
        tmp_path,
        monkeypatch,
        owned_workspace,
        "claude",
        "implementation-unreported-model",
    )
    events, result = asyncio.run(_collect(adapter, config))
    assert isinstance(result.terminal, Completed)
    assert result.terminal.reported_model is None
    assert all(
        event.reported_effort is None
        for event in events
        if isinstance(event, SessionStarted)
    )


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_implementation_runtime_cancellation_waits_for_stopped_process(
    owned_workspace, tmp_path, monkeypatch, provider
):
    adapter, config, _ = _runtime(
        tmp_path, monkeypatch, owned_workspace, provider, "wait"
    )

    async def run():
        turn = await adapter.submit_turn(await adapter.start_session(config), "wait")
        events = []
        async for event in adapter.events(turn):
            events.append(event)
            if isinstance(event, Message) and event.text == "waiting":
                assert await adapter.cancel(turn)
        return events, await adapter.result(turn)

    events, result = asyncio.run(run())
    assert isinstance(result.terminal, Error)
    assert result.terminal.failure_category == FailureCategory.CANCELLED
    assert result.process_exit is not None
    assert isinstance(events[-1], ProcessExited)


def test_implementation_runtime_cleanup_failure_keeps_terminal_without_exit(
    owned_workspace, tmp_path, monkeypatch
):
    adapter, config, _ = _runtime(
        tmp_path, monkeypatch, owned_workspace, "claude", "hang-after-result"
    )
    original = Process.terminate
    owned = []

    async def fail_cleanup(process, grace):
        if process not in owned:
            owned.append(process)
        raise OSError("synthetic cleanup failure")

    monkeypatch.setattr(Process, "terminate", fail_cleanup)

    async def run():
        try:
            return await _collect(adapter, config)
        finally:
            monkeypatch.setattr(Process, "terminate", original)
            for index, process in enumerate(owned):
                with Recorder(
                    tmp_path / f"cleanup-{index}.jsonl", Redactor()
                ) as recorder:
                    process._recorder = recorder
                    await original(process, 0.05)

    events, result = asyncio.run(run())
    assert owned
    assert isinstance(result.terminal, Completed)
    assert result.process_exit is None
    assert not any(isinstance(event, ProcessExited) for event in events)
    assert any(row["kind"] == "cleanup_incomplete" for row in _log_records(tmp_path))


@pytest.mark.parametrize("location", ["root", "repository", "git_directory", "control"])
def test_implementation_runtime_invalid_policy_allocates_no_isolation_owner(
    owned_workspace, tmp_path, monkeypatch, location
):
    record = owned_workspace
    control = Path(record.root) / ".git"
    parked_control = tmp_path / "original-git-control"
    alias = tmp_path / "metadata-alias"
    if location == "control":
        control.rename(parked_control)
        control.symlink_to(parked_control)
    else:
        alias.symlink_to(getattr(record, location), target_is_directory=True)
        record = replace(record, **{location: str(alias)})
    allocated = []

    def unexpected_owner(*args, **kwargs):
        allocated.append(True)
        raise AssertionError("invalid policy reached private owner allocation")

    monkeypatch.setattr(codex_isolation, "mkdtemp", unexpected_owner)
    try:
        with (
            Recorder(tmp_path / "invalid-policy.jsonl", Redactor()) as recorder,
            pytest.raises(RuntimeFailure),
        ):
            CodexIsolation.create(
                Path(owned_workspace.root),
                recorder,
                {},
                log_dir=tmp_path / "logs",
                workspace=record,
            )
        assert not allocated
    finally:
        if location == "control":
            control.unlink()
            parked_control.rename(control)


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize("absolute", [False, True], ids=["default-none", "absolute"])
def test_implementation_runtime_passes_shared_deadline(
    owned_workspace, tmp_path, monkeypatch, provider, absolute
):
    async def run():
        loop = asyncio.get_running_loop()
        deadline_at = loop.time() + 60 if absolute else None
        adapter, config, capture = _runtime(
            tmp_path, monkeypatch, owned_workspace, provider, deadline_at=deadline_at
        )
        assert adapter.deadline_at == deadline_at
        assert adapter.timeout == 3
        observed = []
        execute = getattr(adapter, f"_{provider}")

        async def observe_timer(state, recorder):
            # Capture the active timer before Completed legitimately disables it.
            observed.append((state.deadline.when(), loop.time()))
            await execute(state, recorder)

        monkeypatch.setattr(adapter, f"_{provider}", observe_timer)
        environment = await adapter.check_environment()
        assert environment.logged_in is True
        results = []
        for _ in range(2):
            before = loop.time()
            events, result = await _collect(adapter, config)
            when, observed_at = observed[-1]
            if absolute:
                assert when == deadline_at
            else:
                assert before + adapter.timeout <= when <= observed_at + adapter.timeout
            assert adapter.deadline_at == deadline_at
            results.append((events, result))
        assert len(observed) == 2
        if not absolute:
            assert observed[1][0] > observed[0][0]
        return adapter, config, capture, results

    adapter, config, capture, results = asyncio.run(run())
    for events, result in results:
        assert isinstance(result.terminal, Completed)
        assert result.terminal.reported_model == (
            "actual-claude" if provider == "claude" else None
        )
        started = next(event for event in events if isinstance(event, SessionStarted))
        assert started.reported_model is None and started.reported_effort is None
        assert result.process_exit is not None
        assert events[-1] == result.process_exit
        assert result.process_exit.exit_code == 0
        assert not any(isinstance(event, Error) for event in events)
    assert not (config.cwd / "nope").exists()
    records = _records(capture)
    model_spawns = [
        row
        for row in records
        if row["kind"] == "spawn"
        and row["cwd"] == owned_workspace.root
        and row["argv"][:1] in (["-p"], ["app-server"])
    ]
    assert len(model_spawns) == 2
    if provider == "claude":
        for spawn in model_spawns:
            argv = spawn["argv"]
            assert argv[:2] == ["-p", "literal ; `touch nope` $(touch nope)"]
            assert argv[argv.index("--tools") + 1] == ",".join(TOOLS)
            assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
            assert argv[argv.index("--permission-prompts") + 1] == "none"
            assert {"--restricted", "--safe-mode", "--strict-mcp-config"} <= set(argv)
    else:
        policies = [
            row["config"] for row in records if row["kind"] == "implementation-config"
        ]
        assert policies[0]["default_permissions"] == "multi-agent-frozen-review"
        writes = [
            policy for policy in policies if policy["default_permissions"] == PROFILE
        ]
        assert len(writes) == 2
        for policy in writes:
            assert policy["permissions"] == {
                PROFILE: {
                    "filesystem": {
                        ":minimal": "read",
                        owned_workspace.root: "write",
                        str(Path(owned_workspace.root) / ".git"): "deny",
                        owned_workspace.git_directory: "deny",
                        owned_workspace.repository: "deny",
                    },
                    "network": {"enabled": False},
                }
            }
        requests = [row["message"] for row in records if row["kind"] == "request"]
        for method in ("thread/start", "turn/start"):
            matching = [
                request["params"]
                for request in requests
                if request.get("method") == method
            ]
            assert len(matching) == 2
            for params in matching:
                assert params["approvalPolicy"] == "never"
                assert not {"sandbox", "sandboxPolicy"} & params.keys()
    receipt = adapter.ownership_receipt()
    assert receipt.processes_stopped and receipt.resources_released
    assert receipt.logs_sealed and receipt.outputs_complete
    assert all(not Path(isolation.root).exists() for isolation in receipt.isolations)
    assert all(not Path(directory.root).exists() for directory in receipt.directories)


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize(
    "invalid",
    [float("nan"), float("inf"), -float("inf"), True, False, "123.0", object()],
    ids=[
        "nan",
        "positive-infinity",
        "negative-infinity",
        "true",
        "false",
        "string",
        "object",
    ],
)
def test_implementation_runtime_rejects_invalid_deadline_before_resources(
    tmp_path, monkeypatch, provider, invalid
):
    touched = []

    def forbidden(*args, **kwargs):
        touched.append((args, kwargs))
        raise AssertionError("invalid deadline allocated runtime resources")

    monkeypatch.setattr(runtime_module, "OwnershipLedger", forbidden)
    monkeypatch.setattr(runtime_module, "Recorder", forbidden)
    monkeypatch.setattr(RuntimeAdapter, "_spawn", forbidden)
    with pytest.raises(ValueError, match="deadline"):
        ImplementationRuntimeAdapter(
            provider,
            workspace=object(),
            executable="must-not-execute",
            log_dir=tmp_path / "logs",
            deadline_at=invalid,
        )
    assert touched == []
    assert not (tmp_path / "logs").exists()


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_implementation_runtime_expired_deadline_reports_timeout_without_spawn(
    owned_workspace, tmp_path, monkeypatch, provider
):
    spawns = []

    async def forbidden_spawn(*args, **kwargs):
        spawns.append((args, kwargs))
        raise AssertionError("expired deadline spawned a process")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_spawn)

    async def run():
        deadline_at = asyncio.get_running_loop().time() - 1
        adapter, config, capture = _runtime(
            tmp_path, monkeypatch, owned_workspace, provider, deadline_at=deadline_at
        )
        assert adapter.deadline_at == deadline_at
        assert adapter.ownership_receipt().operations == ()
        with pytest.raises(RuntimeFailure) as failed:
            await adapter.check_environment()
        assert failed.value.event.failure_category == FailureCategory.TIMEOUT
        assert failed.value.event.raw_type != "adapter.cleanup_incomplete"
        events, result = await _collect(adapter, config)
        return adapter, capture, events, result

    adapter, capture, events, result = asyncio.run(run())
    assert spawns == []
    assert not capture.exists()
    assert isinstance(result.terminal, Error)
    assert result.terminal.failure_category == FailureCategory.TIMEOUT
    assert result.terminal.raw_type != "adapter.cleanup_incomplete"
    assert result.process_exit is None
    assert not any(
        isinstance(event, (Completed, SessionStarted, ProcessExited))
        for event in events
    )
    receipt = adapter.ownership_receipt()
    assert len(receipt.operations) == 2
    assert receipt.processes == ()
    assert (
        receipt.processes_stopped and receipt.resources_released and receipt.logs_sealed
    )
    assert len(receipt.logs) == 2


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_implementation_runtime_shared_deadline_preserves_session_boundaries(
    owned_workspace, tmp_path, monkeypatch, provider
):
    async def run():
        deadline_at = asyncio.get_running_loop().time() + 60
        adapter, config, capture = _runtime(
            tmp_path, monkeypatch, owned_workspace, provider, deadline_at=deadline_at
        )
        review = RuntimeAdapter(
            provider, log_dir=tmp_path / "review-logs", deadline_at=deadline_at
        )
        with pytest.raises(ValueError, match="read-only"):
            await review.start_session(config)
        with pytest.raises(ValueError, match="workspace-write"):
            await adapter.start_session(
                replace(config, permission=Permission.READ_ONLY)
            )
        with pytest.raises(ValueError, match="owned root"):
            await adapter.start_session(
                replace(config, cwd=Path(owned_workspace.source))
            )
        nested = config.cwd / "nested"
        nested.mkdir()
        with pytest.raises(ValueError, match="owned root"):
            await adapter.start_session(replace(config, cwd=nested))
        await adapter.start_session(replace(config, cwd=nested / ".."))
        assert not capture.exists()
        assert adapter.ownership_receipt().operations == ()
        assert review.ownership_receipt().operations == ()
        assert not adapter.log_dir.exists() and not review.log_dir.exists()

    asyncio.run(run())
