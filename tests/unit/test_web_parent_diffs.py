"""Check real task and integration revision evidence through the HTTP API."""

import sys

import test_task_workflow_cli as helpers
from test_integration_cli import create_args, implement
from test_integration_rework_cli import _parallel_members, _reviewers, _structural_suite
from test_integration_verification_cli import (
    _advance_target,
    _arguments,
    _review_arguments,
    _test_arguments,
)
from test_web_artifact_coordinates import legacy_locator
from test_web_project_flow import _get, _request
from test_web_workspace import _serve

from multi_agent.core.task_execution import task_execution_hash

workflow = helpers.workflow


def _assert_parent_diff(api, kind, identifier, baseline, parent, candidate):
    url = f"diffs/{kind}/{identifier}"
    cumulative = _get(api, url)
    incremental = _get(api, url + "?against=parent")
    assert cumulative["baseline"] == incremental["baseline"] == baseline
    assert cumulative["candidate"] == incremental["candidate"] == candidate
    assert "parent_candidate" not in cumulative
    assert incremental["parent_candidate"] == parent
    assert "unavailable_code" not in incremental
    return cumulative["diff"], incremental["diff"]


def _assert_log_coordinates(api, task_id, tested, reviewed):
    test = tested["test_attempt"]
    selection = {"implementation_attempt": ["1"], "test_attempt": [str(test)]}
    test_logs = _get(
        api,
        f"resources/task-test/{task_id}?implementation_attempt=1&test_attempt={test}",
    )["artifacts"]
    assert {row["coordinates"]["sequence"] for row in test_logs} == {1, 2}
    for row in test_logs:
        key = row["key"]
        _, _, sequence, index = key.split("-")
        assert key == f"test-{test}-{sequence}-{index}"
        assert row["coordinates"] == {
            "test_attempt": test,
            "sequence": int(sequence),
            "log_index": int(index),
        }
        assert row["artifact_id"] == legacy_locator(
            "task-test", task_id, selection, key, row["sha256"]
        )

    for stream, raw in (
        ("stdout", helpers.FAILED_STDOUT),
        ("stderr", helpers.FAILED_STDERR),
    ):
        (log,) = [
            row
            for row in test_logs
            if row["coordinates"]["sequence"] == 2 and row["stream"] == stream
        ]
        page = _get(api, f"artifacts/{log['artifact_id']}")
        assert page["stream"] == stream and page["text"] == raw.decode(
            "utf-8", "replace"
        )

    for kind, identifier in (
        ("task-review", reviewed["round_id"]),
        ("review", reviewed["round_id"]),
        ("snapshot", reviewed["snapshot_id"]),
    ):
        frozen = _get(api, f"resources/{kind}/{identifier}")["artifacts"]
        assert {
            (row["coordinates"]["frozen_index"], row["stream"]) for row in frozen
        } == {(1, "stdout"), (1, "stderr"), (2, "stdout"), (2, "stderr")}
        for row in frozen:
            assert (
                row["key"]
                == f"frozen-{row['coordinates']['frozen_index']}-{row['stream']}"
            )
            assert row["artifact_id"] == legacy_locator(
                kind, identifier, {}, row["key"], row["sha256"]
            )


