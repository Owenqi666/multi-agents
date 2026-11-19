"""Read workspace-owned product resources without launching product operations."""

import base64
import binascii
import hashlib
import json
import os
import re
import stat
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from multi_agent.cli.integration_cli import _view as integration_view
from multi_agent.cli.integration_verification import verification_view
from multi_agent.cli.project_stage import stage_view
from multi_agent.cli.review_inputs import load_review_input
from multi_agent.cli.runner import view_round
from multi_agent.cli.snapshot import MAX_SNAPSHOT_BYTES, _diff, _git, _path
from multi_agent.cli.task_cli import view_task
from multi_agent.cli.task_review_cli import view_task_review
from multi_agent.cli.task_test_cli import _existing_lease, view_task_test
from multi_agent.cli.task_test_evidence import inspect_task_test_command
from multi_agent.core.review_input import (
    ExecutedReviewInput,
    IntegrationReviewInput,
    ReviewInput,
    review_source,
)
from multi_agent.store.integration_store import IntegrationStore
from multi_agent.store.project_store import ProjectStore

from .gate import ReadGate
from .workspace import Workspace

_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_PROJECT_SCHEMA = {
    "project": 12,
    "goal": 12,
    "project-plan": 12,
    "project-stages": 12,
    "stage": 12,
    "consultation": 12,
    "integration": 6,
}
_HISTORY = {"plan", "task", "task-review", "integration", "review"}
_KINDS = _HISTORY | set(_PROJECT_SCHEMA) | {"task-test", "snapshot"}
_ARTIFACT_PAGE = 65_536
_ARTIFACT_LIMIT = 128 * 1024 * 1024
LOCKED_RESOURCE_KINDS = frozenset(
    {"task", "task-test", "task-review", "integration", "stage", "plan"}
)
LOCKED_DIFF_KINDS = frozenset({"task", "integration"})

type _Query = dict[str, list[str]]


class _ResourceStore(ProjectStore, IntegrationStore):
    """Combined domain queries on one read-only connection."""


def _query(query: _Query, allowed: set[str]) -> dict[str, str]:
    if query.keys() - allowed or any(len(items) != 1 for items in query.values()):
        raise ValueError("unknown or repeated resource query parameter")
    return {key: items[0] for key, items in query.items()}


def _number(value: str, label: str, *, minimum: int = 1) -> int:
    if not re.fullmatch(r"[0-9]{1,10}", value) or int(value) < minimum:
        raise ValueError(f"invalid {label}")
    return int(value)


def _validate_resource_identity(kind: str, identifier: str) -> None:
    if kind not in _KINDS or not _IDENTITY.fullmatch(identifier):
        raise ValueError("invalid resource kind or identifier")


def _scope(workspace: Workspace, kind: str, identifier: str) -> tuple[Path, str]:
    _validate_resource_identity(kind, identifier)
    database = workspace.data_dir / "reviews.sqlite3"
    if database.is_symlink():
        raise ValueError("workspace database must be a regular file")
    if not database.is_file():
        raise KeyError(identifier)
    common = Path(
        _git(
            workspace.repository,
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        )
        .decode("utf-8")
        .removesuffix("\n")
    ).resolve(strict=True)
    return database, str(common)


def _read_snapshot(store: IntegrationStore, identifier: str) -> ReviewInput:
    value = load_review_input(store, identifier)
    if value.snapshot_id != identifier:
        raise KeyError(identifier)
    return value


