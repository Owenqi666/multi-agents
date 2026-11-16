"""Owned implementation workspaces and scope-checked, immutable Git candidates.

This is the filesystem/Git boundary, not a sandbox for hostile host processes.
The caller must keep the lease for the executor's entire lifetime and establish
that it has stopped before freezing or removing its workspace.
"""

import fcntl
import hashlib
import json
import os
import stat
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from uuid import uuid4

from multi_agent.cli import snapshot
from multi_agent.core.events import FailureCategory
from multi_agent.core.path_patterns import normalize_path, path_matches
from multi_agent.core.tasks import FrozenProjectRules, TaskCard
from multi_agent.core.workspaces import (
    WorkspaceControl,
    WorkspaceDelivery,
    WorkspaceFile,
    WorkspaceInspection,
    WorkspaceRecord,
)


class DeliveryRejected(ValueError):
    category = FailureCategory.OUT_OF_SCOPE_PATCH

    def __init__(self, paths: tuple[str, ...]) -> None:
        self.paths = paths
        super().__init__(f"delivery changes forbidden paths: {paths!r}")


@dataclass(slots=True)
class ExecutorLease:
    source: Path
    repository: Path
    fd: int
    active: bool = True


def _boundary[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    @wraps(function)
    def invoke(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return function(*args, **kwargs)
        except (OSError, UnicodeError) as error:
            failure = ValueError(f"workspace filesystem operation failed: {error}")
            for note in getattr(error, "__notes__", ()):
                failure.add_note(note)
            raise failure from error

    return invoke


def _git(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    overrides = (
        "core.fsmonitor=false",
        "core.sparseCheckout=false",
        "core.sparseCheckoutCone=false",
        "index.sparse=false",
        "commit.gpgSign=false",
        "gc.auto=0",
        "maintenance.auto=false",
        "submodule.recurse=false",
        "user.name=Multi-agent",
        "user.email=multi-agent@localhost",
        "author.name=Multi-agent",
        "author.email=multi-agent@localhost",
        "committer.name=Multi-agent",
        "committer.email=multi-agent@localhost",
    )
    return snapshot._git(
        repo,
        *(part for value in overrides for part in ("-c", value)),
        *args,
        input_bytes=input_bytes,
    )


def _git_path(repo: Path, argument: str) -> Path:
    raw = _git(repo, "rev-parse", "--path-format=absolute", argument)
    path = Path(raw.decode("utf-8").removesuffix("\n"))
    if "\n" in str(path) or "\r" in str(path):
        raise ValueError("Git directory contains a line ending")
    return path.resolve(strict=True)


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _check_lease(lease: ExecutorLease) -> None:
    if not lease.active:
        raise ValueError("executor lease is no longer active")
    held = os.fstat(lease.fd)
    current = (lease.repository / "multi-agent-executor.lock").lstat()
    if not stat.S_ISREG(current.st_mode) or _identity(held) != _identity(current):
        raise ValueError("executor lock identity changed")


@contextmanager
def executor_lock(repository: Path) -> Iterator[ExecutorLease]:
    """A cooperative, nonblocking lock shared by all linked worktrees."""
    lease: ExecutorLease | None = None
    fd: int | None = None
    try:
        try:
            source = _git_path(repository.resolve(strict=True), "--show-toplevel")
            common = _git_path(source, "--git-common-dir")
            fd = os.open(
                common / "multi-agent-executor.lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
            )
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("executor lock must be a regular file")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError(
                    "repository already has an implementation executor"
                ) from error
            lease = ExecutorLease(source, common, fd)
            _check_lease(lease)
        except (OSError, UnicodeError) as error:
            raise ValueError(
                f"workspace filesystem operation failed: {error}"
            ) from error
        yield lease
    finally:
        if lease is not None:
            lease.active = False
        if fd is not None:
            os.close(fd)  # Closing releases the lock. Never unlink its stable inode.


def _resolve(repo: Path, revision: str) -> str:
    snapshot._string(revision, "workspace commit", nonempty=True)
    if not snapshot._OID.fullmatch(revision):
        raise ValueError("workspace commits must be full object IDs")
    value = _git(repo, "rev-parse", "--verify", f"{revision}^{{commit}}")
    resolved = value.decode("ascii").strip()
    if not snapshot._OID.fullmatch(resolved):
        raise ValueError("Git returned an invalid commit ID")
    return resolved


def _blob_id(content: bytes, object_format: str) -> str:
    return hashlib.new(
        object_format, f"blob {len(content)}\0".encode("ascii") + content
    ).hexdigest()


def _limits(files: tuple[WorkspaceFile, ...]) -> None:
    if (
        len(files) > snapshot.MAX_FILES
        or sum(len(f.content) for f in files) > snapshot.MAX_SNAPSHOT_BYTES
    ):
        raise ValueError("workspace exceeds the file or byte limit")
    if any(len(f.content) > snapshot.MAX_FILE_BYTES for f in files):
        raise ValueError("workspace file exceeds the byte limit")
    snapshot._check_paths(f.path for f in files)
    # Include directory spellings, not only full file names, in alias checks.
    spellings: dict[str, str] = {}
    for item in files:
        parts = item.path.split("/")
        for length in range(1, len(parts) + 1):
            path = "/".join(parts[:length])
            key = normalize_path(path)
            if key in spellings and spellings[key] != path:
                raise ValueError(f"workspace path aliases collide: {path!r}")
            spellings[key] = path
    _symlinks(files)


def _symlinks(files: tuple[WorkspaceFile, ...]) -> None:
    """Resolve against captured bytes, including symlink prefixes before '..'."""
    by_path = {normalize_path(f.path): f for f in files}
    for item in files:
        if item.mode != "120000":
            continue
        pending = deque(item.path.split("/"))
        resolved: list[str] = []
        expansions = 0
        while pending:
            part = pending.popleft()
            if part in ("", "."):
                continue
            if part == "..":
                if not resolved:
                    raise ValueError(f"symlink escapes workspace: {item.path!r}")
                resolved.pop()
                continue
            snapshot._path(part)
            resolved.append(part)
            target = by_path.get(normalize_path("/".join(resolved)))
            if target is None:
                continue  # Internal dangling targets are allowed.
            if target.mode == "120000":
                expansions += 1
                if expansions > 40:
                    raise ValueError(f"symlink cycle or excessive chain: {item.path!r}")
                text = target.content.decode("utf-8")
                if not text or text.startswith("/") or "\0" in text or "\\" in text:
                    raise ValueError(f"unsafe symlink target: {target.path!r}")
                resolved.pop()
                pending.extendleft(reversed(text.split("/")))
            elif pending:
                raise ValueError(f"symlink traverses a regular file: {item.path!r}")


def _baseline_files(repo: Path, revision: str) -> tuple[WorkspaceFile, ...]:
    records = _git(repo, "ls-tree", "-r", "-z", "-l", "--full-tree", revision)
    entries: list[tuple[str, str, str, int]] = []
    for record in records.split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, kind, oid, length = metadata.decode("ascii").split()
        if kind != "blob" or mode not in ("100644", "100755", "120000"):
            raise ValueError(
                "workspaces do not support gitlinks or special Git entries"
            )
        size = int(length)
        if size > snapshot.MAX_FILE_BYTES:
            raise ValueError("baseline file exceeds the byte limit")
        entries.append((raw_path.decode("utf-8"), mode, oid, size))
    if (
        len(entries) > snapshot.MAX_FILES
        or sum(e[3] for e in entries) > snapshot.MAX_SNAPSHOT_BYTES
    ):
        raise ValueError("baseline exceeds the file or byte limit")
    snapshot._check_paths(e[0] for e in entries)
    raw = (
        _git(
            repo,
            "cat-file",
            "--batch",
            input_bytes="".join(f"{e[2]}\n" for e in entries).encode("ascii"),
        )
        if entries
        else b""
    )
    offset = 0
    files: list[WorkspaceFile] = []
    for path, mode, oid, size in entries:
        end = raw.index(b"\n", offset)
        if raw[offset:end] != f"{oid} blob {size}".encode("ascii"):
            raise ValueError("Git blob response mismatch")
        content = raw[end + 1 : end + 1 + size]
        offset = end + size + 2
        if len(content) != size or raw[offset - 1 : offset] != b"\n":
            raise ValueError("Git blob response incomplete")
        files.append(WorkspaceFile(path, mode, oid, content))
    if offset != len(raw):
        raise ValueError("Git blob response has extra bytes")
    result = tuple(sorted(files, key=lambda f: f.path))
    _limits(result)
    return result


def _read_regular(path: Path) -> tuple[tuple[int, int], bytes]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"control must be a regular file: {path}")
        with os.fdopen(os.dup(fd), "rb") as stream:
            content = stream.read(snapshot.MAX_FILE_BYTES + 1)
        if len(content) > snapshot.MAX_FILE_BYTES:
            raise ValueError("control file exceeds the byte limit")
        return _identity(info), content
    finally:
        os.close(fd)


def _control(path: Path) -> WorkspaceControl:
    try:
        identity, content = _read_regular(path)
    except FileNotFoundError:
        return WorkspaceControl(str(path), None, None)
    return WorkspaceControl(str(path), identity, content)


def _directory_identity(path: Path) -> tuple[int, int]:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        return _identity(os.fstat(fd))
    finally:
        os.close(fd)


def _owned(lease: ExecutorLease, record: WorkspaceRecord) -> None:
    _check_lease(lease)
    if str(lease.repository) != record.repository:
        raise ValueError("workspace belongs to another repository")
    if _directory_identity(Path(record.root)) != record.root_identity:
        raise ValueError("workspace root identity changed")
    admin = Path(record.git_directory)
    if admin.parent != lease.repository / "worktrees" or admin.resolve() != admin:
        raise ValueError("workspace admin directory is not owned")
    if _directory_identity(admin) != record.git_identity:
        raise ValueError("workspace admin identity changed")
    for control in record.controls:
        if _control(Path(control.path)) != control:
            raise ValueError(
                f"workspace control identity/content changed: {control.path}"
            )


def _record(
    lease: ExecutorLease,
    root: Path,
    baseline: str,
    start: str,
    owner: str,
    object_format: str,
) -> WorkspaceRecord:
    control = _control(root / ".git")
    if control.content is None or not control.content.startswith(b"gitdir: "):
        raise ValueError("workspace .git is not a linked-worktree control")
    admin = Path(control.content[8:].decode("utf-8").removesuffix("\n"))
    if (
        not admin.is_absolute()
        or admin.parent != lease.repository / "worktrees"
        or admin.resolve() != admin
    ):
        raise ValueError("workspace Git directory is not owned")
    common = _control(admin / "commondir")
    backlink = _control(admin / "gitdir")
    head = _control(admin / "HEAD")
    if (
        common.content is None
        or (admin / common.content.decode("utf-8").strip()).resolve()
        != lease.repository
    ):
        raise ValueError("workspace common directory mismatch")
    if backlink.content != (str(root / ".git") + "\n").encode(
        "utf-8"
    ) or head.content != (start + "\n").encode("ascii"):
        raise ValueError("workspace Git control pointers mismatch")
    return WorkspaceRecord(
        str(lease.repository),
        str(lease.source),
        baseline,
        start,
        str(root),
        owner,
        _directory_identity(root),
        str(admin),
        _directory_identity(admin),
        (control, common, backlink, head, _control(admin / "config.worktree")),
        object_format,
    )


def _materialize(root: Path, files: tuple[WorkspaceFile, ...]) -> None:
    for item in files:
        target = root / item.path
        target.parent.mkdir(parents=True, exist_ok=True)
        if item.mode == "120000":
            target.symlink_to(item.content.decode("utf-8"))
        else:
            with target.open("xb") as stream:
                stream.write(item.content)
            target.chmod(0o755 if item.mode == "100755" else 0o644)


@_boundary
def create_workspace(
    lease: ExecutorLease,
    task_baseline: str,
    destination: Path,
    *,
    start_commit: str | None = None,
) -> WorkspaceRecord:
    """Materialize raw committed bytes, without checkout filters or source edits."""
    _check_lease(lease)
    baseline = _resolve(lease.repository, task_baseline)
    start = _resolve(
        lease.repository, task_baseline if start_commit is None else start_commit
    )
    _git(lease.repository, "merge-base", "--is-ancestor", baseline, start)
    if destination.is_symlink() or destination.exists():
        raise ValueError("workspace destination must not exist")
    root = destination.parent.resolve(strict=True) / destination.name
    if "\n" in str(root) or "\r" in str(root):
        raise ValueError("workspace path contains a line ending")
    roots = [lease.repository]
    for field in _git(lease.repository, "worktree", "list", "--porcelain", "-z").split(
        b"\0"
    ):
        if field.startswith(b"worktree "):
            roots.append(Path(field[9:].decode("utf-8")).resolve())
    if any(root.is_relative_to(existing) for existing in roots):
        raise ValueError(
            "workspace destination is inside an existing worktree or Git directory"
        )
    files = _baseline_files(lease.repository, start)
    object_format = (
        _git(lease.repository, "rev-parse", "--show-object-format")
        .decode("ascii")
        .strip()
    )
    if object_format not in ("sha1", "sha256"):
        raise ValueError("unsupported Git object format")
    owner = uuid4().hex
    root.mkdir(mode=0o700)
    root_identity = _directory_identity(root)
    record: WorkspaceRecord | None = None
    try:
        _git(
            lease.repository,
            "worktree",
            "add",
            "--detach",
            "--no-checkout",
            "--no-relative-paths",
            str(root),
            start,
        )
        record = _record(lease, root, baseline, start, owner, object_format)
        if record.root_identity != root_identity:
            raise ValueError("workspace root replaced during creation")
        _materialize(root, files)
        _git(
            lease.repository,
            f"--git-dir={record.git_directory}",
            f"--work-tree={root}",
            "read-tree",
            "--no-sparse-checkout",
            start,
        )
        _owned(lease, record)
        return record
    except BaseException as error:
        try:
            if record is not None:
                remove_workspace(lease, record)
            elif _directory_identity(root) == root_identity:
                # Without verified linked metadata, delete only our still-empty dir.
                root.rmdir()
            else:
                raise ValueError("partial workspace root identity changed")
        except (OSError, ValueError) as cleanup_error:
            error.add_note(
                f"workspace cleanup failed; retained {root}: {cleanup_error}"
            )
        raise


def _inspection(files: tuple[WorkspaceFile, ...]) -> WorkspaceInspection:
    manifest = [(f.path, f.mode, hashlib.sha256(f.content).hexdigest()) for f in files]
    digest = hashlib.sha256(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return WorkspaceInspection(files, digest)


@_boundary
def inspect_workspace(
    lease: ExecutorLease, record: WorkspaceRecord
) -> WorkspaceInspection:
    """Capture all final files via directory FDs, never following filesystem links."""
    _owned(lease, record)
    files: list[WorkspaceFile] = []
    total = 0
    visited = 0

    def scan(directory: int, prefix: str) -> None:
        nonlocal total, visited
        if prefix.count("/") > 256:
            raise ValueError("workspace exceeds the directory depth limit")
        with os.scandir(directory) as entries:
            names = []
            for entry in entries:
                visited += 1
                if visited > snapshot.MAX_FILES * 4:
                    raise ValueError("workspace exceeds the scan entry limit")
                names.append(entry.name)
            names.sort()
        for name in names:
            if prefix == "" and name == ".git":
                continue  # _owned verifies this exact regular control file twice.
            path = prefix + name
            snapshot._path(path)
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                )
                try:
                    if _identity(os.fstat(child)) != _identity(info):
                        raise ValueError(f"directory changed during scan: {path}")
                    scan(child, path + "/")
                finally:
                    os.close(child)
                continue
            if stat.S_ISLNK(info.st_mode):
                content = os.readlink(name, dir_fd=directory).encode("utf-8")
                mode = "120000"
            elif stat.S_ISREG(info.st_mode):
                fd = os.open(
                    name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                )
                try:
                    actual = os.fstat(fd)
                    if not stat.S_ISREG(actual.st_mode) or _identity(
                        actual
                    ) != _identity(info):
                        raise ValueError(f"file changed during scan: {path}")
                    with os.fdopen(os.dup(fd), "rb") as stream:
                        content = stream.read(snapshot.MAX_FILE_BYTES + 1)
                    mode = "100755" if actual.st_mode & 0o111 else "100644"
                finally:
                    os.close(fd)
            else:
                raise ValueError(f"workspace contains a special file: {path}")
            total += len(content)
            if (
                len(content) > snapshot.MAX_FILE_BYTES
                or total > snapshot.MAX_SNAPSHOT_BYTES
                or len(files) >= snapshot.MAX_FILES
            ):
                raise ValueError("workspace exceeds the file or byte limit")
            files.append(
                WorkspaceFile(
                    path, mode, _blob_id(content, record.object_format), content
                )
            )

    fd = os.open(record.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if _identity(os.fstat(fd)) != record.root_identity:
            raise ValueError("workspace root changed during scan")
        scan(fd, "")
    finally:
        os.close(fd)
    result = tuple(sorted(files, key=lambda f: f.path))
    _limits(result)
    _owned(lease, record)
    return _inspection(result)


def _write_tree(repo: Path, files: tuple[WorkspaceFile, ...]) -> str:
    directories: dict[str, list[tuple[str, str, str, str]]] = {"": []}
    for item in files:
        oid = (
            _git(
                repo,
                "hash-object",
                "-w",
                "--no-filters",
                "--stdin",
                input_bytes=item.content,
            )
            .decode("ascii")
            .strip()
        )
        if oid != item.oid:
            raise ValueError("written Git blob differs from inspected content")
        parent, _, name = item.path.rpartition("/")
        directories.setdefault(parent, []).append((name, item.mode, "blob", oid))
        while parent:
            parent, _, _ = parent.rpartition("/")
            directories.setdefault(parent, [])
    tree = ""
    for directory in sorted(
        directories, key=lambda p: (p.count("/") + bool(p), p), reverse=True
    ):
        records = b"".join(
            f"{mode} {kind} {oid}\t{name}".encode() + b"\0"
            for name, mode, kind, oid in directories[directory]
        )
        tree = _git(repo, "mktree", "-z", input_bytes=records).decode("ascii").strip()
        if directory:
            parent, _, name = directory.rpartition("/")
            directories[parent].append((name, "040000", "tree", tree))
    return tree


def _candidate_ref(lease: ExecutorLease, record: WorkspaceRecord) -> str:
    # Git's zero-old-OID CAS treats a dangling symbolic ref as missing. Reject
    # existing loose entries explicitly too. The lease serializes our hosts;
    # arbitrary concurrent metadata writers are outside this lock's contract.
    parent = lease.repository
    for component in ("refs", "multi-agent", "candidates"):
        parent = parent / component
        parent.mkdir(exist_ok=True)
        if not stat.S_ISDIR(parent.lstat().st_mode):
            raise ValueError("candidate ref namespace is not a real directory")
    target = parent / record.owner_token
    if target.exists() or target.is_symlink():
        raise ValueError("candidate ref already exists")
    return f"refs/multi-agent/candidates/{record.owner_token}"


@_boundary
def freeze_delivery(
    lease: ExecutorLease,
    record: WorkspaceRecord,
    card: TaskCard,
    frozen_rules: FrozenProjectRules,
) -> WorkspaceDelivery:
    _owned(lease, record)
    if (
        card.baseline_commit != record.task_baseline
        or frozen_rules.baseline_commit != record.task_baseline
        or frozen_rules.repository != record.repository
    ):
        raise ValueError(
            "delivery inputs do not match the original workspace baseline/repository"
        )
    inspection = inspect_workspace(lease, record)
    baseline = {
        f.path: f for f in _baseline_files(lease.repository, record.task_baseline)
    }
    current = {f.path: f for f in inspection.files}
    changed = tuple(
        sorted(
            path
            for path in baseline.keys() | current.keys()
            if baseline.get(path) != current.get(path)
        )
    )
    rejected = tuple(
        path
        for path in changed
        if not any(path_matches(path, pattern) for pattern in card.write_scope)
        or any(
            path_matches(path, pattern)
            for pattern in frozen_rules.rules.protected_paths
        )
    )
    if rejected:
        raise DeliveryRejected(rejected)
    ref = _candidate_ref(lease, record)
    tree = _write_tree(lease.repository, inspection.files)
    candidate = (
        _git(
            lease.repository,
            "commit-tree",
            tree,
            "-p",
            record.start_commit,
            "-m",
            f"Task {card.id} candidate ({record.owner_token})",
        )
        .decode("ascii")
        .strip()
    )
    _owned(lease, record)
    _git(
        lease.repository,
        "update-ref",
        "--no-deref",
        ref,
        candidate,
        "0" * len(candidate),
    )
    return WorkspaceDelivery(
        record.task_baseline, candidate, changed, inspection.digest, ref, tree
    )


@_boundary
def remove_workspace(lease: ExecutorLease, record: WorkspaceRecord) -> None:
    """Remove only a verified owned checkout; preserve its candidate refs."""
    _owned(lease, record)
    _git(lease.repository, "worktree", "remove", "--force", record.root)
    if Path(record.root).exists() or Path(record.git_directory).exists():
        raise ValueError("Git did not completely remove the owned workspace")
