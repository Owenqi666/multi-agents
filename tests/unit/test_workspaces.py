"""Owned workspace services use temporary Git repositories and no model runtime."""

import os
import pickle
import shlex
import shutil
import socket
import stat
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from multi_agent.cli import workspaces
from multi_agent.cli.workspaces import (
    create_workspace,
    executor_lock,
    inspect_workspace,
    remove_workspace,
)


def _git(repo, *args, input_bytes=None):
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_ATTR_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
    )
    result = subprocess.run(
        [
            "git",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "commit.gpgSign=false",
            "-C",
            str(repo),
            *args,
        ],
        input=input_bytes,
        capture_output=True,
        check=False,
        timeout=15,
        env=environment,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result.stdout


@pytest.fixture
def workspace_repo(tmp_path):
    repo = tmp_path / "repository"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "src").mkdir()
    (repo / "src/code.txt").write_bytes(b"baseline\n")
    (repo / "src/run.py").write_bytes(b"print('baseline')\n")
    (repo / "src/run.py").chmod(0o755)
    (repo / "binary.bin").write_bytes(b"binary\x00\xff\r\n")
    (repo / ".gitattributes").write_text("src/*.txt filter=tripwire\n")
    (repo / ".gitignore").write_text("ignored-output.bin\n")
    (repo / "shortcut").symlink_to("src/code.txt")
    (repo / "second-shortcut").symlink_to("shortcut")
    (repo / "dangling").symlink_to("src/not-created-yet")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "baseline")
    baseline = _git(repo, "rev-parse", "HEAD").decode().strip()
    return repo, baseline


def _source_state(repo):
    files = {}

    def visit(directory, prefix=""):
        for entry in sorted(os.scandir(directory), key=lambda item: item.name):
            if not prefix and entry.name == ".git":
                continue
            name = prefix + entry.name
            if entry.is_symlink():
                files[name] = ("link", os.readlink(entry.path))
            elif entry.is_dir(follow_symlinks=False):
                visit(entry.path, name + "/")
            else:
                files[name] = (
                    stat.S_IMODE(entry.stat().st_mode),
                    Path(entry.path).read_bytes(),
                )

    visit(repo)
    index = Path(
        _git(repo, "rev-parse", "--path-format=absolute", "--git-path", "index")
        .decode()
        .strip()
    )
    return (
        _git(repo, "rev-parse", "HEAD"),
        _git(repo, "symbolic-ref", "HEAD"),
        index.read_bytes(),
        files,
    )


def _tripwire(tmp_path):
    marker = tmp_path / "unexpected-execution"
    script = tmp_path / "tripwire.py"
    script.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, sys\n"
        f"pathlib.Path({str(marker)!r}).write_text('executed')\n"
        "sys.stdout.buffer.write(sys.stdin.buffer.read())\n"
    )
    script.chmod(0o755)
    return script, marker


def test_workspace_materializes_exact_baseline_without_filters(
    workspace_repo, tmp_path
):
    repo, baseline = workspace_repo
    (repo / "src/code.txt").write_bytes(b"dirty working tree\n")
    (repo / "staged.txt").write_text("staged but not in baseline\n")
    _git(repo, "add", "staged.txt")
    (repo / "ignored-output.bin").write_bytes(b"source ignored output")
    (repo / "untracked.txt").write_text("source untracked\n")
    script, marker = _tripwire(tmp_path)
    hook_directory = tmp_path / "hooks"
    hook_directory.mkdir()
    for name in ("post-checkout", "post-index-change", "pre-auto-gc"):
        (hook_directory / name).symlink_to(script)
    for name, value in {
        "core.hooksPath": str(hook_directory),
        "core.fsmonitor": str(script),
        "filter.tripwire.clean": shlex.join([sys.executable, str(script)]),
        "filter.tripwire.smudge": shlex.join([sys.executable, str(script)]),
        "filter.tripwire.required": "true",
        "commit.gpgSign": "true",
        "gpg.program": str(script),
        "core.sparseCheckout": "true",
    }.items():
        _git(repo, "config", name, value)
    before = _source_state(repo)
    with executor_lock(repo) as lease:
        record = create_workspace(lease, baseline, tmp_path / "implementation")
        root = Path(record.root)
        inspection = inspect_workspace(lease, record)
        assert inspection == inspect_workspace(lease, record)
        files = {item.path: item for item in inspection.files}
        assert files["src/code.txt"].content == b"baseline\n"
        assert files["binary.bin"].content == b"binary\x00\xff\r\n"
        assert files["src/run.py"].mode == "100755"
        assert root.joinpath("src/run.py").stat().st_mode & 0o111
        assert files["shortcut"].mode == "120000"
        assert files["shortcut"].content == b"src/code.txt"
        assert root.joinpath("second-shortcut").read_bytes() == b"baseline\n"
        assert root.joinpath("dangling").is_symlink()
        assert not root.joinpath("dangling").exists()
        assert not {"staged.txt", "ignored-output.bin", "untracked.txt"} & files.keys()
        assert record.task_baseline == record.start_commit == baseline
        assert record.root_identity == (root.stat().st_dev, root.stat().st_ino)
        assert _git(root, "rev-parse", "HEAD").decode().strip() == baseline
        _git(root, "diff-index", "--cached", "--quiet", baseline)
        remove_workspace(lease, record)
        assert not root.exists()
        assert not Path(record.git_directory).exists()
    assert _source_state(repo) == before
    assert not marker.exists()


