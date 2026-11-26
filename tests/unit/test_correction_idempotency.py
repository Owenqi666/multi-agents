"""Duplicate linked planning requests share a durable plan across submitters."""

import fcntl
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import asdict, replace
from threading import Barrier, Event

import pytest
import test_task_workflow_cli as helpers
from test_planning_store import inputs
from test_web_project_flow import _get, _job, _request
from test_web_project_resources import _repository
from test_web_review_findings import _review
from test_web_workspace import _serve

from multi_agent.cli import task_cli
from multi_agent.cli.main import parser
from multi_agent.cli.snapshot import freeze_diff
from multi_agent.cli.workspaces import executor_lock
from multi_agent.store.planning_store import PlanningStore
from multi_agent.store.review_annotations import FindingAnchor, append_activity
from multi_agent.store.task_review_store import TaskReviewStore
from multi_agent.web.actions import prepare_action
from multi_agent.web.review_findings import findings
from multi_agent.web.workspace import Workspace

workflow = helpers.workflow


def source(workspace):
    patch = freeze_diff(
        "candidate patch",
        "Fix the finding",
        ("works",),
        repository=workspace.repository,
    )
    _review(workspace, patch)
    member = findings(workspace, "round")["groups"][0]["members"][0]
    return FindingAnchor.from_dict(member["anchor"])


@pytest.fixture
def correction(tmp_path):
    repo, _, candidate = _repository(tmp_path / "project")
    workspace = Workspace.open(repo, tmp_path / "data")
    anchor = source(workspace)
    value, _ = inputs()
    rules = replace(
        value.frozen_rules, repository=str(repo / ".git"), baseline_commit=candidate
    )
    return workspace, anchor, replace(value, frozen_rules=rules)


def test_existing_corrections_use_first_record_without_rewriting_history(correction):
    workspace, anchor, value = correction
    with PlanningStore(workspace.database) as store:
        # The original v14 writer could persist duplicates. Both historical links survive.
        for prior in ("old-first", "old-second"):
            with store._connection:
                store._connection.execute("BEGIN IMMEDIATE")
                store._insert_planning(prior, value, "then")
                append_activity(
                    store._connection, anchor, "linked_plan", {}, planning_run_id=prior
                )
    with PlanningStore(workspace.database) as store:
        assert (
            store.create_planning("duplicate", value, "now", review_anchor=anchor)
            == "old-first"
        )
        changed = replace(
            value, requirements=value.requirements + " Include the boundary case."
        )
        assert (
            store.create_planning("different", changed, "now", review_anchor=anchor)
            is None
        )
        # An unlinked planning request is still independently creatable for the same input.
        assert store.create_planning("standalone", value, "now") is None
        assert store.load_planning("standalone").planning_input == value
    assert (
        task_cli.view_task(workspace.database, planning_run_id="standalone")[
            "next_action"
        ]["kind"]
        == "reconciliation_required"
    )
    member = findings(workspace, "round")["groups"][0]["members"][0]
    assert [item["planning_run_id"] for item in member["activity"]] == [
        "old-first",
        "old-second",
        "different",
    ]


@pytest.mark.parametrize("winner", ("original", "resubmission"))
def test_original_and_restart_late_writer_converge_in_both_orders(correction, winner):
    workspace, anchor, value = correction
    joined = Barrier(2)
    uncommitted = Event()
    competing = Event()
    overlapped = Event()

    def submit(request):
        with PlanningStore(workspace.database) as store:

            def coordinate(sql):
                if request == winner and "INSERT INTO planning_runs" in sql:
                    uncommitted.set()
                    if competing.wait(timeout=5):
                        overlapped.set()
                elif request != winner and sql == "BEGIN IMMEDIATE":
                    competing.set()

            store._connection.set_trace_callback(coordinate)
            joined.wait(timeout=5)
            if request != winner:
                assert uncommitted.wait(timeout=5)
            existing = store.create_planning(
                request, value, "now", review_anchor=anchor
            )
            return existing or request

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(submit, ("original", "resubmission"))) == [winner, winner]
    assert overlapped.is_set()
    with PlanningStore(workspace.database) as store:
        assert (
            store._connection.execute("SELECT count(*) FROM planning_runs").fetchone()[
                0
            ]
            == 1
        )
    [entry] = findings(workspace, "round")["groups"][0]["members"][0]["activity"]
    assert entry["planning_run_id"] == winner


