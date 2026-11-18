"""Synthetic host facts in real SQLite transactions, never test execution."""

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, replace
from pathlib import Path
from threading import Barrier

import pytest
import test_task_candidate_store as old_goldens
import test_task_store as legacy
import test_task_tests as helpers

from multi_agent.core import Disposition
from multi_agent.core.events import CancelRequested, Error, FailureCategory
from multi_agent.core.snapshot import FrozenSnapshot
from multi_agent.core.task_events import (
    TaskDiagnostic,
    TaskEnvelope,
    TaskEvidenceRejected,
)
from multi_agent.core.task_execution import task_execution_hash
from multi_agent.core.task_test_events import (
    TaskTestAttemptFinished,
    TaskTestAttemptStarted,
    TaskTestCancelRequested,
    TaskTestCommandExited,
    TaskTestCommandStarted,
    TaskTestEnvelope,
    TaskTestProblem,
    TaskTestReconciled,
)
from multi_agent.core.task_test_input import TaskTestInput
from multi_agent.core.task_test_state import TaskTestPhase
from multi_agent.core.transitions import (
    apply_task_test,
    task_test_phase,
    task_test_ready,
)
from multi_agent.store import database
from multi_agent.store.task_codec import (
    decode_task_attempt,
    decode_task_envelope,
    encode_task_attempt,
    encode_task_envelope,
    encode_task_input,
)
from multi_agent.store.task_test_codec import (
    decode_task_test_attempt,
    decode_task_test_envelope,
    decode_task_test_input,
    encode_task_test_attempt,
    encode_task_test_envelope,
    encode_task_test_input,
)
from multi_agent.store.task_test_store import TaskTestStore


@pytest.fixture
def store(tmp_path):
    with TaskTestStore(tmp_path / "task-tests.sqlite") as connection:
        implementation, value, history = helpers.implementation()
        legacy.create(connection, value=value)
        started, prepared = history[0][0], history[1][0]
        connection.reserve_task_attempt(
            "run",
            started,
            prepared,
            expected_history_length=0,
            expected_history_hash=task_execution_hash(()),
        )
        for envelope, disposition in history[2:-1]:
            assert connection.apply_task("run", envelope) == disposition
        prefix = connection.load_task_history("run")
        assert (
            connection.record_task_candidate(
                "run",
                history[-1][0],
                expected_history_length=len(prefix),
                expected_history_hash=task_execution_hash(prefix),
            )
            == Disposition.APPLIED
        )
        assert connection.load_task("run") == implementation
        assert connection.load_task_history("run") == history
        yield connection


def declared(store):
    return helpers.declared_input(
        store.load_task("run"),
        store.load_task_input("run"),
        store.load_task_history("run"),
    )


def reserve(
    store, *, value=None, number=1, expected=None, prefix=None, occurred_at="test-start"
):
    value = declared(store) if value is None else value
    if prefix is None:
        prefix = () if number == 1 else store.load_task_test_history("run", 1)
    envelope = TaskTestEnvelope(1, number, TaskTestAttemptStarted(value), occurred_at)
    store.reserve_task_test_attempt(
        "run",
        envelope,
        expected_test_attempt=expected,
        expected_history_length=len(prefix),
        expected_history_hash=task_execution_hash(prefix),
    )
    return envelope


def command(
    store,
    sequence=1,
    *,
    expected_sequence=None,
    prefix=None,
    attempt=1,
    expected_attempt=1,
    occurred_at="command-start",
):
    prefix = store.load_task_test_history("run", 1) if prefix is None else prefix
    envelope = TaskTestEnvelope(
        1, attempt, TaskTestCommandStarted(sequence), occurred_at
    )
    store.reserve_task_test_command(
        "run",
        envelope,
        expected_test_attempt=expected_attempt,
        expected_command_sequence=expected_sequence,
        expected_history_length=len(prefix),
        expected_history_hash=task_execution_hash(prefix),
    )
    return envelope


def send(store, event, *, attempt=1, implementation_attempt=1, occurred_at="observed"):
    return store.apply_task_test(
        "run", TaskTestEnvelope(implementation_attempt, attempt, event, occurred_at)
    )


