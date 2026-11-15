import sqlite3
from pathlib import Path

import pytest

from multi_agent import core
from multi_agent.core import (
    AttemptState,
    AttemptStatus,
    Disposition,
    EvidenceSource,
    RoundState,
    SeatConfig,
    SeatState,
)
from multi_agent.core.events import (
    AttemptStarted,
    CancelRequested,
    Completed,
    Envelope,
    Error,
    FailureCategory,
    Message,
    ProcessExited,
    SessionStarted,
    Usage,
)
from multi_agent.store import ReviewStore, database


def initial_round() -> RoundState:
    return RoundState(
        "snapshot-1",
        "prompt-v1",
        (
            SeatState(SeatConfig("z", "provider-z", "requested-z", "high")),
            SeatState(SeatConfig("a", "provider-a", "requested-a", "medium")),
        ),
    )


START = Envelope("a", 1, AttemptStarted())


def test_migration_runs_once_and_database_reopens(tmp_path: Path) -> None:
    path = tmp_path / "review.sqlite"
    with ReviewStore(path) as store:
        store.create_round("round-1", initial_round())
        store.apply("round-1", START)
        expected = store.load_round("round-1")
        history = store.load_history("round-1")
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "rounds",
            "seats",
            "attempts",
            "history",
            "snapshots",
            "round_inputs",
            "executions",
        } <= tables

    for _ in range(2):
        with ReviewStore(path) as reopened:
            assert reopened.load_round("round-1") == expected
            assert reopened.load_history("round-1") == history
            assert tuple(seat.config.key for seat in expected.seats) == ("z", "a")


def test_locked_apply_leaves_no_changes_and_can_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "review.sqlite"
    original_apply = core.apply
    applied = []

    def record_apply(
        state: RoundState, envelope: Envelope
    ) -> tuple[RoundState, Disposition]:
        applied.append(envelope)
        return original_apply(state, envelope)

    monkeypatch.setattr(core, "apply", record_apply)
    with ReviewStore(path, timeout=0) as store:
        store.create_round("round-1", initial_round())
        blocker = sqlite3.connect(path, timeout=0)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                store.apply("round-1", START)
            assert applied == []
            assert store.load_round("round-1") == initial_round()
            assert store.load_history("round-1") == ()
            blocker.rollback()
            assert store.apply("round-1", START) is Disposition.APPLIED
            assert applied == [START]
        finally:
            blocker.close()
        assert store.load_history("round-1") == ((START, Disposition.APPLIED),)


def test_failed_migration_rolls_back_schema_and_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resources = tmp_path / "faulty-resources"
    migrations = resources / "migrations"
    migrations.mkdir(parents=True)
    (migrations / "0001_faulty.sql").write_text(
        "CREATE TABLE partially_created (value TEXT);\n"
        "INSERT INTO nonexistent_table VALUES ('fail');\n",
        encoding="utf-8",
    )
    path = tmp_path / "review.sqlite"
    with monkeypatch.context() as patch:
        patch.setattr(database, "files", lambda package: resources)
        with pytest.raises(sqlite3.OperationalError, match="nonexistent_table"):
            ReviewStore(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (0,)
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
            == []
        )
    with ReviewStore(path) as store:
        store.create_round("round-1", initial_round())
        assert store.load_round("round-1") == initial_round()


def test_core_error_rolls_back_without_changing_history(tmp_path: Path) -> None:
    with ReviewStore(tmp_path / "review.sqlite") as store:
        store.create_round("round-1", initial_round())
        store.apply("round-1", START)
        state = store.load_round("round-1")
        history = store.load_history("round-1")
        with pytest.raises(ValueError):
            store.apply("round-1", Envelope("a", 2, AttemptStarted()))
        assert store.load_round("round-1") == state
        assert store.load_history("round-1") == history
        assert (
            store.apply("round-1", Envelope("a", 1, Completed("valid")))
            is Disposition.APPLIED
        )


def test_history_write_failure_rolls_back_state_and_history(tmp_path: Path) -> None:
    path = tmp_path / "review.sqlite"
    with ReviewStore(path) as store:
        store.create_round("round-1", initial_round())
        store.apply("round-1", START)
        state = store.load_round("round-1")
        history = store.load_history("round-1")
        with sqlite3.connect(path) as connection:
            connection.execute(
                """CREATE TRIGGER reject_history BEFORE INSERT ON history
                BEGIN SELECT RAISE(FAIL, 'injected history failure'); END"""
            )
        completion = Envelope("a", 1, Completed({"report": ["accepted"]}))
        with pytest.raises(sqlite3.IntegrityError, match="injected history failure"):
            store.apply("round-1", completion)
        assert store.load_round("round-1") == state
        assert store.load_history("round-1") == history
        with sqlite3.connect(path) as connection:
            connection.execute("DROP TRIGGER reject_history")
        assert store.apply("round-1", completion) is Disposition.APPLIED
        assert store.load_history("round-1") == history + (
            (completion, Disposition.APPLIED),
        )


