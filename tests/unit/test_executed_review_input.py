"""Executed review evidence from actual fake commands and immutable host inputs."""

import asyncio
import copy
import hashlib
import json
import stat
from types import SimpleNamespace

import pytest
import test_task_test_runner as execution

from multi_agent.core import Disposition
from multi_agent.core.events import CancelRequested
from multi_agent.core.reports import assess
from multi_agent.core.task_events import TaskEnvelope
from multi_agent.core.task_execution import task_execution_hash
from multi_agent.core.task_test_events import TaskTestCancelRequested, TaskTestEnvelope

feature = execution.feature


@pytest.fixture
def frozen(feature):
    from multi_agent.cli.task_review_inputs import prepare_task_review_input
    from multi_agent.cli.workspaces import executor_lock
    from multi_agent.store.task_review_store import TaskReviewStore

    state = asyncio.run(execution.execute(feature))
    assert state.phase == "succeeded"
    with (
        TaskReviewStore(feature.database) as store,
        executor_lock(feature.repo) as lease,
    ):
        value = prepare_task_review_input(store, "implementation", 1, 1, lease)
        yield SimpleNamespace(feature=feature, store=store, lease=lease, value=value)


def test_executed_review_freezes_actual_logs_and_version(frozen):
    from multi_agent.cli.review_inputs import decode_review_input
    from multi_agent.cli.snapshot import materialize_review_input
    from multi_agent.cli.task_review_inputs import inspect_task_review_source

    value, feature = frozen.value, frozen.feature
    suite = value.executed_suite
    assert suite.task_input == frozen.store.load_task_input("implementation")
    assert (
        task_execution_hash(suite.task_input)
        == suite.prepared.checkout.input.task_input_hash
    )
    assert value.source.requirements == suite.task_input.requirements
    assert value.source.acceptance == suite.task_input.card.acceptance_criteria
    assert value.source.tests == value.source.required_tests == ()
    assert value.source.baseline == feature.baseline
    assert value.source.candidate == feature.candidate.delivery.candidate
    assert suite.commands[0].stdout.content == execution.STDOUT_BYTES
    assert suite.commands[0].stderr.content == execution.STDERR_BYTES
    assert (
        suite.commands[0].stdout.sha256
        == hashlib.sha256(execution.STDOUT_BYTES).hexdigest()
    )
    assert suite.commands[0].stdout.byte_count == len(execution.STDOUT_BYTES)
    assert decode_review_input(value.to_dict(), expected_id=value.snapshot_id) == value
    with pytest.raises(TypeError):
        suite.commands[0].manifest["qualified"] = False

    # Source logs are no longer the renderer's input once their bytes are frozen.
    log = feature.directory / "logs/command-1/stdout.bin"
    log.chmod(0o600)
    log.write_bytes(b"changed outside the immutable review")
    log.chmod(0o400)
    destination = feature.base / "review-materialization"
    materialize_review_input(value, destination)
    assert (destination / "logs/1/stdout.bin").read_bytes() == execution.STDOUT_BYTES
    assert (destination / "logs/1/stderr.bin").read_bytes() == execution.STDERR_BYTES
    for path in destination.rglob("*"):
        if path.is_file():
            assert not stat.S_IMODE(path.stat().st_mode) & 0o222
            assert not path.is_symlink()
    rendered = (destination / "review-input.json").read_text()
    assert execution.STDOUT_BYTES.hex() not in rendered
    assert "logs/1/stdout.bin" in rendered
    assert suite.task_input.card.goal in rendered
    assert (destination / "diff.patch").read_bytes() == value.source.diff.encode(
        "utf-8"
    )
    with pytest.raises(ValueError):
        inspect_task_review_source(frozen.store, value, frozen.lease)


def test_executed_review_decoder_rejects_version_hash_and_internal_drift(frozen):
    from multi_agent.cli.review_inputs import decode_review_input

    original = frozen.value.to_dict()
    changes = (
        lambda row: row.update(version=3),
        lambda row: row.update(kind="unknown_kind"),
        lambda row: row["source"].update(candidate="a" * 40),
        lambda row: row["executed_suite"]["task_input"].update(requirements="invented"),
        lambda row: row["executed_suite"]["commands"][0]["manifest"].update(sequence=2),
        lambda row: row["executed_suite"]["commands"].pop(),
    )
    for change in changes:
        row = copy.deepcopy(original)
        change(row)
        with pytest.raises((TypeError, ValueError)):
            decode_review_input(row)
    for missing in ("cli_version", "exit_code_provenance"):
        row = copy.deepcopy(original)
        actual = row["executed_suite"]["commands"][0]["manifest"]["actual_result"]
        if missing == "cli_version":
            actual["environment"]["cli_version"] = None
        else:
            actual["exit_code_provenance"] = None
        # Re-anchor the changed outer bytes: positive provenance is independently
        # required, even when this complete new hash is internally consistent.
        with pytest.raises(ValueError):
            decode_review_input(row, expected_id=task_execution_hash(row))
    with pytest.raises(ValueError):
        decode_review_input(original, expected_id="0" * 64)