def _owned(store: _ResourceStore, common: str, kind: str, identifier: str) -> None:
    repository: str | None
    minimum = _PROJECT_SCHEMA.get(kind)
    if (
        minimum
        and store._connection.execute("PRAGMA user_version").fetchone()[0] < minimum
    ):
        raise KeyError(identifier)
    if kind == "integration":
        repository = store.load_integration(identifier).repository
    elif kind == "plan":
        repository = store.load_planning_input(identifier).frozen_rules.repository
    elif kind in {"task", "task-test"}:
        repository = store.load_task_input(identifier).frozen_rules.repository
    elif kind in {"review", "task-review", "snapshot"}:
        snapshot_id = (
            identifier
            if kind == "snapshot"
            else store.load_round(identifier).snapshot_id
        )
        snapshot = _read_snapshot(store, snapshot_id)
        if kind == "task-review" and not isinstance(snapshot, ExecutedReviewInput):
            raise KeyError(identifier)
        repository = review_source(snapshot).repository
    else:
        match kind:
            case "project":
                repository = store.project_repository(identifier)
            case "goal":
                repository = store.load_goal(identifier).git_common_dir
            case "consultation":
                repository = store._consultation_row(identifier)["git_common_dir"]
            case _:
                plan_id = (
                    store.load_stage(identifier).plan_id
                    if kind == "stage"
                    else identifier
                )
                goal_id = store._plan_row(plan_id)["goal_id"]
                repository = store.load_goal(goal_id).git_common_dir
    if repository != common:
        raise KeyError(identifier)


def require_owned(workspace: Workspace, kind: str, identifier: str) -> None:
    """Authorize a domain ID against its recorded Git common directory."""
    database, common = _scope(workspace, kind, identifier)
    with _ResourceStore.open_readonly(database) as store:
        _owned(store, common, kind, identifier)


def _resource_options(kind: str, query: _Query) -> dict[str, str]:
    allowed = {"history"} if kind in _HISTORY else set()
    if kind == "task-test":
        allowed = {"implementation_attempt", "test_attempt"}
    options = _query(query, allowed)
    if "history" in options and options["history"] != "1":
        raise ValueError("history must be 1")
    return options


def _test_attempts(options: dict[str, str]) -> tuple[int, int | None]:
    implementation = _number(
        options.get("implementation_attempt", ""), "implementation_attempt"
    )
    test = options.get("test_attempt")
    return implementation, _number(test, "test_attempt") if test is not None else None


def _resource_data(
    workspace: Workspace, kind: str, identifier: str, query: _Query
) -> dict[str, Any]:
    options = _resource_options(kind, query)
    history = "history" in options
    database, common = _scope(workspace, kind, identifier)
    with _ResourceStore.open_readonly(database) as store:
        _owned(store, common, kind, identifier)
        if kind == "integration":
            record = store.load_integration(identifier)
            result = integration_view(record, "show")
            if store.has_integration_verification():
                result.update(
                    verification_view(
                        store,
                        record,
                        target_current=result["target_is_current"],
                        history=history,
                    )
                )
            return result
        if kind == "review":
            return view_round(store, identifier, history=history)
        if kind == "snapshot":
            value = _read_snapshot(store, identifier)
            return {"snapshot_id": value.snapshot_id, "input": value.to_dict()}

    if kind == "plan":
        return view_task(database, planning_run_id=identifier, history=history)
    if kind == "task":
        return view_task(database, task_run_id=identifier, history=history)
    if kind == "task-review":
        return view_task_review(database, identifier, history=history)
    if kind == "task-test":
        attempt, test = _test_attempts(options)
        return view_task_test(database, identifier, attempt, test_attempt=test)
    with ProjectStore.open_readonly(database) as projects:
        match kind:
            case "project":
                return projects.show_project(identifier)
            case "consultation":
                return projects.show_consultation(identifier)
            case "goal":
                return projects.show_goal(identifier)
            case "project-plan":
                return projects.show_plan(identifier)
            case "project-stages":
                return {
                    "kind": "project_stages",
                    "plan_id": identifier,
                    "stages": [
                        asdict(stage) for stage in projects.list_stages(identifier)
                    ],
                }
            case "stage":
                return stage_view(projects, database, projects.load_stage(identifier))
    raise ValueError("invalid resource kind")


def _snapshot(workspace: Workspace, identifier: str) -> ReviewInput:
    database, common = _scope(workspace, "snapshot", identifier)
    with IntegrationStore.open_readonly(database) as store:
        value = _read_snapshot(store, identifier)
    if review_source(value).repository != common:
        raise KeyError(identifier)
    return value


