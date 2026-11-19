"""The public CLI with Git, SQLite and the existing synthetic transports."""

import json
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import test_runtime as runtime_helpers

from multi_agent.adapters.ownership import RuntimeOwnershipReceipt
from multi_agent.cli.main import main

synthetic_codex_auth_and_fake_environment = (
    runtime_helpers.synthetic_codex_auth_and_fake_environment
)


def _git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def project(tmp_path, monkeypatch):
    repo = tmp_path / "repository"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "README.md").write_text("committed repository context\n")
    _git(repo, "add", "README.md")
    _git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.org",
        "commit",
        "-qm",
        "baseline",
    )
    baseline = _git(repo, "rev-parse", "HEAD")
    (repo / "README.md").write_text("different working-tree text\n")
    data = tmp_path / "data"
    goals = tmp_path / "original.txt", tmp_path / "confirmed.txt"
    goals[0].write_text("PM 原话。\n")
    goals[1].write_text("PM 确认的目标。\n")
    capture = tmp_path / "runtime.jsonl"
    monkeypatch.setenv("FAKE_RUNTIME_CAPTURE", str(capture))
    return repo, baseline, data, goals, capture


@pytest.fixture
def document():
    stages = []
    for identifier, depends_on in (
        ("ingest", []),
        ("classify", ["ingest"]),
        ("report", ["ingest", "classify"]),
    ):
        stages.append(
            {
                "stage_id": identifier,
                "title": f"Stage {identifier}",
                "goal": f"Goal {identifier}",
                "approach": f"Approach {identifier}",
                "workflow": [f"Step {identifier}"],
                "depends_on": depends_on,
                "acceptance_criteria": [f"Verify {identifier}"],
                "deliverables": [f"Deliver {identifier}"],
            }
        )
    return {
        "title": "Ledger project",
        "architecture": "One local storage layer.",
        "shared_interfaces": ["Ledger API"],
        "stages": stages,
    }


def _invoke(capsys, data, *args, status=0):
    assert main(["--data-dir", str(data), "project", *args]) == status
    output = capsys.readouterr()
    assert not output.err
    return json.loads(output.out)


def _confirm(capsys, project, *extra):
    repo, baseline, data, (original, confirmed), _ = project
    return _invoke(
        capsys,
        data,
        "confirm",
        "--repo",
        str(repo),
        "--baseline",
        baseline,
        "--original-goal",
        str(original),
        "--confirmed-goal",
        str(confirmed),
        *extra,
    )


def _planner(tmp_path, provider):
    executable = Path(runtime_helpers.fake(tmp_path, provider))
    fake_source = executable.read_text()
    shebang, body = fake_source.split("\n", 1)
    inspect_input = """
import json
import signal
import os
from pathlib import Path

goal_file = Path("project-goal.json")
if goal_file.is_file():
    with open(os.environ["FAKE_RUNTIME_CAPTURE"], "a") as stream:
        stream.write(json.dumps({
            "kind": "frozen_input",
            "goal": json.loads(goal_file.read_text()),
            "context": Path("source/baseline/README.md").read_text(),
            "snapshot": json.loads(Path("source/snapshot.json").read_text()),
        }) + "\\n")
"""
    executable.write_text(shebang + "\n" + inspect_input + "\n" + body)
    return str(executable)


@pytest.mark.parametrize(
    ("provider", "model"), [("codex", "model-1"), ("claude", "requested-claude")]
)
def test_project_plan_and_history(
    tmp_path, project, document, capsys, monkeypatch, provider, model
):
    repo, baseline, data, (_, confirmed_file), capture = project
    goal = _confirm(capsys, project)
    assert goal["baseline_commit"] == baseline
    assert goal["repository"] == str(repo)
    assert _invoke(capsys, data, "show", goal["goal_id"]) == goal
    assert not capture.exists()

    monkeypatch.setenv("FAKE_RUNTIME_REPORT", json.dumps(document))
    options = (
        "--planner",
        provider,
        model,
        "high",
        f"--{provider}-bin",
        _planner(tmp_path, provider),
    )
    plan = _invoke(capsys, data, "plan", goal["goal_id"], *options)
    assert plan["status"] == "succeeded"
    assert plan["goal"] == {key: value for key, value in goal.items() if key != "kind"}
    assert plan["document"] == document
    markdown = plan["markdown"]
    assert "One local storage layer." in markdown
    assert "Ledger API" in markdown
    for stage in document["stages"]:
        section = markdown.split(f"## {stage['stage_id']} ·", 1)[1].split("\n## ", 1)[0]
        assert stage["acceptance_criteria"][0] in section
        assert stage["deliverables"][0] in section
        assert (
            "### 依赖\n\n"
            + ("\n".join(f"- {dep}" for dep in stage["depends_on"]) or "无")
        ) in section
    evidence = plan["evidence"]
    assert plan["planner_request"] == {
        "provider": provider,
        "model": model,
        "effort": "high",
    }
    assert evidence["reported_model"] == (
        None if provider == "codex" else "actual-claude"
    )
    assert evidence["reported_effort"] is None
    assert evidence["exit_code"] == 0
    assert evidence["outputs_complete"]
    assert bool(evidence["usage"]) == (provider == "claude")
    assert evidence["logs"] and all(log["sealed"] for log in evidence["logs"])

    lines = [json.loads(line) for line in capture.read_text().splitlines()]
    frozen = [line for line in lines if line["kind"] == "frozen_input"]
    assert frozen
    for observed in frozen:
        assert observed["goal"] == plan["goal"]
        assert observed["context"] == "committed repository context\n"
        assert observed["snapshot"]["baseline"] == baseline
        assert observed["snapshot"]["snapshot_id"] == plan["source_snapshot_id"]
    if provider == "codex":
        turns = [
            line["message"]["params"]
            for line in lines
            if line["kind"] == "request"
            and line["message"].get("method") == "turn/start"
        ]
        assert len(turns) == 1
        assert turns[0]["model"] == model and turns[0]["effort"] == "high"
        prompt = turns[0]["input"][0]["text"]
    else:
        turns = [
            line["argv"]
            for line in lines
            if line["kind"] == "spawn" and line["argv"][0] == "-p"
        ]
        assert len(turns) == 1
        assert turns[0][turns[0].index("--model") + 1] == model
        prompt = turns[0][1]
    assert all(
        value in prompt for value in (baseline, "project-goal.json", "source/baseline/")
    )
    assert _git(repo, "status", "--short") == "M README.md"

    confirmed_file.write_text("PM 修改并重新确认。\n")
    revised = _confirm(capsys, project, "--project-id", goal["project_id"])
    assert revised["goal_id"] != goal["goal_id"]
    assert len(capture.read_text().splitlines()) == len(lines)
    revised_plan = _invoke(capsys, data, "plan", revised["goal_id"], *options)
    assert revised_plan["plan_id"] != plan["plan_id"]
    assert revised_plan["goal"]["goal_id"] == revised["goal_id"]
    launches = capture.read_bytes()
    assert main(["--data-dir", str(data), "snapshot", plan["source_snapshot_id"]]) == 0
    archived = json.loads(capsys.readouterr().out)
    assert archived["input"]["baseline"] == baseline
    reopen = subprocess.check_output(
        [
            sys.executable,
            "-m",
            "multi_agent",
            "--data-dir",
            str(data),
            "project",
            "show",
            plan["plan_id"],
        ]
    )
    assert json.loads(reopen) == plan
    history = _invoke(capsys, data, "show", goal["project_id"])
    assert [version["plan_id"] for version in history["plans"]] == [
        plan["plan_id"],
        revised_plan["plan_id"],
    ]
    assert capture.read_bytes() == launches


