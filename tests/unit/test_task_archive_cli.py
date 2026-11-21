"""The task readback can use the original schema before planning and tests."""

from test_task_store import create

from multi_agent.cli.task_cli import view_task
from multi_agent.store import database
from multi_agent.store.database import ArchivedDatabase, archive_reads
from multi_agent.store.task_store import TaskStore


def test_task_view_reads_schema_three_archive_without_newer_tables(
    tmp_path, monkeypatch
):
    path = (tmp_path / "archive.sqlite3").resolve()
    scripts = [
        (number, sql) for number, sql in database._migration_scripts() if number <= 3
    ]
    with monkeypatch.context() as seed:
        seed.setattr(database, "_migration_scripts", lambda: scripts)
        with TaskStore(path) as store:
            original = create(store, "archived-task")
    contents = path.read_bytes()

    with archive_reads(ArchivedDatabase.open(path)):
        value = view_task(path, task_run_id="archived-task", history=True)

    assert value["task_run_id"] == "archived-task"
    assert value["planning_run_id"] is None
    assert value["tests"] == []
    assert value["details"]["input"]["requirements"] == original.requirements
    assert path.read_bytes() == contents
    assert list(tmp_path.iterdir()) == [path]
