"""Check the HTTP action boundary against the real product argument parser."""

import json
import stat
import sys

import pytest

from multi_agent.cli.main import parser
from multi_agent.web import actions
from multi_agent.web.workspace import Workspace

ASTRA = {"provider": "codex", "model": "gpt-6.2-astra", "effort": "low"}
FABLE = {"provider": "claude", "model": "claude-fable-5-2", "effort": "high"}


@pytest.fixture
def action_context(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path / "repo", tmp_path / "data")
    directory = workspace.data_dir / "web-jobs" / "job-id" / "input"
    directory.mkdir(parents=True)
    owned = []
    monkeypatch.setattr(
        actions,
        "require_owned",
        lambda _, kind, identifier: owned.append((kind, identifier)),
    )

    def prepare(action, **fields):
        return actions.prepare_action(
            action,
            fields,
            workspace=workspace,
            executables={"codex": "/trusted/codex", "claude": "/trusted/claude"},
            directory=directory,
            tool_directories=("/trusted/tools",),
            runtime_read_paths=("/trusted/read",),
        )

    return workspace, directory, owned, prepare


def test_project_consult_uses_private_exact_text_and_ordered_advisors(action_context):
    workspace, directory, owned, prepare = action_context
    original = "Plan cash flow.\r\nNew line.\n"
    argv = prepare(
        "project.consult",
        baseline="refs/heads/main",
        original_goal=original,
        advisors=[FABLE, ASTRA],
        project_id="project-known",
    )
    assert argv[:4] == [sys.executable, "-P", "-m", "multi_agent"]
    parsed = parser().parse_args(argv[4:])
    assert parsed.data_dir == workspace.data_dir
    assert parsed.structured_errors is True
    assert parsed.repo == workspace.repository
    assert parsed.advisor == [
        ["claude", "claude-fable-5-2", "high"],
        ["codex", "gpt-6.2-astra", "low"],
    ]
    assert parsed.original_goal.parent == directory
    assert parsed.original_goal.read_bytes() == original.encode()
    assert stat.S_IMODE(parsed.original_goal.stat().st_mode) == 0o600
    assert owned == [("project", "project-known")]


@pytest.mark.parametrize(
    "invalid_args",
    [
        {"advisors": [FABLE, FABLE]},
        {"advisors": [{**ASTRA, "model": "gpt-6.2-luna"}]},
        {"advisors": [{**FABLE, "effort": "ultra"}]},
        {"advisors": [{**ASTRA, "executable": "/tmp/anything"}]},
        {"baseline": "--codex-bin"},
        {"original_goal": None},
        {"repository": "/tmp/anything"},
    ],
)
def test_rejected_consultation_never_writes_private_inputs(
    action_context, invalid_args
):
    _, directory, _, prepare = action_context
    fields = {"baseline": "HEAD", "original_goal": "A goal.", "advisors": [ASTRA]}
    with pytest.raises(ValueError):
        prepare("project.consult", **(fields | invalid_args))
    assert list(directory.iterdir()) == []


def test_task_test_uses_only_server_configured_paths(action_context):
    workspace, _, owned, prepare = action_context
    argv = prepare(
        "task.test",
        task_run_id="task-known",
        implementation_attempt=2,
        commands=["test", "lint"],
        declaration_source="--command ignored as code",
    )
    parsed = parser().parse_args(argv[4:])
    assert parsed.database == workspace.data_dir / "reviews.sqlite3"
    assert parsed.directory == workspace.data_dir / "web-run-job-id"
    assert not parsed.directory.exists()
    assert parsed.required_commands == ["test", "lint"]
    assert parsed.declaration_source == "--command ignored as code"
    assert parsed.tool_directory == ["/trusted/tools"]
    assert parsed.runtime_read_path == ["/trusted/read"]
    assert parsed.codex_bin == "/trusted/codex"
    assert owned == [("task", "task-known")]
    with pytest.raises(ValueError, match="positive integer"):
        prepare(
            "task.test",
            task_run_id="task-known",
            implementation_attempt=True,
            commands=["test"],
            declaration_source="PM",
        )


