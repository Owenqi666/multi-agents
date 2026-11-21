"""Exercise shared admission through independent CLI processes and the HTTP bridge."""

import json
import signal
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import test_project_cli as project_fixture
import test_runtime as runtime_fixture
import test_task_workflow_cli as workflow_fixture
from test_web_project_flow import _job
from test_web_workspace import _finished, _refresh, _serve, _wait_for

from multi_agent.store.project_store import ProjectStore

project = project_fixture.project
document = project_fixture.document
synthetic_codex_auth_and_fake_environment = (
    runtime_fixture.synthetic_codex_auth_and_fake_environment
)
_FABLE = "claude-fable-5-2"


def _fake(tmp_path, name, provider, *, mode="success", report=None):
    root = tmp_path / name
    root.mkdir()
    program = Path(runtime_fixture.fake(root, provider))
    capture = root / "wire.jsonl"
    environment = {
        "FAKE_RUNTIME_MODE": mode,
        "FAKE_RUNTIME_CAPTURE": str(capture),
        "FAKE_RUNTIME_REPORT": json.dumps(
            {"summary": "Offline review", "findings": []} if report is None else report
        ),
    }
    shebang, body = program.read_text().split("\n", 1)
    # The real Codex adapter strips inherited environment variables. Only the
    # disposable fake program restores its own settings.
    program.write_text(
        f"{shebang}\nimport os\nos.environ.update({environment!r})\n{body}"
    )
    return str(program), capture


def _argv(data, *arguments):
    return (
        "--data-dir",
        str(data),
        *map(str, arguments),
    )


def _cli(data, *arguments, code=0):
    return workflow_fixture.successful(*_argv(data, *arguments), code=code)


