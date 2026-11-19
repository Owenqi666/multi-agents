"""Exercise the service's execution-lock gate through HTTP requests."""

import hashlib
import signal
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import test_web_workspace as helpers
from test_web_workspace import _finished, _refresh, _request, _serve, _wait_for

from multi_agent.cli.workspaces import executor_lock
from multi_agent.web import actions, jobs, resources, server

repository = helpers.repository


def _submit(service, action, args=None):
    return _request(
        service, "POST", "/api/jobs", {"action": action, "args": args or {}}
    )


def _assert_busy(service, identifier):
    status, body = _submit(
        service, "project.stage.integrate", {"stage_run_id": "stage-1"}
    )
    assert status == 409
    assert body["error"]["code"] == "busy"
    assert body["error"]["details"] == {"active_job_id": identifier}
    assert {path.name for path in service.jobs.job_root.iterdir()} == {identifier}


def test_active_cli_waits_for_existing_read_and_blocks_only_lock_taking_requests(
    tmp_path, repository, monkeypatch
):
    reading = Event()
    release_read = Event()
    launched = Event()
    signalled = Event()
    release_cli = Event()

    def resource(_workspace, kind, identifier, _query):
        if identifier == "in-flight":
            reading.set()
            assert release_read.wait(timeout=6)
        return {"kind": kind, "id": identifier, "data": {"real": True}, "artifacts": []}

    def launch(_argv, *, stdout, **_kwargs):
        assert release_read.is_set()
        stdout.write(b"{}")
        stdout.flush()
        launched.set()

        def wait():
            assert release_cli.wait(timeout=6)
            return 130 if signalled.is_set() else 0

        def send_signal(value):
            assert value == signal.SIGINT
            signalled.set()

        return SimpleNamespace(wait=wait, poll=lambda: None, send_signal=send_signal)

    prepare = Mock(return_value=["fake-cli"])
    read = Mock(side_effect=resource)
    diff = Mock(return_value={"diff": "saved snapshot"})
    wire = b"frozen log"
    digest = hashlib.sha256(wire).hexdigest()
    load_artifact = Mock(return_value={"snapshot_id": "s-1"})
    monkeypatch.setattr(server, "prepare_action", prepare)
    monkeypatch.setattr(server, "read_resource", read)
    monkeypatch.setattr(server, "read_diff", diff)
    monkeypatch.setattr(
        jobs,
        "subprocess",
        SimpleNamespace(Popen=launch, DEVNULL=jobs.subprocess.DEVNULL),
    )
    monkeypatch.setattr(jobs.EnvironmentJobs, "_observe", lambda *_: ({}, None))
    monkeypatch.setattr(resources, "_resource_data", load_artifact)
    monkeypatch.setattr(
        resources,
        "_artifacts",
        lambda *_: {
            "frozen-1-stdout": resources._Artifact("stdout", digest, len(wire), wire)
        },
    )

    with (
        _serve(repository, tmp_path / "data", {}) as service,
        ThreadPoolExecutor(1) as pool,
    ):
        in_flight = pool.submit(
            _request, service, "GET", "/api/resources/task/in-flight"
        )
        try:
            assert reading.wait(timeout=2)
            status, accepted = _submit(service, "project.plan")
            assert status == 202
            identifier = accepted["job_id"]
            job_url = f"/api/jobs/{identifier}"
            assert _request(service, "GET", job_url)[1]["state"] == "queued"
            assert not launched.is_set()
            _assert_busy(service, identifier)
            status, refresh = _refresh(service, "codex")
            assert status == 202
            _wait_for(lambda: _finished(service, refresh["job_id"]))

            guarded_kinds = (
                "task",
                "task-test",
                "task-review",
                "integration",
                "stage",
                "plan",
            )
            for kind in guarded_kinds:
                selection = (
                    {"implementation_attempt": ["1"]} if kind == "task-test" else {}
                )
                query = "?implementation_attempt=1" if selection else ""
                assert _request(
                    service, "GET", f"/api/resources/{kind}/resource-1{query}"
                ) == (
                    200,
                    {
                        "kind": kind,
                        "id": "resource-1",
                        "data": None,
                        "artifacts": [],
                        "lock_busy": True,
                    },
                )
                artifact_id = resources._artifact_id(
                    kind, "resource-1", selection, "frozen-1-stdout", digest
                )
                assert _request(
                    service, "GET", f"/api/artifacts/{artifact_id}?offset=3"
                ) == (
                    200,
                    {
                        "stream": None,
                        "text": None,
                        "offset": 3,
                        "next_offset": None,
                        "byte_count": None,
                        "sha256": None,
                        "truncated": None,
                        "lock_busy": True,
                    },
                )
            for kind in ("task", "integration"):
                status, blocked = _request(
                    service, "GET", f"/api/diffs/{kind}/resource-1"
                )
                assert status == 200 and blocked.pop("unavailable_reason")
                assert blocked == {
                    "baseline": None,
                    "candidate": None,
                    "snapshot_id": None,
                    "diff": None,
                    "test_change_paths": [],
                    "unavailable_code": "lock_busy",
                    "lock_busy": True,
                }
            assert read.call_count == 1
            diff.assert_not_called()
            load_artifact.assert_not_called()

            for path in (
                "/api/resources/task-test/resource-1?implementation_attempt=0",
                "/api/resources/task/%20",
                "/api/diffs/task/%20",
            ):
                assert _request(service, "GET", path)[0] == 400
            assert _request(service, "GET", "/api/artifacts/art-invalid")[0] == 404
            for kind in (
                "project",
                "consultation",
                "goal",
                "project-plan",
                "project-stages",
                "review",
                "snapshot",
            ):
                status, direct = _request(
                    service, "GET", f"/api/resources/{kind}/resource-1"
                )
                assert status == 200 and direct["data"] == {"real": True}
            assert _request(service, "GET", "/api/diffs/snapshot/s-1") == (
                200,
                {"diff": "saved snapshot"},
            )
            artifact_id = resources._artifact_id(
                "snapshot", "s-1", {}, "frozen-1-stdout", digest
            )
            assert (
                _request(service, "GET", f"/api/artifacts/{artifact_id}")[1]["text"]
                == "frozen log"
            )

            release_read.set()
            assert in_flight.result(timeout=2)[1]["data"] == {"real": True}
            assert launched.wait(timeout=2)
            assert _request(service, "GET", job_url)[1]["state"] == "running"
            _assert_busy(service, identifier)
            assert _request(service, "POST", job_url + "/cancel", {})[0] == 202
            assert signalled.is_set()
            assert _request(service, "GET", job_url)[1]["state"] == "cancelling"
            _assert_busy(service, identifier)
            prepare.assert_called_once()
        finally:
            release_read.set()
            release_cli.set()
        assert _wait_for(lambda: _finished(service, identifier))["exit_code"] == 130
        assert _request(service, "GET", "/api/resources/task/t-1")[1]["data"] == {
            "real": True
        }


