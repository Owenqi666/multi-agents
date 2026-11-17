"""Real frozen source boundaries; upstream setup is explicitly synthetic."""

import ast
import asyncio
import json
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_task_execution as implementations
import test_task_prompt as prompt_helpers
import test_task_review_cli as reviews

from multi_agent.cli import runner, task_context, task_prompt, task_rework
from multi_agent.cli.review_inputs import load_review_input
from multi_agent.cli.task_runner import execute_task_attempt
from multi_agent.cli.workspaces import executor_lock
from multi_agent.core.events import CancelRequested, thaw_json
from multi_agent.core.review_input import ExecutedLog
from multi_agent.core.task_events import TaskEnvelope, TaskReworkStarted
from multi_agent.core.task_execution import prepared_for_task, task_execution_json
from multi_agent.store.task_review_store import TaskReviewStore

feature = reviews.feature
reviewed_feature = reviews.reviewed_feature
bundle = prompt_helpers.bundle


def report():
    return {
        "summary": "Located synthetic blocker",
        "findings": [
            {
                "id": "located-blocker",
                "severity": "P1",
                "category": "correctness",
                "side": "candidate",
                "path": "src/code.py",
                "line_start": 1,
                "line_end": 1,
                "quote": "CANDIDATE = True",
                "title": "Synthetic blocker",
                "impact": "The fixture contract is unmet",
                "rationale": "An opinion tied to the exact current source line",
            }
        ],
    }


@pytest.fixture
def reviewed(reviewed_feature):
    feature = reviewed_feature
    reviews.configure(feature.reviewers["codex"], report=report())
    result = reviews.review_process(
        reviews.run_arguments(feature, feature.reviewers, dual=False)
    )
    assert result.returncode == 1, (result.stdout, result.stderr)
    view = json.loads(result.stdout)
    assert view["assessment"]["status"] == "needs_rework" and view["source_current"]
    return SimpleNamespace(feature=feature, round_id=view["round_id"], view=view)


def source(reviewed, store, lease):
    metadata = runner.metadata_for(store, reviewed.round_id)
    with runner.execution_lock(Path(metadata["directory"])):
        return task_rework.prepare_task_rework_source(store, reviewed.round_id, lease)


def launch(reviewed, directory="owned-rework"):
    feature = reviewed.feature
    return task_context.prepare_task_rework_launch(
        feature.database,
        "implementation",
        "plan",
        reviewed.round_id,
        repository=feature.repo,
        directory=feature.base / directory,
    )


def test_frontend_rework_uses_claude_and_cold_context_keeps_frozen_source(
    reviewed, monkeypatch
):
    feature = reviewed.feature
    with TaskReviewStore(feature.database) as store:
        original = store.load_task("implementation")
        original_input = store.load_task_input("implementation")
    with launch(reviewed) as handle:
        prepared = handle.claim()
        assert handle.attempt == 2
        assert (
            prepared.workspace.start_commit
            == original.current_candidate.delivery.candidate
        )
        assert prepared.workspace.task_baseline == original.baseline_commit
        assert prepared.policy.provider == "claude"
        assert prepared.policy == prepared_for_task(original.current_attempt).policy
        assert handle.store.load_task_input("implementation") == original_input
        assert (
            Path(prepared.workspace.root, "src/code.py").read_bytes()
            == b"CANDIDATE = True\r\n"
        )
        assert isinstance(
            handle.store.load_task("implementation").current_attempt.request,
            TaskReworkStarted,
        )
        with pytest.raises(ValueError):
            handle.claim()

        def no_authorize(*_args, **_kwargs):
            pytest.fail(
                "cold inspection must not re-authorize the now-historical candidate"
            )

        monkeypatch.setattr(task_rework, "prepare_task_rework_source", no_authorize)
        assert (
            task_context.inspect_task_context(
                handle.store, "implementation", 2, handle.lease
            )
            == prepared
        )
    with pytest.raises(ValueError):
        handle.claim()


