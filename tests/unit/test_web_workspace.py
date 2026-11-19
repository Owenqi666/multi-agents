"""Exercise the workspace through loopback HTTP and synthetic runtime processes."""

import json
import subprocess
import threading
import time
from contextlib import contextmanager
from http.client import HTTPConnection
from pathlib import Path

import pytest
import test_runtime as runtime_helpers

from multi_agent.adapters.base import EnvironmentInfo
from multi_agent.adapters.probe.process import ProcessCleanupIncomplete
from multi_agent.adapters.runtime import RuntimeAdapter
from multi_agent.web.server import WorkspaceServer
from multi_agent.web.workspace import Workspace

synthetic_codex_auth_and_fake_environment = (
    runtime_helpers.synthetic_codex_auth_and_fake_environment
)

_RULES = """
version: 1
commands: {}
protected_paths: [".multi-agent/**"]
implementation:
  - paths: ["frontend/**"]
    provider: claude-code
    default_model: claude-sonnet-5-5
    default_effort: high
default_implementation:
  provider: codex
  default_model: gpt-6.2-sol
  default_effort: medium
task_types:
  documentation:
    provider: claude-code
    allowed_models: [claude-sonnet-5-5]
"""


def _git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _commit_all(repo):
    _git(repo, "add", ".")
    _git(
        repo,
        "-c",
        "user.name=Workspace Test",
        "-c",
        "user.email=test@example.org",
        "commit",
        "-qm",
        "workspace fixture",
    )
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / "repository"
    _git(tmp_path, "init", "-qb", "main", str(repo))
    (repo / "README.md").write_text("Workspace test fixture.\n")
    _commit_all(repo)
    return repo


@pytest.fixture
def executables(tmp_path, monkeypatch):
    capture = tmp_path / "runtime.jsonl"
    monkeypatch.setenv("FAKE_RUNTIME_CAPTURE", str(capture))
    selected = (
        "gpt-6.2-astra",
        "gpt-6.2-sol",
        "gpt-6.1-terra",
        "gpt-6.2-luna",
        "claude-haiku-5-5",
    )
    binaries = {}
    for provider in ("codex", "claude"):
        executable = Path(runtime_helpers.fake(tmp_path, provider))
        source = executable.read_text()
        source = source.replace(
            '{"kind": kind, **values}', '{"kind": kind, "provider": provider, **values}'
        )
        if provider == "codex":
            start = source.index('        name = "model-2"')
            finish = source.index('    elif method == "thread/start":', start)
            source = (
                source[:start]
                + f"""
        result = {{
            "data": [
                {{
                    "id": name,
                    "hidden": False,
                    "supportedReasoningEfforts": [
                        {{"reasoningEffort": level}}
                        for level in (("high",) if name == "gpt-6.1-terra" else ("low", "medium", "high"))
                    ],
                }}
                for name in {selected!r}
            ],
            "nextCursor": None,
        }}
"""
                + source[finish:]
            )
        source = source.replace(
            'if args == ["--version"]:',
            """if args == ["--version"]:
    if provider == "codex" and (gate := os.environ.get("FAKE_WEB_CHECK_GATE")):
        capture("web-check-waiting")
        while not Path(gate).exists():
            time.sleep(0.01)""",
            1,
        )
        executable.write_text(source)
        binaries[provider] = str(executable)
    return binaries, capture


@contextmanager
def _serve(repository, data_dir, executables):
    workspace = Workspace.open(repository, data_dir)
    with WorkspaceServer(0, workspace, executables) as service:
        worker = threading.Thread(
            target=service.serve_forever, kwargs={"poll_interval": 0.01}
        )
        worker.start()
        try:
            yield service
        finally:
            service.shutdown()
            worker.join()


def _request(service, method, path, body=None, **headers):
    connection = HTTPConnection("127.0.0.1", service.server_port, timeout=3)
    try:
        if body is not None:
            headers.setdefault("Content-Type", "application/json")
            body = json.dumps(body).encode()
        connection.request(method, path, body, headers)
        response = connection.getresponse()
        assert response.getheader("Content-Type") == "application/json; charset=utf-8"
        return response.status, json.load(response)
    finally:
        connection.close()


def _workspace(service):
    status, response = _request(service, "GET", "/api/workspace")
    assert status == 200, response
    return response


