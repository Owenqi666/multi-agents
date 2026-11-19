"""Exercise delegated dependencies across a real managed task rework."""

import sys

import test_task_workflow_cli as helpers
from test_project_stage import _planned_project

from multi_agent.store.project_store import ProjectStore

workflow = helpers.workflow


def test_delegated_dependency_must_refreeze_after_upstream_rework(workflow):
    workflow.database = workflow.base / "reviews.sqlite3"
    plan = _planned_project(workflow)
    transport = workflow.base / "bin" / "codex-transport.py"
    transport.write_text(
        transport.read_text().replace(
            'name = "model-2" if "cursor" in params else "model-1"',
            'name = "model-1" if "cursor" in params else "gpt-6.2-sol"',
        )
    )
    upstream_id, downstream_id = "T-0291", "T-0292"
    status, banner = "frontend/status.js", "frontend/banner.js"
    splitter = helpers.executable(
        workflow,
        "codex",
        "stage-delegate",
        split_cards=[
            {"id": upstream_id, "goal": "Ready the status", "write_scope": [status]},
            {
                "id": downstream_id,
                "goal": "Read the status from a banner",
                "write_scope": [banner],
                "depends_on": [upstream_id],
            },
        ],
    )
    card_reviewer = helpers.executable(workflow, "claude", "stage-delegate")
    prefix = ("--data-dir", workflow.base, "project", "stage")
    stage = helpers.successful(
        *prefix,
        "start",
        plan["plan_id"],
        "write",
        "--mode",
        "delegate",
        "--codex-bin",
        splitter,
        "--claude-bin",
        card_reviewer,
        "--timeout",
        "20",
        "--grace",
        "0.2",
    )
    stage_id = stage["stage_run_id"]
    show = (*prefix, "show", stage_id)
    leaf = (*prefix, "leaf", stage_id)
    executor = ("--implementation", "claude-code", "claude-sonnet-5-5", "high")
    refused = helpers.module_cli(*leaf, downstream_id, *executor, "--review-only")
    assert refused.returncode == 2 and "no verified candidate" in refused.stderr
    assert helpers.successful(*show) == stage
    assert stage["leaves"][1]["unmet_dependencies"] == [upstream_id]
    assert stage["leaves"][1]["planning_run_id"] is None

    upstream = helpers.successful(*leaf, upstream_id, *executor, "--review-only")
    implementation = helpers.executable(
        workflow,
        "claude",
        "implementation",
        task_hash=upstream["planning"]["draft_projection"]["card_hash"],
        writes={status: helpers.FIRST_BYTES.hex()},
    )
    first = helpers.successful(
        *helpers.run_arguments(workflow, upstream["planning_run_id"], implementation)
    )
    first_sha, task_id = first["candidate"], first["task_run_id"]
    old_leaf = helpers.successful(*leaf, downstream_id, *executor, "--review-only")
    old_id = old_leaf["planning_run_id"]
    old_projection = old_leaf["planning"]["draft_projection"]
    assert old_leaf["review_ready"] and old_leaf["task_run_id"] is None
    assert old_leaf["planning"]["card"]["baseline_commit"] == first_sha
    assert old_projection["dependencies"] == [
        {
            "card_id": upstream_id,
            "planning_run_id": upstream["planning_run_id"],
            "task_run_id": task_id,
            "implementation_attempt": 1,
            "candidate": first_sha,
        }
    ]

    test_bin = workflow.base / "bin" / "codex-workflow-test"
    test_bin.write_text(
        f"#!{sys.executable}\n" + (helpers.FAKES / "test_cli.py").read_text()
    )
    test_bin.chmod(0o755)
    test_bin.with_suffix(".json").write_text("{}")
    tested = helpers.successful(
        *helpers.suite_arguments(workflow, task_id, 1, test_bin), code=1
    )
    reviewer = helpers.executable(
        workflow, "codex", "review", report=helpers.blocking_report()
    )
    blocking = helpers.successful(
        *helpers.review_arguments(
            workflow, task_id, 1, tested["test_attempt"], reviewer
        ),
        code=1,
    )
    assert blocking["assessment"]["status"] == "needs_rework"
    helpers.configure(implementation, writes={status: helpers.FIXED_BYTES.hex()})
    repaired = helpers.successful(
        "task",
        "rework",
        blocking["round_id"],
        "--database",
        workflow.database,
        "--directory",
        workflow.base / "rework",
    )
    repaired_sha = repaired["candidate"]
    assert repaired["implementation_attempt"] == 2 and repaired_sha != first_sha

    stale = helpers.successful(*show)
    assert stale["leaves"][0]["candidate"] == repaired_sha
    assert stale["leaves"][1]["planning_run_id"] == old_id
    assert not stale["leaves"][1]["review_ready"] and not stale["can_integrate"]
    assert "changed after review" in stale["leaves"][1]["blocked_reason"]
    for command in (
        (*leaf, downstream_id),
        helpers.run_arguments(workflow, old_id, implementation, directory="stale"),
    ):
        rejected = helpers.module_cli(*command)
        assert rejected.returncode == 2 and "changed after review" in rejected.stderr
    assert helpers.successful(*show) == stale

    refrozen = helpers.successful(*leaf, downstream_id, "--refreeze", "--review-only")
    new_id = refrozen["planning_run_id"]
    new_projection = refrozen["planning"]["draft_projection"]
    assert refrozen["review_ready"] and new_id != old_id
    assert refrozen["planning"]["card"]["baseline_commit"] == repaired_sha
    assert new_projection["dependencies"] == [
        {
            **old_projection["dependencies"][0],
            "candidate": repaired_sha,
            "implementation_attempt": 2,
        }
    ]
    assert new_projection["card_hash"] != old_projection["card_hash"]
    assert new_projection["entry_hash"] == old_projection["entry_hash"]
    assert helpers.successful(*show)["leaves"][1]["prior_planning_run_ids"] == [old_id]
    with ProjectStore.open_readonly(workflow.database) as store:
        history = store.load_planning(old_id)
        assert history.bound_task_run_id is None
        assert history.provenance().card.baseline_commit == first_sha
        assert history.provenance().card.task_hash == old_projection["card_hash"]

    helpers.configure(
        implementation,
        task_hash=new_projection["card_hash"],
        writes={banner: b'export { status } from "./status.js";\n'.hex()},
    )
    delivered = helpers.successful(
        *helpers.run_arguments(workflow, new_id, implementation, directory="downstream")
    )
    assert helpers.git(
        workflow.repository, "rev-parse", f"{delivered['candidate']}^"
    ) == (repaired_sha)
    assert helpers.successful(*show)["can_integrate"]
