"""Immutable host-owned workspace and delivery records."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WorkspaceControl:
    path: str
    identity: tuple[int, int] | None
    content: bytes | None


@dataclass(frozen=True, slots=True)
class WorkspaceRecord:
    repository: str
    source: str
    task_baseline: str
    start_commit: str
    root: str
    owner_token: str
    root_identity: tuple[int, int]
    git_directory: str
    git_identity: tuple[int, int]
    controls: tuple[WorkspaceControl, ...]
    object_format: str


@dataclass(frozen=True, slots=True)
class WorkspaceFile:
    path: str
    mode: str
    oid: str
    content: bytes


@dataclass(frozen=True, slots=True)
class WorkspaceInspection:
    files: tuple[WorkspaceFile, ...]
    digest: str


@dataclass(frozen=True, slots=True)
class WorkspaceDelivery:
    baseline: str
    candidate: str
    changed_paths: tuple[str, ...]
    digest: str
    ref: str
    tree: str
