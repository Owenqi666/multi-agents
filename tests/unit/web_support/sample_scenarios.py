"""Workspace API actions used to record and check the four offline scenarios."""

from pathlib import PurePosixPath
from urllib.parse import urlencode

ORIGINAL = "Build the amount and summary, then outline monthly reporting.\n"
CONFIRMED = ORIGINAL + "Use the checked output format.\n"
SOL = {"model": "gpt-6.2-sol", "effort": "low"}
ASTRA = {"provider": "codex", "model": "gpt-6.2-astra", "effort": "low"}
FABLE = {"provider": "claude", "model": "claude-fable-5-2", "effort": "low"}
OPUS = {"provider": "claude", "model": "claude-opus-5-5", "effort": "low"}
SONNET = {"provider": "claude-code", "model": "claude-sonnet-5-5", "effort": "low"}
COMMANDS = ["build", "test"]


def _begin(api):
    workspace = api.get("workspace")
    api.normalizer.bind(repository=workspace["repository"])
    baseline = workspace["head"]
    for provider in ("codex", "claude"):
        observation = api.job("environment.refresh", provider=provider, exit_code=None)
        assert observation["error"] is None, observation
    refreshed = api.get("workspace")
    catalog = refreshed["environments"]["codex"]["models"]
    assert {"gpt-6.2-luna", "claude-haiku-5-5"} <= set(catalog)
    assert len(catalog) == 8
    consultation = api.job(
        "project.consult", baseline=baseline, original_goal=ORIGINAL, advisors=[FABLE]
    )
    consultation_id = consultation["consultation_id"]
    consulted = api.get(f"resources/consultation/{consultation_id}")
    seat_log = consulted["artifacts"][0]
    assert seat_log["key"] == "seat-1-1", seat_log
    assert seat_log["coordinates"] == {"seat": 1, "log_index": 1}, seat_log
    goal = api.job(
        "project.confirm",
        baseline=baseline,
        original_goal=ORIGINAL,
        confirmed_goal=CONFIRMED,
        consultation_id=consultation_id,
        adoption="edited",
    )
    goal_id = goal["goal_id"]
    api.get(f"resources/goal/{goal_id}")
    api.get(f"resources/project/{goal['project_id']}")
    plan = api.job("project.plan", goal_id=goal_id, planner=FABLE)
    plan_id = plan["plan_id"]
    assert plan["status"] == "succeeded", plan
    planner_log = api.get(f"resources/project-plan/{plan_id}")["artifacts"][0]
    assert planner_log["key"] == "planner-1", planner_log
    assert planner_log["coordinates"] == {"log_index": 1}, planner_log
    return plan_id


def _direct(api, plan_id):
    direct = api.job(
        "project.stage.start",
        plan_id=plan_id,
        stage_id="amount",
        mode="direct",
        sol=SOL,
        card_review=OPUS,
    )
    assert direct["mode"] == "direct" and direct["candidate_verified"], direct
    task_id = direct["task_run_id"]
    api.get(f"resources/task/{task_id}")
    api.get(f"diffs/task/{task_id}")
    suite = api.job(
        "task.test",
        task_run_id=task_id,
        implementation_attempt=1,
        commands=COMMANDS,
        declaration_source="PM",
    )
    tests = api.get(
        f"resources/task-test/{task_id}"
        f"?implementation_attempt=1&test_attempt={suite['test_attempt']}"
    )
    artifact = next(
        item
        for item in tests["artifacts"]
        if item["stream"] == "stdout" and item["coordinates"]["sequence"] == 1
    )
    assert artifact["key"] == f"test-{suite['test_attempt']}-1-2", artifact
    assert artifact["coordinates"] == {
        "test_attempt": suite["test_attempt"],
        "sequence": 1,
        "log_index": 2,
    }, artifact
    page = api.get(f"artifacts/{artifact['artifact_id']}?offset=0&limit=4096")
    assert page["sha256"] == artifact["sha256"], page
    assert page["text"] == "Offline build wrote artifact.bin\n", page
    review = api.job(
        "task.review",
        task_run_id=task_id,
        implementation_attempt=1,
        test_attempt=suite["test_attempt"],
        seats=[OPUS],
        auto_rework=False,
    )
    stage_id = direct["stage_run_id"]
    stage = api.get(f"resources/stage/{stage_id}")["data"]
    assert stage["review_satisfied"] and stage["candidate"] == direct["candidate"], (
        stage
    )
    return direct, review, artifact


def _delegate(api, plan_id):
    stage = api.job(
        "project.stage.start",
        plan_id=plan_id,
        stage_id="summary",
        mode="delegate",
        sol=SOL,
        card_review=OPUS,
    )
    assert stage["mode"] == "delegate" and stage["split_verified"], stage
    assert len(stage["leaves"]) == 2 and not stage["can_integrate"], stage
    return stage


def _prepare_leaf(api, stage_id, card_id):
    leaf = api.job(
        "project.stage.leaf",
        stage_run_id=stage_id,
        card_id=card_id,
        implementation=SONNET,
        review_only=True,
    )
    return leaf["planning_run_id"]


