"""Exercise T60 through the public CLI with real Git and synthetic runtimes."""

import hashlib
import json
import sys
from pathlib import Path

import test_task_workflow_cli as helpers
from test_integration_cli import create_args, show
from test_task_batch import batch_arguments, plan

from multi_agent.core.task_execution import task_execution_hash

workflow = helpers.workflow


def _integration(workflow):
    files = {
        "frontend/status.js": helpers.FIXED_BYTES,
        "frontend/other.js": b'export const banner = "welcome";\n',
    }
    plans = [plan(workflow, f"T-027{index}", path) for index, path in enumerate(files)]
    barrier = workflow.base / "implementation-barrier"
    barrier.mkdir()
    implementation = helpers.executable(
        workflow,
        "claude",
        "implementation",
        dispatches={
            task_execution_hash(item["card"]): {path: content.hex()}
            for item, (path, content) in zip(plans, files.items(), strict=True)
        },
        barrier=str(barrier),
    )
    batch = helpers.successful(*batch_arguments(workflow, *plans, implementation))
    assert len({file.read_text() for file in barrier.iterdir()}) == 2
    return helpers.successful(*create_args(workflow, *batch["tasks"]))


def _arguments(workflow, operation, integration, *options):
    return (
        "integration",
        operation,
        integration["integration_id"],
        "--database",
        workflow.database,
        "--repository",
        workflow.repository,
        *options,
    )


def _test_arguments(workflow, integration, test_bin, label):
    return _arguments(
        workflow,
        "test",
        integration,
        "--directory",
        workflow.base / label,
        "--codex-bin",
        test_bin,
        "--timeout",
        "20",
        "--grace",
        "0.2",
    )


def _review_arguments(workflow, integration, reviewers, label, *, dual=False):
    return _arguments(
        workflow,
        "review",
        integration,
        "--directory",
        workflow.base / label,
        "--codex-bin",
        reviewers["codex"],
        "--claude-bin",
        reviewers["claude"],
        "--timeout",
        "20",
        "--grace",
        "0.2",
        "--seat",
        "codex",
        "model-1",
        "high",
        *(("--seat", "claude", "claude-requested", "high") if dual else ()),
    )


def _advance_target(workflow, original):
    repo = workflow.repository
    checkout = workflow.base / "target-movement"
    helpers.git(repo, "worktree", "add", "--quiet", "--detach", str(checkout))
    rules = checkout / ".multi-agent/rules.yaml"
    data = json.loads(rules.read_text())
    data["commands"]["post-test"] = {
        "executable": data["commands"]["test"]["executable"],
        "args": [
            "-c",
            "from pathlib import Path; assert Path('artifact.bin').is_file()",
        ],
    }
    rules.write_text(json.dumps(data))
    helpers.git(checkout, "commit", "--quiet", "-am", "Expand target test suite")
    advanced = helpers.git(checkout, "rev-parse", "HEAD")
    helpers.git(repo, "worktree", "remove", str(checkout))
    helpers.git(repo, "update-ref", original["target_ref"], advanced, workflow.baseline)
    return advanced


