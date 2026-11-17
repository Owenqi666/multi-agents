"""One real standalone checkout proves the feature's essential isolation layout."""

import hashlib
import os
import stat
from pathlib import Path

import pytest
import test_task_test_context as context_helpers
import test_task_test_runner as feature_helpers

from multi_agent.cli.task_test_checkouts import (
    _closure,
    _objects,
    inspect_initial_task_test_checkout,
)
from multi_agent.cli.workspaces import _git

feature = feature_helpers.feature


def test_task_test_checkout_preserves_candidate_raw_bytes_and_independence(feature):
    with context_helpers.launch(feature) as handle:
        record = handle.prepared.checkout
        root = Path(record.root)
        git_directory = root / ".git"
        assert git_directory.is_dir() and not git_directory.is_symlink()
        assert Path(record.git_directory) == git_directory
        assert (root / "src/code.py").read_bytes() == b"CANDIDATE = True\r\n"
        assert (root / "src/data.bin").read_bytes() == b"\x00\xffcandidate bytes\r\n"
        assert stat.S_IMODE((root / "src/build.py").stat().st_mode) == 0o755
        assert (root / "src/internal-link").is_symlink()
        assert os.readlink(root / "src/internal-link") == "code.py"
        assert (git_directory / "shallow").read_bytes() == (
            feature.candidate.delivery.candidate + "\n"
        ).encode()

        def git(repository, *arguments):
            return _git(repository, *arguments).decode("utf-8").strip()

        assert git(root, "rev-parse", "HEAD") == feature.candidate.delivery.candidate
        assert git(root, "rev-parse", "--is-shallow-repository") == "true"
        assert git(root, "remote") == ""
        assert git(root, "tag") == ""
        assert git(root, "status", "--porcelain") == ""
        with pytest.raises(ValueError):
            _git(root, "cat-file", "-e", feature.baseline)
        for forbidden in ("commondir", "objects/info/alternates"):
            assert not (git_directory / forbidden).exists()
        if (git_directory / "hooks").exists():
            assert not any((git_directory / "hooks").glob("*"))
        inspection = inspect_initial_task_test_checkout(handle.lease, record)
        assert inspection.digest == record.initial_digest
        for item in inspection.files:
            if item.mode == "120000":
                continue
            source = feature.workspace / item.path
            assert source.read_bytes() == (root / item.path).read_bytes()
            assert (source.stat().st_dev, source.stat().st_ino) != (
                (root / item.path).stat().st_dev,
                (root / item.path).stat().st_ino,
            )
        tree_entries = git(
            feature.repo, "ls-tree", "-r", "-z", feature.candidate.delivery.candidate
        ).split("\0")
        expected_index = []
        for entry in tree_entries:
            if not entry:
                continue
            header, name = entry.split("\t", 1)
            mode, kind, oid = header.split(" ")
            assert kind == "blob"
            expected_index.append(f"{mode} {oid} 0\t{name}")
        actual_index = git(root, "ls-files", "--stage", "-z").split("\0")
        assert tuple(item for item in actual_index if item) == tuple(expected_index)
        assert not feature.capture.exists()


def test_task_test_pristine_inspector_rejects_changed_initial_bytes(feature):
    with context_helpers.launch(feature) as handle:
        root = Path(handle.prepared.checkout.root)
        (root / "src/code.py").write_bytes(b"changed after candidate materialization")
        with pytest.raises(ValueError):
            inspect_initial_task_test_checkout(handle.lease, handle.prepared.checkout)


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_task_test_object_closure_keeps_non_utf8_commit_and_empty_tree(
    tmp_path, object_format
):
    """SHA256 is checked at the real object layer, not the planning pipeline."""
    source, target = tmp_path / "source", tmp_path / "independent"
    for root in (source, target):
        root.mkdir()
        _git(root, "init", "--quiet", "--template=", f"--object-format={object_format}")

    def write_object(root, kind, body):
        oid = (
            _git(
                root,
                "hash-object",
                "-w",
                "--no-filters",
                "-t",
                kind,
                "--stdin",
                input_bytes=body,
            )
            .decode()
            .strip()
        )
        expected = hashlib.new(
            object_format, f"{kind} {len(body)}\0".encode() + body
        ).hexdigest()
        assert oid == expected
        return oid

    blob_body = b"\x00\xffbinary\r\n"
    blob = write_object(source, "blob", blob_body)
    empty = write_object(source, "tree", b"")
    tree_body = (
        b"40000 empty\0"
        + bytes.fromhex(empty)
        + b"100644 file.bin\0"
        + bytes.fromhex(blob)
    )
    tree = write_object(source, "tree", tree_body)
    identity = b"author Synthetic Fixture <fixture@example.invalid> 1 +0000\ncommitter Synthetic Fixture <fixture@example.invalid> 1 +0000\n"
    parent = write_object(
        source,
        "commit",
        f"tree {tree}\n".encode() + identity + b"\nparent outside closure\n",
    )
    candidate_body = (
        f"tree {tree}\nparent {parent}\n".encode()
        + identity
        + b"\nnon UTF8 message \xff\xfe\n"
    )
    candidate = write_object(source, "commit", candidate_body)
    closure = _closure(source, candidate, object_format)
    assert set(closure) == {
        ("commit", candidate),
        ("tree", tree),
        ("tree", empty),
        ("blob", blob),
    }
    assert ("commit", parent) not in closure
    bodies = {
        ("commit", candidate): candidate_body,
        ("tree", tree): tree_body,
        ("tree", empty): b"",
        ("blob", blob): blob_body,
    }
    for kind, oid in closure:
        (raw,) = _objects(source, (oid,), kind, object_format)
        assert raw == bodies[(kind, oid)]
        assert write_object(target, kind, raw) == oid
        (copied,) = _objects(target, (oid,), kind, object_format)
        assert copied == raw
        old = source / ".git/objects" / oid[:2] / oid[2:]
        new = target / ".git/objects" / oid[:2] / oid[2:]
        assert (old.stat().st_dev, old.stat().st_ino) != (
            new.stat().st_dev,
            new.stat().st_ino,
        )
    with pytest.raises(ValueError):
        _git(target, "cat-file", "-e", parent)