def _lock_child(repo, cwd):
    program = (
        "import sys\nfrom pathlib import Path\n"
        "from multi_agent.cli.workspaces import executor_lock\n"
        "try:\n"
        "    with executor_lock(Path(sys.argv[1])):\n"
        "        print('acquired', flush=True)\n"
        "except ValueError:\n"
        "    print('blocked', flush=True)\n"
        "    raise SystemExit(3)\n"
    )
    return subprocess.run(
        [sys.executable, "-c", program, str(repo)],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )


def test_single_executor_serializes(workspace_repo, tmp_path):
    repo, baseline = workspace_repo
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "--detach", str(linked), baseline)
    first_data, second_data = tmp_path / "first-data", tmp_path / "second-data"
    first_data.mkdir()
    second_data.mkdir()
    with executor_lock(repo) as lease:
        lock_path = lease.repository / "multi-agent-executor.lock"
        identity = lock_path.stat().st_ino
        for source, data_dir in ((repo, first_data), (linked, second_data)):
            child = _lock_child(source, data_dir)
            assert child.returncode == 3, child.stderr
            assert child.stdout.strip() == "blocked"
            assert lease.active
    assert not lease.active
    assert lock_path.stat().st_ino == identity
    for source, data_dir in ((repo, second_data), (linked, first_data)):
        child = _lock_child(source, data_dir)
        assert child.returncode == 0, child.stderr
        assert child.stdout.strip() == "acquired"
    assert lock_path.stat().st_ino == identity


def test_lock_symlink_is_rejected_without_touching_target(workspace_repo, tmp_path):
    repo, _ = workspace_repo
    external = tmp_path / "external"
    external.write_text("keep")
    lock = repo / ".git/multi-agent-executor.lock"
    lock.symlink_to(external)
    with pytest.raises(ValueError), executor_lock(repo):
        pass
    assert external.read_text() == "keep"
    assert lock.is_symlink()


def test_replaced_lock_invalidates_the_active_lease(workspace_repo, tmp_path):
    repo, baseline = workspace_repo
    with executor_lock(repo) as lease:
        lock = lease.repository / "multi-agent-executor.lock"
        lock.rename(tmp_path / "held-lock")
        lock.touch()
        with pytest.raises(ValueError, match="lock"):
            create_workspace(lease, baseline, tmp_path / "rejected")
        assert not (tmp_path / "rejected").exists()


def test_executor_lock_preserves_body_exceptions(workspace_repo):
    repo, _ = workspace_repo
    original = OSError("failure from the caller body")
    with pytest.raises(OSError) as caught, executor_lock(repo) as lease:
        raise original
    assert caught.value is original
    assert not lease.active
    with executor_lock(repo) as reacquired:
        assert reacquired.active


def test_expired_lease_rejects_operations_but_a_new_owner_can_recover(
    workspace_repo, tmp_path
):
    repo, baseline = workspace_repo
    with executor_lock(repo) as lease:
        record = create_workspace(lease, baseline, tmp_path / "implementation")
    for operation in (
        lambda: inspect_workspace(lease, record),
        lambda: remove_workspace(lease, record),
        lambda: create_workspace(lease, baseline, tmp_path / "another"),
    ):
        with pytest.raises(ValueError, match="active"):
            operation()
    assert Path(record.root).is_dir()
    with executor_lock(repo) as recovered:
        assert inspect_workspace(recovered, record).files
        remove_workspace(recovered, record)


