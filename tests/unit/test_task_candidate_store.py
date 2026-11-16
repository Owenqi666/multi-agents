"""Candidate CAS, immutable histories and independent pre-T46 byte goldens."""

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest
import test_task_candidate as core
import test_task_execution as fixtures
import test_task_execution_store as execution_store
import test_task_store as legacy

from multi_agent.core import Disposition
from multi_agent.core.events import (
    CancelRequested,
    Completed,
    Error,
    ProcessExited,
    SessionStarted,
    Usage,
)
from multi_agent.core.snapshot import (
    FrozenFile,
    FrozenSnapshot,
)
from multi_agent.core.snapshot import (
    TestEvidence as Evidence,
)
from multi_agent.core.task_events import (
    TaskCandidateFrozen,
    TaskDiagnostic,
    TaskEnvelope,
    TaskEnvironmentObserved,
    TaskEvidenceRejected,
    TaskReconciled,
    TaskScopeBlocked,
)
from multi_agent.core.task_execution import task_execution_hash
from multi_agent.core.task_state import TaskPhase
from multi_agent.core.transitions import apply_task
from multi_agent.store.planning_store import PlanningStore
from multi_agent.store.task_codec import (
    decode_task_attempt,
    decode_task_envelope,
    encode_task_attempt,
    encode_task_envelope,
)
from multi_agent.store.task_store import TaskStore

store = execution_store.store


def qualify(store):
    execution_store.reserve(store)
    for event in (
        Completed({"actual": True}, raw_type="synthetic.completed"),
        ProcessExited(-15, raw_type="synthetic.exit"),
        core.reconciliation(),
    ):
        assert (
            store.apply_task("run", TaskEnvelope(1, event, "observed"))
            == Disposition.APPLIED
        )
    return core.candidate(store.load_task("run"))


def record(
    store, event, *, number=1, prefix=None, run_id="run", occurred_at="candidate"
):
    history = store.load_task_history(run_id) if prefix is None else prefix
    return store.record_task_candidate(
        run_id,
        TaskEnvelope(number, event, occurred_at),
        expected_history_length=len(history),
        expected_history_hash=task_execution_hash(history),
    )


def persisted(store):
    return (
        store.load_task("run"),
        store.load_task_history("run"),
        tuple(
            tuple(row)
            for row in store._connection.execute(
                "SELECT number,payload FROM task_attempts WHERE run_id='run' ORDER BY number"
            )
        ),
    )


@pytest.mark.parametrize(
    "stale", ["length", "same-length-hash", "history-only-tail", "duplicate"]
)
def test_candidate_store_requires_full_history_cas(store, stale):
    event = qualify(store)
    prefix = store.load_task_history("run")
    if stale == "length":
        expected = prefix[:-1]
    elif stale == "same-length-hash":
        expected = (
            *prefix[:-1],
            (replace(prefix[-1][0], occurred_at="another-time"), prefix[-1][1]),
        )
        assert len(expected) == len(prefix)
    elif stale == "history-only-tail":
        expected = prefix
        late = TaskEnvelope(1, CancelRequested(), "late-cancel")
        assert store.apply_task("run", late) == Disposition.HISTORY_ONLY
    else:
        expected = prefix
        assert record(store, event, prefix=prefix) == Disposition.APPLIED
    before = persisted(store)
    with pytest.raises(ValueError):
        record(store, event, prefix=expected)
    assert persisted(store) == before
    assert record(store, event) == (
        Disposition.HISTORY_ONLY if stale == "duplicate" else Disposition.APPLIED
    )
    state = store.load_task("run")
    assert state.current_candidate == event and state.candidate_ready


@pytest.mark.parametrize("number", [0, 1, 2])
def test_candidate_store_rejects_apply_bypass_without_history(store, number):
    event = qualify(store)
    before = persisted(store)
    with pytest.raises(ValueError):
        store.apply_task("run", TaskEnvelope(number, event))
    assert persisted(store) == before