def _integrate(api, stage):
    stage_id = stage["stage_run_id"]
    blocked = api.request(
        "POST",
        "/api/jobs",
        {"action": "project.stage.integrate", "args": {"stage_run_id": stage_id}},
        status=409,
    )
    assert blocked["error"]["code"] == "job_unavailable", blocked
    for item in stage["leaves"]:
        leaf_id = _prepare_leaf(api, stage_id, item["card_id"])
        api.job("task.run", planning_run_id=leaf_id)
    ready = api.get(f"resources/stage/{stage_id}")["data"]
    assert ready["can_integrate"] and not ready["review_satisfied"], ready
    assert len({leaf["candidate"] for leaf in ready["leaves"]}) == 2, ready
    integration = api.job("project.stage.integrate", stage_run_id=stage_id)
    assert (
        integration["status"] == "ready" and integration["verification"] == "verified"
    ), integration
    return integration


def _integration_review(api, integration, seats, exit_code=0):
    identifier = integration["integration_id"]
    suite = api.job("integration.test", integration_id=identifier)["test"]
    assert suite["status"] == "succeeded", suite
    return api.job(
        "integration.review",
        integration_id=identifier,
        seats=seats,
        auto_rework=False,
        exit_code=exit_code,
    )["review"]


def _accept(api, integration, review):
    identifier = integration["integration_id"]
    assert review["assessment"]["status"] == "ready", review
    acceptance = api.job(
        "integration.accept", integration_id=identifier, round_id=review["round_id"]
    )["acceptance"]
    assert acceptance["candidate"]["commit"] == integration["candidate"]["commit"]
    assert acceptance["accepted_at"], acceptance
    api.get(f"resources/integration/{identifier}")


def accept(api):
    plan_id = _begin(api)
    _, direct_review, _ = _direct(api, plan_id)
    stage = _delegate(api, plan_id)
    integration = _integrate(api, stage)
    identifier = integration["integration_id"]
    refused = api.finish(
        api.start(
            "integration.accept",
            integration_id=identifier,
            round_id=direct_review["round_id"],
        ),
        exit_code=2,
    )
    assert refused["result"] is None and refused["error"] is not None, refused
    review = _integration_review(api, integration, [OPUS])
    snapshot_id = review["snapshot_id"]
    snapshot = api.get(f"resources/snapshot/{snapshot_id}")
    frozen = snapshot["artifacts"][0]
    assert frozen["key"] == "frozen-1-stdout", frozen
    assert frozen["coordinates"] == {"frozen_index": 1}, frozen
    assert (
        api.get(f"diffs/snapshot/{snapshot_id}")["candidate"]
        == integration["candidate"]["commit"]
    )
    _accept(api, integration, review)
    stage = api.get(f"resources/stage/{stage['stage_run_id']}")["data"]
    assert (
        stage["review_satisfied"]
        and stage["candidate"] == integration["candidate"]["commit"]
    )
    api.get(f"resources/project-stages/{plan_id}")
    api.get("runs")


def rework(api):
    plan_id = _begin(api)
    _direct(api, plan_id)
    integration = _integrate(api, _delegate(api, plan_id))
    identifier = integration["integration_id"]
    review = _integration_review(api, integration, [OPUS], exit_code=1)
    assert review["assessment"]["status"] == "needs_rework", review
    findings = review["seats"][0]["attempts"][-1]["result"]["findings"]
    (finding,) = findings
    assert finding["path"] == "frontend/status.js" and finding["line_start"] == 1, (
        finding
    )
    round_id = review["round_id"]
    api.get(f"resources/review/{round_id}")
    snapshot_id = review["snapshot_id"]
    location = urlencode({"side": finding["side"], "path": finding["path"]})
    file = api.get(f"snapshots/{snapshot_id}/file?{location}")
    assert file["text"].splitlines()[finding["line_start"] - 1] == finding["quote"], (
        file
    )
    api.get(f"diffs/integration/{identifier}")
    repaired = api.job(
        "integration.rework", integration_id=identifier, round_id=round_id
    )
    child = repaired["revision"]
    child_id = child["integration_id"]
    assert (
        child_id != identifier and repaired["retest"]["test"]["status"] == "succeeded"
    )
    repaired_diff = api.get(f"diffs/integration/{child_id}")
    assert repaired_diff["candidate"] == child["candidate"]["commit"], repaired_diff
    assert "frontend/status.js" in repaired_diff["diff"], repaired_diff
    incremental = api.get(f"diffs/integration/{child_id}?against=parent")
    assert incremental["parent_candidate"] == integration["candidate"]["commit"], (
        incremental
    )
    assert incremental["candidate"] == child["candidate"]["commit"], incremental
    assert [
        line
        for line in incremental["diff"].splitlines()
        if line.startswith("diff --git ")
    ] == ["diff --git a/frontend/status.js b/frontend/status.js"], incremental
    _accept(api, child, repaired["rereview"]["review"])
    api.get("runs")


