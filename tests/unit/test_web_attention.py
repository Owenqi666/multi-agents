"""Exercise workbench attention across goal, stage, and verified integration states."""

import fcntl
import json
import sys
from pathlib import Path

import test_project_cli as project_helpers
import test_task_review_workflow as task_reviews
import test_task_workflow_cli as workflow_helpers
from test_integration_cli import create_args, implement
from test_integration_verification_cli import (
    _arguments,
    _integration,
    _review_arguments,
    _test_arguments,
)
from test_project_stage import _model_card
from test_web_independent_review import INPUT
from test_web_job_activity import _until
from test_web_project_flow import FABLE, OPUS, SOL, _fixture, _get, _job, _request
from test_web_project_resources import _repository
from test_web_workspace import _serve

from multi_agent.core.planning import PlanningRequest
from multi_agent.core.project_plan import ConfirmedGoal
from multi_agent.store.project_store import ProjectStore
from multi_agent.web.attention import read_attention
from multi_agent.web.gate import ReadGate
from multi_agent.web.workspace import Workspace

workflow = workflow_helpers.workflow
document = project_helpers.document


def test_goal_and_multiple_stage_failures_follow_scoped_state(tmp_path, document):
    local, baseline, _ = _repository(tmp_path / "local")
    foreign, other_base, _ = _repository(tmp_path / "other")
    ws = Workspace.open(local, tmp_path / "data")
    gate = ReadGate()
    request = PlanningRequest("codex", "model", "high")
    assert read_attention(ws, gate) == {"items": [], "complete": True}
    gate.begin_job("first-job")
    assert read_attention(ws, gate) == {"items": [], "complete": False}
    ws.database.touch()
    assert read_attention(ws, gate) == {"items": [], "complete": False}
    gate.finish_job()
    document["stages"] = document["stages"][:2]
    document["stages"][1]["depends_on"] = []
    with ProjectStore(ws.database) as store:
        for name, repo, sha in (
            ("local", local, baseline),
            ("other", foreign, other_base),
        ):
            store.confirm_goal(
                ConfirmedGoal(
                    name,
                    name + "-goal",
                    str(repo),
                    str(repo / ".git"),
                    sha,
                    "original",
                    "confirmed",
                    "today",
                ),
                new_project=True,
            )
        items = read_attention(ws, gate)["items"]
        assert [(i["id"], i["reason"], i["href"]) for i in items] == [
            ("local-goal", "plan_goal", "/projects/local")
        ]
        for candidate in ("obsolete", "plan"):
            store.start_plan(
                candidate, "local-goal", request, "snapshot", "template", "today"
            )
            store.finish_plan(
                candidate, "today", document=document, error=None, evidence={}
            )
        pending = read_attention(ws, gate)["items"]
        assert [(i["kind"], i["reason"], i["label"], i["href"]) for i in pending] == [
            (
                "project-plan",
                "choose_stage",
                "Stage classify",
                "/plans/plan?stage=classify",
            ),
            (
                "project-plan",
                "choose_stage",
                "Stage ingest",
                "/plans/plan?stage=ingest",
            ),
        ]
        for stage in ("ingest", "classify"):
            store._connection.execute(
                """INSERT INTO planning_runs (run_id, input_hash, input_payload, created_at, phase)
                   VALUES (?, 'hash', ?, 'today', 'failed')""",
                (
                    stage + "-planning",
                    json.dumps(
                        {
                            "requirements": stage,
                            "frozen_rules": {"repository": str(local / ".git")},
                        }
                    ),
                ),
            )
            store._connection.execute(
                """INSERT INTO project_stage_runs
                   (stage_run_id,plan_id,stage_id,planning_run_id,mode,execution_baseline,dependencies,created_at)
                   VALUES (?, 'plan', ?, ?, 'direct', ?, '[]', 'today')""",
                (stage, stage, stage + "-planning", baseline),
            )
            if stage == "ingest":
                assert [
                    (i["reason"], i["href"]) for i in read_attention(ws, gate)["items"]
                ] == [
                    ("choose_stage", "/plans/plan?stage=classify"),
                    ("planning_attention", "/stages/ingest"),
                ]
        gate.begin_job("active")
        partial = read_attention(ws, gate)
        assert not partial["complete"]
        assert [(i["reason"], i["label"], i["href"]) for i in partial["items"]] == [
            ("planning_attention", "Stage classify", "/stages/classify"),
            ("planning_attention", "Stage ingest", "/stages/ingest"),
        ]
        store._connection.execute("UPDATE planning_runs SET phase = 'drafting'")
        gate.finish_job()
        assert read_attention(ws, gate) == {"items": [], "complete": True}
        store.block_stage_dispatch("ingest", "Frozen rules prohibit this dispatch.")
        [blocked] = read_attention(ws, gate)["items"]
        assert (blocked["reason"], blocked["href"]) == (
            "dispatch_failed",
            "/plans/plan",
        )
        store.revise_plan("amended", "plan", document, "tomorrow")
        assert {i["reason"] for i in read_attention(ws, gate)["items"]} == {
            "choose_stage"
        }
        # Keep a bound task visible through multiple revisions until a new
        # version actually claims its stage. Unrelated claims cannot dismiss it.
        store._connection.execute(
            """INSERT INTO task_runs(run_id,task_id,task_hash,rules_hash,baseline_commit,input_payload,phase)
               VALUES ('old-task','T-001','hash','rules',?,'{}','planning_required')""",
            (baseline,),
        )
        store._connection.execute(
            "UPDATE planning_runs SET bound_task_run_id = 'old-task' WHERE run_id = 'classify-planning'"
        )
        store.revise_plan("latest", "amended", document, "later")

        def old_task_pending():
            return any(
                i["id"] == "old-task" and i["reason"] == "implementation_attention"
                for i in read_attention(ws, gate)["items"]
            )

        assert old_task_pending()
        for stage in ("ingest", "classify"):
            store._connection.execute(
                """INSERT INTO planning_runs (run_id,input_hash,input_payload,created_at,phase)
                   SELECT ?,input_hash,input_payload,created_at,phase FROM planning_runs WHERE run_id=?""",
                ("new-" + stage + "-planning", stage + "-planning"),
            )
            store._connection.execute(
                """INSERT INTO project_stage_runs(stage_run_id,plan_id,stage_id,planning_run_id,mode,execution_baseline,dependencies,created_at)
                   VALUES (?, 'latest', ?, ?, 'direct', ?, '[]', 'later')""",
                ("new-" + stage, stage, "new-" + stage + "-planning", baseline),
            )
            assert old_task_pending() == (stage == "ingest")


