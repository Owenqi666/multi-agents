"""Admission across callers, independent of any model provider protocol."""

import multiprocessing
import os
import sqlite3
import time
from datetime import UTC, datetime, timedelta

import pytest

from multi_agent.store import provider_limits
from multi_agent.store.provider_limits import (
    ProviderExhausted,
    ProviderLedger,
    ProviderLimits,
    ProviderReservation,
    ProviderWait,
)

DAY = datetime(2026, 11, 21, tzinfo=UTC)


def reserve(ledger, limits, *, now=DAY, provider="codex", seconds=8, group=None):
    return ledger.reserve(provider, limits, now, seconds, os.getpid(), group)


def release(ledger, result, elapsed=0):
    assert isinstance(result, ProviderReservation)
    try:
        ledger.finish(result.reservation_id, elapsed, stopped=True)
    finally:
        result.close()


def test_waiting_for_a_slot_and_settlement_are_per_provider_and_directory(tmp_path):
    limits = ProviderLimits(max_concurrent=1, max_calls_per_day=1)
    ledger = ProviderLedger(tmp_path)
    first = reserve(ledger, limits, group=25)
    assert isinstance(first, ProviderReservation)
    assert reserve(ProviderLedger(tmp_path), limits) == ProviderWait("max_concurrent")
    release(ledger, reserve(ledger, limits, provider="claude"))

    other = tmp_path / "other"
    other.mkdir()
    another = ProviderLedger(other)
    release(another, reserve(another, limits))

    assert not ledger.start(first.reservation_id, 26)
    release(ledger, first)  # An unstarted reservation returns its call slot.
    assert not ledger.start(first.reservation_id, 25)

    second = reserve(ledger, limits, group=25)
    assert isinstance(second, ProviderReservation)
    assert ledger.start(second.reservation_id, 25)
    assert not ledger.start(second.reservation_id, 25)
    release(ledger, second, elapsed=3)
    ledger.finish(second.reservation_id, 1, stopped=True)
    assert reserve(ProviderLedger(tmp_path), limits) == ProviderExhausted(
        "max_calls_per_day"
    )
    release(ledger, reserve(ledger, limits, now=DAY + timedelta(days=1)))
    with sqlite3.connect(ledger.path) as connection:
        rows = connection.execute(
            "SELECT started, elapsed_seconds FROM provider_reservations WHERE provider = 'codex' ORDER BY rowid"
        ).fetchall()
    assert rows == [(0, 0), (1, 3), (0, 0)]
    assert not (tmp_path / "reviews.sqlite3").exists()
    assert list((tmp_path / "provider-limit-owners").iterdir()) == []


def test_long_running_turns_occupy_concurrency_across_utc_days(tmp_path):
    ledger = ProviderLedger(tmp_path)
    limits = ProviderLimits(
        max_concurrent=2, max_turn_seconds=10, max_seconds_per_day=12
    )
    today = DAY + timedelta(hours=23, minutes=59)
    tomorrow = today + timedelta(minutes=2)
    first = reserve(ledger, limits, now=today, seconds=11)
    second = reserve(ledger, limits, now=today)
    assert isinstance(first, ProviderReservation)
    assert isinstance(second, ProviderReservation)
    assert (first.run_seconds, first.limit_field) == (10, "max_turn_seconds")
    assert (second.run_seconds, second.limit_field) == (2, "max_seconds_per_day")
    unrestricted_concurrency = ProviderLimits(max_seconds_per_day=12)
    assert reserve(ledger, unrestricted_concurrency, now=today) == ProviderWait(
        "max_seconds_per_day"
    )
    assert reserve(ledger, limits, now=tomorrow) == ProviderWait("max_concurrent")

    assert ledger.start(first.reservation_id, 87)
    release(ledger, first, elapsed=2.25)
    remaining_yesterday = reserve(
        ledger, unrestricted_concurrency, now=today, seconds=15
    )
    assert isinstance(remaining_yesterday, ProviderReservation)
    assert remaining_yesterday.run_seconds == pytest.approx(7.75)
    release(ledger, remaining_yesterday)

    new_day = reserve(ledger, limits, now=tomorrow)
    assert isinstance(new_day, ProviderReservation)
    assert (new_day.run_seconds, new_day.limit_field) == (8, None)
    release(ledger, second)
    release(ledger, new_day)


