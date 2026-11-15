"""Phase-one CLI tests using scripted adapters and local fake executables only."""

import asyncio
import json
import os
import sys
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

import pytest

from multi_agent.adapters.base import EnvironmentInfo, Permission
from multi_agent.adapters.probe.recording import Recorder, Redactor
from multi_agent.adapters.runtime import RuntimeAdapter, RuntimeFailure
from multi_agent.adapters.scripted import ScriptedAdapter, TurnScript
from multi_agent.cli import main as cli
from multi_agent.cli import runner
from multi_agent.core import (
    AttemptStatus,
    Disposition,
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
)
from multi_agent.core.snapshot import (
    FrozenFile,
    FrozenSnapshot,
)
from multi_agent.core.snapshot import (
    TestEvidence as EvidenceRecord,
)
from multi_agent.store import ReviewStore

GOOD = {"summary": "No issues found", "findings": []}


@pytest.fixture(autouse=True)
def synthetic_codex_auth_and_fake_environment(tmp_path, monkeypatch):
    auth_home = tmp_path / "synthetic-codex-auth"
    auth_home.mkdir(mode=0o700)
    auth = auth_home / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    key: "synthetic-" + key
                    for key in (
                        "id_token",
                        "access_token",
                        "refresh_token",
                        "account_id",
                    )
                },
            }
        )
    )
    auth.chmod(0o600)
    monkeypatch.setenv("CODEX_HOME", str(auth_home))
    original = RuntimeAdapter._environment

    def fake_environment(adapter):
        environment = original(adapter)
        environment.update(
            (key, value) for key, value in os.environ.items() if key.startswith("FAKE_")
        )
        return environment

    monkeypatch.setattr(RuntimeAdapter, "_environment", fake_environment)


def report(identifier="issue-1", severity="P1", title="boundary defect"):
    return {
        "summary": title,
        "findings": [
            {
                "id": identifier,
                "severity": severity,
                "category": "bounds",
                "side": "candidate",
                "path": "source.py",
                "line_start": 1,
                "line_end": 1,
                "quote": "return values[index]",
                "title": title,
                "impact": "Out-of-range callers fail",
                "rationale": "Index has no guard",
            }
        ],
    }


def script(result=GOOD, *, error=None, model=None):
    return TurnScript(
        (
            SessionStarted("synthetic-session", raw_type="fixture/session"),
            Message("synthetic progress", raw_type="fixture/message"),
            error
            if error is not None
            else Completed(result, model, raw_type="fixture/result"),
            ProcessExited(0, raw_type="fixture/exit"),
        ),
        (Error("stopped", FailureCategory.CANCELLED), ProcessExited(0)),
    )


def snapshot(repo: Path, *, tested=True, candidate="b" * 40):
    command = ("python", "-m", "declared_tests")
    return FrozenSnapshot(
        str(repo),
        "a" * 40,
        candidate,
        "-return None\n+return values[index]\n",
        "Review the frozen boundary change",
        ("Existing callers continue to work",),
        (EvidenceRecord(candidate, command, 0, "user supplied passing suite"),)
        if tested
        else (),
        (
            FrozenFile("baseline", "source.py", "return None\n", "c" * 40, "100644"),
            FrozenFile(
                "candidate", "source.py", "return values[index]\n", "d" * 40, "100644"
            ),
        ),
        (command,) if tested else (),
    )


class RecordingAdapter(ScriptedAdapter):
    def __init__(self, owner, seat, planned, environment):
        super().__init__((planned,), environment=environment)
        self.owner = owner
        self.seat = seat

    async def check_environment(self):
        self.owner.preflights.append(self.seat.key)
        if self.owner.preflight_error is not None:
            raise self.owner.preflight_error
        return await super().check_environment()

    async def start_session(self, config):
        self.owner.sessions.append((self.seat, config))
        self.owner.visible_files.append(
            (config.cwd / "source" / "candidate" / "source.py").read_text()
        )
        self.owner.contexts.append(
            {
                str(path.relative_to(config.cwd)): path.read_text()
                for path in config.cwd.rglob("*")
                if path.is_file()
            }
        )
        return await super().start_session(config)

    async def submit_turn(self, session, text):
        self.owner.prompts.append((self.seat.key, text))
        if self.owner.parallel_count:
            if len(self.owner.prompts) == self.owner.parallel_count:
                self.owner.barrier.set()
            await asyncio.wait_for(self.owner.barrier.wait(), 2)
        return await super().submit_turn(session, text)

    async def cancel(self, turn, *, on_accepted=None):
        self.owner.cancelled.append(self.seat.key)
        return await super().cancel(turn, on_accepted=on_accepted)