def test_direct_stage_handoffs_and_interrupted_integration_review(
    workflow, monkeypatch
):
    codex, claude, path, content, _ = _fixture(workflow)
    programs = {"codex": str(codex), "claude": str(claude)}
    document = {
        "title": "Amount",
        "architecture": "One module",
        "shared_interfaces": [],
        "stages": [{**_model_card("amount", []), "approach": f"Implement {path}"}],
    }
    with _serve(workflow.repository, workflow.base, programs) as service:

        def todos():
            return [
                (i["reason"], i["href"]) for i in _get(service, "attention")["items"]
            ]

        goal = _job(
            service,
            "project.confirm",
            baseline=workflow.baseline,
            original_goal="Build amount",
            confirmed_goal="Build amount",
        )["result"]
        workflow_helpers.configure(claude, role="review", report=document)
        plan = _job(service, "project.plan", goal_id=goal["goal_id"], planner=FABLE)[
            "result"
        ]
        workflow_helpers.configure(
            codex, role="stage-direct", write_scope=[path], writes={path: content.hex()}
        )
        workflow_helpers.configure(claude, role="stage-direct")
        stage = _job(
            service,
            "project.stage.start",
            plan_id=plan["plan_id"],
            stage_id="amount",
            card_review=OPUS,
        )["result"]
        task = stage["task_run_id"]
        assert todos() == [("test_task", f"/tasks/{task}")]
        service.jobs.read_gate.begin_job("active")
        try:
            assert _get(service, "attention") == {"items": [], "complete": False}
        finally:
            service.jobs.read_gate.finish_job()
        workflow_helpers.configure(codex, role="test-service")
        test = _job(
            service,
            "task.test",
            task_run_id=task,
            implementation_attempt=1,
            commands=["build", "test"],
            declaration_source="PM",
        )["result"]
        assert todos() == [("review_task", f"/tasks/{task}")]
        workflow_helpers.configure(
            claude, role="review", report={"summary": "Amount is ready", "findings": []}
        )
        _job(
            service,
            "task.review",
            task_run_id=task,
            implementation_attempt=1,
            test_attempt=test["test_attempt"],
            seats=[OPUS],
            auto_rework=False,
        )
        assert todos() == [("integrate_stage", f"/stages/{stage['stage_run_id']}")]
        target = workflow_helpers.git(workflow.repository, "symbolic-ref", "HEAD")
        integrated = _job(
            service, "integration.create", task_run_ids=[task], target=target
        )["result"]
        assert todos() == [
            ("test_integration", f"/integrations/{integrated['integration_id']}")
        ]
        integration = integrated["integration_id"]
        _job(service, "integration.test", integration_id=integration)
        report = {"summary": "Integration is ready", "findings": []}
        workflow_helpers.configure(codex, role="review", report=report)
        workflow_helpers.configure(claude, role="review", mode="wait", report=report)
        status, job = _request(
            service,
            "POST",
            "/api/jobs",
            {
                "action": "integration.review",
                "args": {
                    "integration_id": integration,
                    "seats": [SOL, OPUS],
                    "auto_rework": False,
                },
            },
        )
        assert status == 202
        job_id = job["job_id"]
        activity_path = f"jobs/{job_id}/activity"
        running = _until(
            service,
            activity_path,
            lambda r: (
                r["reviews"]
                and [s["state"] for s in r["reviews"][0]["seats"]]
                == ["succeeded", "running"]
            ),
        )
        assert running["controller_holds_journal"] and not running["recovery"]
        assert _get(service, "attention") == {"items": [], "complete": False}
        round_id = running["reviews"][0]["round_id"]
        # Reopen the web registry as it was before its controller was stopped.
        # SQLite and the activity journal retain the actual unfinished seat.
        state_path = service.jobs.state_path
        pre_crash_registry = state_path.read_bytes()
        monkeypatch.syspath_prepend(str(Path(__file__).parent / "web_support"))
        from offline_env import _hard_stop

        _hard_stop(service.jobs.controllers[job_id])
    state_path.write_bytes(pre_crash_registry)
    with _serve(workflow.repository, workflow.base, programs) as service:
        journal = service.jobs.job_root / job_id / "activity.jsonl"
        with journal.open("rb") as controller:
            fcntl.flock(controller, fcntl.LOCK_EX)
            assert _get(service, "attention") == {"items": [], "complete": True}
        [todo] = _get(service, "attention")["items"]
        assert (todo["reason"], todo["id"], todo["href"]) == (
            "review_incomplete",
            round_id,
            f"/jobs/{job_id}",
        )
        [recovery] = _get(service, activity_path)["recovery"]
        assert recovery["args"]["recover"] and recovery["args"][
            "expected_attempts"
        ] == {"seat-2": 1}
        workflow_helpers.configure(claude, mode="success")
        _job(service, recovery["action"], **recovery["args"])
        assert [i["reason"] for i in _get(service, "attention")["items"]] == [
            "accept_integration"
        ]


