"""Exercise first-run setup over HTTP without invoking logged-in providers."""

import sys
from functools import partial

import test_web_workspace as web
from test_web_project_flow import _job

from multi_agent.cli.task_inputs import RULES_PATH, parse_project_rules
from multi_agent.web.jobs import EnvironmentJobs

repository = web.repository
executables = web.executables


def test_selected_commit_exposes_its_rules_and_preview_is_read_only(
    tmp_path, executables
):
    repository = tmp_path / "repository"
    web._git(tmp_path, "init", "-qb", "main", str(repository))
    files = repository / RULES_PATH
    binaries, capture = executables
    with web._serve(repository, tmp_path / "data", binaries) as service:

        def read(suffix=""):
            status, data = web._request(
                service, "GET", "/api/workspace/onboarding" + suffix
            )
            assert status == 200, data
            return data

        empty = read()
        assert empty["baseline"] is None and empty["rules"]["status"] == "no_commit"
        assert {"kind": "commit_baseline"} in empty["next_steps"]
        assert {"kind": "create_goal"} not in empty["next_steps"]

        (repository / "README.md").write_text("Onboarding fixture.\n")
        missing = web._commit_all(repository)
        initial = read()
        assert initial["rules"]["status"] == "missing"
        assert {"kind": "add_rules"} in initial["next_steps"]
        assert not files.exists()
        template = initial["rules_template"]
        assert parse_project_rules(template).commands[0].name == "unit"

        files.parent.mkdir()
        files.write_text("version: 1\n")
        invalid = web._commit_all(repository)
        web._git(repository, "branch", "setup/incomplete", invalid)
        files.write_text(template)
        older = web._commit_all(repository)
        files.write_text(template.replace("  unit:", "  ci:"))
        current = web._commit_all(repository)
        files.write_text("locally edited rules")
        latest = read()
        selected = read("?baseline=" + older)
        assert latest["baseline"] == current == selected["head"]
        assert selected["baseline"] == older and selected["branch"] == "main"
        assert [item["name"] for item in latest["rules"]["commands"]] == ["ci"]
        assert selected["rules"]["commands"] == [
            {
                "name": "unit",
                "argv": ["python3", "-m", "unittest", "discover", "-s", "tests"],
            }
        ]
        assert read("?baseline=" + missing)["rules"]["status"] == "missing"
        broken = read("?baseline=setup%2Fincomplete")
        assert broken["baseline"] == invalid
        assert broken["rules"]["status"] == "invalid" and broken["rules"]["detail"]
        assert broken["rules"]["commands"] == []
        assert {"kind": "fix_rules"} in broken["next_steps"]
        assert {"kind": "create_goal"} in broken["next_steps"]
        assert files.read_text() == "locally edited rules"
        assert web._git(repository, "rev-parse", "HEAD") == current
        assert web._request(service, "GET", "/api/jobs")[1]["items"] == []
        assert web._records(capture) == []


def test_provider_check_uses_the_latest_observation(repository, tmp_path, monkeypatch):
    binaries = {"codex": sys.executable, "claude": str(tmp_path / "absent-cli")}
    outcomes = iter(
        (
            ({"cli_version": "fixture", "logged_in": True}, None),
            ({}, {"code": "environment_check_failed", "message": "test failure"}),
            ({"cli_version": "fixture", "logged_in": False}, None),
        )
    )
    monkeypatch.setattr(EnvironmentJobs, "_observe", lambda *_: next(outcomes))
    with web._serve(repository, tmp_path / "data", binaries) as service:

        def view():
            status, data = web._request(service, "GET", "/api/workspace/onboarding")
            assert status == 200
            return data

        start = view()
        assert start["providers"]["codex"]["status"] == "unknown"
        assert start["providers"]["claude"]["status"] == "missing_cli"
        assert {"kind": "check_provider", "provider": "codex"} in start["next_steps"]
        assert {"kind": "install_cli", "provider": "claude"} in start["next_steps"]
        for result in ("ready", "check_failed", "login_required"):
            code, job = web._refresh(service, "codex")
            assert code == 202
            web._wait_for(partial(web._finished, service, job["job_id"]))
            latest = view()
            check = latest["providers"]["codex"]
            assert check["status"] == result
            assert check["check_job_id"] == job["job_id"] and check["observed_at"]

        assert {"kind": "sign_in", "provider": "codex"} in latest["next_steps"]
        assert {"kind": "create_goal"} in latest["next_steps"]
        original = "Record a goal before setting up an advisor."
        confirmed = _job(
            service,
            "project.confirm",
            baseline=latest["baseline"],
            original_goal=original,
            confirmed_goal=original,
        )["result"]
        assert confirmed["confirmed_goal"] == original
        assert confirmed["baseline_commit"] == latest["baseline"]