@contextmanager
def _process(data, *arguments):
    with subprocess.Popen(
        [sys.executable, "-m", "multi_agent", *_argv(data, *arguments)],
        cwd=workflow_fixture.REPOSITORY,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as child:
        try:
            yield child
        finally:
            if child.poll() is None:
                child.send_signal(signal.SIGINT)
                try:
                    child.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.communicate(timeout=10)


def _result(child, code):
    stdout, stderr = child.communicate(timeout=15)
    assert child.returncode == code, (stdout, stderr)
    return json.loads(stdout)


def _planner(goal, program):
    return (
        "project",
        "plan",
        goal["goal_id"],
        "--planner",
        "claude",
        _FABLE,
        "high",
        "--claude-bin",
        program,
        "--timeout",
        "30",
        "--grace",
        "0.5",
    )


def _turns(capture):
    return [
        row
        for row in workflow_fixture.records(capture)
        if (row["kind"] == "spawn" and row["argv"][:1] == ["-p"])
        or (row["kind"] == "request" and row["message"].get("method") == "turn/start")
    ]


def _limits(data, **providers):
    (data / "provider-limits.json").write_text(json.dumps(providers))


def _snapshot(data, requirements):
    diff = requirements.parent / "review.diff"
    diff.write_text("--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-old\n+new\n")
    return _cli(
        data,
        "freeze",
        "--diff",
        diff,
        "--requirements",
        requirements,
        "--acceptance",
        "Review the offline documentation change.",
    )["snapshot_id"]


def _codex_finished_review(data):
    with ProjectStore.open_readonly(data / "reviews.sqlite3") as store:
        return any(
            seat.config.provider == "codex"
            and seat.attempts
            and seat.attempts[-1].status == "succeeded"
            for key in store.list_reviews()
            for seat in store.load_round(key).seats
        )


def test_provider_limits_share_parallel_reviews_and_web_jobs(
    tmp_path, project, document, capsys
):
    repo, _, data, (requirements, _), _ = project
    goal = project_fixture._confirm(capsys, project)
    snapshot = _snapshot(data, requirements)
    _limits(
        data,
        codex={"max_concurrent": 1, "max_calls_per_day": 1},
        claude={"max_concurrent": 1, "max_calls_per_day": 2},
    )
    alias = tmp_path / "same-data"
    alias.symlink_to(data, target_is_directory=True)
    holder, holder_wire = _fake(
        tmp_path, "holder", "claude", mode="wait", report=document
    )
    queued, queued_wire = _fake(tmp_path, "queued", "claude")
    codex, codex_wire = _fake(tmp_path, "peer", "codex")
    planner, planner_wire = _fake(tmp_path, "service", "claude", report=document)

    # The project planner and stand-alone review have different lock domains.
    # The review starts the Claude seat first, then runs Codex alongside it.
    with _process(data, *_planner(goal, holder)) as active:
        _wait_for(
            lambda: any(
                '"waiting"' in log.read_text()
                for log in (data / "logs/projects").glob("*/*.jsonl")
            )
        )
        assert len(_turns(holder_wire)) == 1
        with _process(
            alias,
            "run",
            snapshot,
            "--seat",
            "claude",
            _FABLE,
            "high",
            "--seat",
            "codex",
            "model-1",
            "high",
            "--claude-bin",
            queued,
            "--codex-bin",
            codex,
            "--timeout",
            "30",
            "--grace",
            "0.5",
        ) as competing:
            _wait_for(
                lambda: any(
                    row.get("argv") == ["auth", "status"]
                    for row in workflow_fixture.records(queued_wire)
                )
            )
            _wait_for(lambda: _codex_finished_review(data))
            assert len(_turns(codex_wire)) == 1
            assert active.poll() is None and competing.poll() is None
            assert not _turns(queued_wire)
            competing.send_signal(signal.SIGINT)
            review = _result(competing, 0)
        seats = {seat["provider"]: seat["attempts"][-1] for seat in review["seats"]}
        assert seats["codex"]["status"] == "succeeded"
        assert seats["claude"]["failure_category"] == "cancelled"
        assert seats["claude"]["session_id"] is None
        assert not _turns(queued_wire)

        active.send_signal(signal.SIGINT)
        cancelled_turn = _result(active, 1)
    assert cancelled_turn["error"]["failure_category"] == "cancelled"
    assert cancelled_turn["evidence"]["processes_stopped"]

    with _serve(repo, data, {"claude": planner, "codex": codex}) as service:
        status, refresh = _refresh(service, "claude")
        assert status == 202, refresh
        refreshed = _wait_for(lambda: _finished(service, refresh["job_id"]))
        assert refreshed["result"]["logged_in"] is True
        assert not _turns(planner_wire)
        job = _job(
            service,
            "project.plan",
            goal_id=goal["goal_id"],
            planner={"provider": "claude", "model": _FABLE, "effort": "high"},
        )
    assert job["result"]["status"] == "succeeded"
    assert job["result"]["evidence"]["usage"] == [
        {
            "raw_type": "result",
            "counts": {
                "input_tokens": 11,
                "output_tokens": 7,
                "client_estimated_cost_usd": 0.04,
            },
        }
    ]
    assert len(_turns(planner_wire)) == 1

    # A new CLI process sees both earlier admitted Claude calls. The cancelled
    # waiter and environment refresh left the last call available for the web.
    refused = _cli(data, *_planner(goal, planner), code=1)
    assert refused["status"] == "failed"
    assert refused["error"]["raw_type"] == "adapter.provider_limits"
    assert refused["error"]["failure_category"] == "rate_limited"
    assert "max_calls_per_day" in refused["error"]["detail"]
    assert refused["evidence"]["reported_model"] is None
    assert refused["evidence"]["usage"] == []
    assert len(_turns(planner_wire)) == 1

    peer_refused = _cli(
        data,
        "run",
        snapshot,
        "--seat",
        "codex",
        "model-1",
        "high",
        "--codex-bin",
        codex,
    )["seats"][0]["attempts"][-1]
    assert peer_refused["failure_category"] == "rate_limited"
    assert "max_calls_per_day" in peer_refused["detail"]
    assert len(_turns(codex_wire)) == 1


def test_invalid_provider_limit_stops_before_model_launch(
    tmp_path, project, document, capsys
):
    _, _, data, _, _ = project
    goal = project_fixture._confirm(capsys, project)
    planner, capture = _fake(tmp_path, "planner", "claude", report=document)
    assert _cli(data, *_planner(goal, planner))["status"] == "succeeded"
    assert len(_turns(capture)) == 1

    for field, value in (("unknown_limit", 1), ("max_calls_per_day", 0)):
        _limits(data, claude={field: value})
        refused = _cli(data, *_planner(goal, planner), code=1)
        assert refused["status"] == "failed"
        assert refused["error"]["raw_type"] == "adapter.provider_limits"
        assert f"claude.{field}" in refused["error"]["detail"]
        assert len(_turns(capture)) == 1
