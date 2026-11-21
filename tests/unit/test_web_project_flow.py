"""Run the project lifecycle through real loopback HTTP, Git and SQLite."""

import json
import sqlite3
import time
from http.client import HTTPConnection

import pytest
import test_task_workflow_cli as helpers
from test_project_stage import _model_card
from test_web_workspace import _serve

workflow = helpers.workflow

SOL = {"provider": "codex", "model": "gpt-6.2-sol", "effort": "high"}
FABLE = {"provider": "claude", "model": "claude-fable-5-2", "effort": "high"}
OPUS = {"provider": "claude", "model": "claude-opus-5-5", "effort": "high"}
SONNET = {"provider": "claude-code", "model": "claude-sonnet-5-5", "effort": "high"}


def _request(service, method, path, body=None):
    connection = HTTPConnection("127.0.0.1", service.server_port, timeout=30)
    try:
        raw = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if raw is not None else {}
        connection.request(method, path, raw, headers)
        response = connection.getresponse()
        return response.status, json.load(response)
    finally:
        connection.close()


def _job(service, action, *, exit_code=0, **args):
    status, submitted = _request(
        service, "POST", "/api/jobs", {"action": action, "args": args}
    )
    assert status == 202, submitted
    identifier = submitted["job_id"]
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        status, job = _request(service, "GET", f"/api/jobs/{identifier}")
        assert status == 200, job
        if job["state"] in ("finished", "interrupted"):
            assert job["state"] == "finished" and job["exit_code"] == exit_code, job
            return job
        time.sleep(0.05)
    pytest.fail(f"job did not finish: {action}")


def _get(service, suffix):
    status, value = _request(service, "GET", f"/api/{suffix}")
    assert status == 200, value
    return value


def _fixture(workflow):
    repo = workflow.repository
    amount = "backend/amount.py"
    content = b"AMOUNT = 1\n"
    status = "frontend/status.js"
    banner = "frontend/banner.js"
    banner_content = b'export const banner = "welcome";\n'
    (repo / "checks/test.py").write_text(
        "from pathlib import Path\n"
        f"assert Path({amount!r}).read_bytes() == {content!r}\n"
        f"if Path('artifact.bin').read_bytes() == {helpers.FIXED_BYTES!r}:\n"
        f"    assert Path({banner!r}).read_bytes() == {banner_content!r}\n"
    )
    workflow.rules["implementation"].append(
        {
            "paths": [amount],
            "provider": "codex",
            "default_model": SOL["model"],
            "default_effort": SOL["effort"],
            "allowed_models": [SOL["model"]],
        }
    )
    (repo / ".multi-agent/rules.yaml").write_text(json.dumps(workflow.rules))
    helpers.git(repo, "commit", "--quiet", "-am", "verify HTTP project deliveries")
    workflow.baseline = helpers.git(repo, "rev-parse", "HEAD")
    workflow.database = workflow.base / "reviews.sqlite3"
    codex = helpers.executable(workflow, "codex", "web-service")
    claude = helpers.executable(workflow, "claude", "web-service")
    transport = workflow.base / "bin" / "codex-transport.py"
    transport.write_text(
        transport.read_text().replace(
            'name = "model-2" if "cursor" in params else "model-1"',
            'name = "gpt-6.2-astra" if "cursor" in params else "gpt-6.2-sol"',
        )
    )
    code = codex.read_text().split("\n", 1)[1]
    codex.write_text(
        codex.read_text().split("\n", 1)[0] + "\n"
        "import json, sys\nfrom pathlib import Path\n"
        "role = json.loads(Path(sys.argv[0]).with_suffix('.json').read_text())['role']\n"
        f"body = {(helpers.FAKES / 'test_cli.py').read_text()!r} if role == 'test-service' else {code!r}\n"
        "exec(compile(body, sys.argv[0], 'exec'))\n"
    )
    return (
        codex,
        claude,
        amount,
        content,
        {status: helpers.FIXED_BYTES, banner: banner_content},
    )