def test_destination_excludes_every_linked_checkout_and_resolved_parent(
    workspace_repo, tmp_path
):
    repo, baseline = workspace_repo
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "--detach", str(linked), baseline)
    alias = tmp_path / "source-alias"
    alias.symlink_to(repo, target_is_directory=True)
    with executor_lock(linked) as lease:
        for destination in (
            repo / "new",
            linked / "new",
            repo / ".git/new",
            alias / "new",
        ):
            with pytest.raises(ValueError):
                create_workspace(lease, baseline, destination)
            assert not destination.exists()


def test_workspace_cleanup_checks_ownership(workspace_repo, tmp_path):
    repo, baseline = workspace_repo
    unknown = tmp_path / "unknown"
    unknown.mkdir()
    (unknown / "keep").write_text("unowned")
    with executor_lock(repo) as lease:
        record = create_workspace(lease, baseline, tmp_path / "implementation")
        with pytest.raises(ValueError):
            remove_workspace(lease, replace(record, root=str(unknown)))
        assert (unknown / "keep").read_text() == "unowned"
        assert Path(record.root).is_dir()
        remove_workspace(lease, record)
    assert (unknown / "keep").exists()


@pytest.mark.parametrize(
    "control_name", ("HEAD", "commondir", "gitdir", "config.worktree")
)
def test_admin_control_changes_prevent_inspection_and_cleanup(
    workspace_repo, tmp_path, control_name
):
    repo, baseline = workspace_repo
    with executor_lock(repo) as lease:
        record = create_workspace(lease, baseline, tmp_path / "implementation")
        control = Path(record.git_directory) / control_name
        old = control.read_bytes() if control.exists() else None
        control.write_bytes(b"modified control\n")
        with pytest.raises(ValueError):
            inspect_workspace(lease, record)
        with pytest.raises(ValueError):
            remove_workspace(lease, record)
        assert Path(record.root).is_dir()
        if old is None:
            control.unlink()
        else:
            control.write_bytes(old)
        remove_workspace(lease, record)


@pytest.mark.parametrize("replacement", ("symlink", "directory"))
def test_git_control_type_replacement_is_rejected(
    workspace_repo, tmp_path, replacement
):
    repo, baseline = workspace_repo
    with executor_lock(repo) as lease:
        record = create_workspace(lease, baseline, tmp_path / "implementation")
        control = Path(record.root) / ".git"
        original = tmp_path / "original-control"
        control.rename(original)
        if replacement == "symlink":
            control.symlink_to(original)
        else:
            control.mkdir()
        with pytest.raises(ValueError):
            inspect_workspace(lease, record)
        with pytest.raises(ValueError):
            remove_workspace(lease, record)
        assert original.is_file()
        if replacement == "symlink":
            control.unlink()
        else:
            control.rmdir()
        original.rename(control)
        remove_workspace(lease, record)


def test_replaced_root_is_preserved_instead_of_cleaned(workspace_repo, tmp_path):
    repo, baseline = workspace_repo
    with executor_lock(repo) as lease:
        record = create_workspace(lease, baseline, tmp_path / "implementation")
        root = Path(record.root)
        saved = tmp_path / "saved-owned-root"
        root.rename(saved)
        root.mkdir()
        (root / "keep").write_text("replacement")
        shutil.copyfile(saved / ".git", root / ".git")
        for operation in (inspect_workspace, remove_workspace):
            with pytest.raises(ValueError, match="identity"):
                operation(lease, record)
        assert (root / "keep").read_text() == "replacement"
        assert saved.is_dir()
        shutil.rmtree(root)
        saved.rename(root)
        remove_workspace(lease, record)


def _tree_commit(repo, entries):
    records = []
    for name, mode, content in entries:
        oid = (
            _git(
                repo,
                "hash-object",
                "-w",
                "--no-filters",
                "--stdin",
                input_bytes=content,
            )
            .decode()
            .strip()
        )
        records.append(f"{mode} blob {oid}\t{name}".encode() + b"\0")
    tree = _git(repo, "mktree", "-z", input_bytes=b"".join(records)).decode().strip()
    return _git(repo, "commit-tree", tree, "-m", "synthetic tree").decode().strip()


