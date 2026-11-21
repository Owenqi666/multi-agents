"""Replay persisted local accounting through controller loss and a new UTC day."""

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from multi_agent.store import provider_limits
from multi_agent.store.provider_limits import (
    ProviderExhausted,
    ProviderLedger,
    ProviderLimits,
    ProviderReservation,
)


def test_provider_limits_crash_recovery_and_late_settlement(tmp_path, monkeypatch):
    scenario = json.loads(
        (
            Path(__file__).parent / "scenarios/provider_limits_crash_recovery.ledger"
        ).read_text(encoding="utf-8")
    )
    limits = ProviderLimits(**scenario["limits"])
    ledger = ProviderLedger(tmp_path)
    turns = {}
    groups = set()
    monkeypatch.setattr(provider_limits, "_group_alive", groups.__contains__)
    try:
        for step in scenario["steps"]:
            match step["action"]:
                case "reserve":
                    result = ledger.reserve(
                        "codex",
                        limits,
                        datetime.fromisoformat(step["at"]),
                        step["requested"],
                        os.getpid(),
                    )
                    if "exhausted" in step:
                        assert result == ProviderExhausted(step["exhausted"])
                    else:
                        assert isinstance(result, ProviderReservation)
                        turns[step["turn"]] = result
                        assert result.run_seconds == step["granted"]
                case "start":
                    group = step["process_group"]
                    assert ledger.start(turns[step["turn"]].reservation_id, group)
                    groups.add(group)
                case "lose_controller":
                    turns[step["turn"]].close()
                    ledger = ProviderLedger(tmp_path)
                case "stop_group":
                    groups.remove(step["process_group"])
                case "finish":
                    reservation = turns[step["turn"]]
                    ledger.finish(
                        reservation.reservation_id, step["elapsed"], stopped=True
                    )
                    reservation.close()
                case _:
                    raise AssertionError(f"unrecognized scenario step {step}")
    finally:
        for reservation in turns.values():
            reservation.close()

    with sqlite3.connect(ledger.path) as connection:
        history = connection.execute(
            """SELECT utc_day, started, active, reserved_seconds, elapsed_seconds
               FROM provider_reservations ORDER BY rowid"""
        ).fetchall()
    assert [list(row) for row in history] == scenario["history"]
