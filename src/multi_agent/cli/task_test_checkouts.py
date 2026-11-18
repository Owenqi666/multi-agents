"""Materialize one verified candidate into a private, shallow Git repository."""

import os
import stat
from pathlib import Path

from multi_agent.core.integration_testing import TestCheckoutLayout
from multi_agent.core.task_execution import task_execution_hash
from multi_agent.core.task_test_execution import TaskTestCheckout
from multi_agent.core.task_test_input import TaskTestInput
from multi_agent.core.workspaces import WorkspaceFile, WorkspaceInspection
from multi_agent.store.task_test_store import TaskTestStore

from . import snapshot
from .task_candidates import _capture, _objects, _revision, inspect_task_candidate
from .task_context import (
    _canonical,
    _check_lease,
    _directory,
    _identity,
    _store_database,
    _sync_directory,
)
from .task_test_inputs import prepare_task_test_input
from .workspaces import (
    ExecutorLease,
    _blob_id,
    _control,
    _git,
    _inspection,
    _limits,
    _materialize,
)

_CONTROLS = ("HEAD", "config", "index", "shallow", "multi-agent-owner")


def _closure(
    repository: Path, candidate: str, object_format: str
) -> tuple[tuple[str, str], ...]:
    tree, _, files = _revision(repository, candidate, object_format)
    trees = {tree}
    for row in _git(repository, "ls-tree", "-r", "-t", "-z", tree).split(b"\0"):
        if row:
            meta, _ = row.split(b"\t", 1)
            _, kind, oid = meta.decode("ascii").split()
            if kind == "tree":
                trees.add(oid)
    return (
        (("commit", candidate),)
        + tuple(("tree", oid) for oid in sorted(trees))
        + tuple(("blob", oid) for oid in sorted({item.oid for item in files}))
    )


def _git_layout(record: TaskTestCheckout | TestCheckoutLayout) -> None:
    """Reject Git routing or executable metadata before invoking Git."""
    root = Path(record.git_directory)
    expected_files = set(_CONTROLS) | {
        f"objects/{oid[:2]}/{oid[2:]}" for _, oid in record.objects
    }
    expected_dirs = {
        "objects",
        "objects/info",
        "objects/pack",
        "refs",
        "refs/heads",
        "refs/tags",
        *(f"objects/{oid[:2]}" for _, oid in record.objects),
    }
    files, directories = set(), set()
    for directory, names, members in os.walk(root, followlinks=False):
        for name in names:
            path = Path(directory) / name
            if not stat.S_ISDIR(path.lstat().st_mode):
                raise ValueError("test Git metadata contains a symlink directory")
            directories.add(path.relative_to(root).as_posix())
        for name in members:
            path = Path(directory) / name
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("test Git metadata is not private regular storage")
            files.add(path.relative_to(root).as_posix())
        if len(files) + len(directories) > snapshot.MAX_FILES * 5 + 20:
            raise ValueError("test Git metadata exceeds its entry budget")
    if files != expected_files or directories != expected_dirs:
        raise ValueError("test Git metadata members changed")


def inspect_task_test_checkout_controls(
    lease: ExecutorLease, record: TaskTestCheckout | TestCheckoutLayout
) -> None:
    """Inspect fixed controls after a stopped command; generated files are allowed."""
    _check_lease(lease)
    if str(lease.repository) != record.repository:
        raise ValueError("test checkout belongs to another executor lease")
    root, git = _canonical(record.root), _canonical(record.git_directory)
    if git != root / ".git":
        raise ValueError("test checkout requires its private Git directory")
    _directory(root, identity=record.root_identity)
    _directory(git, identity=record.git_identity)
    _git_layout(record)
    expected_paths = tuple(str(git / name) for name in _CONTROLS)
    if tuple(item.path for item in record.controls) != expected_paths:
        raise ValueError("test Git control manifest differs")
    if tuple(_control(Path(item.path)) for item in record.controls) != record.controls:
        raise ValueError("test Git control identity or bytes changed")
    for kind in ("commit", "tree", "blob"):
        ids = tuple(oid for actual_kind, oid in record.objects if actual_kind == kind)
        _objects(root, ids, kind, record.object_format)
    _check_lease(lease)


