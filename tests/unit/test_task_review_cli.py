"""Task reviews through the real CLI, with only fixed synthetic reviewer CLIs."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
import test_task_test_cli as test_cli_helpers
import test_task_test_runner as test_runner_helpers

from multi_agent.core.events import thaw_json
from multi_agent.store.task_test_store import TaskTestStore

feature = test_runner_helpers.feature
_PROCESS_POPEN = subprocess.Popen
_REVIEW_TRANSPORT = Path(__file__).parent / "runtime_fakes/fake_cli.py"
_REVIEW_WRAPPER = Path(__file__).parent / "runtime_fakes/task_review_cli.py"


def review_executables(feature):
    """A test-only wrapper delegates wire traffic to the unchanged old fake."""
    capture = feature.base / "review-transport.jsonl"
    executables = {}
    for provider in ("codex", "claude"):
        transport = feature.base / "bin" / f"{provider}-transport.py"
        transport.write_bytes(_REVIEW_TRANSPORT.read_bytes())
        executable = feature.base / "bin" / f"{provider}-review-fake"
        executable.write_text(f"#!{sys.executable}\n" + _REVIEW_WRAPPER.read_text())
        executable.chmod(0o755)
        executable.with_suffix(".json").write_text(
            json.dumps(
                {
                    "mode": "success",
                    "capture": str(capture),
                    "bundles": str(feature.base / "review-bundles.jsonl"),
                    "provider": provider,
                    "transport": str(transport),
                }
            )
        )
        executables[provider] = executable
    return executables


def allow_review_module(monkeypatch):
    previous = subprocess.Popen

    def only_review_or_existing(argv, *args, **kwargs):
        if (
            isinstance(argv, (list, tuple))
            and argv[0] == sys.executable
            and list(argv[1:4]) == ["-m", "multi_agent", "task-review"]
        ):
            assert not kwargs.get("shell", False)
            return _PROCESS_POPEN(argv, *args, **kwargs)
        return previous(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", only_review_or_existing)


def review_process(arguments):
    return subprocess.run(
        [sys.executable, "-m", "multi_agent", "task-review", *arguments],
        cwd=test_runner_helpers.REPOSITORY,
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
    )


def run_arguments(feature, executables, *, dual=True):
    arguments = [
        "run",
        "implementation",
        "--no-auto-rework",
        "--database",
        str(feature.database),
        "--implementation-attempt",
        "1",
        "--test-attempt",
        "1",
        "--repository",
        str(feature.repo),
        "--directory",
        str(feature.base / "owned-review"),
        "--seat",
        "codex",
        "model-1",
        "high",
        "--codex-bin",
        str(executables["codex"]),
        "--claude-bin",
        str(executables["claude"]),
        "--timeout",
        "15",
        "--grace",
        "0.2",
    ]
    if dual:
        arguments.extend(("--seat", "claude", "claude-requested", "high"))
    return arguments


def configure(executable, **changes):
    path = executable.with_suffix(".json")
    settings = json.loads(path.read_text())
    settings.update(changes)
    path.write_text(json.dumps(settings))


def bundles(feature):
    path = feature.base / "review-bundles.jsonl"
    return (
        [json.loads(line) for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )


@pytest.fixture
def reviewed_feature(feature, monkeypatch):
    tested = test_cli_helpers.cli_process(test_cli_helpers.run_arguments(feature))
    assert tested.returncode == 0, (tested.stdout, tested.stderr)
    assert json.loads(tested.stdout)["verification"] == "verified"
    feature.reviewers = review_executables(feature)
    allow_review_module(monkeypatch)
    return feature


def test_task_review_feature_same_candidate_and_cold_show(feature, monkeypatch):
    # This first executes the real T50 CLI and its approved build/test argv.
    # Planning/implementation ownership in the shared fixture is synthetic;
    # reviewer traffic below is the unchanged phase-one fake, not real models.
    tested = test_cli_helpers.cli_process(test_cli_helpers.run_arguments(feature))
    assert tested.returncode == 0, (tested.stdout, tested.stderr)
    tests_view = json.loads(tested.stdout)
    assert tests_view["phase"] == "succeeded"
    assert tests_view["verification"] == "verified"
    before_tests = test_runner_helpers.load(feature)
    before_transport = feature.capture.read_bytes()
    executables = review_executables(feature)
    allow_review_module(monkeypatch)

    reviewed = review_process(run_arguments(feature, executables))
    assert reviewed.returncode == 0, (reviewed.stdout, reviewed.stderr)
    output = json.loads(reviewed.stdout)
    assert output["candidate"] == feature.candidate.delivery.candidate
    assert output["source_current"] is True
    assert output["verification"] == "verified"
    assert output["current_eligible"] is True
    assert output["assessment"]["status"] == "ready"
    assert output["assessment"]["tests"]["executed_by_workbench"] is True
    assert len(output["seats"]) == 2

    with TaskTestStore(feature.database) as store:
        state = store.load_round(output["round_id"])
        payload = thaw_json(store.load_snapshot(state.snapshot_id))
        assert state.snapshot_id == output["snapshot_id"]
        assert state.prompt_template_version == "executed-review-v2"
        assert payload["version"] == 2 and payload["kind"] == "executed_task_review"
        assert payload["source"]["candidate"] == output["candidate"]
        assert all(seat.current_attempt == 1 for seat in state.seats)

    shown = review_process(
        ["show", output["round_id"], "--database", str(feature.database)]
    )
    assert shown.returncode == 0, (shown.stdout, shown.stderr)
    cold = json.loads(shown.stdout)
    assert cold["snapshot_id"] == output["snapshot_id"]
    assert cold["candidate"] == output["candidate"]
    assert cold["assessment"] == output["assessment"]
    assert cold["seats"] == output["seats"]
    assert cold["source_current"] is True and cold["current_eligible"] is True
    assert cold["verification"] == "verified"
    assert test_runner_helpers.load(feature) == before_tests
    assert feature.capture.read_bytes() == before_transport
    captures = bundles(feature)
    inputs = [row["files"]["review-input.json"] for row in captures]
    assert {row["provider"] for row in captures} == {"codex", "claude"}
    assert len(set(inputs)) == 1
    assert all(
        row["files"]["logs/1/stdout.bin"] == test_runner_helpers.STDOUT_BYTES.hex()
        for row in captures
    )


def test_task_review_retry_and_confirmation_keep_frozen_input(reviewed_feature):
    feature = reviewed_feature
    configure(feature.reviewers["codex"], mode="model-error")
    configure(
        feature.reviewers["claude"],
        report={
            "summary": "Synthetic candidate issue",
            "findings": [
                {
                    "id": "candidate-issue",
                    "severity": "P1",
                    "category": "correctness",
                    "side": "candidate",
                    "path": "src/code.py",
                    "line_start": 1,
                    "line_end": 1,
                    "quote": "CANDIDATE = True",
                    "title": "Synthetic blocker",
                    "impact": "The explicit fixture contract is unmet.",
                    "rationale": "A test-only independent opinion tied to this exact line.",
                }
            ],
        },
    )
    first = review_process(run_arguments(feature, feature.reviewers))
    assert first.returncode == 1, (first.stdout, first.stderr)
    initial = json.loads(first.stdout)
    before = bundles(feature)
    configure(feature.reviewers["codex"], mode="success")
    retry = review_process(
        ["retry", initial["round_id"], "--database", str(feature.database)]
    )
    assert retry.returncode == 1, (retry.stdout, retry.stderr)
    retried = json.loads(retry.stdout)
    assert retried["snapshot_id"] == initial["snapshot_id"]
    assert retried["assessment"]["status"] == "needs_rework"
    assert [seat["current_attempt"] for seat in retried["seats"]] == [2, 1]
    after_retry = bundles(feature)
    assert len([row for row in after_retry if row["provider"] == "claude"]) == len(
        [row for row in before if row["provider"] == "claude"]
    )
    confirmed = review_process(
        ["confirm", initial["round_id"], "--database", str(feature.database)]
    )
    assert confirmed.returncode == 1, (confirmed.stdout, confirmed.stderr)
    confirmation = json.loads(confirmed.stdout)
    assert confirmation["snapshot_id"] == initial["snapshot_id"]
    assert confirmation["round_id"] != initial["round_id"]
    assert confirmation["assessment"]["status"] == "needs_rework"
    assert confirmation["prompt_template_version"] == "executed-confirmation-v2"
    assert len({row["files"]["review-input.json"] for row in bundles(feature)}) == 1
    before_repeat = len(bundles(feature))
    duplicate = review_process(
        ["confirm", initial["round_id"], "--database", str(feature.database)]
    )
    assert duplicate.returncode == 2
    assert len(bundles(feature)) == before_repeat


def test_task_review_single_seat_confirm_is_refused(
    reviewed_feature,
):
    feature = reviewed_feature
    reviewed = review_process(run_arguments(feature, feature.reviewers, dual=False))
    assert reviewed.returncode == 0, (reviewed.stdout, reviewed.stderr)
    output = json.loads(reviewed.stdout)
    capture_before = (feature.base / "review-transport.jsonl").read_bytes()
    with TaskTestStore(feature.database) as store:
        rounds_before = store.list_reviews()
    result = review_process(
        ["confirm", output["round_id"], "--database", str(feature.database)]
    )
    assert result.returncode == 2
    assert (feature.base / "review-transport.jsonl").read_bytes() == capture_before
    with TaskTestStore(feature.database) as store:
        assert store.list_reviews() == rounds_before
        assert store.confirmation_for(output["round_id"]) is None


def test_task_review_current_negative_keeps_historical_reports(reviewed_feature):
    from multi_agent.core.task_test_events import TaskTestEnvelope, TaskTestReconciled

    feature = reviewed_feature
    result = review_process(run_arguments(feature, feature.reviewers, dual=False))
    assert result.returncode == 0, (result.stdout, result.stderr)
    original = json.loads(result.stdout)
    with TaskTestStore(feature.database) as store:
        store.apply_task_test(
            "implementation",
            TaskTestEnvelope(
                1,
                1,
                TaskTestReconciled(2, False, "Actual scope stop withdrawn"),
                "withdrawn",
            ),
        )
        history = store.load_task_test_history("implementation", 1)
    shown = review_process(
        ["show", original["round_id"], "--database", str(feature.database)]
    )
    assert shown.returncode == 0, (shown.stdout, shown.stderr)
    view = json.loads(shown.stdout)
    assert view["source_current"] is False
    assert view["verification"] == "unverified" and view["current_eligible"] is False
    assert view["seats"] == original["seats"]
    assert view["assessment"] == original["assessment"]
    with TaskTestStore(feature.database) as store:
        assert store.load_task_test_history("implementation", 1) == history


def test_task_review_execution_requires_shared_executor_lease(reviewed_feature):
    from multi_agent.cli.workspaces import executor_lock

    feature = reviewed_feature
    with executor_lock(feature.repo):
        result = review_process(run_arguments(feature, feature.reviewers))
    assert result.returncode == 2
    assert not (feature.base / "review-transport.jsonl").exists()
    assert not (feature.base / "owned-review").exists()


def test_task_review_postcommit_failure_never_starts_reviewers(
    reviewed_feature, monkeypatch, capsys
):
    import sqlite3

    from multi_agent.cli.main import main
    from multi_agent.store.task_review_store import TaskReviewStore

    feature = reviewed_feature
    failure = sqlite3.OperationalError("postcommit publication error")
    original = TaskReviewStore.create_task_review

    def fail_after_commit(store, *args, **kwargs):
        original(store, *args, **kwargs)
        raise failure

    monkeypatch.setattr(TaskReviewStore, "create_task_review", fail_after_commit)
    assert main(["task-review", *run_arguments(feature, feature.reviewers)]) == 2
    assert "postcommit publication error" in capsys.readouterr().err
    assert not (feature.base / "review-transport.jsonl").exists()
    with TaskReviewStore(feature.database) as store:
        rounds = store.list_reviews()
        assert len(rounds) == 1
        assert not store.load_history(rounds[0])
        assert all(not seat.attempts for seat in store.load_round(rounds[0]).seats)
