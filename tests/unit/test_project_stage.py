"""Exercise direct delivery through the real project and legacy task CLIs."""

import json
import sys
from copy import deepcopy

import pytest
import test_task_workflow_cli as workflow_helpers

from multi_agent.adapters.project_card_schema import codex_task_card_schema
from multi_agent.cli.planning_prompt import planning_output_schema
from multi_agent.cli.task_inputs import freeze_project_rules
from multi_agent.core.planning import PlanningInput, PlanningRequest
from multi_agent.core.project_plan import StageRun
from multi_agent.store.project_store import ProjectStore

workflow = workflow_helpers.workflow


def _model_card(stage_id, dependencies):
    return {
        "stage_id": stage_id,
        "title": f"Stage {stage_id}",
        "goal": "Set the frontend status to ready",
        "approach": "Update frontend/status.js",
        "workflow": ["Use only the chosen stage and run registered tests"],
        "depends_on": dependencies,
        "acceptance_criteria": ["The registered build and test suite passes"],
        "deliverables": ["The requested status"],
    }


def _planned_project(workflow, stages=None):
    document = {
        "title": "Project",
        "architecture": "One code path",
        "shared_interfaces": ["Shared status field"],
        "stages": stages or [_model_card("write", []), _model_card("later", ["write"])],
    }
    planner = workflow_helpers.executable(workflow, "claude", "review", report=document)
    goal = workflow_helpers.successful(
        "--data-dir",
        workflow.base,
        "project",
        "confirm",
        "--repo",
        workflow.repository,
        "--baseline",
        workflow.baseline,
        "--original-goal",
        workflow.requirements,
        "--confirmed-goal",
        workflow.requirements,
    )
    return workflow_helpers.successful(
        "--data-dir",
        workflow.base,
        "project",
        "plan",
        goal["goal_id"],
        "--planner",
        "claude",
        "claude-opus-5-5",
        "high",
        "--claude-bin",
        planner,
        "--timeout",
        "20",
        "--grace",
        "0.2",
    )


def _direct_inputs(
    workflow, path="frontend/status.js", content=workflow_helpers.FIXED_BYTES
):
    base = workflow.base
    transport = base / "bin" / "codex-transport.py"
    transport.write_text(
        transport.read_text().replace(
            'name = "model-2" if "cursor" in params else "model-1"',
            'name = "model-1" if "cursor" in params else "gpt-6.2-sol"',
        )
    )
    codex = workflow_helpers.executable(
        workflow,
        "codex",
        "stage-direct",
        write_scope=[path],
        writes={path: content.hex()},
    )
    claude = workflow_helpers.executable(workflow, "claude", "stage-direct")
    return (
        "--card-review",
        "claude",
        "claude-opus-5-5",
        "high",
        "--codex-bin",
        codex,
        "--claude-bin",
        claude,
        "--timeout",
        "30",
        "--grace",
        "0.2",
    )


