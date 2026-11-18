"""Verify the integration runtime's standalone checkout and live command evidence."""

import asyncio
import sys

import pytest
import test_task_workflow_cli as helpers

from multi_agent.cli.integration_tests import (
    execute_integration_tests,
    inspect_integration_test_result,
)
from multi_agent.cli.task_inputs import freeze_project_rules
from multi_agent.cli.task_test_context import capture_task_test_policy
from multi_agent.cli.workspaces import executor_lock
from multi_agent.core.integration import IntegrationCandidate
from multi_agent.core.integration_testing import decode_integration_test_result
from multi_agent.core.task_execution import task_execution_value
from multi_agent.store.database import ReviewStore

workflow = helpers.workflow


def test_integration_runtime_reopens_actual_binary_logs(workflow):
    repo = workflow.repository
    branch = helpers.git(repo, "branch", "--show-current")
    helpers.git(repo, "switch", "--detach", workflow.baseline)
    (repo / "frontend/status.js").write_bytes(helpers.FIXED_BYTES)
    helpers.git(repo, "commit", "--quiet", "-am", "Fixed integration fixture")
    commit = helpers.git(repo, "rev-parse", "HEAD")
    tree = helpers.git(repo, "rev-parse", "HEAD^{tree}")
    ref = "refs/multi-agent/integrations/synthetic-runtime"
    helpers.git(repo, "update-ref", ref, commit)
    helpers.git(repo, "switch", branch)

    executable = workflow.base / "bin" / "integration-test-runtime"
    executable.write_text(
        f"#!{sys.executable}\n" + (helpers.FAKES / "test_cli.py").read_text()
    )
    executable.chmod(0o755)
    policy = capture_task_test_policy(executable, timeout=15, grace=0.2)
    root = workflow.base / "owned-integration-test"
    rules = freeze_project_rules(repo, workflow.baseline)

    with ReviewStore(workflow.database), executor_lock(repo) as lease:
        arguments = {
            "integration_id": "synthetic-runtime",
            "attempt": 1,
            "lease": lease,
            "database": workflow.database,
            "root": root,
            "candidate": IntegrationCandidate(ref, commit, tree),
            "rules": rules,
            "policy": policy,
        }
        result = asyncio.run(
            execute_integration_tests(**arguments, stop=asyncio.Event())
        )
        assert result.status == "succeeded", result
        reopened = decode_integration_test_result(task_execution_value(result))
        assert [item.command.name for item in reopened.commands] == ["build", "test"]
        build, test = inspect_integration_test_result(reopened, **arguments)
        assert build.manifest["candidate"] == test.manifest["candidate"] == commit
        assert test.stdout.content == helpers.FAILED_STDOUT
        assert test.stderr.content == helpers.FAILED_STDERR
        assert not (repo / "artifact.bin").exists()
        assert helpers.git(repo, "rev-parse", "HEAD") == workflow.baseline

        log = root / "logs/command-2/stdout.bin"
        log.chmod(0o600)
        original = log.read_bytes()
        log.write_bytes(b"x" + original[1:])
        log.chmod(0o400)
        with pytest.raises(ValueError, match="bytes differ from the actual receipt"):
            inspect_integration_test_result(reopened, **arguments)