class Harness:
    def __init__(self, tmp_path, monkeypatch, capsys):
        self.data = tmp_path / "review-data"
        self.data.mkdir()
        self.repo = tmp_path / "repository"
        self.repo.mkdir()
        self.capsys = capsys
        self.planned = defaultdict(list)
        self.factories = []
        self.sessions = []
        self.visible_files = []
        self.contexts = []
        self.prompts = []
        self.preflights = []
        self.cancelled = []
        self.parallel_count = 0
        self.barrier = asyncio.Event()
        self.preflight_error = None
        self.environment = EnvironmentInfo("synthetic-v1", True, None)
        self.frozen = snapshot(self.repo)
        self.save(self.frozen)
        monkeypatch.setattr(runner, "make_adapter", self.factory)

    def save(self, frozen):
        with ReviewStore(self.data / "reviews.sqlite3") as store:
            store.save_snapshot(frozen.snapshot_id, frozen.to_dict())

    def factory(self, config, log_dir, metadata, schema):
        self.factories.append((config, log_dir, metadata, schema))
        planned = (
            self.planned[config.key].pop(0) if self.planned[config.key] else script()
        )
        return RecordingAdapter(self, config, planned, self.environment)

    def invoke(self, *args, code=0):
        try:
            actual = cli.main(["--data-dir", str(self.data), *args])
        except SystemExit as error:
            actual = error.code
        captured = self.capsys.readouterr()
        assert actual == code, captured.err
        return json.loads(captured.out) if captured.out.strip() else None

    def run(self, *extra, dual=False, identifier=None, code=0):
        args = [
            "run",
            identifier or self.frozen.snapshot_id,
            "--seat",
            "codex",
            "model-one",
            "high",
        ]
        if dual:
            args += ["--seat", "claude", "model-two", "high"]
        return self.invoke(*args, *extra, code=code)

    def state(self, round_id):
        with ReviewStore(self.data / "reviews.sqlite3") as store:
            return store.load_round(round_id)

    def history(self, round_id):
        with ReviewStore(self.data / "reviews.sqlite3") as store:
            return store.load_history(round_id)


@pytest.fixture
def h(tmp_path, monkeypatch, capsys):
    return Harness(tmp_path, monkeypatch, capsys)


def test_single_seat_only_selected_session_and_reported_evidence(h):
    output = h.run()
    assert output["status"] == "ready_for_assessment"
    assert output["assessment"]["status"] == "ready"
    assert len(h.factories) == len(h.sessions) == len(h.prompts) == 1
    config = h.sessions[0][1]
    assert config.permission == Permission.READ_ONLY
    assert config.cwd != h.repo
    assert not h.factories[0][1].is_relative_to(h.repo)
    assert not h.factories[0][1].is_relative_to(config.cwd)
    assert h.visible_files == ["return values[index]\n"]
    attempt = h.state(output["round_id"]).seats[0].attempts[-1]
    assert attempt.reported_model is None and attempt.reported_effort is None
    assert attempt.reported_model_source is None
    assert output["assessment"]["tests"]["source"] == "user_supplied"


def test_dual_review_runs_parallel_identical_blind_input(h):
    h.parallel_count = 2
    h.planned["reviewer-1"] = [script(report(title="PRIVATE FIRST REPORT"))]
    output = h.run(dual=True)
    assert output["status"] == "ready_for_assessment"
    assert output["assessment"]["status"] == "needs_rework"
    assert len(h.sessions) == 2
    assert h.sessions[0][1].cwd != h.sessions[1][1].cwd
    assert h.prompts[0][1] == h.prompts[1][1]
    assert "PRIVATE FIRST REPORT" not in h.prompts[1][1]
    assert h.contexts[0] == h.contexts[1]
    assert (
        json.loads(h.contexts[0]["source/snapshot.json"])["snapshot_id"]
        == h.frozen.snapshot_id
    )
    assert "PRIVATE FIRST REPORT" not in json.dumps(h.contexts[1])


def test_missing_test_evidence_does_not_become_ready(h):
    untested = snapshot(h.repo, tested=False)
    h.save(untested)
    output = h.run(identifier=untested.snapshot_id)
    assert output["assessment"]["status"] == "user_decision"
    assert "complete_candidate_test_evidence_missing" in output["assessment"]["reasons"]


def test_bad_report_never_enters_successful_terminal_history(h):
    h.planned["reviewer-1"] = [
        script({"summary": "malformed", "findings": "not-an-array"})
    ]
    output = h.run()
    assert output["status"] == "incomplete"
    assert output["assessment"]["status"] == "incomplete"
    history = h.history(output["round_id"])
    assert any(isinstance(e.event, Error) for e, _ in history)
    assert not any(isinstance(e.event, Completed) for e, _ in history)


def test_failure_keeps_other_report_then_retries_only_failed_seat(h):
    h.planned["reviewer-2"] = [
        script(error=Error("limited", FailureCategory.RATE_LIMITED, "tomorrow"))
    ]
    first = h.run(dual=True)
    assert first["status"] == "incomplete"
    assert first["assessment"]["groups"] == []
    before = h.state(first["round_id"])
    successful = before.seats[0]
    retried = h.invoke("retry", first["round_id"])
    after = h.state(first["round_id"])
    assert retried["assessment"]["status"] == "ready"
    assert after.snapshot_id == before.snapshot_id
    assert after.seats[0] == successful
    assert after.seats[1].config == before.seats[1].config
    assert [a.number for a in after.seats[1].attempts] == [1, 2]
    assert [config.key for config, *_ in h.factories] == [
        "reviewer-1",
        "reviewer-2",
        "reviewer-2",
    ]
    h.invoke("retry", first["round_id"], "--seat", "reviewer-1", code=2)
    assert len(h.factories) == 3


def test_late_old_result_only_enters_history_after_retry(h):
    h.planned["reviewer-1"] = [script(error=Error("timeout", FailureCategory.TIMEOUT))]
    first = h.run()
    h.invoke("retry", first["round_id"])
    before = h.state(first["round_id"])
    with ReviewStore(h.data / "reviews.sqlite3") as store:
        disposition = store.apply(
            first["round_id"],
            Envelope("reviewer-1", 1, Completed(report(title="late stale report"))),
        )
    assert disposition == Disposition.HISTORY_ONLY
    assert h.state(first["round_id"]) == before
    shown = h.invoke("show", first["round_id"], "--history")
    assert shown["assessment"]["status"] == "ready"
    assert shown["history"]
    assert h.history(first["round_id"])[-1][1] == Disposition.HISTORY_ONLY