def _refresh(service, provider, **headers):
    return _request(
        service,
        "POST",
        "/api/jobs",
        {"action": "environment.refresh", "args": {"provider": provider}},
        **headers,
    )


def _records(capture):
    return (
        [json.loads(line) for line in capture.read_text().splitlines()]
        if capture.exists()
        else []
    )


def _wait_for(check):
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.02)
    pytest.fail("condition did not complete")


def _finished(service, job_id):
    status, outcome = _request(service, "GET", f"/api/jobs/{job_id}")
    assert status == 200
    return outcome if outcome["state"] == "finished" else None


def _candidate_map(view, role):
    selected_role = next(
        item for item in view["presets"]["roles"] if item["id"] == role
    )
    return {candidate["family"]: candidate for candidate in selected_role["candidates"]}


def test_web_workspace_git_and_rules(tmp_path, repository, executables):
    binaries, capture = executables
    data_dir = tmp_path / "data"
    with _serve(repository, data_dir, binaries) as service:
        assert service.server_address[0] == "127.0.0.1"
        view = _workspace(service)
        assert set(view) == {
            "repository",
            "target_branch",
            "head",
            "read_only",
            "presets",
            "environments",
        }
        assert view["repository"] == str(repository)
        assert view["target_branch"] == "main"
        assert view["head"] == _git(repository, "rev-parse", "HEAD")
        assert view["read_only"] is False
        assert view["environments"] == {"claude": None, "codex": None}
        assert view["presets"]["rules"]["status"] == "unavailable"
        assert view["presets"]["rules"]["protected_paths"] == []
        assert _workspace(service) == view
        assert data_dir.stat().st_mode & 0o777 == 0o700
        assert _request(service, "GET", "/")[0] == 404

        rules_path = repository / ".multi-agent" / "rules.yaml"
        rules_path.parent.mkdir()
        rules_path.write_text(_RULES)
        baseline = _commit_all(repository)
        rules_path.write_text(_RULES.replace("frontend/**", "dirty/**"))
        rules = _workspace(service)["presets"]["rules"]
        assert rules["baseline_commit"] == baseline
        assert rules["source"] == ".multi-agent/rules.yaml"
        assert rules["status"] == "available"
        assert len(rules["rules_hash"]) == 64
        assert rules["applies_to"] == ["implementation", "fallback"]
        assert rules["protected_paths"] == [".multi-agent/**"]
        rule = rules["implementation"][0]
        assert rule["paths"] == ["frontend/**"]
        assert rule["provider"] == "claude"
        assert rule["implementation_provider"] == "claude-code"
        assert rule["effective"] == {
            "paths": ["frontend/**"],
            "provider": "claude",
            "allowed_models": ["claude-sonnet-5-5"],
            "matched_paths": ["frontend/**"],
            "error": None,
        }
        assert rules["default_implementation"]["default_model"] == "gpt-6.2-sol"
        assert rules["task_types"][0]["name"] == "documentation"

        _git(repository, "checkout", "--detach", "-q")
        assert _workspace(service)["target_branch"] is None
        rules_path.write_text("rules are invalid")
        bad_baseline = _commit_all(repository)
        failed = _workspace(service)["presets"]["rules"]
        assert failed["baseline_commit"] == bad_baseline
        assert failed["status"] == "error"
        assert failed["error"]
        assert failed["implementation"] == []
        assert failed["protected_paths"] == []
        assert failed["default_implementation"] is None

        _git(repository, "switch", "--orphan", "unborn")
        unborn = _workspace(service)
        assert unborn["target_branch"] == "unborn" and unborn["head"] is None
        assert unborn["presets"]["rules"]["status"] == "unavailable"
    assert not capture.exists()


