"""Task persistence binds current-attempt transitions and histories atomically."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from threading import Barrier

import pytest

from multi_agent.cli.task_inputs import parse_project_rules, parse_task_card
from multi_agent.core import (
    AttemptState,
    AttemptStatus,
    Disposition,
    EvidenceSource,
    RoundState,
    SeatConfig,
    SeatState,
)
from multi_agent.core.dispatch import ImplementationChoice, resolve_dispatch
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
from multi_agent.core.task_events import (
    TaskAttemptStarted,
    TaskDiagnostic,
    TaskEnvelope,
    TaskReconciled,
    TaskScopeBlocked,
)
from multi_agent.core.task_state import TaskAttempt, TaskInput, TaskPhase
from multi_agent.core.tasks import FrozenProjectRules
from multi_agent.store import ReviewStore, database
from multi_agent.store.task_codec import (
    decode_task_attempt,
    decode_task_envelope,
    decode_task_input,
    encode_task_attempt,
    encode_task_envelope,
    encode_task_input,
)
from multi_agent.store.task_store import TaskStore

ROOT = Path(__file__).resolve().parents[2]
BASELINE = "a" * 40


def inputs() -> TaskInput:
    card = parse_task_card(
        json.dumps(
            {
                "schema_version": 1,
                "id": "T-0100",
                "goal": "Implement the requested change.",
                "baseline_commit": BASELINE,
                "write_scope": ["src/**"],
                "depends_on": [],
                "interface_contracts": [
                    {"name": "API", "definition": "Keep the interface."}
                ],
                "deliverables": ["Updated implementation."],
                "acceptance_criteria": ["Checks pass."],
                "test_refs": ["checks"],
                "open_questions": [],
            }
        )
    )
    configuration = {
        "provider": "codex",
        "default_model": "model-a",
        "default_effort": "high",
        "allowed_models": ["model-a", "model-b"],
    }
    raw = json.dumps(
        {
            "version": 1,
            "commands": {"checks": {"executable": "pytest", "args": []}},
            "protected_paths": [".multi-agent/**"],
            "implementation": [{"paths": ["src/**"], **configuration}],
            "default_implementation": configuration,
            "task_types": {
                "backend": {
                    "provider": "codex",
                    "allowed_models": ["model-a", "model-b"],
                }
            },
        },
        ensure_ascii=False,
    )
    rules = FrozenProjectRules(
        "/original/repository/.git", BASELINE, "b" * 40, raw, parse_project_rules(raw)
    )
    return TaskInput(card, rules, "原始需求\r\n", "2026-11-16T10:00:00Z")


def started(value: TaskInput | None = None, **changes) -> TaskAttemptStarted:
    value = inputs() if value is None else value
    decision = resolve_dispatch(
        value.card,
        value.frozen_rules,
        task_type="backend",
        requested=ImplementationChoice("codex", "model-a", "high"),
        fallbacks=(ImplementationChoice("codex", "model-b", "medium"),),
        unavailable=frozenset({("codex", "model-a")}),
    )
    result = TaskAttemptStarted(
        decision,
        "/external/worktree",
        "implement-v1",
        "fixed-start",
        {"cli_version": None, "models": ["observed-catalog-entry"]},
    )
    return replace(result, **changes) if changes else result


def create(
    store: TaskStore, run_id: str = "run", value: TaskInput | None = None
) -> TaskInput:
    value = inputs() if value is None else value
    store.create_task(
        run_id, value.card, value.frozen_rules, value.requirements, value.created_at
    )
    return value


def test_task_inputs_and_request_evidence_are_frozen(tmp_path: Path) -> None:
    path = tmp_path / "task.db"
    value = inputs()
    environment = {"cli_version": None, "details": {"reported": ["original"]}}
    request = started(value, environment=environment)
    environment["details"]["reported"].append("changed")
    with TaskStore(path) as store:
        create(store, value=value)
        store.apply_task("run", TaskEnvelope(1, request))
        original = store.load_task("run")
        original_input = store.load_task_input("run")
        with pytest.raises(FrozenInstanceError):
            original_input.requirements = "changed"
        with pytest.raises(TypeError):
            original.current_attempt.request.environment["cli_version"] = "invented"
        with pytest.raises(sqlite3.IntegrityError):
            create(store, value=replace(value, requirements="replaced input"))
        assert store.load_task_input("run") == value
        assert store.load_task("run") == original
    with TaskStore(path) as store:
        restored = store.load_task("run")
        assert store.load_task_input("run") == original_input
        assert restored == original
        attempt = restored.current_attempt
        assert attempt.request.environment["details"]["reported"] == ("original",)
        assert attempt.request.decision.choice.model == "model-b"
        assert attempt.request.decision.requested.model == "model-a"
        assert attempt.runtime.reported_model is None
        assert attempt.runtime.reported_effort is None
        assert attempt.runtime.reported_model_source is None
        assert attempt.runtime.exit_code is None


def test_task_input_identity_keeps_repository_requirements_and_baseline(
    tmp_path: Path,
) -> None:
    value = inputs()
    other = replace(
        value,
        frozen_rules=replace(value.frozen_rules, repository="/another/repository/.git"),
        requirements="Other requirement",
    )
    assert other.frozen_rules.rules_hash == value.frozen_rules.rules_hash
    with TaskStore(tmp_path / "task.db") as store:
        create(store, "one", value)
        create(store, "two", other)
        assert store.load_task_input("one") == value
        assert store.load_task_input("two") == other
        mismatched = replace(
            value, frozen_rules=replace(value.frozen_rules, baseline_commit="c" * 40)
        )
        with pytest.raises(ValueError):
            create(store, "bad", mismatched)
        assert store.list_recoverable_tasks() == ("one", "two")
        with pytest.raises(KeyError):
            store.load_task("bad")


@pytest.mark.parametrize("name", ["task_hash", "rules_hash"])
def test_mismatched_request_hashes_do_not_leave_attempts_or_history(
    tmp_path: Path, name: str
) -> None:
    with TaskStore(tmp_path / "task.db") as store:
        value = create(store)
        request = started(value)
        invalid = replace(
            request, decision=replace(request.decision, **{name: "bad-hash"})
        )
        before = store.load_task("run")
        with pytest.raises(ValueError):
            store.apply_task("run", TaskEnvelope(1, invalid))
        assert store.load_task("run") == before
        assert store.load_task_history("run") == ()


def test_concurrent_task_start_has_one_winner(tmp_path: Path) -> None:
    path = tmp_path / "task.db"
    with TaskStore(path) as store:
        value = create(store)
    barrier = Barrier(2)

    def compete(name: str) -> str | None:
        with TaskStore(path) as store:
            barrier.wait(timeout=5)
            try:
                store.apply_task("run", TaskEnvelope(1, started(value, worktree=name)))
            except ValueError:
                return None
            return name

    with ThreadPoolExecutor(max_workers=2) as pool:
        winners = list(pool.map(compete, ("worktree-one", "worktree-two")))
    assert winners.count(None) == 1
    winner = next(name for name in winners if name is not None)
    with TaskStore(path) as store:
        state = store.load_task("run")
        assert len(state.attempts) == 1
        assert state.current_attempt.number == 1
        assert state.current_attempt.request.worktree == winner
        assert len(store.load_task_history("run")) == 1
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM task_attempts").fetchone() == (
            1,
        )
        assert connection.execute(
            "SELECT current_attempt FROM task_runs"
        ).fetchone() == (1,)


def test_history_failure_rolls_back_task_phase_attempt_and_history(
    tmp_path: Path,
) -> None:
    path = tmp_path / "task.db"
    with TaskStore(path) as store:
        value = create(store)
        original = store.load_task("run")
        with sqlite3.connect(path) as connection:
            connection.execute("""CREATE TRIGGER reject_task_history BEFORE INSERT ON task_history
                BEGIN SELECT RAISE(FAIL, 'injected task history failure'); END""")
        request = TaskEnvelope(1, started(value))
        with pytest.raises(
            sqlite3.IntegrityError, match="injected task history failure"
        ):
            store.apply_task("run", request)
        assert store.load_task("run") == original
        assert store.load_task_history("run") == ()
        with sqlite3.connect(path) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM task_attempts"
            ).fetchone() == (0,)
            assert connection.execute(
                "SELECT current_attempt FROM task_runs"
            ).fetchone() == (None,)
            connection.execute("DROP TRIGGER reject_task_history")
        assert store.apply_task("run", request) is Disposition.APPLIED
        assert store.load_task_history("run") == ((request, Disposition.APPLIED),)


def test_late_implementation_result_is_history_only(tmp_path: Path) -> None:
    path = tmp_path / "task.db"
    with TaskStore(path) as first, TaskStore(path) as second:
        value = create(first)
        request = started(value)
        first.apply_task("run", TaskEnvelope(1, request))
        first.apply_task(
            "run", TaskEnvelope(1, Error("timed out", FailureCategory.TIMEOUT))
        )
        first.apply_task(
            "run",
            TaskEnvelope(1, TaskReconciled("digest-one", True, "Stopped and checked")),
        )
        second.apply_task(
            "run", TaskEnvelope(2, replace(request, worktree="/external/retry"))
        )
        second.apply_task(
            "run", TaskEnvelope(2, SessionStarted("new-session", "actual-new-model"))
        )
        current = second.load_task("run")
        for event in (
            Completed({"result": "late"}, "actual-old-model"),
            SessionStarted("old-session", "old-model", "old-effort"),
            ProcessExited(-15),
            request,
            TaskReconciled("old-digest", False, "Stale reconciliation"),
        ):
            envelope = TaskEnvelope(1, event, "late-arrival")
            assert first.apply_task("run", envelope) is Disposition.HISTORY_ONLY
            assert first.load_task("run") == current
            assert first.load_task_history("run")[-1] == (
                envelope,
                Disposition.HISTORY_ONLY,
            )
        history = first.load_task_history("run")
    with TaskStore(path) as reopened:
        assert reopened.load_task("run") == current
        assert reopened.load_task_history("run") == history


def test_restart_requires_attempt_and_workspace_reconciliation(tmp_path: Path) -> None:
    path = tmp_path / "task.db"
    request = started()
    with TaskStore(path) as store:
        create(store)
        store.apply_task("run", TaskEnvelope(1, request))
        original = store.load_task("run")
        history = store.load_task_history("run")
    with TaskStore(path) as store:
        assert store.list_recoverable_tasks() == ("run",)
        assert store.load_task("run") == original
        assert store.load_task_history("run") == history
        with pytest.raises(ValueError):
            store.apply_task("run", TaskEnvelope(2, request))
        store.apply_task(
            "run", TaskEnvelope(1, TaskReconciled("first-digest", True, "Stopped"))
        )
        recovered = store.load_task("run")
        assert recovered.phase is TaskPhase.FAILED
        assert recovered.current_attempt.runtime.status is AttemptStatus.RUNNING
        assert recovered.current_attempt.runtime.failure_category is None
        assert recovered.current_attempt.runtime.exit_code is None
        store.apply_task(
            "run", TaskEnvelope(1, TaskReconciled("new-digest", False, "Still active"))
        )
        with pytest.raises(ValueError):
            store.apply_task("run", TaskEnvelope(2, request))
        store.apply_task(
            "run",
            TaskEnvelope(
                1, TaskReconciled("stopped-digest", True, "Confirmed stopped")
            ),
        )
        store.apply_task("run", TaskEnvelope(2, request))
        assert store.load_task("run").current_attempt.number == 2


def test_task_cancellation_preserves_terminal_and_exit(tmp_path: Path) -> None:
    with TaskStore(tmp_path / "task.db") as store:
        create(store)
        store.apply_task("run", TaskEnvelope(1, started()))
        store.apply_task("run", TaskEnvelope(1, CancelRequested()))
        diagnostic = TaskEnvelope(
            1,
            TaskDiagnostic(
                Error("cleanup incomplete", raw_type="adapter.cleanup_incomplete")
            ),
        )
        assert store.apply_task("run", diagnostic) is Disposition.HISTORY_ONLY
        assert store.load_task("run").phase is TaskPhase.CANCELLING
        assert store.load_task("run").current_attempt.runtime.exit_code is None
        terminal = TaskEnvelope(
            1,
            Completed({"late": [True, 1]}, "reported-model", raw_type="fixture.result"),
        )
        store.apply_task("run", terminal)
        assert store.load_task("run").phase is TaskPhase.CANCELLED
        store.apply_task(
            "run", TaskEnvelope(1, ProcessExited(-2, raw_type="fixture.exit"))
        )
        original = store.load_task("run")
        for event in (Completed("duplicate"), ProcessExited(0), Error("duplicate")):
            assert (
                store.apply_task("run", TaskEnvelope(1, event))
                is Disposition.HISTORY_ONLY
            )
        assert store.load_task("run") == original
        assert original.current_attempt.runtime.exit_code == -2
        assert original.current_attempt.runtime.reported_model == "reported-model"
        assert (terminal, Disposition.APPLIED) in store.load_task_history("run")


def test_task_success_requires_exit_for_delivery_and_scope_block_is_persisted(
    tmp_path: Path,
) -> None:
    path = tmp_path / "task.db"
    block = TaskScopeBlocked("Need a shared API change.", ("backend/api.py",))
    with TaskStore(path) as store:
        create(store)
        store.apply_task("run", TaskEnvelope(1, started()))
        store.apply_task("run", TaskEnvelope(1, Completed({"result": "done"})))
        state = store.load_task("run")
        assert state.phase is TaskPhase.AWAITING_DELIVERY
        assert not state.delivery_ready
        with pytest.raises(ValueError):
            store.apply_task("run", TaskEnvelope(1, block))
        store.apply_task("run", TaskEnvelope(1, ProcessExited(-15)))
        state = store.load_task("run")
        assert state.phase is TaskPhase.AWAITING_DELIVERY
        assert state.current_attempt.runtime.status is AttemptStatus.SUCCEEDED
        assert state.delivery_ready
        store.apply_task("run", TaskEnvelope(1, block))
        assert store.load_task("run").phase is TaskPhase.PLANNING_REQUIRED
        with pytest.raises(ValueError):
            store.apply_task("run", TaskEnvelope(2, started()))
    with TaskStore(path) as store:
        state = store.load_task("run")
        assert state.scope_block == block
        assert state.current_attempt.runtime.exit_code == -15
        assert state.current_attempt.runtime.result == {"result": "done"}
        assert store.list_recoverable_tasks() == ("run",)


def test_pending_list_retains_every_phase_without_restarting_tasks(
    tmp_path: Path,
) -> None:
    sequences = {
        "ready": (),
        "implementing": (started(),),
        "cancelling": (started(), CancelRequested()),
        "awaiting_delivery": (started(), Completed(None)),
        "failed": (started(), Error("auth expired", FailureCategory.AUTH_EXPIRED)),
        "cancelled": (started(), CancelRequested(), ProcessExited(130)),
        "planning_required": (
            started(),
            Completed(None),
            ProcessExited(0),
            TaskScopeBlocked("Need scope", ("other.py",)),
        ),
    }
    path = tmp_path / "task.db"
    with TaskStore(path) as store:
        for name, events in sequences.items():
            create(store, name)
            for event in events:
                store.apply_task(name, TaskEnvelope(1, event))
            assert store.load_task(name).phase.value == name
        assert store.list_recoverable_tasks() == tuple(sequences)
    with TaskStore(path) as store:
        assert store.list_recoverable_tasks() == tuple(sequences)
        for name, events in sequences.items():
            assert len(store.load_task_history(name)) == len(events)


def test_unknown_task_operations_fail_without_adding_records(tmp_path: Path) -> None:
    with TaskStore(tmp_path / "task.db") as store:
        for operation in (
            store.load_task,
            store.load_task_input,
            store.load_task_history,
        ):
            with pytest.raises(KeyError):
                operation("missing")
        with pytest.raises(KeyError):
            store.apply_task("missing", TaskEnvelope(1, started()))
        assert store.list_recoverable_tasks() == ()


def test_task_envelope_codec_preserves_all_events_and_evidence() -> None:
    events = (
        started(),
        TaskReconciled("digest", False, "still running"),
        TaskScopeBlocked("need more scope", ("other.py", "frontend/new.ts")),
        TaskDiagnostic(
            Error("diagnostic", None, None, raw_type="adapter.cleanup_incomplete")
        ),
        CancelRequested(),
        SessionStarted(
            "session", "reported-model", "reported-effort", raw_type="fixture.session"
        ),
        Message("消息", raw_type="fixture.message"),
        Usage({"tokens": 9, "cost": 0.25}, raw_type="fixture.usage"),
        Completed(
            {"nested": [{"flag": True, "values": [None, 9]}]},
            "actual",
            raw_type="fixture.result",
        ),
        Error(
            "throttled", FailureCategory.RATE_LIMITED, "later", raw_type="fixture.error"
        ),
        ProcessExited(-15, raw_type="fixture.exit"),
    )
    for event in events:
        envelope = TaskEnvelope(1, event, "fixed-time")
        restored = decode_task_envelope(encode_task_envelope(envelope))
        assert restored == envelope
        assert type(restored.event) is type(event)
    value = inputs()
    assert decode_task_input(encode_task_input(value)) == value


@pytest.mark.parametrize("status", list(AttemptStatus))
def test_task_attempt_codec_preserves_runtime_status_and_sources(
    status: AttemptStatus,
) -> None:
    runtime = AttemptState(
        1,
        status,
        FailureCategory.TIMEOUT,
        "detail",
        "later",
        "reported-session",
        "actual-model",
        EvidenceSource("completed", "fixture.result"),
        "actual-effort",
        EvidenceSource("session_started", "fixture.session"),
        {"report": [True, None, {"中文": "value"}]},
        -15,
    )
    attempt = TaskAttempt(
        started(), runtime, TaskReconciled("digest", True, "verified")
    )
    restored = decode_task_attempt(encode_task_attempt(attempt))
    assert restored == attempt
    assert restored.runtime.status is status
    assert restored.runtime.failure_category is FailureCategory.TIMEOUT
    assert isinstance(restored.runtime.reported_model_source, EvidenceSource)
    with pytest.raises(TypeError):
        restored.runtime.result["new"] = "mutated"


@pytest.mark.parametrize("old_version", [1, 2])
def test_task_migration_preserves_existing_reviews(
    tmp_path: Path, monkeypatch, old_version: int
) -> None:
    path = tmp_path / "old.db"
    resources = tmp_path / "old-resources"
    (resources / "migrations").mkdir(parents=True)
    migrations = sorted((ROOT / "src/multi_agent/store/migrations").glob("*.sql"))
    for migration in migrations:
        if int(migration.name.split("_", 1)[0]) <= old_version:
            (resources / "migrations" / migration.name).write_bytes(
                migration.read_bytes()
            )
    state = RoundState(
        "snapshot",
        "review-v1",
        (SeatState(SeatConfig("gpt", "codex", "requested", "high")),),
    )
    with monkeypatch.context() as patch:
        patch.setattr(database, "files", lambda package: resources)
        with ReviewStore(path) as store:
            if old_version == 1:
                store.create_round("review", state)
            else:
                store.save_snapshot(
                    "snapshot", {"candidate": "candidate", "nested": [True]}
                )
                store.create_review("review", state, {"created_at": "original"})
            store.apply("review", Envelope("gpt", 1, AttemptStarted()))
            if old_version == 2:
                store.set_execution(
                    "review", "gpt", 1, {"cli_version": "original-version"}
                )
            store.apply(
                "review",
                Envelope("gpt", 1, Completed({"report": ["kept"]}, "reported-model")),
            )
            original = store.load_round("review")
            history = store.load_history("review")
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (old_version,)
    for _ in range(2):
        with TaskStore(path) as store:
            assert store.load_round("review") == original
            assert store.load_history("review") == history
            if old_version == 2:
                assert store.load_snapshot("snapshot") == {
                    "candidate": "candidate",
                    "nested": [True],
                }
                assert store.load_review("review") == {"created_at": "original"}
                assert store.load_execution("review") == {
                    "gpt": {"1": {"cli_version": "original-version"}}
                }
                assert store.list_reviews() == ("review",)
            else:
                assert store.list_reviews() == ()
    with TaskStore(path) as store:
        create(store)
        store.apply_task("run", TaskEnvelope(1, started()))
        assert store.load_round("review") == original
        assert store.load_history("review") == history
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (6,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