def test_another_connection_retry_makes_old_result_history_only(tmp_path: Path) -> None:
    path = tmp_path / "review.sqlite"
    with ReviewStore(path) as first, ReviewStore(path) as second:
        first.create_round("round-1", initial_round())
        first.apply("round-1", START)
        first.apply(
            "round-1", Envelope("a", 1, Error("timeout", FailureCategory.TIMEOUT))
        )
        second.apply("round-1", Envelope("a", 2, AttemptStarted()))
        second.apply("round-1", Envelope("a", 2, Completed("new result")))
        expected = second.load_round("round-1")
        old_result = Envelope("a", 1, Completed("late result", "old-model"))
        assert first.apply("round-1", old_result) is Disposition.HISTORY_ONLY
        assert first.load_round("round-1") == expected
        assert first.load_history("round-1")[-1] == (
            old_result,
            Disposition.HISTORY_ONLY,
        )


@pytest.mark.parametrize("status", list(AttemptStatus))
def test_attempt_status_and_evidence_types_round_trip(
    tmp_path: Path, status: AttemptStatus
) -> None:
    source = EvidenceSource("session_started", "synthetic.init")
    attempt = AttemptState(
        number=1,
        status=status,
        session_id="session-1",
        reported_model="observed-model",
        reported_model_source=source,
        reported_effort="high",
        reported_effort_source=EvidenceSource("session_started", None),
    )
    state = RoundState(
        "snapshot", "prompt", (SeatState(initial_round().seats[0].config, (attempt,)),)
    )
    path = tmp_path / "review.sqlite"
    with ReviewStore(path) as store:
        store.create_round("round-1", state)
    with ReviewStore(path) as reopened:
        restored = reopened.load_round("round-1")
        assert restored == state
        assert isinstance(restored.seats, tuple)
        assert isinstance(restored.seats[0].attempts, tuple)
        assert restored.seats[0].attempts[0].status is status
        assert isinstance(
            restored.seats[0].attempts[0].reported_model_source, EvidenceSource
        )


@pytest.mark.parametrize("category", [None, *FailureCategory])
def test_failure_category_and_retry_metadata_round_trip(
    tmp_path: Path, category: FailureCategory | None
) -> None:
    path = tmp_path / "review.sqlite"
    error = Envelope(
        "a",
        1,
        Error(
            "failure detail",
            category,
            "2026-11-14T10:00:30Z",
            raw_type="synthetic.error",
        ),
    )
    with ReviewStore(path) as store:
        store.create_round("round-1", initial_round())
        store.apply("round-1", START)
        store.apply("round-1", error)
        expected = store.load_round("round-1")
    with ReviewStore(path) as reopened:
        restored = reopened.load_round("round-1")
        assert restored == expected
        attempt = restored.seats[1].attempts[0]
        assert attempt.failure_category is category
        assert attempt.detail == "failure detail"
        assert attempt.retry_after == "2026-11-14T10:00:30Z"
        event = reopened.load_history("round-1")[-1][0].event
        assert isinstance(event, Error)
        assert event.failure_category is category


def test_all_event_fields_and_payloads_survive_reopen(tmp_path: Path) -> None:
    path = tmp_path / "review.sqlite"
    events = (
        START,
        Envelope(
            "a",
            1,
            SessionStarted(
                "session-a", "observed-a", "high", raw_type="synthetic.init"
            ),
        ),
        Envelope("a", 1, Message("审核文本", raw_type="synthetic.message")),
        Envelope(
            "a", 1, Usage({"tokens": 9, "cost": 0.25}, raw_type="synthetic.usage")
        ),
        Envelope("a", 1, CancelRequested()),
        Envelope(
            "a",
            1,
            Completed(
                {"discarded": [True, 7, None]},
                "observed-late",
                raw_type="synthetic.result",
            ),
        ),
        Envelope("a", 1, ProcessExited(130)),
        Envelope(
            "a",
            1,
            Message("late", raw_type="synthetic.message"),
            "2026-11-14T10:00:00Z",
        ),
        Envelope("z", 1, AttemptStarted()),
        Envelope(
            "z",
            1,
            Completed(
                {"nested": [{"中文": "report"}], "empty": []},
                raw_type="synthetic.result",
            ),
        ),
    )
    expected_state = initial_round()
    expected_history = []
    with ReviewStore(path) as store:
        store.create_round("round-1", expected_state)
        for envelope in events:
            expected_state, disposition = core.apply(expected_state, envelope)
            expected_history.append((envelope, disposition))
            assert store.apply("round-1", envelope) is disposition
            assert store.load_round("round-1") == expected_state
    with ReviewStore(path) as reopened:
        assert reopened.load_round("round-1") == expected_state
        assert reopened.load_history("round-1") == tuple(expected_history)
        assert all(
            isinstance(disposition, Disposition)
            for _, disposition in reopened.load_history("round-1")
        )


def test_rounds_are_isolated_and_duplicate_creation_is_atomic(tmp_path: Path) -> None:
    with ReviewStore(tmp_path / "review.sqlite") as store:
        store.create_round("one", initial_round())
        store.create_round("two", initial_round())
        store.apply("one", START)
        expected = store.load_round("one")
        with pytest.raises(sqlite3.IntegrityError):
            store.create_round("one", initial_round())
        assert store.load_round("one") == expected
        assert store.load_history("one") == ((START, Disposition.APPLIED),)
        assert store.load_round("two") == initial_round()
        assert store.load_history("two") == ()
        with pytest.raises(KeyError):
            store.load_round("missing")
        with pytest.raises(KeyError):
            store.apply("missing", START)