def test_web_workspace_environment_jobs_and_options(
    tmp_path, repository, executables, monkeypatch
):
    binaries, capture = executables
    rules_path = repository / ".multi-agent" / "rules.yaml"
    rules_path.parent.mkdir()
    rules_path.write_text(_RULES)
    _commit_all(repository)
    data_dir = tmp_path / "data"
    gate = tmp_path / "release"
    monkeypatch.setenv("FAKE_WEB_CHECK_GATE", str(gate))
    with _serve(repository, data_dir, binaries) as service:
        before = _workspace(service)
        assert not capture.exists()
        assert set(_candidate_map(before, "review")) == {"Fable", "Opus", "Sonnet"}
        assert _candidate_map(before, "stage_lead") == {}
        status, accepted = _refresh(service, "codex", Origin=service.origin)
        assert status == 202
        assert accepted["state"] == "queued"
        job_id = accepted["job_id"]
        try:
            _wait_for(
                lambda: any(
                    event["kind"] == "web-check-waiting" for event in _records(capture)
                )
            )
            assert _workspace(service)["environments"] == before["environments"]
            _, running = _request(service, "GET", f"/api/jobs/{job_id}")
            assert running["state"] == "running" and running["result"] is None
            assert _refresh(service, "codex")[0] == 409
            with pytest.raises(ValueError, match="already has a service owner"):
                WorkspaceServer(0, service.workspace, binaries)
            assert _request(service, "GET", f"/api/jobs/{job_id}")[1] == running
            status, claude = _refresh(service, "claude")
            assert status == 202
            claude_job = _wait_for(lambda: _finished(service, claude["job_id"]))
            observed_claude = claude_job["result"]
            assert observed_claude["cli_version"] == "fake-claude 1.0"
            assert observed_claude["logged_in"] is True
            assert (
                observed_claude["models"] is None
                and observed_claude["model_efforts"] is None
            )
            assert _workspace(service)["environments"] == {
                "claude": observed_claude,
                "codex": None,
            }
        finally:
            gate.touch()

        codex_job = _wait_for(lambda: _finished(service, job_id))
        observed_codex = codex_job["result"]
        assert observed_codex["source"] == "runtime_environment_check"
        assert observed_codex["observed_at"]
        assert observed_codex["cli_version"] == "codex-cli 0.169.0"
        assert observed_codex["logged_in"] is True
        assert codex_job["exit_code"] is None and codex_job["error"] is None
        assert {"gpt-6.2-luna", "claude-haiku-5-5"} <= set(observed_codex["models"])

        view = _workspace(service)
        assert view["environments"] == {
            "codex": observed_codex,
            "claude": observed_claude,
        }
        review = _candidate_map(view, "review")
        assert set(review) == {"Astra", "Sol", "Terra", "Fable", "Opus", "Sonnet"}
        assert set(_candidate_map(view, "advisor")) == {"Astra", "Fable"}
        assert set(_candidate_map(view, "planner")) == {"Astra", "Fable"}
        assert set(_candidate_map(view, "stage_lead")) == {"Sol"}
        assert set(_candidate_map(view, "fallback")) == {"Sol", "Terra", "Sonnet"}
        assert (
            review["Sol"]["source"]
            == review["Sol"]["effort_source"]
            == "runtime_catalog"
        )
        assert review["Sol"]["efforts"] == ["low", "medium", "high"]
        assert review["Terra"]["efforts"] == ["high"]
        assert (
            review["Sonnet"]["source"]
            == review["Sonnet"]["effort_source"]
            == "workbench_preset"
        )
        assert "low" in review["Sonnet"]["efforts"]
        assert view["presets"]["rules"]["implementation"][0]["provider"] == "claude"
        assert review["Astra"]["provider"] == "codex"
        assert "reported_model" not in observed_codex

        monkeypatch.setenv("FAKE_VERSION_EXIT", "3")
        status, retry = _refresh(service, "claude")
        assert status == 202
        failed = _wait_for(lambda: _finished(service, retry["job_id"]))
        assert failed["error"]["code"] == "environment_check_failed"
        assert failed["result"]["provider"] == "claude"
        assert all(
            failed["result"][key] is None
            for key in ("cli_version", "logged_in", "models", "model_efforts")
        )
        assert _workspace(service)["environments"] == view["environments"]

    calls = _records(capture)
    assert [
        (event["provider"], event["argv"])
        for event in calls
        if event["kind"] == "spawn"
    ] == [
        ("codex", ["--version"]),
        ("claude", ["--version"]),
        ("claude", ["auth", "status"]),
        ("codex", ["app-server", "--strict-config"]),
        ("claude", ["--version"]),
    ]
    methods = {
        event["message"].get("method") for event in calls if event["kind"] == "request"
    }
    assert "model/list" in methods
    assert methods.isdisjoint({"thread/start", "turn/start"})


