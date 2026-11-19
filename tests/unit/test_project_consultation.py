"""Goal consultation through the project CLI, SQLite, and synthetic runtimes."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import test_project_cli as project_helpers
import test_runtime as runtime_helpers

from multi_agent.cli.main import main

project = project_helpers.project
document = project_helpers.document
synthetic_codex_auth_and_fake_environment = (
    runtime_helpers.synthetic_codex_auth_and_fake_environment
)
_MODELS = {"codex": "gpt-6.2-astra", "claude": "claude-fable-5-2"}
_ADVICE = {
    "codex": {
        "draft_goal": "Ship the ledger with seven-day retention.",
        "background": [{"claim": "The repository has a README.", "path": "README.md"}],
        "assumptions": ["Retention is seven days."],
        "questions": ["Who can access archived entries?"],
        "remaining_disagreements": [],
    },
    "claude": {
        "draft_goal": "Ship the ledger with thirty-day retention.",
        "background": [
            {"claim": "The README describes the source.", "path": "README.md"}
        ],
        "assumptions": ["Retention is thirty days."],
        "questions": ["Who approves the retention window?"],
        "remaining_disagreements": ["The retention window still needs PM approval."],
    },
}


@pytest.fixture(autouse=True)
def synthetic_advisors(tmp_path, monkeypatch):
    bin_dir = tmp_path / "advisor-bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir), prepend=os.pathsep)
    for provider, model in _MODELS.items():
        executable = Path(runtime_helpers.fake(bin_dir, provider))
        shebang, body = executable.read_text().split("\n", 1)
        if provider == "codex":
            body = body.replace('"model-1"', json.dumps(model))
        observe = f"""
import json
import os
import sys
import tomllib
from pathlib import Path

input_file = Path("consultation-input.json")
provider = {provider!r}
is_model_process = sys.argv[1:2] in (["app-server"], ["-p"])
if input_file.is_file() and is_model_process:
    os.environ["FAKE_RUNTIME_REPORT"] = {json.dumps(_ADVICE[provider])!r}
    if provider == "claude" and os.environ.get("FAKE_CONSULTATION_SECOND_FAILURE"):
        os.environ["FAKE_RUNTIME_MODE"] = "model-error"
    permission = None
    if provider == "codex":
        config_file = Path(os.environ["CODEX_HOME"]) / "config.toml"
        config = tomllib.loads(config_file.read_text())
        permissions = config["permissions"][config["default_permissions"]]
        permission = permissions["filesystem"][str(Path.cwd())]
    with open(os.environ["FAKE_RUNTIME_CAPTURE"], "a") as stream:
        stream.write(json.dumps({{
            "kind": "consultation_input",
            "provider": provider,
            "input": json.loads(input_file.read_text()),
            "source": Path("source/baseline/README.md").read_text(),
            "snapshot": json.loads(Path("source/snapshot.json").read_text()),
            "permission": permission,
        }}) + "\\n")
