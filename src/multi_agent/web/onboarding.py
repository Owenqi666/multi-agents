"""Preview the repository and provider setup required by the selected baseline."""

import shutil
from pathlib import Path
from typing import Any

from multi_agent.cli.snapshot import _resolve_commit
from multi_agent.cli.task_inputs import (
    RULES_PATH,
    MissingProjectRules,
    freeze_project_rules,
)

from .workspace import Workspace, _branches

_TEMPLATE = """# Python unittest example. Replace the command and model for your project.
version: 1
commands:
  unit:
    executable: python3
    args: ["-m", "unittest", "discover", "-s", "tests"]
protected_paths: [".multi-agent/**", "tests/**"]
implementation: []
default_implementation:
  provider: codex
  default_model: gpt-6.2-sol
  default_effort: medium
"""


def _rules_check(repository: Path, baseline: str | None) -> dict[str, Any]:
    result: dict[str, Any] = {"path": RULES_PATH, "commands": [], "detail": None}
    if baseline is None:
        return {**result, "status": "no_commit"}
    try:
        frozen = freeze_project_rules(repository, baseline)
    except MissingProjectRules:
        return {**result, "status": "missing"}
    except (ValueError, TypeError) as error:
        return {**result, "status": "invalid", "detail": str(error)}
    commands = [
        {"name": item.name, "argv": item.argv} for item in frozen.rules.commands
    ]
    return {
        **result,
        "status": "ready" if commands else "no_commands",
        "commands": commands,
    }


def _provider_check(executable: str, check: dict[str, Any] | None) -> dict[str, Any]:
    check = check or {}
    available = shutil.which(executable) is not None
    if not available:
        status = "missing_cli"
    elif check.get("state") in ("queued", "running", "cancelling"):
        status = "checking"
    elif check.get("error") or check.get("state") == "interrupted":
        status = "check_failed"
    elif check.get("logged_in") is True:
        status = "ready"
    elif check.get("logged_in") is False:
        status = "login_required"
    else:
        status = "unknown"
    return {
        "status": status,
        "cli_version": check.get("cli_version"),
        "observed_at": check.get("observed_at"),
        "check_job_id": check.get("job_id"),
        "error": check.get("error"),
    }


def _next_steps(
    rules: str, providers: dict[str, dict[str, Any]]
) -> list[dict[str, str]]:
    rules_step = {
        "no_commit": "commit_baseline",
        "missing": "add_rules",
        "invalid": "fix_rules",
        "no_commands": "register_commands",
    }.get(rules)
    steps = [{"kind": rules_step}] if rules_step else []
    provider_steps = {
        "missing_cli": "install_cli",
        "unknown": "check_provider",
        "check_failed": "check_provider",
        "login_required": "sign_in",
    }
    for provider, check in providers.items():
        if action := provider_steps.get(check["status"]):
            steps.append({"kind": action, "provider": provider})
    # Direct goal confirmation needs a baseline but no rules or advisor.
    if rules != "no_commit":
        steps.append({"kind": "create_goal"})
    return steps


def onboarding(
    workspace: Workspace,
    baseline: str | None,
    checks: dict[str, Any],
    executables: dict[str, str],
) -> dict[str, Any]:
    branch, head = workspace.branch_head()
    selected = (
        _resolve_commit(workspace.repository, baseline)
        if baseline is not None
        else head
    )
    rules = _rules_check(workspace.repository, selected)
    providers = {
        provider: _provider_check(executable, checks.get(provider))
        for provider, executable in executables.items()
    }
    return {
        "repository": str(workspace.repository),
        "read_only": workspace.archive is not None,
        "branch": branch,
        "head": head,
        "branches": _branches(workspace.repository),
        "baseline": selected,
        "rules": rules,
        "rules_template": _TEMPLATE,
        "providers": providers,
        "next_steps": []
        if workspace.archive
        else _next_steps(rules["status"], providers),
    }