def test_actual_integration_acceptance_clears_the_queue(workflow):
    ws = Workspace.open(workflow.repository, workflow.base)
    workflow.database = ws.database
    integration = _integration(workflow)
    gate = ReadGate()

    def reasons():
        return [
            item["reason"]
            for item in read_attention(ws, gate)["items"]
            if item["id"] == integration["integration_id"]
        ]

    assert reasons() == ["test_integration"]
    test_bin = workflow.base / "bin" / "codex-integration-test"
    test_bin.write_text(
        f"#!{sys.executable}\n" + (workflow_helpers.FAKES / "test_cli.py").read_text()
    )
    test_bin.chmod(0o755)
    workflow_helpers.successful(
        *_test_arguments(workflow, integration, test_bin, "integration-tests")
    )
    assert reasons() == ["review_integration"]
    reviewed = workflow_helpers.successful(
        *_review_arguments(
            workflow,
            integration,
            {
                "codex": workflow_helpers.executable(workflow, "codex", "review"),
                "claude": workflow.executables["claude"],
            },
            "review",
        )
    )["review"]
    assert reasons() == ["accept_integration"]
    gate.begin_job("active")
    assert read_attention(ws, gate) == {"items": [], "complete": False}
    gate.finish_job()
    workflow_helpers.successful(
        *_arguments(workflow, "accept", integration, "--review", reviewed["round_id"])
    )
    assert not reasons()