def fail_and_finish(store):
    send(store, TaskTestProblem(None, None, "synthetic failure"))
    send(store, helpers.receipt(None))
    send(store, TaskTestAttemptFinished())


def raw_tables(store):
    return tuple(
        (
            table,
            tuple(
                tuple(row)
                for row in store._connection.execute(
                    f"SELECT * FROM {table} ORDER BY rowid"
                )
            ),
        )
        for table in ("task_test_stages", "task_test_attempts", "task_test_history")
    )


def test_task_test_reservation_is_atomic_and_preserves_implementation_history(
    store, tmp_path
):
    original = store.load_task("run"), store.load_task_history("run")
    envelope = reserve(store)
    state = store.load_task_test("run", 1)
    assert state.phase == TaskTestPhase.RUNNING
    assert state.reference_input == state.current_attempt.input == envelope.event.input
    assert state.current_attempt.events == (envelope.event,)
    assert store.load_task_test_history("run", 1) == ((envelope, Disposition.APPLIED),)
    command_envelope = command(store)
    assert store.load_task_test("run", 1).current_command.sequence == 1
    assert store.load_task_test_history("run", 1)[-1] == (
        command_envelope,
        Disposition.APPLIED,
    )
    assert (store.load_task("run"), store.load_task_history("run")) == original
    with TaskTestStore(tmp_path / "task-tests.sqlite") as reopened:
        assert reopened.load_task_test("run", 1) == store.load_task_test("run", 1)
        assert reopened.load_task_test_history(
            "run", 1
        ) == store.load_task_test_history("run", 1)


@pytest.mark.parametrize("kind", ["attempt", "command"])
@pytest.mark.parametrize(
    "stale", ["expected-attempt", "length", "same-length-hash", "history-only-tail"]
)
def test_task_test_reservations_require_full_test_history_cas(store, kind, stale):
    reserve(store)
    if kind == "attempt":
        fail_and_finish(store)
    prefix = store.load_task_test_history("run", 1)
    expected = 1
    if stale == "expected-attempt":
        expected = None if kind == "attempt" else 2
    elif stale == "length":
        prefix = prefix[:-1]
    elif stale == "same-length-hash":
        prefix = (
            *prefix[:-1],
            (replace(prefix[-1][0], occurred_at="changed time"), prefix[-1][1]),
        )
    else:
        event = (
            TaskTestAttemptFinished()
            if kind == "attempt"
            else TaskTestReconciled(None, None, "unknown")
        )
        send(store, event)
        if kind == "command":
            send(store, event)
            send(store, helpers.receipt(None))
    before = raw_tables(store)
    with pytest.raises(ValueError):
        if kind == "attempt":
            reserve(store, number=2, expected=expected, prefix=prefix)
        else:
            command(store, expected_attempt=expected, prefix=prefix)
    assert raw_tables(store) == before


@pytest.mark.parametrize("kind", ["attempt", "command"])
@pytest.mark.parametrize("stale", ["length", "same-length-hash", "history-only-tail"])
def test_task_test_reservation_uses_frozen_implementation_checkpoint(
    store, kind, stale
):
    value = declared(store)
    if stale == "length":
        value = replace(value, task_history_length=value.task_history_length - 1)
    elif stale == "same-length-hash":
        value = replace(value, task_history_hash="0" * 64)
    if kind == "command":
        reserve(store)
        original = store.load_task_history("run")
        if stale == "same-length-hash":
            # Retain every row and its disposition, changing only the stored original timestamp.
            last, disposition = original[-1]
            store._connection.execute(
                "UPDATE task_history SET envelope=? WHERE run_id='run' AND sequence=?",
                (
                    encode_task_envelope(
                        replace(last, occurred_at="changed original timestamp")
                    ),
                    len(original),
                ),
            )
            changed = store.load_task_history("run")
            assert len(changed) == len(original)
            assert changed[:-1] == original[:-1] and changed[-1][1] == disposition
            assert task_execution_hash(changed) != task_execution_hash(original)
        elif stale == "length":
            assert (
                store.apply_task(
                    "run",
                    TaskEnvelope(
                        1,
                        TaskDiagnostic(Error("actual later host note")),
                        "later host note",
                    ),
                )
                == Disposition.APPLIED
            )
            assert len(store.load_task_history("run")) == len(original) + 1
        else:
            assert (
                store.apply_task(
                    "run",
                    TaskEnvelope(1, CancelRequested(), "late implementation cancel"),
                )
                == Disposition.HISTORY_ONLY
            )
        assert store.load_task("run").candidate_ready
    elif stale == "history-only-tail":
        store.apply_task(
            "run", TaskEnvelope(1, CancelRequested(), "late implementation cancel")
        )
    before = raw_tables(store)
    with pytest.raises(ValueError):
        if kind == "attempt":
            reserve(store, value=value)
        else:
            command(store)
    assert raw_tables(store) == before
    if kind == "attempt":
        reserve(store)  # Newly prepared checkpoint remains valid.
    else:
        assert (
            send(
                store, TaskTestProblem(None, None, "still preserve actual observation")
            )
            == Disposition.APPLIED
        )