@pytest.mark.parametrize("allowed", [True, False])
def test_project_direct_stage(workflow, allowed):
    if allowed:
        implementation = workflow.rules["implementation"][0]
        implementation.update(
            provider="codex",
            default_model="gpt-6.2-sol",
            allowed_models=["gpt-6.2-sol"],
        )
        (workflow.repository / ".multi-agent/rules.yaml").write_text(
            json.dumps(workflow.rules)
        )
        workflow_helpers.git(workflow.repository, "add", ".multi-agent/rules.yaml")
        workflow_helpers.git(
            workflow.repository, "commit", "--quiet", "-m", "allow Sol"
        )
        workflow.baseline = workflow_helpers.git(
            workflow.repository, "rev-parse", "HEAD"
        )

    plan = _planned_project(workflow)
    options = _direct_inputs(workflow)
    prefix = ("--data-dir", workflow.base, "project", "stage")
    before = workflow_helpers.module_cli(
        *prefix, "start", plan["plan_id"], "later", *options
    )
    assert before.returncode == 2
    assert "stage write has no verified delivery" in before.stderr
    wire = workflow.base / "stage-direct-wire.jsonl"
    assert not wire.exists()

    result = workflow_helpers.successful(
        *prefix,
        "start",
        plan["plan_id"],
        "write",
        *options,
        code=0 if allowed else 1,
    )
    assert result["mode"] == "direct" and result["stage_id"] == "write"
    assert result["plan_id"] == plan["plan_id"]
    assert result["goal_id"] == plan["goal"]["goal_id"]
    assert result["execution_baseline"] == workflow.baseline
    assert not result["tests_passed"] and not result["review_satisfied"]

    captured = workflow_helpers.records(wire)
    plan_inputs = [item for item in captured if item["kind"] == "stage-card-input"]
    assert [(item["provider"], item["draft"]) for item in plan_inputs] == [
        ("codex", True),
        ("claude", False),
    ]
    context = plan_inputs[0]["requirements"]
    assert all(
        key in context
        for key in (
            plan["plan_id"],
            plan["goal"]["goal_id"],
            workflow.baseline,
            "selected_stage",
        )
    )
    implementations = [item for item in captured if item["kind"] == "implementation"]
    if not allowed:
        assert not implementations and result["task_run_id"] is None
        assert "frontend/status.js" in result["dispatch_error"]
        assert ".multi-agent/rules.yaml" in result["dispatch_error"]
        return

    assert len(implementations) == 1 and implementations[0]["provider"] == "codex"
    for value in (
        plan["plan_id"],
        plan["goal"]["goal_id"],
        workflow.baseline,
        "selected_stage",
    ):
        assert value in implementations[0]["prompt"]
    assert result["candidate_verified"]
    task_id, candidate = result["task_run_id"], result["candidate"]
    assert (
        len(workflow_helpers.successful(*prefix, "list", plan["plan_id"])["stages"])
        == 1
    )
    assert (
        workflow_helpers.successful(*prefix, "show", result["stage_run_id"]) == result
    )
    assert (
        workflow_helpers.successful(
            *prefix, "start", plan["plan_id"], "write", *options
        )
        == result
    )
    assert workflow_helpers.records(wire) == captured

    workflow.database = workflow.base / "reviews.sqlite3"
    test_bin = workflow.base / "bin" / "codex-workflow-test"
    test_bin.write_text(
        f"#!{sys.executable}\n" + (workflow_helpers.FAKES / "test_cli.py").read_text()
    )
    test_bin.chmod(0o755)
    test_bin.with_suffix(".json").write_text(
        json.dumps({"capture": str(workflow.base / "test-wire.jsonl")})
    )
    tested = workflow_helpers.successful(
        *workflow_helpers.suite_arguments(workflow, task_id, 1, test_bin)
    )
    test_view = workflow_helpers.successful(*prefix, "show", result["stage_run_id"])
    assert test_view["tests_passed"] and not test_view["review_satisfied"]
    reviewer = workflow_helpers.executable(workflow, "codex", "review")
    reviewed = workflow_helpers.successful(
        *workflow_helpers.review_arguments(
            workflow, task_id, 1, tested["test_attempt"], reviewer
        )
    )
    ready = workflow_helpers.successful(*prefix, "show", result["stage_run_id"])
    assert (
        ready["candidate"] == candidate == tested["candidate"] == reviewed["candidate"]
    )
    assert ready["tests_passed"] and ready["review_satisfied"]
    assert ready["review_round_id"] == reviewed["round_id"]
    assert (
        ready["implementation"]["attempts"][0]["request"]["decision"]["choice"]["model"]
        == "gpt-6.2-sol"
    )

    _check_revised_plan_reuses_unchanged_delivery(workflow, plan, ready, options)

    report = workflow_helpers.blocking_report()
    report["findings"][0]["quote"] = workflow_helpers.FIXED_BYTES.decode().strip()
    workflow_helpers.configure(reviewer, report=report)
    blocked = workflow_helpers.review_arguments(
        workflow, task_id, 1, tested["test_attempt"], reviewer
    )
    blocked[blocked.index("--directory") + 1] = workflow.base / "review-blocking"
    newer = workflow_helpers.successful(*blocked, code=1)
    assert newer["candidate"] == candidate
    newest = workflow_helpers.successful(*prefix, "show", result["stage_run_id"])
    assert newest["tests_passed"] and not newest["review_satisfied"]
    assert newest["review_round_id"] is None


