"""Exercise real read-only workspace resources and frozen evidence boundaries."""

import hashlib
import json
import os
import sqlite3
from contextlib import closing

import pytest
from test_project_stage import _model_card
from test_runtime import fake
from test_task_workflow_cli import git
from test_web_project_flow import _get, _job, _request
from test_web_workspace import _serve

from multi_agent.cli.snapshot import freeze_diff, freeze_repository
from multi_agent.core.planning import PlanningRequest
from multi_agent.core.project_plan import ConfirmedGoal
from multi_agent.store.database import _migration_scripts
from multi_agent.store.project_store import ProjectStore
from multi_agent.web.resources import (
    read_artifact,
    read_diff,
    read_resource,
    read_snapshot_file,
    references,
    require_owned,
)
from multi_agent.web.workspace import Workspace


def _repository(root):
    root.mkdir()
    git(root, "init", "--quiet", "--template=")
    (root / "a.txt").write_text("initial\n")
    git(root, "add", "a.txt")
    git(root, "commit", "--quiet", "-m", "initial")
    baseline = git(root, "rev-parse", "HEAD")
    (root / "a.txt").write_text("candidate\n")
    git(root, "commit", "--quiet", "-am", "candidate")
    candidate = git(root, "rev-parse", "HEAD")
    return root, baseline, candidate


@pytest.fixture
def bound(tmp_path):
    root, baseline, candidate = _repository(tmp_path / "local")
    workspace = Workspace.open(root, tmp_path / "data")
    return workspace, workspace.data_dir / "reviews.sqlite3", baseline, candidate


def test_project_and_snapshot_resources_are_owned_and_read_only(bound, tmp_path):
    workspace, database, baseline, candidate = bound
    repository = workspace.repository
    with pytest.raises(KeyError):
        read_resource(workspace, "goal", "goal-empty", {})
    assert not database.exists()
    foreign, foreign_base, foreign_candidate = _repository(tmp_path / "foreign")
    local_snapshot = freeze_repository(
        repository, baseline, candidate, "local", ("file",)
    )
    foreign_snapshot = freeze_repository(
        foreign, foreign_base, foreign_candidate, "foreign", ("file",)
    )
    pasted = freeze_diff("pasted patch", "unbound", ("file",))
    with ProjectStore(database) as store:
        for version, root, sha in (
            ("local", repository, baseline),
            ("foreign", foreign, foreign_base),
        ):
            store.confirm_goal(
                ConfirmedGoal(
                    f"project-{version}",
                    f"goal-{version}",
                    str(root),
                    str(root / ".git"),
                    sha,
                    "original",
                    "confirmed",
                    "then",
                ),
                new_project=True,
            )
        for snapshot in (local_snapshot, foreign_snapshot, pasted):
            store.save_snapshot(snapshot.snapshot_id, snapshot.to_dict())
        store.save_snapshot("wrong-snapshot-id", local_snapshot.to_dict())
    before = database.read_bytes()
    (repository / "a.txt").write_text("unsaved tree text\n")

    assert (
        read_resource(workspace, "project", "project-local", {})["data"]["goals"][0][
            "goal_id"
        ]
        == "goal-local"
    )
    require_owned(workspace, "goal", "goal-local")
    assert (
        read_resource(workspace, "goal", "goal-local", {})["data"]["confirmed_goal"]
        == "confirmed"
    )
    with pytest.raises(KeyError):
        require_owned(workspace, "goal", "goal-foreign")
    for forbidden in (foreign_snapshot, pasted):
        with pytest.raises(KeyError):
            read_diff(workspace, "snapshot", forbidden.snapshot_id)
    with pytest.raises(KeyError):
        read_diff(workspace, "snapshot", "wrong-snapshot-id")
    snapshot_id = local_snapshot.snapshot_id
    assert read_diff(workspace, "snapshot", snapshot_id) == {
        "baseline": baseline,
        "candidate": candidate,
        "snapshot_id": snapshot_id,
        "diff": local_snapshot.diff,
        "test_change_paths": [],
    }
    selection = {"side": ["candidate"], "path": ["a.txt"]}
    assert (
        read_snapshot_file(workspace, "snapshot", snapshot_id, selection)["text"]
        == "candidate\n"
    )
    with pytest.raises(ValueError):
        read_snapshot_file(
            workspace, "snapshot", snapshot_id, selection | {"path": ["../a.txt"]}
        )
    with pytest.raises(ValueError):
        read_snapshot_file(
            workspace,
            "snapshot",
            snapshot_id,
            selection | {"side": ["baseline", "candidate"]},
        )
    assert database.read_bytes() == before