def read_snapshot_file(
    workspace: Workspace, kind: str, identifier: str, query: _Query
) -> dict[str, Any]:
    if kind != "snapshot":
        raise ValueError("only snapshots contain frozen files")
    options = _query(query, {"side", "path"})
    side = options.get("side")
    if side not in {"baseline", "candidate"}:
        raise ValueError("side must be baseline or candidate")
    path = _path(options.get("path", ""))
    source = review_source(_snapshot(workspace, identifier))
    for item in source.files:
        if (item.side, item.path) == (side, path):
            return asdict(item)
    raise KeyError(path)


def _empty_diff() -> dict[str, Any]:
    return {
        "baseline": None,
        "candidate": None,
        "snapshot_id": None,
        "diff": None,
        "test_change_paths": [],
    }


def lock_busy_diff(kind: str, identifier: str) -> dict[str, Any]:
    _validate_resource_identity(kind, identifier)
    return _empty_diff() | {
        "unavailable_reason": "A CLI job is active.",
        "unavailable_code": "lock_busy",
        "lock_busy": True,
    }


def read_diff(workspace: Workspace, kind: str, identifier: str) -> dict[str, Any]:
    result = _empty_diff()
    if kind == "snapshot":
        snapshot = _snapshot(workspace, identifier)
        source = review_source(snapshot)
        return result | {
            "baseline": source.baseline,
            "candidate": source.candidate,
            "snapshot_id": snapshot.snapshot_id,
            "diff": source.diff,
            "test_change_paths": list(snapshot.test_change_paths)
            if isinstance(snapshot, IntegrationReviewInput)
            else [],
        }
    if kind not in LOCKED_DIFF_KINDS:
        raise ValueError("diffs require a task, integration or snapshot")
    value = _resource_data(workspace, kind, identifier, {})
    candidate = value["candidate"]
    if kind == "task":
        baseline = value["details"]["input"]["card"]["baseline_commit"]
    else:
        baseline = value["target_baseline"]
        candidate = candidate["commit"] if candidate is not None else None
    result["baseline"] = baseline
    if value["verification"] != "verified" or candidate is None:
        return result | {"unavailable_reason": "No verified frozen candidate."}
    if not _COMMIT.fullmatch(baseline) or not _COMMIT.fullmatch(candidate):
        raise ValueError("candidate diff requires exact commit identities")
    diff = _diff(workspace.repository, baseline, candidate)
    if len(diff.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
        raise ValueError("candidate diff exceeds the preview size limit")
    return result | {"candidate": candidate, "diff": diff}


@dataclass(frozen=True, slots=True)
class _Artifact:
    stream: str
    sha256: str
    byte_count: int
    source: Path | bytes
    identity: tuple[int, int] | None = None


def _receipt_artifact(receipt: dict[str, Any]) -> _Artifact | None:
    path = receipt.get("path")
    digest = receipt.get("sha256")
    size = receipt.get("byte_count")
    identity = receipt.get("identity")
    if not (
        receipt.get("sealed") is True
        and receipt.get("closed") is True
        and receipt.get("healthy") is True
        and isinstance(path, str)
        and isinstance(digest, str)
        and _SHA256.fullmatch(digest)
        and type(size) is int
        and 0 <= size <= _ARTIFACT_LIMIT
        and isinstance(identity, (list, tuple))
        and len(identity) == 2
        and all(type(item) is int for item in identity)
    ):
        return None
    source = Path(path)
    return _Artifact(source.stem, digest, size, source, (identity[0], identity[1]))


def _relative_artifact(workspace: Workspace, path: Path) -> Path:
    if not path.is_absolute() or not path.is_relative_to(workspace.data_dir):
        raise KeyError("artifact is outside the workspace data directory")
    relative = path.relative_to(workspace.data_dir)
    if not relative.parts or any(part == ".." for part in relative.parts):
        raise KeyError("artifact has an invalid registered path")
    return relative


def _sealed_bytes(
    workspace: Workspace,
    path: Path,
    digest: str,
    *,
    limit: int,
    size: int | None = None,
    identity: tuple[int, int] | None = None,
    offset: int = 0,
    length: int | None = None,
) -> bytes:
    relative = _relative_artifact(workspace, path)
    directory = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(workspace.data_dir, directory)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, directory, dir_fd=fd)
            os.close(fd)
            fd = child
        file = os.open(
            relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
        )
        with os.fdopen(file, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_size > limit
                or size is not None
                and before.st_size != size
                or identity is not None
                and (before.st_dev, before.st_ino) != identity
            ):
                raise KeyError("artifact no longer matches its registered identity")
            checksum = hashlib.sha256()
            remaining = before.st_size
            while remaining:
                chunk = stream.read(min(remaining, _ARTIFACT_PAGE))
                if not chunk:
                    raise KeyError("artifact length changed while reading")
                checksum.update(chunk)
                remaining -= len(chunk)
            stream.seek(offset)
            size_to_read = max(0, before.st_size - offset)
            if length is not None:
                size_to_read = min(size_to_read, length)
            content = stream.read(size_to_read)
            after = os.fstat(stream.fileno())
            if (
                len(content) != size_to_read
                or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                or checksum.hexdigest() != digest
            ):
                raise KeyError("artifact no longer matches its registered bytes")
            return content
    except OSError as error:
        raise KeyError("registered artifact is unavailable") from error
    finally:
        os.close(fd)