def test_executed_review_rejects_canonical_budget_without_truncation(frozen):
    from multi_agent.cli.review_inputs import (
        MAX_REVIEW_INPUT_BYTES,
        decode_review_input,
    )

    assert MAX_REVIEW_INPUT_BYTES == 128 * 1024 * 1024
    row = frozen.value.to_dict()
    # Hex alone exceeds the budget by two bytes; all remaining JSON is extra.
    oversized = "00" * (MAX_REVIEW_INPUT_BYTES // 2 + 1)
    row["executed_suite"]["commands"][0]["stdout"]["content"] = {"hex": oversized}
    before = frozen.store.list_reviews()
    with pytest.raises(ValueError, match="(?i)budget|limit|large"):
        decode_review_input(row)
    assert row["executed_suite"]["commands"][0]["stdout"]["content"]["hex"] is oversized
    assert frozen.store.list_reviews() == before


def test_task_review_current_history_only_suffix_preserves_original_checkpoint(frozen):
    from multi_agent.cli.task_review_inputs import inspect_task_review_source

    value = frozen.value
    length, digest = (
        value.executed_suite.test_history_length,
        value.executed_suite.test_history_hash,
    )
    late = TaskTestEnvelope(1, 1, TaskTestCancelRequested(), "unaccepted-late-cancel")
    assert (
        frozen.store.apply_task_test("implementation", late) == Disposition.HISTORY_ONLY
    )
    inspect_task_review_source(frozen.store, value, frozen.lease)
    assert (
        value.executed_suite.test_history_length,
        value.executed_suite.test_history_hash,
    ) == (length, digest)
    assert (
        task_execution_hash(
            frozen.store.load_task_test_history("implementation", 1)[:length]
        )
        == digest
    )
    # An implementation H_ONLY tail still changes the original TaskTestInput CAS.
    assert (
        frozen.store.apply_task(
            "implementation", TaskEnvelope(1, CancelRequested(), "late")
        )
        == Disposition.HISTORY_ONLY
    )
    with pytest.raises(ValueError):
        inspect_task_review_source(frozen.store, value, frozen.lease)


def test_task_review_original_test_history_cannot_be_rewritten(frozen):
    from multi_agent.cli.task_review_inputs import inspect_task_review_source

    value = frozen.value
    selected_before = frozen.store.load_task_test("implementation", 1)
    history = frozen.store.load_task_test_history("implementation", 1)
    row = frozen.store._connection.execute(
        "SELECT envelope FROM task_test_history WHERE task_run_id='implementation' AND implementation_attempt=1 AND sequence=?",
        (len(history),),
    ).fetchone()
    changed = json.loads(row[0])
    changed["occurred_at"] = "rewritten-original-Finished-time"
    with frozen.store._connection:
        frozen.store._connection.execute(
            "UPDATE task_test_history SET envelope=? WHERE task_run_id='implementation' AND implementation_attempt=1 AND sequence=?",
            (json.dumps(changed), len(history)),
        )
    assert frozen.store.load_task_test("implementation", 1) == selected_before
    assert len(frozen.store.load_task_test_history("implementation", 1)) == len(history)
    with pytest.raises(ValueError):
        inspect_task_review_source(frozen.store, value, frozen.lease)


@pytest.mark.parametrize("target", ["stdout.bin", "execution.json", "candidate-ref"])
def test_task_review_freeze_rejects_actual_source_or_archive_tamper(frozen, target):
    from multi_agent.cli.task_review_inputs import prepare_task_review_input

    feature = frozen.feature
    if target == "candidate-ref":
        execution.planning_contexts.git(
            feature.repo, "update-ref", feature.candidate.delivery.ref, feature.baseline
        )
    else:
        path = feature.directory / "logs/command-1" / target
        path.chmod(0o600)
        path.write_bytes(b"tampered actual archive bytes")
        path.chmod(0o400)
    before = frozen.store.list_reviews()
    with pytest.raises(ValueError):
        prepare_task_review_input(frozen.store, "implementation", 1, 1, frozen.lease)
    assert frozen.store.list_reviews() == before


@pytest.mark.parametrize("nonzero", [False, True])
def test_executed_review_named_order_and_nonzero(request, monkeypatch, nonzero):
    from multi_agent.cli.task_review_inputs import prepare_task_review_input
    from multi_agent.cli.workspaces import executor_lock
    from multi_agent.store.task_review_store import TaskReviewStore

    # Add an approved alias before the fixture's actual first Git add/commit.
    # This changes only this new fixture's source registry, never old helpers.
    original_git = execution.planning_contexts.git

    def with_alias(repo, *arguments):
        rules = repo / ".multi-agent/rules.yaml"
        if arguments == ("add", ".") and rules.exists():
            data = json.loads(rules.read_text())
            data["commands"]["build-alias"] = copy.deepcopy(data["commands"]["build"])
            rules.write_text(json.dumps(data))
        return original_git(repo, *arguments)

    monkeypatch.setattr(execution.planning_contexts, "git", with_alias)
    feature = request.getfixturevalue("feature")
    names = ("nonzero", "test") if nonzero else ("build", "build-alias")
    state = asyncio.run(execution.execute(feature, required_commands=names))
    assert state.phase == ("failed" if nonzero else "succeeded")
    with (
        TaskReviewStore(feature.database) as store,
        executor_lock(feature.repo) as lease,
    ):
        value = prepare_task_review_input(store, "implementation", 1, 1, lease)
    commands = value.executed_suite.prepared.checkout.input.commands
    assert tuple(command.name for command in commands) == names
    if not nonzero:
        assert commands[0].argv == commands[1].argv
    assert tuple(
        command.manifest["sequence"] for command in value.executed_suite.commands
    ) == (1, 2)
    result = assess(
        value,
        {"reviewer": {"summary": "No code issues", "findings": []}},
        ("reviewer",),
    )
    assert result["tests"]["executed_by_workbench"] is True
    assert result["tests"]["status"] == ("failed" if nonzero else "passed")
    assert result["status"] == ("needs_rework" if nonzero else "ready")