def test_lock_taking_reads_and_stage_integration_preflight_are_serial(
    tmp_path, repository, monkeypatch
):
    release = {"first": Event(), "second": Event()}
    entered = {"first": Event(), "second": Event(), "preflight": Event()}

    def resource(_workspace, _kind, identifier, _query):
        entered[identifier].set()
        assert release[identifier].wait(timeout=6)
        return {"data": identifier}

    def preflight(_workspace, _kind, _identifier, _query):
        entered["preflight"].set()
        return {"data": {"mode": "direct", "can_integrate": False}}

    monkeypatch.setattr(server, "read_resource", resource)
    monkeypatch.setattr(actions, "require_owned", lambda *_: None)
    monkeypatch.setattr(actions, "read_resource", preflight)
    with (
        _serve(repository, tmp_path / "data", {}) as service,
        ThreadPoolExecutor(3) as pool,
    ):
        read = service.jobs.read_gate.read
        first_attempt, second_attempt, third_attempt = Event(), Event(), Event()
        next_attempt = iter((first_attempt, second_attempt, third_attempt))

        @contextmanager
        def observed_read(**kwargs):
            next(next_attempt).set()
            with read(**kwargs) as allowed:
                yield allowed

        monkeypatch.setattr(service.jobs.read_gate, "read", observed_read)
        first = pool.submit(_request, service, "GET", "/api/resources/task/first")
        try:
            assert first_attempt.wait(timeout=2) and entered["first"].wait(timeout=2)
            second = pool.submit(_request, service, "GET", "/api/resources/plan/second")
            assert second_attempt.wait(timeout=2)
            assert not entered["second"].is_set()
            release["first"].set()
            assert first.result(timeout=2) == (200, {"data": "first"})
            assert entered["second"].wait(timeout=2)

            integration = pool.submit(
                _submit, service, "project.stage.integrate", {"stage_run_id": "stage-1"}
            )
            assert third_attempt.wait(timeout=2)
            assert not entered["preflight"].is_set()
            release["second"].set()
            assert second.result(timeout=2) == (200, {"data": "second"})
            status, body = integration.result(timeout=2)
            assert entered["preflight"].is_set()
            assert status == 409 and body["error"]["code"] == "job_unavailable"
            assert list(service.jobs.job_root.iterdir()) == []
        finally:
            release["first"].set()
            release["second"].set()


