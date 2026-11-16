"""Delivery examines all final paths and commits only the already-inspected bytes."""

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from multi_agent.cli import workspaces as boundary
from multi_agent.cli.task_inputs import freeze_project_rules, parse_task_card
from multi_agent.core.events import FailureCategory
from multi_agent.core.path_patterns import normalize_path


def git(repo: Path, *args: str, data: bytes | None = None) -> bytes:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    result = subprocess.run(
        [
            "git",
            "--no-optional-locks",
            "-c",
            "user.name=Delivery Test",
            "-c",
            "user.email=delivery@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.fsmonitor=false",
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
        timeout=30,
    )
    return result.stdout


def text_git(repo: Path, *args: str) -> str:
    return git(repo, *args).decode("utf-8").strip()


@pytest.fixture
def repository(tmp_path: Path):
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "--quiet", "--template=")
    for directory in ("src", "docs", ".multi-agent"):
        (repo / directory).mkdir()
    files = {
        "src/modify.py": b"original = 1\n",
        "src/delete.py": b"delete this file\n",
        "src/oldname.py": b"rename these unchanged bytes\n",
        "src/tool.sh": b"#!/bin/sh\nexit 0\n",
        "src/data.bin": b"\x00\xff\x80binary\r\n",
        "docs/guide.md": b"Original guide\n",
        "README.md": b"Protected readme\n",
        ".gitignore": b"src/cache/\nsrc/ignored.bin\ncache/\n",
    }
    for name, content in files.items():
        (repo / name).write_bytes(content)
    rules = {
        "version": 1,
        "commands": {"checks": {"executable": "pytest", "args": []}},
        "protected_paths": [
            ".multi-agent/**",
            "README.md",
            "src/secrets",
            "private/secret.py",
            "secrets/straße.txt",
            "assets/ﬁle.txt",
            "café/config",
        ],
        "implementation": [
            {
                "paths": ["src/**"],
                "provider": "codex",
                "default_model": "requested",
                "default_effort": "medium",
            }
        ],
        "default_implementation": {
            "provider": "codex",
            "default_model": "requested",
            "default_effort": "medium",
        },
    }
    (repo / ".multi-agent/rules.yaml").write_text(json.dumps(rules, ensure_ascii=False))
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "baseline")
    baseline = text_git(repo, "rev-parse", "HEAD")
    return repo, baseline, freeze_project_rules(repo, baseline)


def task(baseline: str, scopes: tuple[str, ...] = ("src/**",)):
    return parse_task_card(
        json.dumps(
            {
                "schema_version": 1,
                "id": "T-0100",
                "goal": "Implement the specified changes.",
                "baseline_commit": baseline,
                "write_scope": list(scopes),
                "depends_on": [],
                "interface_contracts": [],
                "deliverables": ["A fixed candidate commit."],
                "acceptance_criteria": ["The configured checks pass."],
                "test_refs": ["checks"],
                "open_questions": [],
            }
        )
    )


@contextmanager
def workspace(
    repo: Path, baseline: str, destination: Path, *, start_commit: str | None = None
):
    with boundary.executor_lock(repo) as lease:
        record = boundary.create_workspace(
            lease, baseline, destination, start_commit=start_commit
        )
        try:
            yield lease, record
        finally:
            boundary.remove_workspace(lease, record)


def names(repo: Path, commit: str) -> set[str]:
    return {
        value.decode("utf-8")
        for value in git(repo, "ls-tree", "-r", "--name-only", "-z", commit).split(
            b"\0"
        )
        if value
    }


