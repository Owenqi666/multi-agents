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


def _attention(api):
    inbox = api.get("attention")
    assert inbox["complete"] and isinstance(inbox["items"], list), inbox
    return inbox["items"]


def _begin(api):
    workspace = api.get("workspace")
    api.normalizer.bind(repository=workspace["repository"])
    baseline = workspace["head"]
    with api.supplement():
        first = api.get("workspace/onboarding")
        assert first["baseline"] == baseline and first["rules"]["status"] == "ready"
        assert {p["status"] for p in first["providers"].values()} == {"unknown"}
    assert workspace["branches"] == [{"name": "main", "head": baseline}], workspace
    defaults = workspace["defaults"]
    assert defaults["source"] == "cli_default" and defaults["stage_mode"] == "direct", (
        defaults
    )
    assert defaults["task_plan"]["plan_review"]["model"] == "gpt-6.1-sol", defaults
    for provider in ("codex", "claude"):
        observation = api.job("environment.refresh", provider=provider, exit_code=None)
        assert observation["error"] is None, observation
    refreshed = api.get("workspace")
    with api.supplement():
        ready = api.get("workspace/onboarding")
        assert {p["status"] for p in ready["providers"].values()} == {"ready"}
    catalog = refreshed["environments"]["codex"]["models"]
    assert {"gpt-6.2-luna", "gpt-6.1-sol", "claude-haiku-5-5"} <= set(catalog)
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
    with api.supplement():
        assert _attention(api)
    return plan_id, defaults["task_plan"]


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
    job_id = api.start(
        "integration.review",
        integration_id=identifier,
        seats=seats,
        auto_rework=False,
    )
    review = api.finish(job_id, exit_code)["result"]["review"]
    with api.supplement():
        activity = api.get(f"jobs/{job_id}/activity")
        assert activity["reviews"][0]["round_id"] == review["round_id"], activity
        if review["assessment"]["status"] == "incomplete":
            seats = activity["reviews"][0]["seats"]
            assert {s["state"] for s in seats} == {"failed", "succeeded"}, seats
            assert activity["recovery"], activity
        if review["assessment"]["status"] != "ready":
            _attention(api)
    return job_id, review


def _accept(api, integration, review):
    identifier = integration["integration_id"]
    assert review["assessment"]["status"] == "ready", review
    with api.supplement():
        assert any(item["id"] == identifier for item in _attention(api))
    job_id = api.start(
        "integration.accept", integration_id=identifier, round_id=review["round_id"]
    )
    acceptance = api.finish(job_id)["result"]["acceptance"]
    assert acceptance["candidate"]["commit"] == integration["candidate"]["commit"]
    assert acceptance["accepted_at"], acceptance
    api.get(f"resources/integration/{identifier}")
    with api.supplement():
        api.get(f"jobs/{job_id}/activity")
        delivery = api.get(f"integrations/{identifier}/delivery")
        assert delivery["receipt"]["acceptance_id"] == acceptance["acceptance_id"]
        assert [test["name"] for test in delivery["tests"]] == COMMANDS
        assert not api.get(f"integrations/{identifier}/rework-eligibility")["eligible"]
        assert all(item["id"] != identifier for item in _attention(api))


def accept(api):
    plan_id, _ = _begin(api)
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
    _, review = _integration_review(api, integration, [OPUS])
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
    runs = {run["id"]: run for run in api.get("runs")["items"]}
    assert runs[identifier]["label"] != identifier, runs[identifier]