def test_repair_clears_current_task_review_decision(workflow):
    ws = Workspace.open(workflow.repository, workflow.base)
    workflow.database = ws.database
    task_reviews.prepare(workflow)
    first = workflow_helpers.successful(
        *task_reviews.review_arguments(workflow, dual=True), "--no-auto-rework", code=1
    )
    gate = ReadGate()
    reviews = [i for i in read_attention(ws, gate)["items"] if i["kind"] == "review"]
    assert [(i["id"], i["reason"], i["href"]) for i in reviews] == [
        (
            first["round_id"],
            "review_decision",
            "/tasks/" + workflow.first["task_run_id"],
        )
    ]
    workflow_helpers.successful(
        *task_reviews.continue_arguments(
            workflow, "confirm", first["round_id"], "--auto-rework"
        )
    )
    assert read_attention(ws, gate) == {"items": [], "complete": True}


def test_http_independent_review_moves_from_confirmation_to_retry_then_exits(workflow):
    codex, claude, *_ = _fixture(workflow)
    workflow_helpers.configure(
        codex, role="review", report=workflow_helpers.blocking_report()
    )
    workflow_helpers.configure(
        claude, role="review", report={"summary": "Checked", "findings": []}
    )
    with _serve(
        workflow.repository, workflow.base, {"codex": str(codex), "claude": str(claude)}
    ) as service:
        frozen = _job(service, "review.freeze", **INPUT)["result"]
        reviewed = _job(
            service, "review.run", snapshot_id=frozen["snapshot_id"], seats=[SOL, FABLE]
        )["result"]
        parent = reviewed["round_id"]
        [item] = _get(service, "attention")["items"]
        assert (item["reason"], item["href"]) == (
            "review_decision",
            f"/reviews/{parent}",
        )
        assert _get(service, f"resources/review/{parent}?session_logs=1")["data"][
            "can_confirm"
        ]
        workflow_helpers.configure(claude, mode="model-error")
        child = _job(service, "review.confirm", round_id=parent)["result"]["round_id"]
        [item] = _get(service, "attention")["items"]
        assert (item["reason"], item["href"]) == (
            "review_incomplete",
            f"/reviews/{child}",
        )
        workflow_helpers.configure(
            claude,
            mode="success",
            report={
                "summary": "Confirmed",
                "confirmations": [
                    {
                        "target_seat": "reviewer-1",
                        "finding_id": "status-blocker",
                        "decision": "agree",
                        "reason": "The reported issue is present.",
                    }
                ],
            },
        )
        _job(service, "review.retry", round_id=child)
        assert _get(service, "attention") == {"items": [], "complete": True}


