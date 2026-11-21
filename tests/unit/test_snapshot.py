"""Frozen inputs use committed objects, including under hostile local settings."""

import json
import os
import stat
import subprocess
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from multi_agent.cli import snapshot as boundary
from multi_agent.cli.main import main
from multi_agent.cli.review_inputs import decode_review_input
from multi_agent.cli.runner import ensure_external
from multi_agent.cli.snapshot import freeze_diff, freeze_repository, materialize
from multi_agent.core.snapshot import FrozenFile, FrozenSnapshot
from multi_agent.core.snapshot import TestEvidence as Evidence

REQUIREMENTS = "Return the correct result."
ACCEPTANCE = ("The documented examples pass.",)
REQUIRED = (("python", "-m", "pytest"),)


def git(repo: Path, *args: str, data: bytes | None = None) -> str:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Snapshot Test",
            "-c",
            "user.email=snapshot@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-C",
            str(repo),
            *args,
        ],
        input=data,
        capture_output=True,
        env=environment,
        check=True,
    )
    return result.stdout.decode("utf-8").strip()


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repository"
    repo.mkdir()
    git(repo, "init", "--quiet", "--template=")
    (repo / "old.txt").write_text("kept content\n")
    (repo / "source.py").write_text("value = 1\n")
    (repo / "deleted.txt").write_text("only baseline\n")
    (repo / "run.py").write_text("print('not executed')\n")
    (repo / "run.py").chmod(0o755)
    (repo / "binary.dat").write_bytes(b"\xff\x00binary")
    (repo / "link").symlink_to("../../external-secret")
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "baseline")
    baseline = git(repo, "rev-parse", "HEAD")
    (repo / "source.py").write_text("value = 2\n")
    (repo / "old.txt").rename(repo / "renamed.txt")
    (repo / "deleted.txt").unlink()
    (repo / "新增.txt").write_text("new\r\ncontent")
    (repo / "empty.txt").touch()
    git(repo, "add", "--all")
    git(repo, "commit", "--quiet", "-m", "candidate")
    return repo, baseline, git(repo, "rev-parse", "HEAD")


def freeze(repo: Path, base: str, candidate: str) -> FrozenSnapshot:
    return freeze_repository(
        repo,
        base,
        candidate,
        REQUIREMENTS,
        ACCEPTANCE,
        (Evidence(candidate, REQUIRED[0], 0, "all tests passed\n"),),
        required_tests=REQUIRED,
    )


def test_commit_trees_renames_deletions_and_opaque_objects(repository) -> None:
    repo, baseline, candidate = repository
    value = freeze(repo, baseline, candidate)
    files = {(item.side, item.path): item for item in value.files}
    assert value.repository == str((repo / ".git").resolve())
    assert value.baseline == baseline and value.candidate == candidate
    assert files["baseline", "source.py"].text == "value = 1\n"
    assert files["candidate", "source.py"].text == "value = 2\n"
    assert files["baseline", "deleted.txt"].text == "only baseline\n"
    assert ("candidate", "deleted.txt") not in files
    assert files["baseline", "old.txt"].oid == files["candidate", "renamed.txt"].oid
    assert "rename from old.txt" in value.diff
    assert "rename to renamed.txt" in value.diff
    assert "deleted file mode" in value.diff
    assert files["candidate", "binary.dat"].text is None
    assert files["candidate", "link"].text == "../../external-secret"
    assert files["candidate", "link"].mode == "120000"
    assert files["candidate", "run.py"].mode == "100755"
    assert files["candidate", "新增.txt"].text == "new\r\ncontent"
    assert files["candidate", "empty.txt"].text == ""


def test_dirty_tree_index_attributes_and_local_config_do_not_change_input(
    repository, monkeypatch, tmp_path: Path
) -> None:
    repo, baseline, candidate = repository
    expected = freeze(repo, baseline, candidate)
    marker = tmp_path / "executed"
    external = tmp_path / "external.py"
    external.write_text(f"#!/usr/bin/env python3\nopen({str(marker)!r}, 'w').close()\n")
    external.chmod(0o755)
    git(repo, "config", "diff.evil.command", str(external))
    git(repo, "config", "diff.evil.textconv", str(external))
    git(repo, "config", "diff.noprefix", "true")
    git(repo, "config", "diff.algorithm", "histogram")
    (repo / "source.py").write_text("dirty and never reviewed\n")
    git(repo, "add", "source.py")
    (repo / ".gitattributes").write_text("* diff=evil\n")
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("*.py -diff\n")
    (repo / "untracked.txt").write_text("never included\n")
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", str(external))
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "wrong-repository"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "diff.external")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(external))
    actual = freeze(repo, baseline, candidate)
    assert actual == expected
    assert actual.snapshot_id == expected.snapshot_id
    assert not marker.exists()
    assert all(item.path != "untracked.txt" for item in actual.files)