@pytest.mark.parametrize(
    "field",
    [
        "task_run_id",
        "implementation_attempt",
        "task_id",
        "task_hash",
        "rules_hash",
        "task_input_hash",
        "candidate-anchor",
        "candidate-tree",
        "command-name",
        "command-argv",
    ],
)
def test_task_test_store_recomputes_original_source_and_registry(store, field):
    before = raw_tables(store)
    with pytest.raises(ValueError):
        reserve(store, value=helpers.changed_input(declared(store), field))
    assert raw_tables(store) == before


@pytest.mark.parametrize("field", helpers.SEMANTIC_CHANGES)
def test_task_test_store_retry_preserves_complete_reference_semantics(store, field):
    reserve(store)
    fail_and_finish(store)
    before = raw_tables(store)
    with pytest.raises(ValueError):
        reserve(
            store,
            number=2,
            expected=1,
            value=helpers.changed_input(declared(store), field),
        )
    assert raw_tables(store) == before


def test_task_test_store_fresh_checkpoint_retry_keeps_old_input_and_restarts_suite(
    store,
):
    first = reserve(store)
    fail_and_finish(store)
    store.apply_task(
        "run", TaskEnvelope(1, CancelRequested(), "late implementation observation")
    )
    new_input = declared(store)
    assert new_input.input_hash != first.event.input.input_hash
    second = reserve(store, number=2, expected=1, value=new_input)
    state = store.load_task_test("run", 1)
    assert state.reference_input == first.event.input
    assert tuple(attempt.input for attempt in state.attempts) == (
        first.event.input,
        new_input,
    )
    assert state.current_attempt.commands == () and state.current_attempt.events == (
        second.event,
    )
    assert state.current_attempt.reconciliation is None
    command(store, attempt=2, expected_attempt=2)
    assert (
        send(store, TaskTestProblem(None, None, "old preflight"), attempt=1)
        == Disposition.HISTORY_ONLY
    )


@pytest.mark.parametrize(
    "event",
    [
        TaskTestCommandExited(1, 0),
        helpers.receipt(1),
        TaskTestProblem(1, None, "host observed"),
        TaskTestAttemptFinished(),
    ],
)
def test_task_test_candidate_revocation_allows_actual_observations(store, event):
    reserve(store)
    command(store)
    store.apply_task(
        "run", TaskEnvelope(1, TaskEvidenceRejected("actual archive invalidated"))
    )
    assert send(store, event) == Disposition.APPLIED
    state = store.load_task_test("run", 1)
    assert state.current_attempt.events[-1] == event
    assert not task_test_ready(state, store.load_task("run"))
    before = raw_tables(store)
    with pytest.raises(ValueError):
        command(store, 2, expected_sequence=1)
    assert raw_tables(store) == before


