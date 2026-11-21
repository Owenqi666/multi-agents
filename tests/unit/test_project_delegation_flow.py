"""Drive a delegated stage through real Git, SQLite, and the public CLIs."""

import json
import sys
from dataclasses import replace

import pytest
import test_task_workflow_cli as helpers
from test_integration_cli import create_args
from test_integration_verification_cli import _review_arguments, _test_arguments
from test_project_stage import _direct_inputs, _model_card, _planned_project
from test_task_batch import batch_arguments

from multi_agent.cli.integration_cli import inspect_delegated_prerequisite_integration
from multi_agent.cli.planning_evidence import inspect_projected_draft
from multi_agent.cli.workspaces import executor_lock
from multi_agent.store.project_store import ProjectStore

workflow = helpers.workflow


def test_delegated_stage_split_projects_two_leaves_and_verifies_exact_integration(
    workflow,
):
    workflow.database = workflow.base / "reviews.sqlite3"
    repo = workflow.repository
    first_id, second_id = "T-0281", "T-0282"
    status, banner = "frontend/status.js", "frontend/banner.js"
    banner_bytes = b'export const banner = "welcome";\n'
    amount = "backend/amount.py"
    amount_bytes = b"AMOUNT = 1\n"
    first_model, second_model = "claude-sonnet-5-5", "claude-sonnet-4-6"

    # The registered suite must read both delivered files from the integration.
    check = repo / "checks/test.py"
    check.write_text(
        "from pathlib import Path\n"
        f"assert Path({amount!r}).read_bytes() == {amount_bytes!r}\n"
        f"if Path('artifact.bin').read_bytes() != {(repo / status).read_bytes()!r}:\n"
        f"    assert Path('artifact.bin').read_bytes() == {helpers.FIXED_BYTES!r}\n"
        f"    assert Path({banner!r}).read_bytes() == {banner_bytes!r}\n"
    )
    workflow.rules["implementation"][0]["allowed_models"].append(second_model)
    workflow.rules["implementation"].append(
        {
            "paths": [amount],
            "provider": "codex",
            "default_model": "gpt-6.2-sol",
            "default_effort": "high",
            "allowed_models": ["gpt-6.2-sol"],
        }
    )
    (repo / ".multi-agent/rules.yaml").write_text(json.dumps(workflow.rules))
    helpers.git(repo, "commit", "--quiet", "-am", "check both stage outputs")
    workflow.baseline = helpers.git(repo, "rev-parse", "HEAD")
    plan = _planned_project(
        workflow,
        [
            dict(
                _model_card("amount", []),
                goal="Provide a verified amount",
                approach=f"Add {amount}",
            ),
            _model_card("write", ["amount"]),
            _model_card("later", ["write"]),
        ],
    )
    prefix = ("--data-dir", workflow.base, "project", "stage")
    options = _direct_inputs(workflow, amount, amount_bytes)
    direct = helpers.successful(*prefix, "start", plan["plan_id"], "amount", *options)
    assert direct["mode"] == "direct" and direct["candidate_verified"]
    test_bin = workflow.base / "bin" / "codex-stage-integration-test"
    test_bin.write_text(
        f"#!{sys.executable}\n" + (helpers.FAKES / "test_cli.py").read_text()
    )
    test_bin.chmod(0o755)
    test_bin.with_suffix(".json").write_text("{}")
    reviewer = helpers.executable(workflow, "codex", "review")
    direct_attempt = helpers.successful(
        *helpers.suite_arguments(workflow, direct["task_run_id"], 1, test_bin)
    )
    helpers.successful(
        *helpers.review_arguments(
            workflow, direct["task_run_id"], 1, direct_attempt["test_attempt"], reviewer
        )
    )
    helpers.git(repo, "branch", "summary-target", direct["candidate"])

    def integrate(*tasks):
        args = list(create_args(workflow, *tasks))
        args[-1] = "summary-target"
        return helpers.successful(*args)

    codex = helpers.executable(
        workflow,
        "codex",
        "stage-delegate",
        split_cards=[
            {"id": first_id, "goal": "Ready the status", "write_scope": [status]},
            {
                "id": second_id,
                "goal": "Add a banner for the ready status",
                "write_scope": [banner],
            },
        ],
    )
    claude = helpers.executable(workflow, "claude", "stage-delegate")
    split = helpers.successful(
        *prefix,
        "start",
        plan["plan_id"],
        "write",
        "--mode",
        "delegate",
        "--codex-bin",
        codex,
        "--claude-bin",
        claude,
        "--timeout",
        "20",
        "--grace",
        "0.2",
    )
    run_id = split["stage_run_id"]
    show_args = (*prefix, "show", run_id)
    leaf_args = (*prefix, "leaf", run_id)
    executor = ("--implementation", "claude-code")
    wire = workflow.base / "stage-delegate-wire.jsonl"
    assert split["mode"] == "delegate" and split["split_verified"]
    assert split["execution_baseline"] == direct["candidate"]
    assert [item["card_id"] for item in split["leaves"]] == [first_id, second_id]
    assert all(item["planning_run_id"] is None for item in split["leaves"])
    assert not split["can_integrate"] and split["integration"] is None

    invalid = helpers.module_cli(*leaf_args, first_id, *executor, first_model, "ultra")
    assert invalid.returncode == 2 and "effort" in invalid.stderr
    same_as_drafter = helpers.module_cli(
        *leaf_args,
        first_id,
        *executor,
        first_model,
        "high",
        "--card-review",
        "codex",
        "gpt-6.2-sol",
        "high",
        "--review-only",
    )
    assert (
        same_as_drafter.returncode == 2 and "distinct model" in same_as_drafter.stderr
    )
    assert helpers.successful(*show_args) == split
    assert sum(item["kind"] == "split" for item in helpers.records(wire)) == 1
    assert all(item["kind"] != "leaf-review" for item in helpers.records(wire))

    fable = {"provider": "claude", "model": "claude-fable-5-2", "effort": "high"}
    opus = {"provider": "claude", "model": "claude-opus-5-5", "effort": "high"}
    helpers.configure(claude, mode="model-error")
    failed = helpers.successful(
        *leaf_args,
        first_id,
        *executor,
        first_model,
        "high",
        "--card-review",
        "claude",
        "claude-fable-5-2",
        "high",
        "--review-only",
        code=1,
    )
    assert failed["task_run_id"] is None and not failed["review_ready"]
    assert failed["planning"]["phase"] == "failed"
    helpers.configure(claude, mode="success")
    change = helpers.module_cli(
        *leaf_args,
        first_id,
        "--card-review",
        "claude",
        "claude-opus-5-5",
        "high",
        "--review-only",
    )
    assert change.returncode == 2 and "exact card reviewer" in change.stderr
    first = helpers.successful(*leaf_args, first_id, "--review-only")
    second = helpers.successful(
        *leaf_args, second_id, *executor, second_model, "high", "--review-only"
    )
    assert first["planning_run_id"] == failed["planning_run_id"]
    assert (
        first["planning"]["attempts"]["review"][:1]
        == failed["planning"]["attempts"]["review"]
    )
    assert [
        row["request"]["request"] for row in first["planning"]["attempts"]["review"]
    ] == [fable, fable]
    assert second["planning"]["attempts"]["review"][0]["request"]["request"] == opus
    pending = helpers.successful(*show_args)
    assert all(
        leaf["review_ready"] and leaf["task_run_id"] is None
        for leaf in pending["leaves"]
    )
    assert not pending["can_integrate"] and pending["integration"] is None

    parents = [item["planning"]["draft_projection"] for item in (first, second)]
    with ProjectStore.open_readonly(workflow.database) as store:
        one = store.load_planning(first["planning_run_id"])
        two = store.load_planning(second["planning_run_id"])
        assert one.card is not None
        for card in (two.card, replace(one.card, goal="Changed after review")):
            with pytest.raises(ValueError, match="projected card"):
                inspect_projected_draft(
                    store, first["planning_run_id"], replace(one, card=card)
                )
    barrier = workflow.base / "leaf-implementation-barrier"
    barrier.mkdir()
    implementation = helpers.executable(
        workflow,
        "claude",
        "implementation",
        dispatches={
            parents[0]["card_hash"]: {status: helpers.FIXED_BYTES.hex()},
            parents[1]["card_hash"]: {banner: banner_bytes.hex()},
        },
        barrier=str(barrier),
    )
    args = batch_arguments(workflow, first, second, implementation)
    offset = args.index("--implementation")
    del args[offset : offset + 4]
    batch = helpers.successful(*args)
    assert len({item.read_text() for item in barrier.iterdir()}) == 2
    projected = helpers.successful(*show_args)
    assert projected["can_integrate"] and projected["integration"] is None
    assert not projected["tests_passed"] and not projected["review_satisfied"]
    assert [item["candidate"] for item in projected["leaves"]] == [
        item["candidate"] for item in batch["tasks"]
    ]

    captured = helpers.records(wire)
    review_runs = [item for item in captured if item["kind"] == "leaf-review"]
    assert [item["input"]["review_request"] for item in review_runs] == [
        fable,
        fable,
        opus,
    ]
    reviewer_argv = [
        item["argv"]
        for item in captured
        if item["kind"] == "spawn" and item["argv"][:1] == ["-p"]
    ]
    assert [argv[argv.index("--model") + 1] for argv in reviewer_argv] == [
        fable["model"],
        fable["model"],
        opus["model"],
    ]
    reviews = review_runs[1:]
    launches = helpers.records(workflow.base / "implementation-wire.jsonl")
    deliveries = [item for item in launches if item["kind"] == "implementation"]
    assert len(reviews) == len(deliveries) == 2
    assert sum(item["kind"] == "split" for item in captured) == 1
    assert (
        parents[0]["split_run_id"]
        == parents[1]["split_run_id"]
        == split["planning_run_id"]
    )
    assert parents[0]["split_manifest_sha256"] == parents[1]["split_manifest_sha256"]
    assert [item["entry_index"] for item in parents] == [0, 1]
    assert parents[0]["card_hash"] != parents[1]["card_hash"]
    settled = {item["planning_run_id"]: item for item in batch["tasks"]}
    for leaf, parent, review, model in zip(
        (first, second), parents, reviews, (first_model, second_model), strict=True
    ):
        assert (
            review["provider"] == "claude" and review["report"]["decision"] == "ready"
        )
        (delivery,) = (
            item for item in deliveries if parent["card_hash"] in item["prompt"]
        )
        frozen = json.loads(
            delivery["prompt"].split(
                "Frozen implementation input (canonical JSON):\n", 1
            )[1]
        )
        assert (
            parent["card_hash"] == review["report"]["task_hash"] == frozen["task_hash"]
        )
        assert frozen["task_input"]["card"] == leaf["planning"]["card"]
        assert set(delivery["after"]) == set(
            frozen["task_input"]["card"]["write_scope"]
        )
        assert plan["plan_id"] in frozen["task_input"]["requirements"]
        assert run_id in frozen["task_input"]["requirements"]
        task = settled[leaf["planning_run_id"]]
        assert task["attempts"][0]["request"]["decision"]["choice"]["model"] == model

    partial = integrate(batch["tasks"][0])
    assert partial["candidate"] and helpers.successful(*show_args) == projected
    integration = integrate(*batch["tasks"])
    candidate = integration["candidate"]["commit"]
    untested = helpers.successful(*show_args)
    assert untested["candidate"] == candidate and untested["candidate_verified"]
    assert not untested["tests_passed"] and not untested["review_satisfied"]

    tested = helpers.successful(
        *_test_arguments(workflow, integration, test_bin, "stage-suite")
    )["test"]
    awaiting_review = helpers.successful(*show_args)
    assert awaiting_review["tests_passed"] and not awaiting_review["review_satisfied"]
    reviewed = helpers.successful(
        *_review_arguments(
            workflow, integration, {"codex": reviewer, "claude": claude}, "stage-review"
        )
    )["review"]
    ready = helpers.successful(*show_args)
    proof = ready["integration"]
    assert ready["tests_passed"] and ready["review_satisfied"]
    assert proof["candidate"] == tested["candidate"]["commit"] == candidate
    assert proof["integration_id"] == integration["integration_id"]
    assert proof["test_attempt"] == tested["attempt"]
    assert proof["test_evidence_hash"] == tested["evidence_hash"]
    assert proof["review_round_id"] == reviewed["round_id"]
    assert proof["review_snapshot_id"] == reviewed["snapshot_id"]

    def require_upstream_review():
        with executor_lock(repo) as lease:
            inspect_delegated_prerequisite_integration(
                workflow.database,
                lease.repository,
                direct["candidate"],
                tuple(
                    (
                        item["task_run_id"],
                        item["implementation_attempt"],
                        item["candidate"],
                    )
                    for item in batch["tasks"]
                ),
                integration["integration_id"],
                candidate,
                lease=lease,
            )

    require_upstream_review()
    report = helpers.blocking_report()
    report["findings"][0]["quote"] = helpers.FIXED_BYTES.decode().strip()
    helpers.configure(reviewer, report=report)
    refused = helpers.successful(
        *_review_arguments(
            workflow,
            integration,
            {"codex": reviewer, "claude": claude},
            "stage-blocking",
        ),
        "--no-auto-rework",
        code=1,
    )["review"]
    assert refused["assessment"]["status"] == "needs_rework"
    blocked = helpers.successful(*show_args)
    assert blocked["candidate"] == candidate and blocked["tests_passed"]
    assert not blocked["review_satisfied"]
    with pytest.raises(ValueError, match="ready review"):
        require_upstream_review()
    helpers.configure(
        reviewer, report={"summary": "The exact candidate is ready", "findings": []}
    )
    helpers.successful(
        *_review_arguments(
            workflow,
            integration,
            {"codex": reviewer, "claude": claude},
            "stage-restored",
        ),
        "--no-auto-rework",
    )
    require_upstream_review()
    stage_runs = helpers.successful(*prefix, "list", plan["plan_id"])["stages"]
    assert [(item["stage_id"], item["mode"]) for item in stage_runs] == [
        ("amount", "direct"),
        ("write", "delegate"),
    ]
    assert helpers.git(repo, "rev-parse", "HEAD") == workflow.baseline
    assert helpers.git(repo, "status", "--porcelain") == ""
