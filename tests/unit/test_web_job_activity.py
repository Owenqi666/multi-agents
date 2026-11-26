"""A real two-seat CLI job exposes observations before either the job or retry finishes."""

import json
import time

import test_task_workflow_cli as helpers
from test_integration_cli import create_args
from test_integration_rework_cli import _parallel_members, _reviewers, _structural_suite
from test_integration_verification_cli import _review_arguments, _test_arguments
from test_web_independent_review import INPUT
from test_web_project_flow import FABLE, SOL, _fixture, _get, _job, _request
from test_web_workspace import _serve

workflow = helpers.workflow


def _until(service, path, predicate):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        value = _get(service, path)
        if predicate(value):
            return value
        time.sleep(0.05)
    raise AssertionError((path, value))


def test_live_dual_review_and_targeted_retry_after_restart(workflow):
    codex, claude, *_ = _fixture(workflow)
    programs = {"codex": str(codex), "claude": str(claude)}
    report = {"summary": "synthetic review", "findings": []}
    helpers.configure(claude, role="review", mode="wait", report=report)
    helpers.configure(codex, role="review", report=report)
    data = workflow.base / "data"
    with _serve(workflow.repository, data, programs) as service:
        snapshot = _job(service, "review.freeze", **INPUT)["result"]["snapshot_id"]
        status, receipt = _request(
            service,
            "POST",
            "/api/jobs",
            {
                "action": "review.run",
                "args": {"snapshot_id": snapshot, "seats": [SOL, FABLE]},
            },
        )
        assert status == 202
        identifier = receipt["job_id"]
        path = f"jobs/{identifier}/activity"
        active = _until(
            service,
            path,
            lambda record: (
                record["reviews"]
                and [s["state"] for s in record["reviews"][0]["seats"]]
                == ["succeeded", "running"]
                and any(
                    e["event"] == "message" and e["provider"] == "claude"
                    for e in record["events"]
                )
            ),
        )
        assert active["state"] == "running" and active["controller_holds_journal"]
        assert not active["recovery"]
        assert all(
            {
                "sequence",
                "at",
                "event",
                "resource",
                "seat",
                "provider",
                "attempt",
                "state",
            }
            == set(e)
            for e in active["events"]
        )
        assert sorted({e["sequence"] for e in active["events"]}) == [
            e["sequence"] for e in active["events"]
        ]
        assert _request(service, "POST", f"/api/jobs/{identifier}/cancel", {})[0] == 202
        ended = _until(service, path, lambda record: record["state"] == "finished")
        assert not ended["controller_holds_journal"]
    with _serve(workflow.repository, data, programs) as service:
        restored = _get(service, path)
        round_id = restored["reviews"][0]["round_id"]
        assert restored["reviews"][0]["snapshot_id"] == snapshot
        assert restored["events"] == ended["events"]
        [action] = restored["recovery"]
        assert action["args"]["seat_keys"] == ["reviewer-2"]
        helpers.configure(claude, mode="success")
        retried = _job(service, action["action"], **action["args"])["result"]
        assert retried["round_id"] == round_id and retried["snapshot_id"] == snapshot
        assert [len(seat["attempts"]) for seat in retried["seats"]] == [1, 2]
        assert not _get(service, path)["recovery"]
        stale = _job(service, action["action"], exit_code=2, **action["args"])
        assert "attempts have changed" in stale["error"]["message"]
        assert _request(service, "GET", "/api/jobs/unknown/activity")[0] == 404