def test_delivery_includes_untracked_and_rename_sides(
    repository, tmp_path: Path
) -> None:
    repo, baseline, rules = repository
    initial = names(repo, baseline)
    destination = tmp_path / "implementation"
    with workspace(repo, baseline, destination) as (lease, record):
        (destination / "src/modify.py").write_bytes(b"updated = 2\n")
        (destination / "src/delete.py").unlink()
        (destination / "src/oldname.py").rename(destination / "src/newname.py")
        (destination / "src/tool.sh").chmod(0o755)
        (destination / "src/new.bin").write_bytes(b"\x00\xfeadded")
        (destination / "src/ignored.bin").write_bytes(b"ignored but must be captured")
        (destination / "src/cache").mkdir()
        (destination / "src/cache/result.txt").write_bytes(b"generated result")
        delivery = boundary.freeze_delivery(lease, record, task(baseline), rules)
        assert set(delivery.changed_paths) == {
            "src/modify.py",
            "src/delete.py",
            "src/oldname.py",
            "src/newname.py",
            "src/tool.sh",
            "src/new.bin",
            "src/ignored.bin",
            "src/cache/result.txt",
        }
        assert delivery.baseline == baseline
        assert text_git(repo, "rev-parse", delivery.ref) == delivery.candidate
        assert (
            text_git(repo, "rev-parse", delivery.candidate + "^{tree}") == delivery.tree
        )
        assert text_git(repo, "rev-parse", delivery.candidate + "^") == baseline
        expected = (initial - {"src/delete.py", "src/oldname.py"}) | {
            "src/newname.py",
            "src/new.bin",
            "src/ignored.bin",
            "src/cache/result.txt",
        }
        assert names(repo, delivery.candidate) == expected
        for name, content in (
            ("src/data.bin", b"\x00\xff\x80binary\r\n"),
            ("src/new.bin", b"\x00\xfeadded"),
            ("src/ignored.bin", b"ignored but must be captured"),
            ("src/cache/result.txt", b"generated result"),
            ("README.md", b"Protected readme\n"),
        ):
            assert (
                git(repo, "cat-file", "blob", f"{delivery.candidate}:{name}") == content
            )
        assert git(repo, "ls-tree", delivery.candidate, "src/tool.sh").startswith(
            b"100755 blob "
        )
        assert boundary.inspect_workspace(lease, record).digest == delivery.digest
    assert not destination.exists()
    assert text_git(repo, "rev-parse", delivery.ref) == delivery.candidate


@pytest.mark.parametrize(
    "path", ["untracked.txt", "cache/generated.bin", "docs/new.md"]
)
def test_out_of_scope_untracked_and_ignored_files_are_rejected(
    repository, tmp_path: Path, path: str
) -> None:
    repo, baseline, rules = repository
    destination = tmp_path / "implementation"
    with workspace(repo, baseline, destination) as (lease, record):
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"outside declared write scope")
        with pytest.raises(boundary.DeliveryRejected) as failure:
            boundary.freeze_delivery(lease, record, task(baseline), rules)
        assert path in failure.value.paths
        assert failure.value.category is FailureCategory.OUT_OF_SCOPE_PATCH
        assert (
            text_git(
                repo,
                "for-each-ref",
                "--format=%(refname)",
                "refs/multi-agent/candidates/",
            )
            == ""
        )


@pytest.mark.parametrize(
    "source,target,rejected",
    [
        ("docs/guide.md", "src/imported.md", "docs/guide.md"),
        ("src/oldname.py", "docs/exported.py", "docs/exported.py"),
    ],
)
def test_delivery_rejects_either_rename_side_outside_scope(
    repository, tmp_path: Path, source: str, target: str, rejected: str
) -> None:
    repo, baseline, rules = repository
    destination = tmp_path / "implementation"
    with workspace(repo, baseline, destination) as (lease, record):
        (destination / source).rename(destination / target)
        with pytest.raises(boundary.DeliveryRejected) as failure:
            boundary.freeze_delivery(lease, record, task(baseline), rules)
        assert rejected in failure.value.paths


@pytest.mark.parametrize("operation", ["delete", "chmod"])
def test_out_of_scope_deletions_and_mode_changes_are_rejected(
    repository, tmp_path: Path, operation: str
) -> None:
    repo, baseline, rules = repository
    destination = tmp_path / "implementation"
    with workspace(repo, baseline, destination) as (lease, record):
        path = destination / "docs/guide.md"
        if operation == "delete":
            path.unlink()
        else:
            path.chmod(0o755)
        with pytest.raises(boundary.DeliveryRejected) as failure:
            boundary.freeze_delivery(lease, record, task(baseline), rules)
        assert "docs/guide.md" in failure.value.paths