@pytest.mark.parametrize("control_event", ["attempt", "command"])
def test_task_test_generic_apply_cannot_bypass_authorization(store, control_event):
    reserve(store)
    event = (
        TaskTestAttemptStarted(declared(store))
        if control_event == "attempt"
        else TaskTestCommandStarted(1)
    )
    before = raw_tables(store)
    with pytest.raises(ValueError):
        send(store, event)
    assert raw_tables(store) == before
    wrong = TaskTestEnvelope(1, 1, TaskTestAttemptFinished())
    with pytest.raises(TypeError):
        if control_event == "attempt":
            store.reserve_task_test_attempt(
                "run",
                wrong,
                expected_test_attempt=1,
                expected_history_length=1,
                expected_history_hash="unused",
            )
        else:
            store.reserve_task_test_command(
                "run",
                wrong,
                expected_test_attempt=1,
                expected_command_sequence=None,
                expected_history_length=1,
                expected_history_hash="unused",
            )
    assert raw_tables(store) == before


@pytest.mark.parametrize("operation", ["load", "history", "apply"])
@pytest.mark.parametrize(
    "number,error",
    [(0, ValueError), (True, ValueError), (2, ValueError), (1, KeyError)],
)
def test_task_test_invalid_numbers_precede_missing_stage(
    store, operation, number, error
):
    before = raw_tables(store)
    with pytest.raises(error):
        if operation == "apply":
            send(store, TaskTestAttemptFinished(), implementation_attempt=number)
        elif operation == "load":
            store.load_task_test("run", number)
        else:
            store.load_task_test_history("run", number)
    assert raw_tables(store) == before
    with pytest.raises(KeyError):
        store.load_task_test("unknown-run", 1)


@pytest.mark.parametrize("kind", ["attempt", "command"])
def test_task_test_two_connections_reserve_exactly_once(store, tmp_path, kind):
    if kind == "command":
        reserve(store)
    prefix = () if kind == "attempt" else store.load_task_test_history("run", 1)
    value = declared(store)
    barrier = Barrier(2)

    def compete(index):
        with TaskTestStore(tmp_path / "task-tests.sqlite") as connection:
            barrier.wait(timeout=10)
            try:
                if kind == "attempt":
                    event = reserve(
                        connection,
                        value=value,
                        prefix=prefix,
                        occurred_at=f"racer-{index}",
                    )
                else:
                    event = command(
                        connection, prefix=prefix, occurred_at=f"racer-{index}"
                    )
            except ValueError:
                return None
            return event

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            future.result(timeout=20)
            for future in [pool.submit(compete, index) for index in range(2)]
        ]
    winners = [item for item in results if item is not None]
    assert len(winners) == 1 and results.count(None) == 1
    assert store.load_task_test_history("run", 1) == (
        *prefix,
        (winners[0], Disposition.APPLIED),
    )
    assert len(store.load_task_test("run", 1).attempts) == 1


@pytest.mark.parametrize(
    "operation", ["first-attempt", "retry", "command", "observation"]
)
@pytest.mark.parametrize("point", ["state-write", "history-write"])
def test_task_test_transaction_failures_leave_no_partial_state(
    store, monkeypatch, operation, point
):
    if operation != "first-attempt":
        reserve(store)
    if operation == "retry":
        fail_and_finish(store)
    before = raw_tables(store), store.load_task_history("run")
    name = "_write_test" if point == "state-write" else "_append_test_history"
    original = getattr(store, name)
    failure = sqlite3.OperationalError("synthetic transaction fault")
    transaction_seen = []

    def fail_after_write(*args):
        original(*args)
        transaction_seen.append(store._connection.in_transaction)
        raise failure

    with monkeypatch.context() as patch:
        patch.setattr(store, name, fail_after_write)
        with pytest.raises(sqlite3.OperationalError) as caught:
            if operation == "first-attempt":
                reserve(store)
            elif operation == "retry":
                reserve(store, number=2, expected=1)
            elif operation == "command":
                command(store)
            else:
                send(store, TaskTestProblem(None, None, "actual problem"))
        assert caught.value is failure
    assert transaction_seen == [True]
    assert (raw_tables(store), store.load_task_history("run")) == before
    assert not store._connection.in_transaction