def test_reopened_service_returns_existing_failed_plan_without_invoking_provider(
    workflow,
):
    workspace = Workspace.open(workflow.repository, workflow.base / "data")
    anchor = source(workspace)
    args = {
        "review_anchor": asdict(anchor),
        "baseline": workflow.baseline,
        "requirements": helpers.REQUIREMENTS,
    }
    programs = {key: str(path) for key, path in workflow.executables.items()}
    helpers.configure(
        workflow.executables["claude"], role="planning", mode="model-error"
    )
    with _serve(workspace.repository, workspace.data_dir, programs) as service:
        first = _job(service, "plan.create", **args, exit_code=1)
        prior = first["result"]["planning_run_id"]
    recorded = workflow.capture.read_bytes()
    with _serve(workspace.repository, workspace.data_dir, programs) as service:
        for busy in (False, True):
            # A previous service's surviving child may still own the executor lease.
            with executor_lock(workspace.repository) if busy else nullcontext():
                reply = _job(service, "plan.create", **args)
            assert reply["result"] == {
                "operation": "plan",
                "planning_run_id": prior,
                "reused": True,
            }
            assert any(
                item["kind"] == "plan" and item["id"] == prior
                for item in reply["resources"]
            )
    assert workflow.capture.read_bytes() == recorded
    [entry] = findings(workspace, "round")["groups"][0]["members"][0]["activity"]
    assert entry["planning_run_id"] == prior


@pytest.mark.parametrize("exhaust_budget", (False, True))
def test_pending_v14_correction_can_start_after_restart(workflow, exhaust_budget):
    workspace = Workspace.open(workflow.repository, workflow.base / "data")
    anchor = source(workspace)
    request = {
        "review_anchor": asdict(anchor),
        "baseline": workflow.baseline,
        "requirements": helpers.REQUIREMENTS,
    }
    programs = {key: str(path) for key, path in workflow.executables.items()}
    # Advertise the frozen planning reviewer in the synthetic provider's catalog.
    transport = workflow.base / "bin" / "codex-transport.py"
    transport.write_text(transport.read_text().replace('"model-1"', '"gpt-6.1-sol"'))
    with _serve(workspace.repository, workspace.data_dir, programs) as service:
        with (workspace.data_dir / ".planning-controller.lock").open("a+b") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            refused = _job(service, "plan.create", **request, exit_code=2)
        assert refused["error_kind"] == "refused"
    with TaskReviewStore.open_readonly(workspace.database) as store:
        assert store._connection.execute("PRAGMA user_version").fetchone()[0] == 14
    member = findings(workspace, "round")["groups"][0]["members"][0]
    identifier = member["activity"][0]["planning_run_id"]
    path = f"resources/plan/{identifier}?implementation=1"
    if exhaust_budget:
        helpers.configure(workflow.executables["claude"], mode="model-error")
    with _serve(workspace.repository, workspace.data_dir, programs) as service:
        assert _job(service, "plan.create", **request)["result"] == {
            "operation": "plan",
            "planning_run_id": identifier,
            "reused": True,
        }
        pending = _get(service, path)["data"]
        assert pending["attempts"] == {"draft": [], "review": []}
        assert (
            pending["retry_configurable"]
            and pending["next_action"]["kind"] == "plan_retry"
        )
        if not exhaust_budget:
            recovered = _job(service, "plan.retry", planning_run_id=identifier)[
                "result"
            ]
            assert (
                recovered["planning_run_id"] == identifier
                and recovered["phase"] == "ready"
            )
            assert {
                stage: len(attempts)
                for stage, attempts in recovered["attempts"].items()
            } == {"draft": 1, "review": 1}
            ready = _get(service, path)["data"]
            assert (
                ready["dispatch_configurable"] and ready["next_action"]["kind"] == "run"
            )
            return
        for attempt in (1, 2):
            result = _job(
                service, "plan.retry", planning_run_id=identifier, exit_code=1
            )["result"]
            assert result["planning_run_id"] == identifier
            assert result["budget"]["claude"]["used"] == attempt
        captured = workflow.capture.read_bytes()
        assert (
            _job(service, "plan.create", **request)["result"]["planning_run_id"]
            == identifier
        )
        for suffix in (path, f"resources/plan/{identifier}"):
            exhausted = _get(service, suffix)["data"]
            assert exhausted["next_action"]["kind"] == "budget_exhausted"
        assert not _get(service, path)["data"]["retry_configurable"]
        status, _ = _request(
            service,
            "POST",
            "/api/jobs",
            {"action": "plan.retry", "args": {"planning_run_id": identifier}},
        )
        assert status == 409 and workflow.capture.read_bytes() == captured


