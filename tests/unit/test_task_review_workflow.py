"""Automatic continuation through real module CLIs and synthetic model programs."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

import pytest
import test_task_workflow_cli as helpers

from multi_agent.cli.review_inputs import load_review_input
from multi_agent.core.task_execution import prepared_for_task, task_execution_hash
from multi_agent.store.task_review_store import TaskReviewStore

workflow = helpers.workflow


def prepare(workflow, *, max_attempts=2):
    planned = helpers.successful(*helpers.plan_arguments(workflow))
    implementation = helpers.executable(
        workflow,
        "claude",
        "implementation",
        task_hash=task_execution_hash(planned["card"]),
        writes={
            "frontend/status.js": helpers.FIRST_BYTES.hex(),
            "frontend/retained.js": helpers.RETAINED_BYTES.hex(),
        },
    )
    first = helpers.successful(
        *helpers.run_arguments(
            workflow,
            planned["planning_run_id"],
            implementation,
            max_attempts=max_attempts,
        )
    )
    test_bin = workflow.base / "bin/codex-workflow-test"
    test_bin.write_text(
        f"#!{sys.executable}\n" + (helpers.FAKES / "test_cli.py").read_text()
    )
    test_bin.chmod(0o755)
    test_bin.with_suffix(".json").write_text(
        json.dumps({"capture": str(workflow.base / "test-wire.jsonl")})
    )
    tested = helpers.successful(
        *helpers.suite_arguments(workflow, first["task_run_id"], 1, test_bin),
        code=1,
    )
    assert tested["verification"] == "verified"
    assert [item["exit_code"] for item in tested["commands"]] == [0, 7]
    workflow.planned = planned
    workflow.first = first
    workflow.tested = tested
    workflow.implementation = implementation
    workflow.test_bin = test_bin
    workflow.reviewers = {
        provider: helpers.executable(
            workflow,
            provider,
            "review",
            capture=str(workflow.base / f"{provider}-review-wire.jsonl"),
            reports_by_candidate={first["candidate"]: helpers.blocking_report()},
            generate_confirmations=True,
        )
        for provider in ("codex", "claude")
    }
    helpers.configure(
        implementation, writes={"frontend/status.js": helpers.FIXED_BYTES.hex()}
    )
    with TaskReviewStore.open_readonly(workflow.database) as store:
        workflow.original = {
            "input": store.load_task_input(first["task_run_id"]),
            "task": store.load_task(first["task_run_id"]),
            "history": store.load_task_history(first["task_run_id"]),
            "tests": store.load_task_test(first["task_run_id"], 1),
            "test_history": store.load_task_test_history(first["task_run_id"], 1),
        }
    workflow.refs = helpers.git(workflow.repository, "show-ref")
    workflow.original_directories = {
        path.name for path in workflow.base.iterdir() if path.is_dir()
    }
    return workflow


def review_arguments(workflow, *, dual=False):
    args = helpers.review_arguments(
        workflow,
        workflow.first["task_run_id"],
        1,
        workflow.tested["test_attempt"],
        workflow.reviewers["codex"],
    )
    args.remove("--no-auto-rework")
    args.extend(("--claude-bin", workflow.reviewers["claude"]))
    if dual:
        args.extend(("--seat", "claude", "claude-requested", "high"))
    return args


def continue_arguments(workflow, operation, identifier, *options):
    return (
        "task-review",
        operation,
        identifier,
        "--database",
        workflow.database,
        *options,
    )


def implementation_calls(workflow):
    return [
        row
        for row in helpers.records(workflow.base / "implementation-wire.jsonl")
        if row["kind"] == "implementation"
    ]


def command_requests(workflow):
    return [
        row["message"]
        for row in helpers.records(workflow.base / "test-wire.jsonl")
        if row["kind"] == "request" and row["message"]["method"] == "command/exec"
    ]


def reviews(workflow, *, confirmation=False):
    return [
        row
        for provider in ("codex", "claude")
        for row in helpers.records(workflow.base / f"{provider}-review-wire.jsonl")
        if row["kind"] == "review-input"
        and row.get("confirmation", False) == confirmation
    ]


def assert_no_downstream(workflow, *, rounds=1):
    run_id = workflow.first["task_run_id"]
    with TaskReviewStore.open_readonly(workflow.database) as store:
        assert store.load_task_input(run_id) == workflow.original["input"]
        assert store.load_task(run_id) == workflow.original["task"]
        assert store.load_task_history(run_id) == workflow.original["history"]
        assert store.load_task_test(run_id, 1) == workflow.original["tests"]
        assert (
            store.load_task_test_history(run_id, 1) == workflow.original["test_history"]
        )
        assert len(store.list_reviews()) == rounds
    assert len(implementation_calls(workflow)) == 1
    assert len(command_requests(workflow)) == 2
    assert {
        path.name for path in workflow.base.iterdir() if path.is_dir()
    } == workflow.original_directories | {"review-1"}
    assert helpers.git(workflow.repository, "show-ref") == workflow.refs
    assert helpers.git(workflow.repository, "rev-parse", "HEAD") == workflow.baseline


def materialized_expectations(value):
    expected = value.to_dict()
    expected["snapshot_id"] = value.snapshot_id
    expected["source"].pop("files")
    expected["source"].pop("diff")
    logs = {}
    for sequence, command in enumerate(value.executed_suite.commands, 1):
        for stream in ("stdout", "stderr"):
            raw = getattr(command, stream).content
            relative = f"logs/{sequence}/{stream}.bin"
            logs[relative] = raw.hex()
            expected["executed_suite"]["commands"][sequence - 1][stream] = {
                "path": relative,
                "byte_count": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "encoding": "raw bytes; may contain non-UTF-8 data",
            }
    return expected, logs


def assert_chain(workflow, result, *, seats=1, ready=True):
    chain = result["workflow"]
    assert chain["enabled"] is True
    assert chain["stage"] == "review" and chain["outcome"] == "completed"
    assert chain["final_review_round_id"] == result["round_id"]
    source_id = chain["source_review_round_id"]
    assert source_id != result["round_id"]
    old_candidate = workflow.first["candidate"]
    candidate = result["candidate"]
    assert candidate != old_candidate
    assert result["source_current"] and result["verification"] == "verified"
    assert result["current_eligible"] is ready
    assert result["assessment"]["status"] == ("ready" if ready else "needs_rework")
    assert result["implementation_attempt"] == 2
    assert chain["rework"]["candidate"] == chain["tests"]["candidate"] == candidate
    assert chain["rework"]["source_review_round_id"] == source_id
    assert chain["tests"]["test_attempt"] == result["test_attempt"]
    assert (
        helpers.git(workflow.repository, "show", "-s", "--format=%P", candidate)
        == old_candidate
    )
    assert helpers.git(
        workflow.repository, "show", f"{candidate}:frontend/retained.js"
    ) == helpers.RETAINED_BYTES.decode().rstrip("\n")
    run_id = workflow.first["task_run_id"]
    with TaskReviewStore.open_readonly(workflow.database) as store:
        current = store.load_task(run_id)
        original = workflow.original
        assert store.load_task_input(run_id) == original["input"]
        assert current.attempts[0] == original["task"].attempts[0]
        assert (
            store.load_task_history(run_id)[: len(original["history"])]
            == original["history"]
        )
        assert store.load_task_test(run_id, 1) == original["tests"]
        assert store.load_task_test_history(run_id, 1) == original["test_history"]
        assert len(current.attempts) == 2
        first, last = (prepared_for_task(item) for item in current.attempts)
        assert (
            last.policy == first.policy
            and last.dispatch_policy == first.dispatch_policy
        )
        assert last.policy.provider == "claude"
        assert last.workspace.start_commit == old_candidate
        assert last.workspace.task_baseline == workflow.baseline
        assert last.workspace.root != first.workspace.root
        assert current.attempts[-1].request.source.review_round_id == source_id
        old_round = store.load_round(source_id)
        new_round = store.load_round(result["round_id"])
        old_input = load_review_input(store, old_round.snapshot_id)
        new_input = load_review_input(store, new_round.snapshot_id)
        assert old_input.source.candidate == old_candidate
        assert new_input.source.candidate == candidate
        assert old_round.snapshot_id != new_round.snapshot_id
        assert new_input.executed_suite.task_input == original["input"]
        old_suite, new_suite = old_input.executed_suite, new_input.executed_suite
        assert old_suite.prepared.policy == new_suite.prepared.policy
        old_declared = old_suite.prepared.checkout.input
        new_declared = new_suite.prepared.checkout.input
        assert old_declared.commands == new_declared.commands
        assert old_declared.declaration_source == new_declared.declaration_source
        assert new_declared.implementation_attempt == 2
        assert new_suite.test_attempt == result["test_attempt"]
        assert [seat.config for seat in new_round.seats] == [
            seat.config for seat in old_round.seats
        ]
        assert len(new_round.seats) == seats
        assert all(seat.current_attempt == 1 for seat in new_round.seats)
        old_snapshot, old_logs = materialized_expectations(old_input)
        old_diff = old_input.source.diff.encode("utf-8").hex()
    old_show = helpers.successful(*continue_arguments(workflow, "show", source_id))
    assert not old_show["source_current"] and not old_show["current_eligible"]
    assert old_show["candidate"] == old_candidate
    assert result["metadata"]["kind"] == "review"
    assert result["metadata"]["parent_round_id"] is None
    assert result["metadata"]["automatic_rework"] is False
    assert result["metadata"]["rework_from_round_id"] == source_id
    for name in ("timeout", "grace", "executables"):
        assert result["metadata"][name] == old_show["metadata"][name]
    old_captures = [
        row
        for row in reviews(workflow)
        if row["payload"]["source"]["candidate"] == old_candidate
    ]
    assert old_captures and all(row["payload"] == old_snapshot for row in old_captures)
    assert all(row["diff_hex"] == old_diff for row in old_captures)
    assert all(row["logs_hex"] == old_logs for row in old_captures)
    writes = implementation_calls(workflow)
    assert len(writes) == 2 and all(row["provider"] == "claude" for row in writes)
    assert writes[1]["before"]["frontend/status.js"] == helpers.FIRST_BYTES.hex()
    assert (
        old_candidate in writes[1]["prompt"] and "status-blocker" in writes[1]["prompt"]
    )
    assert helpers.FAILED_STDOUT.hex() in writes[1]["prompt"]
    assert helpers.FAILED_STDERR.hex() in writes[1]["prompt"]
    requests = command_requests(workflow)
    assert len(requests) == 4
    assert [row["params"]["command"] for row in requests[:2]] == [
        row["params"]["command"] for row in requests[2:]
    ]
    assert len({row["params"]["cwd"] for row in requests}) == 2
    commands = [
        row
        for row in helpers.records(workflow.base / "test-wire.jsonl")
        if row["kind"] == "command"
    ]
    assert len(commands) == 4
    assert [row["argv"] for row in commands] == [
        row["params"]["command"] for row in requests
    ]
    final_logs = chain["tests"]["commands"][1]["logs"]
    assert Path(final_logs["stdout"]).read_bytes() == helpers.FAILED_STDOUT
    assert Path(final_logs["stderr"]).read_bytes() == helpers.FAILED_STDERR
    assert helpers.git(workflow.repository, "rev-parse", "HEAD") == workflow.baseline
    assert (
        workflow.repository / ".multi-agent/rules.yaml"
    ).read_bytes() == workflow.rules_bytes


def test_task_review_workflow_run_automates_one_rework(workflow):
    prepare(workflow)
    result = helpers.successful(*review_arguments(workflow))
    assert_chain(workflow, result)
    assert len(reviews(workflow)) == 2 and not reviews(workflow, confirmation=True)


def test_task_review_workflow_retry_automates_when_complete(workflow):
    prepare(workflow)
    helpers.configure(
        workflow.reviewers["codex"],
        modes_by_candidate={workflow.first["candidate"]: "model-error"},
    )
    first = helpers.successful(*review_arguments(workflow, dual=True), code=1)
    assert first["assessment"]["status"] == "incomplete"
    assert first["metadata"]["automatic_rework"] is True
    assert_no_downstream(workflow)
    helpers.configure(workflow.reviewers["codex"], modes_by_candidate={})
    result = helpers.successful(
        *continue_arguments(workflow, "retry", first["round_id"])
    )
    assert_chain(workflow, result, seats=2)
    assert len(reviews(workflow)) == 5
    with TaskReviewStore.open_readonly(workflow.database) as store:
        assert [
            seat.current_attempt for seat in store.load_round(first["round_id"]).seats
        ] == [2, 1]


def test_task_review_workflow_confirm_automates(workflow):
    prepare(workflow)
    first = helpers.successful(
        *review_arguments(workflow, dual=True), "--no-auto-rework", code=1
    )
    assert_no_downstream(workflow)
    result = helpers.successful(
        *continue_arguments(workflow, "confirm", first["round_id"], "--auto-rework")
    )
    assert_chain(workflow, result, seats=2)
    assert result["workflow"]["source_review_round_id"] == first["round_id"]
    assert (
        len(reviews(workflow)) == 4 and len(reviews(workflow, confirmation=True)) == 2
    )
    with TaskReviewStore.open_readonly(workflow.database) as store:
        child = store.confirmation_for(first["round_id"])
        assert child is not None
        assert store.load_round(child).snapshot_id == first["snapshot_id"]
    shown = helpers.successful(*continue_arguments(workflow, "show", first["round_id"]))
    assert shown["metadata"]["automatic_rework"] is False


def test_task_review_workflow_confirmation_retry_automates(workflow):
    prepare(workflow)
    first = helpers.successful(
        *review_arguments(workflow, dual=True), "--no-auto-rework", code=1
    )
    helpers.configure(
        workflow.reviewers["codex"],
        modes_by_candidate={workflow.first["candidate"]: "model-error"},
    )
    failed = helpers.successful(
        *continue_arguments(workflow, "confirm", first["round_id"], "--auto-rework"),
        code=1,
    )
    assert failed["assessment"]["status"] == "incomplete"
    assert_no_downstream(workflow, rounds=2)
    helpers.configure(workflow.reviewers["codex"], modes_by_candidate={})
    result = helpers.successful(
        *continue_arguments(workflow, "retry", failed["round_id"], "--auto-rework")
    )
    assert_chain(workflow, result, seats=2)
    assert result["workflow"]["source_review_round_id"] == first["round_id"]
    assert (
        len(reviews(workflow)) == 4 and len(reviews(workflow, confirmation=True)) == 3
    )
    with TaskReviewStore.open_readonly(workflow.database) as store:
        assert [
            seat.current_attempt for seat in store.load_round(failed["round_id"]).seats
        ] == [2, 1]


def repaired_blocker():
    report = helpers.blocking_report()
    report["findings"][0].update(
        id="repaired-blocker",
        quote=helpers.FIXED_BYTES.decode().rstrip("\n"),
        title="Synthetic second blocker",
    )
    return report


def test_task_review_workflow_stops_after_second_blocker(workflow):
    prepare(workflow)
    helpers.configure(workflow.reviewers["codex"], report=repaired_blocker())
    result = helpers.successful(*review_arguments(workflow), code=1)
    assert_chain(workflow, result, ready=False)
    assert len(reviews(workflow)) == 2
    assert result["workflow"]["tests"]["phase"] == "succeeded"
    with TaskReviewStore.open_readonly(workflow.database) as store:
        assert len(store.list_reviews()) == 2


@pytest.mark.parametrize(
    "case", ["incomplete", "unverified", "outside", "test-only", "budget", "disputed"]
)
def test_task_review_workflow_refuses_unqualified(workflow, case):
    prepare(workflow, max_attempts=1 if case == "budget" else 2)
    report = helpers.blocking_report()
    if case == "incomplete":
        helpers.configure(workflow.reviewers["codex"], mode="model-error")
    elif case == "unverified":
        report["findings"][0]["quote"] = "This line is not in the candidate"
    elif case == "outside":
        report["findings"][0].update(path="checks/test.py", quote="import os, sys")
    elif case == "test-only":
        report["findings"] = []
    helpers.configure(
        workflow.reviewers["codex"],
        reports_by_candidate={workflow.first["candidate"]: report},
    )
    if case == "disputed":
        parent = helpers.successful(
            *review_arguments(workflow, dual=True), "--no-auto-rework", code=1
        )
        helpers.configure(
            workflow.reviewers["claude"], confirmation_decision="disagree"
        )
        result = helpers.successful(
            *continue_arguments(
                workflow, "confirm", parent["round_id"], "--auto-rework"
            ),
            code=1,
        )
    else:
        result = helpers.successful(*review_arguments(workflow), code=1)
    assert result["workflow"]["outcome"] == "refused"
    assert result["workflow"]["reason"]
    assert result["workflow"]["tests"] is None
    assert result["workflow"]["final_review_round_id"] is None
    assert_no_downstream(workflow, rounds=2 if case == "disputed" else 1)
    before = {
        provider: (workflow.base / f"{provider}-review-wire.jsonl").read_bytes()
        for provider in ("codex", "claude")
        if (workflow.base / f"{provider}-review-wire.jsonl").exists()
    }
    shown = helpers.successful(
        *continue_arguments(workflow, "show", result["round_id"])
    )
    assert shown["assessment"] == result["assessment"]
    assert_no_downstream(workflow, rounds=2 if case == "disputed" else 1)
    assert all(
        (workflow.base / f"{provider}-review-wire.jsonl").read_bytes() == value
        for provider, value in before.items()
    )


def test_task_review_workflow_nonzero_complete_suite_is_reviewed(workflow):
    prepare(workflow)
    still_failing = b'export const status = "almost-ready";\n'
    helpers.configure(
        workflow.implementation, writes={"frontend/status.js": still_failing.hex()}
    )
    result = helpers.successful(*review_arguments(workflow), code=1)
    assert_chain(workflow, result, ready=False)
    assert [item["exit_code"] for item in result["workflow"]["tests"]["commands"]] == [
        0,
        7,
    ]
    assert result["assessment"]["tests"]["status"] == "failed"
    assert len(reviews(workflow)) == 2


def test_same_implementation_retest_requires_a_new_review(workflow):
    # Both attempts execute this frozen suite; only their owned run directories differ.
    (workflow.repository / "checks/test.py").write_text(
        "from pathlib import Path\n"
        "raise SystemExit(7 if 'tests-1' in Path.cwd().parts else 0)\n"
    )
    helpers.git(
        workflow.repository,
        "commit",
        "--quiet",
        "-am",
        "Fixture for a transient test failure",
    )
    workflow.baseline = helpers.git(workflow.repository, "rev-parse", "HEAD")
    prepare(workflow)
    task = workflow.first["task_run_id"]
    first = workflow.tested
    original = helpers.successful(
        *helpers.review_arguments(
            workflow, task, 1, first["test_attempt"], workflow.reviewers["codex"]
        ),
        code=1,
    )
    assert original["source_current"] and first["details"]["retry_ready"]
    second = helpers.successful(
        *helpers.suite_arguments(workflow, task, 1, workflow.test_bin, suffix="-retry")
    )
    assert [item["exit_code"] for item in second["commands"]] == [0, 0]
    assert second["details"]["input_hash"] == first["details"]["input_hash"]
    assert second["candidate"] == original["candidate"] == first["candidate"]
    shown = helpers.successful(
        "task", "show", "--task", task, "--database", workflow.database
    )
    assert (
        shown["implementation_attempt"] == 1
        and shown["next_action"]["kind"] == "task_review"
    )
    assert shown["next_action"]["test_attempt"] == second["test_attempt"] == 2
    assert [(r["round_id"], r["test_attempt"]) for r in shown["reviews"]] == [
        (original["round_id"], 1)
    ]


@pytest.mark.parametrize("stage", ["rework", "tests", "review"])
def test_task_review_workflow_stops_on_execution_failure(workflow, stage):
    prepare(workflow)
    if stage == "rework":
        helpers.configure(workflow.implementation, mode="model-error")
    elif stage == "tests":
        helpers.configure(workflow.test_bin, mode="malformed")
    else:
        helpers.configure(
            workflow.reviewers["codex"],
            mode="model-error",
            modes_by_candidate={workflow.first["candidate"]: "success"},
        )
    result = helpers.successful(*review_arguments(workflow), code=1)
    chain = result["workflow"]
    assert chain["stage"] == stage and chain["outcome"] == "stopped"
    assert chain["reason"] and len(implementation_calls(workflow)) == 2
    assert (
        len(command_requests(workflow)) == {"rework": 2, "tests": 3, "review": 4}[stage]
    )
    assert len(reviews(workflow)) == (2 if stage == "review" else 1)
    with TaskReviewStore.open_readonly(workflow.database) as store:
        assert len(store.load_task(workflow.first["task_run_id"]).attempts) == 2
    if stage == "review":
        assert result["assessment"]["status"] == "incomplete"
        assert chain["final_review_round_id"] == result["round_id"]
    else:
        assert chain["final_review_round_id"] is None


def test_task_review_workflow_rereview_retry_never_reworks_again(workflow):
    prepare(workflow)
    helpers.configure(
        workflow.reviewers["codex"],
        mode="model-error",
        modes_by_candidate={workflow.first["candidate"]: "success"},
    )
    first = helpers.successful(*review_arguments(workflow), code=1)
    assert first["workflow"]["stage"] == "review"
    assert first["metadata"]["automatic_rework"] is False
    helpers.configure(
        workflow.reviewers["codex"], mode="success", report=repaired_blocker()
    )
    retried = helpers.successful(
        *continue_arguments(workflow, "retry", first["round_id"], "--auto-rework"),
        code=1,
    )
    assert retried["round_id"] == first["round_id"]
    assert retried["snapshot_id"] == first["snapshot_id"]
    assert retried["assessment"]["status"] == "needs_rework"
    assert retried["workflow"]["outcome"] == "refused"
    assert retried["workflow"]["reason"]
    assert len(implementation_calls(workflow)) == 2
    assert len(command_requests(workflow)) == 4 and len(reviews(workflow)) == 3
    with TaskReviewStore.open_readonly(workflow.database) as store:
        assert len(store.load_task(workflow.first["task_run_id"]).attempts) == 2
        assert len(store.list_reviews()) == 2


def boundary_input(workflow, monkeypatch):
    from multi_agent.cli import task_review_cli

    prepare(workflow)
    initial = helpers.successful(
        *review_arguments(workflow), "--no-auto-rework", code=1
    )
    # The real CLI has already settled the initial review. Only the dispatch
    # return is supplied here so interruption can be placed at an exact seam.
    monkeypatch.setattr(task_review_cli, "_continue", lambda args: (initial, False))
    arguments = argparse.Namespace(
        review_operation="retry", database=workflow.database, automatic_rework=True
    )
    return task_review_cli, arguments


@pytest.mark.parametrize("stage", ["rework", "tests", "review"])
def test_task_review_workflow_cancel_stops_followup(
    workflow, monkeypatch, capsys, stage
):
    from multi_agent.cli import task_review_workflow as automatic

    cli, arguments = boundary_input(workflow, monkeypatch)
    observed = []
    if stage == "rework":
        original = automatic._rework

        def interrupted_rework(args):
            result, code = original(args)
            assert code == 0
            observed.append(stage)
            return result, 130

        monkeypatch.setattr(automatic, "_rework", interrupted_rework)
    elif stage == "tests":
        original = automatic._interruptible

        async def interrupted_tests(operation):
            state, interrupted = await original(operation)
            assert not interrupted
            observed.append(stage)
            return state, True

        monkeypatch.setattr(automatic, "_interruptible", interrupted_tests)
    else:
        original = automatic._create_review

        def interrupted_review(*args, **kwargs):
            result, interrupted = original(*args, **kwargs)
            assert not interrupted
            observed.append(stage)
            return result, True

        monkeypatch.setattr(automatic, "_create_review", interrupted_review)

    assert cli.main(arguments) == 130
    output = json.loads(capsys.readouterr().out)
    assert observed == [stage]
    assert output["workflow"]["outcome"] == "interrupted"
    assert output["workflow"]["stage"] == stage
    assert len(implementation_calls(workflow)) == 2
    assert len(command_requests(workflow)) == (2 if stage == "rework" else 4)
    assert len(reviews(workflow)) == (2 if stage == "review" else 1)
    with TaskReviewStore.open_readonly(workflow.database) as store:
        assert len(store.load_task(workflow.first["task_run_id"]).attempts) == 2
        assert len(store.list_reviews()) == (2 if stage == "review" else 1)


@pytest.mark.parametrize("boundary", ["before-rework", "after-rework", "after-tests"])
def test_task_review_workflow_handoff_interrupt_stops(
    workflow, monkeypatch, capsys, boundary
):
    from multi_agent.cli import task_review_workflow as automatic

    cli, arguments = boundary_input(workflow, monkeypatch)
    observed = []
    if boundary == "after-tests":
        original = automatic.execute_task_tests

        async def interrupt_tests(*args, **kwargs):
            await original(*args, **kwargs)
            observed.append(boundary)
            raise KeyboardInterrupt

        monkeypatch.setattr(automatic, "execute_task_tests", interrupt_tests)
    else:
        original = automatic._rework

        def interrupt_rework(*args, **kwargs):
            if boundary == "after-rework":
                original(*args, **kwargs)
            observed.append(boundary)
            raise KeyboardInterrupt

        monkeypatch.setattr(automatic, "_rework", interrupt_rework)

    assert cli.main(arguments) == 130
    captured = capsys.readouterr()
    assert "interrupt" in captured.err.lower() and not captured.out
    assert observed == [boundary]
    assert len(implementation_calls(workflow)) == (
        1 if boundary == "before-rework" else 2
    )
    assert len(command_requests(workflow)) == (4 if boundary == "after-tests" else 2)
    assert len(reviews(workflow)) == 1
    with TaskReviewStore.open_readonly(workflow.database) as store:
        assert len(store.list_reviews()) == 1
        assert len(store.load_task(workflow.first["task_run_id"]).attempts) == (
            1 if boundary == "before-rework" else 2
        )


@pytest.mark.parametrize("stage", ["rework", "tests"])
def test_task_review_workflow_interruption_survives_display_error(
    workflow, monkeypatch, capsys, stage
):
    from multi_agent.cli import task_review_workflow as automatic

    cli, arguments = boundary_input(workflow, monkeypatch)
    observed = []
    failure = OSError(f"Synthetic {stage} display failure after settlement")

    def broken_view(*args, **kwargs):
        assert observed == [stage]
        observed.append("view")
        raise failure

    def forbidden_review(*args, **kwargs):
        pytest.fail("review started after an acknowledged interruption")

    monkeypatch.setattr(automatic, "_create_review", forbidden_review)
    if stage == "rework":
        original = automatic._rework

        def interrupted_rework(args):
            result, code = original(args)
            assert code == 0
            observed.append(stage)
            return result, 130

        async def forbidden_tests(*args, **kwargs):
            pytest.fail("tests started after an acknowledged interruption")

        monkeypatch.setattr(automatic, "_rework", interrupted_rework)
        monkeypatch.setattr(automatic, "view_task_review", broken_view)
        monkeypatch.setattr(automatic, "execute_task_tests", forbidden_tests)
    else:
        original = automatic._interruptible

        async def interrupted_tests(operation):
            state, interrupted = await original(operation)
            assert not interrupted
            observed.append(stage)
            return state, True

        monkeypatch.setattr(automatic, "_interruptible", interrupted_tests)
        monkeypatch.setattr(automatic, "view_task_test", broken_view)

    assert cli.main(arguments) == 130
    captured = capsys.readouterr()
    assert not captured.out
    assert "interrupt" in captured.err.lower()
    assert "OSError" in captured.err and str(failure) in captured.err
    assert "retained" in captured.err.lower()
    assert f"stage {stage}" in captured.err
    assert observed == [stage, "view"]
    assert len(implementation_calls(workflow)) == 2
    assert len(command_requests(workflow)) == (2 if stage == "rework" else 4)
    assert len(reviews(workflow)) == 1
    with TaskReviewStore.open_readonly(workflow.database) as store:
        assert len(store.load_task(workflow.first["task_run_id"]).attempts) == 2
        rounds = store.list_reviews()
        assert len(rounds) == 1 and rounds[0] in captured.err


@pytest.mark.parametrize("operation", ["run", "retry"])
def test_task_review_workflow_entry_interruption_survives_display_error(
    workflow, monkeypatch, capsys, operation
):
    from multi_agent.cli import task_review_cli as cli
    from multi_agent.cli import task_review_workflow as automatic

    prepare(workflow)
    if operation == "retry":
        helpers.configure(workflow.reviewers["codex"], mode="model-error")
        initial = helpers.successful(
            *review_arguments(workflow), "--no-auto-rework", code=1
        )
        argv = continue_arguments(workflow, "retry", initial["round_id"])
    else:
        argv = review_arguments(workflow)
    parser = argparse.ArgumentParser()
    cli.add_parser(parser.add_subparsers(dest="command", required=True))
    arguments = parser.parse_args([str(value) for value in argv])
    observed = []
    failure = OSError(f"Synthetic {operation} review display failure")

    async def interrupted_execution(*args, **kwargs):
        # No reviewer program starts here. This is the existing controller's
        # explicit post-settlement interruption result at the CLI boundary.
        observed.append("settled")
        return True

    def broken_view(*args, **kwargs):
        assert observed == ["settled"]
        observed.append("view")
        raise failure

    def forbidden_continuation(*args, **kwargs):
        pytest.fail("automatic continuation started after interrupted review")

    monkeypatch.setattr(cli, "_execute", interrupted_execution)
    monkeypatch.setattr(cli, "_view", broken_view)
    monkeypatch.setattr(automatic, "continue_task_review", forbidden_continuation)
    assert cli.main(arguments) == 130
    captured = capsys.readouterr()
    assert not captured.out
    assert "interrupt" in captured.err.lower()
    assert "OSError" in captured.err and str(failure) in captured.err
    assert "retained" in captured.err.lower()
    assert observed == ["settled", "view"]
    assert len(implementation_calls(workflow)) == 1
    assert len(command_requests(workflow)) == 2
    assert len(reviews(workflow)) == (1 if operation == "retry" else 0)
    assert_no_downstream(workflow)


@pytest.mark.parametrize("entry", ["task-review", "task"])
def test_task_review_workflow_inner_rework_display_preserves_interrupt(
    workflow, monkeypatch, capsys, entry
):
    from multi_agent.cli import task_cli
    from multi_agent.cli.main import main

    prepare(workflow)
    argv = review_arguments(workflow)
    if entry == "task":
        parent = helpers.successful(*argv, "--no-auto-rework", code=1)
        argv = [
            "task",
            "rework",
            parent["round_id"],
            "--database",
            workflow.database,
            "--directory",
            workflow.base / "manual-rework",
        ]
    interruptible = task_cli._interruptible
    observed = []

    async def settled_interruption(operation):
        state, interrupted = await interruptible(operation)
        assert not interrupted and state.current_attempt.number == 2
        observed.append("settled")
        return state, True

    def broken_task_view(database, *, task_run_id):
        assert observed == ["settled"]
        assert task_run_id == workflow.first["task_run_id"]
        observed.append("inner view")
        raise OSError("cannot read the settled rework result")

    monkeypatch.setattr(task_cli, "_interruptible", settled_interruption)
    monkeypatch.setattr(task_cli, "view_task", broken_task_view)
    code = main([str(arg) for arg in argv])
    captured = capsys.readouterr()
    assert observed == ["settled", "inner view"]
    assert len(implementation_calls(workflow)) == 2
    assert len(command_requests(workflow)) == 2 and len(reviews(workflow)) == 1
    with TaskReviewStore.open_readonly(workflow.database) as store:
        assert len(store.load_task(workflow.first["task_run_id"]).attempts) == 2
        assert len(store.list_reviews()) == 1
    assert code == 130, captured.err
    assert not captured.out
    assert "OSError: cannot read the settled rework result" in captured.err
    if entry == "task-review":
        assert "stage rework" in captured.err


def test_task_review_workflow_competing_reservation_reports_refused(
    workflow, monkeypatch, capsys
):
    from multi_agent.cli import task_cli
    from multi_agent.cli import task_review_workflow as automatic
    from multi_agent.cli.main import main

    prepare(workflow)
    original_enabled = automatic._enabled
    winner_directory = workflow.base / "competing-rework"
    run_id = workflow.first["task_run_id"]
    winner = {}

    def enable_after_competitor(database, parent_id, override):
        enabled = original_enabled(database, parent_id, override)
        # The initiating review already observed attempt 1. Another caller now
        # takes the sole reservation before this workflow attempts to acquire it.
        result, code = task_cli._rework(
            argparse.Namespace(
                database=database, review_round_id=parent_id, directory=winner_directory
            )
        )
        assert code == 0 and result["implementation_attempt"] == 2
        winner["candidate"] = result["candidate"]
        with TaskReviewStore.open_readonly(database) as store:
            winner["history"] = store.load_task_history(run_id)
        return enabled

    monkeypatch.setattr(automatic, "_enabled", enable_after_competitor)
    assert main([str(arg) for arg in review_arguments(workflow)]) == 1
    result = json.loads(capsys.readouterr().out)
    chain = result["workflow"]
    assert result["implementation_attempt"] == 1 and not result["source_current"]
    assert result["candidate"] == workflow.first["candidate"]
    assert chain["rework"]["implementation_attempt"] == 2
    assert chain["rework"]["candidate"] == winner["candidate"]
    assert chain["rework"]["next_action"]["kind"] == "user_decision"
    assert "reservation is already used" in chain["reason"]
    assert chain["stage"] == "rework" and chain["outcome"] == "refused"
    assert chain["tests"] is None and chain["final_review_round_id"] is None
    assert len(implementation_calls(workflow)) == 2
    assert len(command_requests(workflow)) == 2 and len(reviews(workflow)) == 1
    assert not list(workflow.base.glob("automatic-*"))
    with TaskReviewStore.open_readonly(workflow.database) as store:
        assert store.load_task_history(run_id) == winner["history"]
        assert len(store.load_task(run_id).attempts) == 2
        assert len(store.list_reviews()) == 1
