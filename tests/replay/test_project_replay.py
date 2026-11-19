"""Finish independent planning versions out of order and reopen their history."""

import json
from contextlib import nullcontext
from pathlib import Path

import pytest

from multi_agent.core.planning import PlanningRequest
from multi_agent.core.project_plan import ConfirmedGoal
from multi_agent.store.project_store import ProjectStore


def test_project_version_history_replay(tmp_path):
    scenario = json.loads(
        (
            Path(__file__).parent / "scenarios" / "project" / "version_history.json"
        ).read_bytes()
    )
    path = tmp_path / "project.sqlite"
    with ProjectStore(path) as store:
        for index, name in enumerate(("one", "two")):
            store.confirm_goal(
                ConfirmedGoal(
                    "project-replay",
                    f"goal-{name}",
                    "/repo",
                    "/repo/.git",
                    "a" * 40,
                    "original",
                    f"confirmed-{name}",
                    f"0{index}",
                ),
                new_project=index == 0,
            )
        for position, event in enumerate(scenario["events"]):
            plan_id = event["plan"]
            if event["action"] == "start":
                store.start_plan(
                    plan_id,
                    event["goal"],
                    PlanningRequest("codex", "test", "low"),
                    "snapshot",
                    "v1",
                    str(position),
                )
                assert store.show_plan(plan_id)["status"] == "running"
                continue
            succeeds = event["result"] == "succeeded"
            document = {
                "title": "Recorded result",
                "architecture": "Architecture",
                "shared_interfaces": [],
                "stages": [],
            }

            expected = (
                pytest.raises(ValueError, match="already has a terminal result")
                if event.get("rejected")
                else nullcontext()
            )
            with expected:
                store.finish_plan(
                    plan_id,
                    str(position),
                    document=document if succeeds else None,
                    error=None if succeeds else {"detail": "runtime stopped"},
                    evidence={},
                )
    with ProjectStore(path) as store:
        history = store.show_project("project-replay")["plans"]
        assert [
            {field: record[field] for field in ("plan_id", "goal_id", "status")}
            for record in history
        ] == scenario["history"]
        assert store.show_plan("plan-one")["error"] == {"detail": "runtime stopped"}
        assert store.show_plan("plan-two")["document"]["title"] == "Recorded result"


def test_project_stage_reservation_replay(tmp_path):
    import sqlite3

    from multi_agent.core.planning import PlanningInput
    from multi_agent.core.project_plan import StageRun
    from multi_agent.core.tasks import FrozenProjectRules, ProjectRules

    fixture = Path(__file__).parent / "scenarios" / "project" / "direct_stage.json"
    scenario = json.loads(fixture.read_bytes())
    path = tmp_path / "stages.sqlite"
    draft = PlanningRequest("codex", "gpt-6.2-sol", "high")
    review = PlanningRequest("claude", "claude-opus-5-5", "high")
    baseline = "a" * 40
    planning = PlanningInput(
        "Selected project stage",
        FrozenProjectRules(
            "/repo/.git", baseline, baseline, "raw", ProjectRules(1, (), (), (), None)
        ),
        draft,
        review,
    )
    with ProjectStore(path) as store:
        store.confirm_goal(
            ConfirmedGoal(
                "project-replay",
                "goal-one",
                "/repo",
                "/repo/.git",
                baseline,
                "original",
                "confirmed",
                "00",
            ),
            new_project=True,
        )
        store.start_plan("plan-one", "goal-one", draft, "snapshot", "v1", "01")
        for position, event in enumerate(scenario["events"]):
            stage = StageRun(
                event["stage_run_id"],
                "plan-one",
                event["stage_id"],
                event["planning_run_id"],
                "direct",
                baseline,
                (),
                str(position),
            )
            outcome = (
                nullcontext()
                if event["accepted"]
                else pytest.raises(sqlite3.IntegrityError)
            )
            with outcome:
                store.reserve_stage(stage, planning)
            if not event["accepted"]:
                with pytest.raises(KeyError):
                    store.load_planning(stage.planning_run_id)
    with ProjectStore(path) as store:
        history = store.list_stages("plan-one")
        assert [item.stage_run_id for item in history] == scenario["history"]
        assert all(
            store.load_planning(item.planning_run_id).planning_input == planning
            for item in history
        )
