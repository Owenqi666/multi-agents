"""Freeze committed Git objects without reading or executing working-tree files.

The diff is generated in an empty bare repository borrowing only the source
object database. This prevents local attributes, an index, dirty files, hooks,
replacement objects and user diff configuration from altering the review input.
All child processes receive literal argument lists and bounded run times.

UTF-8 text is retained verbatim. Binary blobs and gitlinks retain their object
IDs and modes, with text=None; their contents are not materialized. Symlinks are
retained as inert text, never followed. No checkout filters or scripts run.
"""

import json
import os
import re
import subprocess
import unicodedata
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Any

from multi_agent.core.review_input import (
    ExecutedReviewInput,
    ReviewInput,
    review_source,
)
from multi_agent.core.snapshot import FrozenFile, FrozenSnapshot, TestEvidence

MAX_FILES = 20_000
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
GIT_TIMEOUT = 30
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def _git(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    # User-selected repositories may have different settings. These read-only
    # commands do not need inherited Git routing/configuration or replacement refs.
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
        str(repo),
        *args,
    ]
    try:
        completed = subprocess.run(
            argv,
            input=input_bytes,
            capture_output=True,
            check=False,
            timeout=GIT_TIMEOUT,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"cannot read Git repository: {error}") from error
    if completed.returncode:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"Git input rejected: {detail}")
    return completed.stdout


