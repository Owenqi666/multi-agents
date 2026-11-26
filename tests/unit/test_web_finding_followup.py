"""Exercise the finding-to-answer and finding-to-correction HTTP paths."""

import json
from dataclasses import replace

import test_task_workflow_cli as helpers
from test_reports import finding, report, snapshot
from test_web_independent_review import INPUT
from test_web_project_flow import FABLE, OPUS, _get, _job, _request
from test_web_workspace import _serve

from multi_agent.cli.finding_followup import _followup_prompt

workflow = helpers.workflow


def test_followup_prompt_delivers_frozen_evidence_to_the_reviewer():
    patch = "diff --git a/value.py b/value.py\n--- a/value.py\n+++ b/value.py\n@@ -1 +1 @@\n-old\n+new"
    source = replace(snapshot(), diff=("unrelated file\n" * 1024) + patch)
    prompt = _followup_prompt(source, finding(), "What happens for zero?")
    data = json.loads(prompt.split("\n", 1)[1])
    assert patch in data["patch_preview"] and data["patch_truncated"]
    assert data["source_excerpt"] == {
        "first_line": 1,
        "path": "source/candidate/value.py",
        "text": "def f(x):\n    return 1 / x\n",
    }
    pasted = replace(source, files=(), diff=INPUT["diff"])
    data = json.loads(_followup_prompt(pasted, finding(), "?").split("\n", 1)[1])
    assert data["source_excerpt"] is None
    assert data["patch_preview"] == INPUT["diff"] and not data["patch_truncated"]


def test_large_pasted_patch_preview_locates_the_cited_side_and_line():
    # Inserting 1,000 lines puts candidate line 450 and baseline line 450 in different hunks.
    first_hunk = "@@ -1 +1,1001 @@\n first line\n" + "".join(
        f"+candidate line {line:04d}\u2028+embedded fragment\n"
        for line in range(2, 1002)
    )
    second_hunk = "@@ -450 +1450 @@\n-baseline line 450\n+replacement\n"
    source = replace(
        snapshot(),
        files=(),
        diff=(
            "diff --git a/other b/other\n--- a/other\n+++ b/other\n@@ -450 +450 @@\n-other\n+other\n"
            'diff --git a/old.py "b/\\346\\226\\260.py"\n--- a/old.py\n+++ "b/\\346\\226\\260.py"\n'
            + first_hunk
            + second_hunk
        ),
    )
    for side, path, expected, excluded in (
        ("candidate", "新.py", "+candidate line 0450", "-baseline line 450"),
        ("baseline", "old.py", second_hunk, "+candidate line 0450"),
    ):
        location = finding(side=side, path=path, line_start=450, line_end=450)
        prompt = _followup_prompt(source, location, "Why?")
        data = json.loads(prompt.split("\n", 1)[1])
        assert data["patch_truncated"] and data["source_excerpt"] is None
        assert len(data["patch_preview"]) <= 8192
        assert data["patch_preview"].startswith(expected)
        assert excluded not in data["patch_preview"]
        assert "diff.patch" in prompt and "review-input.json" in prompt


def test_followup_and_linked_plan_share_the_original_finding(workflow):
    programs = {key: str(path) for key, path in workflow.executables.items()}
    claude = workflow.executables["claude"]
    helpers.configure(claude, role="review", report=report(finding()))
    with _serve(workflow.repository, workflow.base / "data", programs) as service:
        frozen = _job(service, "review.freeze", **INPUT)["result"]
        review = _job(
            service,
            "review.run",
            snapshot_id=frozen["snapshot_id"],
            seats=[FABLE, OPUS],
        )["result"]
        path = f"reviews/{review['round_id']}/findings"
        member, other = _get(service, path)["groups"][0]["members"]
        anchor = member["anchor"]
        status, decision = _request(
            service,
            "POST",
            f"/api/reviews/{review['round_id']}/decisions",
            {"anchor": anchor, "decision": "deferred", "reason": "Check zero first."},
        )
        assert status == 201 and decision["kind"] == "decision"
        helpers.configure(claude, report={"answer": "Zero input triggers the finding."})
        answer = _job(
            service,
            "review.followup",
            review_anchor=anchor,
            question="When does this happen?",
        )
        assert answer["result"]["entry"]["answer"] == "Zero input triggers the finding."
        assert answer["args"]["question"] == {"length": len("When does this happen?")}
        # A failed planning turn still creates a real, retryable correction linked to the finding.
        helpers.configure(claude, role="planning", mode="model-error")
        plan = _job(
            service,
            "plan.create",
            review_anchor=anchor,
            baseline=workflow.baseline,
            requirements=helpers.REQUIREMENTS,
            exit_code=1,
        )["result"]
        updated = _get(service, path)
        current, untouched = updated["groups"][0]["members"]
        assert untouched == other
        entries = current["activity"]
        assert [entry["kind"] for entry in entries] == [
            "decision",
            "answer",
            "linked_plan",
        ]
        assert entries[-1]["planning_run_id"] == plan["planning_run_id"]
        assert updated["status"] == review["assessment"]["status"]