@pytest.mark.parametrize(
    "case",
    [
        "test-only",
        "disputed",
        "unverified",
        "outside",
        "protected",
        "unsafe",
        "single-verified",
    ],
)
def test_incomplete_or_disputed_review_requires_user(case):
    value = implementations.inputs()
    value = replace(
        value,
        frozen_rules=replace(
            value.frozen_rules,
            rules=replace(
                value.frozen_rules.rules, protected_paths=("src/protected.py",)
            ),
        ),
    )
    finding = {
        **report()["findings"][0],
        "path": "src/code.py",
        "reference_status": "verified",
    }
    assessment = {
        "status": "needs_rework",
        "reports": {"single": {"findings": [finding]}},
    }
    if case == "test-only":
        assessment["reports"]["single"]["findings"] = []
    elif case == "disputed":
        assessment["status"] = "user_decision"
    elif case == "unverified":
        finding["reference_status"] = "unverified"
    elif case in {"outside", "protected", "unsafe"}:
        finding["path"] = {
            "outside": "other/file.py",
            "protected": "src/protected.py",
            "unsafe": "../src/code.py",
        }[case]
    if case == "single-verified":
        task_rework._actionable(assessment, value)
    else:
        with pytest.raises(task_rework.ReworkRefused) as caught:
            task_rework._actionable(assessment, value)
        assert caught.value.kind == "user_decision"


def test_task_rework_source_does_not_refresh_checkpoint_after_inspection(
    reviewed, monkeypatch
):
    feature = reviewed.feature
    original = task_rework.inspect_task_review_source
    with (
        TaskReviewStore(feature.database) as store,
        executor_lock(feature.repo) as lease,
    ):
        before = store.load_task_history("implementation")

        def inspect_then_change(*args):
            original(*args)
            store.apply_task(
                "implementation",
                TaskEnvelope(1, CancelRequested(), "late-after-sample"),
            )

        monkeypatch.setattr(
            task_rework, "inspect_task_review_source", inspect_then_change
        )
        with pytest.raises(ValueError, match="changed"):
            source(reviewed, store, lease)
        assert store.load_task_history("implementation")[: len(before)] == before
        assert len(store.load_task("implementation").attempts) == 1


def test_task_rework_inline_handoff_keeps_full_binary_and_legacy_bytes(reviewed):
    feature = reviewed.feature
    with (
        TaskReviewStore(feature.database) as store,
        executor_lock(feature.repo) as lease,
    ):
        selected = source(reviewed, store, lease)
        value = load_review_input(store, selected.review_snapshot_id)
        original = store.load_task_input("implementation")
        attempt = store.load_task("implementation").current_attempt
        prepared = prepared_for_task(attempt)
        arguments = {
            "provenance": store.load_task_provenance("implementation"),
            "decision": attempt.request.decision,
            "dispatch_policy": prepared.dispatch_policy,
            "workspace": prepared.workspace,
            "initial_workspace_digest": prepared.initial_workspace_digest,
        }
        legacy = task_prompt.task_prompt(original, **arguments)
        # The committed pre-T52 AST supplies literal instructions only. No old
        # executable code or current renderer is used to manufacture an oracle.
        old = subprocess.run(
            [
                "git",
                "show",
                "e694d99a793cb079ac9b7fc1dc9aaef1bc78c64b:src/multi_agent/cli/task_prompt.py",
            ],
            cwd=reviews.test_runner_helpers.REPOSITORY,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        tree = ast.parse(old)
        joined = next(
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "instructions"
                for target in node.targets
            )
        )
        instructions = "".join(
            node.value
            if isinstance(node, ast.Constant)
            else "implementation-managed-v1"
            for node in joined.values
        )
        payload = {
            "task_hash": original.card.task_hash,
            "task_input": original,
            "planning": {
                "plan_hash": arguments["provenance"].plan_hash,
                "dependencies": arguments["provenance"].planning_input.dependencies,
                "decisions": arguments["provenance"].planning_input.decisions,
            },
            "dispatch_policy": prepared.dispatch_policy,
            "dispatch_decision": attempt.request.decision,
            "workspace": prepared.workspace,
            "initial_workspace_digest": prepared.initial_workspace_digest,
            "output_schema": prompt_helpers.EXPECTED_SCHEMA,
        }
        assert (
            legacy.encode()
            == (instructions + task_execution_json(payload) + "\n").encode()
        )
        # The real source has passing commands; this renderer-only value adds a
        # trusted failed-result/log fixture to check complete handoff semantics.
        command = value.executed_suite.commands[0]
        manifest = thaw_json(command.manifest)
        manifest["actual_result"]["exit_code"] = 7
        changed = replace(
            command,
            manifest=manifest,
            stdout=ExecutedLog(content=b"\x00\xff\r\nwhole-output"),
        )
        value = replace(
            value,
            executed_suite=replace(
                value.executed_suite,
                commands=(changed, *value.executed_suite.commands[1:]),
            ),
        )
        selected = replace(selected, review_snapshot_id=value.snapshot_id)
        text = task_prompt.task_prompt(
            original,
            **(
                arguments
                | {
                    "workspace": replace(
                        prepared.workspace, start_commit=selected.candidate_commit
                    )
                }
            ),
            rework_source=selected,
            rework_input=value,
        )
        encoded = json.loads(text.split(prompt_helpers.MARKER, 1)[1])
        handoff = encoded["rework"]
        assert handoff["diff"] == value.source.diff
        assert (
            handoff["source"]["assessment"]["reports"]["reviewer-1"]["findings"][0][
                "id"
            ]
            == "located-blocker"
        )
        assert handoff["failed_commands"][0]["stdout"] == {
            "hex": b"\x00\xff\r\nwhole-output".hex()
        }
        assert handoff["failed_commands"][0]["command"]["name"] == "build"
        assert "host has not proved" in text


