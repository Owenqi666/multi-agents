"""Replay consultation interruption without inventing seat events or results."""

import json
from contextlib import nullcontext
from pathlib import Path

import pytest

from multi_agent.core.planning import PlanningRequest
from multi_agent.store.project_store import ProjectStore


def test_consultation_owner_recovery_replay(tmp_path):
    scenario = json.loads(
        (
            Path(__file__).parent / "scenarios/project/consultation_owner_recovery.json"
        ).read_bytes()
    )
    database = tmp_path / "consultation.sqlite3"
    owner = (3, 5)
    evidence = {"reported_model": "model-observed", "reported_effort": None}
    with ProjectStore(database) as store:
        store.save_snapshot("source", {"baseline": "a" * 40})
        for identifier in scenario["expected"]:
            store.start_consultation(
                identifier,
                "/repo",
                "/repo/.git",
                "a" * 40,
                "Original",
                None,
                "source",
                (
                    PlanningRequest("codex", "astra", "high"),
                    PlanningRequest("claude", "fable", "high"),
                ),
                "00",
                owner=owner,
            )
        for event in scenario["events"]:
            identifier, timestamp = event["consultation"], event["at"]
            rejected = event.get("rejected")
            with (
                pytest.raises(ValueError, match=rejected) if rejected else nullcontext()
            ):
                if event["action"] == "reserve":
                    store.reserve_consultation_seat(
                        identifier, event["seat"], "advice-v1", timestamp
                    )
                    continue
                if event["action"] == "interrupt":
                    disposition = store.reconcile_consultation(
                        identifier, owner, timestamp
                    )
                else:
                    disposition = store.finish_consultation_seat(
                        identifier,
                        event["seat"],
                        timestamp,
                        advice={"draft_goal": event["advice"]},
                        error=None,
                        evidence=evidence,
                    )
                assert disposition == event["disposition"]

    with ProjectStore.open_readonly(database) as store:
        for identifier, expected in scenario["expected"].items():
            shown = store.show_consultation(identifier)
            assert shown["status"] == "failed" and shown["draft"] is None
            assert shown["interrupted_at"] == expected["interrupted_at"]
            first, second = shown["seats"]
            assert first["status"] == "succeeded"
            assert first["finished_at"] == "02"
            assert first["advice"] == {"draft_goal": "First draft"}
            assert first["evidence"] == evidence
            assert second["advice"] is None and second["evidence"] is None
            assert {
                **{key: second[key] for key in ("status", "started_at", "finished_at")},
                "error": second["error"]["raw_type"] if second["error"] else None,
            } == expected["second"]
            late = shown.get("late_results", [])
            assert [
                {
                    "number": event["number"],
                    "finished_at": event["finished_at"],
                    "draft": event["advice"]["draft_goal"],
                }
                for event in late
            ] == expected["late"]
            assert all(event["evidence"] == evidence for event in late)