def _add_logs(
    workspace: Workspace,
    artifacts: dict[str, _Artifact],
    prefix: str,
    records: list[dict[str, Any]],
) -> None:
    for index, receipt in enumerate(records, 1):
        artifact = _receipt_artifact(receipt)
        if artifact is not None and isinstance(artifact.source, Path):
            try:
                _relative_artifact(workspace, artifact.source)
            except KeyError:
                continue
            artifacts[f"{prefix}-{index}"] = artifact


def _integration_artifacts(
    workspace: Workspace, identifier: str, artifacts: dict[str, _Artifact]
) -> None:
    database = workspace.data_dir / "reviews.sqlite3"
    with IntegrationStore.open_readonly(database) as store:
        if not store.has_integration_verification():
            return
        attempts = store.integration_test_history(identifier)
        for attempt, _ in attempts:
            if attempt.result is None:
                continue
            for command in attempt.result.commands:
                root = (
                    Path(attempt.request.directory)
                    / "logs"
                    / f"command-{command.sequence}"
                )
                path = root / "execution.json"
                if (
                    command.manifest_path != str(path)
                    or command.manifest_sha256 is None
                ):
                    continue
                try:
                    raw = _sealed_bytes(
                        workspace, path, command.manifest_sha256, limit=8 * 1024 * 1024
                    )
                except KeyError:
                    continue
                receipt = json.loads(raw)["actual_result"]["receipt"]
                logs = [
                    row for row in receipt["logs"] if Path(row["path"]).parent == root
                ]
                _add_logs(
                    workspace,
                    artifacts,
                    f"test-{attempt.number}-{command.sequence}",
                    logs,
                )


def _task_test_artifacts(
    workspace: Workspace, value: dict[str, Any], artifacts: dict[str, _Artifact]
) -> None:
    database = workspace.data_dir / "reviews.sqlite3"
    identifier = value["task_run_id"]
    implementation = value["implementation_attempt"]
    test = value["test_attempt"]
    with IntegrationStore.open_readonly(database) as store:
        try:
            with _existing_lease(store, identifier, implementation) as lease:
                for row in value["commands"]:
                    sequence = row["sequence"]
                    if row["status"] == "not_run":
                        continue
                    try:
                        archived = inspect_task_test_command(
                            store, identifier, implementation, test, sequence, lease
                        )
                    except (OSError, KeyError, ValueError):
                        continue
                    _add_logs(
                        workspace,
                        artifacts,
                        f"test-{test}-{sequence}",
                        [asdict(log) for log in archived.receipt.logs],
                    )
        except (OSError, KeyError, ValueError):
            # The read-only test view still exposes actual source errors and state.
            pass