def _check_revised_plan_reuses_unchanged_delivery(workflow, plan, delivered, options):
    prefix = ("--data-dir", workflow.base, "project")
    new_document = deepcopy(plan["document"])
    new_document["stages"][1]["acceptance_criteria"] = [
        "The status reports the next stage"
    ]
    source = workflow.base / "revised-plan.json"
    invalid = deepcopy(new_document)
    invalid["stages"][1]["depends_on"] = ["unknown"]
    source.write_text(json.dumps(invalid))
    denied = workflow_helpers.module_cli(
        *prefix, "revise", plan["plan_id"], "--document", source
    )
    assert denied.returncode == 2 and "does not precede it" in denied.stderr
    history = workflow_helpers.successful(*prefix, "show", plan["goal"]["project_id"])
    assert [item["plan_id"] for item in history["plans"]] == [plan["plan_id"]]

    source.write_text(json.dumps(new_document))
    revised = workflow_helpers.successful(
        *prefix, "revise", plan["plan_id"], "--document", source
    )
    assert revised["source_plan_id"] == plan["plan_id"] and revised["origin"] == "pm"
    assert revised["goal"] == plan["goal"] and revised["evidence"] is None
    assert workflow_helpers.successful(*prefix, "show", plan["plan_id"]) == plan
    assert workflow_helpers.successful(*prefix, "show", revised["plan_id"]) == revised
    assert (
        workflow_helpers.successful(*prefix, "stage", "show", delivered["stage_run_id"])
        == delivered
    )
    assert not workflow_helpers.successful(
        *prefix, "stage", "list", revised["plan_id"]
    )["stages"]
    assert "The status reports the next stage" in revised["markdown"]
    executable = options[options.index("--codex-bin") + 1]
    status = b'export const status = "next";\n'
    workflow_helpers.configure(executable, writes={"frontend/status.js": status.hex()})
    later = workflow_helpers.successful(
        *prefix, "stage", "start", revised["plan_id"], "later", *options
    )
    assert later["plan_id"] == revised["plan_id"] and later["candidate_verified"]
    assert later["execution_baseline"] == delivered["candidate"]
    (dependency,) = later["dependencies"]
    assert dependency["stage_run_id"] == delivered["stage_run_id"]
    assert dependency["plan_id"] == plan["plan_id"]


def test_unprepared_direct_stage_can_explicitly_capture_policy(workflow):
    plan = _planned_project(workflow)
    options = _direct_inputs(workflow)
    run = StageRun(
        "stage-pending",
        plan["plan_id"],
        "write",
        "planning-pending",
        "direct",
        workflow.baseline,
        (),
        "2026-11-19T00:00:00+00:00",
    )
    draft = PlanningRequest("codex", "gpt-6.2-sol", "high")
    review = PlanningRequest("claude", "claude-opus-5-5", "high")
    with ProjectStore(workflow.base / "reviews.sqlite3") as store:
        store.reserve_stage(
            run,
            PlanningInput(
                "Set the selected stage status",
                freeze_project_rules(workflow.repository, workflow.baseline),
                draft,
                review,
            ),
        )
    prefix = (
        "--data-dir",
        workflow.base,
        "project",
        "stage",
        "resume",
        run.stage_run_id,
    )
    refused = workflow_helpers.module_cli(*prefix)
    assert refused.returncode == 2
    assert "never prepared" in refused.stderr
    assert not (workflow.base / "stage-direct-wire.jsonl").exists()
    resumed = workflow_helpers.successful(*prefix, "--prepare", *options[4:], code=1)
    assert resumed["stage_run_id"] == run.stage_run_id
    assert resumed["planning_run_id"] == run.planning_run_id
    assert resumed["planning"]["phase"] == "ready"
    assert len(resumed["planning"]["attempts"]["draft"]) == 1
    assert resumed["dispatch_error"] and not resumed["task_run_id"]