def test_close_drains_the_held_executor_lock_before_reopening(
    tmp_path, repository, monkeypatch
):
    holding_lock = Event()
    release_lock = Event()
    first_attempt, waiting_attempt, preflight_attempt = Event(), Event(), Event()
    attempts = iter((first_attempt, waiting_attempt, preflight_attempt))

    def resource(workspace, _kind, identifier, _query):
        assert identifier == "held"
        with executor_lock(workspace.repository):
            holding_lock.set()
            assert release_lock.wait(timeout=6)
        return {"data": "released"}

    stage_view = Mock(
        return_value={"data": {"mode": "delegate", "can_integrate": True}}
    )
    monkeypatch.setattr(server, "read_resource", resource)
    monkeypatch.setattr(actions, "require_owned", lambda *_: None)
    monkeypatch.setattr(actions, "read_resource", stage_view)
    with (
        _serve(repository, tmp_path / "data", {}) as service,
        ThreadPoolExecutor(4) as pool,
    ):
        address = service.server_port
        workspace = service.workspace
        read = service.jobs.read_gate.read

        @contextmanager
        def observed_read(**kwargs):
            next(attempts).set()
            with read(**kwargs) as allowed:
                yield allowed

        monkeypatch.setattr(service.jobs.read_gate, "read", observed_read)
        held = pool.submit(_request, service, "GET", "/api/resources/task/held")
        try:
            assert first_attempt.wait(timeout=2) and holding_lock.wait(timeout=2)
            waiting = pool.submit(
                _request, service, "GET", "/api/resources/task/waiting"
            )
            assert waiting_attempt.wait(timeout=2)
            preflight = pool.submit(
                _submit, service, "project.stage.integrate", {"stage_run_id": "stage-1"}
            )
            assert preflight_attempt.wait(timeout=2)
            service.shutdown()
            closing = pool.submit(service.server_close)

            status, blocked = waiting.result(timeout=2)
            assert status == 200 and blocked["lock_busy"] is True
            status, unavailable = preflight.result(timeout=2)
            assert status == 409 and unavailable["error"]["code"] == "job_unavailable"
            stage_view.assert_not_called()
            assert list(service.jobs.job_root.iterdir()) == []
            assert not closing.done()
            with pytest.raises(ValueError, match="already has a service owner"):
                server.WorkspaceServer(0, workspace, {})
        finally:
            release_lock.set()
        assert held.result(timeout=2) == (200, {"data": "released"})
        closing.result(timeout=2)

    script = """
import sys
from pathlib import Path
from multi_agent.cli.workspaces import executor_lock

try:
    with executor_lock(Path(sys.argv[1])):
        print("{}")
except ValueError as error:
    print(error, file=sys.stderr)
    sys.exit(2)
"""
    monkeypatch.setattr(
        server,
        "prepare_action",
        lambda *_args, **_kwargs: [sys.executable, "-c", script, str(repository)],
    )
    with server.WorkspaceServer(address, workspace, {}) as reopened:
        worker = Thread(target=reopened.serve_forever, kwargs={"poll_interval": 0.01})
        worker.start()
        try:
            status, accepted = _submit(reopened, "project.plan")
            assert status == 202
            finished = _wait_for(lambda: _finished(reopened, accepted["job_id"]))
            assert finished["exit_code"] == 0, finished
        finally:
            reopened.shutdown()
            worker.join()