def test_new_snapshot_does_not_rebind_old_report(h):
    old = h.run()
    changed = replace(h.frozen, candidate="e" * 40, requirements="Revised request")
    h.save(changed)
    new = h.run(identifier=changed.snapshot_id)
    assert old["round_id"] != new["round_id"]
    assert h.state(old["round_id"]).snapshot_id == h.frozen.snapshot_id
    assert h.state(new["round_id"]).snapshot_id == changed.snapshot_id
    old_shown = h.invoke("show", old["round_id"], "--history")
    assert old_shown["assessment"]["status"] == "ready"
    assert len(h.history(old["round_id"])) > 0


@pytest.mark.parametrize(
    "extra",
    [
        ["--seat", "claude", "model-one", "high"],
        [
            "--seat",
            "claude",
            "model-two",
            "high",
            "--seat",
            "codex",
            "model-three",
            "high",
        ],
        ["--timeout", "0"],
        ["--timeout", "nan"],
        ["--grace", "-1"],
    ],
)
def test_invalid_run_parameters_do_not_start_adapters(h, extra):
    h.run(*extra, code=2)
    assert h.factories == []


def test_invalid_provider_does_not_start_adapter(h):
    h.invoke("run", h.frozen.snapshot_id, "--seat", "unknown", "model", "high", code=2)
    assert h.factories == []


@pytest.mark.parametrize(
    "environment",
    [
        EnvironmentInfo("v", False, None),
        EnvironmentInfo("v", True, ("different-model",)),
        EnvironmentInfo("v", True, ("model-one",), {"model-one": ("low",)}),
    ],
)
def test_preflight_rejection_never_submits_a_turn(h, environment):
    h.environment = environment
    output = h.run()
    assert output["status"] == "incomplete"
    assert h.prompts == []
    assert (
        h.state(output["round_id"]).seats[0].attempts[-1].status == AttemptStatus.FAILED
    )


def test_preflight_normalized_error_is_preserved(h):
    h.preflight_error = RuntimeFailure(
        Error("auth expired", FailureCategory.AUTH_EXPIRED)
    )
    output = h.run()
    assert h.prompts == []
    attempt = h.state(output["round_id"]).seats[0].attempts[-1]
    assert attempt.failure_category == FailureCategory.AUTH_EXPIRED


def unfinished(h, *, cancelling=False):
    config = SeatConfig("reviewer-1", "codex", "model-one", "high")
    with ReviewStore(h.data / "reviews.sqlite3") as store:
        store.create_review(
            "interrupted",
            RoundState(h.frozen.snapshot_id, "review-v1", (SeatState(config),)),
            {
                "kind": "review",
                "created_at": "fixed",
                "timeout": 180,
                "grace": 2,
                "executables": {"codex": "codex", "claude": "claude"},
                "parent_round_id": None,
            },
        )
        store.apply("interrupted", Envelope(config.key, 1, AttemptStarted()))
        if cancelling:
            store.apply("interrupted", Envelope(config.key, 1, CancelRequested()))


def test_recovery_is_explicit_and_does_not_fabricate_old_process_exit(h):
    unfinished(h)
    h.invoke("show", "interrupted")
    h.invoke("retry", "interrupted", code=2)
    assert h.factories == []
    recovered = h.invoke("retry", "interrupted", "--recover")
    assert recovered["assessment"]["status"] == "ready"
    state = h.state("interrupted")
    assert len(state.seats[0].attempts) == 2
    original = state.seats[0].attempts[0]
    assert original.status == AttemptStatus.FAILED
    assert original.exit_code is None
    assert original.failure_category is None


def test_unconfirmed_cancellation_cannot_be_recovered_as_stopped(h):
    unfinished(h, cancelling=True)
    h.invoke("retry", "interrupted", "--recover", code=2)
    assert h.factories == []
    assert (
        h.state("interrupted").seats[0].attempts[0].status == AttemptStatus.CANCELLING
    )


def test_execution_lock_blocks_dispatch_but_allows_show(h):
    initial = h.run()
    with runner.execution_lock(h.data):
        h.run(code=2)
        shown = h.invoke("show", initial["round_id"])
        assert shown["round_id"] == initial["round_id"]
    assert len(h.factories) == 1


def test_confirmation_is_unique_and_binds_actual_source_seat(h):
    h.planned["reviewer-1"] = [script(report())]
    parent = h.run(dual=True)
    h.planned["reviewer-1"] = [
        script({"summary": "No opposite blockers", "confirmations": []})
    ]
    h.planned["reviewer-2"] = [
        script(
            {
                "summary": "Reviewed blocker",
                "confirmations": [
                    {
                        "target_seat": "reviewer-1",
                        "finding_id": "issue-1",
                        "decision": "disagree",
                        "reason": "Caller validates the range",
                    }
                ],
            }
        )
    ]
    h.invoke("confirm", parent["round_id"])
    with ReviewStore(h.data / "reviews.sqlite3") as store:
        child_id = store.confirmation_for(parent["round_id"])
        assert child_id is not None
        child = store.load_round(child_id)
        assert child.snapshot_id == h.frozen.snapshot_id
        original = store.load_round(parent["round_id"])
        for seat in child.seats:
            assert seat.config == next(
                s.config for s in original.seats if s.config.key == seat.config.key
            )
        assert store.load_review(child_id)["parent_round_id"] == parent["round_id"]
    shown = h.invoke("show", parent["round_id"])
    assert shown["assessment"]["status"] == "user_decision"
    confirmation = shown["assessment"]["groups"][0]["confirmations"][0]
    assert confirmation["seat"] == "reviewer-2"
    factories = len(h.factories)
    h.invoke("confirm", parent["round_id"], code=2)
    assert len(h.factories) == factories


