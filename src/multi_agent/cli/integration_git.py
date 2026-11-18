"""Apply verified candidate trees through isolated Git plumbing."""

import os
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory, TemporaryFile
from typing import Self

from .snapshot import _OID, GIT_TIMEOUT, _check_paths, _path, _string


def _git_invocation(
    repository: Path, args: tuple[str, ...], objects: Path | None
) -> tuple[list[str], dict[str, str]]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_ATTR_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
        LC_ALL="C",
    )
    if objects is not None:
        environment["GIT_OBJECT_DIRECTORY"] = str(objects)
    argv = [
        "git",
        "--no-replace-objects",
        "--no-lazy-fetch",
        "--no-optional-locks",
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        f"core.attributesFile={os.devnull}",
        "-C",
        str(repository),
        *args,
    ]
    return argv, environment


def _git(
    repository: Path,
    *args: str,
    statuses: tuple[int, ...] = (0,),
    objects: Path | None = None,
) -> subprocess.CompletedProcess[bytes]:
    argv, environment = _git_invocation(repository, args, objects)
    try:
        result = subprocess.run(
            argv, capture_output=True, check=False, env=environment, timeout=GIT_TIMEOUT
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"cannot run Git {args[0]}: {error}") from error
    if result.returncode not in statuses:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"Git {args[0]} exited {result.returncode}: {detail}")
    return result


def _oid(value: str) -> str:
    if not _OID.fullmatch(value):
        raise ValueError("integration requires full Git object IDs")
    return value


def _full_ref(repository: Path, ref: str) -> None:
    if not _string(ref, "Git reference").startswith("refs/"):
        raise ValueError("integration requires an exact full Git reference")
    _git(repository, "check-ref-format", ref)
    if ref.startswith(("refs/heads/", "refs/multi-agent/")):
        common = _git(
            repository, "rev-parse", "--path-format=absolute", "--git-common-dir"
        ).stdout
        path = Path(common.decode("utf-8").removesuffix("\n"))
        for part in ref.split("/"):
            path /= part
            if path.is_symlink():
                raise ValueError(f"Git reference path is a symlink: {path}")


def _commit_object(repository: Path, commit: str) -> None:
    if _git(repository, "cat-file", "-t", _oid(commit)).stdout != b"commit\n":
        raise ValueError(f"Git object is not a commit: {commit}")


def read_ref(repository: Path, ref: str) -> str | None:
    """Read an exact direct ref, including packed refs, without resolving aliases."""
    _full_ref(repository, ref)
    symbolic = _git(
        repository, "symbolic-ref", "--quiet", "--no-recurse", ref, statuses=(0, 1)
    )
    if symbolic.returncode == 0:
        raise ValueError(f"integration requires a direct Git reference: {ref}")
    exists = _git(repository, "show-ref", "--exists", ref, statuses=(0, 2))
    if exists.returncode == 2:
        return None
    raw = _git(repository, "show-ref", "--verify", "--hash", ref).stdout
    commit = _oid(raw.decode("ascii").removesuffix("\n"))
    _commit_object(repository, commit)
    return commit


def read_tree(repository: Path, commit: str) -> str:
    raw = _git(
        repository,
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{_oid(commit)}^{{commit}}^{{tree}}",
    ).stdout
    return _oid(raw.decode("ascii").removesuffix("\n"))


def _prepare_ref_verification(channel: socket.socket, commands: bytes) -> None:
    expected = b"start: ok\nprepare: ok\n"
    deadline = time.monotonic() + GIT_TIMEOUT
    channel.settimeout(GIT_TIMEOUT)
    channel.sendall(commands)
    reply = bytearray()
    while len(reply) < len(expected):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("timed out preparing Git reference verification")
        channel.settimeout(remaining)
        part = channel.recv(len(expected) - len(reply))
        if not part:
            break
        reply.extend(part)
    if reply != expected:
        raise ValueError("Git did not confirm both reference locks")


def _close_ref_verification(
    process: subprocess.Popen[bytes], channel: socket.socket
) -> None:
    # EOF aborts a prepared transaction without updating or logging any refs.
    channel.close()
    try:
        process.wait(timeout=GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)


@contextmanager
def verify_integration_refs(
    repository: Path,
    target_ref: str,
    target_baseline: str,
    candidate_ref: str,
    candidate_commit: str,
) -> Iterator[None]:
    """Hold Git's verify-only locks for the caller's acceptance transaction."""
    if not target_ref.startswith("refs/heads/") or not candidate_ref.startswith(
        "refs/multi-agent/"
    ):
        raise ValueError(
            "acceptance requires a local target and a private candidate ref"
        )
    refs = ((target_ref, target_baseline), (candidate_ref, candidate_commit))
    for ref, commit in refs:
        _full_ref(repository, ref)
        _oid(commit)
    commands = (
        "start\0"
        + "".join(f"option no-deref\0verify {ref}\0{commit}\0" for ref, commit in refs)
        + "prepare\0"
    ).encode("utf-8")
    argv, environment = _git_invocation(
        repository, ("update-ref", "--stdin", "-z"), None
    )
    channel, child = socket.socketpair()
    with channel, child, TemporaryFile() as diagnostics:
        try:
            process = subprocess.Popen(
                argv,
                stdin=child.fileno(),
                stdout=child.fileno(),
                stderr=diagnostics,
                env=environment,
                start_new_session=True,
            )
        except OSError as error:
            raise ValueError(
                f"cannot start Git reference verification: {error}"
            ) from error
        child.close()
        try:
            try:
                _prepare_ref_verification(channel, commands)
            except (OSError, ValueError) as error:
                _close_ref_verification(process, channel)
                diagnostics.seek(0)
                detail = (
                    diagnostics.read(4096).decode("utf-8", errors="replace").strip()
                )
                raise ValueError(
                    f"Git reference verification failed: {detail or error}"
                ) from error
            # Git's no-deref verify may accept a symbolic ref with a matching
            # resolved OID. Check the exact direct refs while both are locked.
            for ref, expected in refs:
                if read_ref(repository, ref) != expected:
                    raise ValueError(
                        f"Git reference changed during verification: {ref}"
                    )
            if process.poll() is not None:
                raise ValueError("Git reference locks were lost before acceptance")
            yield
        finally:
            _close_ref_verification(process, channel)


