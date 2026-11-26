"""Round-bound user annotations and read-only rework inspection."""

from dataclasses import asdict, replace

import pytest
import test_task_workflow_cli as helpers
from test_integration_cli import create_args
from test_integration_rework_cli import _parallel_members, _reviewers, _structural_suite
from test_integration_verification_cli import (
    _arguments,
    _review_arguments,
    _test_arguments,
)
from test_planning_store import inputs
from test_web_project_flow import _get, _job
from test_web_project_resources import _repository
from test_web_workspace import _serve

from multi_agent.cli.review_findings import resolve_finding
from multi_agent.cli.snapshot import freeze_diff, freeze_repository
from multi_agent.core import RoundState, SeatConfig, SeatState
from multi_agent.core.events import AttemptStarted, Completed, Envelope
from multi_agent.store.database import archive_reads
from multi_agent.store.planning_store import PlanningStore
from multi_agent.store.review_annotations import FindingAnchor
from multi_agent.web.review_findings import (
    findings,
    record_decision,
    rework_eligibility,
)
from multi_agent.web.workspace import Workspace

workflow = helpers.workflow


def _review(workspace, snapshot):
    seats = tuple(
        SeatState(SeatConfig(key, "codex", key, "high")) for key in ("one", "two")
    )
    report = helpers.blocking_report()
    report["findings"][0].update(
        path="a.txt", quote="candidate", line_start=1, line_end=1
    )
    with PlanningStore(workspace.database) as store:
        store.save_snapshot(snapshot.snapshot_id, snapshot.to_dict())
        store.create_review(
            "round",
            RoundState(snapshot.snapshot_id, "prompt", seats),
            {"kind": "review"},
        )
        for key in ("one", "two"):
            store.apply("round", Envelope(key, 1, AttemptStarted()))
            store.apply("round", Envelope(key, 1, Completed(report)))


@pytest.mark.parametrize("pasted", (False, True))
def test_decisions_and_linked_plans_are_bound_to_the_reviewed_evidence(
    tmp_path, pasted
):
    repo, baseline, candidate = _repository(tmp_path / "project")
    ws = Workspace.open(repo, tmp_path / "data")
    snapshot = (
        freeze_diff("pasted patch", "fix a", ("works",), repository=ws.repository)
        if pasted
        else freeze_repository(ws.repository, baseline, candidate, "fix a", ("works",))
    )
    _review(ws, snapshot)
    original = findings(ws, "round")
    (group,) = original["groups"]
    assert group["independent_reports"] == {"one": "reported", "two": "reported"}
    assert original["source"]["correction_baseline"] == (None if pasted else candidate)
    member, other = group["members"]
    assert member["finding"]["reference_status"] == (
        "unverified" if pasted else "verified"
    )
    anchor = FindingAnchor.from_dict(member["anchor"])
    decision = {
        "anchor": asdict(anchor),
        "decision": "disputed",
        "reason": "Compare against the frozen line.",
    }
    entry = record_decision(ws, "round", decision)
    assert entry["reason"] == decision["reason"]
    with pytest.raises(ValueError, match="attempt"):
        record_decision(
            ws, "round", {**decision, "anchor": asdict(replace(anchor, attempt=2))}
        )
    other_repo, _, _ = _repository(tmp_path / "foreign")
    foreign = Workspace.open(other_repo, ws.data_dir)
    with pytest.raises(KeyError):
        record_decision(foreign, "round", decision)
    value, _ = inputs()
    rules = replace(
        value.frozen_rules,
        repository=str(ws.repository / ".git"),
        baseline_commit=candidate,
    )
    with PlanningStore(ws.database) as store:
        finding, seat = resolve_finding(store, anchor)
        assert finding == member["finding"] and seat.key == member["seat"]
        store.record_finding_answer(
            anchor,
            "What does this line do?",
            "It outputs candidate.",
            {"provider": "codex"},
        )
        store.create_planning(
            "plan", replace(value, frozen_rules=rules), "now", review_anchor=anchor
        )
        with pytest.raises(ValueError, match="repository"):
            store.create_planning("wrong", value, "now", review_anchor=anchor)
        with pytest.raises(KeyError):
            store.load_planning("wrong")
        if not pasted:
            with pytest.raises(ValueError, match="baseline"):
                store.create_planning(
                    "moved",
                    replace(
                        value, frozen_rules=replace(rules, baseline_commit=baseline)
                    ),
                    "now",
                    review_anchor=anchor,
                )
    updated = findings(ws, "round")
    assert (
        updated["status"] == original["status"]
        and updated["reasons"] == original["reasons"]
    )
    current, untouched = updated["groups"][0]["members"]
    assert untouched == other
    assert [item["kind"] for item in current["activity"]] == [
        "decision",
        "answer",
        "linked_plan",
    ]
    assert current["activity"][-1]["planning_run_id"] == "plan"
    archive = Workspace.open(
        ws.repository, tmp_path / "archive-view", read_only_database=ws.database
    )
    with archive_reads(archive.archive):
        assert findings(archive, "round") == updated
        with pytest.raises(ValueError, match="read-only"):
            record_decision(archive, "round", decision)