@pytest.mark.parametrize(
    "table", ["task_test_stages", "task_test_attempts", "task_test_history"]
)
def test_task_test_sql_write_failure_rolls_back_first_reservation(store, table):
    store._connection.execute(
        f"CREATE TRIGGER injected_failure BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT, 'synthetic insert failure'); END"
    )
    before = raw_tables(store)
    with pytest.raises(sqlite3.IntegrityError, match="synthetic insert failure"):
        reserve(store)
    assert raw_tables(store) == before
    assert not store._connection.in_transaction
    store._connection.execute("DROP TRIGGER injected_failure")
    reserve(store)


def test_task_test_postcommit_outer_error_does_not_claim_rollback(store, monkeypatch):
    original = store.reserve_task_test_attempt
    failure = RuntimeError("caller failed after actual commit")
    captured = []

    def committed_then_raise(*args, **kwargs):
        original(*args, **kwargs)
        captured.append(args[1])
        raise failure

    with monkeypatch.context() as patch:
        patch.setattr(store, "reserve_task_test_attempt", committed_then_raise)
        with pytest.raises(RuntimeError) as caught:
            reserve(store)
        assert caught.value is failure
    assert store.load_task_test_history("run", 1) == (
        (captured[0], Disposition.APPLIED),
    )
    assert store.load_task_test("run", 1).current_attempt.events == (captured[0].event,)
    with pytest.raises(ValueError):
        reserve(store)
    assert len(store.load_task_test_history("run", 1)) == 1


def test_task_test_phase_rebuild_uses_one_derivation(store, tmp_path):
    reserve(store)
    pure = store.load_task_test("run", 1)
    impl = store.load_task("run")
    events = (
        TaskTestCommandStarted(1),
        TaskTestCommandExited(1, 0),
        helpers.receipt(1),
        TaskTestCommandStarted(2),
        TaskTestCommandExited(2, 0),
        helpers.receipt(2),
        TaskTestAttemptFinished(),
        helpers.receipt(2, stopped=False),
        helpers.receipt(2),
    )
    for event in events:
        envelope = TaskTestEnvelope(1, 1, event, "step")
        pure, disposition = apply_task_test(pure, envelope, impl)
        if isinstance(event, TaskTestCommandStarted):
            command(
                store,
                event.sequence,
                expected_sequence=None if event.sequence == 1 else event.sequence - 1,
                occurred_at="step",
            )
        else:
            assert store.apply_task_test("run", envelope) == disposition
        assert store.load_task_test("run", 1) == pure
        with TaskTestStore(tmp_path / "task-tests.sqlite") as reopened:
            assert reopened.load_task_test("run", 1) == pure
            assert reopened.load_task_test("run", 1).phase == task_test_phase(
                pure.current_attempt
            )
    assert pure.phase == TaskTestPhase.SUCCEEDED
    for table, _ in raw_tables(store):
        assert "phase" not in {
            row[1] for row in store._connection.execute(f"PRAGMA table_info({table})")
        }


@pytest.mark.parametrize("stopped", [None, False, True])
def test_task_test_codec_preserves_unknown_receipts_and_input_hash(stopped):
    impl, state = helpers.started()
    state = helpers.advance(state, impl, helpers.receipt(None, stopped=stopped))
    input_value = state.reference_input
    restored_input = decode_task_test_input(encode_task_test_input(input_value))
    assert (
        restored_input == input_value
        and restored_input.input_hash == input_value.input_hash
    )
    assert restored_input.candidate_hash == input_value.candidate_hash
    restored_attempt = decode_task_test_attempt(
        encode_task_test_attempt(state.current_attempt)
    )
    assert restored_attempt == state.current_attempt
    assert restored_attempt.reconciliation.process_stopped is stopped
    assert isinstance(restored_attempt.commands, tuple) and isinstance(
        restored_attempt.events, tuple
    )
    for event in (
        *state.current_attempt.events,
        TaskTestCommandStarted(1),
        TaskTestCommandExited(1, -9),
        TaskTestProblem(1, FailureCategory.TESTS_FAILED, "测试失败"),
        TaskTestCancelRequested(),
        TaskTestAttemptFinished(),
    ):
        envelope = TaskTestEnvelope(1, 1, event, "original 时间\r\n")
        assert (
            decode_task_test_envelope(encode_task_test_envelope(envelope)) == envelope
        )


