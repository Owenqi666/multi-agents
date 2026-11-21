"""Real module entry points and transports, using only synthetic CLI executables."""

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from multi_agent.core.events import thaw_json
from multi_agent.store import ReviewStore


@pytest.fixture(autouse=True)
def synthetic_codex_auth(tmp_path, monkeypatch):
    auth_home = tmp_path / "synthetic-codex-auth"
    auth_home.mkdir(mode=0o700)
    auth = auth_home / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    key: "synthetic-" + key
                    for key in (
                        "id_token",
                        "access_token",
                        "refresh_token",
                        "account_id",
                    )
                },
            }
        )
    )
    auth.chmod(0o600)
    monkeypatch.setenv("CODEX_HOME", str(auth_home))


def cli(data, *args):
    process = subprocess.run(
        [sys.executable, "-m", "multi_agent", "--data-dir", str(data), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert process.returncode == 0, process.stderr
    return json.loads(process.stdout)


def fake_executable(directory, provider):
    fake_body = (Path(__file__).parent / "runtime_fakes/fake_cli.py").read_text()
    executable = directory / f"{provider}-fake"
    # This test launches the real module in another Python process. Carry fake
    # controls in the generated fixture itself, outside the product allowlist.
    fake_environment = {
        key: value for key, value in os.environ.items() if key.startswith("FAKE_")
    }
    executable.write_text(
        f"#!{sys.executable}\nimport os\nos.environ.update({fake_environment!r})\n"
        + fake_body
    )
    executable.chmod(0o755)
    return str(executable)


def execution_evidence(data, round_id):
    with ReviewStore(data / "reviews.sqlite3") as store:
        return thaw_json(store.load_execution(round_id))


def assert_sealed_session_log(data, output, key, provider):
    round_id = output["round_id"]
    [receipt] = execution_evidence(data, round_id)[key]["1"]["session_logs"]
    path = Path(receipt["path"])
    assert path.parent == (data / "logs" / round_id / key / "1").resolve()
    assert path.name.startswith(provider + "-") and path.suffix == ".jsonl"
    assert receipt["sealed"] and receipt["closed"] and receipt["healthy"]
    raw = path.read_bytes()
    info = path.stat()
    assert receipt["identity"] == [info.st_dev, info.st_ino]
    assert receipt["byte_count"] == len(raw)
    assert receipt["sha256"] == hashlib.sha256(raw).hexdigest()
    records = [json.loads(line) for line in raw.splitlines()]
    assert receipt["records"] == len(records)
    assert records[0]["kind"] == "requested"
    assert any(record["kind"] == "exit" for record in records)
    assert "session_logs" not in output["execution"][key]["1"]


@pytest.mark.parametrize("dual", [False, True])
def test_frozen_git_input_through_real_cli_and_fake_transports(
    tmp_path, monkeypatch, dual
):
    repo = tmp_path / "repository"
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(
            [
                "git",
                "-c",
                f"core.hooksPath={os.devnull}",
                "-c",
                "commit.gpgSign=false",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-C",
                str(repo),
                *args,
            ],
            text=True,
        ).strip()

    git("init", "-q", "--template=")
    (repo / "source.py").write_text("value = 1\n")
    git("add", ".")
    git("commit", "-qm", "baseline")
    baseline = git("rev-parse", "HEAD")
    (repo / "source.py").write_text("value = 2\n")
    git("commit", "-qam", "candidate")
    candidate = git("rev-parse", "HEAD")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("Update the constant to two")
    evidence = tmp_path / "tests.json"
    evidence.write_text(
        json.dumps(
            {
                "required_commands": [["must-not-execute-test"]],
                "results": [
                    {
                        "commit": candidate,
                        "command": ["must-not-execute-test"],
                        "exit_code": 0,
                        "log": "synthetic user-provided evidence",
                    }
                ],
            }
        )
    )
    capture = tmp_path / "wire.jsonl"
    monkeypatch.setenv("FAKE_RUNTIME_MODE", "success")
    monkeypatch.setenv("FAKE_RUNTIME_CAPTURE", str(capture))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-remove-me")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "synthetic-remove-me")
    executables = {
        provider: fake_executable(tmp_path, provider)
        for provider in ("codex", "claude")
    }
    data = tmp_path / "data"

    frozen = cli(
        data,
        "freeze",
        "--repo",
        str(repo),
        "--baseline",
        baseline,
        "--candidate",
        candidate,
        "--requirements",
        str(requirements),
        "--acceptance",
        "value equals two",
        "--tests",
        str(evidence),
    )
    seats = ["--seat", "codex", "model-1", "high"]
    if dual:
        seats.extend(("--seat", "claude", "claude-requested", "high"))
    output = cli(
        data,
        "run",
        frozen["snapshot_id"],
        *seats,
        "--codex-bin",
        executables["codex"],
        "--claude-bin",
        executables["claude"],
        "--timeout",
        "5",
        "--grace",
        "0.1",
    )
    assert output["assessment"]["status"] == "ready"
    assert output["snapshot_id"] == frozen["snapshot_id"]
    restored = cli(data, "show", output["round_id"], "--history")
    assert restored["assessment"] == output["assessment"]
    assert len(restored["seats"]) == (2 if dual else 1)
    terminals = [
        item
        for item in restored["history"]
        if item["envelope"]["event"]["type"] == "completed"
    ]
    assert len(terminals) == (2 if dual else 1)
    assert all(item["disposition"] == "applied" for item in terminals)
    for seat in restored["seats"]:
        attempt = seat["attempts"][0]
        assert attempt["status"] == "succeeded"
        assert attempt["exit_code"] == 0
        assert attempt["reported_effort"] is None
        assert attempt["reported_model"] == (
            "actual-claude" if seat["provider"] == "claude" else None
        )
        assert_sealed_session_log(data, restored, seat["key"], seat["provider"])
    wire = [json.loads(line) for line in capture.read_text().splitlines()]
    turns = [
        item
        for item in wire
        if item["kind"] == "request" and item["message"].get("method") == "turn/start"
    ]
    assert len(turns) == 1
    assert "sandboxPolicy" not in turns[0]["message"]["params"]
    assert "sandbox" not in turns[0]["message"]["params"]
    config_reads = [
        item
        for item in wire
        if item["kind"] == "request" and item["message"].get("method") == "config/read"
    ]
    assert len(config_reads) == 2  # Environment check and actual review process.
    assert all(item["message"]["params"]["includeLayers"] for item in config_reads)
    claude_turns = [
        item for item in wire if item["kind"] == "spawn" and item["argv"][:1] == ["-p"]
    ]
    assert len(claude_turns) == int(dual)
    for item in claude_turns:
        assert item["api_key_present"] is False
        assert item["auth_token_present"] is False
    assert len(list((data / "logs").rglob("*.jsonl"))) >= (4 if dual else 2)


def test_real_cli_claude_logged_out_exit_one_persists_auth_expired_without_turn(
    tmp_path, monkeypatch
):
    data = tmp_path / "data"
    capture = tmp_path / "wire.jsonl"
    monkeypatch.setenv("FAKE_RUNTIME_CAPTURE", str(capture))
    monkeypatch.setenv("FAKE_RUNTIME_MODE", "success")
    monkeypatch.setenv("FAKE_AUTH_JSON", '{"loggedIn": false}')
    monkeypatch.setenv("FAKE_AUTH_EXIT", "1")
    monkeypatch.setenv("FAKE_VERSION_EXIT", "0")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-remove-me")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "synthetic-remove-me")
    executable = fake_executable(tmp_path, "claude")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("Review the constant change.")
    diff = tmp_path / "change.patch"
    diff.write_text(
        "--- a/source.py\n+++ b/source.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
    )
    frozen = cli(
        data,
        "freeze",
        "--diff",
        str(diff),
        "--requirements",
        str(requirements),
        "--acceptance",
        "value equals two",
    )
    output = cli(
        data,
        "run",
        frozen["snapshot_id"],
        "--seat",
        "claude",
        "claude-requested",
        "high",
        "--claude-bin",
        executable,
        "--timeout",
        "5",
        "--grace",
        "0.1",
    )
    restored = cli(data, "show", output["round_id"], "--history")
    assert restored["snapshot_id"] == frozen["snapshot_id"]
    assert restored["status"] == output["status"] == "incomplete"
    assert restored["assessment"]["status"] == "incomplete"
    assert len(restored["seats"]) == 1
    attempt = restored["seats"][0]["attempts"][0]
    assert attempt["status"] == "failed"
    assert attempt["failure_category"] == "auth_expired"
    assert attempt["detail"] == "runtime reports no login"
    assert attempt["session_id"] is None
    assert attempt["result"] is None
    assert attempt["exit_code"] is None
    assert attempt["reported_model"] is None
    assert attempt["reported_effort"] is None
    evidence = restored["execution"]["reviewer-1"]["1"]["environment"]
    assert evidence["logged_in"] is False
    assert evidence["cli_version"] == "fake-claude 1.0"
    stored = execution_evidence(data, output["round_id"])["reviewer-1"]["1"]
    assert "session_logs" not in stored
    events = [item["envelope"]["event"] for item in restored["history"]]
    assert [event["type"] for event in events] == ["attempt_started", "error"]
    assert events[-1]["failure_category"] == "auth_expired"
    assert all(item["disposition"] == "applied" for item in restored["history"])
    wire = [json.loads(line) for line in capture.read_text().splitlines()]
    assert [item["argv"] for item in wire if item["kind"] == "spawn"] == [
        ["--version"],
        ["auth", "status"],
    ]
    assert all(item["kind"] == "spawn" for item in wire)
    assert all(
        not item["api_key_present"] and not item["auth_token_present"] for item in wire
    )
    records = [
        json.loads(line)
        for path in (data / "logs").rglob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    assert any(
        record["kind"] == "exit" and record["returncode"] == 1 for record in records
    )


@pytest.mark.parametrize("cancel", [False, True])
def test_real_cli_preserves_sealed_session_when_launched_reviewer_stops(
    tmp_path, monkeypatch, cancel
):
    repo = tmp_path / "repository"
    subprocess.run(
        ["git", "init", "-q", "--template=", str(repo)], check=True, capture_output=True
    )
    data = tmp_path / "data"
    capture = tmp_path / "wire.jsonl"
    monkeypatch.setenv("FAKE_RUNTIME_CAPTURE", str(capture))
    monkeypatch.setenv("FAKE_RUNTIME_MODE", "wait" if cancel else "model-error")
    executable = fake_executable(tmp_path, "claude")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("Review the constant change.")
    diff = tmp_path / "change.patch"
    diff.write_text(
        "--- a/source.py\n+++ b/source.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
    )
    frozen = cli(
        data,
        "freeze",
        "--diff",
        str(diff),
        "--diff-repository",
        str(repo),
        "--requirements",
        str(requirements),
        "--acceptance",
        "value equals two",
    )
    run = (
        "run",
        frozen["snapshot_id"],
        "--seat",
        "claude",
        "claude-requested",
        "high",
        "--claude-bin",
        executable,
        "--timeout",
        "5",
        "--grace",
        "0.1",
    )
    if cancel:
        with subprocess.Popen(
            [sys.executable, "-m", "multi_agent", "--data-dir", str(data), *run],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as process:
            deadline = time.monotonic() + 10
            while not (capture.exists() and '"argv": ["-p"' in capture.read_text()):
                assert process.poll() is None and time.monotonic() < deadline
                time.sleep(0.01)
            process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=10)
            assert process.returncode == 0, stderr
            output = json.loads(stdout)
    else:
        output = cli(data, *run)
    assert output["assessment"]["status"] == "incomplete"
    attempt = output["seats"][0]["attempts"][0]
    assert attempt["status"] == "failed"
    assert (attempt["failure_category"] == "cancelled") == cancel
    assert_sealed_session_log(data, output, "reviewer-1", "claude")


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_real_cli_preserves_literal_email_path_quotes_and_masks_registered_auth(
    tmp_path, monkeypatch, provider
):
    repo = tmp_path / "quoted-repository"
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(
            [
                "git",
                "-c",
                f"core.hooksPath={os.devnull}",
                "-c",
                "commit.gpgSign=false",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-C",
                str(repo),
                *args,
            ],
            text=True,
        ).strip()

    git("init", "-q", "--template=")
    (repo / "source.py").write_text("contact = {}\n")
    git("add", ".")
    git("commit", "-qm", "baseline")
    baseline = git("rev-parse", "HEAD")
    quote = "contact = " + json.dumps(
        {
            "email": "reviewer@example.invalid",
            "path": str(Path.home() / "public-project" / "settings.json"),
            "access_token": "public-example-value",
        }
    )
    (repo / "source.py").write_text(quote + "\n")
    git("commit", "-qam", "candidate")
    candidate = git("rev-parse", "HEAD")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("Review the changed literal without altering its quotes.")
    secret = "synthetic-access_token"
    summary = "Synthetic review " + (secret if provider == "codex" else "complete")
    report = {
        "summary": summary,
        "findings": [
            {
                "id": "literal-quote",
                "severity": "P2",
                "category": "fixture",
                "side": "candidate",
                "path": "source.py",
                "line_start": 1,
                "line_end": 1,
                "quote": quote,
                "title": "Synthetic literal finding",
                "impact": "Literal evidence must remain exact",
                "rationale": "The cited line is the frozen candidate source",
            }
        ],
    }
    monkeypatch.setenv("FAKE_RUNTIME_MODE", "success")
    monkeypatch.setenv("FAKE_RUNTIME_REPORT", json.dumps(report))
    executable = fake_executable(tmp_path, provider)
    data = tmp_path / "data"
    frozen = cli(
        data,
        "freeze",
        "--repo",
        str(repo),
        "--baseline",
        baseline,
        "--candidate",
        candidate,
        "--requirements",
        str(requirements),
        "--acceptance",
        "Retain exact source quotations",
    )
    output = cli(
        data,
        "run",
        frozen["snapshot_id"],
        "--seat",
        provider,
        "model-1",
        "high",
        f"--{provider}-bin",
        executable,
        "--timeout",
        "5",
        "--grace",
        "0.1",
    )
    restored = cli(data, "show", output["round_id"], "--history")
    attempt = restored["seats"][0]["attempts"][0]
    assert attempt["status"] == "succeeded"
    assert attempt["result"]["findings"][0]["quote"] == quote
    checked = restored["assessment"]["reports"]["reviewer-1"]["findings"][0]
    assert checked["quote"] == quote and checked["reference_status"] == "verified"
    assert attempt["result"]["summary"] == (
        "Synthetic review <REDACTED>" if provider == "codex" else summary
    )
    assert secret not in json.dumps(output)
    assert secret not in json.dumps(restored)
    assert secret.encode() not in (data / "reviews.sqlite3").read_bytes()
    assert all(
        secret not in path.read_text() for path in (data / "logs").rglob("*.jsonl")
    )


_PREFLIGHT_CLEANUP_SCRIPT = """
import asyncio
import json
import sys
from pathlib import Path

from multi_agent.adapters.base import EnvironmentInfo
from multi_agent.adapters.runtime_decode import RuntimeFailure
from multi_agent.adapters.scripted import ScriptedAdapter, TurnScript
from multi_agent.cli import runner
from multi_agent.core import SeatConfig
from multi_agent.core.events import (
    Completed, Error, FailureCategory, ProcessExited, SessionStarted,
)
from multi_agent.store import ReviewStore


async def main():
    data = Path(sys.argv[1])
    snapshot_id, mode = sys.argv[2:]
    entered = asyncio.Event()
    stop = asyncio.Event()
    detail = 'fixture preflight cleanup remains unknown'

    class UnknownPreflight(ScriptedAdapter):
        def __init__(self, log_dir):
            super().__init__(())
            self.log_dir = log_dir

        async def check_environment(self):
            self.log_dir.mkdir(parents=True)
            (self.log_dir / 'preflight.jsonl').write_text(json.dumps({
                'kind': 'cleanup_incomplete', 'detail': detail,
                'synthetic_adapter_evidence': True,
            }) + '\\n')
            entered.set()
            if mode != 'failure':
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    if mode == 'cancel-unclassified':
                        raise OSError(detail) from None
            raise RuntimeFailure(Error(
                detail, raw_type='adapter.cleanup_incomplete',
            ))

        async def start_session(self, config):
            raise AssertionError('unknown preflight must not launch a session')

    def factory(config, log_dir, metadata, schema):
        if config.key == 'reviewer-2':
            return UnknownPreflight(log_dir)
        return ScriptedAdapter((TurnScript(
            (
                SessionStarted('fixture-success-session'),
                Completed({'summary': 'Preserve successful seat', 'findings': []}),
                ProcessExited(0),
            ),
            (Error('fixture stopped', FailureCategory.CANCELLED), ProcessExited(0)),
        ),), environment=EnvironmentInfo('fixture-runtime', True))

    runner.make_adapter = factory
    with ReviewStore(data / 'reviews.sqlite3') as store:
        with runner.execution_lock(data):
            round_id = runner.new_review(
                store, snapshot_id,
                (
                    SeatConfig('reviewer-1', 'codex', 'success-model', 'high'),
                    SeatConfig('reviewer-2', 'claude', 'unknown-model', 'high'),
                ),
                timeout=5, grace=0.1,
                executables={provider: str(data / ('must-not-launch-' + provider))
                             for provider in ('codex', 'claude')},
            )
            # All persisted state and history are produced by the real runner.
            await runner.execute_round(
                store, round_id, data, stop=asyncio.Event(), seat_keys=('reviewer-1',),
            )
            success_before = runner.view_round(store, round_id, history=True)

            async def request_cancel():
                await entered.wait()
                stop.set()

            cancelling = (asyncio.create_task(request_cancel())
                          if mode != 'failure' else None)
            boundary_error = None
            try:
                await runner.execute_round(
                    store, round_id, data, stop=stop, seat_keys=('reviewer-2',),
                )
            except ValueError as error:
                boundary_error = str(error)
            finally:
                if cancelling is not None:
                    cancelling.cancel()
                    await asyncio.gather(cancelling, return_exceptions=True)
    return {'round_id': round_id, 'success_before': success_before,
            'boundary_error': boundary_error}


async def bounded():
    async with asyncio.timeout(5):
        return await main()


print(json.dumps(asyncio.run(bounded())))
"""


@pytest.mark.parametrize("mode", ["failure", "cancel", "cancel-unclassified"])
def test_real_cli_preserves_unconfirmed_preflight_and_rejects_all_retries(
    tmp_path, mode
):
    data = tmp_path / "data"
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("Review the frozen constant change.")
    diff = tmp_path / "change.patch"
    diff.write_text(
        "--- a/source.py\n+++ b/source.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
    )
    frozen = cli(
        data,
        "freeze",
        "--diff",
        str(diff),
        "--requirements",
        str(requirements),
        "--acceptance",
        "value equals two",
    )
    fixture = tmp_path / "preflight_fixture.py"
    fixture.write_text(_PREFLIGHT_CLEANUP_SCRIPT)
    prepared = subprocess.run(
        [sys.executable, str(fixture), str(data), frozen["snapshot_id"], mode],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert prepared.returncode == 0, prepared.stderr
    receipt = json.loads(prepared.stdout)
    restored = cli(data, "show", receipt["round_id"], "--history")
    assert restored["snapshot_id"] == frozen["snapshot_id"]
    assert restored["status"] == "in_progress"
    assert restored["assessment"]["status"] == "incomplete"
    assert receipt["boundary_error"] is not None
    success, unknown = restored["seats"]
    assert success == receipt["success_before"]["seats"][0]
    assert success["attempts"][0]["status"] == "succeeded"
    assert success["attempts"][0]["result"]["summary"] == "Preserve successful seat"
    assert unknown["current_attempt"] == 1
    assert len(unknown["attempts"]) == 1
    attempt = unknown["attempts"][0]
    assert attempt["status"] == ("running" if mode == "failure" else "cancelling")
    for key in (
        "failure_category",
        "session_id",
        "result",
        "exit_code",
        "reported_model",
        "reported_effort",
    ):
        assert attempt[key] is None
    evidence = restored["execution"]["reviewer-2"]["1"]
    assert (
        "fixture preflight cleanup remains unknown"
        in evidence["completion_unconfirmed"]
    )
    if mode == "cancel-unclassified":
        assert "OSError" in evidence["completion_unconfirmed"]
    assert evidence["environment"] is None
    adapter_log = Path(evidence["log_dir"]) / "preflight.jsonl"
    assert json.loads(adapter_log.read_text())["kind"] == "cleanup_incomplete"
    unknown_events = [
        item["envelope"]["event"]["type"]
        for item in restored["history"]
        if item["envelope"]["seat"] == "reviewer-2"
    ]
    assert unknown_events == (
        ["attempt_started"]
        if mode == "failure"
        else ["attempt_started", "cancel_requested"]
    )
    successful_history = [
        item for item in restored["history"] if item["envelope"]["seat"] == "reviewer-1"
    ]
    assert successful_history == receipt["success_before"]["history"]
    for options in ([], ["--recover"]):
        rejected = subprocess.run(
            [
                sys.executable,
                "-m",
                "multi_agent",
                "--data-dir",
                str(data),
                "retry",
                receipt["round_id"],
                *options,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        assert rejected.returncode == 2, (rejected.stdout, rejected.stderr)
        assert cli(data, "show", receipt["round_id"], "--history") == restored