@pytest.mark.parametrize(
    "entries",
    [
        (("outside", "120000", b"../outside"),),
        (("outside", "120000", b"/tmp/outside"),),
        (("a", "120000", b"b"), ("b", "120000", b"a")),
        (("a", "120000", b"."), ("b", "120000", b"a/../outside")),
        (("git-alias", "120000", b".git/config"),),
        (("git-alias", "120000", b".GIT/config"),),
        (("Case.txt", "100644", b"one"), ("case.txt", "100644", b"two")),
        (("café", "100644", b"one"), ("cafe\u0301", "100644", b"two")),
    ],
)
def test_unsafe_baseline_symlink_graphs_and_alias_collisions_are_rejected(
    workspace_repo, tmp_path, entries
):
    repo, _ = workspace_repo
    baseline = _tree_commit(repo, entries)
    before = _git(repo, "worktree", "list", "--porcelain", "-z")
    with executor_lock(repo) as lease, pytest.raises(ValueError):
        create_workspace(lease, baseline, tmp_path / "implementation")
    assert not (tmp_path / "implementation").exists()
    assert _git(repo, "worktree", "list", "--porcelain", "-z") == before


@pytest.mark.parametrize("git_name", (".git", ".GIT"))
def test_nested_git_control_directories_are_rejected(
    workspace_repo, tmp_path, git_name
):
    repo, baseline = workspace_repo
    with executor_lock(repo) as lease:
        record = create_workspace(lease, baseline, tmp_path / "implementation")
        nested = Path(record.root) / "nested"
        nested.mkdir()
        control = nested / git_name
        control.mkdir()
        with pytest.raises(ValueError, match="unsafe repository file path"):
            inspect_workspace(lease, record)
        assert control.is_dir()
        control.rmdir()
        nested.rmdir()
        remove_workspace(lease, record)


def test_gitlink_baseline_is_not_silently_dropped(workspace_repo, tmp_path):
    repo, baseline = workspace_repo
    tree = (
        _git(
            repo,
            "mktree",
            "-z",
            input_bytes=f"160000 commit {baseline}\tsubmodule\0".encode(),
        )
        .decode()
        .strip()
    )
    baseline = _git(repo, "commit-tree", tree, "-m", "gitlink").decode().strip()
    with executor_lock(repo) as lease, pytest.raises(ValueError, match="gitlink"):
        create_workspace(lease, baseline, tmp_path / "implementation")
    assert not (tmp_path / "implementation").exists()


@pytest.mark.parametrize("cleanup_fails", (False, True))
def test_partial_workspace_failure_is_traceable(
    workspace_repo, tmp_path, monkeypatch, cleanup_fails
):
    repo, baseline = workspace_repo
    destination = tmp_path / "implementation"
    retained = []
    original_remove = remove_workspace

    def fail_materialization(root, files):
        assert files
        (root / "partial-output.txt").write_text("owned partial output")
        raise OSError("injected materialization failure")

    def fail_cleanup(lease, record):
        retained.append(record)
        raise ValueError("injected cleanup failure")

    before = _git(repo, "worktree", "list", "--porcelain", "-z")
    with executor_lock(repo) as lease:
        with monkeypatch.context() as patch:
            patch.setattr(workspaces, "_materialize", fail_materialization)
            if cleanup_fails:
                patch.setattr(workspaces, "remove_workspace", fail_cleanup)
            with pytest.raises(
                ValueError, match="injected materialization failure"
            ) as caught:
                create_workspace(lease, baseline, destination)
        assert isinstance(caught.value.__cause__, OSError)
        if cleanup_fails:
            assert len(retained) == 1
            assert (
                destination / "partial-output.txt"
            ).read_text() == "owned partial output"
            notes = getattr(caught.value, "__notes__", ())
            assert any(
                "injected cleanup failure" in note and str(destination) in note
                for note in notes
            )
            original_remove(lease, retained[0])
        else:
            assert not destination.exists()
        assert _git(repo, "worktree", "list", "--porcelain", "-z") == before


@pytest.mark.parametrize("name", ("MAX_FILES", "MAX_FILE_BYTES", "MAX_SNAPSHOT_BYTES"))
def test_baseline_limits_reject_before_creating_a_workspace(
    workspace_repo, tmp_path, monkeypatch, name
):
    repo, baseline = workspace_repo
    before = _git(repo, "worktree", "list", "--porcelain", "-z")
    monkeypatch.setattr(workspaces.snapshot, name, 1)
    with executor_lock(repo) as lease, pytest.raises(ValueError, match="limit"):
        create_workspace(lease, baseline, tmp_path / "implementation")
    assert not (tmp_path / "implementation").exists()
    assert _git(repo, "worktree", "list", "--porcelain", "-z") == before


