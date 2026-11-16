"""Probe orchestration uses synthetic adapters and disposable Git fixtures only."""

import asyncio
import copy
import hashlib
import json
import os
import signal
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from multi_agent.adapters import implementation_probe as probe
from multi_agent.adapters.base import (
    EnvironmentInfo,
    Permission,
    SessionHandle,
    TurnHandle,
    TurnResult,
)
from multi_agent.adapters.codex_isolation import CodexIsolation
from multi_agent.adapters.implementation_runtime import ImplementationRuntimeAdapter
from multi_agent.adapters.probe.process import Process
from multi_agent.adapters.probe.recording import Recorder, Redactor
from multi_agent.adapters.runtime import _CODEX_NULL_TOP_LEVEL
from multi_agent.core.events import (
    Completed,
    Error,
    FailureCategory,
    ProcessExited,
    SessionStarted,
)

SOURCE_HASHES = {
    "src/multi_agent/adapters/implementation_runtime.py": "1" * 64,
    "protocol/codex/config.schema.json": "2" * 64,
}


@pytest.fixture(autouse=True)
def prohibit_real_runtime_processes(monkeypatch):
    async def forbidden_async(*args, **kwargs):
        pytest.fail("probe test attempted to launch a real runtime process")

    def forbidden_shell(*args, **kwargs):
        pytest.fail("probe test attempted shell execution")

    original_run = subprocess.run

    def git_only(arguments, *args, **kwargs):
        assert isinstance(arguments, (list, tuple))
        assert Path(arguments[0]).name == "git", arguments
        assert not kwargs.get("shell", False)
        return original_run(arguments, *args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_async)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden_async)
    monkeypatch.setattr(Process, "start", forbidden_async)
    monkeypatch.setattr(subprocess, "run", git_only)
    monkeypatch.setattr(os, "system", forbidden_shell)


@pytest.fixture
def plan_path(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "_source_hashes", lambda: dict(SOURCE_HASHES))
    root = tmp_path / "probe-plan"
    probe.prepare_plan(root)
    return root / "plan.json"


def _load(path):
    return json.loads(path.read_text())


def _rewrite_plan(path, value, *, update_anchor=False):
    encoded = (json.dumps(value, indent=2) + "\n").encode()
    path.write_bytes(encoded)
    if update_anchor:
        owner_path = path.parent / ".probe-owner.json"
        owner = _load(owner_path)
        owner["plan_sha256"] = hashlib.sha256(encoded).hexdigest()
        owner_path.write_text(json.dumps(owner))


class FakeAdapter:
    """Keep real fixture I/O while independently supplying runtime observations."""

    def __init__(self, provider, workspace, log_dir, mode="success"):
        self.provider = provider
        self.workspace = workspace
        self.log_dir = log_dir
        self.mode = mode
        self.config = None
        self.prompts = []
        self.cancellations = []
        self.observed = []
        self.stopped = False

    async def check_environment(self):
        return EnvironmentInfo(
            cli_version="codex-cli 0.169.0"
            if self.provider == "codex"
            else "2.1.325 (Claude Code)",
            logged_in=True,
        )

    async def start_session(self, config):
        self.config = config
        root = Path(self.workspace.root)
        info = root.stat()
        assert config.cwd == root and config.permission == Permission.WORKSPACE_WRITE
        assert (info.st_dev, info.st_ino) == self.workspace.root_identity
        assert (root / "edit.txt").read_bytes() == b"probe baseline\n"
        assert not (root / "model-created.txt").exists()
        assert not self.log_dir.is_relative_to(root)
        return SessionHandle("synthetic-session")

    async def submit_turn(self, session, text):
        self.prompts.append(text)
        assert len(self.prompts) == 1
        case_root = Path(self.workspace.root).parent
        assert (case_root / "report.json").is_file(), (
            "running report precedes model submission"
        )
        root = Path(self.workspace.root)
        if self.mode != "no-writes":
            (root / "model-created.txt").write_bytes(b"model created marker\n")
            (root / "edit.txt").write_bytes(
                b"incorrect bytes\n"
                if self.mode == "wrong-bytes"
                else b"model edited marker\n"
            )
        if self.mode == "corrupt-control":
            with (root / ".git").open("ab") as stream:
                stream.write(b"unexpected control mutation\n")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        redactor = Redactor()
        redactor.add_workdir(root)
        with Recorder(
            self.log_dir / f"{self.provider}-synthetic.jsonl", redactor
        ) as recorder:
            recorder.emit(
                "recv",
                proc=self.provider,
                stream="stdout",
                message={
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": "I refused everything outside the workspace.",
                            }
                        ]
                    },
                },
            )
            if self.mode == "claude-tool-errors":
                outside = root.parent / "outside"
                targets = [
                    ("Read", {"file_path": str(outside / "read.txt")}),
                    (
                        "Write",
                        {
                            "file_path": str(outside / "model-new.txt"),
                            "content": "probe forbidden marker\n",
                        },
                    ),
                    (
                        "Write",
                        {
                            "file_path": str(root / "escape/model-alias.txt"),
                            "content": "probe forbidden marker\n",
                        },
                    ),
                    (
                        "Write",
                        {
                            "file_path": str(root / ".git"),
                            "content": "probe forbidden marker\n",
                        },
                    ),
                ]
                for index, (tool, inputs) in enumerate(targets):
                    identifier = f"synthetic-tool-{index}"
                    recorder.emit(
                        "recv",
                        proc="claude",
                        stream="stdout",
                        message={
                            "type": "assistant",
                            "session_id": "synthetic-runtime-session",
                            "message": {
                                "content": [
                                    {
                                        "type": "tool_use",
                                        "id": identifier,
                                        "name": tool,
                                        "input": inputs,
                                    }
                                ]
                            },
                        },
                    )
                    recorder.emit(
                        "recv",
                        proc="claude",
                        stream="stdout",
                        message={
                            "type": "user",
                            "session_id": "synthetic-runtime-session",
                            "message": {
                                "content": [
                                    {
                                        "type": "tool_result",
                                        "tool_use_id": identifier,
                                        "is_error": True,
                                        "content": "synthetic generic tool failure",
                                    }
                                ]
                            },
                        },
                    )
        return TurnHandle("synthetic-turn", session)

    async def events(self, turn):
        started = SessionStarted("synthetic-runtime-session")
        self.observed.append(started)
        yield started
        if self.mode == "stream-failure":
            raise OSError("synthetic event stream failure")
        if self.mode != "exit-only":
            terminal = (
                Error("synthetic cancelled", FailureCategory.CANCELLED)
                if self.mode == "cancelled"
                else Error("synthetic model error")
                if self.mode == "model-error"
                else Completed(
                    {"claim": "all done"}, reported_model="runtime-reported-model"
                )
            )
            self.observed.append(terminal)
            yield terminal
        if self.mode != "unknown-stop":
            exited = ProcessExited(-signal.SIGTERM)
            self.observed.append(exited)
            self.stopped = True
            yield exited

    async def result(self, turn):
        return TurnResult(
            next(
                (
                    event
                    for event in self.observed
                    if isinstance(event, (Completed, Error))
                ),
                None,
            ),
            next(
                (event for event in self.observed if isinstance(event, ProcessExited)),
                None,
            ),
        )

    async def cancel(self, turn, *, on_accepted=None):
        self.cancellations.append(turn)
        if on_accepted is not None:
            on_accepted()
        return True