def test_hash_and_codec_cover_every_input_field(repository) -> None:
    repo, baseline, candidate = repository
    original = freeze(repo, baseline, candidate)
    assert original.snapshot_id == freeze(repo, baseline, candidate).snapshot_id
    restored = FrozenSnapshot.from_dict(json.loads(json.dumps(original.to_dict())))
    assert restored == original
    assert restored.snapshot_id == original.snapshot_id
    variants = (
        replace(original, repository="different repository"),
        replace(original, baseline=candidate),
        replace(original, candidate=baseline),
        replace(original, diff=original.diff + "\n"),
        replace(original, requirements="Changed requirement"),
        replace(original, acceptance=("Changed criterion",)),
        replace(original, tests=(replace(original.tests[0], log="different log"),)),
        replace(original, tests=(replace(original.tests[0], exit_code=1),)),
        replace(original, tests=(replace(original.tests[0], command=("true",)),)),
        replace(original, tests=(replace(original.tests[0], commit=baseline),)),
        replace(original, files=original.files[1:]),
        replace(original, required_tests=(("different",),)),
    )
    assert all(value.snapshot_id != original.snapshot_id for value in variants)
    encoded = original.to_dict()
    encoded["tests"][0]["command"].append("--changed")
    encoded["files"][0]["text"] = "changed"
    assert original == restored
    with pytest.raises(FrozenInstanceError):
        original.diff = "changed"


def test_materialize_is_read_only_and_never_follows_links(repository, tmp_path) -> None:
    repo, baseline, candidate = repository
    snapshot = freeze(repo, baseline, candidate)
    external = tmp_path / "external-secret"
    external.write_text("private")
    destination = tmp_path / "reviewer"
    destination.mkdir()
    materialize(snapshot, destination)
    assert (destination / "candidate" / "link").is_file()
    assert not (destination / "candidate" / "link").is_symlink()
    assert (destination / "candidate" / "link").read_text() == "../../external-secret"
    assert external.read_text() == "private"
    assert (destination / "candidate" / "新增.txt").read_bytes() == b"new\r\ncontent"
    assert (destination / "candidate" / "empty.txt").read_bytes() == b""
    assert not (destination / "candidate" / "binary.dat").exists()
    assert not any(path.name.casefold() == ".git" for path in destination.rglob("*"))
    for path in destination.rglob("*"):
        mode = stat.S_IMODE(path.stat().st_mode)
        assert not mode & 0o222
        if path.is_file():
            assert not mode & 0o111
    manifest = json.loads((destination / "snapshot.json").read_text())
    assert manifest["snapshot_id"] == snapshot.snapshot_id
    assert {item["path"] for item in manifest["omitted_files"]} == {"binary.dat"}
    assert manifest["tests_are_user_supplied"] is True
    with pytest.raises(ValueError, match="empty directory"):
        materialize(snapshot, destination)


@pytest.mark.parametrize(
    "path",
    (
        "../escape",
        "/absolute",
        "a/../../escape",
        "./name",
        "a//name",
        "a\\name",
        ".git/config",
        "a/.GiT /config",
    ),
)
def test_unsafe_snapshot_paths_rejected_before_writing(path, tmp_path) -> None:
    value = replace(
        freeze_diff("", REQUIREMENTS, ACCEPTANCE),
        files=(FrozenFile("candidate", path, "bad", "a" * 40, "100644"),),
    )
    destination = tmp_path / "reviewer"
    with pytest.raises(ValueError, match="unsafe"):
        materialize(value, destination)
    assert not destination.exists()


@pytest.mark.parametrize("paths", (("a", "a/file"), ("A", "a"), ("e\u0301", "é")))
def test_materialization_path_collisions_are_rejected(paths, tmp_path) -> None:
    value = replace(
        freeze_diff("", REQUIREMENTS, ACCEPTANCE),
        files=tuple(
            FrozenFile("baseline", path, "", "b" * 40, "100644") for path in paths
        ),
    )
    with pytest.raises(ValueError, match="collide"):
        materialize(value, tmp_path / "reviewer")