@pytest.mark.parametrize("path", ["README.md", ".multi-agent/rules.yaml"])
def test_protected_paths_rejected(repository, tmp_path: Path, path: str) -> None:
    repo, baseline, rules = repository
    destination = tmp_path / "implementation"
    with workspace(repo, baseline, destination) as (lease, record):
        (destination / path).write_bytes(b"scope does not override protection")
        with pytest.raises(boundary.DeliveryRejected) as failure:
            boundary.freeze_delivery(lease, record, task(baseline, ("**",)), rules)
        assert path in failure.value.paths
        assert failure.value.category is FailureCategory.OUT_OF_SCOPE_PATCH


@pytest.mark.parametrize(
    "path", ["SECRETS/STRASSE.TXT", "assets/file.txt", "cafe\u0301/config"]
)
def test_delivery_protection_checks_case_and_unicode_aliases(
    repository, tmp_path: Path, path: str
) -> None:
    repo, baseline, rules = repository
    destination = tmp_path / "implementation"
    with workspace(repo, baseline, destination) as (lease, record):
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"protected alias")
        with pytest.raises(boundary.DeliveryRejected) as failure:
            boundary.freeze_delivery(lease, record, task(baseline, ("**",)), rules)
        assert normalize_path(path) in {
            normalize_path(item) for item in failure.value.paths
        }


def test_actual_path_metacharacters_are_not_treated_as_globs(
    repository, tmp_path: Path
) -> None:
    repo, baseline, rules = repository
    destination = tmp_path / "implementation"
    with workspace(repo, baseline, destination) as (lease, record):
        (destination / "src/*").write_bytes(b"literal star filename")
        delivery = boundary.freeze_delivery(
            lease, record, task(baseline, ("src/?",)), rules
        )
        assert delivery.changed_paths == ("src/*",)
        assert (
            git(repo, "cat-file", "blob", f"{delivery.candidate}:src/*")
            == b"literal star filename"
        )
    with workspace(repo, baseline, tmp_path / "not-allowed") as (lease, record):
        (Path(record.root) / "src/*").write_bytes(b"not the file named a")
        with pytest.raises(boundary.DeliveryRejected) as failure:
            boundary.freeze_delivery(lease, record, task(baseline, ("src/a",)), rules)
        assert failure.value.paths == ("src/*",)


def test_fixed_candidate_uses_validated_bytes(
    repository, tmp_path: Path, monkeypatch
) -> None:
    repo, baseline, rules = repository
    destination = tmp_path / "implementation"
    with workspace(repo, baseline, destination) as (lease, record):
        path = destination / "src/modify.py"
        path.write_bytes(b"validated = True\n")
        inspect = boundary.inspect_workspace
        captured = []

        def inspect_then_change(active, owned):
            result = inspect(active, owned)
            captured.append(result)
            path.write_bytes(b"unvalidated = True\n")
            (destination / "src/after-scan.py").write_bytes(b"late addition")
            return result

        with monkeypatch.context() as patch:
            patch.setattr(boundary, "inspect_workspace", inspect_then_change)
            delivery = boundary.freeze_delivery(lease, record, task(baseline), rules)
        assert len(captured) == 1
        assert delivery.digest == captured[0].digest
        assert delivery.changed_paths == ("src/modify.py",)
        assert (
            git(repo, "cat-file", "blob", f"{delivery.candidate}:src/modify.py")
            == b"validated = True\n"
        )
        assert "src/after-scan.py" not in names(repo, delivery.candidate)
        assert path.read_bytes() == b"unvalidated = True\n"


def test_candidate_preserves_dirty_source_index_branch_and_files(
    repository, tmp_path: Path
) -> None:
    repo, baseline, rules = repository
    source = repo / "src/modify.py"
    source.write_bytes(b"staged source change\n")
    git(repo, "add", "src/modify.py")
    source.write_bytes(b"unstaged source change\n")
    (repo / "source-only.txt").write_bytes(b"untracked source file\n")
    index = Path(
        text_git(repo, "rev-parse", "--path-format=absolute", "--git-path", "index")
    )
    before_index = index.read_bytes()
    before_status = git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    before_head = text_git(repo, "rev-parse", "HEAD")
    before_branch = text_git(repo, "symbolic-ref", "HEAD")
    with workspace(repo, baseline, tmp_path / "implementation") as (lease, record):
        target = Path(record.root) / "src/modify.py"
        assert target.read_bytes() == b"original = 1\n"
        target.write_bytes(b"implementation change\n")
        delivery = boundary.freeze_delivery(lease, record, task(baseline), rules)
        assert text_git(repo, "rev-parse", delivery.ref) == delivery.candidate
    assert index.read_bytes() == before_index
    assert (
        git(repo, "status", "--porcelain=v1", "--untracked-files=all") == before_status
    )
    assert text_git(repo, "rev-parse", "HEAD") == before_head
    assert text_git(repo, "symbolic-ref", "HEAD") == before_branch
    assert source.read_bytes() == b"unstaged source change\n"
    assert (repo / "source-only.txt").read_bytes() == b"untracked source file\n"


