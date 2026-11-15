import json
import subprocess
import sys

import pytest

from multi_agent.cli.main import main


@pytest.fixture
def cli_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(repo), *args], text=True
        ).strip()

    git("init", "-q")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    (repo / "code.py").write_text("value = 1\n")
    git("add", ".")
    git("commit", "-qm", "baseline")
    baseline = git("rev-parse", "HEAD")
    (repo / "code.py").write_text("value = 2\n")
    git("commit", "-qam", "candidate")
    candidate = git("rev-parse", "HEAD")
    requirement = tmp_path / "requirements.txt"
    requirement.write_text("Update the value")
    args = [
        "--data-dir",
        str(tmp_path / "data"),
        "freeze",
        "--repo",
        str(repo),
        "--baseline",
        baseline,
        "--candidate",
        candidate,
        "--requirements",
        str(requirement),
        "--acceptance",
        "value becomes 2",
    ]
    return repo, candidate, args


def test_cli_freeze_snapshot_and_real_module_entry(cli_repo, tmp_path, capsys):
    _, candidate, args = cli_repo
    evidence = tmp_path / "tests.json"
    evidence.write_text(
        json.dumps(
            {
                "required_commands": [["not-executed", "--flag"]],
                "results": [
                    {
                        "commit": candidate,
                        "command": ["not-executed", "--flag"],
                        "exit_code": 0,
                        "log": "user provided",
                    }
                ],
            }
        )
    )
    assert main([*args, "--tests", str(evidence)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["test_evidence_source"] == "user_supplied"
    assert result["test_records"] == 1
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "multi_agent",
            *args[:2],
            "snapshot",
            result["snapshot_id"],
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode == 0, process.stderr
    frozen = json.loads(process.stdout)
    assert frozen["input"]["candidate"] == candidate
    assert frozen["input"]["required_tests"] == [["not-executed", "--flag"]]
    assert main([*args, "--tests", str(evidence)]) == 0
    assert json.loads(capsys.readouterr().out)["snapshot_id"] == result["snapshot_id"]


@pytest.mark.parametrize(
    "value",
    [
        {"results": [], "required_commands": [], "extra": True},
        {"results": [], "required_commands": ["shell string"]},
        {
            "results": [
                {"commit": "x", "command": ["x"], "exit_code": True, "log": ""}
            ],
            "required_commands": [],
        },
        {
            "results": [{"commit": "x", "command": ["x"], "exit_code": 0, "log": ""}],
            "required_commands": [],
        },
    ],
)
def test_cli_rejects_invalid_evidence(cli_repo, tmp_path, capsys, value):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(value))
    assert main([*cli_repo[2], "--tests", str(path)]) == 2
    assert capsys.readouterr().out == ""


def test_data_directory_must_be_outside_actual_repository(cli_repo, tmp_path, capsys):
    repo, _, args = cli_repo
    nested = repo / "src"
    nested.mkdir()
    link = tmp_path / "linked-output"
    link.symlink_to(repo, target_is_directory=True)
    for data in (repo / "out", link / "out"):
        invalid = list(args)
        invalid[1] = str(data)
        invalid[4] = str(nested)
        assert main(invalid) == 2
        assert "outside" in capsys.readouterr().err
    assert not (repo / "out").exists()


def test_diff_only_cannot_claim_test_evidence(tmp_path, capsys):
    diff = tmp_path / "input.diff"
    diff.write_text("diff --git a/x b/x\n")
    requirement = tmp_path / "requirement"
    requirement.write_text("Review pasted patch")
    args = [
        "--data-dir",
        str(tmp_path / "data"),
        "freeze",
        "--diff",
        str(diff),
        "--requirements",
        str(requirement),
        "--acceptance",
        "Check patch",
    ]
    assert main(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["report_only"] is True
    assert output["repository"] is None
    assert output["test_records"] == 0
    assert main([*args, "--tests", "does-not-exist"]) == 2
    assert "cannot include" in capsys.readouterr().err


def test_repository_requires_two_commits(cli_repo, capsys):
    args = list(cli_repo[2])
    index = args.index("--candidate")
    del args[index : index + 2]
    assert main(args) == 2
    assert "both" in capsys.readouterr().err


def test_data_directory_cannot_use_external_git_metadata(cli_repo, tmp_path, capsys):
    repo, _, args = cli_repo
    external = tmp_path / "external.git"
    subprocess.run(
        ["git", "-C", str(repo), "init", "--separate-git-dir", str(external)],
        check=True,
        capture_output=True,
    )
    args[1] = str(external / "data")
    assert main(args) == 2
    assert "Git metadata" in capsys.readouterr().err
    assert not (external / "data").exists()