@pytest.fixture
def fake_adapters(monkeypatch):
    created = []
    settings = {"mode": "success"}

    def factory(provider, *, workspace, log_dir):
        adapter = FakeAdapter(provider, workspace, log_dir, settings["mode"])
        created.append(adapter)
        return adapter

    monkeypatch.setattr(probe, "_make_adapter", factory)
    return settings, created


def test_probe_default_only_prepares_reviewable_plan(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("preparing a plan must not use CLI, authentication, or adapters")

    monkeypatch.setattr(probe, "_source_hashes", lambda: dict(SOURCE_HASHES))
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(CodexIsolation, "create", forbidden)
    monkeypatch.setattr(probe, "_make_adapter", forbidden)
    original_read = Path.read_bytes

    def no_auth(path):
        assert path.name != "auth.json"
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", no_auth)
    root = tmp_path / "prepare-only"
    plan = probe.prepare_plan(root)
    assert plan == _load(root / "plan.json")
    assert plan["root"] == str(root.resolve())
    assert plan["source_hashes"] == SOURCE_HASHES
    assert plan["schema_version"] == 1 and plan["created_at"]
    assert set(plan["cases"]) == {"codex-native", "codex-write", "claude-write"}
    assert plan["requests"] == {
        "codex": {"model": "gpt-6.1-terra", "effort": "medium"},
        "claude": {"model": "claude-sonnet-5-5", "effort": "medium"},
    }
    assert plan["owner"]
    owner = _load(root / ".probe-owner.json")
    assert owner["owner"] == plan["owner"]
    assert owner["root_identity"] == [root.stat().st_dev, root.stat().st_ino]
    assert (
        owner["plan_sha256"]
        == hashlib.sha256((root / "plan.json").read_bytes()).hexdigest()
    )
    assert (root / ".probe-owner.json").stat().st_mode & 0o077 == 0
    assert {path.name for path in root.iterdir()} == {"plan.json", ".probe-owner.json"}


@pytest.mark.parametrize(
    "kind", ["existing-directory", "existing-file", "symlink", "git-ancestor"]
)
def test_probe_prepare_rejects_occupied_or_aliased_workspace_roots(
    tmp_path, monkeypatch, kind
):
    monkeypatch.setattr(probe, "_source_hashes", lambda: dict(SOURCE_HASHES))
    root = tmp_path / "invalid-root"
    if kind == "existing-directory":
        root.mkdir()
    elif kind == "existing-file":
        root.write_text("preserve")
    elif kind == "symlink":
        real = tmp_path / "real-parent"
        real.mkdir()
        alias = tmp_path / "alias-parent"
        alias.symlink_to(real, target_is_directory=True)
        root = alias / "new-plan"
    else:
        parent = tmp_path / "existing-worktree"
        parent.mkdir()
        (parent / ".git").write_text("gitdir: /not-used\n")
        root = parent / "new-plan"
    with pytest.raises(ValueError):
        probe.prepare_plan(root)
    if root.is_dir():
        assert not (root / "plan.json").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("codex_effort", "invented"),
        ("claude_effort", "invented"),
        ("codex_model", ""),
        ("claude_model", ""),
    ],
)
def test_probe_prepare_rejects_invalid_requests_without_creating_root(
    tmp_path, field, value
):
    root = tmp_path / "invalid-request"
    with pytest.raises(ValueError):
        probe.prepare_plan(root, **{field: value})
    assert not root.exists()