def _string(value: object, name: str, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or "\0" in value:
        raise ValueError(f"{name} must be a string without NUL")
    if nonempty and not value.strip():
        raise ValueError(f"{name} must not be empty")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(f"{name} must be valid UTF-8") from error
    return value


def _requirements(requirements: str, acceptance: tuple[str, ...]) -> None:
    _string(requirements, "requirements", nonempty=True)
    if not isinstance(acceptance, tuple) or not acceptance:
        raise ValueError("acceptance must be a nonempty tuple of criteria")
    for criterion in acceptance:
        _string(criterion, "acceptance criterion", nonempty=True)


def _command(command: tuple[str, ...]) -> None:
    if not isinstance(command, tuple) or not command:
        raise ValueError("test command must be a nonempty tuple of argv strings")
    for part in command:
        _string(part, "test command argument")
    _string(command[0], "test executable", nonempty=True)


def _tests(
    tests: tuple[TestEvidence, ...], required_tests: tuple[tuple[str, ...], ...]
) -> None:
    if not isinstance(tests, tuple) or not isinstance(required_tests, tuple):
        raise TypeError("tests and required_tests must be tuples")
    for command in required_tests:
        _command(command)
    if len(set(required_tests)) != len(required_tests):
        raise ValueError("required test commands must be unique")
    for evidence in tests:
        if not isinstance(evidence, TestEvidence):
            raise TypeError("test evidence must be a TestEvidence record")
        if not isinstance(evidence.commit, str) or not _OID.fullmatch(evidence.commit):
            raise ValueError("test evidence must name a full commit object ID")
        _command(evidence.command)
        if type(evidence.exit_code) is not int:
            raise TypeError("test exit_code must be an integer")
        _string(evidence.log, "test log")


def _path(path: str) -> str:
    _string(path, "repository file path", nonempty=True)
    parts = path.split("/")
    if (
        PurePosixPath(path).is_absolute()
        or "\\" in path
        or any(part in ("", ".", "..") for part in parts)
        or any(part.rstrip(" .").casefold() == ".git" for part in parts)
    ):
        raise ValueError(f"unsafe repository file path: {path!r}")
    return path


def _check_paths(paths: Iterable[str]) -> None:
    # The workstation may use a case-insensitive, Unicode-normalizing filesystem.
    # Refuse collisions instead of silently materializing the wrong file.
    seen: dict[str, str] = {}
    for path in paths:
        _path(path)
        normalized = unicodedata.normalize("NFC", path).casefold()
        if normalized in seen:
            raise ValueError(f"file paths collide on this workstation: {path!r}")
        seen[normalized] = path
    for path in seen:
        for parent in PurePosixPath(path).parents:
            if parent.as_posix() in seen:
                raise ValueError(f"file and directory paths collide: {path!r}")


def _resolve_commit(repo: Path, revision: str) -> str:
    _string(revision, "commit revision", nonempty=True)
    if revision.startswith("-") or "\n" in revision or "\r" in revision:
        raise ValueError("invalid commit revision")
    resolved = (
        _git(
            repo, "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"
        )
        .decode("ascii")
        .strip()
    )
    if not _OID.fullmatch(resolved):
        raise ValueError("Git did not resolve a full commit object ID")
    return resolved


def _files(repo: Path, revision: str, side: str) -> tuple[FrozenFile, ...]:
    records = _git(repo, "ls-tree", "-r", "-z", "-l", "--full-tree", revision)
    entries: list[tuple[str, str, str, str, int]] = []
    total_size = 0
    for record in records.split(b"\0"):
        if not record:
            continue
        metadata, path_bytes = record.split(b"\t", 1)
        mode, kind, oid, length = metadata.decode("ascii").split()
        try:
            path = path_bytes.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("repository filenames must be UTF-8") from error
        size = int(length) if kind == "blob" else 0
        if size > MAX_FILE_BYTES:
            raise ValueError(
                f"repository file exceeds {MAX_FILE_BYTES} bytes: {path!r}"
            )
        total_size += size
        entries.append((path, mode, kind, oid, size))
    if len(entries) > MAX_FILES or total_size > MAX_SNAPSHOT_BYTES:
        raise ValueError("repository snapshot exceeds the file or byte limit")
    _check_paths(entry[0] for entry in entries)
    blob_entries = [entry for entry in entries if entry[2] == "blob"]
    output = (
        _git(
            repo,
            "cat-file",
            "--batch",
            input_bytes="".join(f"{entry[3]}\n" for entry in blob_entries).encode(
                "ascii"
            ),
        )
        if blob_entries
        else b""
    )
    offset = 0
    texts: dict[str, str | None] = {}
    for path, _, _, oid, size in blob_entries:
        end = output.index(b"\n", offset)
        expected = f"{oid} blob {size}".encode("ascii")
        if output[offset:end] != expected:
            raise ValueError(f"Git blob response does not match {path!r}")
        raw = output[end + 1 : end + 1 + size]
        offset = end + size + 2
        if len(raw) != size or output[offset - 1 : offset] != b"\n":
            raise ValueError(f"Git blob response is incomplete for {path!r}")
        try:
            texts[oid] = raw.decode("utf-8") if b"\0" not in raw else None
        except UnicodeDecodeError:
            texts[oid] = None
    if offset != len(output):
        raise ValueError("Git returned extra blob output")
    return tuple(
        FrozenFile(side, path, texts[oid] if kind == "blob" else None, oid, mode)
        for path, mode, kind, oid, _ in sorted(entries)
    )


def _diff(repo: Path, baseline: str, candidate: str) -> str:
    objects = (
        _git(repo, "rev-parse", "--path-format=absolute", "--git-path", "objects")
        .decode("utf-8")
        .removesuffix("\n")
    )
    # An alternates entry is one path per line. Reject unsupported paths rather
    # than allowing an embedded line ending to add another object database.
    if "\n" in objects or "\r" in objects:
        raise ValueError("repository object database path contains a line ending")
    with TemporaryDirectory(prefix="multi-agent-diff-") as temporary:
        directory = Path(temporary)
        object_format = (
            _git(repo, "rev-parse", "--show-object-format").decode("ascii").strip()
        )
        _git(
            directory,
            "init",
            "--bare",
            "--quiet",
            "--template=",
            f"--object-format={object_format}",
        )
        (directory / "objects" / "info" / "alternates").write_text(
            objects + "\n", encoding="utf-8"
        )
        result = _git(
            directory,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--no-relative",
            "--no-indent-heuristic",
            "--diff-algorithm=myers",
            "--find-renames=50%",
            "--full-index",
            "--ignore-submodules=none",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            "--unified=3",
            baseline,
            candidate,
            "--",
        )
    # A non-UTF-8 patch can contain byte-oriented source. Files themselves retain
    # text=None and their object IDs; replacement characters here are not source.
    return result.decode("utf-8", errors="replace")


def _repository_dir(repo: Path) -> Path:
    if not isinstance(repo, Path):
        raise TypeError("repository must be a Path")
    try:
        repo = repo.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"repository cannot be opened: {error}") from error
    if not repo.is_dir():
        raise ValueError("repository must be a directory")
    if "\n" in str(repo) or "\r" in str(repo):
        raise ValueError("repository path contains a line ending")
    return repo


def _common_directory(repo: Path) -> str:
    repository = (
        _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
        .decode("utf-8")
        .removesuffix("\n")
    )
    return str(Path(repository).resolve())


def freeze_repository(
    repo: Path,
    baseline: str,
    candidate: str,
    requirements: str,
    acceptance: tuple[str, ...],
    tests: tuple[TestEvidence, ...] = (),
    *,
    required_tests: tuple[tuple[str, ...], ...] = (),
) -> FrozenSnapshot:
    """Freeze both commit trees, their diff, and supplied acceptance evidence."""
    _requirements(requirements, acceptance)
    _tests(tests, required_tests)
    repo = _repository_dir(repo)
    repository = _common_directory(repo)
    baseline_id = _resolve_commit(repo, baseline)
    candidate_id = _resolve_commit(repo, candidate)
    files = _files(repo, baseline_id, "baseline") + _files(
        repo, candidate_id, "candidate"
    )
    return FrozenSnapshot(
        repository=repository,
        baseline=baseline_id,
        candidate=candidate_id,
        diff=_diff(repo, baseline_id, candidate_id),
        requirements=requirements,
        acceptance=acceptance,
        tests=tests,
        files=files,
        required_tests=required_tests,
    )