def dual_partial(api):
    plan_id = _begin(api)
    _direct(api, plan_id)
    integration = _integrate(api, _delegate(api, plan_id))
    identifier = integration["integration_id"]
    review = _integration_review(api, integration, [OPUS, ASTRA], exit_code=1)
    round_id = review["round_id"]
    data = api.get(f"resources/review/{round_id}?history=1")["data"]
    claude, codex = data["seats"]
    assert data["assessment"]["status"] == "incomplete", data
    assert claude["attempts"][-1]["status"] == "failed", claude
    assert codex["attempts"][-1]["status"] == "succeeded", codex
    assert codex["attempts"][-1]["reported_model"] is None, codex
    retried = api.job(
        "integration.retry_review",
        integration_id=identifier,
        round_id=round_id,
        seat_keys=[claude["key"]],
    )["review"]
    assert (
        retried["round_id"] == round_id
        and retried["snapshot_id"] == review["snapshot_id"]
    )
    assert [len(seat["attempts"]) for seat in retried["seats"]] == [2, 1], retried
    api.get(f"resources/review/{round_id}?history=1")
    _accept(api, integration, retried)


def _wait_for_implementation(api, identifier, planning_id):
    live = api.wait(identifier, {"running"})
    assert live["args"]["planning_run_id"] == planning_id, live
    # The cancel fixture delays this real environment check until the current
    # Claude implementation's waiting message is committed to task history.
    check = api.start("environment.refresh", provider="codex")
    observed = api.finish(check, exit_code=None)
    assert observed["error"] is None and observed["result"]["error"] is None
    assert observed["result"]["logged_in"] and observed["result"]["provider"] == "codex"
    return observed


def _assert_waiting_implementation(task, identifier, planning_id):
    assert task["planning_run_id"] == planning_id, task
    attempt = task["attempts"][-1]
    worktree = PurePosixPath(attempt["request"]["worktree"])
    assert worktree.parent.name == f"web-run-{identifier}", worktree
    assert attempt["request"]["decision"]["choice"]["provider"] == "claude-code", (
        attempt
    )
    session_id = attempt["runtime"]["session_id"]
    assert session_id, attempt
    events = attempt["events"]
    assert any(
        event["kind"] == "task_environment_observed"
        and event["environment"]["logged_in"]
        and event["environment"]["cli_version"] == "2.1.325 (Claude Code)"
        for event in events
    ), events
    assert any(
        event["kind"] == "session_started" and event["session_id"] == session_id
        for event in events
    ), events
    assert any(
        event["kind"] == "message"
        and event["raw_type"] == "assistant"
        and event["text"] == "waiting"
        for event in events
    ), events


def cancel(api):
    plan_id = _begin(api)
    direct, _, artifact = _direct(api, plan_id)
    stage = _delegate(api, plan_id)
    stage_id = stage["stage_run_id"]
    first, second = [
        _prepare_leaf(api, stage_id, item["card_id"]) for item in stage["leaves"]
    ]
    running = api.start("task.run", planning_run_id=first)
    first_check = _wait_for_implementation(api, running, first)
    busy = api.request(
        "POST",
        "/api/jobs",
        {"action": "task.run", "args": {"planning_run_id": second}},
        status=409,
    )
    assert busy["error"]["code"] == "busy", busy
    assert busy["error"]["details"]["active_job_id"] == running, busy
    task_id = direct["task_run_id"]
    for kind, identifier in (("task", task_id), ("stage", stage_id)):
        locked = api.get(f"resources/{kind}/{identifier}")
        assert locked["lock_busy"] and locked["data"] is None, locked
        assert locked["artifacts"] == [], locked
    locked = api.get(f"diffs/task/{task_id}")
    assert locked["lock_busy"] and locked["diff"] is None, locked
    assert locked["unavailable_code"] == "lock_busy", locked
    locked = api.get(f"artifacts/{artifact['artifact_id']}?offset=0&limit=4096")
    assert locked["lock_busy"] and locked["text"] is None, locked
    assert locked["next_offset"] is None, locked
    cancelled = api.request("POST", f"/api/jobs/{running}/cancel", {}, status=202)
    assert cancelled["state"] == "cancelling", cancelled
    settled = api.finish(running, exit_code=130)
    assert settled["cancel_requested_at"], settled
    assert settled["result"]["phase"] == "cancelled", settled
    _assert_waiting_implementation(settled["result"], running, first)
    api.get(f"resources/plan/{first}")

    next_job = api.start("task.run", planning_run_id=second)
    second_check = _wait_for_implementation(api, next_job, second)
    api.restart()
    interrupted = api.wait(next_job, {"interrupted"})
    assert interrupted["result"] is None, interrupted
    assert interrupted["error"]["code"] == "service_interrupted", interrupted
    plan = api.get(f"resources/plan/{second}")["data"]
    task = api.get(f"resources/task/{plan['task_run_id']}")["data"]
    _assert_waiting_implementation(task, next_job, second)
    resumed_jobs = {job["job_id"]: job for job in api.get("jobs")["items"]}
    for job in (settled, interrupted, first_check, second_check):
        assert resumed_jobs[job["job_id"]] == job, job


SCENARIOS = {
    "accept": accept,
    "rework": rework,
    "dual-partial": dual_partial,
    "cancel": cancel,
}
