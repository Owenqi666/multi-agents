"""Read an old sealed archive through the real service without changing it."""

import json
import shutil
import sqlite3
import threading
from contextlib import closing, contextmanager
from dataclasses import replace

import pytest
from test_planning_store import inputs as planning_inputs
from test_task_store import create, inputs, started
from test_web_project_flow import _get, _request
from test_web_project_resources import _repository

from multi_agent.cli.snapshot import freeze_repository
from multi_agent.core.task_events import TaskEnvelope
from multi_agent.store import database as store_database
from multi_agent.store.database import _migration_scripts
from multi_agent.store.project_store import ProjectStore
from multi_agent.store.task_store import TaskStore
from multi_agent.web.server import WorkspaceServer
from multi_agent.web.workspace import Workspace


def _schema(connection, version):
    for number, script in _migration_scripts():
        if number > version:
            break
        connection.executescript(script.read_text(encoding="utf-8"))
        connection.execute(f"PRAGMA user_version = {number}")


@contextmanager
def _serve_archive(repository, scratch, database):
    workspace = Workspace.open(repository, scratch, read_only_database=database)
    with WorkspaceServer(0, workspace, {}) as service:
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        try:
            yield service
        finally:
            service.shutdown()
            thread.join(timeout=5)


def test_web_reads_committed_wal_of_old_archive_without_source_changes(tmp_path):
    repo, base, head = _repository(tmp_path / "repository")
    foreign_repo, foreign_base, foreign_head = _repository(tmp_path / "foreign")
    local = freeze_repository(repo, base, head, "The local change", ("Check it.",))
    foreign = freeze_repository(
        foreign_repo, foreign_base, foreign_head, "The other change", ("Check it.",)
    )
    source = tmp_path / "archive"
    source.mkdir()
    database = source / "sealed database.sqlite3"
    seed = tmp_path / "seed.sqlite3"
    with closing(sqlite3.connect(seed)) as connection:
        _schema(connection, 2)
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA wal_autocheckpoint = 0")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        with connection:
            for snapshot in (local, foreign):
                connection.execute(
                    "INSERT INTO snapshots VALUES (?, ?)",
                    (snapshot.snapshot_id, json.dumps(snapshot.to_dict())),
                )
        shutil.copyfile(seed, database)
        shutil.copyfile(
            seed.with_name(seed.name + "-wal"),
            database.with_name(database.name + "-wal"),
        )

    # The record is only in the archived WAL.
    with closing(sqlite3.connect(database.as_uri() + "?immutable=1", uri=True)) as main:
        assert main.execute("SELECT COUNT(*) FROM snapshots").fetchone() == (0,)
    original = {path.name: path.read_bytes() for path in source.iterdir()}
    scratch = tmp_path / "scratch"
    with _serve_archive(repo, scratch, database) as service:
        assert _get(service, "workspace")["read_only"] is True
        resource = _get(service, f"resources/snapshot/{local.snapshot_id}")
        assert resource["data"]["input"]["requirements"] == "The local change"
        shown = _get(service, f"diffs/snapshot/{local.snapshot_id}")
        assert "+candidate" in shown["diff"]
        assert "+candidate" in resource["data"]["input"]["diff"]
        assert (
            _get(
                service, f"snapshots/{local.snapshot_id}/file?side=candidate&path=a.txt"
            )["text"]
            == "candidate\n"
        )
        assert (
            _request(service, "GET", f"/api/resources/snapshot/{foreign.snapshot_id}")[
                0
            ]
            == 404
        )
        status, error = _request(service, "GET", "/api/resources/consultation/missing")
        assert status == 404
        assert error["error"]["code"] == "archive_unsupported"
        assert "version 2" in error["error"]["message"]

        commands = (
            (
                "/api/jobs",
                {"action": "task.run", "args": {"planning_run_id": "unused"}},
            ),
            (
                "/api/jobs",
                {"action": "task.retry", "args": {"planning_run_id": "unused"}},
            ),
            (
                "/api/jobs",
                {"action": "environment.refresh", "args": {"provider": "codex"}},
            ),
            ("/api/jobs/job/cancel", {}),
        )
        for url, command in commands:
            status, error = _request(service, "POST", url, command)
            assert status == 403 and error["error"]["code"] == "archive_read_only"
        assert _get(service, "jobs")["items"] == []
        assert {path.name: path.read_bytes() for path in source.iterdir()} == original
    assert {path.name: path.read_bytes() for path in source.iterdir()} == original
    assert not (scratch / "reviews.sqlite3").exists()