@pytest.mark.parametrize("limit", ("file", "count", "total"))
def test_inspection_limits_include_new_untracked_files(
    workspace_repo, tmp_path, monkeypatch, limit
):
    repo, baseline = workspace_repo
    with executor_lock(repo) as lease:
        record = create_workspace(lease, baseline, tmp_path / "implementation")
        initial = inspect_workspace(lease, record)
        root = Path(record.root)
        with monkeypatch.context() as patch:
            if limit == "file":
                patch.setattr(workspaces.snapshot, "MAX_FILE_BYTES", 512)
                (root / "untracked").write_bytes(b"x" * 513)
            elif limit == "count":
                patch.setattr(workspaces.snapshot, "MAX_FILES", len(initial.files))
                (root / "untracked").write_bytes(b"x")
            else:
                patch.setattr(
                    workspaces.snapshot,
                    "MAX_SNAPSHOT_BYTES",
                    sum(len(item.content) for item in initial.files) + 4,
                )
                (root / "untracked").write_bytes(b"12345")
            with pytest.raises(ValueError, match="limit"):
                inspect_workspace(lease, record)
        remove_workspace(lease, record)


def test_file_replaced_by_symlink_during_scan_is_never_followed(
    workspace_repo, tmp_path, monkeypatch
):
    repo, baseline = workspace_repo
    external = tmp_path / "external-secret"
    external.write_bytes(b"must not be read as candidate content")
    real_open = os.open
    swapped = []
    with executor_lock(repo) as lease:
        record = create_workspace(lease, baseline, tmp_path / "implementation")
        victim = Path(record.root) / "src/code.txt"

        def replace_before_open(path, flags, *args, **kwargs):
            if path == "code.txt" and kwargs.get("dir_fd") is not None and not swapped:
                assert flags & os.O_NOFOLLOW
                victim.unlink()
                victim.symlink_to(external)
                swapped.append(True)
            return real_open(path, flags, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(workspaces.os, "open", replace_before_open)
            with pytest.raises(ValueError):
                inspect_workspace(lease, record)
        assert swapped == [True]
        assert external.read_bytes() == b"must not be read as candidate content"
        victim.unlink()
        victim.write_bytes(b"baseline\n")
        remove_workspace(lease, record)


@pytest.mark.parametrize("kind", ("fifo", "socket"))
def test_special_files_are_rejected_without_blocking(workspace_repo, tmp_path, kind):
    repo, baseline = workspace_repo
    # Unix socket addresses have a small platform limit. Keep this owned
    # workspace under a short temporary prefix rather than the long pytest path.
    with TemporaryDirectory(prefix="ma-special-", dir="/tmp") as temporary:
        destination = Path(temporary) / "tree"
        with executor_lock(repo) as lease:
            record = create_workspace(lease, baseline, destination)
        special = destination / "special"
        bound_socket = None
        if kind == "fifo":
            os.mkfifo(special)
        else:
            bound_socket = socket.socket(socket.AF_UNIX)
            bound_socket.bind(str(special))
        payload = tmp_path / "trusted-owned-record.pickle"
        payload.write_bytes(pickle.dumps(record))
        program = (
            "import pathlib, pickle, sys\n"
            "from multi_agent.cli.workspaces import executor_lock, inspect_workspace\n"
            "record = pickle.loads(pathlib.Path(sys.argv[2]).read_bytes())\n"
            "with executor_lock(pathlib.Path(sys.argv[1])) as lease:\n"
            "    try:\n"
            "        inspect_workspace(lease, record)\n"
            "    except ValueError:\n"
            "        print('rejected', flush=True)\n"
            "    else:\n"
            "        raise SystemExit('special file was accepted')\n"
        )
        try:
            child = subprocess.run(
                [sys.executable, "-c", program, str(repo), str(payload)],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            assert child.returncode == 0, child.stderr
            assert child.stdout.strip() == "rejected"
        finally:
            if bound_socket is not None:
                bound_socket.close()
            special.unlink()
            with executor_lock(repo) as lease:
                remove_workspace(lease, record)