def test_probe_rejects_stale_product_before_starting_case(
    plan_path, monkeypatch, fake_adapters
):
    monkeypatch.setattr(
        probe,
        "_source_hashes",
        lambda: SOURCE_HASHES | {"protocol/codex/config.schema.json": "3" * 64},
    )
    with pytest.raises(ValueError):
        asyncio.run(probe.execute_plan(plan_path, case="codex-write"))
    assert not fake_adapters[1]
    assert not (plan_path.parent / "codex-write").exists()


@pytest.mark.parametrize(
    "case", ["unknown", "all", "../codex-write", "codex-write/../claude-write", ""]
)
def test_probe_rejects_unknown_or_traversing_case_before_execution(
    plan_path, fake_adapters, case
):
    with pytest.raises(ValueError):
        asyncio.run(probe.execute_plan(plan_path, case=case))
    assert not fake_adapters[1]
    assert {path.name for path in plan_path.parent.iterdir()} == {
        "plan.json",
        ".probe-owner.json",
    }


@pytest.mark.parametrize(
    "tamper",
    [
        "owner",
        "root",
        "source-hash",
        "untrusted-source-path",
        "case-definition",
        "digest",
    ],
)
def test_probe_rejects_stale_or_tampered_plan(plan_path, fake_adapters, tamper):
    plan = _load(plan_path)
    if tamper == "owner":
        plan["owner"] = "different-owner"
    elif tamper == "root":
        plan["root"] = str(plan_path.parent.parent / "unowned")
    elif tamper == "source-hash":
        plan["source_hashes"][next(iter(SOURCE_HASHES))] = "0" * 64
    elif tamper == "untrusted-source-path":
        plan["source_hashes"]["../../private/auth.json"] = "0" * 64
    elif tamper == "case-definition":
        plan["cases"] = {"codex-native": {"command": ["sh", "-c", "untrusted"]}}
    else:
        plan["owner"] = "unanchored-change"
    # Semantic tampering is checked even if the byte hash anchor was also changed.
    _rewrite_plan(plan_path, plan, update_anchor=tamper != "digest")
    with pytest.raises(ValueError):
        asyncio.run(probe.execute_plan(plan_path, case="codex-write"))
    assert not fake_adapters[1]
    assert not (plan_path.parent / "codex-write").exists()


@pytest.mark.parametrize("target", ["plan.json", ".probe-owner.json"])
def test_probe_rejects_symlinked_plan_and_owner(plan_path, fake_adapters, target):
    original = plan_path.parent / target
    parked = plan_path.parent.parent / f"parked-{target}"
    original.rename(parked)
    original.symlink_to(parked)
    with pytest.raises(ValueError):
        asyncio.run(probe.execute_plan(plan_path, case="codex-write"))
    assert not fake_adapters[1]
    assert not (plan_path.parent / "codex-write").exists()


def test_probe_rejects_replaced_plan_root(plan_path, fake_adapters):
    parked = plan_path.parent.parent / "original-plan-root"
    plan_path.parent.rename(parked)
    plan_path.parent.mkdir()
    for name in ("plan.json", ".probe-owner.json"):
        (plan_path.parent / name).write_bytes((parked / name).read_bytes())
    with pytest.raises(ValueError):
        asyncio.run(probe.execute_plan(plan_path, case="codex-write"))
    assert not fake_adapters[1]
    assert not (plan_path.parent / "codex-write").exists()


@pytest.mark.parametrize("component", [0, 1], ids=["device", "inode"])
def test_probe_rejects_sidecar_identity_mismatch_before_adapter_execution(
    plan_path, fake_adapters, component
):
    owner_path = plan_path.parent / ".probe-owner.json"
    owner = _load(owner_path)
    owner["root_identity"][component] += 1
    owner_path.write_text(json.dumps(owner))
    before = plan_path.read_bytes()

    with pytest.raises(ValueError, match="no longer owned"):
        asyncio.run(probe.execute_plan(plan_path, case="codex-write"))

    assert not fake_adapters[1]
    assert plan_path.read_bytes() == before
    assert not (plan_path.parent / "codex-write").exists()