def test_http_and_cli_share_stage_delivery_and_restart(workflow):
    codex, claude, amount, amount_content, leaves = _fixture(workflow)
    repo, data, baseline = workflow.repository, workflow.base, workflow.baseline
    programs = {"codex": str(codex), "claude": str(claude)}
    original = "Build the amount and summary, then outline monthly reporting.\n"
    advice = {
        "draft_goal": original,
        "background": [
            {"claim": "Repository contains a workflow", "path": "AGENTS.md"}
        ],
        "assumptions": [],
        "questions": [],
        "remaining_disagreements": [],
    }
    document = {
        "title": "HTTP provenance sample",
        "architecture": "One code path",
        "shared_interfaces": [],
        "stages": [
            {**_model_card("amount", []), "approach": f"Implement {amount}"},
            _model_card("summary", ["amount"]),
            _model_card("report", ["summary"]),
        ],
    }
    with _serve(repo, data, programs) as service:
        helpers.configure(claude, role="review", report=advice)
        consulted = _job(
            service,
            "project.consult",
            baseline=baseline,
            original_goal=original,
            advisors=[FABLE],
        )["result"]
        goal = _job(
            service,
            "project.confirm",
            baseline=baseline,
            original_goal=original,
            confirmed_goal=original + "Use the checked output format.\n",
            consultation_id=consulted["consultation_id"],
            adoption="edited",
        )["result"]
        helpers.configure(claude, role="review", report=document)
        plan = _job(service, "project.plan", goal_id=goal["goal_id"], planner=FABLE)[
            "result"
        ]
        plan_id = plan["plan_id"]
        assert (
            helpers.successful("--data-dir", data, "project", "show", plan_id) == plan
        )
        assert _get(service, f"resources/project-plan/{plan_id}")["data"] == plan

        direct_capture = data / "direct-http-wire.jsonl"
        helpers.configure(
            codex,
            role="stage-direct",
            capture=str(direct_capture),
            write_scope=[amount],
            writes={amount: amount_content.hex()},
        )
        helpers.configure(claude, role="stage-direct", capture=str(direct_capture))
        direct = _job(
            service,
            "project.stage.start",
            plan_id=plan_id,
            stage_id="amount",
            card_review=OPUS,
        )["result"]
        assert direct["mode"] == "direct" and direct["candidate_verified"]
        assert not _get(
            service, f"resources/plan/{direct['planning_run_id']}?implementation=1"
        )["data"]["dispatch_configurable"]
        assert [
            (item["provider"], item["kind"])
            for item in helpers.records(direct_capture)
            if item["kind"] == "implementation"
        ] == [("codex", "implementation")]
        task_id = direct["task_run_id"]
        helpers.configure(codex, role="test-service")
        tested = _job(
            service,
            "task.test",
            task_run_id=task_id,
            implementation_attempt=1,
            commands=["build", "test"],
            declaration_source="PM",
        )["result"]
        helpers.configure(
            claude,
            role="review",
            report={"summary": "The amount is ready", "findings": []},
        )
        direct_review = _job(
            service,
            "task.review",
            task_run_id=task_id,
            implementation_attempt=1,
            test_attempt=tested["test_attempt"],
            seats=[OPUS],
            auto_rework=False,
        )["result"]
        assert direct["candidate"] == tested["candidate"] == direct_review["candidate"]
        assert _get(service, f"resources/stage/{direct['stage_run_id']}")["data"][
            "review_satisfied"
        ]

        split_capture = data / "split-http-wire.jsonl"
        helpers.configure(
            codex,
            role="stage-delegate",
            capture=str(split_capture),
            split_cards=[
                {
                    "id": f"T-08{number:02d}",
                    "goal": f"Deliver {path}",
                    "write_scope": [path],
                }
                for number, path in enumerate(leaves)
            ],
        )
        delegated = _job(
            service,
            "project.stage.start",
            plan_id=plan_id,
            stage_id="summary",
            mode="delegate",
            card_review=OPUS,
        )["result"]
        stage_id = delegated["stage_run_id"]
        branch_ref = f"refs/heads/multi-agent/stages/{stage_id}"
        assert delegated["execution_baseline"] == direct["candidate"]
        assert not delegated["can_integrate"] and not delegated["review_satisfied"]
        status, blocked = _request(
            service,
            "POST",
            "/api/jobs",
            {"action": "project.stage.integrate", "args": {"stage_run_id": stage_id}},
        )
        assert status == 409 and blocked["error"]["code"] == "job_unavailable"
        missing = helpers.module_cli(
            "--data-dir", data, "project", "stage", "integrate", stage_id
        )
        assert missing.returncode == 2
        assert not helpers.git(repo, "for-each-ref", "--format=%(refname)", branch_ref)
        for index, item in enumerate(delegated["leaves"]):
            helpers.configure(claude, role="stage-delegate", capture=str(split_capture))
            reviewer = {"card_review": FABLE} if index == 0 else {}
            leaf = _job(
                service,
                "project.stage.leaf",
                stage_run_id=stage_id,
                card_id=item["card_id"],
                implementation=SONNET,
                review_only=True,
                **reviewer,
            )["result"]
            card = leaf["planning"]["card"]
            leaf_plan = _get(
                service, f"resources/plan/{leaf['planning_run_id']}?implementation=1"
            )["data"]
            assert not leaf_plan["dispatch_configurable"]
            assert not leaf_plan["retry_configurable"]
            assert leaf_plan["details"]["input"]["review_request"] == (
                FABLE if index == 0 else OPUS
            )
            review = leaf_plan["attempts"]["review"][0]["request"]["request"]
            assert review == leaf_plan["details"]["input"]["review_request"]
            (path,) = card["write_scope"]
            helpers.configure(
                claude,
                role="implementation",
                task_hash=leaf["planning"]["draft_projection"]["card_hash"],
                writes={path: leaves[path].hex()},
            )
            _job(service, "task.run", planning_run_id=leaf["planning_run_id"])
        prepared = _get(service, f"resources/stage/{stage_id}")["data"]
        assert prepared["can_integrate"] and not prepared["review_satisfied"]
        assert len({row["candidate"] for row in prepared["leaves"]}) == 2
        target = _job(service, "project.stage.integrate", stage_run_id=stage_id)[
            "result"
        ]
        assert (
            target["target_ref"] == branch_ref
            and target["target_baseline"] == direct["candidate"]
        )
        repeated = _job(service, "project.stage.integrate", stage_run_id=stage_id)[
            "result"
        ]
        assert repeated["target_ref"] == branch_ref
        integration = _job(
            service, "project.stage.integrate", stage_run_id=stage_id, target=branch_ref
        )["result"]
        identifier = integration["integration_id"]
        assert (
            integration["target_ref"] == branch_ref
            and integration["target_baseline"] == direct["candidate"]
        )
        assert helpers.git(repo, "rev-parse", branch_ref) == direct["candidate"]
        wrong = helpers.module_cli(
            "--data-dir",
            data,
            "project",
            "stage",
            "integrate",
            stage_id,
            "--target",
            helpers.git(repo, "symbolic-ref", "HEAD"),
        )
        assert wrong.returncode == 2
        refused = _job(
            service,
            "integration.accept",
            integration_id=identifier,
            round_id=direct_review["round_id"],
            exit_code=2,
        )
        assert refused["result"] is None
        assert refused["error"] == {
            "code": "integration_accept_refused",
            "message": "integration requires its current review round",
        }
        assert (
            "multi-agent: integration requires its current review round"
            in refused["stderr_tail"]
        )
        helpers.configure(codex, role="test-service")
        suite = _job(service, "integration.test", integration_id=identifier)["result"][
            "test"
        ]
        evidence = _get(service, f"resources/integration/{identifier}")["artifacts"]
        artifact = next(item for item in evidence if item["stream"] == "stdout")
        preview = _get(
            service, f"artifacts/{artifact['artifact_id']}?offset=0&limit=32"
        )
        assert preview["sha256"] == artifact["sha256"]
        helpers.configure(
            claude,
            role="review",
            report={"summary": "Both leaves are ready", "findings": []},
        )
        reviewed = _job(
            service,
            "integration.review",
            integration_id=identifier,
            seats=[OPUS],
            auto_rework=False,
        )["result"]["review"]
        integration_commit = integration["candidate"]["commit"]
        assert suite["candidate"]["commit"] == integration_commit
        assert (
            _get(service, f"diffs/snapshot/{reviewed['snapshot_id']}")["candidate"]
            == integration_commit
        )
        decision = _job(
            service,
            "integration.accept",
            integration_id=identifier,
            round_id=reviewed["round_id"],
        )
        assert (
            decision["result"]["acceptance"]["candidate"]["commit"]
            == integration_commit
        )
        shown = _get(service, f"resources/stage/{stage_id}")
        assert (
            shown["data"]["review_satisfied"]
            and shown["data"]["candidate"] == integration_commit
        )
        assert helpers.git(repo, "rev-parse", "HEAD") == baseline
        assert helpers.git(repo, "rev-parse", branch_ref) == direct["candidate"]
        assert [
            item["stage_id"]
            for item in _get(service, f"resources/project-stages/{plan_id}")["data"][
                "stages"
            ]
        ] == ["amount", "summary"]
        document["stages"][2]["acceptance_criteria"].append(
            "Identify the opening balance"
        )
        revised = _job(service, "project.revise", plan_id=plan_id, document=document)[
            "result"
        ]
        assert revised["source_plan_id"] == plan_id
        assert _get(service, f"resources/project-plan/{plan_id}")["data"] == plan

    with _serve(repo, data, programs) as restarted:
        assert _get(restarted, f"jobs/{decision['job_id']}") == decision
        assert _get(restarted, f"resources/stage/{stage_id}") == shown
        runs = _get(restarted, "runs?kind=stage")["items"]
        assert {item["id"] for item in runs} == {stage_id, direct["stage_run_id"]}
    assert helpers.git(repo, "status", "--porcelain") == ""


