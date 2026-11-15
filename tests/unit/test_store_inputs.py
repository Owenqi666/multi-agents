import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from multi_agent.core import RoundState, SeatConfig, SeatState
from multi_agent.core.events import AttemptStarted, Completed, Envelope, freeze_json
from multi_agent.store import ReviewStore, database


def state(snapshot: str = "frozen") -> RoundState:
    return RoundState(
        snapshot, "review-v1", (SeatState(SeatConfig("one", "codex", "x", "high")),)
    )


def test_snapshot_and_execution_are_immutable_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "review.db"
    payload = {"candidate": "abc", "requirements": "do it", "files": []}
    with ReviewStore(path) as store:
        store.save_snapshot("frozen", payload)
        store.save_snapshot("frozen", dict(reversed(tuple(payload.items()))))
        store.save_snapshot("frozen", freeze_json(payload))
        store.save_snapshot("typed", {"nested": (True,)})
        store.save_snapshot("typed", {"nested": [True]})
        with pytest.raises(ValueError, match="different content"):
            store.save_snapshot("typed", {"nested": [1]})
        with pytest.raises(ValueError, match="different content"):
            store.save_snapshot("frozen", {"candidate": "bad"})
        store.create_review("r", state(), {"created_at": "fixed"})
        with pytest.raises(sqlite3.IntegrityError):
            store.set_execution("r", "one", 1, {"version": "reported"})
        store.apply("r", Envelope("one", 1, AttemptStarted()))
        store.set_execution("r", "one", 1, {"version": "reported"})
        store.set_execution("r", "one", 1, {"version": "reported"})
        with pytest.raises(ValueError, match="immutable"):
            store.set_execution("r", "one", 1, {"version": "invented"})
        store.apply("r", Envelope("one", 1, Completed(None)))
        store.create_review("typed", state(), {})
        store.apply("typed", Envelope("one", 1, AttemptStarted()))
        store.set_execution("typed", "one", 1, freeze_json({"usage": [True]}))
        store.set_execution("typed", "one", 1, {"usage": (True,)})
        with pytest.raises(ValueError, match="immutable"):
            store.set_execution("typed", "one", 1, {"usage": [1]})
        history = store.load_history("r")
    with ReviewStore(path) as store:
        assert store.load_snapshot("frozen") == payload
        assert store.load_review("r") == {"created_at": "fixed"}
        assert store.load_execution("r") == {"one": {"1": {"version": "reported"}}}
        assert store.list_reviews() == ("r", "typed")
        assert store.load_history("r") == history
        with pytest.raises(KeyError):
            store.load_snapshot("missing")
        with pytest.raises(KeyError):
            store.load_review("missing")


def test_child_unique_parent_and_atomic_insert(tmp_path: Path) -> None:
    path = tmp_path / "review.db"
    with ReviewStore(path) as first, ReviewStore(path) as second:
        first.save_snapshot("frozen", {})
        first.create_review("parent", state(), {})
        first.create_review("child", state(), {"kind": "confirmation"}, "parent")
        with pytest.raises(sqlite3.IntegrityError):
            second.create_review("second", state(), {}, "parent")
        with pytest.raises(sqlite3.IntegrityError):
            second.create_review("orphan", state(), {}, "missing")
        with pytest.raises(sqlite3.IntegrityError):
            second.create_review("unknown-snapshot", state("missing"), {})
        assert second.list_reviews() == ("parent", "child")
        assert second.confirmation_for("parent") == "child"
        assert second.confirmation_for("child") is None
        for key in ("second", "orphan", "unknown-snapshot"):
            with pytest.raises(KeyError):
                second.load_round(key)


def test_competing_confirmation_writers_create_only_one_child(tmp_path: Path) -> None:
    path = tmp_path / "race.db"
    with ReviewStore(path) as store:
        store.save_snapshot("frozen", {})
        store.create_review("parent", state(), {})
    barrier = Barrier(2)

    def compete(name: str) -> str | None:
        with ReviewStore(path) as store:
            barrier.wait(timeout=5)
            try:
                store.create_review(name, state(), {}, "parent")
            except sqlite3.IntegrityError:
                return None
            return name

    with ThreadPoolExecutor(max_workers=2) as pool:
        winners = list(pool.map(compete, ("one", "two")))
    assert winners.count(None) == 1
    winner = next(name for name in winners if name is not None)
    with ReviewStore(path) as store:
        assert store.list_reviews() == ("parent", winner)
        assert store.confirmation_for("parent") == winner
        with pytest.raises(KeyError):
            store.load_round("two" if winner == "one" else "one")


def test_actual_version_one_upgrade_preserves_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "old.db"
    resources = tmp_path / "v1"
    (resources / "migrations").mkdir(parents=True)
    (resources / "migrations/0001_review.sql").write_text(
        Path("src/multi_agent/store/migrations/0001_review.sql").read_text()
    )
    with monkeypatch.context() as patch:
        patch.setattr(database, "files", lambda package: resources)
        with ReviewStore(path) as store:
            store.create_round("old", state("snapshot"))
            store.apply("old", Envelope("one", 1, AttemptStarted()))
            store.apply("old", Envelope("one", 1, Completed({"report": "kept"})))
            history = store.load_history("old")
            original = store.load_round("old")
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (1,)
    with ReviewStore(path) as store:
        assert store.load_round("old") == original
        assert store.load_history("old") == history
        assert store.load_round("old").seats[0].attempts[0].result == {"report": "kept"}
        assert store.list_reviews() == ()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (2,)