@pytest.mark.parametrize(
    "provider,field,value", [("codex", "effort", "invented"), ("claude", "model", "")]
)
def test_probe_revalidates_request_fields_before_execution(
    plan_path, fake_adapters, provider, field, value
):
    plan = _load(plan_path)
    plan["requests"][provider][field] = value
    _rewrite_plan(plan_path, plan, update_anchor=True)
    with pytest.raises(ValueError):
        asyncio.run(probe.execute_plan(plan_path, case="codex-write"))
    assert not fake_adapters[1]
    assert not (plan_path.parent / "codex-write").exists()


def test_model_probe_requires_files_and_runtime_evidence(plan_path, fake_adapters):
    report = asyncio.run(probe.execute_plan(plan_path, case="codex-write"))
    adapter = fake_adapters[1][0]
    assert report["status"] == "passed"
    assert report["requested"] == {"model": "gpt-6.1-terra", "effort": "medium"}
    assert adapter.config.model == "gpt-6.1-terra" and adapter.config.effort == "medium"
    assert report["terminal"]["reported_model"] == "runtime-reported-model"
    assert report["process_exit"] == -signal.SIGTERM
    assert report["cleanup"] == {
        "process_stopped": True,
        "workspace_removed": True,
        "error": None,
    }
    assert report["source_hashes"] == SOURCE_HASHES
    assert report["plan_sha256"] == hashlib.sha256(plan_path.read_bytes()).hexdigest()
    assert report["baseline"] == adapter.workspace.task_baseline
    assert len(report["baseline"]) == 40
    assert len(adapter.prompts) == 1
    assert not Path(adapter.workspace.root).exists()
    assert Path(adapter.workspace.source).is_dir()
    assert list(adapter.log_dir.glob("*.jsonl"))
    assert _load(plan_path.parent / "codex-write/report.json") == report
    assert plan_path.is_file()


@pytest.mark.parametrize(
    "mode", ["wrong-bytes", "no-writes", "exit-only", "model-error"]
)
def test_probe_cannot_pass_on_claims_exit_or_wrong_file_bytes(
    plan_path, fake_adapters, mode
):
    fake_adapters[0]["mode"] = mode
    report = asyncio.run(probe.execute_plan(plan_path, case="codex-write"))
    assert report["status"] != "passed"
    assert report["cleanup"]["process_stopped"] is True
    assert report["cleanup"]["workspace_removed"] is True
    assert report["process_exit"] == -signal.SIGTERM
    assert len(fake_adapters[1]) == 1 and len(fake_adapters[1][0].prompts) == 1


def test_probe_claude_oral_refusal_is_not_tool_attempt_evidence(
    plan_path, fake_adapters
):
    report = asyncio.run(probe.execute_plan(plan_path, case="claude-write"))
    assert report["status"] in {"incomplete", "review_required"}
    assert report["requested"] == {"model": "claude-sonnet-5-5", "effort": "medium"}
    assert report["cleanup"]["process_stopped"] is True
    assert report["cleanup"]["workspace_removed"] is True
    assert len(fake_adapters[1][0].prompts) == 1


def test_probe_claude_exact_tool_failures_still_require_permission_review(
    plan_path, fake_adapters
):
    fake_adapters[0]["mode"] = "claude-tool-errors"
    report = asyncio.run(probe.execute_plan(plan_path, case="claude-write"))
    assert report["status"] == "review_required"
    assert report["cleanup"]["workspace_removed"] is True
    assert "synthetic generic tool failure" in json.dumps(report["observations"])


@pytest.mark.parametrize("mode", ["unknown-stop", "stream-failure", "corrupt-control"])
def test_probe_preserves_partial_failure_and_owned_cleanup(
    plan_path, fake_adapters, mode
):
    fake_adapters[0]["mode"] = mode
    report = asyncio.run(probe.execute_plan(plan_path, case="codex-write"))
    adapter = fake_adapters[1][0]
    assert report["status"] != "passed"
    assert report["cleanup"]["workspace_removed"] is False
    assert report["workspace"]["root"] == adapter.workspace.root
    assert Path(adapter.workspace.root).is_dir()
    assert Path(adapter.workspace.git_directory).is_dir()
    assert (Path(adapter.workspace.root) / "model-created.txt").is_file()
    assert _load(plan_path.parent / "codex-write/report.json") == report
    if mode == "corrupt-control":
        assert report["cleanup"]["process_stopped"] is True
        assert report["cleanup"]["error"]
    else:
        assert report["cleanup"]["process_stopped"] is not True
        assert report["process_exit"] is None
    assert report["events"], "already observed runtime evidence survives failure"


def test_probe_cleanup_failure_retains_workspace_and_locator(
    plan_path, fake_adapters, monkeypatch
):
    def fail_remove(lease, workspace):
        assert fake_adapters[1][0].stopped
        raise ValueError("synthetic ownership check failed")

    monkeypatch.setattr(probe, "remove_workspace", fail_remove)
    report = asyncio.run(probe.execute_plan(plan_path, case="codex-write"))
    assert report["cleanup"]["workspace_removed"] is False
    assert "synthetic ownership check failed" in report["cleanup"]["error"]
    assert Path(report["workspace"]["root"]).is_dir()
    assert report["status"] != "passed"