def test_materialize_refuses_symlink_destination(tmp_path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    destination = tmp_path / "reviewer"
    destination.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        materialize(freeze_diff("", REQUIREMENTS, ACCEPTANCE), destination)
    assert list(target.iterdir()) == []


@pytest.mark.parametrize(
    "revision", ("--help", "missing", "HEAD:path", "HEAD\n--all", "HEAD\0")
)
def test_bad_revisions_are_rejected(repository, revision) -> None:
    repo, _, candidate = repository
    with pytest.raises(ValueError):
        freeze(repo, revision, candidate)


@pytest.mark.parametrize(
    "evidence",
    (
        Evidence("HEAD", ("pytest",), 0, ""),
        Evidence("a" * 40, (), 0, ""),
        Evidence("a" * 40, ("",), 0, ""),
        Evidence("a" * 40, ("pytest\0",), 0, ""),
        Evidence("a" * 40, ("pytest",), True, ""),
        Evidence("a" * 40, ("pytest",), 0, "bad\0log"),
    ),
)
def test_invalid_test_evidence_is_rejected_before_git(
    repository, evidence, monkeypatch
) -> None:
    repo, baseline, candidate = repository
    monkeypatch.setattr(
        boundary,
        "_git",
        lambda *args, **kwargs: pytest.fail("Git should not be called"),
    )
    with pytest.raises((ValueError, TypeError)):
        freeze_repository(
            repo, baseline, candidate, REQUIREMENTS, ACCEPTANCE, (evidence,)
        )


def test_required_tests_are_unique_and_do_not_claim_execution(repository) -> None:
    repo, baseline, candidate = repository
    with pytest.raises(ValueError, match="unique"):
        freeze_repository(
            repo,
            baseline,
            candidate,
            REQUIREMENTS,
            ACCEPTANCE,
            required_tests=REQUIRED + REQUIRED,
        )
    value = freeze_repository(
        repo, baseline, candidate, REQUIREMENTS, ACCEPTANCE, required_tests=REQUIRED
    )
    assert value.required_tests == REQUIRED and value.tests == ()


def test_diff_only_has_no_repository_or_test_evidence() -> None:
    value = freeze_diff("--- a/a\n+++ b/a\n", REQUIREMENTS, ACCEPTANCE)
    assert value.repository is value.baseline is value.candidate is None
    assert value.files == value.tests == value.required_tests == ()
    assert "bound_repository" not in value.to_dict()
    assert FrozenSnapshot.from_dict(value.to_dict()) == value
    assert freeze_diff("", REQUIREMENTS, ACCEPTANCE).diff == ""
    with pytest.raises(ValueError):
        freeze_diff("diff", "", ACCEPTANCE)
    with pytest.raises(ValueError):
        freeze_diff("diff", REQUIREMENTS, ())


def test_bound_patch_cli_preserves_text_and_shares_identity_only_with_linked_worktree(
    repository, tmp_path, capsys
) -> None:
    repo, baseline, _ = repository
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "--detach", str(linked), baseline)
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "--quiet", "--template=")
    diff = tmp_path / "paste.diff"
    patch = "--- a/a\r\n+++ b/a\r\n@@ -1 +1 @@\r\n-old\r\n+新\r\n"
    diff.write_bytes(patch.encode())
    requirements = tmp_path / "requirements.txt"
    requirement_text = "保留原文\r\n"
    requirements.write_bytes(requirement_text.encode())
    arguments = ["--data-dir", str(tmp_path / "data")]
    inputs = ["--requirements", str(requirements), "--acceptance", "更新文件"]

    def freeze_patch(root):
        assert (
            main(
                [
                    *arguments,
                    "freeze",
                    "--diff",
                    str(diff),
                    "--diff-repository",
                    str(root),
                    *inputs,
                ]
            )
            == 0
        )
        return json.loads(capsys.readouterr().out)

    frozen = freeze_patch(repo)
    assert frozen["report_only"]
    assert frozen["bound_repository"] == str((repo / ".git").resolve())
    assert frozen == freeze_patch(linked)
    assert frozen["snapshot_id"] != freeze_patch(other)["snapshot_id"]

    assert main([*arguments, "snapshot", frozen["snapshot_id"]]) == 0
    stored = json.loads(capsys.readouterr().out)["input"]
    assert stored["diff"] == patch
    assert stored["requirements"] == requirement_text
    assert stored["repository"] is stored["baseline"] is stored["candidate"] is None
    assert stored["tests"] == stored["files"] == stored["required_tests"] == []
    snapshot = decode_review_input(stored, expected_id=frozen["snapshot_id"])
    ensure_external(tmp_path / "data", snapshot)
    with pytest.raises(ValueError, match="outside the target repository"):
        ensure_external(repo / "data", snapshot)
    with pytest.raises(ValueError, match="complete frozen hash"):
        decode_review_input(
            {**stored, "bound_repository": str((other / ".git").resolve())},
            expected_id=frozen["snapshot_id"],
        )
    with pytest.raises(ValueError, match="cannot contain commit or test evidence"):
        decode_review_input({**stored, "candidate": baseline})
    with pytest.raises(ValueError, match="canonical Git common directory"):
        decode_review_input({**stored, "bound_repository": "relative/.git"})

    assert (
        main(
            [
                *arguments,
                "freeze",
                "--repo",
                str(repo),
                "--diff-repository",
                str(other),
                *inputs,
            ]
        )
        == 2
    )
    assert "--diff-repository requires --diff" in capsys.readouterr().err
    archive_data = repo / ".git" / "review-data"
    assert (
        main(
            [
                "--data-dir",
                str(archive_data),
                "freeze",
                "--diff",
                str(diff),
                "--diff-repository",
                str(repo),
                *inputs,
            ]
        )
        == 2
    )
    assert "outside the repository Git metadata" in capsys.readouterr().err
    assert not archive_data.exists()