@pytest.mark.parametrize("field", [item.name for item in fields(TaskTestInput)])
def test_task_test_codec_input_fields_are_required(field):
    _, state = helpers.started()
    raw = json.loads(encode_task_test_input(state.reference_input))
    del raw[field]
    with pytest.raises(KeyError):
        decode_task_test_input(json.dumps(raw))


@pytest.mark.parametrize(
    "mutation,error",
    [
        ("candidate-missing-kind", KeyError),
        ("candidate-unknown-kind", ValueError),
        ("event-missing-kind", KeyError),
        ("event-unknown-kind", ValueError),
        ("unknown-category", ValueError),
    ],
)
def test_task_test_codec_rejects_unknown_or_missing_kinds(mutation, error):
    _, state = helpers.started()
    if mutation.startswith("candidate"):
        raw = json.loads(encode_task_test_input(state.reference_input))
        if mutation == "candidate-missing-kind":
            del raw["candidate"]["kind"]
        else:
            raw["candidate"]["kind"] = "different"
        decoder = decode_task_test_input
    else:
        raw = json.loads(
            encode_task_test_envelope(
                TaskTestEnvelope(1, 1, TaskTestProblem(None, None, "host"))
            )
        )
        if mutation == "event-missing-kind":
            del raw["event"]["kind"]
        elif mutation == "event-unknown-kind":
            raw["event"]["kind"] = "different"
        else:
            raw["event"]["category"] = "not_a_failure"
        decoder = decode_task_test_envelope
    with pytest.raises(error):
        decoder(json.dumps(raw))


def test_task_test_legacy_eight_goldens_remain_byte_exact():
    for name, raw in old_goldens.OLD_GOLDENS.items():
        assert (
            hashlib.sha256(raw.encode()).hexdigest()
            == old_goldens.OLD_PAYLOAD_HASHES[name]
        )
        if name == "snapshot":
            restored = FrozenSnapshot.from_dict(json.loads(raw))
            assert restored.snapshot_id == old_goldens.OLD_SNAPSHOT_ID
            assert restored == old_goldens.snapshot_golden_value()
        elif name.endswith("attempt"):
            assert encode_task_attempt(decode_task_attempt(raw)) == raw
        else:
            assert encode_task_envelope(decode_task_envelope(raw)) == raw


OLD_TABLES = (
    "rounds",
    "seats",
    "attempts",
    "history",
    "snapshots",
    "round_inputs",
    "executions",
    "task_runs",
    "task_attempts",
    "task_history",
    "planning_runs",
    "planning_attempts",
    "planning_history",
    "task_planning_provenance",
)
NEW_TABLES = {
    "task_test_stages",
    "task_test_attempts",
    "task_test_history",
    "integrations",
    "integration_rebases",
    "integration_test_attempts",
    "integration_review_links",
    "integration_acceptances",
    "integration_reworks",
}


def v4_database(path):
    migrations = Path(database.__file__).with_name("migrations")
    text = ' \n\t{"原文": "保持\\r\\n字节", "space": [ 1,  2 ]}\r\n '
    blob = b"\x00\xff\r\n" + "真实 BLOB".encode()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for script in sorted(migrations.glob("*.sql")):
            if int(script.name.split("_", 1)[0]) <= 4:
                connection.executescript(script.read_text())
        connection.execute("PRAGMA user_version = 4")
        connection.execute("BEGIN")
        rows = {
            "rounds": ("review", "snapshot", "legacy-v4"),
            "seats": ("review", "reviewer", 0, "codex", "model", "medium"),
            "attempts": (
                "review",
                "reviewer",
                1,
                "succeeded",
                None,
                text,
                None,
                "session",
                None,
                None,
                None,
                None,
                text,
                -15,
            ),
            "history": ("review", 1, blob, "applied"),
            "snapshots": ("snapshot", blob),
            "round_inputs": ("review", "snapshot", text, None),
            "executions": ("review", "reviewer", 1, blob),
            "task_runs": (
                "legacy-task",
                "T-0490",
                "taskhash",
                "ruleshash",
                "a" * 40,
                blob,
                "awaiting_delivery",
                1,
                None,
            ),
            "task_attempts": ("legacy-task", 1, text),
            "task_history": ("legacy-task", 1, blob, "history_only"),
            "planning_runs": (
                "legacy-plan",
                "inputhash",
                text,
                "created",
                "bound",
                "review",
                text,
                blob,
                None,
                "legacy-task",
            ),
            "planning_attempts": ("legacy-plan", "review", 1, blob),
            "planning_history": ("legacy-plan", 1, text, "applied"),
            "task_planning_provenance": (
                "legacy-task",
                "legacy-plan",
                "planhash",
                blob,
            ),
        }
        assert set(rows) == set(OLD_TABLES) and len(rows) == 14
        for table, values in rows.items():
            placeholders = ",".join("?" for _ in values)
            connection.execute(f"INSERT INTO {table} VALUES ({placeholders})", values)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    return raw_legacy(path)