def test_web_workspace_trust_boundary(tmp_path, repository, executables):
    binaries, capture = executables
    with _serve(repository, tmp_path / "data", binaries) as service:
        assert _request(service, "GET", "/api/workspace", Host="host.example")[0] == 403
        assert _refresh(service, "codex", Origin="http://untrusted.example")[0] == 403
        cases = [
            ({"action": "unknown", "args": {"provider": "codex"}}, "unknown_action"),
            (
                {"action": "environment.refresh", "args": {"provider": "unknown"}},
                "invalid_provider",
            ),
        ]
        cases += [
            (
                {
                    "action": "environment.refresh",
                    "args": {"provider": "codex", key: "override"},
                },
                "invalid_request",
            )
            for key in ("repository", "database", "executable", "directory", "argv")
        ]
        for body, expected in cases:
            status, response = _request(service, "POST", "/api/jobs", body)
            assert status == 400 and response["error"]["code"] == expected
        assert _request(service, "GET", "/api/jobs/nonexistent")[0] == 404
        assert not capture.exists()

        status, accepted = _refresh(service, "claude", Origin=service.origin)
        assert status == 202
        assert (
            _wait_for(lambda: _finished(service, accepted["job_id"]))["error"] is None
        )
        assert all(record["provider"] == "claude" for record in _records(capture))


def test_web_workspace_restart_recovery(tmp_path, repository, executables):
    binaries, capture = executables
    data_dir = tmp_path / "data"
    with _serve(repository, data_dir, binaries) as service:
        status, accepted = _refresh(service, "claude")
        assert status == 202
        job_id = accepted["job_id"]
        finished = _wait_for(lambda: _finished(service, job_id))

    saved_path = data_dir / "web-environment" / "state.json"
    saved = json.loads(saved_path.read_text())
    shadow = "a" * 32
    saved["jobs"][shadow] = {
        **finished,
        "job_id": shadow,
        "state": "running",
        "result": None,
    }
    saved_path.write_text(json.dumps(saved))
    calls = capture.read_bytes()
    with _serve(repository, data_dir, binaries) as service:
        assert _request(service, "GET", f"/api/jobs/{job_id}") == (200, finished)
        status, interrupted = _request(service, "GET", f"/api/jobs/{shadow}")
        assert status == 200 and interrupted["state"] == "interrupted"
        assert interrupted["error"]["code"] == "service_interrupted"
        assert interrupted["result"] is None and interrupted["exit_code"] is None
        assert _workspace(service)["environments"]["claude"] == finished["result"]
        assert _request(service, "GET", f"/api/jobs/{shadow}") == (200, interrupted)
        assert capture.read_bytes() == calls
    assert json.loads(saved_path.read_text())["jobs"][shadow] == interrupted

    different = tmp_path / "different"
    _git(tmp_path, "init", "-qb", "main", str(different))
    with pytest.raises(ValueError, match="different repository"):
        WorkspaceServer(0, Workspace.open(different, data_dir), binaries)
    assert json.loads(saved_path.read_text())["repository"] == str(repository)


def test_web_workspace_rejects_nonprivate_and_aliased_state_paths(
    tmp_path, repository, executables
):
    binaries, capture = executables
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o755)
    with pytest.raises(ValueError, match="private directories owned"):
        Workspace.open(repository, shared)

    data_dir = tmp_path / "data"
    workspace = Workspace.open(repository, data_dir)
    namespace = data_dir / "web-environment"
    namespace.mkdir(mode=0o700)
    (namespace / "logs").symlink_to(repository, target_is_directory=True)
    with pytest.raises(ValueError, match="private directories owned"):
        WorkspaceServer(0, workspace, binaries)
    assert not capture.exists()
    assert _git(repository, "status", "--short") == ""