@pytest.mark.parametrize("kind", ["prompt", "manifest"])
def test_task_rework_size_rejection_precedes_reservation(reviewed, monkeypatch, kind):
    feature = reviewed.feature
    original = task_context.task_prompt

    def oversized(*args, **kwargs):
        if kind == "prompt":
            return original(*args, **kwargs) + "x" * (8 * 1024 * 1024)
        # Within the prompt byte limit; escaped controls make the actual
        # serialized context manifest exceed its independent byte limit.
        return original(*args, **kwargs) + "\x01" * (2 * 1024 * 1024)

    monkeypatch.setattr(task_context, "task_prompt", oversized)
    with TaskReviewStore(feature.database) as store:
        before = store.load_task_history("implementation")
    with pytest.raises(ValueError), launch(reviewed):
        pytest.fail("oversized preparation must not yield a launch")
    with TaskReviewStore(feature.database) as store:
        assert store.load_task_history("implementation") == before
        assert not any(
            isinstance(item.request, TaskReworkStarted)
            for item in store.load_task("implementation").attempts
        )


def test_task_rework_postcommit_launch_failure_retains_consumption(
    reviewed, monkeypatch
):
    feature = reviewed.feature
    original = TaskReviewStore.reserve_task_rework_execution
    failure = sqlite3.OperationalError(
        "actual reservation committed then caller failed"
    )

    def after_commit(store, *args):
        original(store, *args)
        raise failure

    monkeypatch.setattr(TaskReviewStore, "reserve_task_rework_execution", after_commit)
    with pytest.raises(sqlite3.OperationalError) as caught, launch(reviewed):
        pytest.fail("postcommit failure cannot yield a live capability")
    assert caught.value is failure
    with TaskReviewStore(feature.database) as store:
        current = store.load_task("implementation").current_attempt
        assert isinstance(current.request, TaskReworkStarted)
        assert len(current.events) == 2 and current.runtime.exit_code is None
    assert any("retained" in note for note in caught.value.__notes__)


@pytest.mark.parametrize("argument", ["policy", "dispatch_policy", "unavailable"])
def test_task_rework_library_rejects_policy_override(reviewed, argument):
    feature = reviewed.feature
    with TaskReviewStore(feature.database) as store:
        before = store.load_task_history("implementation")
        prepared = prepared_for_task(store.load_task("implementation").current_attempt)
    options = {
        "policy": prepared.policy,
        "dispatch_policy": prepared.dispatch_policy,
        "unavailable": frozenset({("claude-code", "another-model")}),
    }
    with pytest.raises(ValueError):
        asyncio.run(
            execute_task_attempt(
                feature.database,
                "implementation",
                "plan",
                repository=feature.repo,
                directory=feature.base / "override",
                rework_from=reviewed.round_id,
                **{argument: options[argument]},
            )
        )
    with TaskReviewStore(feature.database) as store:
        assert store.load_task_history("implementation") == before