def raw_legacy(path):
    with sqlite3.connect(path) as connection:
        schema = tuple(
            row
            for row in connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
            )
            if row[2] in OLD_TABLES
        )
        rows = []
        for table in OLD_TABLES:
            columns = tuple(
                row[1] for row in connection.execute(f"PRAGMA table_info({table})")
            )
            expressions = ",".join(f'typeof("{column}")' for column in columns)
            rows.append(
                (
                    table,
                    columns,
                    tuple(connection.execute(f"SELECT * FROM {table} ORDER BY rowid")),
                    tuple(
                        connection.execute(
                            f"SELECT {expressions} FROM {table} ORDER BY rowid"
                        )
                    ),
                )
            )
        return schema, tuple(rows)


def test_task_test_v4_to_v5_preserves_all_original_schema_and_values(tmp_path):
    path = tmp_path / "real-v4.sqlite"
    before = v4_database(path)
    assert len(before[1]) == 14
    observed_types = {
        kind for _, _, _, rows in before[1] for row in rows for kind in row
    }
    assert {"text", "blob"} <= observed_types
    for _ in range(2):
        with TaskTestStore(path) as store:
            assert store._connection.execute("PRAGMA user_version").fetchone()[0] == 8
            assert (
                store._connection.execute("PRAGMA foreign_key_check").fetchall() == []
            )
            actual_tables = {
                row[0]
                for row in store._connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert actual_tables == set(OLD_TABLES) | NEW_TABLES
            for table in NEW_TABLES:
                assert (
                    store._connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
                    == 0
                )
        assert raw_legacy(path) == before


def test_task_test_failed_v5_migration_rolls_back_schema_version_and_rows(
    tmp_path, monkeypatch
):
    path = tmp_path / "fault-v4.sqlite"
    before = v4_database(path)
    package = tmp_path / "migration-resource"
    destination = package / "migrations"
    destination.mkdir(parents=True)
    for script in Path(database.__file__).with_name("migrations").glob("*.sql"):
        if int(script.name.split("_", 1)[0]) <= 4:
            (destination / script.name).write_bytes(script.read_bytes())
    (destination / "0005_task_tests.sql").write_text(
        "CREATE TABLE task_test_stages (id TEXT);\nINSERT INTO absent_migration_table VALUES (1);\nCREATE TABLE task_test_attempts (id TEXT);\n"
    )
    with monkeypatch.context() as patch:
        patch.setattr(database, "files", lambda _package: package)
        with pytest.raises(sqlite3.OperationalError, match="absent_migration_table"):
            TaskTestStore(path)
    assert raw_legacy(path) == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        } == set(OLD_TABLES)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    with TaskTestStore(path) as recovered:
        assert recovered._connection.execute("PRAGMA user_version").fetchone()[0] == 8
    assert raw_legacy(path) == before