@pytest.mark.parametrize(
    "result",
    [
        {
            "summary": "spoof source",
            "confirmations": [
                {
                    "seat": "reviewer-1",
                    "target_seat": "reviewer-1",
                    "finding_id": "issue-1",
                    "decision": "agree",
                    "reason": "claimed",
                }
            ],
        },
        {
            "summary": "self confirmation",
            "confirmations": [
                {
                    "target_seat": "reviewer-2",
                    "finding_id": "other",
                    "decision": "agree",
                    "reason": "self",
                }
            ],
        },
        {
            "summary": "unknown target",
            "confirmations": [
                {
                    "target_seat": "reviewer-1",
                    "finding_id": "missing",
                    "decision": "agree",
                    "reason": "claimed",
                }
            ],
        },
    ],
)
def test_invalid_confirmation_cannot_become_success(h, result):
    h.planned["reviewer-1"] = [script(report())]
    parent = h.run(dual=True)
    h.planned["reviewer-1"] = [script({"summary": "None", "confirmations": []})]
    h.planned["reviewer-2"] = [script(result)]
    h.invoke("confirm", parent["round_id"])
    with ReviewStore(h.data / "reviews.sqlite3") as store:
        child_id = store.confirmation_for(parent["round_id"])
        child = store.load_round(child_id)
        bad = next(s for s in child.seats if s.config.key == "reviewer-2")
        assert bad.attempts[-1].status == AttemptStatus.FAILED
        assert not any(
            e.seat == "reviewer-2" and isinstance(e.event, Completed)
            for e, _ in store.load_history(child_id)
        )


def test_confirmation_rejected_without_two_complete_reports(h):
    single = h.run()
    h.invoke("confirm", single["round_id"], code=2)
    h.planned["reviewer-2"] = [script(error=Error("failed"))]
    incomplete = h.run(dual=True)
    h.invoke("confirm", incomplete["round_id"], code=2)
    assert len(h.factories) == 3


def test_only_blocking_findings_are_sent_for_cross_confirmation(h):
    h.planned["reviewer-1"] = [
        script(report(severity="P2", title="PRIVATE NONBLOCKING"))
    ]
    parent = h.run(dual=True)
    h.invoke("confirm", parent["round_id"], code=2)
    assert len(h.factories) == 2


def test_unanswered_confirmation_is_not_agreement(h):
    h.planned["reviewer-1"] = [script(report())]
    parent = h.run(dual=True)
    for key in ("reviewer-1", "reviewer-2"):
        h.planned[key] = [
            script({"summary": "No explicit conclusion", "confirmations": []})
        ]
    h.invoke("confirm", parent["round_id"])
    shown = h.invoke("show", parent["round_id"])
    assert shown["assessment"]["status"] != "ready"
    assert shown["assessment"]["groups"][0]["confirmations"] == []
    assert shown["unanswered_confirmations"] == [
        {"seat": "reviewer-2", "target_seat": "reviewer-1", "finding_id": "issue-1"}
    ]
    with ReviewStore(h.data / "reviews.sqlite3") as store:
        child = store.load_round(store.confirmation_for(parent["round_id"]))
        result = next(
            s.attempts[-1].result for s in child.seats if s.config.key == "reviewer-2"
        )
        assert result["confirmations"] == ()


def test_cross_confirmation_failure_retries_existing_child_only(h):
    h.planned["reviewer-1"] = [script(report())]
    parent = h.run(dual=True)
    h.planned["reviewer-1"] = [script({"summary": "None", "confirmations": []})]
    h.planned["reviewer-2"] = [script(error=Error("timeout", FailureCategory.TIMEOUT))]
    h.invoke("confirm", parent["round_id"])
    with ReviewStore(h.data / "reviews.sqlite3") as store:
        child_id = store.confirmation_for(parent["round_id"])
    h.planned["reviewer-2"] = [
        script(
            {
                "summary": "Confirmed",
                "confirmations": [
                    {
                        "target_seat": "reviewer-1",
                        "finding_id": "issue-1",
                        "decision": "agree",
                        "reason": "Frozen line lacks a guard",
                    }
                ],
            }
        )
    ]
    h.invoke("retry", child_id)
    with ReviewStore(h.data / "reviews.sqlite3") as store:
        assert store.confirmation_for(parent["round_id"]) == child_id
        child = store.load_round(child_id)
        target = next(s for s in child.seats if s.config.key == "reviewer-2")
        assert target.current_attempt == 2
    shown = h.invoke("show", parent["round_id"])
    assert shown["assessment"]["status"] == "needs_rework"


def test_run_rejects_data_inside_target_repository(h):
    h.data = h.repo / "inside-data"
    h.data.mkdir()
    h.save(h.frozen)
    h.run(code=2)
    assert h.factories == []


def test_unknown_snapshot_never_starts_runtime(h):
    h.run(identifier="missing", code=2)
    assert h.factories == []


