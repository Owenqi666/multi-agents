"""Managed test pairs and read-only views in real SQLite, without execution."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest
import test_task_test_execution as core_helpers
import test_task_test_store as legacy

from multi_agent.core import Disposition
from multi_agent.core.events import CancelRequested
from multi_agent.core.task_events import TaskEnvelope
from multi_agent.core.task_execution import task_execution_hash
from multi_agent.core.task_test_events import (
    TaskTestAttemptStarted,
    TaskTestCommandExited,
    TaskTestEnvelope,
)
from multi_agent.core.task_test_execution import prepared_for_test
from multi_agent.store.database import _migration_scripts
from multi_agent.store.task_test_codec import (
    decode_task_test_attempt,
    decode_task_test_envelope,
    encode_task_test_attempt,
    encode_task_test_envelope,
)
from multi_agent.store.task_test_store import TaskTestStore

store = legacy.store


def pair(store, *, number=1, history=None):
    value = legacy.declared(store)
    if history is None:
        _, history = store.load_task_test_checkpoint("run", 1)
    prepared = core_helpers.preparation(
        value,
        number=number,
        history=history,
        repository=store.load_task_input("run").frozen_rules.repository,
    )
    return (
        TaskTestEnvelope(1, number, TaskTestAttemptStarted(value), f"start-{number}"),
        TaskTestEnvelope(1, number, prepared, f"prepare-{number}"),
    )


def reserve(store, *, envelopes=None, number=1, prefix=None):
    state, actual_history = store.load_task_test_checkpoint("run", 1)
    prefix = actual_history if prefix is None else prefix
    envelopes = pair(store, number=number) if envelopes is None else envelopes
    store.reserve_task_test_execution(
        "run",
        *envelopes,
        expected_test_attempt=state.current_attempt.number if state else None,
        expected_history_length=len(prefix),
        expected_history_hash=task_execution_hash(prefix),
    )
    return envelopes


def test_task_test_execution_store_pair_roundtrip_and_binary_controls(store, tmp_path):
    original = store.load_task_history("run")
    assert store.list_task_test_stages() == ()
    assert store.load_task_test_checkpoint("run", 1) == (None, ())
    envelopes = reserve(store)
    assert store.list_task_test_stages() == (("run", 1),)
    state, history = store.load_task_test_checkpoint("run", 1)
    assert history == tuple((event, Disposition.APPLIED) for event in envelopes)
    assert state.current_attempt.events == tuple(event.event for event in envelopes)
    prepared = prepared_for_test(state.current_attempt)
    assert prepared.checkout.controls[0].content == b"\xff\x00\r\n"
    assert prepared.checkout.controls[1].content is None
    encoded = encode_task_test_attempt(state.current_attempt)
    assert '"content":{"hex":"ff000d0a"}' in encoded
    assert decode_task_test_attempt(encoded) == state.current_attempt
    for envelope in envelopes:
        assert (
            decode_task_test_envelope(encode_task_test_envelope(envelope)) == envelope
        )
    with TaskTestStore(tmp_path / "task-tests.sqlite") as reopened:
        assert reopened.load_task_test_checkpoint("run", 1) == (state, history)
        assert reopened.load_task_history("run") == original


@pytest.mark.parametrize("field", ["scratch", "checkout", "context", "policy"])
def test_task_test_execution_codec_has_no_missing_preparation_defaults(store, field):
    _, envelope = pair(store)
    raw = json.loads(encode_task_test_envelope(envelope))
    del raw["event"][field]
    with pytest.raises(KeyError):
        decode_task_test_envelope(json.dumps(raw))


def test_task_test_execution_codec_preserves_report_and_legacy_bytes(store):
    reserve(store)
    legacy.command(store)
    reported = core_helpers.report(code=124)
    assert legacy.send(store, reported) == Disposition.APPLIED
    attempt = store.load_task_test("run", 1).current_attempt
    assert attempt.current_command.exit_code is None
    assert decode_task_test_attempt(encode_task_test_attempt(attempt)) == attempt
    legacy_envelope = TaskTestEnvelope(1, 2, TaskTestCommandExited(1, -9), "fixed")
    assert (
        encode_task_test_envelope(legacy_envelope)
        == '{"event":{"exit_code":-9,"kind":"task_test_command_exited","sequence":1},"implementation_attempt":1,"occurred_at":"fixed","test_attempt":2}'
    )


def test_task_test_execution_store_rejects_nonatomic_and_legacy_routes(store):
    started, prepared = pair(store)
    with pytest.raises(ValueError, match="atomic"):
        store.apply_task_test("run", prepared)
    legacy.reserve(store)
    legacy.fail_and_finish(store)
    with pytest.raises(ValueError, match="legacy"):
        reserve(store, number=2)
    assert store.load_task_test("run", 1).current_attempt.number == 1
    assert started.event.input == store.load_task_test("run", 1).reference_input


def test_task_test_execution_store_managed_cannot_downgrade(store):
    reserve(store)
    legacy.fail_and_finish(store)
    before = legacy.raw_tables(store)
    with pytest.raises(ValueError, match="atomic"):
        legacy.reserve(store, number=2, expected=1)
    assert legacy.raw_tables(store) == before
    reserve(store, number=2)
    assert store.load_task_test("run", 1).current_attempt.number == 2


@pytest.mark.parametrize(
    "changed", ["current", "length", "hash", "source", "repository"]
)
def test_task_test_execution_store_artifact_cannot_borrow_fresh_cas(store, changed):
    reserve(store)
    legacy.fail_and_finish(store)
    started, prepared = pair(store, number=2)
    checkout = prepared.event.checkout
    if changed == "current":
        checkout = replace(checkout, observed_test_attempt=None)
    elif changed == "length":
        checkout = replace(
            checkout, test_history_length=checkout.test_history_length - 1
        )
    elif changed == "hash":
        checkout = replace(checkout, test_history_hash="a" * 64)
    elif changed == "repository":
        checkout = replace(checkout, repository="/another/.git")
    else:
        store.apply_task(
            "run", TaskEnvelope(1, CancelRequested(), "new history-only source")
        )
    prepared = replace(prepared, event=replace(prepared.event, checkout=checkout))
    before = legacy.raw_tables(store)
    with pytest.raises(ValueError):
        reserve(store, envelopes=(started, prepared), number=2)
    assert legacy.raw_tables(store) == before


@pytest.mark.parametrize("point", ["write", "first-history", "second-history"])
@pytest.mark.parametrize("retry", [False, True])
def test_task_test_execution_store_precommit_fault_rolls_back_both_events(
    store, monkeypatch, point, retry
):
    if retry:
        reserve(store)
        legacy.fail_and_finish(store)
    before, implementation_history = (
        legacy.raw_tables(store),
        store.load_task_history("run"),
    )
    original_error = OSError("synthetic write fault")
    calls = 0
    name = "_write_test" if point == "write" else "_append_test_history"
    original = getattr(store, name)

    def failing(*args):
        nonlocal calls
        calls += 1
        original(*args)
        if calls == (2 if point == "second-history" else 1):
            assert store._connection.in_transaction
            raise original_error

    monkeypatch.setattr(store, name, failing)
    with pytest.raises(OSError) as caught:
        reserve(store, number=2 if retry else 1)
    assert caught.value is original_error
    assert legacy.raw_tables(store) == before
    assert store.load_task_history("run") == implementation_history


def test_task_test_execution_store_two_connections_have_one_pair_winner(
    store, tmp_path
):
    envelopes = pair(store)
    barrier = Barrier(2)

    def contender(number):
        with TaskTestStore(tmp_path / "task-tests.sqlite") as connection:
            started = replace(envelopes[0], occurred_at=f"winner-{number}")
            barrier.wait(timeout=10)
            try:
                connection.reserve_task_test_execution(
                    "run",
                    started,
                    envelopes[1],
                    expected_test_attempt=None,
                    expected_history_length=0,
                    expected_history_hash=task_execution_hash(()),
                )
            except ValueError:
                return None
            return started

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(contender, (1, 2)))
    winners = [item for item in results if item is not None]
    assert len(winners) == 1
    assert store.load_task_test_history("run", 1) == (
        (winners[0], Disposition.APPLIED),
        (envelopes[1], Disposition.APPLIED),
    )


def test_task_test_execution_store_postcommit_error_keeps_actual_pair(
    store, monkeypatch
):
    envelopes = pair(store)
    original = store._connection
    error = OSError("synthetic return after commit")

    class AfterCommit:
        def __getattr__(self, name):
            return getattr(original, name)

        def __enter__(self):
            original.__enter__()
            return self

        def __exit__(self, *args):
            result = original.__exit__(*args)
            if args[0] is None:
                raise error
            return result

    with monkeypatch.context() as patch:
        patch.setattr(store, "_connection", AfterCommit())
        with pytest.raises(OSError) as caught:
            store.reserve_task_test_execution(
                "run",
                *envelopes,
                expected_test_attempt=None,
                expected_history_length=0,
                expected_history_hash=task_execution_hash(()),
            )
        assert caught.value is error
    assert store.load_task_test_history("run", 1) == tuple(
        (event, Disposition.APPLIED) for event in envelopes
    )
    assert store.load_task_test("run", 1).current_attempt.number == 1


def test_task_test_execution_checkpoint_does_not_hide_corruption(store):
    reserve(store)
    with store._connection:
        store._connection.execute("UPDATE task_test_attempts SET payload='{}'")
    with pytest.raises(KeyError):
        store.load_task_test_checkpoint("run", 1)
    with pytest.raises(KeyError):
        store.load_task_test_checkpoint("missing", 1)


def test_task_test_execution_readonly_observes_wal_without_migration(
    store, tmp_path, monkeypatch
):
    store._connection.execute("PRAGMA journal_mode=WAL")
    reserve(store)
    before = legacy.raw_tables(store)

    def forbidden(*_args):
        pytest.fail("read-only open must not initialize or migrate")

    monkeypatch.setattr(TaskTestStore, "__init__", forbidden)
    monkeypatch.setattr(TaskTestStore, "_migrate", forbidden)
    with TaskTestStore.open_readonly(tmp_path / "task-tests.sqlite") as reader:
        assert reader._connection.execute("PRAGMA query_only").fetchone()[0] == 1
        assert reader.load_task_test_checkpoint(
            "run", 1
        ) == store.load_task_test_checkpoint("run", 1)
        assert reader.list_task_test_stages() == (("run", 1),)
        with pytest.raises(sqlite3.OperationalError):
            reader._connection.execute("CREATE TABLE unauthorized(value)")
    assert legacy.raw_tables(store) == before


def test_task_test_execution_readonly_refuses_missing_incompatible_and_symlink_database(
    tmp_path,
):
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(FileNotFoundError):
        TaskTestStore.open_readonly(missing)
    assert not missing.exists()
    for version in (4, 999):
        database = tmp_path / f"v{version}.sqlite"
        with sqlite3.connect(database) as connection:
            connection.execute(f"PRAGMA user_version={version}")
        before = database.read_bytes()
        with pytest.raises(ValueError, match=f"found {version}"):
            TaskTestStore.open_readonly(database)
        assert database.read_bytes() == before
    alias = tmp_path / "alias.sqlite"
    alias.symlink_to(database)
    with pytest.raises(ValueError, match="canonical"):
        TaskTestStore.open_readonly(alias)


def test_task_test_execution_readonly_keeps_v5_database_at_v5(tmp_path):
    database = tmp_path / "v5.sqlite"
    with sqlite3.connect(database) as connection:
        for version, script in _migration_scripts():
            if version <= 5:
                connection.executescript(script.read_text(encoding="utf-8"))
        connection.execute("PRAGMA user_version=5")
    before = database.read_bytes()
    with TaskTestStore.open_readonly(database) as reader:
        assert reader.list_task_test_stages() == ()
        assert reader._connection.execute("PRAGMA user_version").fetchone()[0] == 5
    assert database.read_bytes() == before
