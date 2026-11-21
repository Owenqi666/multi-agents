"""Recover real web consultation jobs after the CLI controller is lost."""

import json
import os
import signal
from contextlib import suppress
from pathlib import Path

import pytest
import test_project_cli as project_fixture
import test_project_consultation as consultation_fixture
import test_provider_limits_cli as provider_fixture
import test_runtime as runtime_fixture
import test_task_workflow_cli as workflow_fixture
from test_web_project_flow import _get, _job, _request
from test_web_workspace import _finished, _serve, _wait_for

from multi_agent.core.planning import PlanningRequest
from multi_agent.core.project_plan import ConfirmedGoal
from multi_agent.store import database as migrations
from multi_agent.store.project_store import ProjectStore

project = project_fixture.project
document = project_fixture.document
synthetic_codex_auth_and_fake_environment = (
    runtime_fixture.synthetic_codex_auth_and_fake_environment
)


def _command(data, identifier):
    return workflow_fixture.module_cli(
        "--structured-errors",
        "--data-dir",
        data,
        "project",
        "reconcile-consultation",
        identifier,
    )


def _refusal(data, identifier, code):
    result = _command(data, identifier)
    assert result.returncode == 2, (result.stdout, result.stderr)
    error = json.loads(result.stdout)
    assert error["kind"] == "cli_error"
    assert error["error"]["code"] == code
    assert error["error"]["message"] in result.stderr


def _read(data, identifier):
    database = data / "reviews.sqlite3"
    if not database.is_file():
        return None
    with ProjectStore.open_readonly(database) as store:
        try:
            return store.show_consultation(identifier)
        except KeyError:
            return None


def test_web_consultation_reconciles_after_killing_live_owner(tmp_path, project):
    repository, baseline, data, (original, _), _ = project
    claude, _ = provider_fixture._fake(
        tmp_path,
        "first-advisor",
        "claude",
        report=consultation_fixture._ADVICE["claude"],
    )
    codex, wire = provider_fixture._fake(
        tmp_path, "second-advisor", "codex", mode="wait"
    )
    executable = Path(codex)
    executable.write_text(
        executable.read_text().replace('"model-1"', '"gpt-6.2-astra"')
    )
    executables = {"claude": claude, "codex": codex}

    with _serve(repository, data, executables) as service:
        status, accepted = _request(
            service,
            "POST",
            "/api/jobs",
            {
                "action": "project.consult",
                "args": {
                    "baseline": baseline,
                    "original_goal": original.read_text(),
                    "advisors": [
                        {
                            "provider": "claude",
                            "model": "claude-fable-5-2",
                            "effort": "high",
                        },
                        {
                            "provider": "codex",
                            "model": "gpt-6.2-astra",
                            "effort": "high",
                        },
                    ],
                },
            },
        )
        assert status == 202, accepted
        job_id = accepted["job_id"]
        identifier = f"consultation-{job_id}"
        _wait_for(lambda: provider_fixture._turns(wire))
        runtime_pid = next(
            row["pid"]
            for row in reversed(workflow_fixture.records(wire))
            if row["kind"] == "spawn" and row["argv"][:1] == ["app-server"]
        )
        controller = service.jobs.controllers[job_id]
        try:
            before = _read(data, identifier)
            assert before is not None and before["status"] == "running"
            first, second = before["seats"]
            assert first["status"] == "succeeded"
            assert first["advice"] == consultation_fixture._ADVICE["claude"]
            assert second["status"] == "running"
            _refusal(data, identifier, "consultation_live")
            assert _read(data, identifier) == before
        finally:
            controller.kill()
            controller.wait(timeout=10)
            with suppress(ProcessLookupError):
                os.killpg(runtime_pid, signal.SIGKILL)
        stopped = _wait_for(lambda: _finished(service, job_id))
        assert stopped["exit_code"] == -signal.SIGKILL
        assert stopped["result"] is None

    with _serve(repository, data, executables) as service:
        job = _job(service, "project.consult.reconcile", consultation_id=identifier)
        recovered = job["result"]
        assert recovered["status"] == "failed"
        assert recovered["finished_at"] == recovered["interrupted_at"]
        assert recovered["draft"] is None
        assert recovered["source_snapshot_id"] == before["source_snapshot_id"]
        assert recovered["seats"][0] == first
        interrupted = recovered["seats"][1]
        assert interrupted["started_at"] == second["started_at"]
        assert interrupted["finished_at"] is None
        assert interrupted["status"] == "failed"
        assert interrupted["evidence"] is None
        assert interrupted["error"]["raw_type"] == "project.consultation.interrupted"
        assert {"kind": "consultation", "id": identifier} in job["resources"]
        assert (
            _get(service, f"resources/consultation/{identifier}")["data"] == recovered
        )
        repeated = _job(
            service, "project.consult.reconcile", consultation_id=identifier
        )
        assert repeated["result"] == recovered