def test_owner_candidate_ref_is_created_once(repository, tmp_path: Path) -> None:
    repo, baseline, rules = repository
    with workspace(repo, baseline, tmp_path / "implementation") as (lease, record):
        (Path(record.root) / "src/modify.py").write_bytes(b"first candidate")
        first = boundary.freeze_delivery(lease, record, task(baseline), rules)
        (Path(record.root) / "src/modify.py").write_bytes(b"second candidate")
        with pytest.raises(ValueError):
            boundary.freeze_delivery(lease, record, task(baseline), rules)
        assert text_git(repo, "rev-parse", first.ref) == first.candidate


def test_candidate_compare_and_swap_preserves_ref_created_after_precheck(
    repository, tmp_path: Path, monkeypatch
) -> None:
    repo, baseline, rules = repository
    with workspace(repo, baseline, tmp_path / "implementation") as (lease, record):
        ref = f"refs/multi-agent/candidates/{record.owner_token}"
        (Path(record.root) / "src/modify.py").write_bytes(b"new candidate bytes")
        write_tree = boundary._write_tree

        def write_tree_then_create_ref(repository_path, files):
            tree = write_tree(repository_path, files)
            git(repo, "update-ref", ref, baseline)
            return tree

        with monkeypatch.context() as patch:
            patch.setattr(boundary, "_write_tree", write_tree_then_create_ref)
            with pytest.raises(ValueError):
                boundary.freeze_delivery(lease, record, task(baseline), rules)
        assert text_git(repo, "rev-parse", ref) == baseline
        assert text_git(repo, "rev-parse", "HEAD") == baseline


def test_candidate_creation_never_runs_clean_signing_or_hooks(
    repository, tmp_path: Path, monkeypatch
) -> None:
    repo, baseline, rules = repository
    marker = tmp_path / "forbidden-helper-ran"
    helper = tmp_path / "forbidden-helper"
    helper.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('unexpected execution')\n"
        "sys.stdout.buffer.write(sys.stdin.buffer.read())\n"
    )
    helper.chmod(0o755)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for name in ("post-checkout", "post-commit", "reference-transaction"):
        (hooks / name).write_bytes(helper.read_bytes())
        (hooks / name).chmod(0o755)
    for key, value in (
        ("filter.evil.clean", str(helper)),
        ("filter.evil.smudge", str(helper)),
        ("filter.evil.required", "true"),
        ("core.fsmonitor", str(helper)),
        ("core.hooksPath", str(hooks)),
        ("commit.gpgSign", "true"),
        ("gpg.program", str(helper)),
        ("gpg.format", "openpgp"),
        ("user.signingKey", "synthetic-test-key"),
    ):
        git(repo, "config", key, value)
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Injected")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "injected@example.invalid")
        monkeypatch.setenv(f"GIT_{role}_DATE", "2001-01-01T00:00:00Z")
    with workspace(repo, baseline, tmp_path / "implementation") as (lease, record):
        root = Path(record.root)
        (root / ".gitattributes").write_bytes(b"src/*.bin filter=evil\n")
        raw = b"\x00\xffliteral candidate bytes\r\n"
        (root / "src/new.bin").write_bytes(raw)
        delivery = boundary.freeze_delivery(
            lease, record, task(baseline, ("src/**", ".gitattributes")), rules
        )
        assert git(repo, "cat-file", "blob", f"{delivery.candidate}:src/new.bin") == raw
        commit = git(repo, "cat-file", "commit", delivery.candidate).decode()
        assert "author Multi-agent <multi-agent@localhost> " in commit
        assert "committer Multi-agent <multi-agent@localhost> " in commit
        assert "Injected" not in commit and "978307200" not in commit
        assert not marker.exists()
    assert not marker.exists()


