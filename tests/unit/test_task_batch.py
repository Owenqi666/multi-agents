"""Run independently planned tasks through the batch module CLI."""

import json
import signal
import subprocess
import sys
import time

import pytest
import test_task_workflow_cli as helpers

from multi_agent.cli.planning_evidence import bind_planning_task
from multi_agent.cli.task_context import capture_task_policy, prepare_task_launch
from multi_agent.core.task_execution import TaskDispatchPolicy, task_execution_hash
from multi_agent.core.task_state import TaskPhase
from multi_agent.store.planning_store import PlanningStore
from multi_agent.store.task_review_store import TaskReviewStore

workflow = helpers.workflow


def plan(workflow, task_id, scope, *, depends_on=None):
    helpers.configure(
        workflow.executables["claude"], task_id=task_id, write_scope=[scope]
    )
    arguments = list(helpers.plan_arguments(workflow))
    arguments[arguments.index("--directory") + 1] = workflow.base / task_id
    workflow.requirements.write_text(f"Implement the requested change to {scope}.\n")
    if depends_on is not None:
        declarations = workflow.base / f"{task_id}-declarations.json"
        declarations.write_text(
            json.dumps(
                {
                    "dependencies": [
                        {
                            "task_id": depends_on,
                            "baseline_commit": workflow.baseline,
                            "source": "user_declared",
                            "detail": "Declared dependency for batch admission",
                        }
                    ],
                    "decisions": [],
                }
            )
        )
        arguments.extend(("--declarations", declarations))
    planned = helpers.successful(*arguments)
    assert planned["phase"] == "ready"
    return planned


def batch_arguments(workflow, first, second, implementation):
    arguments = helpers.run_arguments(
        workflow, first["planning_run_id"], implementation, directory="batch"
    )
    arguments[1] = "run-batch"
    arguments.insert(3, second["planning_run_id"])
    return arguments


def test_run_batch_implements_ready_plans_concurrently(workflow):
    files = {"frontend/status.js": b"status\n", "frontend/other.js": b"other\n"}
    plans = [plan(workflow, f"T-020{index}", path) for index, path in enumerate(files)]
    dispatches = {
        task_execution_hash(planned["card"]): {path: contents.hex()}
        for planned, (path, contents) in zip(plans, files.items(), strict=True)
    }
    barrier = workflow.base / "barrier"
    barrier.mkdir()
    implementation = helpers.executable(
        workflow,
        "claude",
        "implementation",
        dispatches=dispatches,
        barrier=str(barrier),
    )
    result = helpers.successful(*batch_arguments(workflow, *plans, implementation))

    # Each fake writes its marker, then waits for its peer before writing any code.
    # A serial implementation cannot produce two successful candidates.
    worktrees = {(barrier / key).read_text() for key in dispatches}
    assert len(worktrees) == 2
    by_plan = {task["planning_run_id"]: task for task in result["tasks"]}
    assert len(result["tasks"]) == len(plans)
    assert set(by_plan) == {planned["planning_run_id"] for planned in plans}
    for planned, (path, contents) in zip(plans, files.items(), strict=True):
        task = by_plan[planned["planning_run_id"]]
        shown = helpers.successful(
            "task",
            "show",
            "--task",
            task["task_run_id"],
            "--database",
            workflow.database,
        )
        candidate = task["candidate"]
        assert candidate and shown["candidate"] == candidate
        assert task["verification"] == shown["verification"] == "verified"
        assert helpers.git(workflow.repository, "rev-parse", f"{candidate}^") == (
            workflow.baseline
        )
        assert (
            helpers.git(
                workflow.repository, "diff", "--name-only", f"{candidate}^", candidate
            )
            == path
        )
        assert helpers.git(workflow.repository, "show", f"{candidate}:{path}") == (
            contents.decode().rstrip("\n")
        )
    assert helpers.git(workflow.repository, "rev-parse", "HEAD") == workflow.baseline