def freeze_diff(
    diff: str,
    requirements: str,
    acceptance: tuple[str, ...],
    *,
    repository: Path | None = None,
) -> FrozenSnapshot:
    """Freeze report-only text with optional Git common-directory ownership."""
    _requirements(requirements, acceptance)
    _string(diff, "diff")
    if len(diff.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
        raise ValueError("diff exceeds the snapshot byte limit")
    return FrozenSnapshot(
        None,
        None,
        None,
        diff,
        requirements,
        acceptance,
        (),
        (),
        bound_repository=_common_directory(_repository_dir(repository))
        if repository is not None
        else None,
    )


def materialize(snapshot: FrozenSnapshot, destination: Path) -> None:
    _materialize(snapshot, destination)


def _materialize(
    snapshot: FrozenSnapshot,
    destination: Path,
    review_manifest: dict[str, Any] | None = None,
) -> None:
    """Write fresh, non-executable text trees and a manifest for one reviewer.

    Existing nonempty destinations and ambiguous paths are refused. No Git
    metadata, binary bytes, executable bits, or functioning symlinks are created.
    This is a read-only source view, not an environment for running tests.
    """
    for side in ("baseline", "candidate"):
        _check_paths(item.path for item in snapshot.files if item.side == side)
    if any(item.side not in ("baseline", "candidate") for item in snapshot.files):
        raise ValueError("snapshot file side must be baseline or candidate")
    if destination.is_symlink():
        raise ValueError("materialization destination must not be a symlink")
    if destination.exists() and (
        not destination.is_dir() or any(destination.iterdir())
    ):
        raise ValueError("materialization destination must be a new or empty directory")
    destination.mkdir(parents=True, exist_ok=True)
    for side in ("baseline", "candidate"):
        (destination / side).mkdir()
    omitted: list[dict[str, Any]] = []
    for item in snapshot.files:
        if item.text is None:
            omitted.append(
                {
                    "side": item.side,
                    "path": item.path,
                    "mode": item.mode,
                    "oid": item.oid,
                }
            )
            continue
        target = destination / item.side / item.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(item.text, encoding="utf-8", newline="")
        target.chmod(0o444)
    manifest = destination / "snapshot.json"
    manifest.write_text(
        json.dumps(
            {
                "snapshot_id": snapshot.snapshot_id,
                "repository": snapshot.repository,
                "baseline": snapshot.baseline,
                "candidate": snapshot.candidate,
                "omitted_files": omitted,
                "symlinks_are_inert_text": True,
                "tests_are_user_supplied": True,
                **(review_manifest or {}),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    manifest.chmod(0o444)
    for directory in sorted(
        (entry for entry in destination.rglob("*") if entry.is_dir()),
        key=lambda entry: len(entry.parts),
        reverse=True,
    ):
        directory.chmod(0o555)
    destination.chmod(0o555)


def materialize_review_input(value: ReviewInput, destination: Path) -> None:
    """Render one complete reviewer cwd without reading archived host paths."""
    if destination.is_symlink() or (
        destination.exists()
        and (not destination.is_dir() or any(destination.iterdir()))
    ):
        raise ValueError("review materialization requires a new or empty directory")
    destination.mkdir(parents=True, exist_ok=True)
    source = review_source(value)
    if not isinstance(value, FrozenSnapshot):
        _materialize(
            source,
            destination / "source",
            {
                "snapshot_id": value.snapshot_id,
                "source_snapshot_id": source.snapshot_id,
                "tests_are_user_supplied": False,
                "executed_test_input": "../review-input.json",
                "executed_log_directory": "../logs",
            },
        )
        review_input = value.to_dict()
        review_input["snapshot_id"] = value.snapshot_id
        review_input["source"].pop("files")
        review_input["source"].pop("diff")
        if isinstance(value, ExecutedReviewInput):
            commands = value.executed_suite.commands
            rows = review_input["executed_suite"]["commands"]
        else:
            commands = value.commands
            rows = review_input["commands"]
        for index, command in enumerate(commands, 1):
            directory = destination / "logs" / str(index)
            directory.mkdir(parents=True)
            row = rows[index - 1]
            for name, log in (("stdout", command.stdout), ("stderr", command.stderr)):
                relative = f"logs/{index}/{name}.bin"
                path = destination / relative
                path.write_bytes(log.content)
                path.chmod(0o444)
                row[name] = {
                    "path": relative,
                    "byte_count": log.byte_count,
                    "sha256": log.sha256,
                    "encoding": "raw bytes; may contain non-UTF-8 data",
                }
            directory.chmod(0o555)
        (destination / "logs").chmod(0o555)
    else:
        # Keep the original v1 run_seat JSON and file bytes exactly.
        materialize(source, destination / "source")
        review_input = source.to_dict()
        review_input.pop("files")
        review_input.pop("diff")
    for name, text in (
        ("review-input.json", json.dumps(review_input, ensure_ascii=False)),
        ("diff.patch", source.diff),
    ):
        (destination / name).write_text(text, encoding="utf-8")
        (destination / name).chmod(0o444)