@pytest.mark.parametrize(
    "invalid",
    [
        "missing-implementation",
        "missing-stage-attempt",
        "zero-attempt",
        "zero-history",
        "invalid-disposition",
        "wrong-stage-command",
    ],
)
def test_task_test_composite_foreign_keys_and_sql_constraints(store, invalid):
    reserve(store)
    before = raw_tables(store)
    with pytest.raises(sqlite3.IntegrityError), store._connection:
        store._connection.execute("BEGIN IMMEDIATE")
        if invalid == "missing-implementation":
            store._connection.execute(
                "INSERT INTO task_test_stages VALUES ('run',2,'{}',1)"
            )
            store._connection.execute(
                "INSERT INTO task_test_attempts VALUES ('run',2,1,'{}')"
            )
        elif invalid == "missing-stage-attempt":
            store._connection.execute(
                "UPDATE task_test_stages SET current_test_attempt=2 WHERE task_run_id='run'"
            )
        elif invalid == "zero-attempt":
            store._connection.execute(
                "INSERT INTO task_test_attempts VALUES ('run',1,0,'{}')"
            )
        elif invalid == "zero-history":
            store._connection.execute(
                "INSERT INTO task_test_history VALUES ('run',1,0,'{}','applied')"
            )
        elif invalid == "invalid-disposition":
            store._connection.execute(
                "INSERT INTO task_test_history VALUES ('run',1,2,'{}','invented')"
            )
        else:
            store._connection.execute(
                "INSERT INTO task_test_attempts VALUES ('run',2,1,'{}')"
            )
    assert raw_tables(store) == before


def test_task_test_old_implementation_stage_survives_task_upsert(store):
    reserve(store)
    command(store)
    old = store.load_task_test("run", 1)
    store.apply_task(
        "run", TaskEnvelope(1, TaskEvidenceRejected("explicit implementation retry"))
    )
    value = store.load_task_input("run")
    started, prepared = helpers.implementation_helpers.reservation(2, value=value)
    task_history = store.load_task_history("run")
    store.reserve_task_attempt(
        "run",
        started,
        prepared,
        expected_history_length=len(task_history),
        expected_history_hash=task_execution_hash(task_history),
    )
    assert send(store, TaskTestCommandExited(1, 0)) == Disposition.HISTORY_ONLY
    assert store.load_task_test("run", 1) == old
    assert store._connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert store.load_task_test_history("run", 1)[-1][1] == Disposition.HISTORY_ONLY
    with pytest.raises(ValueError):
        command(store, 2, expected_sequence=1)


@pytest.mark.parametrize("sequence", [0, True])
@pytest.mark.parametrize(
    "event_type", [TaskTestCommandExited, TaskTestProblem, TaskTestReconciled]
)
def test_task_test_invalid_sequence_precedes_missing_stage(store, sequence, event_type):
    if event_type is TaskTestCommandExited:
        event = event_type(sequence, 0)
    elif event_type is TaskTestProblem:
        event = event_type(sequence, None, "host")
    else:
        event = event_type(sequence, None, "host")
    with pytest.raises(ValueError):
        send(store, event)
    assert all(not rows for _, rows in raw_tables(store))


def test_task_test_first_input_boolean_implementation_is_not_integer_one(store):
    selected = replace(declared(store), implementation_attempt=True)
    with pytest.raises(ValueError):
        reserve(store, value=selected)
    assert all(not rows for _, rows in raw_tables(store))
    impl, state = helpers.setup_stage()
    state = replace(state, reference_input=selected)
    with pytest.raises(ValueError):
        helpers.observe(state, impl, TaskTestAttemptStarted(selected))


@pytest.mark.parametrize("expected", [0, True, 1, 2])
def test_task_test_current_command_cas_is_not_only_history(store, expected):
    reserve(store)
    before = raw_tables(store)
    with pytest.raises(ValueError):
        command(store, expected_sequence=expected)
    assert raw_tables(store) == before


def test_task_test_current_original_input_is_read_in_reservation_transaction(store):
    selected = declared(store)
    original = store.load_task_input("run")
    store._connection.execute(
        "UPDATE task_runs SET input_payload=? WHERE run_id='run'",
        (
            encode_task_input(
                replace(original, requirements="changed original requirement")
            ),
        ),
    )
    with pytest.raises(ValueError):
        reserve(store, value=selected)
    assert all(not rows for _, rows in raw_tables(store))