def test_empty_tree_and_submodule_metadata(tmp_path) -> None:
    repo = tmp_path / "empty"
    repo.mkdir()
    git(repo, "init", "--quiet", "--template=")
    git(repo, "commit", "--quiet", "--allow-empty", "-m", "empty")
    baseline = git(repo, "rev-parse", "HEAD")
    value = freeze(repo, baseline, baseline)
    assert value.files == () and value.diff == ""
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{baseline},dependency")
    git(repo, "commit", "--quiet", "-m", "gitlink")
    candidate = git(repo, "rev-parse", "HEAD")
    value = freeze(repo, baseline, candidate)
    assert value.files == (
        FrozenFile("candidate", "dependency", None, baseline, "160000"),
    )
    assert "Subproject commit" in value.diff


def test_binary_diff_uses_marker_and_size_limits_reject_without_truncation(
    repository, monkeypatch
) -> None:
    repo, baseline, _ = repository
    (repo / "binary.dat").write_bytes(b"\x00different")
    git(repo, "add", "binary.dat")
    git(repo, "commit", "--quiet", "-m", "binary")
    candidate = git(repo, "rev-parse", "HEAD")
    value = freeze(repo, baseline, candidate)
    assert "Binary files a/binary.dat and b/binary.dat differ" in value.diff
    monkeypatch.setattr(boundary, "MAX_FILE_BYTES", 1)
    with pytest.raises(ValueError, match="exceeds"):
        freeze(repo, baseline, candidate)


def test_all_git_calls_use_argv_no_shell(repository, monkeypatch) -> None:
    repo, baseline, candidate = repository
    original = subprocess.run
    calls = []

    def record(argv, **kwargs):
        calls.append((argv, kwargs))
        return original(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", record)
    freeze(repo, baseline, candidate)
    assert calls
    assert all(isinstance(argv, list) and argv[0] == "git" for argv, _ in calls)
    assert all("--no-lazy-fetch" in argv for argv, _ in calls)
    assert all(not options.get("shell", False) for _, options in calls)
    assert all(options["timeout"] == boundary.GIT_TIMEOUT for _, options in calls)
    diff = next(argv for argv, _ in calls if "diff" in argv)
    assert "--no-ext-diff" in diff and "--no-textconv" in diff


def test_subdirectory_input_and_worktrees_share_repository_identity(
    repository, tmp_path
) -> None:
    repo, baseline, candidate = repository
    nested = repo / "nested"
    nested.mkdir()
    worktree = tmp_path / "linked"
    git(repo, "worktree", "add", "--detach", str(worktree), candidate)
    assert freeze(repo, baseline, candidate) == freeze(nested, baseline, candidate)
    assert freeze(repo, baseline, candidate) == freeze(worktree, baseline, candidate)


def test_replace_refs_do_not_rewrite_snapshot(repository) -> None:
    repo, baseline, candidate = repository
    original = freeze(repo, baseline, candidate)
    git(repo, "replace", baseline, candidate)
    assert freeze(repo, baseline, candidate) == original
