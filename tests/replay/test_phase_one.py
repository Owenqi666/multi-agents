"""Replay CLI commands with synthetic seat events, checking state and history."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from multi_agent.adapters.base import EnvironmentInfo
from multi_agent.adapters.scripted import ScriptedAdapter, TurnScript
from multi_agent.cli import main as cli
from multi_agent.cli import runner
from multi_agent.core import Disposition, round_status
from multi_agent.core.events import (
    Completed,
    Envelope,
    Error,
    FailureCategory,
    ProcessExited,
    SessionStarted,
)
from multi_agent.core.snapshot import (
    FrozenFile,
    FrozenSnapshot,
)
from multi_agent.core.snapshot import (
    TestEvidence as EvidenceRecord,
)
from multi_agent.store import ReviewStore

SCENARIOS = Path(__file__).parent / "scenarios" / "phase-one"


def event_script(response):
    terminal = (
        Error("recorded failure", FailureCategory(response["failure"]))
        if "failure" in response
        else Completed({"summary": response["summary"], "findings": []})
    )
    return TurnScript(
        (SessionStarted("replay-session"), terminal, ProcessExited(0)),
        (Error("cancelled", FailureCategory.CANCELLED), ProcessExited(0)),
    )


def replay(scenario, directory, monkeypatch, capsys):
    data = directory / "data"
    data.mkdir(parents=True)
    repo = directory / "repository"
    repo.mkdir()
    command = ("python", "-m", "declared_tests")
    initial = FrozenSnapshot(
        str(repo),
        "a" * 40,
        "b" * 40,
        "-before\n+after\n",
        "Review this fixed change",
        ("Preserve callers",),
        (EvidenceRecord("b" * 40, command, 0, "provided passing log"),),
        (FrozenFile("candidate", "source.py", "after\n", "c" * 40, "100644"),),
        (command,),
    )
    snapshots = {"initial": initial}
    with ReviewStore(data / "reviews.sqlite3") as store:
        store.save_snapshot(initial.snapshot_id, initial.to_dict())
    rounds = {}
    planned = {}
    dispatches = []

    def factory(config, log_dir, metadata, schema):
        dispatches.append(config.key)
        response = planned.pop(config.key)
        return ScriptedAdapter(
            (event_script(response),),
            environment=EnvironmentInfo("replay-cli", True, None),
        )

    monkeypatch.setattr(runner, "make_adapter", factory)

    def invoke(arguments):
        assert cli.main(["--data-dir", str(data), *arguments]) == 0
        captured = capsys.readouterr()
        assert not captured.err
        return json.loads(captured.out)

    for step in scenario["steps"]:
        op = step["op"]
        if op == "snapshot":
            updated = replace(
                initial,
                candidate=step["candidate"],
                requirements="Updated requirement",
                tests=(
                    EvidenceRecord(
                        step["candidate"], command, 0, "new provided passing log"
                    ),
                ),
            )
            snapshots[step["snapshot"]] = updated
            with ReviewStore(data / "reviews.sqlite3") as store:
                store.save_snapshot(updated.snapshot_id, updated.to_dict())
            continue
        if op == "late":
            with ReviewStore(data / "reviews.sqlite3") as store:
                before = store.load_round(rounds[step["round"]])
                disposition = store.apply(
                    rounds[step["round"]],
                    Envelope(
                        step["seat"],
                        step["attempt"],
                        Completed({"summary": step["summary"], "findings": []}),
                    ),
                )
                assert disposition == Disposition.HISTORY_ONLY
                assert store.load_round(rounds[step["round"]]) == before
            continue
        previous_dispatches = len(dispatches)
        planned = dict(step.get("responses", {}))
        if op == "run":
            args = [
                "run",
                snapshots[step["snapshot"]].snapshot_id,
                "--seat",
                "codex",
                "model-one",
                "high",
            ]
            if step["dual"]:
                args += ["--seat", "claude", "model-two", "high"]
            output = invoke(args)
            rounds[step["round"]] = output["round_id"]
        elif op == "retry":
            output = invoke(["retry", rounds[step["round"]]])
        else:
            assert op == "show"
            output = invoke(["show", rounds[step["round"]], "--history"])
            assert output["history"]
        assert output["status"] == step["expect_status"]
        assert planned == {}
        assert sorted(dispatches[previous_dispatches:]) == sorted(
            step.get("responses", {})
        )

    normalized = {}
    # Reopen after all commands: current attempts and full event history must
    # survive, including the late terminal that was barred from changing state.
    with ReviewStore(data / "reviews.sqlite3") as store:
        for label, expected in scenario["expected"].items():
            state = store.load_round(rounds[label])
            history = store.load_history(rounds[label])
            assert state.snapshot_id == snapshots[expected["snapshot"]].snapshot_id
            assert round_status(state) == "ready_for_assessment"
            assert {
                seat.config.key: seat.current_attempt for seat in state.seats
            } == expected["attempts"]
            summaries = {
                seat.config.key: seat.attempts[-1].result["summary"]
                for seat in state.seats
            }
            assert summaries == expected["current_summaries"]
            completed = [
                envelope.event.result["summary"]
                for envelope, _ in history
                if isinstance(envelope.event, Completed)
            ]
            errors = [
                envelope.event.failure_category
                for envelope, _ in history
                if isinstance(envelope.event, Error)
            ]
            assert sorted(completed) == sorted(expected["completed_summaries"])
            assert errors == expected["failure_categories"]
            assert (
                sum(
                    disposition == Disposition.HISTORY_ONLY
                    for _, disposition in history
                )
                == expected["history_only"]
            )
            normalized[label] = {
                "summaries": summaries,
                "history": [
                    (event.seat, event.attempt, event.event.kind, disposition)
                    for event, disposition in history
                ],
            }
    return normalized


@pytest.mark.parametrize(
    "path", sorted(SCENARIOS.glob("*.json")), ids=lambda path: path.stem
)
def test_phase_one_scenario(path, tmp_path, monkeypatch, capsys):
    scenario = json.loads(path.read_text())
    assert replay(scenario, tmp_path / "first", monkeypatch, capsys) == replay(
        scenario, tmp_path / "second", monkeypatch, capsys
    )