@pytest.mark.parametrize("failure_at", ["state-write", "history-write"])
def test_candidate_store_rolls_back_state_and_history(store, monkeypatch, failure_at):
    event = qualify(store)
    before = persisted(store)
    failure = sqlite3.OperationalError("injected candidate persistence failure")
    method = "_write_task" if failure_at == "state-write" else "_append_task_history"
    original = getattr(store, method)
    seen = []

    def fail_after_actual_write(*args):
        original(*args)
        seen.append(store._connection.in_transaction)
        raise failure

    with monkeypatch.context() as patch:
        patch.setattr(store, method, fail_after_actual_write)
        with pytest.raises(sqlite3.OperationalError) as caught:
            record(store, event)
        assert caught.value is failure
    assert seen == [True]
    assert persisted(store) == before
    assert not store._connection.in_transaction
    assert record(store, event) == Disposition.APPLIED
    assert store.load_task("run").candidate_ready


@pytest.mark.parametrize(
    "invalid",
    ["not-candidate", "unknown-run", "zero", "negative", "future", "no-current"],
)
def test_candidate_store_invalid_calls_preserve_exact_state_and_history(store, invalid):
    event = core.candidate()
    if invalid != "no-current":
        event = qualify(store)
    before = persisted(store)
    number, run_id, expected = 1, "run", ValueError
    if invalid == "not-candidate":
        event, expected = TaskDiagnostic(Error("not a candidate")), TypeError
    elif invalid == "unknown-run":
        run_id, expected = "missing", KeyError
    elif invalid in {"zero", "negative", "future"}:
        number = {"zero": 0, "negative": -1, "future": 2}[invalid]
    prefix = store.load_task_history("run")
    with pytest.raises(expected):
        record(store, event, number=number, run_id=run_id, prefix=prefix)
    assert persisted(store) == before
    assert not store._connection.in_transaction


@pytest.mark.parametrize("same", [False, True])
def test_candidate_store_two_connections_have_one_winner(store, tmp_path, same):
    event = qualify(store)
    prefix = store.load_task_history("run")
    barrier = Barrier(2)

    def compete(index):
        proposed = (
            event if same or index == 0 else core.changed_candidate(event, "candidate")
        )
        with TaskStore(tmp_path / "task.sqlite") as connection:
            barrier.wait(timeout=5)
            try:
                disposition = record(
                    connection, proposed, prefix=prefix, occurred_at=f"racer-{index}"
                )
            except ValueError:
                return None
            return proposed, disposition, index

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(compete, index) for index in range(2)]
        outcomes = [future.result(timeout=10) for future in futures]
    winners = [outcome for outcome in outcomes if outcome is not None]
    assert len(winners) == 1 and outcomes.count(None) == 1
    proposed, disposition, index = winners[0]
    assert disposition == Disposition.APPLIED
    actual = store.load_task_history("run")
    assert actual == (
        *prefix,
        (TaskEnvelope(1, proposed, f"racer-{index}"), Disposition.APPLIED),
    )
    state = store.load_task("run")
    assert state.current_candidate == proposed and state.candidate_ready
    assert (
        sum(
            isinstance(item, TaskCandidateFrozen)
            for item in state.current_attempt.events
        )
        == 1
    )


@pytest.mark.parametrize("old_qualified", [False, True])
@pytest.mark.parametrize("conflicting", [False, True])
def test_late_candidate_is_history_only_after_retry(store, old_qualified, conflicting):
    event = core.candidate()
    if old_qualified:
        event = qualify(store)
        assert record(store, event) == Disposition.APPLIED
        store.apply_task("run", TaskEnvelope(1, TaskEvidenceRejected("explicit retry")))
    else:
        execution_store.reserve(store)
        execution_store.fail(store)
    old_prefix = store.load_task_history("run")
    original_old = store.load_task("run").current_attempt
    execution_store.reserve(store, 2)
    current = store.load_task("run")
    before = store.load_task_history("run")
    proposed = core.changed_candidate(event, "baseline") if conflicting else event
    assert (
        record(store, proposed, number=1, prefix=old_prefix, occurred_at="late")
        == Disposition.HISTORY_ONLY
    )
    assert store.load_task("run") == current
    assert store.load_task("run").attempts[0] == original_old
    assert current.current_candidate is None and not current.candidate_ready
    assert store.load_task_history("run") == (
        *before,
        (TaskEnvelope(1, proposed, "late"), Disposition.HISTORY_ONLY),
    )


def test_candidate_store_planning_required_first_record_is_rejected(store):
    event = qualify(store)
    store.apply_task(
        "run", TaskEnvelope(1, TaskScopeBlocked("Need shared", ("shared/api.py",)))
    )
    before = persisted(store)
    assert before[0].phase == TaskPhase.PLANNING_REQUIRED
    with pytest.raises(ValueError):
        record(store, event)
    assert persisted(store) == before