def test_execute_round_stop_confirms_cancel_and_keeps_success(h, monkeypatch):
    stop = asyncio.Event()
    first_done = asyncio.Event()
    config = (
        SeatConfig("reviewer-1", "codex", "model-one", "high"),
        SeatConfig("reviewer-2", "claude", "model-two", "high"),
    )

    class CancellableAdapter(RecordingAdapter):
        async def events(self, turn):
            if self.seat.key == "reviewer-2":
                await asyncio.wait_for(first_done.wait(), 2)
                stop.set()
                # Await the runner's actual cancel call, rather than fabricating
                # a stop terminal merely because the request flag was set.
                async with asyncio.timeout(2):
                    while self.seat.key not in self.owner.cancelled:
                        await asyncio.sleep(0)
            async for event in super().events(turn):
                yield event
            if self.seat.key == "reviewer-1":
                first_done.set()

    def factory(seat, log_dir, metadata, schema):
        return CancellableAdapter(h, seat, script(), h.environment)

    monkeypatch.setattr(runner, "make_adapter", factory)
    with ReviewStore(h.data / "reviews.sqlite3") as store:
        store.create_review(
            "cancel-flow",
            RoundState(
                h.frozen.snapshot_id,
                "review-v1",
                tuple(SeatState(seat) for seat in config),
            ),
            {
                "kind": "review",
                "created_at": "fixed",
                "timeout": 180,
                "grace": 2,
                "executables": {"codex": "codex", "claude": "claude"},
                "parent_round_id": None,
            },
        )
        with runner.execution_lock(h.data):
            asyncio.run(runner.execute_round(store, "cancel-flow", h.data, stop=stop))
        state = store.load_round("cancel-flow")
        history = store.load_history("cancel-flow")
    assert state.seats[0].attempts[-1].status == AttemptStatus.SUCCEEDED
    assert state.seats[1].attempts[-1].failure_category == FailureCategory.CANCELLED
    assert state.seats[1].attempts[-1].exit_code == 0
    assert h.cancelled == ["reviewer-2"]
    request = next(
        i
        for i, (e, _) in enumerate(history)
        if e.seat == "reviewer-2" and isinstance(e.event, CancelRequested)
    )
    terminal = next(
        i
        for i, (e, _) in enumerate(history)
        if e.seat == "reviewer-2" and isinstance(e.event, Error)
    )
    assert request < terminal


@pytest.mark.parametrize(
    "field", ["summary", "target_seat", "finding_id", "decision", "reason"]
)
def test_confirmation_invalid_utf8_fails_before_persistence(h, field):
    h.planned["reviewer-1"] = [script(report())]
    parent = h.run(dual=True)
    result = {
        "summary": "Confirmed",
        "confirmations": [
            {
                "target_seat": "reviewer-1",
                "finding_id": "issue-1",
                "decision": "agree",
                "reason": "The cited line lacks a guard",
            }
        ],
    }
    if field == "summary":
        result[field] = "invalid \ud800 text"
    else:
        result["confirmations"][0][field] = "invalid \ud800 text"
    h.planned["reviewer-2"] = [script(result)]
    h.invoke("confirm", parent["round_id"])
    with ReviewStore(h.data / "reviews.sqlite3") as store:
        child_id = store.confirmation_for(parent["round_id"])
        child = store.load_round(child_id)
        attempt = child.seats[0].attempts[-1]
        assert attempt.status == AttemptStatus.FAILED
        assert attempt.result is None
        assert not any(
            isinstance(envelope.event, Completed)
            for envelope, _ in store.load_history(child_id)
        )


def test_cancelled_malformed_report_cannot_become_success(h, monkeypatch):
    stop = asyncio.Event()

    class MalformedOnCancel(RecordingAdapter):
        async def submit_turn(self, session, text):
            turn = await super().submit_turn(session, text)
            stop.set()
            return turn

        async def events(self, turn):
            async with asyncio.timeout(2):
                while self.seat.key not in self.owner.cancelled:
                    await asyncio.sleep(0)
            async for event in super().events(turn):
                yield event

    def factory(seat, log_dir, metadata, schema):
        planned = TurnScript(
            (Completed(GOOD), ProcessExited(0)),
            (Completed({"summary": "bad", "findings": "malformed"}), ProcessExited(0)),
        )
        return MalformedOnCancel(h, seat, planned, h.environment)

    monkeypatch.setattr(runner, "make_adapter", factory)
    seat = SeatConfig("reviewer-1", "codex", "model-one", "high")
    with ReviewStore(h.data / "reviews.sqlite3") as store:
        store.create_review(
            "cancel-malformed",
            RoundState(h.frozen.snapshot_id, "review-v1", (SeatState(seat),)),
            {
                "kind": "review",
                "created_at": "fixed",
                "timeout": 180,
                "grace": 2,
                "executables": {"codex": "codex", "claude": "claude"},
                "parent_round_id": None,
            },
        )
        with runner.execution_lock(h.data):
            asyncio.run(
                runner.execute_round(store, "cancel-malformed", h.data, stop=stop)
            )
        attempt = store.load_round("cancel-malformed").seats[0].attempts[-1]
        history = store.load_history("cancel-malformed")
    assert attempt.status == AttemptStatus.FAILED
    assert attempt.failure_category == FailureCategory.CANCELLED
    assert attempt.result is None and attempt.exit_code == 0
    assert any(isinstance(envelope.event, CancelRequested) for envelope, _ in history)
    assert not any(isinstance(envelope.event, Completed) for envelope, _ in history)