@pytest.mark.parametrize("changed", ["legacy", "missing", "replaced"])
def test_unverifiable_consultation_owner_leaves_state_untouched(
    tmp_path, monkeypatch, changed
):
    data = tmp_path / "data"
    data.mkdir()
    database = data / "reviews.sqlite3"
    identifier = f"consultation-{'1' * 32}"
    owner = None
    if changed != "legacy":
        directory = data / "consultation-owners"
        directory.mkdir(mode=0o700)
        file = directory / identifier
        file.touch(mode=0o600)
        info = file.stat()
        owner = (info.st_dev, info.st_ino)
        if changed == "replaced":
            file.rename(directory / "old-file")
            file.touch(mode=0o600)
        else:
            file.unlink()

    scripts = migrations._migration_scripts()
    with monkeypatch.context() as old:
        if changed == "legacy":
            old.setattr(
                migrations,
                "_migration_scripts",
                lambda: [script for script in scripts if script[0] <= 12],
            )
        with ProjectStore(database) as store:
            store.save_snapshot("frozen-source", {"baseline": "a" * 40})
            args = (
                identifier,
                "/repo",
                "/repo/.git",
                "a" * 40,
                "Original goal",
                None,
                "frozen-source",
                (PlanningRequest("claude", "fable", "high"),),
                "01",
            )
            if changed == "legacy":
                # Seed actual v12 rows; this path has no registered owner column.
                store._connection.execute(
                    "INSERT INTO project_consultations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (*args[:7], "01"),
                )
                store._connection.execute(
                    """INSERT INTO project_consultation_seats
                       (consultation_id, number, request, prompt_template_version, started_at)
                       VALUES (?, 1, ?, 'advice-v1', '02')""",
                    (
                        identifier,
                        json.dumps(
                            {"provider": "claude", "model": "fable", "effort": "high"}
                        ),
                    ),
                )
            else:
                store.start_consultation(*args, owner=owner)
                store.reserve_consultation_seat(identifier, 1, "advice-v1", "02")
            original = store.show_consultation(identifier)

    _refusal(data, identifier, "consultation_owner_unknown")
    assert _read(data, identifier) == original


@pytest.mark.parametrize("version", [9, 10, 11])
def test_archive_projects_before_consultations_and_revisions(
    tmp_path, monkeypatch, document, version
):
    database = tmp_path / "archive.sqlite3"
    scripts = migrations._migration_scripts()
    with monkeypatch.context() as old:
        old.setattr(
            migrations,
            "_migration_scripts",
            lambda: [script for script in scripts if script[0] <= version],
        )
        with ProjectStore(database) as store:
            store.confirm_goal(
                ConfirmedGoal(
                    "project-old",
                    "goal-old",
                    "/repo",
                    "/repo/.git",
                    "a" * 40,
                    "Original historical goal",
                    "Confirmed historical goal",
                    "01",
                ),
                new_project=True,
            )
            store.save_snapshot("frozen-source", {"baseline": "a" * 40})
            store.start_plan(
                "plan-old",
                "goal-old",
                PlanningRequest("claude", "fable", "high"),
                "frozen-source",
                "plan-v1",
                "02",
            )
            store.finish_plan(
                "plan-old", "03", document=document, error=None, evidence={}
            )

    with ProjectStore.open_readonly(database) as archived:
        shown = archived.show_project("project-old")
        goal_view = archived.show_goal("goal-old")
        plan = archived.show_plan("plan-old")
        assert "consultation" not in goal_view
        assert plan["goal"] == shown["goals"][0]
        assert plan["document"] == document
        assert (
            "source_plan_id" not in plan and "source_plan_id" not in shown["plans"][0]
        )
        assert (
            archived._connection.execute("PRAGMA user_version").fetchone()[0] == version
        )
