import json
from pathlib import Path

from multi_agent.core import RoundState, SeatConfig, SeatState
from multi_agent.core.events import AttemptStarted, Completed, Envelope, Error
from multi_agent.store import ReviewStore


def test_snapshots_confirmations_and_late_history_replay(tmp_path: Path) -> None:
    scenario = json.loads(
        (Path(__file__).parent / "scenarios/inputs/history-isolation.json").read_text()
    )
    constructors = {e.kind: e for e in (AttemptStarted, Completed, Error)}
    expected = None
    for replay in range(2):
        path = tmp_path / f"{replay}.db"
        with ReviewStore(path) as store:
            for key in ("old", "new"):
                store.save_snapshot(key, {"candidate": key})
                store.create_review(
                    key,
                    RoundState(
                        key, "v1", (SeatState(SeatConfig("one", "codex", "x", "high")),)
                    ),
                    {},
                )
            history = []
            for row, disposition in zip(
                scenario["events"], scenario["dispositions"], strict=True
            ):
                fields = dict(row)
                seat, attempt = fields.pop("seat"), fields.pop("attempt")
                event = constructors[fields.pop("type")](**fields)
                envelope = Envelope(seat, attempt, event)
                assert store.apply("old", envelope) == disposition
                history.append((envelope, disposition))
            current = store.load_round("old")
            assert current.seats[0].attempts[-1].result == {"report": "current"}
            assert store.load_history("old") == tuple(history)
            assert store.load_round("new").seats[0].attempts == ()
            store.create_review(
                "confirm",
                RoundState("old", "confirm-v1", (SeatState(current.seats[0].config),)),
                {},
                "old",
            )
        with ReviewStore(path) as reopened:
            assert reopened.load_round("old") == current
            assert reopened.load_history("old") == tuple(history)
            assert reopened.confirmation_for("old") == "confirm"
            assert reopened.load_snapshot("new") == {"candidate": "new"}
            observed = (current, reopened.load_history("old"))
            if expected is None:
                expected = observed
            assert observed == expected
