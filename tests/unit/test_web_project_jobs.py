"""Exercise CLI job transport through the real loopback service."""

import hashlib
import json
import stat
import sys

import pytest
import test_web_workspace as helpers
from test_web_workspace import _finished, _request, _serve, _wait_for

from multi_agent.web import server

repository = helpers.repository


def _submit(service):
    status, accepted = _request(
        service, "POST", "/api/jobs", {"action": "project.plan", "args": {}}
    )
    assert status == 202 and accepted["state"] == "queued", accepted
    return accepted["job_id"]


def test_cli_job_cancels_owned_process_once_and_reopens_receipts(
    tmp_path, repository, monkeypatch
):
    data = tmp_path / "data"
    ready = tmp_path / "ready"
    signalled = tmp_path / "signalled"
    release = tmp_path / "release"
    launched = []

    def prepare(action, args, *, directory, **_kwargs):
        launched.append(directory)
        script = f"""
import json
import signal
import sys
import time
from pathlib import Path

def stop(_signum, _frame):
    with Path({str(signalled)!r}).open('a') as count:
        count.write('signal\\n')

signal.signal(signal.SIGINT, stop)
Path({str(ready)!r}).touch()
deadline = time.monotonic() + 8
while time.monotonic() < deadline and not Path({str(release)!r}).exists():
    time.sleep(0.02)
print(json.dumps({{'project_id': 'project-cancelled', 'partial': True}}))
print('cancellation receipt', file=sys.stderr)
sys.exit(130 if Path({str(signalled)!r}).exists() else 3)
"""
        return [sys.executable, "-c", script]

    monkeypatch.setattr(server, "prepare_action", prepare)
    with _serve(repository, data, {}) as service:
        job_id = _submit(service)
        job_url = f"/api/jobs/{job_id}"
        try:
            _wait_for(ready.exists)
            assert _request(service, "GET", job_url)[1]["state"] == "running"
            assert (
                _request(
                    service,
                    "POST",
                    job_url + "/cancel",
                    {},
                    Origin="https://invalid.test",
                )[0]
                == 403
            )
            assert _request(service, "POST", job_url + "/cancel", [])[0] == 400
            assert not signalled.exists()
            assert _request(service, "POST", job_url + "/cancel", {}) == (
                202,
                {"job_id": job_id, "state": "cancelling"},
            )
            _wait_for(signalled.exists)
            assert _request(service, "POST", job_url + "/cancel", {})[0] == 409
        finally:
            release.touch()
        finished = _wait_for(lambda: _finished(service, job_id))
        assert finished["exit_code"] == 130 and finished["error"] is None
        assert finished["result"] == {
            "project_id": "project-cancelled",
            "partial": True,
        }
        assert finished["resources"] == [{"kind": "project", "id": "project-cancelled"}]
        assert signalled.read_text() == "signal\n"
        assert launched == [data / "web-jobs" / job_id / "input"]
        assert stat.S_IMODE(launched[0].stat().st_mode) == 0o700

    stderr = next(item for item in finished["artifacts"] if item["stream"] == "stderr")
    artifact_url = f"/api/artifacts/{stderr['artifact_id']}"
    wire = b"cancellation receipt\n"
    digest = hashlib.sha256(wire).hexdigest()
    assert stderr["byte_count"] == len(wire) and stderr["sha256"] == digest

    state_path = data / "web-environment" / "state.json"
    state = json.loads(state_path.read_text())
    orphan = "a" * 32
    state["jobs"][orphan] = {
        "job_id": orphan,
        "action": "project.plan",
        "state": "running",
        "exit_code": None,
        "result": None,
        "error": None,
        "resources": [],
    }
    state_path.write_text(json.dumps(state))
    with _serve(repository, data, {}) as service:
        assert _request(service, "GET", job_url) == (200, finished)
        assert _request(service, "POST", job_url + "/cancel", {})[0] == 409
        assert _request(service, "POST", f"/api/jobs/{orphan}/cancel", {})[0] == 409
        status, interrupted = _request(service, "GET", f"/api/jobs/{orphan}")
        assert status == 200 and interrupted["state"] == "interrupted"
        assert interrupted["error"]["code"] == "service_interrupted"
        assert interrupted["exit_code"] is None and interrupted["result"] is None
        status, runs = _request(service, "GET", "/api/runs?kind=project")
        assert status == 200 and [item["id"] for item in runs["items"]] == [
            "project-cancelled"
        ]
        assert _request(service, "GET", artifact_url + "?offset=1&limit=4") == (
            200,
            {
                "stream": "stderr",
                "text": "ance",
                "offset": 1,
                "next_offset": 5,
                "byte_count": len(wire),
                "sha256": digest,
                "truncated": True,
            },
        )
        assert _request(service, "GET", artifact_url + "?limit=0")[0] == 400
        (launched[0].parent / "stderr").write_bytes(wire.upper())
        assert _request(service, "GET", artifact_url)[0] == 404
        assert len(launched) == 1 and signalled.read_text() == "signal\n"


@pytest.mark.parametrize(
    ("stdout", "exit_code", "result", "error"),
    [
        ('{\n  "value": true\n}\n', 1, {"value": True}, None),
        ('{"value": true, "value": false}\n', 1, None, "invalid_cli_output"),
        ('{"value": true}\nextra\n', 0, None, "invalid_cli_output"),
        ("", 130, None, "cli_cancelled"),
    ],
)
def test_cli_job_uses_entire_stdout_and_observed_exit(
    tmp_path, repository, monkeypatch, stdout, exit_code, result, error
):
    def prepare(_action, _args, **_kwargs):
        return [
            sys.executable,
            "-c",
            f"import sys; sys.stdout.write({stdout!r}); sys.exit({exit_code})",
        ]

    monkeypatch.setattr(server, "prepare_action", prepare)
    with _serve(repository, tmp_path / "data", {}) as service:
        identifier = _submit(service)
        settled = _wait_for(lambda: _finished(service, identifier))
        assert settled["exit_code"] == exit_code
        assert settled["result"] == result
        if error is None:
            assert settled["error"] is None
        else:
            assert settled["error"]["code"] == error
        assert (
            settled["artifacts"][0]["sha256"]
            == hashlib.sha256(stdout.encode()).hexdigest()
        )
