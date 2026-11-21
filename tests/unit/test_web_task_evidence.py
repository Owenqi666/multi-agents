"""Only the selected attempt's sealed log can contribute runtime reroutes."""

import hashlib
import json
from dataclasses import replace

import pytest
import test_task_workflow_cli as helpers
from test_web_project_flow import _get, _job, _request
from test_web_project_resources import _repository
from test_web_workspace import _serve

from multi_agent.cli.task_inputs import freeze_project_rules
from multi_agent.core.planning import PlanningInput, PlanningRequest
from multi_agent.store.project_store import ProjectStore
from multi_agent.web.resources import _task_attempt_evidence
from multi_agent.web.workspace import Workspace

workflow = helpers.workflow


def test_standalone_dispatch_rejects_unverified_and_foreign_plans(workflow, tmp_path):
    foreign, _, _ = _repository(tmp_path / "foreign")
    frozen = freeze_project_rules(workflow.repository, workflow.baseline)
    planning = PlanningInput(
        "Prepare one implementation.",
        frozen,
        PlanningRequest("claude", "claude-opus-5-5", "high"),
        PlanningRequest("codex", "gpt-6.2-sol", "high"),
    )
    with ProjectStore(workflow.base / "reviews.sqlite3") as store:
        store.create_planning("local", planning, "then")
        store.create_planning(
            "foreign",
            replace(
                planning, frozen_rules=replace(frozen, repository=str(foreign / ".git"))
            ),
            "then",
        )

    programs = {provider: str(path) for provider, path in workflow.executables.items()}
    with _serve(workflow.repository, workflow.base, programs) as service:
        basic = _get(service, "resources/plan/local")["data"]
        assert "dispatch_configurable" not in basic
        local = _get(service, "resources/plan/local?implementation=1")["data"]
        assert local["dispatch_configurable"] is False
        assert (
            _job(service, "task.run", exit_code=1, planning_run_id="local")["result"][
                "task_run_id"
            ]
            is None
        )
        assert (
            _request(service, "GET", "/api/resources/plan/foreign?implementation=1")[0]
            == 404
        )
        assert (
            _request(
                service,
                "POST",
                "/api/jobs",
                {"action": "task.run", "args": {"planning_run_id": "foreign"}},
            )[0]
            == 404
        )
        assert not workflow.capture.exists()


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _transcript():
    def send(identifier, method, params):
        return {
            "kind": "send",
            "proc": "codex",
            "message": {"id": identifier, "method": method, "params": params},
        }

    def recv(message):
        return {"kind": "recv", "proc": "codex", "stream": "stdout", "message": message}

    rows = (
        send(2, "thread/start", {}),
        recv({"id": 2, "result": {"thread": {"id": "session"}}}),
        send(3, "turn/start", {"threadId": "session"}),
        recv({"id": 3, "result": {"turn": {"id": "turn"}}}),
        recv(
            {
                "method": "model/rerouted",
                "params": {
                    "threadId": "session",
                    "turnId": "turn",
                    "fromModel": "original",
                    "toModel": "replacement",
                    "reason": "highRiskCyberActivity",
                },
            }
        ),
    )
    return ("\n".join(json.dumps(row) for row in rows) + "\n").encode()


def _attempt(workspace, *, number, manifest_number):
    root = workspace.data_dir / f"attempt-{number}"
    directory = root / "logs"
    directory.mkdir(parents=True)
    log = directory / "codex-turn.jsonl"
    raw = _transcript()
    log.write_bytes(raw)
    info = log.stat()
    context = {"path": str(root / "context")}
    request = {
        "decision": {"choice": {"provider": "codex"}},
        "worktree": str(root / "workspace"),
    }
    archive = {
        "version": 1,
        "binding": {
            "run_id": "task",
            "attempt": manifest_number,
            "context": context,
            "requested": request,
            "logs": {"path": str(directory)},
            "owner_id": "owner",
        },
        "receipt": {
            "owner_id": "owner",
            "logs": [
                {
                    "path": str(log),
                    "identity": [info.st_dev, info.st_ino],
                    "byte_count": len(raw),
                    "sha256": _digest(raw),
                    "healthy": True,
                    "closed": True,
                    "sealed": True,
                }
            ],
        },
    }
    manifest = directory / "execution.json"
    sealed = json.dumps(archive).encode()
    manifest.write_bytes(sealed)
    return (
        log,
        manifest,
        {
            "request": request,
            "runtime": {
                "number": number,
                "session_id": "session",
                "reported_model": None,
            },
            "events": [{"kind": "task_execution_prepared", "context": context}],
            "reconciliation": {
                "process_stopped": True,
                "log_path": str(manifest),
                "log_sha256": _digest(sealed),
            },
        },
    )


@pytest.mark.parametrize("modified", ["manifest", "log", "session", "receipt", "none"])
def test_reroutes_require_the_task_attempt_anchor(tmp_path, modified):
    workspace = Workspace(tmp_path / "repo", tmp_path / "data")
    log, manifest, first = _attempt(workspace, number=1, manifest_number=1)
    _, _, second = _attempt(workspace, number=2, manifest_number=1)
    if modified == "manifest":
        manifest.write_bytes(manifest.read_bytes() + b" ")
    if modified == "log":
        log.write_bytes(log.read_bytes().replace(b"original", b"different"))
    if modified == "session":
        first["runtime"]["session_id"] = "unrelated-session"
    if modified == "receipt":
        original = log.read_bytes()
        log.rename(log.with_suffix(".old"))
        log.write_bytes(original)
    task = {"task_run_id": "task", "attempts": [first, second]}

    _task_attempt_evidence(workspace, task)

    expected = [
        {
            "source": "model/rerouted",
            "from_model": "original",
            "to_model": "replacement",
            "reason": "highRiskCyberActivity",
        }
    ]
    assert first["runtime_reroutes"] == (expected if modified == "none" else None)
    assert first["runtime"]["reported_model"] is None
    assert second["runtime_reroutes"] is None


def test_attempt_usage_keeps_latest_report_and_zero_estimate(tmp_path):
    workspace = Workspace(tmp_path / "repo", tmp_path / "data")
    first = {"runtime": {}, "events": []}
    second = {
        "runtime": {},
        "events": [
            {
                "kind": "usage",
                "raw_type": "thread/tokenUsage/updated",
                "counts": {"total.totalTokens": 4},
            },
            {
                "kind": "usage",
                "raw_type": "thread/tokenUsage/updated",
                "counts": {"total.totalTokens": 8, "last.totalTokens": 4},
            },
        ],
    }
    third = {
        "runtime": {},
        "events": [
            {
                "kind": "usage",
                "raw_type": "result",
                "counts": {"input_tokens": 10, "client_estimated_cost_usd": 0},
            },
        ],
    }

    _task_attempt_evidence(
        workspace, {"task_run_id": "task", "attempts": [first, second, third]}
    )

    assert first["usage_evidence"] is None
    assert second["usage_evidence"] == {
        "source": "thread/tokenUsage/updated",
        "counts": {"total.totalTokens": 8, "last.totalTokens": 4},
    }
    assert third["usage_evidence"] == {
        "source": "result",
        "counts": {"input_tokens": 10, "client_estimated_cost_usd": 0},
    }