def test_lost_controller_only_releases_a_confirmed_stopped_group(tmp_path, monkeypatch):
    ledger = ProviderLedger(tmp_path)
    limits = ProviderLimits(
        max_concurrent=1, max_calls_per_day=2, max_seconds_per_day=10
    )
    pending = reserve(ledger, limits, group=44)
    assert isinstance(pending, ProviderReservation)
    pending.close()
    assert not ledger.start(pending.reservation_id, 44)

    active = reserve(ledger, limits, seconds=3)
    assert isinstance(active, ProviderReservation)
    assert ledger.start(active.reservation_id, 44)
    ledger.finish(active.reservation_id, elapsed=1, stopped=False)
    active.close()
    active.close()
    alive = {44}
    monkeypatch.setattr(provider_limits, "_group_alive", alive.__contains__)
    assert reserve(ProviderLedger(tmp_path), limits) == ProviderWait("max_concurrent")

    alive.clear()
    resumed = reserve(ProviderLedger(tmp_path), limits)
    assert isinstance(resumed, ProviderReservation)
    assert resumed.run_seconds == 7  # No verified elapsed time survived the crash.
    ledger.finish(active.reservation_id, elapsed=1, stopped=True)
    assert ledger.start(resumed.reservation_id, 45)
    release(ledger, resumed, elapsed=7)
    assert reserve(ledger, limits) == ProviderExhausted("max_calls_per_day")
    assert reserve(ledger, ProviderLimits(max_seconds_per_day=10)) == ProviderExhausted(
        "max_seconds_per_day"
    )


def test_sqlite_contention_preserves_a_valid_reservation(tmp_path):
    ledger = ProviderLedger(tmp_path)
    limits = ProviderLimits(max_calls_per_day=1)
    result = reserve(ledger, limits)
    assert isinstance(result, ProviderReservation)
    try:
        with sqlite3.connect(ledger.path, isolation_level=None) as writer:
            writer.execute("BEGIN IMMEDIATE")
            assert reserve(ledger, limits) == ProviderWait("ledger_busy")
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                ledger.start(result.reservation_id, 11)
        assert ledger.start(result.reservation_id, 11)
        assert reserve(ledger, limits) == ProviderExhausted("max_calls_per_day")
    finally:
        release(ledger, result)


def _compete(directory, channel):
    with channel:
        assert channel.recv() == "race"
        ledger = ProviderLedger(directory)
        limits = ProviderLimits(max_calls_per_day=1)
        for _ in range(100):
            result = reserve(ledger, limits)
            if result != ProviderWait("ledger_busy"):
                break
            time.sleep(0.005)
        if isinstance(result, ProviderReservation):
            try:
                channel.send("reserved")
                assert channel.recv() == "done"
                ledger.finish(result.reservation_id, 0, stopped=True)
            finally:
                result.close()
        else:
            channel.send(result.field)


def test_last_call_is_reserved_once_across_processes(tmp_path):
    context = multiprocessing.get_context("spawn")
    channels = [context.Pipe() for _ in range(2)]
    workers = []
    try:
        for parent, child in channels:
            process = context.Process(target=_compete, args=(tmp_path, child))
            process.start()
            workers.append(process)
            child.close()
            parent.send("race")
        results = []
        for parent, _ in channels:
            assert parent.poll(10), "contending worker did not report admission"
            results.append(parent.recv())
        assert sorted(results) == ["max_calls_per_day", "reserved"]
        channels[results.index("reserved")][0].send("done")
        for process in workers:
            process.join(10)
            assert process.exitcode == 0
    finally:
        for parent, child in channels:
            parent.close()
            child.close()
        for process in workers:
            if process.is_alive():
                process.terminate()
            process.join(10)