def test_run_batch_interrupt_preserves_the_settled_candidate(workflow):
    fast = plan(workflow, "T-0200", "frontend/status.js")
    slow = plan(workflow, "T-0201", "frontend/other.js")
    fast_hash, slow_hash = (
        task_execution_hash(planned["card"]) for planned in (fast, slow)
    )
    barrier = workflow.base / "barrier"
    barrier.mkdir()
    implementation = helpers.executable(
        workflow,
        "claude",
        "implementation",
        dispatches={
            fast_hash: {"frontend/status.js": b"status\n".hex()},
            slow_hash: {"frontend/other.js": b"other\n".hex()},
        },
        barrier=str(barrier),
        wait_task_hash=slow_hash,
    )
    args = batch_arguments(workflow, fast, slow, implementation)
    args[args.index("--timeout") + 1] = "90"
    child = subprocess.Popen(
        [sys.executable, "-m", "multi_agent", *map(str, args)],
        cwd=helpers.REPOSITORY,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 60
        with TaskReviewStore.open_readonly(workflow.database) as store:
            while child.poll() is None and time.monotonic() < deadline:
                fast_id = store.load_planning(fast["planning_run_id"]).bound_task_run_id
                slow_id = store.load_planning(slow["planning_run_id"]).bound_task_run_id
                if fast_id is not None and slow_id is not None:
                    slow_state = store.load_task(slow_id)
                    slow_attempt = slow_state.current_attempt
                    if (
                        store.load_task(fast_id).delivery_ready
                        and slow_state.phase == TaskPhase.IMPLEMENTING
                        and slow_attempt is not None
                        and slow_attempt.runtime.session_id is not None
                    ):
                        break
                time.sleep(0.01)
            else:
                pytest.fail("fast task did not settle while the slow task was running")
        child.send_signal(signal.SIGINT)
        stdout, stderr = child.communicate(timeout=30)
        assert child.returncode == 130, (stdout, stderr)
    finally:
        if child.poll() is None:
            child.send_signal(signal.SIGINT)
            try:
                child.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate(timeout=10)

    by_id = {task["task_run_id"]: task for task in json.loads(stdout)["tasks"]}
    for task_id in (slow_id, fast_id):
        shown = helpers.successful(
            "task", "show", "--task", task_id, "--database", workflow.database
        )
        task = by_id[task_id]
        assert task["candidate"] == shown["candidate"]
        assert task["verification"] == shown["verification"]
        assert shown["attempts"][-1]["reconciliation"]["process_stopped"]
        if task_id == slow_id:
            assert task["phase"] == shown["phase"] == "cancelled"
            assert shown["candidate"] is None
            assert shown["verification"] == "unverified"
        else:
            assert shown["candidate"] is not None
            assert shown["verification"] == "verified"


@pytest.mark.parametrize("conflict", ["overlap", "dependency"])
def test_run_batch_rejects_incompatible_plans_before_dispatch(workflow, conflict):
    first = plan(workflow, "T-0200", "frontend/status.js")
    second = plan(
        workflow,
        "T-0201",
        "frontend/**" if conflict == "overlap" else "frontend/other.js",
        depends_on=first["card"]["id"] if conflict == "dependency" else None,
    )
    implementation = helpers.executable(
        workflow,
        "claude",
        "implementation",
        task_hash=task_execution_hash(first["card"]),
        writes={},
    )
    rejected = helpers.module_cli(
        *batch_arguments(workflow, first, second, implementation)
    )
    assert rejected.returncode == 2, (rejected.stdout, rejected.stderr)
    assert "T-0200" in rejected.stderr and "T-0201" in rejected.stderr
    assert not (workflow.base / "implementation-wire.jsonl").exists()
    for planned in (first, second):
        shown = helpers.successful(
            "task",
            "show",
            "--plan",
            planned["planning_run_id"],
            "--database",
            workflow.database,
        )
        assert shown["task_run_id"] is None


def test_run_batch_checks_unsettled_owners_before_reserving_members(workflow):
    orphan = plan(workflow, "T-0202", "frontend/orphan.js")
    plans = [
        plan(workflow, "T-0200", "frontend/status.js"),
        plan(workflow, "T-0201", "frontend/other.js"),
    ]
    implementation = helpers.executable(
        workflow,
        "claude",
        "implementation",
        task_hash=task_execution_hash(orphan["card"]),
        writes={},
    )
    planning_id = orphan["planning_run_id"]
    with PlanningStore(workflow.database) as store:
        bind_planning_task(store, planning_id, "orphan", "prepared-orphan")
    with prepare_task_launch(
        workflow.database,
        "orphan",
        planning_id,
        repository=workflow.repository,
        directory=workflow.base / "orphan",
        policy=capture_task_policy(
            "claude", implementation, max_attempts=2, timeout=20, grace=0.2
        ),
        dispatch_policy=TaskDispatchPolicy(None, None, ()),
    ):
        pass  # Simulate a prior controller that left a reservation without stopping facts.

    rejected = helpers.module_cli(*batch_arguments(workflow, *plans, implementation))
    assert rejected.returncode == 2, (rejected.stdout, rejected.stderr)
    assert "stopping is unknown: orphan/1" in rejected.stderr
    assert not (workflow.base / "implementation-wire.jsonl").exists()
    with PlanningStore(workflow.database) as store:
        assert store.list_recoverable_tasks() == ("orphan",)