def test_workspace_upgrades_v11_goals_before_serving_http(bound, tmp_path, monkeypatch):
    workspace, database, baseline, _ = bound
    foreign, foreign_baseline, _ = _repository(tmp_path / "foreign")
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for version, script in _migration_scripts():
            if version > 11:
                break
            connection.executescript(script.read_text(encoding="utf-8"))
            connection.execute(f"PRAGMA user_version = {version}")
        for label, root, commit in (
            ("local", workspace.repository, baseline),
            ("foreign", foreign.resolve(), foreign_baseline),
        ):
            connection.execute(
                "INSERT INTO projects VALUES (?, ?)",
                (f"project-{label}", str(root / ".git")),
            )
            connection.execute(
                "INSERT INTO project_goals VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    f"goal-{label}",
                    f"project-{label}",
                    str(root),
                    commit,
                    "Original wording.\r\n",
                    "Confirmed before the upgrade.",
                    "then",
                ),
            )
        assert connection.execute("PRAGMA user_version").fetchone() == (11,)

    document = {
        "title": "Plan for the existing goal",
        "architecture": "Use existing source",
        "shared_interfaces": [],
        "stages": [_model_card("deliver", [])],
    }
    monkeypatch.setenv("FAKE_RUNTIME_REPORT", json.dumps(document))
    programs = {provider: fake(tmp_path, provider) for provider in ("codex", "claude")}
    with _serve(workspace.repository, workspace.data_dir, programs) as service:
        with closing(sqlite3.connect(database)) as connection:
            assert connection.execute("PRAGMA user_version").fetchone() == (13,)
        shown = _get(service, "resources/goal/goal-local")["data"]
        assert shown["project_id"] == "project-local"
        assert shown["original_goal"] == "Original wording.\r\n"
        assert shown["confirmed_goal"] == "Confirmed before the upgrade."
        assert _request(service, "GET", "/api/resources/goal/goal-foreign")[0] == 404
        result = _job(
            service,
            "project.plan",
            goal_id="goal-local",
            planner={
                "provider": "claude",
                "model": "claude-fable-5-2",
                "effort": "high",
            },
        )["result"]
        assert (
            result["goal"]["goal_id"] == "goal-local" and result["document"] == document
        )


def test_domain_artifact_bytes_are_scoped_and_checked_on_every_page(bound, tmp_path):
    workspace, database, baseline, _ = bound
    common = workspace.repository / ".git"
    root = workspace.data_dir / "logs" / "projects" / "plan-logs"
    root.mkdir(parents=True)
    log = root / "stdout.bin"
    raw = b"bytes\x00\xff\r\n"
    log.write_bytes(raw)
    info = log.stat()
    digest = hashlib.sha256(raw).hexdigest()
    receipt = {
        "path": str(log),
        "byte_count": len(raw),
        "sha256": digest,
        "identity": [info.st_dev, info.st_ino],
        "sealed": True,
        "healthy": True,
        "closed": True,
    }
    outside = tmp_path / "outside.bin"
    outside.write_bytes(raw)
    with ProjectStore(database) as store:
        store.confirm_goal(
            ConfirmedGoal(
                "project-local",
                "goal-local",
                str(workspace.repository),
                str(common),
                baseline,
                "original",
                "confirmed",
                "then",
            ),
            new_project=True,
        )
        store.start_plan(
            "plan-logs",
            "goal-local",
            PlanningRequest("codex", "model", "high"),
            "snapshot",
            "v1",
            "then",
        )
        store.finish_plan(
            "plan-logs",
            "later",
            document=None,
            error={"detail": "synthetic"},
            evidence={
                "logs": [
                    receipt,
                    receipt | {"path": str(outside)},
                    receipt | {"sealed": False},
                ]
            },
        )

    (artifact,) = read_resource(workspace, "project-plan", "plan-logs", {})["artifacts"]
    artifact_id = artifact["artifact_id"]
    page = read_artifact(workspace, artifact_id, {"offset": ["5"], "limit": ["2"]})
    assert page == {
        "stream": "stdout",
        "text": "\x00\ufffd",
        "offset": 5,
        "next_offset": 7,
        "byte_count": len(raw),
        "sha256": digest,
        "truncated": True,
    }
    assert read_artifact(workspace, artifact_id, {"offset": ["7"]})["text"] == "\r\n"
    log.write_bytes(b"different")
    with pytest.raises(KeyError):
        read_artifact(workspace, artifact_id, {})
    log.write_bytes(raw)
    displaced = root.with_name("displaced")
    root.rename(displaced)
    os.symlink(displaced, root)
    with pytest.raises(KeyError):
        read_artifact(workspace, artifact_id, {})


def test_references_keep_batch_tasks_and_child_review_snapshots():
    batch = {"tasks": [{"task_run_id": "task-first"}, {"task_run_id": "task-second"}]}
    assert references("task.run_batch", batch) == [
        {"kind": "task", "id": "task-first"},
        {"kind": "task", "id": "task-second"},
    ]
    result = {
        "integration_id": "integration-parent",
        "review": {"round_id": "review-parent", "snapshot_id": "snapshot-parent"},
        "revision": {"integration_id": "integration-child"},
        "rereview": {
            "review": {"round_id": "review-child", "snapshot_id": "snapshot-child"}
        },
    }
    assert {
        (item["kind"], item["id"]) for item in references("integration.review", result)
    } == {
        ("integration", "integration-parent"),
        ("integration", "integration-child"),
        ("review", "review-parent"),
        ("review", "review-child"),
        ("snapshot", "snapshot-parent"),
        ("snapshot", "snapshot-child"),
    }