@pytest.mark.parametrize("winner", ("original", "retry"))
def test_first_draft_race_after_original_releases_repository_lease(
    workflow, monkeypatch, winner
):
    workspace = Workspace.open(workflow.repository, workflow.base / "data")
    anchor = source(workspace)
    programs = {key: str(path) for key, path in workflow.executables.items()}
    helpers.configure(workflow.executables["claude"], mode="model-error")
    execute = task_cli.execute_planning_attempt

    def command(action, args):
        directory = workspace.data_dir / action / "input"
        directory.mkdir(parents=True)
        return prepare_action(
            action,
            args,
            workspace=workspace,
            executables=programs,
            directory=directory,
            tool_directories=(),
            runtime_read_paths=(),
        )[4:]

    with _serve(workspace.repository, workspace.data_dir, programs) as service:

        async def compete(database, identifier, stage, **kwargs):
            # The interrupted creator is still alive here, after committing the plan
            # and releasing the repository lock, before acquiring the controller lock.
            with executor_lock(workspace.repository):
                pending = _get(
                    service, f"resources/plan/{identifier}?implementation=1"
                )["data"]
                assert (
                    pending["retry_configurable"] and not pending["attempts"]["draft"]
                )
            if winner == "retry":
                recovered = _job(
                    service, "plan.retry", planning_run_id=identifier, exit_code=1
                )
                assert recovered["result"]["planning_run_id"] == identifier
                captured = workflow.capture.read_bytes()
                with pytest.raises(
                    ValueError, match="prepared planning runs do not reread"
                ):
                    await execute(database, identifier, stage, **kwargs)
            else:
                delayed = command("plan.retry", {"planning_run_id": identifier})
                await execute(database, identifier, stage, **kwargs)
                captured = workflow.capture.read_bytes()
                late = helpers.module_cli(*delayed)
                assert (
                    late.returncode == 2
                    and json.loads(late.stdout)["error"]["code"] == "cli_refused"
                )
            assert workflow.capture.read_bytes() == captured
            with TaskReviewStore.open_readonly(database) as store:
                state = store.load_planning(identifier)
                assert len(state.draft_attempts) == 1 and not state.review_attempts
                return state

        monkeypatch.setattr(task_cli, "execute_planning_attempt", compete)
        original = command(
            "plan.create",
            {
                "review_anchor": asdict(anchor),
                "baseline": workflow.baseline,
                "requirements": helpers.REQUIREMENTS,
            },
        )
        result, status = task_cli._plan(parser().parse_args(original))
        assert status == 1 and result["budget"]["claude"]["used"] == 1
