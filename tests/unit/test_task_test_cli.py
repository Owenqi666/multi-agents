"""The actual task-test parser/entrypoint and read-only result presentation."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import test_task_test_runner as runner_helpers
import test_task_test_store as stage_helpers

from multi_agent.cli.main import main
from multi_agent.core.task_test_events import TaskTestEnvelope, TaskTestReconciled
from multi_agent.core.task_test_state import TaskTestPhase
from multi_agent.store.task_test_store import TaskTestStore

feature = runner_helpers.feature


def run_arguments(feature, *, commands=("build", "test")):
    arguments = [
        "task-test",
        "run",
        "implementation",
        "--database",
        str(feature.database),
        "--implementation-attempt",
        "1",
        "--repository",
        str(feature.repo),
        "--directory",
        str(feature.directory),
        "--declaration-source",
        "Caller declares the ordered complete suite.",
        "--codex-bin",
        str(feature.executable),
        "--timeout",
        "15",
        "--grace",
        "0.2",
        "--output-bytes-limit",
        "1048576",
        "--tool-directory",
        str(Path(feature.python).parent),
        "--runtime-read-path",
        str(Path(sys.base_prefix).resolve()),
    ]
    for command in commands:
        arguments.extend(("--command", command))
    return arguments


def show_arguments(database):
    return [
        "task-test",
        "show",
        "implementation",
        "--database",
        str(database),
        "--implementation-attempt",
        "1",
    ]


def cli_process(arguments):
    return subprocess.run(
        [sys.executable, "-m", "multi_agent", *arguments],
        cwd=runner_helpers.REPOSITORY,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def assert_view_keys(value):
    assert set(value) == {
        "task_run_id",
        "implementation_attempt",
        "test_attempt",
        "candidate",
        "phase",
        "verification",
        "source_current",
        "commands",
        "details",
    }
    for command in value["commands"]:
        assert set(command) == {
            "sequence",
            "name",
            "status",
            "reported_exit_code",
            "exit_code",
            "verification",
            "logs",
        }
        assert set(command["logs"]) == {"stdout", "stderr", "wire"}


def test_task_test_feature_build_then_test_and_show(feature):
    result = cli_process(run_arguments(feature))
    assert result.returncode == 0, (result.stdout, result.stderr)
    output = json.loads(result.stdout)
    assert_view_keys(output)
    assert output["candidate"] == feature.candidate.delivery.candidate
    assert output["phase"] == "succeeded"
    assert [item["name"] for item in output["commands"]] == ["build", "test"]
    assert [item["exit_code"] for item in output["commands"]] == [0, 0]
    assert [item["reported_exit_code"] for item in output["commands"]] == [0, 0]
    assert (
        Path(output["commands"][0]["logs"]["stdout"]).read_bytes()
        == runner_helpers.STDOUT_BYTES
    )
    assert (
        Path(output["commands"][0]["logs"]["stderr"]).read_bytes()
        == runner_helpers.STDERR_BYTES
    )
    persisted, _ = runner_helpers.load(feature)
    checkout = Path(runner_helpers.prepared(persisted).checkout.root)
    assert (checkout / "artifact.bin").read_bytes() == runner_helpers.BUILD_BYTES
    assert (checkout / "test-read-artifact").read_bytes() == b"confirmed"
    assert not (feature.workspace / "artifact.bin").exists()
    assert not (feature.repo / "artifact.bin").exists()
    before = runner_helpers.load(feature)
    shown = cli_process(show_arguments(feature.database))
    assert shown.returncode == 0, (shown.stdout, shown.stderr)
    view = json.loads(shown.stdout)
    assert_view_keys(view)
    assert view["candidate"] == output["candidate"]
    assert view["phase"] == "succeeded" and view["source_current"] is True
    assert view["commands"] == output["commands"]
    assert runner_helpers.load(feature) == before
    assert (
        runner_helpers.planning_contexts.git(feature.repo, "status", "--porcelain")
        == ""
    )
    assert (
        runner_helpers.planning_contexts.git(feature.repo, "rev-parse", "HEAD")
        == feature.baseline
    )


@pytest.mark.parametrize("name", ["unknown-command", "proposal"])
def test_task_test_cli_rejects_unapproved_names_without_spawning(feature, capsys, name):
    if name == "proposal":
        with TaskTestStore(feature.database) as store:
            assert name in {
                item.name
                for item in store.load_task_input("implementation").card.proposed_tests
            }
    assert main(run_arguments(feature, commands=(name,))) == 2
    assert not feature.capture.exists()
    assert not feature.directory.exists()
    with TaskTestStore(feature.database) as store:
        assert store.load_task_test_checkpoint("implementation", 1) == (None, ())
    assert capsys.readouterr().err


def test_task_test_cli_requires_explicit_suite_declaration(tmp_path):
    with pytest.raises(SystemExit) as caught:
        main(
            [
                "task-test",
                "run",
                "implementation",
                "--database",
                str(tmp_path / "missing.sqlite"),
                "--implementation-attempt",
                "1",
                "--repository",
                str(tmp_path),
                "--directory",
                str(tmp_path / "new"),
                "--command",
                "build",
            ]
        )
    assert caught.value.code == 2
    assert not (tmp_path / "missing.sqlite").exists()
    assert not (tmp_path / "new").exists()


@pytest.mark.parametrize("version", [None, 4])
def test_task_test_show_does_not_create_or_migrate_database(tmp_path, capsys, version):
    database = tmp_path / "readonly.sqlite"
    before = stage_helpers.v4_database(database) if version == 4 else None
    existing = set(tmp_path.iterdir())
    assert main(show_arguments(database)) == 2
    assert capsys.readouterr().err
    assert set(tmp_path.iterdir()) == existing
    if version == 4:
        assert stage_helpers.raw_legacy(database) == before
        with sqlite3.connect(database) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
    else:
        assert not database.exists()


def test_task_test_show_sees_committed_wal_without_writes(feature, capsys):
    assert main(run_arguments(feature)) == 0
    capsys.readouterr()
    with TaskTestStore(feature.database) as writer:
        assert (
            writer._connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        )
        writer._connection.execute("PRAGMA wal_autocheckpoint=0")
        writer.apply_task_test(
            "implementation",
            TaskTestEnvelope(
                1,
                1,
                TaskTestReconciled(2, False, "Actual stop evidence withdrawn"),
                "withdrawn",
            ),
        )
        assert (
            writer.load_task_test("implementation", 1).phase
            == TaskTestPhase.AWAITING_EVIDENCE
        )
        wal = Path(str(feature.database) + "-wal")
        assert wal.exists() and wal.stat().st_size > 0
        before = writer.load_task_test_history("implementation", 1)
        assert main(show_arguments(feature.database)) == 0
        shown = json.loads(capsys.readouterr().out)
        assert shown["phase"] == "awaiting_evidence"
        assert writer.load_task_test_history("implementation", 1) == before


def test_task_test_show_detects_archive_tamper_without_reexecuting(feature, capsys):
    assert main(run_arguments(feature)) == 0
    output = json.loads(capsys.readouterr().out)
    log = Path(output["commands"][0]["logs"]["stdout"])
    log.chmod(0o600)
    log.write_bytes(b"tampered received output")
    log.chmod(0o400)
    before = runner_helpers.load(feature)
    capture = feature.capture.read_bytes()
    assert main(show_arguments(feature.database)) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["verification"] != output["verification"]
    assert shown["commands"][0]["verification"] != output["commands"][0]["verification"]
    assert (
        shown["phase"] == "succeeded"
    )  # Stored historical phase is not rewritten by inspection.
    assert runner_helpers.load(feature) == before
    assert feature.capture.read_bytes() == capture


def test_task_test_show_incomplete_reservation_has_no_invented_logs_or_resume(
    feature, capsys
):
    import test_task_test_context as context_helpers

    with context_helpers.launch(feature) as handle:
        context_helpers.reserve_command(handle)
    before = runner_helpers.load(feature)
    files = {path.relative_to(feature.base) for path in feature.base.rglob("*")}
    assert main(show_arguments(feature.database)) == 0
    view = json.loads(capsys.readouterr().out)
    assert view["phase"] == "running" and view["verification"] == "unverified"
    assert [command["status"] for command in view["commands"]] == [
        "incomplete",
        "not_run",
    ]
    assert all(
        command["exit_code"] is None and command["reported_exit_code"] is None
        for command in view["commands"]
    )
    assert all(
        command["logs"] == {"stdout": None, "stderr": None, "wire": None}
        for command in view["commands"]
    )
    assert view["details"]["retained_root"] == str(feature.directory)
    assert runner_helpers.load(feature) == before
    assert {path.relative_to(feature.base) for path in feature.base.rglob("*")} == files
    assert not feature.capture.exists()