@pytest.mark.parametrize("diagnostic", [False, True])
def test_cancellation_without_terminal_or_exit_stays_unconfirmed(
    h, monkeypatch, diagnostic
):
    stop = asyncio.Event()

    class UnconfirmedAdapter(RecordingAdapter):
        async def submit_turn(self, session, text):
            turn = await super().submit_turn(session, text)
            stop.set()
            return turn

        async def events(self, turn):
            yield SessionStarted("unconfirmed-process")
            async with asyncio.timeout(2):
                while self.seat.key not in self.owner.cancelled:
                    await asyncio.sleep(0)
            if diagnostic:
                yield Error(
                    "process stop remains unconfirmed",
                    raw_type="adapter.cleanup_incomplete",
                )

    def factory(seat, log_dir, metadata, schema):
        return UnconfirmedAdapter(h, seat, script(), h.environment)

    monkeypatch.setattr(runner, "make_adapter", factory)
    seat = SeatConfig("reviewer-1", "codex", "model-one", "high")
    with ReviewStore(h.data / "reviews.sqlite3") as store:
        store.create_review(
            "unconfirmed-cancel",
            RoundState(h.frozen.snapshot_id, "review-v1", (SeatState(seat),)),
            {
                "kind": "review",
                "created_at": "fixed",
                "timeout": 180,
                "grace": 2,
                "executables": {"codex": "codex", "claude": "claude"},
                "parent_round_id": None,
            },
        )
        with runner.execution_lock(h.data), pytest.raises(ValueError):
            asyncio.run(
                runner.execute_round(store, "unconfirmed-cancel", h.data, stop=stop)
            )
        attempt = store.load_round("unconfirmed-cancel").seats[0].attempts[-1]
        history = store.load_history("unconfirmed-cancel")
        execution = store.load_execution("unconfirmed-cancel")
    assert attempt.status == AttemptStatus.CANCELLING
    assert attempt.failure_category is None and attempt.exit_code is None
    assert not any(
        isinstance(envelope.event, (Error, Completed, ProcessExited))
        for envelope, _ in history
    )
    assert any(isinstance(envelope.event, CancelRequested) for envelope, _ in history)
    assert execution["reviewer-1"]["1"]["completion_unconfirmed"]


@pytest.fixture
def runtime_executables(tmp_path):
    source = (Path(__file__).parent / "runtime_fakes" / "fake_cli.py").read_text()
    executables = {}
    for provider in ("codex", "claude"):
        path = tmp_path / f"fake-{provider}"
        path.write_text(f"#!{sys.executable}\n{source}")
        path.chmod(0o755)
        executables[provider] = str(path)
    return executables


class CompletionWindowAdapter(RuntimeAdapter):
    """Observe real decoding, holding cleanup until the runner decides cancellation."""

    def __init__(
        self,
        seat,
        log_dir,
        metadata,
        schema,
        monkeypatch,
        *,
        mode,
        observe,
        report=None,
    ):
        super().__init__(
            seat.provider,
            executable=metadata["executables"][seat.provider],
            timeout=metadata["timeout"],
            grace=metadata["grace"],
            log_dir=log_dir,
            output_schema=schema,
        )
        self.mode = mode
        self.observe = observe
        self.report = report
        self.monkeypatch = monkeypatch
        self.release_cleanup = asyncio.Event()
        self.cancel_decisions = []
        self.completion_windows = []

    def _environment(self):
        env = super()._environment()
        env["FAKE_RUNTIME_MODE"] = self.mode
        if self.report is not None:
            env["FAKE_RUNTIME_REPORT"] = json.dumps(self.report)
        return env

    def _observe(self, state, event):
        super()._observe(state, event)
        if isinstance(event, Completed):
            self.completion_windows.append(
                (
                    isinstance(state.observed_terminal, Completed)
                    and state.observed_terminal.result == event.result,
                    state.terminal is None,
                    state.process.child.returncode is None,
                )
            )
            if self.mode in ("hang-after-result", "contradictory"):
                original = state.process.wait_for_exit

                async def wait_for_exit(seconds):
                    await asyncio.wait_for(self.release_cleanup.wait(), 5)
                    return await original(seconds)

                self.monkeypatch.setattr(state.process, "wait_for_exit", wait_for_exit)
        self.observe(event)

    async def cancel(self, turn, *, on_accepted=None):
        accepted = await super().cancel(turn, on_accepted=on_accepted)
        self.cancel_decisions.append(accepted)
        self.release_cleanup.set()
        return accepted


async def execute_bounded(store, identifier, directory, *, stop, seat_keys=None):
    async with asyncio.timeout(10):
        await runner.execute_round(
            store, identifier, directory, stop=stop, seat_keys=seat_keys
        )


def test_runtime_completed_before_cancel_preserves_report(
    h, monkeypatch, runtime_executables
):
    stop = asyncio.Event()
    adapters = []

    def factory(seat, log_dir, metadata, schema):
        def observe(event):
            if isinstance(event, Completed):
                stop.set()

        adapter = CompletionWindowAdapter(
            seat,
            log_dir,
            metadata,
            schema,
            monkeypatch,
            mode="hang-after-result",
            observe=observe,
        )
        adapters.append(adapter)
        return adapter

    monkeypatch.setattr(runner, "make_adapter", factory)
    with ReviewStore(h.data / "reviews.sqlite3") as store:
        identifier = runner.new_review(
            store,
            h.frozen.snapshot_id,
            (SeatConfig("reviewer-1", "claude", "requested-claude", "high"),),
            timeout=5,
            grace=0.1,
            executables=runtime_executables,
        )
        with runner.execution_lock(h.data):
            asyncio.run(execute_bounded(store, identifier, h.data, stop=stop))
        attempt = store.load_round(identifier).seats[0].attempts[-1]
        history = store.load_history(identifier)
    assert adapters[0].completion_windows == [(True, True, True)]
    assert adapters[0].cancel_decisions == [False]
    assert attempt.status == AttemptStatus.SUCCEEDED
    assert attempt.result == {"summary": "ok", "findings": ()}
    assert attempt.reported_model == "actual-claude"
    assert attempt.failure_category is None and attempt.exit_code is not None
    assert sum(isinstance(envelope.event, Completed) for envelope, _ in history) == 1
    assert not any(
        isinstance(envelope.event, CancelRequested) for envelope, _ in history
    )