def test_web_rejects_missing_archive_before_creating_scratch(tmp_path):
    repo, _, _ = _repository(tmp_path / "repository")
    missing = tmp_path / "no-database.sqlite3"
    scratch = tmp_path / "scratch"
    with pytest.raises(FileNotFoundError):
        Workspace.open(repo, scratch, read_only_database=missing)
    assert not missing.exists() and not scratch.exists()


def test_web_archive_reads_project_from_before_consultation_schema(tmp_path):
    repo, base, _ = _repository(tmp_path / "repository")
    archive = tmp_path / "archive"
    archive.mkdir()
    database = archive / "project.sqlite3"
    with closing(sqlite3.connect(database)) as connection, connection:
        _schema(connection, 9)
        connection.execute(
            "INSERT INTO projects VALUES (?, ?)", ("project", str(repo / ".git"))
        )
        connection.execute(
            "INSERT INTO project_goals VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("goal", "project", str(repo), base, "Original", "Confirmed", "then"),
        )
    original = database.read_bytes()
    with _serve_archive(repo, tmp_path / "scratch", database) as service:
        assert (
            _get(service, "resources/project/project")["data"]["goals"][0][
                "confirmed_goal"
            ]
            == "Confirmed"
        )
        goal = _get(service, "resources/goal/goal")["data"]
        assert goal["original_goal"] == "Original"
        assert "consultation" not in goal
    assert list(archive.iterdir()) == [database]
    assert database.read_bytes() == original


def test_web_archive_reads_v3_task_after_product_migration(tmp_path, monkeypatch):
    repo, baseline, _ = _repository(tmp_path / "repository")
    archive = tmp_path / "archive"
    archive.mkdir()
    database = archive / "reviews.sqlite3"
    identifier = "archived-task"
    value = inputs()
    value = replace(
        value,
        card=replace(value.card, baseline_commit=baseline),
        frozen_rules=replace(
            value.frozen_rules,
            repository=str(repo / ".git"),
            baseline_commit=baseline,
        ),
    )
    scripts = store_database._migration_scripts()
    with monkeypatch.context() as migration:
        migration.setattr(
            store_database,
            "_migration_scripts",
            lambda: [script for script in scripts if script[0] <= 3],
        )
        with TaskStore(database) as store:
            create(store, identifier, value)
            store.apply_task(identifier, TaskEnvelope(1, started(value)))

    def read(scratch):
        before = {path.name: path.read_bytes() for path in archive.iterdir()}
        with _serve_archive(repo, tmp_path / scratch, database) as service:
            data = _get(service, f"resources/task/{identifier}")["data"]
            assert data["task_run_id"] == identifier
            assert data["planning_run_id"] is None
            assert data["details"]["input"]["requirements"] == value.requirements
            assert data["attempts"][0]["request"]["worktree"] == "/external/worktree"
            assert data["attempts"][0]["runtime"]["reported_model"] is None
            assert _get(service, "jobs")["items"] == []
        assert {path.name: path.read_bytes() for path in archive.iterdir()} == before

    read("before-migration")
    with ProjectStore(database) as store:
        assert (
            store._connection.execute("PRAGMA user_version").fetchone()[0]
            == scripts[-1][0]
        )
        assert store.load_task(identifier).current_attempt is not None
    read("after-migration")

    # A present provenance row still has to bind both ways.
    with ProjectStore(database) as store:
        planned, _ = planning_inputs()
        store.create_planning(
            "unbound-plan",
            replace(planned, frozen_rules=value.frozen_rules),
            value.created_at,
        )
        with store._connection:
            store._connection.execute(
                "INSERT INTO task_planning_provenance VALUES (?, ?, ?, ?)",
                (identifier, "unbound-plan", "unverified-hash", "{}"),
            )
    with _serve_archive(repo, tmp_path / "invalid-binding", database) as service:
        status, error = _request(service, "GET", f"/api/resources/task/{identifier}")
        assert status == 400
        assert error["error"]["message"] == "task and planning binding differ"
