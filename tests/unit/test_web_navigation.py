"""Check workspace navigation data exposed by the loopback API."""

import json
import sys

from test_web_project_flow import _get, _job
from test_web_workspace import _commit_all, _git, _serve, _workspace

from multi_agent.web import server


def test_web_navigation_workspace_branches_and_defaults(tmp_path):
    repository = tmp_path / "repository"
    _git(tmp_path, "init", "--quiet", "--initial-branch=main", str(repository))
    (repository / "README.md").write_text("Initial commit.\n")
    baseline = _commit_all(repository)
    _git(repository, "branch", "release/candidate")
    _git(repository, "update-ref", "refs/remotes/origin/main", baseline)
    (repository / "README.md").write_text("New main commit.\n")
    new_head = _commit_all(repository)
    _git(repository, "checkout", "--quiet", "--detach")

    binaries = {"codex": "codex", "claude": "claude"}
    with _serve(repository, tmp_path / "data", binaries) as service:
        value = _workspace(service)
        assert value["target_branch"] is None
        assert value["head"] == new_head
        assert value["branches"] == [
            {"name": "main", "head": new_head},
            {"name": "release/candidate", "head": baseline},
        ]
        assert value["defaults"] == {
            "source": "cli_default",
            "stage_mode": "direct",
            "task_plan": {
                "draft": {
                    "provider": "claude",
                    "model": "claude-opus-5-5",
                    "effort": "high",
                },
                "plan_review": {
                    "provider": "codex",
                    "model": "gpt-6.1-sol",
                    "effort": "high",
                },
            },
            "sol": {"model": "gpt-6.2-sol", "effort": "high"},
            "card_review": {
                "provider": "claude",
                "model": "claude-opus-5-5",
                "effort": "high",
            },
        }


def test_web_navigation_unborn_repository_has_no_local_branches(tmp_path):
    repository = tmp_path / "repository"
    _git(tmp_path, "init", "--quiet", "--initial-branch=main", str(repository))
    binaries = {"codex": "codex", "claude": "claude"}
    with _serve(repository, tmp_path / "data", binaries) as service:
        value = _workspace(service)
        assert value["target_branch"] == "main"
        assert value["head"] is None
        assert value["branches"] == []


def test_web_navigation_runs_labels_and_review_history_survive_restart(
    tmp_path, monkeypatch
):
    repository = tmp_path / "repository"
    _git(tmp_path, "init", "--quiet", "--initial-branch=main", str(repository))
    data = tmp_path / "data"
    binaries = {"codex": "codex", "claude": "claude"}
    results = {
        "project.confirm": {
            "goal_id": "goal-current",
            "confirmed_goal": "   First confirmed line\nMore details.\n",
        },
        "task.run": {
            "task_run_id": "task-leaf",
            "details": {"input": {"card": {"id": "T-0800", "goal": "Deliver status"}}},
        },
        "task.confirm": {
            "task_run_id": "task-leaf",
            "round_id": "round-rechecked",
            "workflow": {
                "source_review_round_id": "round-original",
                "final_review_round_id": "round-rechecked",
                "rework": {
                    "task_run_id": "task-leaf",
                    "reviews": [
                        {
                            "round_id": "round-original",
                            "confirmation_round_id": "round-confirmation",
                        }
                    ],
                },
            },
        },
        "integration.rework": {
            "integration_id": "integration-original",
            "revision": {
                "integration_id": "integration-revised",
                "target_ref": "refs/heads/release/stable",
            },
            "rework": {
                "source_id": "integration-original",
                "child_id": "integration-revised",
                "request": {"review_round_id": "integration-original-review"},
            },
        },
        "integration.rebase": {
            "integration_id": "integration-rebased",
            "rebased_from": "integration-revised",
            "target_ref": "refs/heads/release/stable",
        },
        "integration.test": {"integration_id": "integration-revised"},
    }

    def prepare(action, _args, **_options):
        output = json.dumps(results[action])
        return [sys.executable, "-c", f"print({output!r})"]

    monkeypatch.setattr(server, "prepare_action", prepare)
    with _serve(repository, data, binaries) as service:
        for action in results:
            _job(service, action)
        entries = _get(service, "runs")["items"]
        runs = {(item["kind"], item["id"]): item for item in entries}
        assert runs["goal", "goal-current"]["label"] == "First confirmed line"
        assert runs["task", "task-leaf"]["label"] == "T-0800"
        for round_id in ("round-original", "round-confirmation", "round-rechecked"):
            assert runs["task-review", round_id]["parent_id"] == "task-leaf"
            assert runs["task-review", round_id]["label"] == "Task review"
        assert (
            runs["review", "integration-original-review"]["parent_id"]
            == "integration-original"
        )
        assert runs["integration", "integration-original"]["parent_id"] is None
        for child, parent in (
            ("integration-revised", "integration-original"),
            ("integration-rebased", "integration-revised"),
        ):
            assert runs["integration", child]["label"] == "release/stable"
            assert runs["integration", child]["parent_id"] == parent

    with _serve(repository, data, binaries) as service:
        assert _get(service, "runs?kind=integration")["items"] == [
            item for item in entries if item["kind"] == "integration"
        ]


