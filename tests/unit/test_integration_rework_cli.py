"""Exercise bounded integration rework through the public CLI and real Git."""

import sys

import test_task_workflow_cli as helpers
from test_integration_cli import create_args, implement, show
from test_integration_verification_cli import (
    _advance_target,
    _arguments,
    _review_arguments,
    _test_arguments,
)
from test_task_batch import batch_arguments, plan

from multi_agent.core.task_execution import task_execution_hash

workflow = helpers.workflow

_STATUS = "frontend/status.js"
_OTHER = "frontend/other.js"


def _structural_suite(workflow):
    (workflow.repository / "checks/test.py").write_text(
        "from pathlib import Path\n"
        "assert Path('artifact.bin').read_bytes() == Path('frontend/status.js').read_bytes()\n"
        "assert Path('frontend/other.js').read_bytes()\n"
    )
    helpers.git(
        workflow.repository, "commit", "--quiet", "-am", "Check integration build"
    )
    workflow.baseline = helpers.git(workflow.repository, "rev-parse", "HEAD")
    binary = workflow.base / "bin" / "codex-integration-check"
    binary.write_text(
        f"#!{sys.executable}\n" + (helpers.FAKES / "test_cli.py").read_text()
    )
    binary.chmod(0o755)
    return binary


def _parallel_members(workflow):
    first, second = [
        plan(workflow, f"T-029{index}", path)
        for index, path in enumerate((_STATUS, _OTHER))
    ]
    barrier = workflow.base / "seed-workers"
    barrier.mkdir()
    owner_hash = task_execution_hash(first["card"])
    implementation = helpers.executable(
        workflow,
        "claude",
        "implementation",
        dispatches={
            owner_hash: {_STATUS: helpers.FIRST_BYTES.hex()},
            task_execution_hash(second["card"]): {_OTHER: helpers.RETAINED_BYTES.hex()},
        },
        barrier=str(barrier),
    )
    batch = helpers.successful(
        *batch_arguments(workflow, first, second, implementation)
    )
    assert len({file.read_text() for file in barrier.iterdir()}) == 2
    return batch["tasks"], implementation, owner_hash


def _reviewers(workflow, candidate):
    report = helpers.blocking_report()
    report["findings"][0]["rationale"] = (
        "The rework fixture's structural tests permit draft. The requirement is ready."
    )
    return {
        "codex": helpers.executable(
            workflow, "codex", "review", reports_by_candidate={candidate: report}
        ),
        "claude": helpers.executable(workflow, "claude", "review"),
    }


def _seats(review):
    return [
        (
            seat["key"],
            seat["provider"],
            seat["requested_model"],
            seat["requested_effort"],
        )
        for seat in review["seats"]
    ]


def _budget_refused(workflow, integration, round_id):
    blocked = helpers.successful(
        *_arguments(workflow, "rework", integration, "--review", round_id), code=1
    )
    assert blocked["rework"]["status"] == "budget_exhausted"