@pytest.mark.parametrize(
    ("exception", "code"),
    [
        (ProcessCleanupIncomplete, "environment_cleanup_incomplete"),
        (RuntimeError, "environment_check_failed"),
    ],
)
def test_web_workspace_adapter_exception_settles_and_releases_provider(
    tmp_path, repository, executables, monkeypatch, exception, code
):
    binaries, capture = executables

    async def fail(_adapter):
        raise exception("private adapter context")

    with _serve(repository, tmp_path / "data", binaries) as service:
        with monkeypatch.context() as patch:
            patch.setattr(RuntimeAdapter, "check_environment", fail)
            status, accepted = _refresh(service, "claude")
            assert status == 202
            failed = _wait_for(lambda: _finished(service, accepted["job_id"]))
            assert failed["result"]["provider"] == "claude"
            assert failed["error"]["code"] == code
            assert "private adapter context" not in str(failed)
            assert _workspace(service)["environments"]["claude"] is None
            assert not capture.exists()
        status, accepted = _refresh(service, "claude")
        assert status == 202
        assert (
            _wait_for(lambda: _finished(service, accepted["job_id"]))["error"] is None
        )


def test_web_workspace_interrupted_close_retains_live_ownership(
    tmp_path, repository, executables, monkeypatch
):
    binaries, capture = executables
    data_dir = tmp_path / "data"
    entered = threading.Event()
    release = threading.Event()

    async def wait(_adapter):
        entered.set()
        assert release.wait(timeout=6)
        return EnvironmentInfo("fixture-version", True, None)

    def interrupt(*, wait):
        assert wait is True
        raise KeyboardInterrupt

    monkeypatch.setattr(RuntimeAdapter, "check_environment", wait)
    with _serve(repository, data_dir, binaries) as service:
        status, accepted = _refresh(service, "claude")
        assert status == 202
        try:
            assert entered.wait(timeout=4)
            with monkeypatch.context() as patch:
                patch.setattr(service.jobs.pool, "shutdown", interrupt)
                with pytest.raises(KeyboardInterrupt):
                    service.jobs.close()
            with pytest.raises(ValueError, match="already has a service owner"):
                WorkspaceServer(0, service.workspace, binaries)
            assert (
                _request(service, "GET", f"/api/jobs/{accepted['job_id']}")[1]["state"]
                == "running"
            )
        finally:
            release.set()
        finished = _wait_for(lambda: _finished(service, accepted["job_id"]))
    with _serve(repository, data_dir, binaries) as reopened:
        assert _request(reopened, "GET", f"/api/jobs/{accepted['job_id']}") == (
            200,
            finished,
        )
        assert _workspace(reopened)["environments"]["claude"] == finished["result"]
    assert not capture.exists()


@pytest.mark.parametrize("include_previous", [True, False])
def test_web_workspace_latest_visible_version_supplies_its_own_effort(
    tmp_path, repository, executables, monkeypatch, include_previous
):
    binaries, capture = executables
    preferred = {"codex": "gpt-6.10-sol", "claude": "claude-sonnet-5-10"}
    previous = {"codex": "gpt-6.9-sol", "claude": "claude-sonnet-5-9"}
    outsider = {"codex": "gpt-6.21-luna", "claude": "claude-haiku-5-11"}

    async def observe(adapter):
        provider = adapter.provider
        catalog = [preferred[provider], outsider[provider]]
        if include_previous:
            # A lower version returned later must not replace the newest.
            catalog.append(previous[provider])
        return EnvironmentInfo(
            "catalog-fixture",
            True,
            tuple(catalog),
            {preferred[provider]: ("low", "high"), previous[provider]: ("ultra",)},
        )

    monkeypatch.setattr(RuntimeAdapter, "check_environment", observe)
    with _serve(repository, tmp_path / "data", binaries) as service:
        for provider in preferred:
            status, job = _refresh(service, provider)
            assert status == 202
            assert (
                _wait_for(lambda job_id=job["job_id"]: _finished(service, job_id))[
                    "error"
                ]
                is None
            )
        view = _workspace(service)
        for role in ("implementation", "fallback", "review"):
            candidates = _candidate_map(view, role)
            assert set(candidates) == {"Sol", "Sonnet"}
            assert {entry["model"] for entry in candidates.values()} == set(
                preferred.values()
            )
            assert all(
                entry["efforts"] == ["low", "high"] for entry in candidates.values()
            )
            assert all(
                entry["effort_source"] == "runtime_catalog"
                for entry in candidates.values()
            )
        assert set(_candidate_map(view, "stage_lead")) == {"Sol"}
        assert _candidate_map(view, "advisor") == {}
        for provider, model in preferred.items():
            raw = view["environments"][provider]["models"]
            assert model in raw and outsider[provider] in raw
            assert (previous[provider] in raw) is include_previous
    assert not capture.exists()