def test_integration_cli_tests_reviews_retries_and_accepts_current_target(workflow):
    repo = workflow.repository
    integration = _integration(workflow)
    candidate = integration["candidate"]["commit"]
    test_bin = workflow.base / "bin" / "codex-integration-test"
    test_bin.write_text(
        f"#!{sys.executable}\n" + (helpers.FAKES / "test_cli.py").read_text()
    )
    test_bin.chmod(0o755)
    reviewers = {
        "codex": helpers.executable(workflow, "codex", "review"),
        "claude": helpers.executable(workflow, "claude", "review", mode="model-error"),
    }
    (repo / "AGENTS.md").write_text("staged user state\n")
    helpers.git(repo, "add", "AGENTS.md")
    (repo / "AGENTS.md").write_text("unstaged user state\n")
    index = (repo / ".git/index").read_bytes()

    early = helpers.module_cli(
        *_review_arguments(workflow, integration, reviewers, "premature-review")
    )
    assert early.returncode == 2
    assert "no matching current test attempt" in early.stderr
    assert not (workflow.base / "premature-review").exists()

    control = test_bin.with_suffix(".json")
    control.write_text(json.dumps({"mode": "wrong-version"}))
    preflight = helpers.successful(
        *_test_arguments(workflow, integration, test_bin, "failed-test-preflight"),
        code=1,
    )["test"]
    assert preflight["status"] == "failed"
    assert preflight["unexecuted_commands"] == preflight["required_commands"]
    failed_command = preflight["result"]["commands"][0]
    failed_manifest = json.loads(Path(failed_command["manifest_path"]).read_bytes())
    assert failed_manifest["actual_result"]["request_attempted"] is False
    blocked = helpers.module_cli(
        *_review_arguments(workflow, integration, reviewers, "review-after-failed-test")
    )
    assert blocked.returncode == 2 and "complete passing test suite" in blocked.stderr
    assert not (workflow.base / "review-after-failed-test").exists()

    control.write_text("{}")
    tested = helpers.successful(
        *_test_arguments(workflow, integration, test_bin, "integration-tests")
    )["test"]
    assert tested["status"] == "succeeded"
    assert [command["name"] for command in tested["required_commands"]] == [
        "build",
        "test",
    ]
    assert not tested["unexecuted_commands"]
    assert tested["candidate"]["commit"] == candidate
    assert tested["result"]["process_stopped"] is True
    for command in tested["result"]["commands"]:
        assert command["exit_code"] == command["reported_exit_code"] == 0
        archive = Path(command["manifest_path"])
        assert (
            hashlib.sha256(archive.read_bytes()).hexdigest()
            == command["manifest_sha256"]
        )
    assert show(workflow, integration)["test_evidence_verification"] == "verified"

    failed = helpers.successful(
        *_review_arguments(
            workflow, integration, reviewers, "integration-review", dual=True
        ),
        code=1,
    )["review"]
    assert failed["assessment"]["status"] == "incomplete"
    assert failed["seats"][0]["attempts"][0]["status"] == "succeeded"
    assert failed["seats"][1]["attempts"][0]["status"] == "failed"
    assert show(workflow, integration)["reviews"][-1]["current_eligible"] is False
    accept = _arguments(workflow, "accept", integration, "--review", failed["round_id"])
    refusal = helpers.module_cli(*accept)
    assert refusal.returncode == 2 and show(workflow, integration)["acceptance"] is None

    helpers.configure(reviewers["claude"], mode="success")
    ready = helpers.successful(
        *_arguments(
            workflow,
            "retry-review",
            integration,
            "--review",
            failed["round_id"],
            "--seat",
            "seat-2",
        )
    )["review"]
    assert ready["round_id"] == failed["round_id"]
    assert ready["snapshot_id"] == failed["snapshot_id"]
    assert ready["assessment"]["status"] == "ready"
    assert ready["seats"][0] == failed["seats"][0]
    assert ready["seats"][1]["current_attempt"] == 2
    shown_review = show(workflow, integration)["reviews"][-1]
    assert (
        shown_review["current_eligible"] and shown_review["verification"] == "verified"
    )
    launches = [
        item
        for item in helpers.records(workflow.base / "review-wire.jsonl")
        if item["kind"] == "review-input"
    ]
    assert sorted(item["provider"] for item in launches) == [
        "claude",
        "claude",
        "codex",
    ]
    frozen = [launch["payload"] for launch in launches]
    assert {item["snapshot_id"] for item in frozen} == {ready["snapshot_id"]}
    assert {item["source"]["candidate"] for item in frozen} == {candidate}
    assert {item["test_evidence_hash"] for item in frozen} == {tested["evidence_hash"]}
    assert all(
        item["commands"][1]["stdout"]["sha256"]
        == hashlib.sha256(helpers.FAILED_STDOUT).hexdigest()
        and item["commands"][1]["stderr"]["sha256"]
        == hashlib.sha256(helpers.FAILED_STDERR).hexdigest()
        for item in frozen
    )
    accepted = helpers.successful(*accept)["acceptance"]
    assert accepted["candidate"]["commit"] == candidate
    assert accepted["snapshot_id"] == ready["snapshot_id"]
    assert accepted["test_evidence_hash"] == tested["evidence_hash"]
    assert helpers.git(repo, "rev-parse", "HEAD") == workflow.baseline

    advanced = _advance_target(workflow, integration)
    stale = helpers.module_cli(*accept)
    assert (
        stale.returncode == 2
        and f"integration rebase {integration['integration_id']}" in stale.stderr
    )
    rebased = helpers.successful(*_arguments(workflow, "rebase", integration))
    assert rebased["rebased_from"] == integration["integration_id"]
    assert rebased["integration_id"] != integration["integration_id"]
    assert rebased["target_baseline"] == advanced
    assert rebased["candidate"]["commit"] != candidate

    retested = helpers.successful(
        *_test_arguments(workflow, rebased, test_bin, "rebased-tests")
    )["test"]
    assert [command["name"] for command in retested["required_commands"]] == [
        "build",
        "test",
        "post-test",
    ]
    assert [item["exit_code"] for item in retested["result"]["commands"]] == [0, 0, 0]
    assert retested["rules_baseline"] == advanced
    assert retested["rules_hash"] != tested["rules_hash"]
    reviewed = helpers.successful(
        *_review_arguments(workflow, rebased, reviewers, "rebased-review")
    )["review"]
    assert reviewed["snapshot_id"] != ready["snapshot_id"]
    assert len(reviewed["seats"]) == 1 and reviewed["assessment"]["status"] == "ready"
    receipt = helpers.successful(
        *_arguments(workflow, "accept", rebased, "--review", reviewed["round_id"])
    )["acceptance"]
    assert receipt["target_baseline"] == advanced
    assert receipt["candidate"] == rebased["candidate"]
    assert receipt["test_evidence_hash"] == retested["evidence_hash"]
    historical = show(workflow, integration)
    assert not historical["target_is_current"] and historical["acceptance"] == accepted
    assert historical["reviews"][-1]["source_current"] is False
    assert show(workflow, rebased)["acceptance"] == receipt
    assert helpers.git(repo, "rev-parse", "HEAD") == advanced
    assert (repo / ".git/index").read_bytes() == index
    assert (repo / "AGENTS.md").read_text() == "unstaged user state\n"
    assert not (repo / "artifact.bin").exists()
