"""Replay an ordered consultation and retain each immutable terminal result."""

import json
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path

import pytest

from multi_agent.core.planning import PlanningRequest
from multi_agent.core.project_plan import ConfirmedGoal
from multi_agent.store.project_store import ProjectStore


def test_project_consultation_replay(tmp_path):
    fixture = Path(__file__).parent / "scenarios" / "project" / "goal_consultation.json"
    scenario = json.loads(fixture.read_bytes())
    database = tmp_path / "consultation.sqlite"
    baseline, snapshot_id = "a" * 40, "same-frozen-source"
    original = " PM 的原话。\r\n"
    advisors = (
        PlanningRequest("codex", "gpt-6.2-astra", "low"),
        PlanningRequest("claude", "claude-fable-5-2", "high"),
    )
    evidence = {"reported_model": None, "reported_effort": None}
    with ProjectStore(database) as store:
        store.save_snapshot(snapshot_id, {"baseline": baseline, "original": original})
        for name in scenario["history"]:
            store.start_consultation(
                name,
                "/repo",
                "/repo/.git",
                baseline,
                original,
                None,
                snapshot_id,
                advisors,
                "00",
            )
            assert store.show_consultation(name)["status"] == "running"
        for counter, event in enumerate(scenario["events"], 1):
            key = event["consultation"]
            number = event["seat"]
            timestamp = f"{counter:02}"
            rejected = event.get("rejected")
            expectation = (
                pytest.raises(ValueError, match=rejected) if rejected else nullcontext()
            )
            with expectation:
                if event["action"] == "reserve":
                    store.reserve_consultation_seat(key, number, "advice-v1", timestamp)
                else:
                    store.finish_consultation_seat(
                        key,
                        number,
                        timestamp,
                        advice={"draft_goal": event["advice"]}
                        if "advice" in event
                        else None,
                        error={"detail": event["error"]} if "error" in event else None,
                        evidence=evidence,
                    )
    with ProjectStore(database) as store:
        for name, expected in scenario["history"].items():
            shown = store.show_consultation(name)
            assert shown["status"] == expected["status"]
            assert shown["draft"] == expected["draft"]
            assert shown["original_goal"] == original
            assert shown["baseline_commit"] == baseline
            assert [seat["number"] for seat in shown["seats"]] == [1, 2]
            assert [
                {field: seat[field] for field in ("status", "advice", "error")}
                for seat in shown["seats"]
            ] == expected["seats"]
            assert all(
                seat["source_snapshot_id"] == snapshot_id
                and seat["evidence"] == evidence
                for seat in shown["seats"]
            )

        goal = ConfirmedGoal(
            "project-pm",
            "goal-edited",
            "/repo",
            "/repo/.git",
            baseline,
            original,
            "PM edited the draft",
            "12",
        )
        with pytest.raises(ValueError, match="successful consultation"):
            store.confirm_goal(goal, new_project=True, consultation_id="incomplete")
        with pytest.raises(ValueError, match="edited adoption decision"):
            store.confirm_goal(
                goal, new_project=True, consultation_id="complete", adoption="accepted"
            )
        store.confirm_goal(goal, new_project=True, consultation_id="complete")
        with pytest.raises(ValueError, match="different project"):
            store.confirm_goal(
                replace(goal, project_id="other", goal_id="other-goal"),
                new_project=True,
                consultation_id="complete",
            )
        assert store.show_consultation("complete")["project_id"] is None
        assert store.show_goal(goal.goal_id)["consultation"] == {
            "consultation_id": "complete",
            "adoption": "edited",
            "source_snapshot_id": snapshot_id,
        }
        assert store.load_goal(goal.goal_id) == goal
        accepted = replace(
            goal, goal_id="goal-accepted", confirmed_goal="Reviewed draft"
        )
        store.confirm_goal(
            accepted, new_project=False, consultation_id="complete", adoption="accepted"
        )
        assert (
            store.show_goal(accepted.goal_id)["consultation"]["adoption"] == "accepted"
        )
