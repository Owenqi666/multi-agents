"""The suite never acts on the repository whose git process launched it."""

import os
import subprocess
import sys
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]


def _git(directory, *args):
    return subprocess.check_output(
        [
            "git",
            "-C",
            str(directory),
            "-c",
            "user.name=Committer",
            "-c",
            "user.email=committer@example.invalid",
            *args,
        ],
        text=True,
    ).strip()


def test_suite_leaves_the_committing_repository_untouched(tmp_path):
    # A commit made from a linked worktree hands its pre-commit hook GIT_DIR and
    # GIT_INDEX_FILE. Fixtures that run plain `git init` and `git config` used to
    # follow them and rewrite the shared config of the repository being committed
    # to. This stands a scratch repository in for that one.
    checkout = tmp_path / "checkout"
    linked = tmp_path / "linked"
    checkout.mkdir()
    _git(checkout, "init", "-q")
    (checkout / "tracked.txt").write_text("tracked\n")
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-qm", "baseline")
    _git(checkout, "worktree", "add", "-q", "-b", "linked", str(linked))
    git_dir = Path(_git(linked, "rev-parse", "--absolute-git-dir"))
    assert git_dir.parent.name == "worktrees"
    config = checkout / ".git" / "config"
    before = config.read_bytes()
    head = _git(linked, "rev-parse", "HEAD")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "tests/unit/test_cli_inputs.py::test_repository_requires_two_commits",
        ],
        cwd=REPOSITORY,
        env={
            **os.environ,
            "GIT_DIR": str(git_dir),
            "GIT_INDEX_FILE": str(git_dir / "index"),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert config.read_bytes() == before
    assert _git(checkout, "rev-parse", "--is-bare-repository") == "false"
    assert _git(linked, "rev-parse", "HEAD") == head
    assert _git(linked, "status", "--porcelain") == ""
    assert result.returncode == 0, result.stdout + result.stderr