def test_direct_codex_wire_schema_preserves_canonical_validation():
    schema = planning_output_schema("draft")
    strict = codex_task_card_schema(schema)
    assert "$ref" not in json.dumps(strict)
    for word in ("$schema", "uniqueItems", "pattern"):
        assert f'"{word}"' not in json.dumps(strict)
    assert "$defs" in schema and "proposed_tests" not in schema["required"]
    assert set(strict["required"]) == set(strict["properties"])
    alternatives = strict["properties"]["open_questions"]["items"]["anyOf"]
    assert [set(item["required"]) for item in alternatives] == [
        {"question", "blocks_dispatch"},
        {"question", "blocks_dispatch", "resolution"},
    ]


def test_project_baseline_can_use_verified_integrated_member(workflow, monkeypatch):
    from contextlib import nullcontext
    from pathlib import Path
    from types import SimpleNamespace

    from multi_agent.cli import project_stage
    from multi_agent.core.integration import IntegrationCandidate

    repo = workflow.repository
    git = workflow_helpers.git
    common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    member = git(repo, "commit-tree", tree, "-p", workflow.baseline, "-m", "member")
    merged = git(
        repo, "commit-tree", tree, "-p", workflow.baseline, "-m", "integration"
    )
    descendant = git(repo, "commit-tree", tree, "-p", merged, "-m", "next baseline")
    target_ref = git(repo, "symbolic-ref", "HEAD")
    candidate = IntegrationCandidate(
        ref="refs/multi-agent/integrations/verified", commit=merged, tree=tree
    )
    record = SimpleNamespace(
        integration_id="verified",
        result=candidate,
        target_ref=target_ref,
        target_baseline=workflow.baseline,
        members=[
            SimpleNamespace(
                task_run_id="task",
                attempt=1,
                delivery=SimpleNamespace(candidate=member),
            )
        ],
    )
    store = SimpleNamespace(
        list_integration_candidates=lambda repository: (
            (record,) if repository == str(common) else ()
        )
    )
    monkeypatch.setattr(
        project_stage.IntegrationStore, "open_readonly", lambda _: nullcontext(store)
    )
    checked = []
    monkeypatch.setattr(
        project_stage,
        "inspect_integration_candidate",
        lambda recorded: checked.append(recorded),
    )
    proof = {
        "superseded_by": None,
        "test_evidence_verification": "verified",
        "test": {"attempt": 2, "evidence_hash": "test-evidence"},
        "reviews": [
            {
                "verification": "verified",
                "assessment": {"status": "ready"},
                "round_id": "integration-review",
                "snapshot_id": "review-source",
            }
        ],
    }
    monkeypatch.setattr(
        project_stage, "verification_view", lambda *args, **kwargs: proof
    )
    dependency = {
        "stage_id": "write",
        "stage_run_id": "stage",
        "task_run_id": "task",
        "candidate": member,
        "implementation_attempt": 1,
        "test_attempt": 1,
        "review_round_id": "task-review",
    }
    baseline, dependencies = project_stage._execution_baseline(
        repo, common, workflow.database, workflow.baseline, (dependency,), descendant
    )
    assert baseline == descendant and checked == [record]
    assert dependencies[0]["integration"] == {
        "integration_id": "verified",
        "candidate": merged,
        "test_attempt": 2,
        "test_evidence_hash": "test-evidence",
        "review_round_id": "integration-review",
        "review_snapshot_id": "review-source",
    }
    proof["reviews"][-1]["assessment"]["status"] = "needs_rework"
    with pytest.raises(ValueError, match="verified prerequisite candidate"):
        project_stage._execution_baseline(
            repo,
            common,
            workflow.database,
            workflow.baseline,
            (dependency,),
            descendant,
        )