def rework(api):
    plan_id, defaults = _begin(api)
    _direct(api, plan_id)
    integration = _integrate(api, _delegate(api, plan_id))
    identifier = integration["integration_id"]
    _, review = _integration_review(api, integration, [OPUS], exit_code=1)
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
    with api.supplement():
        parent = api.get(f"resources/integration/{identifier}")["data"]
        assert parent["superseded_by"] is None and parent["rework"] is None
        eligibility_path = f"integrations/{identifier}/rework-eligibility"
        before = api.get(eligibility_path)
        assert before["eligible"] and before["owner"], before
        findings_path = f"reviews/{round_id}/findings"
        original = api.get(findings_path)
        member = original["groups"][0]["members"][0]
        anchor = member["anchor"]
        assert not member["activity"] and anchor["finding_id"] == finding["id"]
        decision = api.request(
            "POST",
            f"/api/reviews/{round_id}/decisions",
            {
                "anchor": anchor,
                "decision": "confirmed",
                "reason": "The candidate still shows draft.",
            },
            status=201,
        )
        assert decision["kind"] == "decision"
        decided = api.get(findings_path)["groups"][0]["members"][0]
        assert decided["activity"] == [decision]
        answer = api.job(
            "review.followup",
            review_anchor=anchor,
            question="Which status does the frozen candidate expose?",
        )["entry"]
        assert "draft" in answer["answer"]
        answered = api.get(findings_path)["groups"][0]["members"][0]
        assert answered["activity"] == [decision, answer]
        args = {
            "review_anchor": anchor,
            "baseline": original["source"]["correction_baseline"],
            "requirements": "Implement the standalone status.\n",
        }
        correction = api.job("plan.create", **args)
        frozen = correction["details"]["input"]
        assert frozen["draft_request"] == defaults["draft"]
        assert frozen["review_request"] == defaults["plan_review"]
        correction_id = correction["planning_run_id"]
        repeated = api.job("plan.create", **args)
        assert repeated["reused"] and repeated["planning_run_id"] == correction_id
        api.get(f"resources/plan/{correction_id}")
        updated = api.get(findings_path)
        entries = updated["groups"][0]["members"][0]["activity"]
        assert [entry["kind"] for entry in entries] == [
            "decision",
            "answer",
            "linked_plan",
        ]
        assert entries[-1]["planning_run_id"] == correction_id
        assert updated["status"] == original["status"]
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
    parent = api.get(f"resources/integration/{identifier}", file_number=82)["data"]
    assert parent["superseded_by"] == child_id
    assert parent["rework"]["child_id"] == child_id
    with api.supplement():
        after = api.get(eligibility_path)
        assert not after["eligible"] and after["reason"], after
    _accept(api, child, repaired["rereview"]["review"])
    # This diff is between two immutable candidates and is also available after acceptance.
    incremental = api.get(
        f"diffs/integration/{child_id}?against=parent", file_number=52
    )
    assert incremental["parent_candidate"] == integration["candidate"]["commit"], (
        incremental
    )
    assert incremental["candidate"] == child["candidate"]["commit"], incremental
    assert [
        line
        for line in incremental["diff"].splitlines()
        if line.startswith("diff --git ")
    ] == ["diff --git a/frontend/status.js b/frontend/status.js"], incremental
    runs = {run["id"]: run for run in api.get("runs")["items"]}
    assert runs[child_id]["parent_id"] == identifier, runs[child_id]
    assert runs[child_id]["label"] != child_id, runs[child_id]


def dual_partial(api):
    plan_id, _ = _begin(api)
    _direct(api, plan_id)
    integration = _integrate(api, _delegate(api, plan_id))
    identifier = integration["integration_id"]
    original_job, review = _integration_review(
        api, integration, [OPUS, ASTRA], exit_code=1
    )
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
    refreshed = api.get(f"jobs/{original_job}/activity", file_number=63)
    assert {a["seat"]: a["state"] for a in refreshed["actors"]} == {
        claude["key"]: "failed",
        codex["key"]: "succeeded",
    }
    original_round = refreshed["reviews"][0]
    assert original_round["round_id"] == round_id
    assert [(s["attempt"], s["state"]) for s in original_round["seats"]] == [
        (2, "succeeded"),
        (1, "succeeded"),
    ]
    assert not refreshed["recovery"] and original_round["retry"] is None
    assert not any(seat["retryable"] for seat in original_round["seats"])
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
    plan_id, _ = _begin(api)
    direct, _, artifact = _direct(api, plan_id)
    stage = _delegate(api, plan_id)
    stage_id = stage["stage_run_id"]
    first, second = [
        _prepare_leaf(api, stage_id, item["card_id"]) for item in stage["leaves"]
    ]
    running = api.start("task.run", planning_run_id=first)
    first_check = _wait_for_implementation(api, running, first)
    with api.supplement():
        active = api.get(f"jobs/{running}/activity")
        assert active["state"] == "running" and active["actors"], active
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
    cancelled_activity = api.get(f"jobs/{running}/activity", file_number=60)
    assert cancelled_activity["state"] == "finished"
    assert cancelled_activity["finished_at"] == settled["finished_at"]
    (actor,) = cancelled_activity["actors"]
    assert actor["last_event"] == "attempt_settled" and actor["state"] == "failed"
    assert not cancelled_activity["recovery"]

    next_job = api.start("task.run", planning_run_id=second)
    second_check = _wait_for_implementation(api, next_job, second)
    api.restart()
    interrupted = api.wait(next_job, {"interrupted"})
    assert interrupted["result"] is None, interrupted
    assert interrupted["error"]["code"] == "service_interrupted", interrupted
    with api.supplement():
        activity = api.get(f"jobs/{next_job}/activity")
        assert activity["state"] == "interrupted" and activity["resources"], activity
        _attention(api)
    api.get(f"resources/plan/{first}", file_number=45)
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