def test_candidate_store_duplicates_are_history_only_without_state_writes(
    store, monkeypatch
):
    event = qualify(store)
    assert record(store, event) == Disposition.APPLIED
    store.apply_task("run", TaskEnvelope(1, TaskEvidenceRejected("revoked")))
    before = store.load_task("run")
    prefix = store.load_task_history("run")

    def no_state_write(*args):
        pytest.fail("an exact candidate duplicate rewrote applied state")

    monkeypatch.setattr(store, "_write_task", no_state_write)
    assert record(store, event, occurred_at="duplicate") == Disposition.HISTORY_ONLY
    assert store.load_task("run") == before
    assert not before.candidate_ready
    assert store.load_task_history("run") == (
        *prefix,
        (TaskEnvelope(1, event, "duplicate"), Disposition.HISTORY_ONLY),
    )
    with pytest.raises(ValueError):
        record(store, core.changed_candidate(event, "tree"))


def test_candidate_reopen_preserves_anchors_and_unknowns(store, tmp_path):
    event = qualify(store)
    store.save_snapshot(OLD_SNAPSHOT_ID, json.loads(OLD_GOLDENS["snapshot"]))
    before_tables = tuple(
        tuple(row)
        for row in store._connection.execute(
            "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
        )
    )
    assert record(store, event) == Disposition.APPLIED
    before, before_history, payloads = persisted(store)
    with PlanningStore(tmp_path / "task.sqlite") as reopened:
        assert reopened.load_task("run") == before
        assert reopened.load_task_history("run") == before_history
        assert reopened.load_task("run").candidate_ready
        assert reopened.load_task("run").current_candidate == event
        current = reopened.load_task("run").current_attempt
        assert current.runtime.reported_model is current.runtime.reported_effort is None
        assert current.runtime.exit_code == -15
        assert current.reconciliation.log_path == event.execution_log_path
        assert current.reconciliation.log_sha256 == event.execution_log_sha256
        assert (
            tuple(
                tuple(row)
                for row in reopened._connection.execute(
                    "SELECT number,payload FROM task_attempts WHERE run_id='run' ORDER BY number"
                )
            )
            == payloads
        )
        assert reopened.load_snapshot(OLD_SNAPSHOT_ID) == json.loads(
            OLD_GOLDENS["snapshot"]
        )
        assert (
            reopened._connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
            == 1
        )
        assert (
            tuple(
                tuple(row)
                for row in reopened._connection.execute(
                    "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
                )
            )
            == before_tables
        )
        assert record(reopened, event) == Disposition.HISTORY_ONLY


def snapshot_golden_value():
    return FrozenSnapshot(
        "/synthetic/repository/.git",
        "a" * 40,
        "b" * 40,
        "diff --git a/src/新增.py b/src/新增.py\n+value = 1\n",
        "原始需求\r\n",
        ("保持公开接口",),
        (
            Evidence(
                "b" * 40,
                ("python", "-m", "pytest"),
                0,
                "Supplied record only; not executed.\n",
            ),
        ),
        (
            FrozenFile("candidate", "src/新增.py", "value = 1\r\n", "c" * 40, "100644"),
            FrozenFile("baseline", "binary.bin", None, "d" * 40, "100644"),
        ),
        (("python", "-m", "pytest"),),
    )