class IntegrationGit:
    """Use fresh bare metadata and write merge objects to the repository object store."""

    def __init__(self, repository: Path) -> None:
        self.repository = repository
        self._temporary: TemporaryDirectory[str] | None = None
        self._objects: Path | None = None

    def __enter__(self) -> Self:
        if self._temporary is not None:
            raise ValueError("Git integration context is already active")
        object_format = (
            _git(self.repository, "rev-parse", "--show-object-format")
            .stdout.decode("ascii")
            .removesuffix("\n")
        )
        objects = (
            _git(
                self.repository,
                "rev-parse",
                "--path-format=absolute",
                "--git-path",
                "objects",
            )
            .stdout.decode("utf-8")
            .removesuffix("\n")
        )
        temporary = TemporaryDirectory(prefix="multi-agent-integration-")
        try:
            _git(
                Path(temporary.name),
                "init",
                "--bare",
                "--quiet",
                "--template=",
                f"--object-format={object_format}",
            )
        except BaseException:
            temporary.cleanup()
            raise
        self._temporary = temporary
        self._objects = Path(objects)
        return self

    def __exit__(self, *_: object) -> None:
        assert self._temporary is not None
        self._temporary.cleanup()
        self._temporary = None
        self._objects = None

    def branch_ref(self, branch: str) -> str:
        _string(branch, "target branch", nonempty=True)
        ref = branch if branch.startswith("refs/") else f"refs/heads/{branch}"
        if branch.startswith("-") or not ref.startswith("refs/heads/"):
            raise ValueError("integration target must be an exact local branch")
        _full_ref(self.repository, ref)
        return ref

    def ref_commit(self, ref: str) -> str | None:
        return read_ref(self.repository, ref)

    def tree(self, commit: str) -> str:
        return read_tree(self.repository, commit)

    def _run(
        self, *args: str, statuses: tuple[int, ...] = (0,)
    ) -> subprocess.CompletedProcess[bytes]:
        if self._temporary is None or self._objects is None:
            raise ValueError("Git integration context is closed")
        return _git(
            Path(self._temporary.name), *args, statuses=statuses, objects=self._objects
        )

    def is_ancestor(self, ancestor: str, commit: str) -> bool:
        result = self._run(
            "merge-base", "--is-ancestor", _oid(ancestor), _oid(commit), statuses=(0, 1)
        )
        return result.returncode == 0

    def merge(
        self, base: str, tip: str, candidate: str, task_id: str
    ) -> tuple[str, tuple[str, ...] | None]:
        """Merge a member's cumulative change and report original tip on conflict."""
        _oid(base)
        if candidate == tip:
            return tip, None
        # Apply the verified paths themselves. Rename inference could move an
        # edit to a different path in the target than the task's checked delta.
        result = self._run(
            "-c",
            "merge.renames=false",
            "merge-tree",
            "--write-tree",
            "--name-only",
            "--no-messages",
            "-z",
            f"--merge-base={base}",
            tip,
            candidate,
            statuses=(0, 1),
        )
        if not result.stdout.endswith(b"\0"):
            raise ValueError("Git merge-tree did not return NUL-delimited output")
        tree, *paths = result.stdout[:-1].split(b"\0")
        tree_id = _oid(tree.decode("ascii"))
        if result.returncode == 1:
            return tip, tuple(sorted({_path(path.decode("utf-8")) for path in paths}))
        names = self._run("ls-tree", "-r", "-z", "--name-only", tree_id).stdout
        try:
            _check_paths(path.decode("utf-8") for path in names.split(b"\0")[:-1])
        except ValueError as error:
            raise ValueError(
                f"{task_id} produces a tree that cannot be materialized: {error}"
            ) from error
        # Applying base-to-candidate is a patch. A second parent would claim
        # ancestry for baseline changes we never copied into this tree.
        commit = self._run(
            "-c",
            "user.name=Multi-agent",
            "-c",
            "user.email=multi-agent@localhost",
            "commit-tree",
            tree_id,
            "-p",
            tip,
            "-m",
            f"Integrate task {task_id}",
        ).stdout
        return _oid(commit.decode("ascii").removesuffix("\n")), None

    def pin(self, ref: str, commit: str) -> None:
        if not ref.startswith("refs/multi-agent/"):
            raise ValueError("integration may only pin a private workbench reference")
        if self.ref_commit(ref) is not None:
            raise ValueError(f"integration reference already exists: {ref}")
        _commit_object(self.repository, commit)
        _git(
            self.repository, "update-ref", "--no-deref", ref, commit, "0" * len(commit)
        )
