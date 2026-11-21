"""Read an old sealed archive through the real service without changing it."""

import json
import shutil
import sqlite3
import threading
from contextlib import closing, contextmanager

import pytest
from test_web_project_flow import _get, _request
from test_web_project_resources import _repository

from multi_agent.cli.snapshot import freeze_repository
from multi_agent.store.database import _migration_scripts
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