def test_runtime_dual_cancel_preserves_completed_seat_and_retries_only_failed_seat(
    h, monkeypatch, runtime_executables
):
    stop = asyncio.Event()
    completed = asyncio.Event()
    other_running = asyncio.Event()
    adapters = defaultdict(list)

    def factory(seat, log_dir, metadata, schema):
        retry = bool(adapters[seat.key])

        def observe(event):
            if not retry:
                if seat.key == "reviewer-1" and isinstance(event, Completed):
                    completed.set()
                if (
                    seat.key == "reviewer-2"
                    and isinstance(event, Message)
                    and event.text == "waiting"
                ):
                    other_running.set()
                if completed.is_set() and other_running.is_set():
                    stop.set()

        adapter = CompletionWindowAdapter(
            seat,
            log_dir,
            metadata,
            schema,
            monkeypatch,
            mode="success"
            if retry
            else ("hang-after-result" if seat.key == "reviewer-1" else "wait"),
            observe=observe,
        )
        adapters[seat.key].append(adapter)
        return adapter

    monkeypatch.setattr(runner, "make_adapter", factory)
    seats = (
        SeatConfig("reviewer-1", "claude", "requested-claude", "high"),
        SeatConfig("reviewer-2", "codex", "model-1", "high"),
    )
    with ReviewStore(h.data / "reviews.sqlite3") as store:
        identifier = runner.new_review(
            store,
            h.frozen.snapshot_id,
            seats,
            timeout=5,
            grace=0.1,
            executables=runtime_executables,
        )
        with runner.execution_lock(h.data):
            asyncio.run(execute_bounded(store, identifier, h.data, stop=stop))
            initial = store.load_round(identifier)
            first, second = (seat.attempts[-1] for seat in initial.seats)
            assert first.status == AttemptStatus.SUCCEEDED
            assert first.result == {"summary": "ok", "findings": ()}
            assert second.status == AttemptStatus.FAILED
            assert second.failure_category == FailureCategory.CANCELLED
            assert second.result is None and second.exit_code is not None
            selected = runner.prepare_retry(store, identifier, None, recover=False)
            assert selected == ("reviewer-2",)
            asyncio.run(
                execute_bounded(
                    store, identifier, h.data, stop=asyncio.Event(), seat_keys=selected
                )
            )
        final = store.load_round(identifier)
        history = store.load_history(identifier)
    assert adapters["reviewer-1"][0].completion_windows == [(True, True, True)]
    assert adapters["reviewer-1"][0].cancel_decisions == [False]
    assert adapters["reviewer-2"][0].cancel_decisions == [True]
    assert len(adapters["reviewer-1"]) == 1 and len(adapters["reviewer-2"]) == 2
    assert final.snapshot_id == initial.snapshot_id == h.frozen.snapshot_id
    assert final.seats[0] == initial.seats[0]
    assert len(final.seats[1].attempts) == 2
    assert final.seats[1].config == seats[1]
    assert final.seats[1].attempts[-1].status == AttemptStatus.SUCCEEDED
    requests = [
        envelope
        for envelope, _ in history
        if isinstance(envelope.event, CancelRequested)
    ]
    assert [(envelope.seat, envelope.attempt) for envelope in requests] == [
        ("reviewer-2", 1)
    ]
    request_index = next(
        i
        for i, (envelope, _) in enumerate(history)
        if isinstance(envelope.event, CancelRequested)
    )
    cancelled_index = next(
        i
        for i, (envelope, _) in enumerate(history)
        if envelope.seat == "reviewer-2" and isinstance(envelope.event, Error)
    )
    assert request_index < cancelled_index


@pytest.mark.parametrize(
    ("provider", "mode", "invalid_report", "detail"),
    [
        (
            "claude",
            "hang-after-result",
            {"summary": "bad", "findings": "malformed"},
            "invalid structured report",
        ),
        ("codex", "contradictory", None, "multiple terminal"),
    ],
)
def test_runtime_late_cancel_does_not_hide_invalid_report_or_contradictory_terminal(
    h, monkeypatch, runtime_executables, provider, mode, invalid_report, detail
):
    stop = asyncio.Event()
    adapters = []

    def factory(seat, log_dir, metadata, schema):
        def observe(event):
            if isinstance(event, Completed):
                stop.set()

        adapter = CompletionWindowAdapter(
            seat,
            log_dir,
            metadata,
            schema,
            monkeypatch,
            mode=mode,
            observe=observe,
            report=invalid_report,
        )
        adapters.append(adapter)
        return adapter

    monkeypatch.setattr(runner, "make_adapter", factory)
    with ReviewStore(h.data / "reviews.sqlite3") as store:
        identifier = runner.new_review(
            store,
            h.frozen.snapshot_id,
            (SeatConfig("reviewer-1", provider, "model-1", "high"),),
            timeout=5,
            grace=0.1,
            executables=runtime_executables,
        )
        with runner.execution_lock(h.data):
            asyncio.run(execute_bounded(store, identifier, h.data, stop=stop))
        attempt = store.load_round(identifier).seats[0].attempts[-1]
        history = store.load_history(identifier)
    assert adapters[0].completion_windows == [(True, True, True)]
    assert adapters[0].cancel_decisions == [False]
    assert attempt.status == AttemptStatus.FAILED
    assert attempt.failure_category is None and attempt.result is None
    assert detail in attempt.detail
    assert attempt.exit_code is not None
    assert not any(
        isinstance(envelope.event, (CancelRequested, Completed))
        for envelope, _ in history
    )