def test_legacy_interrupted_retry_requires_an_identified_seat_attempt(workflow):
    codex, claude, *_ = _fixture(workflow)
    programs = {"codex": str(codex), "claude": str(claude)}
    for executable in (codex, claude):
        helpers.configure(executable, role="review", mode="model-error")
    data = workflow.base / "data"
    with _serve(workflow.repository, data, programs) as service:
        snapshot = _job(service, "review.freeze", **INPUT)["result"]["snapshot_id"]
        review = _job(service, "review.run", snapshot_id=snapshot, seats=[SOL, FABLE])
        round_id = review["result"]["round_id"]
        retry = _job(
            service, "review.retry", round_id=round_id, seat_keys=["reviewer-2"]
        )
        identifier = retry["job_id"]
        path = f"jobs/{identifier}/activity"
        [action] = _get(service, path)["recovery"]
        assert action["args"] == {
            "round_id": round_id,
            "seat_keys": ["reviewer-2"],
            "expected_attempts": {"reviewer-2": 2},
        }

    # Reopen the format used before activity journals, with no terminal receipt.
    (data / "web-jobs" / identifier / "activity.jsonl").unlink()
    state_path = data / "web-environment" / "state.json"
    state = json.loads(state_path.read_text())
    state["jobs"][identifier].update(
        state="running",
        result=None,
        resources=[],
        finished_at=None,
        exit_code=None,
    )
    state_path.write_text(json.dumps(state))
    with _serve(workflow.repository, data, programs) as service:
        restored = _get(service, path)
        assert restored["state"] == "interrupted"
        assert restored["events"] == [] and restored["controller_holds_journal"] is None
        [review] = restored["reviews"]
        assert review["snapshot_id"] == snapshot
        assert [s["state"] for s in review["seats"]] == ["failed", "failed"]
        assert restored["recovery"] == []

    # A versioned request can identify a failed seat even without a receipt.
    state["jobs"][identifier]["args"].update(action["args"])
    state_path.write_text(json.dumps(state))
    with _serve(workflow.repository, data, programs) as service:
        assert _get(service, path)["recovery"] == [action]
        _job(service, action["action"], **action["args"])
        assert _get(service, path)["recovery"] == []


def test_legacy_integration_confirmation_receipt_and_stale_retry(workflow):
    data = workflow.base
    workflow.database = data / "reviews.sqlite3"
    test_bin = _structural_suite(workflow)
    members, *_ = _parallel_members(workflow)
    integration = helpers.successful(*create_args(workflow, *members))
    helpers.successful(
        *_test_arguments(workflow, integration, test_bin, "initial-tests")
    )
    reviewers = _reviewers(workflow, integration["candidate"]["commit"])
    parent = helpers.successful(
        *_review_arguments(
            workflow, integration, reviewers, "initial-review", dual=True
        ),
        "--no-auto-rework",
        code=1,
    )["review"]
    integration_id = integration["integration_id"]
    helpers.configure(reviewers["claude"], mode="model-error")
    programs = {name: str(path) for name, path in reviewers.items()}
    with _serve(workflow.repository, data, programs) as service:
        confirm = _job(
            service,
            "integration.confirm",
            integration_id=integration_id,
            round_id=parent["round_id"],
            exit_code=1,
        )
        identifier = confirm["job_id"]
        child = confirm["result"]["confirmation"]["round_id"]
        assert child != parent["round_id"]
    (data / "web-jobs" / identifier / "activity.jsonl").unlink()

    with _serve(workflow.repository, data, programs) as service:
        path = f"jobs/{identifier}/activity"
        legacy = _get(service, path)
        [review] = legacy["reviews"]
        assert (review["round_id"], review["snapshot_id"]) == (
            child,
            parent["snapshot_id"],
        )
        [action] = legacy["recovery"]
        assert action == {
            "resource": {"kind": "review", "id": child},
            "action": "integration.retry_review",
            "args": {
                "integration_id": integration_id,
                "round_id": child,
                "seat_keys": ["seat-2"],
                "expected_attempts": {"seat-2": 1},
            },
        }
        retry = _job(service, action["action"], **action["args"], exit_code=1)
        assert retry["result"]["review"]["seats"] == parent["seats"]
        assert retry["result"]["confirmation"]["seats"][0]["current_attempt"] == 2
        assert _get(service, path)["recovery"] == []
        stale = _job(service, action["action"], **action["args"], exit_code=2)
        assert "attempts have changed" in stale["error"]["message"]
        assert _get(service, f"jobs/{stale['job_id']}/activity")["recovery"] == []
        retry_path = f"jobs/{retry['job_id']}/activity"
        [latest] = _get(service, retry_path)["recovery"]
        assert latest["args"]["expected_attempts"] == {"seat-2": 2}

        helpers.successful(
            *_test_arguments(workflow, integration, test_bin, "new-tests")
        )
        assert _get(service, retry_path)["recovery"] == []
