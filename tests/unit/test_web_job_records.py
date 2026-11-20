"""Exercise newly accepted and previously persisted work through the job API."""

import json
import sys
from datetime import UTC, datetime, timedelta
from threading import Event

import test_web_workspace as helpers
from test_web_workspace import _finished, _refresh, _request, _serve, _wait_for

from multi_agent.web import server
from multi_agent.web.jobs import EnvironmentJobs

repository = helpers.repository


def _submit(service, action, args):
    status, accepted = _request(
        service, "POST", "/api/jobs", {"action": action, "args": args}
    )
    assert status == 202 and accepted["state"] == "queued", accepted
    return accepted["job_id"]


def _legacy_job(identifier, state):
    return {
        "job_id": identifier,
        "action": "project.plan",
        "state": state,
        "exit_code": None,
        "result": None,
        "error": None,
        "resources": [],
    }


def test_job_record_timestamps_redacted_args_and_raw_stderr_survive_restart(
    tmp_path, repository, monkeypatch
):
    data = tmp_path / "data"
    ready = tmp_path / "ready"
    signalled = tmp_path / "signalled"
    release = tmp_path / "release"
    suffix = b"\xff" + "文本😀".encode()
    wire = b"discarded" + "界".encode() + b"a" * (8192 - 2 - len(suffix)) + suffix
    args = {
        "baseline": "HEAD",
        "original_goal": "原始问题😀\r\n下一行",
        "confirmed_goal": "确认问题é😀",
    }

    def prepare(_action, _args, **_kwargs):
        script = f"""
import signal
import sys
import time
from pathlib import Path

signal.signal(signal.SIGINT, lambda *_: Path({str(signalled)!r}).touch())
Path({str(ready)!r}).touch()
deadline = time.monotonic() + 8
while time.monotonic() < deadline and not Path({str(release)!r}).exists():
    time.sleep(0.02)
sys.stderr.buffer.write({wire!r})
sys.exit(130)
"""
        return [sys.executable, "-c", script]

    monkeypatch.setattr(server, "prepare_action", prepare)
    with _serve(repository, data, {}) as service:
        identifier = _submit(service, "project.confirm", args)
        url = f"/api/jobs/{identifier}"
        try:
            _wait_for(ready.exists)
            status, running = _request(service, "GET", url)
            assert status == 200 and running["state"] == "running"
            assert running["args"] == {
                "baseline": "HEAD",
                "original_goal": {"length": len(args["original_goal"])},
                "confirmed_goal": {"length": len(args["confirmed_goal"])},
            }
            assert running["finished_at"] is None
            assert running["cancel_requested_at"] is None
            assert running["stderr_tail"] is None
            assert _request(service, "POST", url + "/cancel", {})[0] == 202
            _wait_for(signalled.exists)
            status, cancelling = _request(service, "GET", url)
            assert status == 200 and cancelling["state"] == "cancelling"
            assert cancelling["cancel_requested_at"] is not None
            assert cancelling["finished_at"] is None
            assert cancelling["stderr_tail"] is None
            assert _request(service, "GET", "/api/jobs") == (
                200,
                {"items": [cancelling]},
            )
        finally:
            release.touch()
        finished = _wait_for(lambda: _finished(service, identifier))
        assert finished["exit_code"] == 130
        assert finished["cancel_requested_at"] == cancelling["cancel_requested_at"]
        assert finished["stderr_tail"] == wire[-8192:].decode("utf-8", "replace")
        stamps = [
            datetime.fromisoformat(finished[field])
            for field in (
                "created_at",
                "started_at",
                "cancel_requested_at",
                "finished_at",
            )
        ]
        assert all(stamp.utcoffset() == timedelta(0) for stamp in stamps)
        assert stamps == sorted(stamps)

    state_path = data / "web-environment" / "state.json"
    persisted = json.loads(state_path.read_text())
    assert persisted["jobs"][identifier] == finished
    legacy = "a" * 32
    persisted["jobs"][legacy] = _legacy_job(legacy, "running")
    interrupted = "b" * 32
    persisted["jobs"][interrupted] = {
        **cancelling,
        "job_id": interrupted,
    }
    state_path.write_text(json.dumps(persisted))

    with _serve(repository, data, {}) as service:
        assert _request(service, "GET", url) == (200, finished)
        status, restored = _request(service, "GET", f"/api/jobs/{legacy}")
        assert status == 200 and restored["state"] == "interrupted"
        assert restored["error"]["code"] == "service_interrupted"
        for field in (
            "args",
            "created_at",
            "started_at",
            "finished_at",
            "cancel_requested_at",
            "stderr_tail",
        ):
            assert restored[field] is None
        status, recovered = _request(service, "GET", f"/api/jobs/{interrupted}")
        assert status == 200 and recovered == {
            **persisted["jobs"][interrupted],
            "state": "interrupted",
            "error": restored["error"],
        }
        assert _request(service, "GET", "/api/jobs") == (
            200,
            {"items": [recovered, finished, restored]},
        )