@pytest.mark.parametrize(
    "invalid", ["forward_dependency", "wrong_shape", "incomplete_output"]
)
def test_invalid_planner_output_is_retained_as_failure(
    tmp_path, project, document, capsys, monkeypatch, invalid
):
    _, _, data, _, _ = project
    goal = _confirm(capsys, project)
    if invalid == "forward_dependency":
        document["stages"][0]["depends_on"] = ["report"]
    elif invalid == "wrong_shape":
        del document["stages"][0]["workflow"]
    else:
        monkeypatch.setattr(
            RuntimeOwnershipReceipt, "outputs_complete", property(lambda _: False)
        )
    monkeypatch.setenv("FAKE_RUNTIME_REPORT", json.dumps(document))
    failure = _invoke(
        capsys,
        data,
        "plan",
        goal["goal_id"],
        "--planner",
        "claude",
        "requested-claude",
        "high",
        "--claude-bin",
        _planner(tmp_path, "claude"),
        status=1,
    )
    assert failure["status"] == "failed"
    assert failure["document"] is None and failure["markdown"] is None
    assert failure["error"]["raw_type"] == (
        "adapter.cleanup_incomplete"
        if invalid == "incomplete_output"
        else "project.validation"
    )
    assert failure["evidence"]["reported_model"] == "actual-claude"
    assert _invoke(capsys, data, "show", failure["plan_id"]) == failure


def test_goal_confirmation_rejects_database_symlink(project, capsys):
    repo, baseline, data, (original, confirmed), capture = project
    data.mkdir()
    target = repo / "unexpected.sqlite3"
    (data / "reviews.sqlite3").symlink_to(target)
    status = main(
        [
            "--data-dir",
            str(data),
            "project",
            "confirm",
            "--repo",
            str(repo),
            "--baseline",
            baseline,
            "--original-goal",
            str(original),
            "--confirmed-goal",
            str(confirmed),
        ]
    )
    assert status == 2
    assert "project database must be a regular file" in capsys.readouterr().err
    assert not target.exists() and not capture.exists()


def test_interrupted_planning_retains_reported_shutdown(
    tmp_path, project, capsys, monkeypatch
):
    _, _, data, _, _ = project
    goal = _confirm(capsys, project)
    monkeypatch.setenv("FAKE_RUNTIME_MODE", "wait")
    with subprocess.Popen(
        [
            sys.executable,
            "-m",
            "multi_agent",
            "--data-dir",
            str(data),
            "project",
            "plan",
            goal["goal_id"],
            "--planner",
            "claude",
            "requested-claude",
            "high",
            "--claude-bin",
            _planner(tmp_path, "claude"),
            "--timeout",
            "10",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    ) as process:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and process.poll() is None:
            if any(
                '"waiting"' in log.read_text(encoding="utf-8")
                for log in (data / "logs" / "projects").glob("*/*.jsonl")
            ):
                process.send_signal(signal.SIGINT)
                break
            time.sleep(0.02)
        else:
            pytest.fail("the synthetic planner never began waiting")
        stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 1, stderr.decode()
    receipt = json.loads(stdout)
    assert receipt["status"] == "failed"
    assert receipt["error"]["failure_category"] == "cancelled"
    assert receipt["evidence"]["reported_model"] is None
    assert receipt["evidence"]["processes_stopped"]
    assert _invoke(capsys, data, "show", receipt["plan_id"]) == receipt