def test_stage_integration_defers_target_and_refuses_unready_stage(
    action_context, monkeypatch
):
    _, _, owned, prepare = action_context
    stage = {"mode": "delegate", "can_integrate": False}
    monkeypatch.setattr(actions, "read_resource", lambda *_: {"data": stage})
    with pytest.raises(actions.ActionConflict):
        prepare("project.stage.integrate", stage_run_id="stage-known")
    stage["can_integrate"] = True
    argv = prepare("project.stage.integrate", stage_run_id="stage-known")
    parsed = parser().parse_args(argv[4:])
    assert parsed.stage_run_id == "stage-known"
    assert parsed.target is None
    assert owned == [("stage", "stage-known")] * 2


def test_frozen_task_retry_does_not_override_policy(action_context):
    _, _, owned, prepare = action_context
    argv = prepare("task.retry", planning_run_id="plan-known")
    parsed = parser().parse_args(argv[4:])
    assert parsed.retry
    assert parsed.codex_bin is None
    assert parsed.claude_bin is None
    assert owned == [("plan", "plan-known")]
    with pytest.raises(ValueError, match="unexpected"):
        prepare("task.retry", planning_run_id="plan-known", implementation=ASTRA)
    with pytest.raises(ValueError, match="identifier"):
        prepare("task.retry", planning_run_id="--help")


def test_plan_create_passes_roles_and_private_frozen_inputs(action_context):
    workspace, directory, owned, prepare = action_context
    baseline = "a" * 40
    requirements = "保留精确需求。\r\n第二行\n"
    declarations = {
        "dependencies": [
            {
                "task_id": "T-0003",
                "baseline_commit": baseline,
                "source": "user_declared",
                "detail": "在指定提交已验收",
            }
        ],
        "decisions": [
            {
                "question": "选哪一种？",
                "resolution": "第一种",
                "source": "user_declared",
                "detail": "用户确认",
            }
        ],
    }
    draft = {key: FABLE[key] for key in ("model", "effort")}
    review = {key: ASTRA[key] for key in ("model", "effort")}
    argv = prepare(
        "plan.create",
        baseline=baseline,
        requirements=requirements,
        draft=draft,
        plan_review=review,
        declarations=declarations,
    )
    parsed = parser().parse_args(argv[4:])
    assert (parsed.task_operation, parsed.baseline, parsed.retry) == (
        "plan",
        baseline,
        None,
    )
    assert parsed.repository == workspace.repository
    assert parsed.directory == workspace.data_dir / "web-run-job-id"
    assert parsed.database == workspace.data_dir / "reviews.sqlite3"
    assert (parsed.codex_bin, parsed.claude_bin) == (
        "/trusted/codex",
        "/trusted/claude",
    )
    assert parsed.draft == list(draft.values())
    assert parsed.plan_review == list(review.values())
    assert parsed.requirements.read_bytes() == requirements.encode()
    assert json.loads(parsed.declarations.read_text()) == declarations
    for path in (parsed.requirements, parsed.declarations):
        assert path.parent == directory
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert owned == []


def test_plan_retry_is_scoped_and_passes_only_the_same_frozen_id(
    action_context, monkeypatch
):
    workspace, directory, _, prepare = action_context
    checked = []

    def read(workspace_arg, kind, identifier, query):
        checked.append((workspace_arg, kind, identifier, query))
        if identifier == "foreign-plan":
            raise KeyError(identifier)
        return {"data": {"retry_configurable": identifier == "owned-plan"}}

    monkeypatch.setattr(actions, "read_resource", read)
    argv = prepare("plan.retry", planning_run_id="owned-plan")
    parsed = parser().parse_args(argv[4:])
    assert parsed.retry == "owned-plan"
    assert parsed.database == workspace.data_dir / "reviews.sqlite3"
    assert all(
        getattr(parsed, field) is None
        for field in (
            "repository",
            "directory",
            "baseline",
            "requirements",
            "draft",
            "plan_review",
            "declarations",
            "codex_bin",
            "claude_bin",
        )
    )
    with pytest.raises(KeyError, match="foreign-plan"):
        prepare("plan.retry", planning_run_id="foreign-plan")
    with pytest.raises(actions.ActionConflict, match="cannot be retried"):
        prepare("plan.retry", planning_run_id="stage-plan")
    with pytest.raises(ValueError, match="unexpected"):
        prepare("plan.retry", planning_run_id="owned-plan", requirements="new input")
    assert checked == [
        (workspace, "plan", identifier, {"implementation": ["1"]})
        for identifier in ("owned-plan", "foreign-plan", "stage-plan")
    ]
    assert not list(directory.iterdir())