def test_probe_cancelled_runtime_preserves_terminal_and_real_exit(
    plan_path, fake_adapters
):
    fake_adapters[0]["mode"] = "cancelled"
    report = asyncio.run(probe.execute_plan(plan_path, case="codex-write"))
    assert report["status"] == "cancelled"
    assert report["terminal"]["failure_category"] == "cancelled"
    assert report["process_exit"] == -signal.SIGTERM
    assert report["cleanup"]["process_stopped"] is True
    assert report["cleanup"]["workspace_removed"] is True


def test_probe_caller_cancellation_waits_for_evidence_and_collects_consumer(
    plan_path, monkeypatch
):
    async def run():
        consumer_started = asyncio.Event()
        release = asyncio.Event()
        adapters = []

        class CancellableAdapter(FakeAdapter):
            async def events(self, turn):
                self.consumer_task = asyncio.current_task()
                self.consumer_closed = False
                try:
                    started = SessionStarted("synthetic-runtime-session")
                    self.observed.append(started)
                    yield started
                    consumer_started.set()
                    await release.wait()
                    terminal = Error(
                        "synthetic caller cancellation", FailureCategory.CANCELLED
                    )
                    self.observed.append(terminal)
                    yield terminal
                    exited = ProcessExited(-signal.SIGTERM)
                    self.observed.append(exited)
                    self.stopped = True
                    yield exited
                finally:
                    self.consumer_closed = True

            async def cancel(self, turn, *, on_accepted=None):
                accepted = await super().cancel(turn, on_accepted=on_accepted)
                release.set()
                return accepted

        def factory(provider, *, workspace, log_dir):
            adapter = CancellableAdapter(provider, workspace, log_dir)
            adapters.append(adapter)
            return adapter

        monkeypatch.setattr(probe, "_make_adapter", factory)
        task = asyncio.create_task(probe.execute_plan(plan_path, case="codex-write"))
        await consumer_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        (adapter,) = adapters
        assert len(adapter.cancellations) == 1
        assert adapter.consumer_closed
        assert adapter.consumer_task.done()
        assert adapter.consumer_task not in asyncio.all_tasks()
        report = _load(plan_path.parent / "codex-write/report.json")
        assert report["status"] == "cancelled"
        assert report["error"] == "probe caller cancelled"
        assert report["terminal"]["failure_category"] == "cancelled"
        assert report["process_exit"] == -signal.SIGTERM
        assert report["cleanup"]["process_stopped"] is True
        assert report["cleanup"]["workspace_removed"] is True
        assert len(report["events"]) == 3
        assert not Path(adapter.workspace.root).exists()
        assert not Path(adapter.workspace.git_directory).exists()

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["cancel-error", "reader-timeout"])
def test_probe_caller_cancellation_preserves_unknown_stop_and_collects_consumer(
    plan_path, monkeypatch, mode
):
    real_timeout = asyncio.timeout

    def short_cancel_timeout(delay):
        return real_timeout(0.001 if delay == 10 else delay)

    def forbidden_cleanup(*args, **kwargs):
        pytest.fail("unknown process stop must retain the owned workspace")

    monkeypatch.setattr(probe.asyncio, "timeout", short_cancel_timeout)
    monkeypatch.setattr(probe, "remove_workspace", forbidden_cleanup)

    async def run():
        consumer_started = asyncio.Event()
        never_released = asyncio.Event()
        adapters = []

        class UnconfirmedAdapter(FakeAdapter):
            async def events(self, turn):
                self.consumer_task = asyncio.current_task()
                self.consumer_closed = False
                try:
                    started = SessionStarted("synthetic-runtime-session")
                    self.observed.append(started)
                    yield started
                    consumer_started.set()
                    await never_released.wait()
                finally:
                    self.consumer_closed = True

            async def cancel(self, turn, *, on_accepted=None):
                accepted = await super().cancel(turn, on_accepted=on_accepted)
                if mode == "cancel-error":
                    raise OSError("synthetic cancel transport failed")
                return accepted

        def factory(provider, *, workspace, log_dir):
            adapter = UnconfirmedAdapter(provider, workspace, log_dir)
            adapters.append(adapter)
            return adapter

        monkeypatch.setattr(probe, "_make_adapter", factory)
        task = asyncio.create_task(probe.execute_plan(plan_path, case="codex-write"))
        await consumer_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        (adapter,) = adapters
        assert len(adapter.cancellations) == 1
        assert adapter.consumer_closed
        assert adapter.consumer_task.done()
        assert adapter.consumer_task not in asyncio.all_tasks()
        report = _load(plan_path.parent / "codex-write/report.json")
        assert report["status"] == "cancelled"
        assert report["error"] == "probe caller cancelled"
        assert report["terminal"] is None
        assert report["process_exit"] is None
        assert report["cleanup"]["process_stopped"] is False
        assert report["cleanup"]["workspace_removed"] is False
        assert report["cleanup"]["error"].startswith("cancel cleanup unconfirmed:")
        assert len(report["events"]) == 1
        assert report["workspace"]["owner_token"] == adapter.workspace.owner_token
        assert Path(adapter.workspace.root).is_dir()
        assert Path(adapter.workspace.git_directory).is_dir()

    asyncio.run(run())