def _files(record: TaskTestCheckout | TestCheckoutLayout) -> tuple[WorkspaceFile, ...]:
    files: list[WorkspaceFile] = []
    visited = total = 0

    def scan(directory: int, prefix: str) -> None:
        nonlocal visited, total
        if prefix.count("/") > 256:
            raise ValueError("test checkout exceeds its directory depth budget")
        with os.scandir(directory) as entries:
            names = sorted(entry.name for entry in entries)
        visited += len(names)
        if visited > snapshot.MAX_FILES * 4:
            raise ValueError("test checkout exceeds its entry budget")
        for name in names:
            if not prefix and name == ".git":
                continue
            path = snapshot._path(prefix + name)
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory,
                )
                try:
                    if _identity(os.fstat(child)) != _identity(info):
                        raise ValueError("test checkout directory changed")
                    scan(child, path + "/")
                finally:
                    os.close(child)
                continue
            if stat.S_ISLNK(info.st_mode):
                content = os.readlink(name, dir_fd=directory).encode("utf-8")
                mode = "120000"
            elif stat.S_ISREG(info.st_mode):
                fd = os.open(
                    name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=directory,
                )
                try:
                    opened = os.fstat(fd)
                    if not stat.S_ISREG(opened.st_mode) or _identity(
                        opened
                    ) != _identity(info):
                        raise ValueError("test checkout file changed")
                    with os.fdopen(os.dup(fd), "rb") as stream:
                        content = stream.read(snapshot.MAX_FILE_BYTES + 1)
                    mode = "100755" if opened.st_mode & 0o111 else "100644"
                finally:
                    os.close(fd)
            else:
                raise ValueError("test checkout contains a special file")
            total += len(content)
            if (
                len(content) > snapshot.MAX_FILE_BYTES
                or total > snapshot.MAX_SNAPSHOT_BYTES
            ):
                raise ValueError("test checkout exceeds its byte budget")
            files.append(
                WorkspaceFile(
                    path, mode, _blob_id(content, record.object_format), content
                )
            )

    fd = os.open(record.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if _identity(os.fstat(fd)) != record.root_identity:
            raise ValueError("test checkout root changed")
        scan(fd, "")
    finally:
        os.close(fd)
    value = tuple(sorted(files, key=lambda item: item.path))
    _limits(value)
    return value


def inspect_initial_task_test_checkout(
    lease: ExecutorLease, record: TaskTestCheckout | TestCheckoutLayout
) -> WorkspaceInspection:
    inspect_task_test_checkout_controls(lease, record)
    tree, _, expected = _revision(
        Path(record.root),
        record.commit,
        record.object_format,
    )
    index = b"".join(
        f"{item.mode} {item.oid} 0\t{item.path}".encode() + b"\0" for item in expected
    )
    if _git(Path(record.root), "ls-files", "--stage", "-z") != index:
        raise ValueError("test checkout index differs from candidate files")
    actual = _inspection(_files(record))
    if (
        tree != record.tree
        or actual.files != expected
        or actual.digest != record.initial_digest
    ):
        raise ValueError("test checkout is not the exact initial candidate")
    inspect_task_test_checkout_controls(lease, record)
    return actual


def create_test_checkout_layout(
    lease: ExecutorLease,
    *,
    candidate: str,
    expected_tree: str,
    object_format: str,
    root: Path,
    owner: str,
) -> TestCheckoutLayout:
    """Copy and verify only the pinned candidate's bytes and Git objects."""
    _check_lease(lease)
    tree, _, files = _revision(lease.repository, candidate, object_format)
    if tree != expected_tree:
        raise ValueError("test candidate tree changed")
    objects = _closure(lease.repository, candidate, object_format)
    root.mkdir(mode=0o700)
    _git(root, "init", "--quiet", "--template=", f"--object-format={object_format}")
    git = root / ".git"
    git.chmod(0o700)
    config = (
        "[core]\n\trepositoryformatversion = "
        + ("1" if object_format == "sha256" else "0")
        + "\n\tbare = false\n\tfilemode = true\n\tlogallrefupdates = false\n"
    )
    if object_format == "sha256":
        config += "[extensions]\n\tobjectformat = sha256\n"
    (git / "config").write_text(config)
    for kind in ("commit", "tree", "blob"):
        ids = tuple(oid for item_kind, oid in objects if item_kind == kind)
        for oid, body in zip(
            ids, _objects(lease.repository, ids, kind, object_format), strict=True
        ):
            actual = (
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
                .decode("ascii")
                .strip()
            )
            if actual != oid:
                raise ValueError("test object copy changed its content address")
    (git / "HEAD").write_text(candidate + "\n")
    (git / "shallow").write_text(candidate + "\n")
    (git / "multi-agent-owner").write_text(owner + "\n")
    _materialize(root, files)
    _git(root, "read-tree", tree)
    record = TestCheckoutLayout(
        repository=str(lease.repository),
        root=str(root),
        root_identity=_identity(_directory(root)),
        git_directory=str(git),
        git_identity=_identity(_directory(git)),
        owner_token=owner,
        object_format=object_format,
        initial_digest=_inspection(files).digest,
        controls=tuple(_control(git / name) for name in _CONTROLS),
        objects=objects,
        commit=candidate,
        tree=tree,
    )
    inspect_initial_task_test_checkout(lease, record)
    _sync_directory(root)
    return record


def create_task_test_checkout(
    store: TaskTestStore, value: TaskTestInput, lease: ExecutorLease, destination: Path
) -> TaskTestCheckout:
    from .task_test_context import _test_root_owner

    guard = _capture(
        store, value.task_run_id, value.implementation_attempt, lease, bound=True
    )
    declared = prepare_task_test_input(
        store,
        value.task_run_id,
        value.implementation_attempt,
        lease,
        required_commands=tuple(command.name for command in value.commands),
        declaration_source=value.declaration_source,
    )
    if task_execution_hash(declared) != task_execution_hash(value):
        raise ValueError(
            "test checkout input differs from the frozen candidate declaration"
        )
    database = _store_database(store)
    root = _canonical(destination, existing=False)
    if (
        root.name != "checkout"
        or root.exists()
        or root.is_symlink()
        or root.parent.parent != database.path.parent
    ):
        raise ValueError("test checkout must be a new owned root/checkout")
    owner = _test_root_owner(store, lease, root.parent)
    checkpoint, history = store.load_task_test_checkpoint(
        value.task_run_id, value.implementation_attempt
    )
    observed = (
        checkpoint.current_attempt.number
        if checkpoint is not None and checkpoint.current_attempt is not None
        else None
    )
    if value.task_history_length != len(
        guard.history
    ) or value.task_history_hash != task_execution_hash(guard.history):
        raise ValueError("test input original task history changed")
    if (
        inspect_task_candidate(
            store, value.task_run_id, value.implementation_attempt, lease
        )
        != value.candidate
    ):
        raise ValueError("test input candidate changed")
    layout = create_test_checkout_layout(
        lease,
        candidate=value.candidate.delivery.candidate,
        expected_tree=value.candidate.delivery.tree,
        object_format=guard.prepared.workspace.object_format,
        root=root,
        owner=owner,
    )
    record = TaskTestCheckout(
        input=value,
        repository=layout.repository,
        database_path=str(database.path),
        database_identity=database.identity,
        database_parent_identity=database.parent_identity,
        root=layout.root,
        root_identity=layout.root_identity,
        git_directory=layout.git_directory,
        git_identity=layout.git_identity,
        owner_token=layout.owner_token,
        object_format=layout.object_format,
        initial_digest=layout.initial_digest,
        controls=layout.controls,
        objects=layout.objects,
        observed_test_attempt=observed,
        test_history_length=len(history),
        test_history_hash=task_execution_hash(history),
    )
    if store.load_task_test_checkpoint(
        value.task_run_id, value.implementation_attempt
    ) != (checkpoint, history):
        raise ValueError("test stage changed during checkout creation")
    if (
        inspect_task_candidate(
            store, value.task_run_id, value.implementation_attempt, lease
        )
        != value.candidate
    ):
        raise ValueError("test candidate changed during checkout creation")
    guard.check()
    return record