def test_rework_eligibility_matches_manual_rework(workflow):
    workspace = Workspace.open(workflow.repository, workflow.base)
    workflow.database = workspace.database
    test_bin = _structural_suite(workflow)
    members, _, _ = _parallel_members(workflow)
    parent = helpers.successful(*create_args(workflow, *members))
    integration = parent["integration_id"]
    before = rework_eligibility(workspace, integration)
    assert not before["eligible"] and before["round_id"] is None
    helpers.successful(*_test_arguments(workflow, parent, test_bin, "inspection-tests"))
    reviewers = _reviewers(workflow, parent["candidate"]["commit"])

    def review(label):
        return helpers.successful(
            *_review_arguments(workflow, parent, reviewers, label, dual=True),
            "--no-auto-rework",
            code=1,
        )["review"]

    def refused(round_id):
        eligibility = rework_eligibility(workspace, integration)
        assert eligibility["round_id"] == round_id
        assert not eligibility["eligible"] and eligibility["owner"] is None
        actual = helpers.successful(
            *_arguments(workflow, "rework", parent, "--review", round_id), code=1
        )["rework"]
        assert actual == {"status": "user_decision", "reason": eligibility["reason"]}
        return eligibility

    second = helpers.blocking_report()
    second["findings"][0].update(
        path="frontend/other.js", quote=helpers.RETAINED_BYTES.decode().rstrip()
    )
    helpers.configure(reviewers["claude"], report=second)
    split = refused(review("split-review")["round_id"])
    assert "different members" in split["reason"]
    assert {item["owners"][0]["task_run_id"] for item in split["findings"]} == {
        member["task_run_id"] for member in members
    }

    helpers.configure(reviewers["claude"], report={"summary": "Clear", "findings": []})
    current = review("inspection-review")
    round_id = current["round_id"]
    ready = rework_eligibility(workspace, integration)
    assert ready["round_id"] == round_id
    assert (
        ready["eligible"] and ready["owner"]["task_run_id"] == members[0]["task_run_id"]
    )
    assert ready["findings"][0]["owners"] == [ready["owner"]]

    programs = {key: str(path) for key, path in workflow.executables.items()}
    helpers.configure(
        workflow.executables["codex"],
        role="review",
        report={"answer": "See status.js."},
    )
    with _serve(workflow.repository, workflow.base, programs) as service:
        anchor = _get(service, f"reviews/{round_id}/findings")["groups"][0]["members"][
            0
        ]["anchor"]
        _job(service, "review.followup", review_anchor=anchor, question="Which file?")
        [registered] = _get(service, "runs?kind=review")["items"]
        assert registered["id"] == round_id and registered["parent_id"] == integration
        assert "scope" not in registered

    directory = workflow.base / "inspection-review"
    parked = directory.rename(workflow.base / "moved-review")
    assert str(directory) in refused(round_id)["reason"]
    parked.rename(directory)

    helpers.configure(
        reviewers["claude"],
        report={
            "summary": "Needs a decision",
            "confirmations": [
                {
                    "target_seat": "seat-1",
                    "finding_id": "status-blocker",
                    "decision": "uncertain",
                    "reason": "Intent unclear.",
                }
            ],
        },
    )
    confirmation = helpers.successful(
        *_arguments(workflow, "confirm", parent, "--review", round_id), code=1
    )
    assert (
        "blocking_confirmation_unresolved"
        in confirmation["review"]["assessment"]["reasons"]
    )
    assert "requires resolution" in refused(round_id)["reason"]
    helpers.git(
        workflow.repository, "commit", "--allow-empty", "--quiet", "-m", "Move target"
    )
    moved = rework_eligibility(workspace, integration)
    assert not moved["eligible"] and "moved" in moved["reason"]