def _artifacts(
    workspace: Workspace, kind: str, identifier: str, value: dict[str, Any]
) -> dict[str, _Artifact]:
    artifacts: dict[str, _Artifact] = {}
    if kind == "project-plan" and value.get("evidence"):
        _add_logs(workspace, artifacts, "planner", value["evidence"].get("logs", []))
    elif kind == "consultation":
        for seat in value["seats"]:
            if seat["evidence"]:
                _add_logs(
                    workspace,
                    artifacts,
                    f"seat-{seat['number']}",
                    seat["evidence"].get("logs", []),
                )
    elif kind in {"review", "task-review", "snapshot"}:
        snapshot = _snapshot(workspace, value["snapshot_id"])
        if isinstance(snapshot, ExecutedReviewInput):
            commands = snapshot.executed_suite.commands
        elif isinstance(snapshot, IntegrationReviewInput):
            commands = snapshot.commands
        else:
            commands = ()
        for index, command in enumerate(commands, 1):
            for stream, log in (("stdout", command.stdout), ("stderr", command.stderr)):
                artifacts[f"frozen-{index}-{stream}"] = _Artifact(
                    stream, log.sha256, log.byte_count, log.content
                )
    elif kind == "integration":
        _integration_artifacts(workspace, identifier, artifacts)
    elif kind == "task-test":
        _task_test_artifacts(workspace, value, artifacts)
    return artifacts


def _artifact_id(
    kind: str, identifier: str, options: _Query, key: str, digest: str
) -> str:
    locator = json.dumps(
        [kind, identifier, options, key, digest], separators=(",", ":")
    )
    return "art-" + base64.urlsafe_b64encode(locator.encode()).decode().rstrip("=")


def _validate_resource_request(kind: str, identifier: str, query: _Query) -> None:
    _validate_resource_identity(kind, identifier)
    options = _resource_options(kind, query)
    if kind == "task-test":
        _test_attempts(options)


def lock_busy_resource(kind: str, identifier: str, query: _Query) -> dict[str, Any]:
    _validate_resource_request(kind, identifier, query)
    return {
        "kind": kind,
        "id": identifier,
        "data": None,
        "artifacts": [],
        "lock_busy": True,
    }


def read_resource(
    workspace: Workspace, kind: str, identifier: str, query: _Query
) -> dict[str, Any]:
    data = _resource_data(workspace, kind, identifier, query)
    options = (
        {
            field: [str(data[field])]
            for field in ("implementation_attempt", "test_attempt")
        }
        if kind == "task-test"
        else {}
    )
    return {
        "kind": kind,
        "id": identifier,
        "data": data,
        "artifacts": [
            {
                "artifact_id": _artifact_id(
                    kind, identifier, options, key, artifact.sha256
                ),
                "stream": artifact.stream,
                "byte_count": artifact.byte_count,
                "sha256": artifact.sha256,
            }
            for key, artifact in _artifacts(workspace, kind, identifier, data).items()
        ],
    }