def test_integration_rework_cli_keeps_repair_and_budget_across_lineage(workflow):
    repo = workflow.repository
    test_bin = _structural_suite(workflow)
    members, writer, owner_hash = _parallel_members(workflow)
    parent = helpers.successful(*create_args(workflow, *members))
    candidate = parent["candidate"]["commit"]
    tested = helpers.successful(
        *_test_arguments(workflow, parent, test_bin, "original-tests")
    )["test"]
    assert (
        tested["status"] == "succeeded" and tested["candidate"]["commit"] == candidate
    )
    helpers.configure(
        writer,
        dispatches=None,
        task_hash=owner_hash,
        writes={_STATUS: helpers.FIXED_BYTES.hex()},
    )
    reviewers = _reviewers(workflow, candidate)
    review = helpers.successful(
        *_review_arguments(workflow, parent, reviewers, "original-review", dual=True)
    )
    assert review["review"]["assessment"]["status"] == "needs_rework"
    rework = review["rework"]
    revision = review["revision"]
    child = revision["candidate"]["commit"]
    assert (
        rework["result"]["status"] == "succeeded"
        and rework["result"]["process_stopped"] is True
    )
    assert rework["source_id"] == parent["integration_id"]
    assert rework["child_id"] == revision["integration_id"] != parent["integration_id"]
    assert rework["request"]["member"]["task_run_id"] == members[0]["task_run_id"]
    assert rework["request"]["snapshot_id"] == review["review"]["snapshot_id"]
    assert rework["request"]["requested_actor"]["policy"]["executable"] == str(writer)
    assert rework["result"]["evidence"]["reported_model"] == "actual-claude"
    assert rework["result"]["delta"] == {
        "base": candidate,
        "commit": child,
        "tree": revision["candidate"]["tree"],
        "paths": [_STATUS],
    }
    assert helpers.git(repo, "rev-parse", f"{child}^") == candidate
    assert helpers.git(repo, "diff", "--name-only", candidate, child) == _STATUS
    assert helpers.git(
        repo, "show", f"{child}:{_OTHER}"
    ) == helpers.RETAINED_BYTES.decode().rstrip("\n")
    implementation_calls = [
        item
        for item in helpers.records(workflow.base / "implementation-wire.jsonl")
        if item["kind"] == "implementation"
    ]
    assert len(implementation_calls) == 3
    repaired = implementation_calls[-1]
    assert repaired["before"] == {_STATUS: helpers.FIRST_BYTES.hex()}
    assert candidate in repaired["prompt"] and "status-blocker" in repaired["prompt"]

    retested = review["retest"]["test"]
    rereview = review["rereview"]["review"]
    assert retested["candidate"] == revision["candidate"]
    assert retested["required_commands"] == tested["required_commands"]
    assert retested["status"] == "succeeded"
    assert [item["exit_code"] for item in retested["result"]["commands"]] == [0, 0]
    assert rereview["round_id"] != review["review"]["round_id"]
    assert rereview["snapshot_id"] != review["review"]["snapshot_id"]
    assert _seats(rereview) == _seats(review["review"])
    assert rereview["assessment"]["status"] == "ready"
    assert show(workflow, revision)["acceptance"] is None

    for integration in (parent, revision):
        old = helpers.module_cli(
            *_arguments(
                workflow,
                "accept",
                integration,
                "--review",
                review["review"]["round_id"],
            )
        )
        assert old.returncode == 2
        _budget_refused(workflow, integration, review["review"]["round_id"])
    acceptance = helpers.successful(
        *_arguments(workflow, "accept", revision, "--review", rereview["round_id"])
    )["acceptance"]
    assert acceptance["candidate"]["commit"] == child
    assert acceptance["snapshot_id"] == rereview["snapshot_id"]
    assert helpers.git(repo, "rev-parse", "HEAD") == workflow.baseline

    advanced = _advance_target(workflow, parent)
    rebased = helpers.successful(*_arguments(workflow, "rebase", revision))
    assert rebased["rebased_from"] == revision["integration_id"]
    assert rebased["target_baseline"] == advanced
    rebuilt = rebased["candidate"]["commit"]
    assert rebuilt not in (candidate, child)
    assert helpers.git(
        repo, "show", f"{rebuilt}:{_STATUS}"
    ) == helpers.FIXED_BYTES.decode().rstrip("\n")
    assert helpers.git(
        repo, "show", f"{rebuilt}:{_OTHER}"
    ) == helpers.RETAINED_BYTES.decode().rstrip("\n")
    _budget_refused(workflow, rebased, rereview["round_id"])
    assert show(workflow, rebased)["rework"]["rework_id"] == rework["rework_id"]
    assert show(workflow, revision)["acceptance"] == acceptance
    assert helpers.git(repo, "rev-parse", "HEAD") == advanced


def test_integration_rework_cli_will_not_publish_another_members_delivery(workflow):
    repo = workflow.repository
    test_bin = _structural_suite(workflow)
    owner, writer = implement(
        workflow, "T-0290", "frontend/**", {_STATUS: helpers.FIRST_BYTES}
    )
    sibling, _ = implement(workflow, "T-0291", _OTHER, {_OTHER: helpers.RETAINED_BYTES})
    integration = helpers.successful(*create_args(workflow, owner, sibling))
    before = helpers.git(repo, "show-ref")
    candidate = integration["candidate"]["commit"]
    helpers.successful(*_test_arguments(workflow, integration, test_bin, "ready-tests"))
    helpers.configure(
        writer,
        task_hash=task_execution_hash(owner["details"]["input"]["card"]),
        writes={_STATUS: helpers.FIXED_BYTES.hex(), _OTHER: b"wrong owner\n".hex()},
    )
    result = helpers.successful(
        *_review_arguments(
            workflow, integration, _reviewers(workflow, candidate), "scope-review"
        ),
        code=1,
    )
    refusal = result["rework"]["result"]
    assert (
        refusal["status"] == "delivery_blocked" and refusal["process_stopped"] is True
    )
    assert refusal["runtime_status"] == "succeeded"
    assert _OTHER in refusal["reason"] and "other members" in refusal["reason"]
    assert (
        "revision" not in result and "retest" not in result and "rereview" not in result
    )
    assert helpers.git(repo, "show-ref") == before
    assert show(workflow, integration)["acceptance"] is None