def test_web_navigation_upgrades_legacy_runs_without_replacing_existing_times(tmp_path):
    repository = tmp_path / "repository"
    _git(tmp_path, "init", "--quiet", "--initial-branch=main", str(repository))
    data = tmp_path / "data"
    with _serve(repository, data, {}):
        pass

    original_time = "2026-11-19T10:00:00+00:00"
    rework_time = "2026-11-19T11:00:00+00:00"
    state_path = data / "web-environment" / "state.json"
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    saved.pop("runs_version")
    saved["runs"] = [
        {
            "kind": "integration",
            "id": identifier,
            "label": identifier,
            "parent_id": None,
            "updated_at": timestamp,
        }
        for identifier, timestamp in (
            ("integration-original", original_time),
            ("integration-revised", rework_time),
        )
    ]
    saved["jobs"]["legacy-rework"] = {
        "action": "integration.rework",
        "state": "finished",
        "resources": [{"kind": "integration", "id": "integration-revised"}],
        "result": {
            "revision": {
                "integration_id": "integration-revised",
                "target_ref": "refs/heads/main",
            },
            "rework": {
                "source_id": "integration-original",
                "child_id": "integration-revised",
                "request": {"review_round_id": "original-review"},
            },
        },
    }
    state_path.write_text(json.dumps(saved), encoding="utf-8")

    with _serve(repository, data, {}) as service:
        upgraded = _get(service, "runs")["items"]
    assert upgraded == [
        {
            "kind": "integration",
            "id": "integration-revised",
            "label": "main",
            "parent_id": "integration-original",
            "updated_at": rework_time,
        },
        {
            "kind": "review",
            "id": "original-review",
            "label": "Review",
            "parent_id": "integration-original",
            "updated_at": rework_time,
        },
        {
            "kind": "integration",
            "id": "integration-original",
            "label": "Integration",
            "parent_id": None,
            "updated_at": original_time,
        },
    ]
    persisted = state_path.read_bytes()
    assert json.loads(persisted)["runs_version"] == 1
    with _serve(repository, data, {}) as service:
        assert _get(service, "runs")["items"] == upgraded
    assert state_path.read_bytes() == persisted


def test_web_navigation_keeps_project_labels_across_stage_and_integration_jobs(
    tmp_path, monkeypatch
):
    repository = tmp_path / "repository"
    _git(tmp_path, "init", "--quiet", "--initial-branch=main", str(repository))
    goal_text = "Prepare the monthly summary."
    goal = {
        "project_id": "project-a",
        "goal_id": "goal-a",
        "original_goal": "Prepare a report.",
        "confirmed_goal": f"{goal_text}\nRun both named checks.",
    }
    stage = {"kind": "project_stage", "plan_id": "plan-a", "goal_id": "goal-a"}
    responses = [
        ("project.confirm", {"kind": "confirmed_goal", **goal}),
        (
            "project.plan",
            {"kind": "project_plan", "plan_id": "plan-a", "goal": goal},
        ),
        (
            "project.stage.start",
            {
                **stage,
                "mode": "direct",
                "stage_id": "amount",
                "stage_run_id": "stage-amount",
                "planning_run_id": "direct-plan",
                "task_run_id": "direct-task",
                "planning": {"card": {"id": "T-0799"}},
                "implementation": {
                    "planning_run_id": "direct-plan",
                    "task_run_id": "direct-task",
                    "details": {"input": {"card": {"id": "T-0799"}}},
                },
            },
        ),
        (
            "project.stage.start",
            {
                **stage,
                "mode": "delegate",
                "stage_id": "summary",
                "stage_run_id": "stage-summary",
                "planning_run_id": "split-plan",
                "planning": {
                    "card": None,
                    "planning_kind": "stage_split",
                    "details": {
                        "input": {
                            "requirements": "Split only selected_stage into executable task cards."
                        }
                    },
                },
            },
        ),
        (
            "project.stage.leaf",
            {
                "kind": "project_leaf",
                "plan_id": "plan-a",
                "stage_id": "summary",
                "stage_run_id": "stage-summary",
                "card_id": "T-0800",
                "planning_run_id": "status-plan",
                "planning": {"card": {"id": "T-0800"}},
            },
        ),
        (
            "project.stage.integrate",
            {
                "kind": "project_stage_integration",
                "operation": "create",
                "plan_id": "plan-a",
                "stage_id": "summary",
                "stage_run_id": "stage-summary",
                "integration_id": "integration-summary",
                "target_ref": "refs/heads/multi-agent/stages/stage-summary",
            },
        ),
    ]
    pending = iter(responses)

    def prepare(action, _args, **_options):
        expected, result = next(pending)
        assert action == expected
        output = json.dumps(result)
        return [sys.executable, "-c", f"print({output!r})"]

    monkeypatch.setattr(server, "prepare_action", prepare)
    with _serve(repository, tmp_path / "data", {}) as service:
        for index, (action, _) in enumerate(responses):
            _job(service, action)
            entries = _get(service, "runs")["items"]
            labels = {(item["kind"], item["id"]): item["label"] for item in entries}
            assert labels["project", "project-a"] == goal_text
            assert labels["goal", "goal-a"] == goal_text
            if index > 0:
                assert labels["project-plan", "plan-a"] == goal_text

        assert labels["stage", "stage-amount"] == "amount"
        assert labels["stage", "stage-summary"] == "summary"
        assert (
            labels["plan", "direct-plan"] == labels["task", "direct-task"] == "T-0799"
        )
        assert labels["plan", "split-plan"] == "summary"
        assert labels["plan", "status-plan"] == "T-0800"
        assert (
            labels["integration", "integration-summary"]
            == "multi-agent/stages/stage-summary"
        )