def read_artifact(
    workspace: Workspace,
    artifact_id: str,
    query: _Query,
    *,
    gate: ReadGate | None = None,
) -> dict[str, Any]:
    options = _query(query, {"offset", "limit"})
    offset = _number(options.get("offset", "0"), "offset", minimum=0)
    limit = _number(options.get("limit", str(_ARTIFACT_PAGE)), "limit")
    if limit > _ARTIFACT_PAGE:
        raise ValueError("artifact page cannot exceed 65536 bytes")
    if not re.fullmatch(r"art-[A-Za-z0-9_-]{1,1024}", artifact_id):
        raise KeyError(artifact_id)
    token = artifact_id.removeprefix("art-")
    try:
        kind, identifier, selection, key, digest = json.loads(
            base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        )
        if not (
            all(isinstance(item, str) for item in (kind, identifier, key, digest))
            and isinstance(selection, dict)
            and set(selection) <= {"implementation_attempt", "test_attempt"}
            and all(
                isinstance(value, list)
                and len(value) == 1
                and isinstance(value[0], str)
                for value in selection.values()
            )
            and _SHA256.fullmatch(digest)
            and _artifact_id(kind, identifier, selection, key, digest) == artifact_id
        ):
            raise ValueError("invalid artifact locator")
    except (ValueError, TypeError, UnicodeError, binascii.Error) as error:
        raise KeyError(artifact_id) from error

    admission = (
        gate.read(guarded=kind in LOCKED_RESOURCE_KINDS)
        if gate is not None
        else nullcontext(True)
    )
    with admission as allowed:
        if not allowed:
            _validate_resource_request(kind, identifier, selection)
            return {
                "stream": None,
                "text": None,
                "offset": offset,
                "next_offset": None,
                "byte_count": None,
                "sha256": None,
                "truncated": None,
                "lock_busy": True,
            }
        data = _resource_data(workspace, kind, identifier, selection)
        artifact = _artifacts(workspace, kind, identifier, data).get(key)
        if artifact is None or artifact.sha256 != digest:
            raise KeyError(artifact_id)
        source = artifact.source
        part = (
            _sealed_bytes(
                workspace,
                source,
                artifact.sha256,
                limit=_ARTIFACT_LIMIT,
                size=artifact.byte_count,
                identity=artifact.identity,
                offset=offset,
                length=limit,
            )
            if isinstance(source, Path)
            else source[offset : offset + limit]
        )
    next_offset = offset + len(part)
    return {
        "stream": artifact.stream,
        "text": part.decode("utf-8", errors="replace"),
        "offset": offset,
        "next_offset": next_offset if next_offset < artifact.byte_count else None,
        "byte_count": artifact.byte_count,
        "sha256": artifact.sha256,
        "truncated": next_offset < artifact.byte_count,
    }


def references(action: str, result: object) -> list[dict[str, str]]:
    """Collect explicit product IDs from normal and partially successful results."""
    items: dict[tuple[str, str], dict[str, str]] = {}

    def remember(kind: str, identifier: object, parent_id: object = None) -> None:
        if isinstance(identifier, str) and _IDENTITY.fullmatch(identifier):
            record = items.setdefault(
                (kind, identifier), {"kind": kind, "id": identifier}
            )
            if (
                isinstance(parent_id, str)
                and _IDENTITY.fullmatch(parent_id)
                and parent_id != identifier
            ):
                record["parent_id"] = parent_id

    def visit(value: object, parent: object = None) -> None:
        if not isinstance(value, dict):
            return
        goal = value.get("goal")
        review_parent = (
            value.get("task_run_id") or value.get("integration_id") or parent
        )
        for field, kind, owner in (
            ("project_id", "project", None),
            ("consultation_id", "consultation", value.get("project_id")),
            ("goal_id", "goal", value.get("project_id")),
            (
                "plan_id",
                "project-plan",
                goal.get("goal_id") if isinstance(goal, dict) else None,
            ),
            ("stage_run_id", "stage", value.get("plan_id")),
            ("planning_run_id", "plan", value.get("stage_run_id")),
            ("task_run_id", "task", value.get("planning_run_id")),
            (
                "integration_id",
                "integration",
                value.get("stage_run_id") or value.get("rebased_from"),
            ),
            ("snapshot_id", "snapshot", value.get("round_id")),
            (
                "source_snapshot_id",
                "snapshot",
                value.get("consultation_id") or value.get("plan_id"),
            ),
        ):
            remember(kind, value.get(field), owner)
        review_kind = (
            "task-review"
            if action.startswith("task.") or value.get("task_run_id") is not None
            else "review"
        )
        for field in (
            "round_id",
            "review_round_id",
            "confirmation_round_id",
            "assessment_round_id",
            "source_review_round_id",
        ):
            remember(review_kind, value.get(field), review_parent)
        for field in (
            "goal",
            "consultation",
            "review",
            "confirmation",
            "revision",
            "rework",
            "retest",
            "rereview",
            "acceptance",
            "integration",
            "implementation",
        ):
            visit(value.get(field), review_parent)
        for field in ("tasks", "leaves", "reviews"):
            children = value.get(field)
            if isinstance(children, list):
                for child in children:
                    visit(child)

    visit(result)
    return list(items.values())