def test_job_record_counts_unicode_in_compact_document(
    tmp_path, repository, monkeypatch
):
    def prepare(*_args, **_kwargs):
        return [sys.executable, "-c", "print('{}')"]

    monkeypatch.setattr(server, "prepare_action", prepare)
    with _serve(repository, tmp_path / "data", {}) as service:
        identifier = _submit(
            service,
            "project.revise",
            {"plan_id": "plan-known", "document": {"步骤": ["甲😀", {"k": True}]}},
        )
        finished = _wait_for(lambda: _finished(service, identifier))
        assert finished["args"] == {
            "plan_id": "plan-known",
            "document": {"length": len('{"步骤":["甲😀",{"k":true}]}')},
        }
        assert finished["cancel_requested_at"] is None
        assert finished["stderr_tail"] == ""


def test_job_list_keeps_an_active_refresh_older_than_the_latest_fifty(
    tmp_path, repository, monkeypatch
):
    checking = Event()
    release = Event()

    def observe(_self, _provider, _identifier):
        checking.set()
        release.wait(timeout=8)
        return {}, None

    monkeypatch.setattr(EnvironmentJobs, "_observe", observe)
    data = tmp_path / "data"
    with _serve(repository, data, {}):
        pass
    state_path = data / "web-environment" / "state.json"
    state = json.loads(state_path.read_text())
    after_now = datetime.now(UTC) + timedelta(days=1)
    identifiers = [f"{i:032x}" for i in range(1, 56)]
    for index, identifier in enumerate(identifiers):
        state["jobs"][identifier] = {
            **_legacy_job(identifier, "finished"),
            "created_at": (after_now - timedelta(seconds=index)).isoformat(),
        }
    state_path.write_text(json.dumps(state))

    with _serve(repository, data, {}) as service:
        assert _request(service, "GET", "/api/jobs?limit=1")[0] == 400
        status, recent = _request(service, "GET", "/api/jobs")
        assert status == 200
        assert [item["job_id"] for item in recent["items"]] == identifiers[:50]

        status, accepted = _refresh(service, "codex")
        assert status == 202
        try:
            assert checking.wait(timeout=6)
            status, with_active = _request(service, "GET", "/api/jobs")
            assert status == 200 and with_active["items"][:-1] == recent["items"]
            active = with_active["items"][-1]
            assert active["job_id"] == accepted["job_id"]
            assert active["state"] == "running"
            assert active["args"] == {"provider": "codex"}
            assert active["stderr_tail"] is None
        finally:
            release.set()
        refresh = _wait_for(lambda: _finished(service, accepted["job_id"]))
        assert refresh["started_at"] and refresh["finished_at"]
        assert _request(service, "GET", "/api/jobs") == (200, recent)
