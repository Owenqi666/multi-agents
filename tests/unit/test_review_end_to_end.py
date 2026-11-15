"""Real module entry points and transports, using only synthetic CLI executables."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


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
    fake_body = (Path(__file__).parent / "runtime_fakes/fake_cli.py").read_text()
    executables = {}
    for provider in ("codex", "claude"):
        executable = tmp_path / f"{provider}-fake"
        executable.write_text(f"#!{sys.executable}\n" + fake_body)
        executable.chmod(0o755)
        executables[provider] = str(executable)
    capture = tmp_path / "wire.jsonl"
    monkeypatch.setenv("FAKE_RUNTIME_MODE", "success")
    monkeypatch.setenv("FAKE_RUNTIME_CAPTURE", str(capture))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-remove-me")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "synthetic-remove-me")
    data = tmp_path / "data"

    def cli(*args):
        process = subprocess.run(
            [sys.executable, "-m", "multi_agent", "--data-dir", str(data), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert process.returncode == 0, process.stderr
        return json.loads(process.stdout)

    frozen = cli(
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
    restored = cli("show", output["round_id"], "--history")
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
