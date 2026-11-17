"""Trusted synthetic rework observations and real SQLite CAS, never execution."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from threading import Barrier
from types import SimpleNamespace

import pytest
import test_task_candidate as candidates
import test_task_execution as implementations
import test_task_tests as tests

from multi_agent.core import Disposition, RoundState, SeatConfig, SeatState, apply
from multi_agent.core.events import (
    AttemptStarted,
    CancelRequested,
    Completed,
    Envelope,
    Error,
    ProcessExited,
    thaw_json,
)
from multi_agent.core.review_input import (
    ExecutedCommand,
    ExecutedLog,
    ExecutedReviewInput,
    ExecutedSuite,
)
from multi_agent.core.snapshot import FrozenSnapshot
from multi_agent.core.task_events import TaskEnvelope, TaskReconciled, TaskReworkStarted
from multi_agent.core.task_execution import (
    prepared_for_task,
    reserve_task_execution,
    task_execution_hash,
    task_execution_value,
)
from multi_agent.core.task_rework import TaskReworkSource
from multi_agent.core.task_test_events import (
    TaskTestAttemptFinished,
    TaskTestAttemptStarted,
    TaskTestCancelRequested,
    TaskTestCommandExited,
    TaskTestCommandReported,
    TaskTestCommandStarted,
    TaskTestEnvelope,
    TaskTestExecutionPrepared,
    TaskTestReconciled,
)
from multi_agent.core.task_test_execution import (
    TaskTestCheckout,
    TaskTestContext,
    TaskTestPolicy,
    TaskTestScratch,
)
from multi_agent.core.transitions import apply_task
from multi_agent.store.task_codec import (
    decode_task_attempt,
    decode_task_envelope,
    encode_task_attempt,
    encode_task_envelope,
    encode_task_input,
)
from multi_agent.store.task_review_store import TaskReviewStore


def source_for(state, value, history=(), **changes):
    return replace(
        TaskReworkSource(
            task_run_id="run",
            implementation_attempt=1,
            task_input_hash=task_execution_hash(value),
            candidate_commit=state.current_candidate.delivery.candidate,
            candidate_hash=task_execution_hash(state.current_candidate),
            task_history_length=len(history),
            task_history_hash=task_execution_hash(history),
            test_attempt=1,
            test_history_length=0,
            test_history_hash=task_execution_hash(()),
            review_round_id="review",
            review_snapshot_id="a" * 64,
            review_checkpoint_hash="b" * 64,
            assessment={
                "status": "needs_rework",
                "reports": {"gpt": {"findings": [{"id": "synthetic-blocker"}]}},
            },
        ),
        **changes,
    )


def pair(state, value, source, number=2):
    prior = prepared_for_task(state.current_attempt)
    started, prepared = implementations.reservation(
        number, value=value, policy=prior.policy, dispatch=prior.dispatch_policy
    )
    original = started.event
    request = TaskReworkStarted(
        original.decision,
        original.worktree,
        "rework-managed-v1",
        original.started_at,
        managed=True,
        source=source,
    )
    prepared = replace(
        prepared,
        event=replace(
            prepared.event,
            workspace=replace(
                prepared.event.workspace, start_commit=source.candidate_commit
            ),
        ),
    )
    return replace(started, event=request), prepared


def prepared_test(value, *, implementation_attempt=1):
    return TaskTestExecutionPrepared(
        TaskTestCheckout(
            input=value,
            repository=tests.task_input().frozen_rules.repository,
            database_path="/synthetic/database",
            database_identity=(1, 1),
            database_parent_identity=(1, 2),
            root=f"/synthetic/test-{implementation_attempt}",
            root_identity=(1, 300 + implementation_attempt),
            git_directory=f"/synthetic/test-{implementation_attempt}/.git",
            git_identity=(1, 400 + implementation_attempt),
            owner_token=f"test-owner-{implementation_attempt}",
            object_format="sha1",
            initial_digest=value.candidate.delivery.digest,
            controls=(),
            objects=(
                ("commit", value.candidate.delivery.candidate),
                ("tree", value.candidate.delivery.tree),
            ),
            observed_test_attempt=None,
            test_history_length=0,
            test_history_hash=task_execution_hash(()),
        ),
        TaskTestContext(
            path="/synthetic/test-context",
            identity=(1, 500),
            token="test-context",
            manifest_sha256="e" * 64,
        ),
        TaskTestPolicy(
            executable="/synthetic/codex",
            executable_identity=(1, 600),
            executable_sha256="f" * 64,
            timeout=30,
            grace=1,
            output_bytes_limit=1024,
        ),
        TaskTestScratch(
            path="/synthetic/test-scratch",
            identity=(1, 700),
            home_identity=(1, 701),
            tmp_identity=(1, 702),
            cache_identity=(1, 703),
        ),
    )


def synthetic(store):
    """Populate trusted host facts explicitly; no files/runtime evidence claimed."""
    state, value, history = tests.implementation()
    store.create_task(
        "run", value.card, value.frozen_rules, value.requirements, value.created_at
    )
    store.reserve_task_attempt(
        "run",
        history[0][0],
        history[1][0],
        expected_history_length=0,
        expected_history_hash=task_execution_hash(()),
    )
    for envelope, disposition in history[2:-1]:
        assert store.apply_task("run", envelope) == disposition
    store.record_task_candidate(
        "run",
        history[-1][0],
        expected_history_length=len(history) - 1,
        expected_history_hash=task_execution_hash(history[:-1]),
    )
    declared = tests.declared_input(state, value, history)
    prepared = prepared_test(declared)
    store.reserve_task_test_execution(
        "run",
        TaskTestEnvelope(1, 1, TaskTestAttemptStarted(declared), "test-start"),
        TaskTestEnvelope(1, 1, prepared, "test-prepare"),
        expected_test_attempt=None,
        expected_history_length=0,
        expected_history_hash=task_execution_hash(()),
    )
    commands = []
    for sequence in (1, 2):
        prefix = store.load_task_test_history("run", 1)
        store.reserve_task_test_command(
            "run",
            TaskTestEnvelope(
                1, 1, TaskTestCommandStarted(sequence), f"command-{sequence}"
            ),
            expected_test_attempt=1,
            expected_command_sequence=None if sequence == 1 else sequence - 1,
            expected_history_length=len(prefix),
            expected_history_hash=task_execution_hash(prefix),
        )
        code = 7 if sequence == 1 else 0
        response = {"exitCode": code, "stdout": "", "stderr": ""}
        receipt = TaskTestReconciled(
            sequence,
            True,
            "Synthetic receipt",
            f"/synthetic/{sequence}/execution.json",
            str(sequence) * 64,
            True,
        )
        for observed in (
            TaskTestCommandReported(sequence, code, response),
            TaskTestCommandExited(sequence, code),
            receipt,
        ):
            assert (
                store.apply_task_test(
                    "run", TaskTestEnvelope(1, 1, observed, "test-observed")
                )
                == Disposition.APPLIED
            )
        manifest = {
            "task_run_id": "run",
            "implementation_attempt": 1,
            "test_attempt": 1,
            "sequence": sequence,
            "input_hash": declared.input_hash,
            "prepared_hash": task_execution_hash(prepared),
            "path": receipt.receipt_path,
            "sha256": receipt.receipt_sha256,
            "process_stopped": True,
            "qualified": True,
            "validation_error": None,
            "stop_scope": prepared.policy.required_stop_scope,
            "output_scope": prepared.policy.output_scope,
            "history_length": 0,
            "history_hash": task_execution_hash(()),
            "actual_result": {
                "sequence": sequence,
                "reported_exit_code": code,
                "response": response,
                "exit_code": code,
            },
        }
        commands.append(
            ExecutedCommand(
                manifest=manifest,
                stdout=ExecutedLog(content=b"synthetic\xff\r\n"),
                stderr=ExecutedLog(content=b""),
            )
        )
    store.apply_task_test(
        "run", TaskTestEnvelope(1, 1, TaskTestAttemptFinished(), "test-finish")
    )
    test_history = store.load_task_test_history("run", 1)
    snapshot = FrozenSnapshot(
        repository=value.frozen_rules.repository,
        baseline=value.card.baseline_commit,
        candidate=state.current_candidate.delivery.candidate,
        diff="synthetic diff",
        requirements=value.requirements,
        acceptance=value.card.acceptance_criteria,
        tests=(),
        files=(),
    )
    outer = ExecutedReviewInput(
        source=snapshot,
        executed_suite=ExecutedSuite(
            task_input=value,
            prepared=prepared,
            test_attempt=1,
            test_history_length=len(test_history),
            test_history_hash=task_execution_hash(test_history),
            commands=tuple(commands),
        ),
    )
    review = RoundState(
        outer.snapshot_id,
        "executed-review-v2",
        (SeatState(SeatConfig("gpt", "codex", "model-1", "high")),),
    )
    metadata = {"kind": "review", "created_at": "fixed", "parent_round_id": None}
    store.create_task_review("review", review, outer, metadata)
    review_history = []
    for event in (
        AttemptStarted(),
        Completed({"summary": "Synthetic boundary-validated report", "findings": []}),
        ProcessExited(0),
    ):
        envelope = Envelope("gpt", 1, event, "review-observed")
        review, disposition = apply(review, envelope)
        assert store.apply("review", envelope) == disposition
        review_history.append((envelope, disposition))
    checkpoint = {
        "version": 1,
        "review": {
            "round_id": "review",
            "state": task_execution_value(review),
            "input_snapshot_id": outer.snapshot_id,
            "parent_round_id": None,
            "metadata": metadata,
            "execution": {},
            "history": task_execution_value(tuple(review_history)),
        },
        "confirmation": None,
    }
    assert thaw_json(store.load_task_rework_checkpoint("review")) == checkpoint
    source = source_for(
        state,
        value,
        history,
        test_history_length=len(test_history),
        test_history_hash=task_execution_hash(test_history),
        review_snapshot_id=outer.snapshot_id,
        review_checkpoint_hash=task_execution_hash(checkpoint),
    )
    return SimpleNamespace(
        store=store,
        state=state,
        value=value,
        history=history,
        source=source,
        outer=outer,
        checkpoint=checkpoint,
        pair=pair(state, value, source),
    )


@pytest.fixture
def stored(tmp_path):
    path = tmp_path / "rework.sqlite3"
    with TaskReviewStore(path) as store:
        result = synthetic(store)
        result.path = path
        yield result


def test_task_rework_source_and_codec_keep_distinct_immutable_identity():
    state, value, history = tests.implementation()
    source = source_for(state, value, history)
    with pytest.raises(FrozenInstanceError):
        source.candidate_commit = "x"
    with pytest.raises(TypeError):
        source.assessment["status"] = "ready"
    started, prepared = pair(state, value, source)
    encoded = encode_task_envelope(started)
    assert json.loads(encoded)["event"]["type"] == "task_rework_started"
    assert decode_task_envelope(encoded) == started
    reserved = reserve_task_execution(state, started, prepared)
    attempt = reserved.current_attempt
    assert isinstance(
        decode_task_attempt(encode_task_attempt(attempt)).request, TaskReworkStarted
    )
    assert decode_task_attempt(encode_task_attempt(attempt)) == attempt
    original = implementations.reservation()[0]
    assert "source" not in json.loads(encode_task_envelope(original))["event"]
    assert decode_task_envelope(encode_task_envelope(original)) == original
    changed = replace(source, review_checkpoint_hash="c" * 64)
    assert source.source_hash != changed.source_hash


@pytest.mark.parametrize(
    "change",
    ["candidate", "candidate-hash", "attempt", "start", "baseline", "budget", "policy"],
)
def test_task_rework_requires_current_candidate_and_fresh_frozen_pair(change):
    state, value, history = tests.implementation()
    source = source_for(state, value, history)
    if change in {"candidate", "candidate-hash", "attempt"}:
        source = replace(
            source,
            **{
                "candidate": {"candidate_commit": "f" * 40},
                "candidate-hash": {"candidate_hash": "f" * 64},
                "attempt": {"implementation_attempt": 2},
            }[change],
        )
    started, prepared = pair(state, value, source)
    if change in {"start", "baseline"}:
        prepared = replace(
            prepared,
            event=replace(
                prepared.event,
                workspace=replace(
                    prepared.event.workspace,
                    **{
                        "start_commit" if change == "start" else "task_baseline": "f"
                        * 40
                    },
                ),
            ),
        )
    elif change in {"budget", "policy"}:
        prepared = replace(
            prepared,
            event=replace(
                prepared.event,
                policy=replace(
                    prepared.event.policy,
                    **{"max_attempts": 1} if change == "budget" else {"timeout": 99},
                ),
            ),
        )
    before = state
    with pytest.raises(ValueError):
        reserve_task_execution(state, started, prepared)
    assert state == before and len(state.attempts) == 1


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
def test_second_rework_is_refused_even_after_failure_or_cancel(outcome):
    state, value, history = tests.implementation()
    prior = prepared_for_task(state.current_attempt)
    expanded = replace(prior, policy=replace(prior.policy, max_attempts=3))
    state = replace(
        state,
        attempts=(
            replace(
                state.current_attempt,
                events=tuple(
                    expanded if item is prior else item
                    for item in state.current_attempt.events
                ),
            ),
        ),
    )
    history = tuple(
        (
            replace(envelope, event=expanded) if envelope.event is prior else envelope,
            disposition,
        )
        for envelope, disposition in history
    )
    started, prepared = pair(state, value, source_for(state, value, history))
    state = reserve_task_execution(state, started, prepared)
    events = {
        "completed": (Completed({"fixed": True}), ProcessExited(-15)),
        "failed": (Error("failed"), ProcessExited(1)),
        "cancelled": (CancelRequested(), ProcessExited(-15)),
    }[outcome]
    for event in (*events, TaskReconciled("new-digest", True, "stopped")):
        state, _ = apply_task(state, TaskEnvelope(2, event, "observed"))
    before = state
    ordinary = implementations.reservation(3, value=value)[0]
    with pytest.raises(ValueError):
        apply_task(state, ordinary)
    with pytest.raises(ValueError):
        apply_task(state, replace(started, attempt=3))
    assert state == before
    old = replace(started, attempt=1)
    assert apply_task(state, old) == (state, Disposition.HISTORY_ONLY)
    assert apply_task(state, TaskEnvelope(1, Completed({"late": True}))) == (
        state,
        Disposition.HISTORY_ONLY,
    )


def test_task_rework_store_atomic_reservation_and_reopen(stored):
    before = stored.store.load_task_history("run")
    stored.store.reserve_task_rework_execution("run", *stored.pair)
    expected = reserve_task_execution(stored.state, *stored.pair)
    assert stored.store.load_task("run") == expected
    assert stored.store.load_task_history("run") == (
        *before,
        *((event, Disposition.APPLIED) for event in stored.pair),
    )
    with TaskReviewStore(stored.path) as other:
        assert other.load_task("run") == expected
    with pytest.raises(ValueError):
        stored.store.reserve_task_rework_execution("run", *stored.pair)


@pytest.mark.parametrize(
    "drift",
    [
        "task-history",
        "test-history",
        "review-history",
        "confirmation",
        "snapshot",
        "input",
    ],
)
def test_task_rework_source_cas(stored, drift):
    store = stored.store
    if drift == "task-history":
        store.apply_task("run", TaskEnvelope(1, CancelRequested(), "late"))
    elif drift == "test-history":
        store.apply_task_test(
            "run", TaskTestEnvelope(1, 1, TaskTestCancelRequested(), "late")
        )
    elif drift == "review-history":
        store.apply("review", Envelope("gpt", 1, CancelRequested(), "late"))
    elif drift == "confirmation":
        store.create_review(
            "confirmation",
            RoundState(
                stored.outer.snapshot_id,
                "executed-confirmation-v2",
                (SeatState(SeatConfig("gpt", "codex", "model-1", "high")),),
            ),
            {"kind": "confirmation"},
            "review",
        )
    elif drift == "snapshot":
        store._connection.execute(
            "UPDATE snapshots SET payload=? WHERE snapshot_id=?",
            ("{}", stored.outer.snapshot_id),
        )
    else:
        store._connection.execute(
            "UPDATE task_runs SET input_payload=? WHERE run_id='run'",
            (
                encode_task_input(
                    replace(
                        stored.value,
                        requirements=stored.value.requirements
                        + " changed requirements",
                    )
                ),
            ),
        )
    before = store.load_task("run"), store.load_task_history("run")
    with pytest.raises(ValueError):
        store.reserve_task_rework_execution("run", *stored.pair)
    assert (store.load_task("run"), store.load_task_history("run")) == before


@pytest.mark.parametrize("method", ["_write_task", "_append_task_history"])
def test_task_rework_reservation_rolls_back_pair(stored, monkeypatch, method):
    store = stored.store
    before = store.load_task("run"), store.load_task_history("run")
    original = getattr(store, method)
    error = sqlite3.OperationalError("actual write then fault")

    def fail(*args):
        original(*args)
        raise error

    with monkeypatch.context() as patch:
        patch.setattr(store, method, fail)
        with pytest.raises(sqlite3.OperationalError) as caught:
            store.reserve_task_rework_execution("run", *stored.pair)
    assert caught.value is error
    assert (store.load_task("run"), store.load_task_history("run")) == before
    assert not store._connection.in_transaction


def test_task_rework_two_connections_reserve_only_once(stored):
    gate = Barrier(2)

    def reserve():
        with TaskReviewStore(stored.path) as store:
            gate.wait()
            try:
                store.reserve_task_rework_execution("run", *stored.pair)
            except ValueError:
                return "rejected"
            return "committed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(reserve) for _ in range(2)]
        assert sorted(f.result() for f in futures) == ["committed", "rejected"]
    assert len(stored.store.load_task("run").attempts) == 2
    assert (
        sum(
            isinstance(e.event, TaskReworkStarted)
            for e, d in stored.store.load_task_history("run")
            if d == Disposition.APPLIED
        )
        == 1
    )


def test_task_rework_cannot_use_generic_store_control_paths(stored):
    before = stored.store.load_task_history("run")
    with pytest.raises(TypeError):
        stored.store.apply_task("run", stored.pair[0])
    with pytest.raises(TypeError):
        stored.store.reserve_task_attempt(
            "run",
            *stored.pair,
            expected_history_length=len(before),
            expected_history_hash=task_execution_hash(before),
        )
    assert stored.store.load_task_history("run") == before


@pytest.mark.parametrize("change", ["shrink", "order", "same"])
def test_rework_keeps_complete_test_suite_in_store(stored, change):
    store = stored.store
    store.reserve_task_rework_execution("run", *stored.pair)
    for event in (
        Completed({"fixed": True}),
        ProcessExited(-15),
        candidates.reconciliation(),
    ):
        store.apply_task("run", TaskEnvelope(2, event, "rework-observed"))
    candidate = candidates.candidate(store.load_task("run"))
    candidate = replace(
        candidate, delivery=replace(candidate.delivery, candidate="e" * 40)
    )
    history = store.load_task_history("run")
    store.record_task_candidate(
        "run",
        TaskEnvelope(2, candidate, "new-candidate"),
        expected_history_length=len(history),
        expected_history_hash=task_execution_hash(history),
    )
    current = store.load_task("run")
    declared = tests.declared_input(
        current, stored.value, store.load_task_history("run")
    )
    declared = replace(
        declared, declaration_source="A new actual complete-suite declaration"
    )
    assert declared.commands[0].name != declared.commands[1].name
    assert declared.commands[0].argv == declared.commands[1].argv
    if change == "shrink":
        declared = replace(declared, commands=declared.commands[:1])
    elif change == "order":
        declared = replace(declared, commands=tuple(reversed(declared.commands)))
    envelope = TaskTestEnvelope(2, 1, TaskTestAttemptStarted(declared), "new-suite")

    def reserve():
        store.reserve_task_test_attempt(
            "run",
            envelope,
            expected_test_attempt=None,
            expected_history_length=0,
            expected_history_hash=task_execution_hash(()),
        )

    if change == "same":
        reserve()
        assert store.load_task_test("run", 2).current_attempt.input == declared
        assert declared.candidate.delivery.candidate == "e" * 40
    else:
        with pytest.raises(ValueError):
            reserve()
        with pytest.raises(KeyError):
            store.load_task_test("run", 2)


def test_task_rework_postcommit_failure_retains_consumed_reservation(
    stored, monkeypatch
):
    method = stored.store.reserve_task_rework_execution
    failure = sqlite3.OperationalError("synthetic postcommit failure")

    def after_commit(*args):
        method(*args)
        raise failure

    monkeypatch.setattr(stored.store, "reserve_task_rework_execution", after_commit)
    with pytest.raises(sqlite3.OperationalError) as caught:
        stored.store.reserve_task_rework_execution("run", *stored.pair)
    assert caught.value is failure
    current = stored.store.load_task("run")
    assert len(current.attempts) == 2 and isinstance(
        current.current_attempt.request, TaskReworkStarted
    )
    assert current.current_attempt.events == tuple(item.event for item in stored.pair)
    assert current.current_attempt.runtime.exit_code is None
