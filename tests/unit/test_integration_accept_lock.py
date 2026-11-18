"""Verify the accept guard against a separate real Git process."""

import subprocess

import pytest
from test_task_workflow_cli import git

from multi_agent.cli.integration_git import read_ref, verify_integration_refs

TARGET = "refs/heads/main"
PINNED = "refs/multi-agent/integrations/fixed"


def _commits(repository):
    git(repository, "init", "--quiet", "--template=", "--initial-branch=main")
    (repository / "tracked.txt").write_text("baseline\n")
    git(repository, "add", "tracked.txt")
    git(repository, "commit", "--quiet", "-m", "baseline")
    base = git(repository, "rev-parse", "HEAD")
    tree = git(repository, "rev-parse", "HEAD^{tree}")
    candidate = git(repository, "commit-tree", tree, "-p", base, "-m", "candidate")
    git(repository, "update-ref", PINNED, candidate)
    return base, candidate


def test_acceptance_guard_locks_both_refs_without_writing_them(tmp_path):
    base, candidate = _commits(tmp_path)
    git(tmp_path, "pack-refs", "--all")
    dirty = tmp_path / "tracked.txt"
    dirty.write_text("staged change\n")
    git(tmp_path, "add", "tracked.txt")
    dirty.write_text("working change\n")
    metadata = tmp_path / ".git"
    index = (metadata / "index").read_bytes()
    reflog = (metadata / "logs" / TARGET).read_bytes()
    packed = (metadata / "packed-refs").read_bytes()

    with verify_integration_refs(tmp_path, TARGET, base, PINNED, candidate):
        for ref, old, new in (
            (TARGET, base, candidate),
            (PINNED, candidate, base),
        ):
            with pytest.raises(subprocess.CalledProcessError) as blocked:
                git(tmp_path, "update-ref", "--no-deref", ref, new, old)
            assert b".lock" in blocked.value.stderr
            assert read_ref(tmp_path, ref) == old

    assert read_ref(tmp_path, TARGET) == base
    assert read_ref(tmp_path, PINNED) == candidate
    assert (metadata / "index").read_bytes() == index
    assert (metadata / "logs" / TARGET).read_bytes() == reflog
    assert (metadata / "packed-refs").read_bytes() == packed
    assert dirty.read_text() == "working change\n"

    git(tmp_path, "update-ref", "--no-deref", TARGET, candidate, base)
    with (
        pytest.raises(ValueError, match=TARGET),
        verify_integration_refs(tmp_path, TARGET, base, PINNED, candidate),
    ):
        pytest.fail("guard yielded for a stale target")
    # A failed prepare may have locked the private ref before finding the stale
    # target. Its lock must also be gone when control returns.
    git(tmp_path, "update-ref", "--no-deref", PINNED, base, candidate)


def test_acceptance_guard_rejects_matching_symbolic_candidate(tmp_path):
    base, candidate = _commits(tmp_path)
    git(tmp_path, "symbolic-ref", PINNED, TARGET)
    with (
        pytest.raises(ValueError, match="direct Git reference"),
        verify_integration_refs(tmp_path, TARGET, base, PINNED, base),
    ):
        pytest.fail("guard yielded for a matching but symbolic candidate")
    assert git(tmp_path, "symbolic-ref", PINNED) == TARGET
    git(tmp_path, "update-ref", "--no-deref", TARGET, candidate, base)