def test_accepted_cancellation_finishes_after_stream_before_terminal_persistence(
    h, monkeypatch
):
    stop = asyncio.Event()
    accepted_started = asyncio.Event()
    stream_finished = asyncio.Event()
    decisions = []

    class DelayedCancellation(RecordingAdapter):
        async def submit_turn(self, session, text):
            turn = await super().submit_turn(session, text)
            stop.set()
            return turn

        async def cancel(self, turn, *, on_accepted=None):
            accepted = await super().cancel(turn, on_accepted=on_accepted)
            accepted_started.set()
            await asyncio.wait_for(stream_finished.wait(), 5)
            decisions.append(accepted)
            return accepted

        async def events(self, turn):
            await asyncio.wait_for(accepted_started.wait(), 5)
            async for event in super().events(turn):
                yield event
            stream_finished.set()

    def factory(seat, log_dir, metadata, schema):
        planned = TurnScript(
            (Completed(GOOD), ProcessExited(0)),
            (Completed(GOOD), ProcessExited(0)),
        )
        return DelayedCancellation(h, seat, planned, h.environment)

    monkeypatch.setattr(runner, "make_adapter", factory)
    with ReviewStore(h.data / "reviews.sqlite3") as store:
        identifier = runner.new_review(
            store,
            h.frozen.snapshot_id,
            (SeatConfig("reviewer-1", "codex", "model-one", "high"),),
            timeout=5,
            grace=0.1,
            executables={"codex": "unused", "claude": "unused"},
        )
        with runner.execution_lock(h.data):
            asyncio.run(execute_bounded(store, identifier, h.data, stop=stop))
        attempt = store.load_round(identifier).seats[0].attempts[-1]
        history = store.load_history(identifier)
    assert decisions == [True] and stream_finished.is_set()
    assert h.cancelled == ["reviewer-1"]
    assert attempt.status == AttemptStatus.FAILED
    assert attempt.failure_category == FailureCategory.CANCELLED
    assert attempt.result is None and attempt.exit_code == 0
    request_index = next(
        i
        for i, (envelope, _) in enumerate(history)
        if isinstance(envelope.event, CancelRequested)
    )
    terminal_index = next(
        i
        for i, (envelope, _) in enumerate(history)
        if isinstance(envelope.event, Completed)
    )
    assert request_index < terminal_index


def test_runtime_accepted_cancel_io_failures_keep_cancelling_and_forbid_recovery(
    h, monkeypatch, runtime_executables
):
    stop = asyncio.Event()
    adapters = []

    class BrokenCancellation(RuntimeAdapter):
        process = None
        original_terminate = None
        acceptance_before_io = False

        def _environment(self):
            env = super()._environment()
            env["FAKE_RUNTIME_MODE"] = "wait"
            return env

        def _observe(self, state, event):
            super()._observe(state, event)
            if isinstance(event, Message) and event.text == "waiting":
                self.process = state.process
                self.original_terminate = state.process.terminate

                async def failed_terminate(grace):
                    raise OSError("injected cleanup failure")

                monkeypatch.setattr(state.process, "terminate", failed_terminate)
                stop.set()

        async def _send_cancel(self, state):
            self.acceptance_before_io = (
                store.load_round(identifier).seats[0].attempts[-1].status
                == AttemptStatus.CANCELLING
            )
            # Wake the real reader into its cleanup path without waiting for a
            # deadline. Both cleanup attempts fail, so the child is still live.
            state.task.cancel()
            raise OSError("injected interrupt failure")

    def factory(seat, log_dir, metadata, schema):
        adapter = BrokenCancellation(
            seat.provider,
            executable=metadata["executables"][seat.provider],
            timeout=5,
            grace=0.1,
            log_dir=log_dir,
            output_schema=schema,
        )
        adapters.append(adapter)
        return adapter

    monkeypatch.setattr(runner, "make_adapter", factory)
    with ReviewStore(h.data / "reviews.sqlite3") as store:
        identifier = runner.new_review(
            store,
            h.frozen.snapshot_id,
            (SeatConfig("reviewer-1", "claude", "requested-claude", "high"),),
            timeout=5,
            grace=0.1,
            executables=runtime_executables,
        )

        async def exercise():
            try:
                with pytest.raises(OSError, match="injected cleanup failure"):
                    await execute_bounded(store, identifier, h.data, stop=stop)
                attempt = store.load_round(identifier).seats[0].attempts[-1]
                assert attempt.status == AttemptStatus.CANCELLING
                assert attempt.exit_code is None and attempt.failure_category is None
                assert attempt.result is None
                assert adapters[0].acceptance_before_io
                assert adapters[0].process.child.returncode is None
                with pytest.raises(ValueError, match="lacks terminal evidence"):
                    runner.prepare_retry(store, identifier, None, recover=True)
            finally:
                for index, adapter in enumerate(adapters):
                    if adapter.process is not None:
                        monkeypatch.setattr(
                            adapter.process, "terminate", adapter.original_terminate
                        )
                        with Recorder(
                            h.data / f"test-cleanup-{index}.jsonl", Redactor()
                        ) as recorder:
                            adapter.process._recorder = recorder
                            await adapter.original_terminate(0.1)

        with runner.execution_lock(h.data):
            asyncio.run(exercise())
        history = store.load_history(identifier)
    assert (
        sum(isinstance(envelope.event, CancelRequested) for envelope, _ in history) == 1
    )
    assert not any(
        isinstance(envelope.event, (Completed, Error, ProcessExited))
        for envelope, _ in history
    )
    assert adapters[0].process.child.returncode is not None