"""
        executable.write_text(shebang + "\n" + observe + "\n" + body)
        executable.rename(bin_dir / provider)


def _consult(capsys, project, providers, *extra, status=0):
    repo, baseline, data, (original, _), _ = project
    advisors = [
        argument
        for provider in providers
        for argument in ("--advisor", provider, _MODELS[provider], "high")
    ]
    return project_helpers._invoke(
        capsys,
        data,
        "consult",
        "--repo",
        str(repo),
        "--baseline",
        baseline,
        "--original-goal",
        str(original),
        *advisors,
        *extra,
        status=status,
    )


def _observed_turns(records):
    turns = []
    for record in records:
        if (
            record["kind"] == "request"
            and record["message"].get("method") == "turn/start"
        ):
            request = record["message"]["params"]
            assert request["approvalPolicy"] == "never"
            turns.append(("codex", request["model"], request["effort"]))
        elif record["kind"] == "spawn" and record["argv"][0] == "-p":
            args = record["argv"]
            assert args[args.index("--tools") + 1] == "Read,Grep,Glob"
            assert "--safe-mode" in args and "--no-session-persistence" in args
            turns.append(
                (
                    "claude",
                    args[args.index("--model") + 1],
                    args[args.index("--effort") + 1],
                )
            )
    return turns


def test_direct_goal_confirmation_does_not_call_an_advisor(project, capsys):
    _, _, data, (original, confirmed), capture = project
    goal = project_helpers._confirm(capsys, project)
    assert goal["original_goal"] == original.read_text()
    assert goal["confirmed_goal"] == confirmed.read_text()
    assert project_helpers._invoke(capsys, data, "show", goal["goal_id"]) == goal
    assert not capture.exists()


@pytest.mark.parametrize(
    "providers",
    [("claude",), ("codex", "claude"), ("claude", "codex")],
    ids=["single", "astra-then-fable", "fable-then-astra"],
)
def test_consultation_uses_ordered_read_only_seats_against_one_source(
    project, capsys, providers
):
    repo, baseline, data, (original, _), capture = project
    result = _consult(capsys, project, providers)
    assert result["kind"] == "goal_consultation"
    assert result["status"] == "succeeded"
    assert result["draft"] == _ADVICE[providers[-1]]
    seats = result["seats"]
    assert [seat["number"] for seat in seats] == list(range(1, len(providers) + 1))
    launches = capture.read_bytes()
    records = [json.loads(line) for line in launches.splitlines()]
    assert _observed_turns(records) == [
        (provider, _MODELS[provider], "high") for provider in providers
    ]
    inputs = [record for record in records if record["kind"] == "consultation_input"]
    assert [record["provider"] for record in inputs] == list(providers)
    for i, (provider, seat, observed) in enumerate(
        zip(providers, seats, inputs, strict=True)
    ):
        assert seat["request"] == {
            "provider": provider,
            "model": _MODELS[provider],
            "effort": "high",
        }
        assert seat["advice"] == _ADVICE[provider]
        assert seat["status"] == "succeeded"
        assert seat["error"] is None
        source_id = result["source_snapshot_id"]
        assert seat["source_snapshot_id"] == source_id
        assert observed["input"]["baseline_commit"] == baseline
        assert observed["input"]["source_snapshot_id"] == source_id
        assert observed["input"]["original_goal"] == original.read_text()
        assert observed["input"]["first_advice"] == (
            _ADVICE[providers[0]] if i else None
        )
        assert observed["snapshot"]["snapshot_id"] == source_id
        assert observed["snapshot"]["baseline"] == baseline
        assert observed["source"] == "committed repository context\n"
        if provider == "codex":
            assert observed["permission"] == "read"
        assert seat["evidence"]["reported_model"] == (
            "actual-claude" if provider == "claude" else None
        )
        assert seat["evidence"]["reported_effort"] is None
    reopened = subprocess.check_output(
        [
            sys.executable,
            "-m",
            "multi_agent",
            "--data-dir",
            str(data),
            "project",
            "show",
            result["consultation_id"],
        ],
        timeout=15,
    )
    assert json.loads(reopened) == result
    assert capture.read_bytes() == launches
    assert project_helpers._git(repo, "status", "--short") == "M README.md"


def test_failed_second_advisor_preserves_first_without_confirming(
    project, capsys, monkeypatch
):
    _, _, data, _, capture = project
    goal = project_helpers._confirm(capsys, project)
    monkeypatch.setenv("FAKE_CONSULTATION_SECOND_FAILURE", "1")
    failed = _consult(
        capsys,
        project,
        ("codex", "claude"),
        "--project-id",
        goal["project_id"],
        status=1,
    )
    saved = project_helpers._invoke(capsys, data, "show", failed["consultation_id"])
    assert saved == failed
    assert saved["status"] == "failed" and saved["draft"] is None
    first, second = saved["seats"]
    assert first["advice"] == _ADVICE["codex"] and first["error"] is None
    assert second["status"] == "failed" and second["advice"] is None
    assert second["error"]["failure_category"] == "model_unavailable"
    assert second["evidence"]["reported_model"] is None
    assert second["evidence"]["reported_effort"] is None
    assert _observed_turns(
        [json.loads(line) for line in capture.read_bytes().splitlines()]
    ) == [
        ("codex", _MODELS["codex"], "high"),
        ("claude", _MODELS["claude"], "high"),
    ]
    history = project_helpers._invoke(capsys, data, "show", goal["project_id"])
    assert [item["goal_id"] for item in history["goals"]] == [goal["goal_id"]]
    assert history["plans"] == []


def test_edited_consultation_requires_confirmation_and_its_own_project(
    project, document, capsys, monkeypatch
):
    repo, baseline, data, (original, confirmed), capture = project
    first = project_helpers._confirm(capsys, project)
    second = project_helpers._confirm(capsys, project)
    consultation = _consult(
        capsys, project, ("codex",), "--project-id", first["project_id"]
    )
    consultation_id = consultation["consultation_id"]
    launches = capture.read_bytes()
    history = project_helpers._invoke(capsys, data, "show", first["project_id"])
    assert [item["goal_id"] for item in history["goals"]] == [first["goal_id"]]
    assert history["plans"] == []
    planner = ("--planner", "codex", _MODELS["codex"], "high")
    prefix = ["--data-dir", str(data), "project"]
    assert main([*prefix, "plan", consultation_id, *planner]) == 2
    assert "unknown confirmed goal" in capsys.readouterr().err

    confirmed.write_text("Ship the ledger with fourteen-day retention.\n")
    adoption = ("--consultation", consultation_id, "--adoption", "edited")
    assert (
        main(
            [
                *prefix,
                "confirm",
                "--repo",
                str(repo),
                "--baseline",
                baseline,
                "--original-goal",
                str(original),
                "--confirmed-goal",
                str(confirmed),
                "--project-id",
                second["project_id"],
                *adoption,
            ]
        )
        == 2
    )
    assert "consultation belongs to a different project" in capsys.readouterr().err
    unrelated = project_helpers._invoke(capsys, data, "show", second["project_id"])
    assert [goal["goal_id"] for goal in unrelated["goals"]] == [second["goal_id"]]

    adopted = project_helpers._confirm(
        capsys, project, "--project-id", first["project_id"], *adoption
    )
    assert adopted["goal_id"] != first["goal_id"]
    assert adopted["original_goal"] == original.read_text()
    assert adopted["confirmed_goal"] == confirmed.read_text()
    assert adopted["consultation"] == {
        "consultation_id": consultation_id,
        "adoption": "edited",
        "source_snapshot_id": consultation["source_snapshot_id"],
    }
    assert project_helpers._invoke(capsys, data, "show", adopted["goal_id"]) == adopted
    assert (
        project_helpers._invoke(capsys, data, "show", consultation_id) == consultation
    )
    assert capture.read_bytes() == launches

    monkeypatch.setenv("FAKE_RUNTIME_REPORT", json.dumps(document))
    plan = project_helpers._invoke(capsys, data, "plan", adopted["goal_id"], *planner)
    assert plan["document"] == document
    assert plan["goal"]["goal_id"] == adopted["goal_id"]
    assert plan["goal"]["confirmed_goal"] == adopted["confirmed_goal"]
    assert plan["goal"]["consultation"] == adopted["consultation"]