@pytest.mark.parametrize("kind", ["direct", "symbolic", "dangling-symbolic"])
def test_existing_candidate_ref_never_updates_a_user_branch(
    repository, tmp_path: Path, kind: str
) -> None:
    repo, baseline, rules = repository
    with workspace(repo, baseline, tmp_path / "implementation") as (lease, record):
        ref = f"refs/multi-agent/candidates/{record.owner_token}"
        target = "refs/heads/user-target"
        if kind == "direct":
            git(repo, "update-ref", ref, baseline)
        else:
            if kind == "symbolic":
                git(repo, "update-ref", target, baseline)
            git(repo, "symbolic-ref", ref, target)
        (Path(record.root) / "src/modify.py").write_bytes(
            b"must not overwrite reference"
        )
        with pytest.raises(ValueError):
            boundary.freeze_delivery(lease, record, task(baseline), rules)
        if kind == "direct":
            assert text_git(repo, "rev-parse", ref) == baseline
        else:
            assert text_git(repo, "symbolic-ref", ref) == target
            if kind == "symbolic":
                assert text_git(repo, "rev-parse", target) == baseline
            else:
                assert (
                    text_git(repo, "for-each-ref", "--format=%(refname)", target) == ""
                )


def test_rework_delivery_checks_original_baseline(repository, tmp_path: Path) -> None:
    repo, baseline, rules = repository
    with workspace(repo, baseline, tmp_path / "first") as (lease, record):
        (Path(record.root) / "src/modify.py").write_bytes(b"first change")
        first = boundary.freeze_delivery(lease, record, task(baseline), rules)
    with workspace(
        repo, baseline, tmp_path / "rework", start_commit=first.candidate
    ) as (lease, record):
        assert record.task_baseline == baseline
        assert record.start_commit == first.candidate
        assert (Path(record.root) / "src/modify.py").read_bytes() == b"first change"
        (Path(record.root) / "src/new.py").write_bytes(b"rework change")
        second = boundary.freeze_delivery(lease, record, task(baseline), rules)
        assert second.baseline == baseline
        assert second.changed_paths == ("src/modify.py", "src/new.py")
        assert text_git(repo, "rev-parse", second.candidate + "^") == first.candidate
        assert (
            git(repo, "cat-file", "blob", f"{second.candidate}:.multi-agent/rules.yaml")
            == rules.raw_text.encode()
        )
    assert text_git(repo, "rev-parse", first.ref) == first.candidate


def test_rework_cannot_hide_out_of_scope_changes_in_its_start_commit(
    repository, tmp_path: Path
) -> None:
    repo, baseline, rules = repository
    (repo / "docs/guide.md").write_bytes(b"inherited outside-scope change")
    git(repo, "add", "docs/guide.md")
    git(repo, "commit", "--quiet", "-m", "descendant with broader changes")
    start_commit = text_git(repo, "rev-parse", "HEAD")
    with workspace(repo, baseline, tmp_path / "rework", start_commit=start_commit) as (
        lease,
        record,
    ):
        (Path(record.root) / "src/new.py").write_bytes(b"only new work is in scope")
        with pytest.raises(boundary.DeliveryRejected) as failure:
            boundary.freeze_delivery(lease, record, task(baseline), rules)
        assert "docs/guide.md" in failure.value.paths


@pytest.mark.parametrize("mismatch", ["card-baseline", "rules-baseline", "repository"])
def test_delivery_requires_matching_frozen_repository_and_baselines(
    repository, tmp_path: Path, mismatch: str
) -> None:
    repo, baseline, rules = repository
    card = task(baseline)
    if mismatch == "card-baseline":
        card = replace(card, baseline_commit="f" * 40)
    elif mismatch == "rules-baseline":
        rules = replace(rules, baseline_commit="f" * 40)
    else:
        rules = replace(rules, repository=str(tmp_path / "unrelated/.git"))
    with workspace(repo, baseline, tmp_path / "implementation") as (lease, record):
        with pytest.raises(ValueError):
            boundary.freeze_delivery(lease, record, card, rules)
        assert (
            text_git(
                repo,
                "for-each-ref",
                "--format=%(refname)",
                "refs/multi-agent/candidates/",
            )
            == ""
        )