def test_http_failed_consultation_exits_after_direct_confirmation(workflow):
    claude = workflow.executables["claude"]
    workflow_helpers.configure(claude, role="review", mode="model-error")
    programs = {provider: str(path) for provider, path in workflow.executables.items()}
    with _serve(workflow.repository, workflow.base, programs) as service:
        args = {"baseline": workflow.baseline, "original_goal": "Build a report"}
        result = _job(
            service, "project.consult", **args, advisors=[FABLE], exit_code=1
        )["result"]
        [failed] = _get(service, "attention")["items"]
        assert (failed["reason"], failed["href"]) == (
            "consultation_failed",
            "/new?consultation=" + result["consultation_id"],
        )
        goal = _job(
            service, "project.confirm", **args, confirmed_goal=args["original_goal"]
        )["result"]
        [pending] = _get(service, "attention")["items"]
        assert (pending["id"], pending["href"]) == (
            goal["goal_id"],
            "/projects/" + goal["project_id"],
        )


def test_http_answered_planning_question_leaves_the_queue(workflow):
    codex, claude, *_ = _fixture(workflow)
    programs = {"codex": str(codex), "claude": str(claude)}
    workflow_helpers.configure(codex, role="planning")
    workflow_helpers.configure(claude, role="planning", question=True)
    args = {
        "requirements": workflow_helpers.REQUIREMENTS,
        "baseline": workflow.baseline,
        "plan_review": {"model": SOL["model"], "effort": SOL["effort"]},
    }
    with _serve(workflow.repository, workflow.base, programs) as service:
        plan = _job(service, "plan.create", **args, exit_code=1)["result"]
        identifier = plan["planning_run_id"]
        [pending] = _get(service, "attention")["items"]
        assert (pending["reason"], pending["href"]) == (
            "planning_question",
            f"/task-plans/new?from={identifier}&question=0",
        )
        workflow_helpers.configure(claude, question=False)
        declarations = {
            "dependencies": [],
            "decisions": [
                {
                    "question": "Which status?",
                    "resolution": "ready",
                    "source": "user_declared",
                    "detail": "The ready state was chosen.",
                }
            ],
        }
        _job(
            service,
            "plan.create",
            **(args | {"requirements": "A different task."}),
            declarations=declarations,
        )
        assert _get(service, "attention")["items"] == [pending]
        _job(service, "plan.create", **args, declarations=declarations)
        assert _get(service, "attention") == {"items": [], "complete": True}


def test_busy_integration_conflict_uses_only_recorded_state(workflow, monkeypatch):
    ws = Workspace.open(workflow.repository, workflow.base)
    workflow.database = ws.database
    path = "frontend/status.js"
    first, _ = implement(workflow, "T-0210", path, {path: b"first\n"})
    second, _ = implement(workflow, "T-0211", path, {path: b"second\n"})
    conflict = workflow_helpers.successful(
        *create_args(workflow, first, second), code=1
    )

    def unexpected(*args, **kwargs):
        raise AssertionError("Busy inbox inspected live evidence.")

    monkeypatch.setattr("multi_agent.web.attention.integration_view", unexpected)
    monkeypatch.setattr("multi_agent.web.attention.verification_view", unexpected)
    programs = {provider: str(path) for provider, path in workflow.executables.items()}
    workflow_helpers.configure(
        workflow.executables["claude"], role="review", mode="wait"
    )
    with _serve(ws.repository, ws.data_dir, programs) as service:
        status, _ = _request(
            service,
            "POST",
            "/api/jobs",
            {
                "action": "project.consult",
                "args": {
                    "baseline": workflow.baseline,
                    "original_goal": "Another goal",
                    "advisors": [FABLE],
                },
            },
        )
        assert status == 202
        partial = _get(service, "attention")
        assert not partial["complete"]
        [item] = partial["items"]
        assert (item["reason"], item["href"]) == (
            "integration_conflict",
            "/integrations/" + conflict["integration_id"],
        )