def test_http_plan_retry_rejects_known_id_in_foreign_workspace(workflow):
    programs = {provider: str(path) for provider, path in workflow.executables.items()}
    local = workflow.base / "local-data"
    requirements = "Keep this frozen:\n" + helpers.REQUIREMENTS
    helpers.configure(workflow.executables["claude"], mode="model-error")
    with _serve(workflow.repository, local, programs) as service:
        job = _job(
            service,
            "plan.create",
            baseline=workflow.baseline,
            requirements=requirements,
            exit_code=1,
            draft={"model": FABLE["model"], "effort": FABLE["effort"]},
            declarations={"dependencies": [], "decisions": []},
        )
        assert job["args"]["requirements"] == {"length": len(requirements)}
        assert job["args"]["declarations"] == {"dependencies": 0, "decisions": 0}
        identifier = job["result"]["planning_run_id"]
        url = f"/api/resources/plan/{identifier}?implementation=1"
        status, own = _request(service, "GET", url)
        assert status == 200
        assert own["data"]["retry_configurable"]
        assert own["data"]["details"]["input"]["requirements"] == requirements
        assert own["data"]["phase"] == "failed"

    foreign = workflow.base / "foreign"
    helpers.git(
        workflow.base, "clone", "--quiet", str(workflow.repository), str(foreign)
    )
    outside = workflow.base / "foreign-data"
    outside.mkdir(mode=0o700)
    source = sqlite3.connect(local / "reviews.sqlite3", autocommit=True)
    destination = sqlite3.connect(outside / "reviews.sqlite3", autocommit=True)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()

    with _serve(foreign, outside, programs) as service:
        assert _request(service, "GET", url)[0] == 404
        status, response = _request(
            service,
            "POST",
            "/api/jobs",
            {"action": "plan.retry", "args": {"planning_run_id": identifier}},
        )
        assert status == 404
        assert response["error"]["code"] == "not_found"
        assert _get(service, "jobs")["items"] == []
