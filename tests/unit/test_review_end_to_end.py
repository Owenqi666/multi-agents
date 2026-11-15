"""Real module entry points and transports, using only synthetic CLI executables."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


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
    executable.write_text(f"#!{sys.executable}\n" + fake_body)
    executable.chmod(0o755)
    return str(executable)


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
    executables = {
        provider: fake_executable(tmp_path, provider)
        for provider in ("codex", "claude")
    }
    capture = tmp_path / "wire.jsonl"
    monkeypatch.setenv("FAKE_RUNTIME_MODE", "success")
    monkeypatch.setenv("FAKE_RUNTIME_CAPTURE", str(capture))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-remove-me")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "synthetic-remove-me")
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
    wire = [json.loads(line) for line in capture.read_text().splitlines()]
    turns = [
        item
        for item in wire
        if item["kind"] == "request" and item["message"].get("method") == "turn/start"
    ]
    assert len(turns) == 1
    assert turns[0]["message"]["params"]["sandboxPolicy"] == {"type": "readOnly"}
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
    executable = fake_executable(tmp_path, "claude")
    capture = tmp_path / "wire.jsonl"
    monkeypatch.setenv("FAKE_RUNTIME_CAPTURE", str(capture))
    monkeypatch.setenv("FAKE_RUNTIME_MODE", "success")
    monkeypatch.setenv("FAKE_AUTH_JSON", '{"loggedIn": false}')
    monkeypatch.setenv("FAKE_AUTH_EXIT", "1")
    monkeypatch.setenv("FAKE_VERSION_EXIT", "0")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-remove-me")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "synthetic-remove-me")
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