def test_probe_cases_are_independent_and_existing_results_are_never_overwritten(
    plan_path, fake_adapters
):
    first = asyncio.run(probe.execute_plan(plan_path, case="codex-write"))
    first_path = plan_path.parent / "codex-write/report.json"
    first_bytes = first_path.read_bytes()
    second = asyncio.run(probe.execute_plan(plan_path, case="claude-write"))
    assert first["workspace"]["root"] != second["workspace"]["root"]
    assert first["workspace"]["source"] != second["workspace"]["source"]
    assert first_path.read_bytes() == first_bytes
    assert len(fake_adapters[1]) == 2
    with pytest.raises(ValueError):
        asyncio.run(probe.execute_plan(plan_path, case="codex-write"))
    assert len(fake_adapters[1]) == 2
    assert first_path.read_bytes() == first_bytes


def test_probe_failed_case_is_also_nonrepeatable(plan_path, fake_adapters):
    fake_adapters[0]["mode"] = "unknown-stop"
    asyncio.run(probe.execute_plan(plan_path, case="codex-write"))
    first_report = (plan_path.parent / "codex-write/report.json").read_bytes()
    with pytest.raises(ValueError):
        asyncio.run(probe.execute_plan(plan_path, case="codex-write"))
    assert len(fake_adapters[1]) == 1
    assert (plan_path.parent / "codex-write/report.json").read_bytes() == first_report


def test_probe_request_overrides_are_recorded_not_promoted_to_actual(
    tmp_path, monkeypatch, fake_adapters
):
    monkeypatch.setattr(probe, "_source_hashes", lambda: dict(SOURCE_HASHES))
    root = tmp_path / "custom-plan"
    plan = probe.prepare_plan(
        root,
        codex_model="custom-codex",
        codex_effort="high",
        claude_model="custom-claude",
        claude_effort="low",
    )
    report = asyncio.run(probe.execute_plan(root / "plan.json", case="codex-write"))
    assert report["status"] == "passed"
    assert plan["requests"] == {
        "codex": {"model": "custom-codex", "effort": "high"},
        "claude": {"model": "custom-claude", "effort": "low"},
    }
    assert report["requested"] == plan["requests"]["codex"]
    assert report["terminal"]["reported_model"] == "runtime-reported-model"
    assert fake_adapters[1][0].config.model == "custom-codex"