def test_task_parent_diff_uses_recorded_rework_candidate_and_logs_keep_coordinates(
    workflow,
):
    workflow.database = workflow.base / "reviews.sqlite3"
    path = "frontend/status.js"
    retained = "frontend/retained.js"
    task, implementation = implement(
        workflow,
        "T-0210",
        "frontend/**",
        {path: helpers.FIRST_BYTES, retained: helpers.RETAINED_BYTES},
    )
    identifier = task["task_run_id"]
    url = f"diffs/task/{identifier}"
    suffix = "?against=parent"
    test_program = workflow.base / "bin" / "codex-workflow-test"
    test_program.write_text(
        f"#!{sys.executable}\n" + (helpers.FAKES / "test_cli.py").read_text()
    )
    test_program.chmod(0o755)
    test_program.with_suffix(".json").write_text("{}")

    with _serve(workflow.repository, workflow.base, {}) as api:
        initial = _get(api, url + suffix)
        assert (
            initial["unavailable_code"] == "no_parent" and initial["unavailable_reason"]
        )
        assert initial["diff"] is None and initial["parent_candidate"] is None
        assert initial["candidate"] == task["candidate"]
        for invalid in (
            url + "?against=baseline",
            url + "?against=parent&against=parent",
            "diffs/snapshot/irrelevant?against=parent",
        ):
            assert _request(api, "GET", "/api/" + invalid)[0] == 400
        api.jobs.read_gate.begin_job("active-for-parent-diff")
        try:
            blocked = _get(api, url + suffix)
            assert (
                blocked["lock_busy"] is True
                and blocked["unavailable_code"] == "lock_busy"
            )
            assert blocked["diff"] is None and blocked["parent_candidate"] is None
        finally:
            api.jobs.read_gate.finish_job()

        tested = helpers.successful(
            *helpers.suite_arguments(workflow, identifier, 1, test_program), code=1
        )
        reviewer = helpers.executable(
            workflow, "codex", "review", report=helpers.blocking_report()
        )
        reviewed = helpers.successful(
            *helpers.review_arguments(
                workflow, identifier, 1, tested["test_attempt"], reviewer
            ),
            code=1,
        )
        _assert_log_coordinates(api, identifier, tested, reviewed)
        helpers.configure(
            implementation,
            task_hash=task_execution_hash(task["details"]["input"]["card"]),
            writes={path: helpers.FIXED_BYTES.hex()},
        )
        revised = helpers.successful(
            "task",
            "rework",
            reviewed["round_id"],
            "--database",
            workflow.database,
            "--directory",
            workflow.base / "rework",
        )
        cumulative, incremental = _assert_parent_diff(
            api,
            "task",
            identifier,
            workflow.baseline,
            task["candidate"],
            revised["candidate"],
        )
        assert retained in cumulative and retained not in incremental
        assert '-export const status = "draft";' in incremental
        assert '+export const status = "ready";' in incremental


def test_integration_parent_diff_checks_lineage_child_and_prefers_rebase(workflow):
    workflow.database = workflow.base / "reviews.sqlite3"
    test_program = _structural_suite(workflow)
    members, writer, owner_hash = _parallel_members(workflow)
    original = helpers.successful(*create_args(workflow, *members))
    source_id = original["integration_id"]
    source_commit = original["candidate"]["commit"]
    helpers.successful(
        *_test_arguments(workflow, original, test_program, "original-tests")
    )
    helpers.configure(
        writer,
        dispatches=None,
        task_hash=owner_hash,
        writes={"frontend/status.js": helpers.FIXED_BYTES.hex()},
    )
    outcome = helpers.successful(
        *_review_arguments(
            workflow,
            original,
            _reviewers(workflow, source_commit),
            "original-review",
            dual=True,
        )
    )
    child = outcome["revision"]
    child_id = child["integration_id"]
    child_commit = child["candidate"]["commit"]
    with _serve(workflow.repository, workflow.base, {}) as api:
        url = f"diffs/integration/{source_id}"
        assert (
            _get(api, f"resources/integration/{source_id}")["data"]["rework"][
                "child_id"
            ]
            == child_id
        )
        original_diff = _get(api, url + "?against=parent")
        assert original_diff["unavailable_code"] == "no_parent"
        assert (
            original_diff["diff"] is None and original_diff["parent_candidate"] is None
        )
        cumulative, incremental = _assert_parent_diff(
            api, "integration", child_id, workflow.baseline, source_commit, child_commit
        )
        assert (
            "frontend/other.js" in cumulative and "frontend/other.js" not in incremental
        )
        assert '-export const status = "draft";' in incremental
        assert '+export const status = "ready";' in incremental

        advanced = _advance_target(workflow, original)
        rebased = helpers.successful(*_arguments(workflow, "rebase", child))
        rebased_url = f"diffs/integration/{rebased['integration_id']}"
        _, delta = _assert_parent_diff(
            api,
            "integration",
            rebased["integration_id"],
            advanced,
            child_commit,
            rebased["candidate"]["commit"],
        )
        assert ".multi-agent/rules.yaml" in delta and "frontend/status.js" not in delta

        helpers.git(
            workflow.repository, "update-ref", "-d", rebased["candidate"]["ref"]
        )
        unavailable = _get(api, rebased_url + "?against=parent")
        assert (
            unavailable["unavailable_code"] == "not_verified"
            and unavailable["unavailable_reason"]
        )
        assert unavailable["diff"] is None and unavailable["parent_candidate"] is None
        assert _get(api, rebased_url)["unavailable_code"] == "not_verified"