def old_payloads():
    started_legacy = TaskEnvelope(1, legacy.started(), "legacy-start")
    old, _ = apply_task(fixtures.initial(), started_legacy)
    error_legacy = TaskEnvelope(
        1, Error("旧失败\r\n", raw_type="synthetic.legacy.error"), "legacy-failure"
    )
    old, _ = apply_task(old, error_legacy)
    reconciled_legacy = TaskEnvelope(
        1, TaskReconciled("old-final", True, "旧收据"), "legacy-stop"
    )
    old, _ = apply_task(old, reconciled_legacy)
    managed, started, prepared = fixtures.managed()
    for event in (
        TaskEnvironmentObserved(
            {"cli_version": None, "models": None, "nested": {"flags": [True, None]}}
        ),
        SessionStarted("synthetic-session", raw_type="synthetic.init"),
        Usage({"input_tokens": 1, "output_tokens": 2}, raw_type="synthetic.usage"),
        Completed(
            {"summary": "原字节\r\n", "unknown": None, "flags": [True, False]},
            raw_type="synthetic.completed",
        ),
        ProcessExited(-15, raw_type="synthetic.exit"),
        TaskReconciled(
            "final-digest",
            True,
            "Synthetic supplied host facts",
            "/synthetic/execution.json",
            "e" * 64,
        ),
    ):
        managed, _ = fixtures.send(managed, event)
    return {
        "legacy_started_envelope": encode_task_envelope(started_legacy),
        "legacy_error_envelope": encode_task_envelope(error_legacy),
        "legacy_reconciled_envelope": encode_task_envelope(reconciled_legacy),
        "legacy_attempt": encode_task_attempt(old.current_attempt),
        "managed_started_envelope": encode_task_envelope(started),
        "managed_prepared_envelope": encode_task_envelope(prepared),
        "managed_attempt": encode_task_attempt(managed.current_attempt),
        "snapshot": json.dumps(
            snapshot_golden_value().to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    }


def test_candidate_codec_roundtrip_and_legacy_bytes(store):
    event = qualify(store)
    envelope = TaskEnvelope(1, event, None)
    expected = {
        "attempt": 1,
        "occurred_at": None,
        "event": {
            "type": "task_candidate_frozen",
            "delivery": {
                "baseline": event.delivery.baseline,
                "candidate": "c" * 40,
                "changed_paths": ["src/changed.py", "src/新增.py"],
                "digest": core.DIGEST,
                "ref": "refs/multi-agent/candidates/owner-1",
                "tree": "d" * 40,
            },
            "execution_log_path": core.LOG_PATH,
            "execution_log_sha256": core.LOG_HASH,
        },
    }
    assert json.loads(encode_task_envelope(envelope)) == expected
    restored = decode_task_envelope(encode_task_envelope(envelope))
    assert restored == envelope
    assert type(restored.event.delivery.changed_paths) is tuple
    assert record(store, event) == Disposition.APPLIED
    assert record(store, event, occurred_at="duplicate") == Disposition.HISTORY_ONLY
    attempt = store.load_task("run").current_attempt
    assert decode_task_attempt(encode_task_attempt(attempt)) == attempt
    assert set(json.loads(encode_task_attempt(attempt))) == {
        "request",
        "runtime",
        "reconciliation",
        "events",
    }
    complete_history = store.load_task_history("run")
    restored_history = tuple(
        (
            decode_task_envelope(encode_task_envelope(item)),
            Disposition(disposition.value),
        )
        for item, disposition in complete_history
    )
    assert restored_history == complete_history
    for name, actual in old_payloads().items():
        expected_raw = OLD_GOLDENS[name].encode("utf-8")
        assert actual.encode("utf-8") == expected_raw, name
        assert (
            hashlib.sha256(actual.encode("utf-8")).hexdigest()
            == OLD_PAYLOAD_HASHES[name]
        ), name
        if name.endswith("envelope"):
            assert (
                encode_task_envelope(decode_task_envelope(OLD_GOLDENS[name])).encode(
                    "utf-8"
                )
                == expected_raw
            )
        elif name.endswith("attempt"):
            assert (
                encode_task_attempt(decode_task_attempt(OLD_GOLDENS[name])).encode(
                    "utf-8"
                )
                == expected_raw
            )
    legacy_attempt = decode_task_attempt(OLD_GOLDENS["legacy_attempt"])
    assert legacy_attempt.request.managed is False and legacy_attempt.events == ()
    assert (
        legacy_attempt.reconciliation.log_path
        is legacy_attempt.reconciliation.log_sha256
        is None
    )
    snapshot = snapshot_golden_value()
    assert snapshot.snapshot_id == OLD_SNAPSHOT_ID
    assert FrozenSnapshot.from_dict(json.loads(OLD_GOLDENS["snapshot"])) == snapshot


@pytest.mark.parametrize(
    "field",
    [
        "baseline",
        "candidate",
        "changed_paths",
        "digest",
        "ref",
        "tree",
        "execution_log_path",
        "execution_log_sha256",
    ],
)
def test_candidate_codec_requires_every_new_field(field):
    payload = json.loads(encode_task_envelope(TaskEnvelope(1, core.candidate())))
    target = (
        payload["event"]
        if field.startswith("execution_")
        else payload["event"]["delivery"]
    )
    del target[field]
    with pytest.raises(KeyError):
        decode_task_envelope(json.dumps(payload))


# Captured before T46 from source HEAD 473bcce4305807fc909f4927b226954c27e3e737.
# Runtime tests read these literals only, never the external acceptance file.
OLD_GOLDENS = {
    "legacy_attempt": '{"reconciliation":{"detail":"旧收据","process_stopped":true,"worktree_digest":"old-final"},"request":{"decision":{"allowed_models":["model-a","model-b"],"choice":{"effort":"medium","model":"model-b","provider":"codex"},"matched_rule_indices":[0],"reason":"enabled_ordered_fallback","requested":{"effort":"high","model":"model-a","provider":"codex"},"rules_hash":"f546efcf695a5fd3471c66434921cb423758820ff3a04784845b3dd239a4577d","selection_history":[{"choice":{"effort":"high","model":"model-a","provider":"codex"},"outcome":"known_unavailable"},{"choice":{"effort":"medium","model":"model-b","provider":"codex"},"outcome":"selected_request"}],"task_hash":"f0ffee99d275da47dce4d9320a147fd5dcc628daff429e5498b948ef1eace42e","task_type":"backend","unavailable_models":[["codex","model-a"]],"used_default":false},"environment":{"cli_version":null,"models":["observed-catalog-entry"]},"prompt_template_version":"implement-v1","started_at":"fixed-start","type":"task_attempt_started","worktree":"/external/worktree"},"runtime":{"detail":"旧失败\\r\\n","exit_code":null,"failure_category":null,"number":1,"reported_effort":null,"reported_effort_source":null,"reported_model":null,"reported_model_source":null,"result":null,"retry_after":null,"session_id":null,"status":"failed"}}',
    "legacy_error_envelope": '{"attempt":1,"event":{"detail":"旧失败\\r\\n","failure_category":null,"raw_type":"synthetic.legacy.error","retry_after":null,"type":"error"},"occurred_at":"legacy-failure"}',
    "legacy_reconciled_envelope": '{"attempt":1,"event":{"detail":"旧收据","process_stopped":true,"type":"task_reconciled","worktree_digest":"old-final"},"occurred_at":"legacy-stop"}',
    "legacy_started_envelope": '{"attempt":1,"event":{"decision":{"allowed_models":["model-a","model-b"],"choice":{"effort":"medium","model":"model-b","provider":"codex"},"matched_rule_indices":[0],"reason":"enabled_ordered_fallback","requested":{"effort":"high","model":"model-a","provider":"codex"},"rules_hash":"f546efcf695a5fd3471c66434921cb423758820ff3a04784845b3dd239a4577d","selection_history":[{"choice":{"effort":"high","model":"model-a","provider":"codex"},"outcome":"known_unavailable"},{"choice":{"effort":"medium","model":"model-b","provider":"codex"},"outcome":"selected_request"}],"task_hash":"f0ffee99d275da47dce4d9320a147fd5dcc628daff429e5498b948ef1eace42e","task_type":"backend","unavailable_models":[["codex","model-a"]],"used_default":false},"environment":{"cli_version":null,"models":["observed-catalog-entry"]},"prompt_template_version":"implement-v1","started_at":"fixed-start","type":"task_attempt_started","worktree":"/external/worktree"},"occurred_at":"legacy-start"}',
    "managed_attempt": '{"events":[{"decision":{"allowed_models":["model-a","model-b"],"choice":{"effort":"medium","model":"model-b","provider":"codex"},"matched_rule_indices":[0],"reason":"enabled_ordered_fallback","requested":{"effort":"high","model":"model-a","provider":"codex"},"rules_hash":"f546efcf695a5fd3471c66434921cb423758820ff3a04784845b3dd239a4577d","selection_history":[{"choice":{"effort":"high","model":"model-a","provider":"codex"},"outcome":"known_unavailable"},{"choice":{"effort":"low","model":"not-allowed","provider":"codex"},"outcome":"disallowed_by_rules"},{"choice":{"effort":"medium","model":"model-b","provider":"codex"},"outcome":"selected_request"}],"task_hash":"f0ffee99d275da47dce4d9320a147fd5dcc628daff429e5498b948ef1eace42e","task_type":"backend","unavailable_models":[["codex","model-a"]],"used_default":false},"environment":null,"managed":true,"prompt_template_version":"implementation-v1","started_at":"start-1","type":"task_attempt_started","worktree":"/synthetic/worktree-1"},{"context":{"identity":[1,601],"manifest_sha256":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd","path":"/synthetic/context-1","token":"context-token-1"},"dispatch_policy":{"fallbacks":[{"effort":"low","model":"not-allowed","provider":"codex"},{"effort":"medium","model":"model-b","provider":"codex"},{"effort":"low","model":"model-a","provider":"codex"}],"requested":{"effort":"high","model":"model-a","provider":"codex"},"task_type":"backend"},"initial_workspace_digest":"initial-digest-1","output_schema":{"nested":{"required":["status"],"unknown":null},"type":"object"},"policy":{"executable":"/synthetic/codex","executable_identity":[1,42],"executable_sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","grace":1,"max_attempts":2,"provider":"codex","timeout":30},"prompt":"Frozen '
    "prompt "
    '1","type":"task_execution_prepared","workspace":{"controls":[{"content":{"hex":"6769746469723a2073796e7468657469630a"},"identity":[1,301],"path":".git"},{"content":null,"identity":null,"path":"absent"},{"content":{"hex":""},"identity":[1,401],"path":"empty"},{"content":{"hex":"00ff"},"identity":[1,501],"path":"binary"}],"git_directory":"/synthetic/repository/.git/worktrees/task-1","git_identity":[1,201],"object_format":"sha1","owner_token":"owner-1","repository":"/original/repository/.git","root":"/synthetic/worktree-1","root_identity":[1,101],"source":"/synthetic/source","start_commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","task_baseline":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}},{"environment":{"cli_version":null,"models":null,"nested":{"flags":[true,null]}},"type":"task_environment_observed"},{"raw_type":"synthetic.init","reported_effort":null,"reported_model":null,"session_id":"synthetic-session","type":"session_started"},{"counts":{"input_tokens":1,"output_tokens":2},"raw_type":"synthetic.usage","type":"usage"},{"raw_type":"synthetic.completed","reported_model":null,"result":{"flags":[true,false],"summary":"原字节\\r\\n","unknown":null},"type":"completed"},{"exit_code":-15,"raw_type":"synthetic.exit","type":"process_exited"},{"detail":"Synthetic '
    "supplied host "
    'facts","log_path":"/synthetic/execution.json","log_sha256":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee","process_stopped":true,"type":"task_reconciled","worktree_digest":"final-digest"}],"reconciliation":{"detail":"Synthetic '
    "supplied host "
    'facts","log_path":"/synthetic/execution.json","log_sha256":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee","process_stopped":true,"worktree_digest":"final-digest"},"request":{"decision":{"allowed_models":["model-a","model-b"],"choice":{"effort":"medium","model":"model-b","provider":"codex"},"matched_rule_indices":[0],"reason":"enabled_ordered_fallback","requested":{"effort":"high","model":"model-a","provider":"codex"},"rules_hash":"f546efcf695a5fd3471c66434921cb423758820ff3a04784845b3dd239a4577d","selection_history":[{"choice":{"effort":"high","model":"model-a","provider":"codex"},"outcome":"known_unavailable"},{"choice":{"effort":"low","model":"not-allowed","provider":"codex"},"outcome":"disallowed_by_rules"},{"choice":{"effort":"medium","model":"model-b","provider":"codex"},"outcome":"selected_request"}],"task_hash":"f0ffee99d275da47dce4d9320a147fd5dcc628daff429e5498b948ef1eace42e","task_type":"backend","unavailable_models":[["codex","model-a"]],"used_default":false},"environment":null,"managed":true,"prompt_template_version":"implementation-v1","started_at":"start-1","type":"task_attempt_started","worktree":"/synthetic/worktree-1"},"runtime":{"detail":null,"exit_code":-15,"failure_category":null,"number":1,"reported_effort":null,"reported_effort_source":null,"reported_model":null,"reported_model_source":null,"result":{"flags":[true,false],"summary":"原字节\\r\\n","unknown":null},"retry_after":null,"session_id":"synthetic-session","status":"succeeded"}}',
    "managed_prepared_envelope": '{"attempt":1,"event":{"context":{"identity":[1,601],"manifest_sha256":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd","path":"/synthetic/context-1","token":"context-token-1"},"dispatch_policy":{"fallbacks":[{"effort":"low","model":"not-allowed","provider":"codex"},{"effort":"medium","model":"model-b","provider":"codex"},{"effort":"low","model":"model-a","provider":"codex"}],"requested":{"effort":"high","model":"model-a","provider":"codex"},"task_type":"backend"},"initial_workspace_digest":"initial-digest-1","output_schema":{"nested":{"required":["status"],"unknown":null},"type":"object"},"policy":{"executable":"/synthetic/codex","executable_identity":[1,42],"executable_sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","grace":1,"max_attempts":2,"provider":"codex","timeout":30},"prompt":"Frozen '
    "prompt "
    '1","type":"task_execution_prepared","workspace":{"controls":[{"content":{"hex":"6769746469723a2073796e7468657469630a"},"identity":[1,301],"path":".git"},{"content":null,"identity":null,"path":"absent"},{"content":{"hex":""},"identity":[1,401],"path":"empty"},{"content":{"hex":"00ff"},"identity":[1,501],"path":"binary"}],"git_directory":"/synthetic/repository/.git/worktrees/task-1","git_identity":[1,201],"object_format":"sha1","owner_token":"owner-1","repository":"/original/repository/.git","root":"/synthetic/worktree-1","root_identity":[1,101],"source":"/synthetic/source","start_commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","task_baseline":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}},"occurred_at":"prepared-1"}',
    "managed_started_envelope": '{"attempt":1,"event":{"decision":{"allowed_models":["model-a","model-b"],"choice":{"effort":"medium","model":"model-b","provider":"codex"},"matched_rule_indices":[0],"reason":"enabled_ordered_fallback","requested":{"effort":"high","model":"model-a","provider":"codex"},"rules_hash":"f546efcf695a5fd3471c66434921cb423758820ff3a04784845b3dd239a4577d","selection_history":[{"choice":{"effort":"high","model":"model-a","provider":"codex"},"outcome":"known_unavailable"},{"choice":{"effort":"low","model":"not-allowed","provider":"codex"},"outcome":"disallowed_by_rules"},{"choice":{"effort":"medium","model":"model-b","provider":"codex"},"outcome":"selected_request"}],"task_hash":"f0ffee99d275da47dce4d9320a147fd5dcc628daff429e5498b948ef1eace42e","task_type":"backend","unavailable_models":[["codex","model-a"]],"used_default":false},"environment":null,"managed":true,"prompt_template_version":"implementation-v1","started_at":"start-1","type":"task_attempt_started","worktree":"/synthetic/worktree-1"},"occurred_at":"started-1"}',
    "snapshot": '{"acceptance":["保持公开接口"],"baseline":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","candidate":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","diff":"diff '
    "--git a/src/新增.py b/src/新增.py\\n+value = "
    '1\\n","files":[{"mode":"100644","oid":"cccccccccccccccccccccccccccccccccccccccc","path":"src/新增.py","side":"candidate","text":"value '
    "= "
    '1\\r\\n"},{"mode":"100644","oid":"dddddddddddddddddddddddddddddddddddddddd","path":"binary.bin","side":"baseline","text":null}],"repository":"/synthetic/repository/.git","required_tests":[["python","-m","pytest"]],"requirements":"原始需求\\r\\n","tests":[{"command":["python","-m","pytest"],"commit":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","exit_code":0,"log":"Supplied '
    'record only; not executed.\\n"}]}',
}

OLD_PAYLOAD_HASHES = {
    "legacy_attempt": "e3b1521eeab7fac1ee8b8041344ef82315266ee65410f05bc3ca56949d86f5f0",
    "legacy_error_envelope": "d48925a07cfa7ec115ecfd5c4b5943037afe3adec8b0bbb65dd532ca9a6c713d",
    "legacy_reconciled_envelope": "13183f7d20030089e2851f9f0374ce92fa2eb128f8f2f0133cd83c255febc8c0",
    "legacy_started_envelope": "99d1c13c1f8a3544a21512b3d1a69b31c6a26e6268238a077b89c7dd85425e69",
    "managed_attempt": "6f3ff84a2831423de6e6a2f09063dfa2f933148705be21160bf0a9346bd82820",
    "managed_prepared_envelope": "3afb1b30ff0ff9bd05ffaba093ea7848bc4db5c40c3cbc8c407d1be60e999c0d",
    "managed_started_envelope": "9ded1ea409f88a232ec61ff5440bfb2dae0f1e3758da379d42e7994e97727e60",
    "snapshot": "6cbd44fd7b1b28bd2d97a0e1946e864e8a11668fd903310e742a700dcfecd329",
}

OLD_SNAPSHOT_ID = "6cbd44fd7b1b28bd2d97a0e1946e864e8a11668fd903310e742a700dcfecd329"