def test_probe_cli_default_does_not_execute_a_case(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "_source_hashes", lambda: dict(SOURCE_HASHES))

    async def forbidden(*args, **kwargs):
        pytest.fail("default CLI called execute_plan")

    monkeypatch.setattr(probe, "execute_plan", forbidden)
    root = tmp_path / "cli-preparation"
    assert probe.main(["--output", str(root)]) == 0
    assert (root / "plan.json").is_file()
    assert not (root / "codex-write").exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ["--execute-plan", "unused.json"],
        ["--case", "codex-write"],
        ["--execute-plan", "unused.json", "--case", "all"],
    ],
)
def test_probe_cli_requires_one_explicit_known_execution_case(arguments, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("invalid CLI arguments reached execute_plan")

    monkeypatch.setattr(probe, "execute_plan", forbidden)
    try:
        code = probe.main(arguments)
    except SystemExit as error:
        code = error.code
    assert code != 0


@pytest.fixture
def native_transport(tmp_path, monkeypatch):
    settings = {"mode": "normal"}
    instances = []
    requests = []
    auth_home = tmp_path / "synthetic-native-auth"
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

    class FakeProcess:
        def __init__(self, isolation):
            self.isolation = isolation
            self.child = SimpleNamespace(returncode=None)

        async def terminate(self, grace):
            if settings["mode"] == "unknown-stop":
                raise OSError("synthetic native stop not confirmed")
            self.child.returncode = -signal.SIGTERM

    class NativeAdapter(ImplementationRuntimeAdapter):
        def _create_isolation(self, state, recorder):
            self.isolation = super()._create_isolation(state, recorder)
            self.policy = copy.deepcopy(self.isolation.config)
            return self.isolation

        async def _collect(self, argv, cwd, recorder, *, isolation=None):
            assert argv == [self.executable, "--version"]
            assert isolation is self.isolation
            return "codex-cli 0.169.0\n", 0

        async def _spawn(self, argv, cwd, recorder, *, pipe=False, isolation=None):
            assert argv == [self.executable, "app-server", "--strict-config"]
            assert pipe and isolation is self.isolation
            process = FakeProcess(isolation)
            isolation._processes[process] = False
            self.process = process
            return process

        async def _audit_codex(self, rpc, isolation):
            self.audit_called = True
            await super()._audit_codex(rpc, isolation)
            self.audit_finished = True

    class FakeRpc:
        def __init__(self, process):
            self.isolation = process.isolation

        async def initialize(self):
            requests.append(("initialize", {}))

        async def request(self, method, params):
            requests.append((method, copy.deepcopy(params)))
            if method == "config/read":
                expected = self.isolation.config
                effective = {
                    **dict.fromkeys(_CODEX_NULL_TOP_LEVEL),
                    "background_terminal_max_timeout": 300000,
                    "file_opener": "vscode",
                    "include_permissions_instructions": True,
                    "chatgpt_base_url": "https://chatgpt.com/backend-api/",
                    "project_root_markers": [".git"],
                    "mcp_oauth_credentials_store": "auto",
                    "hide_agent_reasoning": False,
                    "profiles": {},
                    "model_providers": {},
                    "mcp_servers": {},
                    "marketplaces": {},
                    **copy.deepcopy(expected),
                }
                if settings["mode"] == "audit-drift":
                    effective["permissions"]["multi-agent-owned-implementation"][
                        "filesystem"
                    ][str(self.isolation.cwd)] = "read"
                return {
                    "config": effective,
                    "origins": {},
                    "layers": [
                        {
                            "name": {
                                "type": "user",
                                "file": str(self.isolation.codex_home / "config.toml"),
                            },
                            "version": "synthetic",
                            "config": copy.deepcopy(expected),
                        }
                    ],
                }
            if method == "configRequirements/read":
                return {"requirements": None}
            if method == "account/read":
                return {
                    "account": {
                        "type": "apiKey"
                        if settings["mode"] == "account-api"
                        else "chatgpt"
                    }
                }
            assert method == "command/exec", (
                "native probe must never create a thread or turn"
            )
            assert not {"sandboxPolicy", "sandbox", "permissionProfile"} & params.keys()
            assert params["cwd"] == str(self.isolation.cwd)
            command = params["command"]
            assert isinstance(command, list) and all(
                isinstance(part, str) for part in command
            )
            root = self.isolation.cwd
            if settings["mode"] == "malformed":
                return {"exitCode": True, "stdout": "", "stderr": ""}
            if command == ["/bin/cat", str(root / "baseline.txt")]:
                return {
                    "exitCode": 1 if settings["mode"] == "positive-failure" else 0,
                    "stdout": "probe baseline\n",
                    "stderr": "",
                }
            if command[0] == "/bin/cp" and command[-1] in (
                str(root / "created.txt"),
                str(root / "edit.txt"),
            ):
                if settings["mode"] == "transport-failure" and command[-1] == str(
                    root / "edit.txt"
                ):
                    raise OSError("synthetic native transport failure")
                Path(command[-1]).write_bytes(Path(command[1]).read_bytes())
                return {"exitCode": 0, "stdout": "", "stderr": ""}
            if command == ["/bin/rm", str(root / "delete.txt")]:
                (root / "delete.txt").unlink()
                return {"exitCode": 0, "stdout": "", "stderr": ""}
            if settings["mode"] == "negative-success":
                return {
                    "exitCode": 0,
                    "stdout": "unexpected external read",
                    "stderr": "",
                }
            return {
                "exitCode": 1,
                "stdout": "",
                "stderr": "synthetic generic command failure",
            }

    def factory(provider, *, workspace, log_dir):
        assert provider == "codex"
        adapter = NativeAdapter(
            provider,
            workspace=workspace,
            log_dir=log_dir,
            executable="never-launched-native-cli",
        )
        adapter.audit_called = False
        adapter.audit_finished = False
        instances.append(adapter)
        return adapter

    monkeypatch.setattr(probe, "_make_adapter", factory)
    monkeypatch.setattr(probe, "_Rpc", FakeRpc)
    try:
        yield settings, instances, requests
    finally:
        # The unknown-stop case deliberately retains a real private directory,
        # but its only credential content was generated by this test fixture.
        settings["mode"] = "normal"
        for index, adapter in enumerate(instances):
            if hasattr(adapter, "isolation") and adapter.isolation.root.exists():
                with Recorder(
                    tmp_path / f"native-fixture-cleanup-{index}.jsonl", Redactor()
                ) as recorder:
                    adapter.isolation.recorder = recorder
                    if hasattr(adapter, "process"):
                        asyncio.run(adapter.isolation.terminate(adapter.process, 0.05))
                    adapter.isolation.cleanup()


def test_native_probe_uses_product_policy_and_argv(plan_path, native_transport):
    report = asyncio.run(probe.execute_plan(plan_path, case="codex-native"))
    _, instances, requests = native_transport
    adapter = instances[0]
    record = adapter.workspace
    assert adapter.audit_called and adapter.audit_finished
    assert adapter.policy["default_permissions"] == "multi-agent-owned-implementation"
    assert adapter.policy["permissions"] == {
        "multi-agent-owned-implementation": {
            "filesystem": {
                ":minimal": "read",
                record.root: "write",
                str(Path(record.root) / ".git"): "deny",
                record.repository: "deny",
                record.git_directory: "deny",
            },
            "network": {"enabled": False},
        }
    }
    methods = [method for method, _ in requests]
    assert methods[:4] == [
        "initialize",
        "config/read",
        "configRequirements/read",
        "account/read",
    ]
    assert set(methods[4:]) == {"command/exec"}
    commands = [
        params["command"] for method, params in requests if method == "command/exec"
    ]
    assert len(commands) == 13
    assert commands[0] == ["/bin/cat", str(Path(record.root) / "baseline.txt")]
    assert commands[1] == [
        "/bin/cp",
        str(Path(record.root) / "replacement.txt"),
        str(Path(record.root) / "created.txt"),
    ]
    assert [command[:2] for command in commands[-2:]] == [
        ["/usr/bin/wc", "-c"],
        ["/usr/bin/wc", "-c"],
    ]
    assert report["status"] == "review_required"
    assert [entry["status"] for entry in report["observations"]] == ["allowed"] * 4 + [
        "attempted_failed"
    ] * 9
    assert report["cleanup"] == {
        "process_stopped": True,
        "workspace_removed": True,
        "error": None,
    }
    assert not adapter.isolation.root.exists()
    assert not Path(record.root).exists()
    assert report["requested"] is None
    assert report["process_exit"] == -signal.SIGTERM


def test_native_probe_report_and_logs_never_retain_synthetic_auth_values(
    plan_path, native_transport, monkeypatch
):
    secrets = tuple(
        "synthetic-" + name for name in ("id_token", "access_token", "refresh_token")
    )
    original_rpc = probe._Rpc

    class DiagnosticRpc(original_rpc):
        async def request(self, method, params):
            response = await super().request(method, params)
            if method == "command/exec":
                # Deliberately exercise both product Recorder and report redaction;
                # merely using a fixture that never echoes credentials is weaker.
                diagnostic = "synthetic runtime diagnostic: " + " ".join(secrets)
                self.isolation.recorder.emit(
                    "recv", proc="codex", stream="stderr", text=diagnostic
                )
                response = {**response, "stderr": diagnostic}
            return response

    monkeypatch.setattr(probe, "_Rpc", DiagnosticRpc)
    report = asyncio.run(probe.execute_plan(plan_path, case="codex-native"))
    assert report["status"] == "review_required"
    case_root = plan_path.parent / "codex-native"
    raw_files = tuple((case_root / "logs").glob("*.jsonl"))
    assert raw_files
    artifacts = [json.dumps(report), (case_root / "report.json").read_text()]
    artifacts.extend(path.read_text() for path in raw_files)
    for artifact in artifacts:
        assert "<REDACTED>" in artifact
        assert all(secret not in artifact for secret in secrets)
    assert report["cleanup"]["workspace_removed"] is True
    assert not native_transport[1][0].isolation.root.exists()


@pytest.mark.parametrize("mode", ["audit-drift", "account-api"])
def test_native_probe_rejects_policy_and_account_before_any_command(
    plan_path, native_transport, mode
):
    native_transport[0]["mode"] = mode
    report = asyncio.run(probe.execute_plan(plan_path, case="codex-native"))
    assert report["status"] != "passed"
    assert not any(method == "command/exec" for method, _ in native_transport[2])
    assert report["cleanup"]["workspace_removed"] is True
    assert native_transport[1][0].audit_called
    assert native_transport[1][0].audit_finished is (mode == "account-api")


@pytest.mark.parametrize(
    "mode,count,finished",
    [
        ("positive-failure", 1, 0),
        ("negative-success", 5, 4),
        ("malformed", 1, 0),
        ("transport-failure", 3, 2),
    ],
)
def test_native_probe_stops_after_first_failure_and_preserves_unrun_cases(
    plan_path, native_transport, mode, count, finished
):
    native_transport[0]["mode"] = mode
    report = asyncio.run(probe.execute_plan(plan_path, case="codex-native"))
    commands = [
        params for method, params in native_transport[2] if method == "command/exec"
    ]
    assert len(commands) == count
    assert report["status"] not in {"passed", "review_required"}
    assert len(report["observations"]) == 13
    assert [entry["status"] for entry in report["observations"][:finished]] == [
        "allowed"
    ] * finished
    assert all(
        entry["status"] == "incomplete" for entry in report["observations"][count:]
    )
    assert _load(plan_path.parent / "codex-native/report.json") == report


def test_native_probe_unknown_stop_retains_workspace_and_private_owner(
    plan_path, native_transport
):
    native_transport[0]["mode"] = "unknown-stop"
    report = asyncio.run(probe.execute_plan(plan_path, case="codex-native"))
    adapter = native_transport[1][0]
    assert report["cleanup"]["workspace_removed"] is False
    assert report["cleanup"]["process_stopped"] is not True
    assert report["process_exit"] is None
    assert Path(adapter.workspace.root).is_dir()
    assert adapter.isolation.root.is_dir()
    assert report["error"] or report["cleanup"]["error"]
