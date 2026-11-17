"""Planning evidence survives restart and binds a task in one SQLite transaction."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from threading import Barrier

import pytest

from multi_agent.cli.task_inputs import parse_project_rules, parse_task_card
from multi_agent.core import Disposition, transitions
from multi_agent.core.events import (
    CancelRequested,
    Completed,
    Error,
    FailureCategory,
    Message,
    ProcessExited,
    SessionStarted,
    Usage,
)
from multi_agent.core.planning import (
    DependencyReceipt,
    PlanningInput,
    PlanningPhase,
    PlanningRequest,
    PlanningReview,
    PlanningState,
    UserDecision,
    planning_hash,
    planning_value,
)
from multi_agent.core.planning_events import (
    PlanningAttemptStarted,
    PlanningDiagnostic,
    PlanningDraftValidated,
    PlanningEnvelope,
    PlanningEnvironmentObserved,
    PlanningEvidenceRejected,
    PlanningExecutionPrepared,
    PlanningReconciled,
    PlanningReviewValidated,
    PlanningTaskBound,
    PlanningValidationRejected,
)
from multi_agent.core.planning_execution import (
    PlanningAttemptContext,
    PlanningExecutionPolicy,
    PlanningProviderPolicy,
    PlanningRunOwner,
    prepared_for,
)
from multi_agent.core.snapshot import FrozenFile, FrozenSnapshot
from multi_agent.core.snapshot import TestEvidence as SnapshotTestEvidence
from multi_agent.core.task_state import TaskInput, TaskPhase
from multi_agent.core.tasks import FrozenProjectRules, TaskCard
from multi_agent.store import database
from multi_agent.store.planning_codec import (
    decode_planning_attempt,
    decode_planning_envelope,
    decode_planning_input,
    decode_planning_provenance,
    decode_planning_review,
    encode_planning_attempt,
    encode_planning_envelope,
    encode_planning_input,
    encode_planning_provenance,
    encode_planning_review,
)
from multi_agent.store.planning_store import PlanningStore
from multi_agent.store.task_store import TaskStore

BASELINE = "a" * 40
ROOT = Path(__file__).resolve().parents[2]


def inputs():
    raw = (
        json.dumps(
            {
                "version": 1,
                "commands": {"unit": {"executable": "pytest", "args": ["tests/unit"]}},
                "protected_paths": [".multi-agent/**"],
                "implementation": [],
                "default_implementation": {
                    "provider": "codex",
                    "default_model": "implementation",
                    "default_effort": "medium",
                    "allowed_models": ["implementation"],
                },
            }
        )
        + "\n"
    )
    frozen = FrozenProjectRules(
        "/synthetic/repo/.git", BASELINE, "b" * 40, raw, parse_project_rules(raw)
    )
    value = PlanningInput(
        "原始需求\r\n保留 Unicode 与换行。",
        frozen,
        PlanningRequest("claude", "requested-draft-model", "high"),
        PlanningRequest("codex", "requested-review-model", "high"),
        (
            DependencyReceipt(
                "T-0100",
                BASELINE,
                "user_declared",
                "user supplied baseline association",
            ),
        ),
        (
            UserDecision(
                "Which format?", "JSON", "user_declared", "explicit user answer"
            ),
        ),
    )
    card = TaskCard.from_dict(
        {
            "schema_version": 1,
            "id": "T-0101",
            "goal": "Implement the request.",
            "baseline_commit": BASELINE,
            "write_scope": ["src/**"],
            "depends_on": ["T-0100"],
            "interface_contracts": [
                {"name": "API", "definition": "Keep existing callers."}
            ],
            "deliverables": ["Implementation."],
            "acceptance_criteria": ["Unit tests pass."],
            "test_refs": ["unit"],
            "open_questions": [
                {
                    "question": "Which format?",
                    "blocks_dispatch": True,
                    "resolution": "JSON",
                }
            ],
            "proposed_tests": [
                {
                    "name": "suggested",
                    "executable": "never-run",
                    "args": ["--new"],
                    "reason": "proposal only",
                }
            ],
        }
    )
    return value, card


def sequence(value, card):
    review = PlanningReview(
        card.task_hash, "ready", "Checked the original requirements.", ()
    )
    events = []
    for stage, request, result, validation in (
        ("draft", value.draft_request, card.to_dict(), PlanningDraftValidated(card)),
        (
            "review",
            value.review_request,
            planning_value(review),
            PlanningReviewValidated(review),
        ),
    ):
        emitted = (
            PlanningAttemptStarted(
                request,
                f"exact {stage} prompt\r\n",
                f"{stage}-v1",
                "fixed-start",
                {"cli_version": None, "nested": ["reported"]},
                1 if stage == "review" else None,
                card.task_hash if stage == "review" else None,
            ),
            SessionStarted(f"{stage}-session", raw_type="synthetic.init"),
            Message("progress", raw_type="synthetic.message"),
            Usage({"input_tokens": 11, "output_tokens": 7}, raw_type="synthetic.usage"),
            Completed(
                result,
                reported_model="actual-draft" if stage == "draft" else None,
                raw_type="synthetic.result",
            ),
            ProcessExited(-15, raw_type="synthetic.exit"),
            PlanningReconciled(
                True, "observed stopped", f"/external/{stage}.jsonl", "c" * 64
            ),
            validation,
        )
        events.extend(
            PlanningEnvelope(stage, 1, event, f"time-{len(events) + index}")
            for index, event in enumerate(emitted)
        )
    return tuple(events)


def ready(store, run_id="plan", value=None, card=None):
    default_value, default_card = inputs()
    value = default_value if value is None else value
    card = default_card if card is None else card
    store.create_planning(run_id, value, "created-at")
    envelopes = sequence(value, card)
    expected = PlanningState(value)
    for envelope in envelopes:
        expected, disposition = transitions.apply_planning(expected, envelope)
        assert disposition == Disposition.APPLIED
        assert store.apply_planning(run_id, envelope) == disposition
    assert store.load_planning(run_id) == expected
    assert expected.can_bind
    return value, card, envelopes


def test_planning_inputs_evidence_and_hashes_round_trip_after_restart(tmp_path):
    path = tmp_path / "planning.db"
    with PlanningStore(path) as store:
        value, card, envelopes = ready(store)
        state = store.load_planning("plan")
        with pytest.raises(FrozenInstanceError):
            store.load_planning_input("plan").requirements = "replacement"
        with pytest.raises(TypeError):
            state.current("draft").request.environment["cli_version"] = "invented"
        with pytest.raises(sqlite3.IntegrityError):
            store.create_planning("plan", replace(value, requirements="other"), "later")
        assert store.load_planning("plan") == state
        assert store.list_recoverable_planning() == ("plan",)
    with PlanningStore(path) as store:
        assert store.load_planning("plan") == state
        assert store.load_planning_input("plan") == value
        assert store.load_planning_history("plan") == tuple(
            (event, Disposition.APPLIED) for event in envelopes
        )
        provenance = store.load_planning("plan").provenance()
        assert provenance.card == card
        assert provenance.planning_input.input_hash == value.input_hash
        assert provenance.draft.request.prompt == "exact draft prompt\r\n"
        assert provenance.draft.runtime.reported_model == "actual-draft"
        assert provenance.review.runtime.reported_model is None
        assert provenance.review.runtime.reported_effort is None
        assert provenance.review.runtime.exit_code == -15
        assert provenance.review.reconciliation.log_sha256 == "c" * 64
        assert isinstance(provenance.review.events[3], Usage)
        assert provenance.review.events[3].counts == {
            "input_tokens": 11,
            "output_tokens": 7,
        }
        assert decode_planning_input(encode_planning_input(value)) == value
        assert (
            decode_planning_review(encode_planning_review(provenance.review_artifact))
            == provenance.review_artifact
        )
        assert (
            decode_planning_attempt(encode_planning_attempt(provenance.draft))
            == provenance.draft
        )
        assert (
            decode_planning_provenance(encode_planning_provenance(provenance)).plan_hash
            == provenance.plan_hash
        )


@pytest.mark.parametrize(
    "event",
    [
        CancelRequested(),
        Error(
            "limited", FailureCategory.RATE_LIMITED, "later", raw_type="synthetic.limit"
        ),
        PlanningDiagnostic(
            Error("cleanup unknown", raw_type="adapter.cleanup_incomplete")
        ),
        PlanningReconciled(False, "unknown", None, None),
        PlanningValidationRejected("invalid artifact"),
        PlanningTaskBound("task"),
    ],
)
def test_planning_codec_preserves_nonhappy_events(event):
    envelope = PlanningEnvelope("review", 2, event, "fixed-time")
    assert decode_planning_envelope(encode_planning_envelope(envelope)) == envelope


def test_planned_task_binding_is_atomic_and_immutable(tmp_path):
    path = tmp_path / "planning.db"
    with PlanningStore(path) as store:
        value, card, envelopes = ready(store)
        original = store.load_planning("plan").provenance()
        store.create_planned_task("task", "plan", "task-created")
        bound = store.load_planning("plan")
        assert bound.phase == PlanningPhase.BOUND and bound.bound_task_run_id == "task"
        assert bound.provenance() == original
        assert store.load_task_input("task") == TaskInput(
            card, value.frozen_rules, value.requirements, "task-created"
        )
        assert store.load_task("task").phase == TaskPhase.READY
        assert store.load_task_provenance("task") == original
        assert store.load_task_history("task") == ()
        assert store.list_recoverable_planning() == ()
        last, disposition = store.load_planning_history("plan")[-1]
        assert last == PlanningEnvelope(
            "review", 1, PlanningTaskBound("task"), "task-created"
        )
        assert disposition == Disposition.APPLIED
        assert len(store.load_planning_history("plan")) == len(envelopes) + 1
        for late in (
            PlanningEnvelope(
                "draft", 1, PlanningReconciled(False, "late contradiction")
            ),
            PlanningEnvelope("review", 1, Completed({"late": True})),
            PlanningEnvelope("review", 1, Usage({"late": 99})),
            PlanningEnvelope("review", 1, ProcessExited(23)),
        ):
            assert store.apply_planning("plan", late) == Disposition.HISTORY_ONLY
        assert store.load_planning("plan") == bound
        assert store.load_task_provenance("task").plan_hash == original.plan_hash
        with pytest.raises(ValueError):
            store.create_planned_task("another-task", "plan", "later")
        with pytest.raises(KeyError):
            store.load_task("another-task")
    with PlanningStore(path) as store:
        assert store.load_task_provenance("task") == original
        assert store.load_planning("plan") == bound


def test_planning_public_apply_cannot_publish_a_task_binding(tmp_path):
    with PlanningStore(tmp_path / "planning.db") as store:
        _, _, envelopes = ready(store)
        before = store.load_planning("plan")
        with pytest.raises(TypeError, match="create_planned_task"):
            store.apply_planning(
                "plan", PlanningEnvelope("review", 1, PlanningTaskBound("uncreated"))
            )
        assert store.load_planning("plan") == before
        assert len(store.load_planning_history("plan")) == len(envelopes)
        with pytest.raises(KeyError):
            store.load_task("uncreated")


@pytest.mark.parametrize(
    "table,operation",
    [
        ("task_runs", "INSERT"),
        ("task_planning_provenance", "INSERT"),
        ("planning_runs", "UPDATE"),
        ("planning_history", "INSERT"),
    ],
)
def test_planning_binding_failure_rolls_back_every_written_table(
    tmp_path, table, operation
):
    path = tmp_path / "planning.db"
    with PlanningStore(path) as store:
        ready(store)
        before = store.load_planning("plan")
        history = store.load_planning_history("plan")
        store._connection.execute(
            f"CREATE TRIGGER fail_binding BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, 'synthetic binding failure'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="synthetic binding failure"):
            store.create_planned_task("task", "plan", "task-created")
        assert store.load_planning("plan") == before
        assert store.load_planning_history("plan") == history
        with pytest.raises(KeyError):
            store.load_task("task")
        with pytest.raises(KeyError):
            store.load_task_provenance("task")
        store._connection.execute("DROP TRIGGER fail_binding")
        store.create_planned_task("task", "plan", "task-created")
        assert store.load_task_provenance("task") == before.provenance()


def test_planning_apply_history_failure_rolls_back_attempt_and_phase(tmp_path):
    value, card = inputs()
    first = sequence(value, card)[0]
    with PlanningStore(tmp_path / "planning.db") as store:
        store.create_planning("plan", value, "created")
        store._connection.execute(
            "CREATE TRIGGER fail_history BEFORE INSERT ON planning_history BEGIN SELECT RAISE(ABORT, 'synthetic history failure'); END"
        )
        with pytest.raises(sqlite3.IntegrityError):
            store.apply_planning("plan", first)
        assert store.load_planning("plan") == PlanningState(value)
        assert store.load_planning_history("plan") == ()
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM planning_attempts"
            ).fetchone()[0]
            == 0
        )


def test_planning_two_connections_can_bind_only_one_task(tmp_path):
    path = tmp_path / "planning.db"
    with PlanningStore(path) as store:
        _, _, envelopes = ready(store)
    barrier = Barrier(2)

    def bind(task_id):
        with PlanningStore(path) as store:
            barrier.wait(timeout=5)
            try:
                store.create_planned_task(task_id, "plan", "same-time")
            except (ValueError, sqlite3.IntegrityError):
                return task_id, False
            return task_id, True

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(bind, ("task-one", "task-two")))
    winners = [name for name, succeeded in outcomes if succeeded]
    losers = [name for name, succeeded in outcomes if not succeeded]
    assert len(winners) == len(losers) == 1
    with PlanningStore(path) as store:
        assert store.load_planning("plan").bound_task_run_id == winners[0]
        assert store.list_recoverable_tasks() == (winners[0],)
        assert len(store.load_planning_history("plan")) == len(envelopes) + 1
        assert store.load_task_provenance(winners[0]).card.id == "T-0101"
        with pytest.raises(KeyError):
            store.load_task(losers[0])


def test_latest_draft_stop_reconciliation_revokes_binding_before_insert(tmp_path):
    with PlanningStore(tmp_path / "planning.db") as store:
        ready(store)
        assert (
            store.apply_planning(
                "plan",
                PlanningEnvelope(
                    "draft", 1, PlanningReconciled(False, "new stop uncertainty")
                ),
            )
            == Disposition.APPLIED
        )
        before = store.load_planning("plan")
        assert not before.can_bind
        with pytest.raises(ValueError):
            store.create_planned_task("task", "plan", "created")
        assert store.load_planning("plan") == before
        with pytest.raises(KeyError):
            store.load_task("task")


def test_planning_store_cannot_validate_a_different_draft_than_completed(tmp_path):
    value, card = inputs()
    other = parse_task_card(
        json.dumps(
            replace(card, goal="A different, structurally valid goal.").to_dict()
        )
    )
    assert other.task_hash != card.task_hash
    with PlanningStore(tmp_path / "planning.db") as store:
        store.create_planning("plan", value, "created")
        for envelope in sequence(value, card):
            if isinstance(envelope.event, PlanningDraftValidated):
                validation = envelope
                break
            assert store.apply_planning("plan", envelope) == Disposition.APPLIED
        before = store.load_planning("plan")
        history = store.load_planning_history("plan")
        assert planning_value(before.current("draft").runtime.result) == card.to_dict()
        with pytest.raises(ValueError):
            store.apply_planning(
                "plan", replace(validation, event=PlanningDraftValidated(other))
            )
        assert store.load_planning("plan") == before
        assert store.load_planning_history("plan") == history
        with pytest.raises(ValueError):
            store.create_planned_task("forged", "plan", "created")
        with pytest.raises(KeyError):
            store.load_task("forged")
        assert store.apply_planning("plan", validation) == Disposition.APPLIED
        assert store.load_planning("plan").card == card


def test_planning_store_cannot_turn_completed_revision_request_into_ready(tmp_path):
    value, card = inputs()
    rejected = PlanningReview(
        card.task_hash,
        "needs_revision",
        "Required behavior is missing.",
        ("missing behavior",),
    )
    with PlanningStore(tmp_path / "planning.db") as store:
        store.create_planning("plan", value, "created")
        for envelope in sequence(value, card):
            if isinstance(envelope.event, PlanningReviewValidated):
                validation = envelope
                break
            if envelope.stage == "review" and isinstance(envelope.event, Completed):
                envelope = replace(
                    envelope,
                    event=replace(envelope.event, result=planning_value(rejected)),
                )
            assert store.apply_planning("plan", envelope) == Disposition.APPLIED
        before = store.load_planning("plan")
        history = store.load_planning_history("plan")
        assert before.current("review").runtime.result["decision"] == "needs_revision"
        with pytest.raises(ValueError):
            store.apply_planning("plan", validation)
        assert store.load_planning("plan") == before
        assert store.load_planning_history("plan") == history
        with pytest.raises(ValueError):
            store.create_planned_task("forged", "plan", "created")
        with pytest.raises(KeyError):
            store.load_task_provenance("forged")
        corrected = replace(validation, event=PlanningReviewValidated(rejected))
        assert store.apply_planning("plan", corrected) == Disposition.APPLIED
        assert store.load_planning("plan").phase == PlanningPhase.REVISION_REQUIRED


def test_legacy_task_input_does_not_acquire_planning_provenance(tmp_path):
    value, card = inputs()
    with PlanningStore(tmp_path / "planning.db") as store:
        store.create_task(
            "legacy", card, value.frozen_rules, value.requirements, "created"
        )
        assert store.load_task("legacy").phase == TaskPhase.READY
        with pytest.raises(KeyError):
            store.load_task_provenance("legacy")
        ready(store)
        state = store.load_planning("plan")
        with pytest.raises(sqlite3.IntegrityError):
            store.create_planned_task("legacy", "plan", "later")
        assert store.load_planning("plan") == state
        with pytest.raises(KeyError):
            store.load_task_provenance("legacy")


def test_planning_records_do_not_deduplicate_same_rules_hash_across_inputs(tmp_path):
    value, _ = inputs()
    other = replace(
        value,
        requirements="different requirement",
        frozen_rules=replace(value.frozen_rules, repository="/another/repo/.git"),
    )
    with PlanningStore(tmp_path / "planning.db") as store:
        store.create_planning("one", value, "first")
        store.create_planning("two", other, "second")
        assert value.frozen_rules.rules_hash == other.frozen_rules.rules_hash
        assert value.input_hash != other.input_hash
        assert store.load_planning_input("one") == value
        assert store.load_planning_input("two") == other
        assert store.list_recoverable_planning() == ("one", "two")


def test_planning_v4_migration_preserves_a_real_v3_task(tmp_path, monkeypatch):
    path = tmp_path / "upgraded.db"
    resources = tmp_path / "old-resources"
    (resources / "migrations").mkdir(parents=True)
    for migration in (ROOT / "src/multi_agent/store/migrations").glob("*.sql"):
        if int(migration.name.split("_", 1)[0]) <= 3:
            (resources / "migrations" / migration.name).write_bytes(
                migration.read_bytes()
            )
    value, card = inputs()
    with monkeypatch.context() as context:
        context.setattr(database, "files", lambda package: resources)
        with TaskStore(path) as store:
            store.create_task(
                "legacy", card, value.frozen_rules, value.requirements, "original-time"
            )
            original_state = store.load_task("legacy")
            original_input = store.load_task_input("legacy")
            assert store._connection.execute("PRAGMA user_version").fetchone()[0] == 3
    with PlanningStore(path) as store:
        assert store._connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert store.load_task("legacy") == original_state
        assert store.load_task_input("legacy") == original_input
        assert store.load_task_history("legacy") == ()
        assert store.list_recoverable_planning() == ()
        with pytest.raises(KeyError):
            store.load_task_provenance("legacy")
        ready(store)
        store.create_planned_task("new", "plan", "new-time")
        assert store.load_task("legacy") == original_state


def test_planning_codec_unknown_stored_event_is_not_repaired():
    with pytest.raises(ValueError, match="unknown stored planning event"):
        decode_planning_envelope(
            json.dumps(
                {
                    "stage": "draft",
                    "attempt": 1,
                    "occurred_at": None,
                    "event": {"kind": "not-a-planning-event"},
                }
            )
        )


def test_unknown_planning_runs_raise_without_history_or_task_writes(tmp_path):
    value, card = inputs()
    with PlanningStore(tmp_path / "planning.db") as store:
        for operation in (
            lambda: store.load_planning("missing"),
            lambda: store.load_planning_input("missing"),
            lambda: store.load_planning_history("missing"),
            lambda: store.apply_planning("missing", sequence(value, card)[0]),
            lambda: store.create_planned_task("new-task", "missing", "created"),
        ):
            with pytest.raises(KeyError):
                operation()
        assert store.list_recoverable_planning() == ()
        assert store.list_recoverable_tasks() == ()


def planning_source(value):
    return FrozenSnapshot(
        value.frozen_rules.repository,
        value.frozen_rules.baseline_commit,
        value.frozen_rules.baseline_commit,
        "",
        value.requirements,
        ("Plan from the frozen requirements and source.",),
        (),
        (
            FrozenFile(
                "candidate", "src/main.py", "frozen source\n", "d" * 40, "100644"
            ),
        ),
    )


def execution_policy(*, draft_attempts=2, review_attempts=2):
    return PlanningExecutionPolicy(
        (
            PlanningProviderPolicy(
                "claude", "/synthetic/bin/claude", (10, 11), "a" * 64, draft_attempts
            ),
            PlanningProviderPolicy(
                "codex", "/synthetic/bin/codex", (10, 12), "b" * 64, review_attempts
            ),
        ),
        90.0,
        0.5,
    )


def reservation(value, source, *, stage="draft", attempt=1, policy=None, card=None):
    owner = PlanningRunOwner("/external/planning", (20, 21), "owner-token", "c" * 64)
    context = PlanningAttemptContext(
        f"/external/planning/{stage}-{attempt}",
        (20, (100 if stage == "draft" else 200) + attempt),
        f"{stage}-{attempt}-token",
        "d" * 64,
        "e" * 64,
    )
    started = PlanningEnvelope(
        stage,
        attempt,
        PlanningAttemptStarted(
            value.draft_request if stage == "draft" else value.review_request,
            f"exact {stage} launch prompt\n",
            "planning-execution-v1",
            "fixed-start",
            draft_attempt=1 if stage == "review" else None,
            task_hash=card.task_hash if stage == "review" else None,
        ),
        "start-time",
    )
    prepared = PlanningEnvelope(
        stage,
        attempt,
        PlanningExecutionPrepared(
            source.snapshot_id,
            execution_policy() if policy is None else policy,
            owner,
            context,
        ),
        "prepared-time",
    )
    return started, prepared


def create_reservable_plan(store, *, policy=None):
    value, card = inputs()
    source = planning_source(value)
    store.create_planning("plan", value, "created")
    store.save_snapshot(source.snapshot_id, source.to_dict())
    return value, card, source, reservation(value, source, policy=policy)


def finish_reserved_draft(store, value, card):
    for envelope in sequence(value, card)[1:8]:
        assert store.apply_planning("plan", envelope) == Disposition.APPLIED
    assert store.load_planning("plan").phase == PlanningPhase.AWAITING_REVIEW


def fail_reserved_attempt(store, stage, attempt):
    for event in (
        Error("synthetic preflight failure"),
        ProcessExited(2),
        PlanningReconciled(
            True, "observed stopped", "/external/failed.jsonl", "f" * 64
        ),
    ):
        assert (
            store.apply_planning("plan", PlanningEnvelope(stage, attempt, event))
            == Disposition.APPLIED
        )


def test_planning_reservation_is_atomic_and_blocks_unknown_stop(tmp_path):
    path = tmp_path / "reservation.db"
    with PlanningStore(path) as store:
        value, _, source, pair = create_reservable_plan(store)
        assert store.reserve_planning_attempt("plan", *pair) is None
        state = store.load_planning("plan")
        assert state.phase == PlanningPhase.DRAFTING
        assert state.current("draft").events == tuple(item.event for item in pair)
        assert not state.current("draft").stopped
        assert store.load_planning_history("plan") == tuple(
            (item, Disposition.APPLIED) for item in pair
        )
    with PlanningStore(path) as store:
        before = store.load_planning("plan")
        history = store.load_planning_history("plan")
        for attempted in (pair, reservation(value, source, attempt=2)):
            with pytest.raises(ValueError):
                store.reserve_planning_attempt("plan", *attempted)
            assert store.load_planning("plan") == before
            assert store.load_planning_history("plan") == history
        fail_reserved_attempt(store, "draft", 1)
        store.apply_planning(
            "plan",
            PlanningEnvelope(
                "draft", 1, PlanningReconciled(False, "stop unknown again")
            ),
        )
        revoked = store.load_planning("plan")
        history = store.load_planning_history("plan")
        with pytest.raises(ValueError):
            store.reserve_planning_attempt(
                "plan", *reservation(value, source, attempt=2)
            )
        assert store.load_planning("plan") == revoked
        assert store.load_planning_history("plan") == history


@pytest.mark.parametrize(
    "table,operation,condition",
    [
        ("planning_attempts", "INSERT", ""),
        ("planning_runs", "UPDATE", ""),
        ("planning_history", "INSERT", "WHEN NEW.sequence = 1"),
        ("planning_history", "INSERT", "WHEN NEW.sequence = 2"),
    ],
)
def test_planning_reservation_sql_failures_leave_no_partial_attempt(
    tmp_path, table, operation, condition
):
    with PlanningStore(tmp_path / "rollback.db") as store:
        value, _, source, pair = create_reservable_plan(store)
        store._connection.execute(
            f"CREATE TRIGGER fail_reservation BEFORE {operation} ON {table} {condition} BEGIN SELECT RAISE(ABORT, 'injected reservation failure'); END"
        )
        with pytest.raises(
            sqlite3.IntegrityError, match="injected reservation failure"
        ):
            store.reserve_planning_attempt("plan", *pair)
        assert store.load_planning("plan") == PlanningState(value)
        assert store.load_planning_history("plan") == ()
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM planning_attempts"
            ).fetchone()[0]
            == 0
        )
        assert store.load_snapshot(source.snapshot_id) == source.to_dict()
        store._connection.execute("DROP TRIGGER fail_reservation")
        store.reserve_planning_attempt("plan", *pair)
        assert len(store.load_planning_history("plan")) == 2


@pytest.mark.parametrize("which", ["started", "prepared"])
def test_planning_reservation_rejects_nonapplied_fold_without_writes(
    tmp_path, monkeypatch, which
):
    original = transitions.apply_planning

    def history_only(state, envelope):
        updated, disposition = original(state, envelope)
        selected = (
            PlanningAttemptStarted if which == "started" else PlanningExecutionPrepared
        )
        if isinstance(envelope.event, selected):
            return updated, Disposition.HISTORY_ONLY
        return updated, disposition

    with PlanningStore(tmp_path / "nonapplied.db") as store:
        value, _, _, pair = create_reservable_plan(store)
        monkeypatch.setattr(transitions, "apply_planning", history_only)
        with pytest.raises(ValueError):
            store.reserve_planning_attempt("plan", *pair)
        assert store.load_planning("plan") == PlanningState(value)
        assert store.load_planning_history("plan") == ()


@pytest.mark.parametrize(
    "mismatch", ["stage", "attempt", "start-type", "prepared-type", "environment"]
)
def test_planning_reservation_rejects_mismatched_envelopes_atomically(
    tmp_path, mismatch
):
    with PlanningStore(tmp_path / "envelopes.db") as store:
        value, _, _, (started, prepared) = create_reservable_plan(store)
        if mismatch == "stage":
            prepared = replace(prepared, stage="review")
        elif mismatch == "attempt":
            prepared = replace(prepared, attempt=2)
        elif mismatch == "start-type":
            started = replace(started, event=Message("not a start"))
        elif mismatch == "prepared-type":
            prepared = replace(prepared, event=Message("not a preparation"))
        else:
            started = replace(
                started, event=replace(started.event, environment={"unreserved": True})
            )
        with pytest.raises(TypeError if mismatch.endswith("type") else ValueError):
            store.reserve_planning_attempt("plan", started, prepared)
        assert store.load_planning("plan") == PlanningState(value)
        assert store.load_planning_history("plan") == ()


@pytest.mark.parametrize(
    "changed",
    [
        {"repository": "/other/repo/.git"},
        {"baseline": "e" * 40},
        {"candidate": "e" * 40},
        {"requirements": "different requirements"},
        {"diff": "a change"},
        {
            "tests": (
                SnapshotTestEvidence(BASELINE, ("pytest",), 0, "not a planning source"),
            )
        },
        {"required_tests": (("pytest",),)},
    ],
)
def test_planning_reservation_validates_stored_source_binding(tmp_path, changed):
    with PlanningStore(tmp_path / "source.db") as store:
        value, _, source, (started, prepared) = create_reservable_plan(store)
        other = replace(source, **changed)
        store.save_snapshot(other.snapshot_id, other.to_dict())
        prepared = replace(
            prepared,
            event=replace(prepared.event, source_snapshot_id=other.snapshot_id),
        )
        with pytest.raises(ValueError, match="source snapshot"):
            store.reserve_planning_attempt("plan", started, prepared)
        assert store.load_planning("plan") == PlanningState(value)
        assert store.load_planning_history("plan") == ()


@pytest.mark.parametrize("corruption", ["missing", "wrong-id", "changed-content"])
def test_planning_reservation_rechecks_snapshot_content_identity(tmp_path, corruption):
    with PlanningStore(tmp_path / "source-id.db") as store:
        value, _, source, (started, prepared) = create_reservable_plan(store)
        if corruption == "changed-content":
            damaged = replace(
                source, files=(replace(source.files[0], text="tampered\n"),)
            )
            store._connection.execute(
                "UPDATE snapshots SET payload = ? WHERE snapshot_id = ?",
                (json.dumps(damaged.to_dict()), source.snapshot_id),
            )
        else:
            if corruption == "wrong-id":
                store.save_snapshot("0" * 64, source.to_dict())
            prepared = replace(
                prepared, event=replace(prepared.event, source_snapshot_id="0" * 64)
            )
        with pytest.raises(KeyError if corruption == "missing" else ValueError):
            store.reserve_planning_attempt("plan", started, prepared)
        assert store.load_planning("plan") == PlanningState(value)
        assert store.load_planning_history("plan") == ()


def test_planning_reservation_two_connections_have_one_winner(tmp_path):
    path = tmp_path / "race.db"
    with PlanningStore(path) as store:
        _, _, _, pair = create_reservable_plan(store)
    barrier = Barrier(2)

    def reserve(label):
        started, prepared = pair
        context = prepared.event.attempt_context
        prepared = replace(
            prepared,
            event=replace(
                prepared.event, attempt_context=replace(context, token=label)
            ),
        )
        with PlanningStore(path) as store:
            barrier.wait(timeout=5)
            try:
                store.reserve_planning_attempt("plan", started, prepared)
            except ValueError:
                return label, None
            return label, prepared

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(reserve, ("first-context-token", "second-context-token"))
        )
    winners = [
        (label, prepared) for label, prepared in outcomes if prepared is not None
    ]
    assert len(winners) == 1
    with PlanningStore(path) as store:
        label, prepared = winners[0]
        state = store.load_planning("plan")
        assert len(state.draft_attempts) == 1
        assert prepared_for(state.current("draft")).attempt_context.token == label
        assert store.load_planning_history("plan") == (
            (pair[0], Disposition.APPLIED),
            (prepared, Disposition.APPLIED),
        )


def test_planning_execution_policy_and_source_survive_restart(tmp_path):
    path = tmp_path / "execution-restart.db"
    with PlanningStore(path) as store:
        value, card, source, pair = create_reservable_plan(store)
        store.reserve_planning_attempt("plan", *pair)
        finish_reserved_draft(store, value, card)
        review_pair = reservation(value, source, stage="review", card=card)
        store.reserve_planning_attempt("plan", *review_pair)
        before = store.load_planning("plan")
        history = store.load_planning_history("plan")
    with PlanningStore(path) as store:
        state = store.load_planning("plan")
        assert state == before
        assert store.load_planning_history("plan") == history
        assert store.load_snapshot(source.snapshot_id) == source.to_dict()
        for stage, expected in (
            ("draft", pair[1].event),
            ("review", review_pair[1].event),
        ):
            actual = prepared_for(state.current(stage))
            assert actual == expected
            assert isinstance(actual.policy.providers, tuple)
            assert isinstance(actual.policy.providers[0].executable_identity, tuple)
            assert isinstance(actual.run_owner.identity, tuple)
            assert isinstance(actual.attempt_context.identity, tuple)
            with pytest.raises(FrozenInstanceError):
                actual.policy.timeout = 1
            assert (
                decode_planning_envelope(
                    encode_planning_envelope(PlanningEnvelope(stage, 1, actual))
                ).event
                == actual
            )
        # The old draft's identical preparation is retained only as history.
        assert store.apply_planning("plan", pair[1]) == Disposition.HISTORY_ONLY
        assert store.load_planning("plan") == state
        fail_reserved_attempt(store, "review", 1)
        retry = reservation(value, source, stage="review", attempt=2, card=card)
        store.reserve_planning_attempt("plan", *retry)
        current = store.load_planning("plan")
        assert store.apply_planning("plan", review_pair[1]) == Disposition.HISTORY_ONLY
        assert store.load_planning("plan") == current
        assert prepared_for(current.current("review")) == retry[1].event


@pytest.mark.parametrize(
    "change",
    [
        "source",
        "timeout",
        "executable",
        "budget",
        "owner-root",
        "owner-token",
        "owner-identity",
        "owner-manifest",
        "context-path",
        "context-identity",
        "context-token",
    ],
)
def test_planning_reservation_cannot_change_frozen_execution_or_reuse_context(
    tmp_path, change
):
    with PlanningStore(tmp_path / "frozen-execution.db") as store:
        value, card, source, pair = create_reservable_plan(store)
        store.reserve_planning_attempt("plan", *pair)
        finish_reserved_draft(store, value, card)
        started, prepared = reservation(value, source, stage="review", card=card)
        event = prepared.event
        if change == "source":
            other = replace(
                source, files=(replace(source.files[0], text="other frozen source\n"),)
            )
            store.save_snapshot(other.snapshot_id, other.to_dict())
            event = replace(event, source_snapshot_id=other.snapshot_id)
        elif change == "timeout":
            event = replace(event, policy=replace(event.policy, timeout=100.0))
        elif change in ("executable", "budget"):
            provider = event.policy.providers[1]
            provider = replace(
                provider,
                **(
                    {"executable": "/different/codex"}
                    if change == "executable"
                    else {"max_attempts": 3}
                ),
            )
            event = replace(
                event,
                policy=replace(
                    event.policy, providers=(event.policy.providers[0], provider)
                ),
            )
        elif change.startswith("owner-"):
            field = {
                "owner-root": "root",
                "owner-token": "token",
                "owner-identity": "identity",
                "owner-manifest": "manifest_sha256",
            }[change]
            replacement = (99, 99) if field == "identity" else "replacement"
            event = replace(
                event, run_owner=replace(event.run_owner, **{field: replacement})
            )
        else:
            field = change.removeprefix("context-")
            original = getattr(pair[1].event.attempt_context, field)
            event = replace(
                event,
                attempt_context=replace(event.attempt_context, **{field: original}),
            )
        before = store.load_planning("plan")
        history = store.load_planning_history("plan")
        with pytest.raises(ValueError):
            store.reserve_planning_attempt(
                "plan", started, replace(prepared, event=event)
            )
        assert store.load_planning("plan") == before
        assert store.load_planning_history("plan") == history
        store.reserve_planning_attempt(
            "plan", *reservation(value, source, stage="review", card=card)
        )


@pytest.mark.parametrize("stage", ["draft", "review"])
def test_planning_reservation_preflight_failure_consumes_provider_budget(
    tmp_path, stage
):
    policy = execution_policy(draft_attempts=1, review_attempts=1)
    path = tmp_path / "budget.db"
    with PlanningStore(path) as store:
        value, card, source, pair = create_reservable_plan(store, policy=policy)
        store.reserve_planning_attempt("plan", *pair)
        if stage == "review":
            finish_reserved_draft(store, value, card)
            store.reserve_planning_attempt(
                "plan",
                *reservation(value, source, stage="review", card=card, policy=policy),
            )
        fail_reserved_attempt(store, stage, 1)
    with PlanningStore(path) as store:
        before = store.load_planning("plan")
        history = store.load_planning_history("plan")
        with pytest.raises(ValueError, match="budget"):
            store.reserve_planning_attempt(
                "plan",
                *reservation(
                    value, source, stage=stage, attempt=2, card=card, policy=policy
                ),
            )
        assert store.load_planning("plan") == before
        assert store.load_planning_history("plan") == history
        assert store.load_planning("plan").current(stage).stopped


def test_planning_reservation_rejects_legacy_attempt_chain(tmp_path):
    with PlanningStore(tmp_path / "legacy-chain.db") as store:
        value, card, source, _ = create_reservable_plan(store)
        for envelope in sequence(value, card)[:8]:
            store.apply_planning("plan", envelope)
        before = store.load_planning("plan")
        history = store.load_planning_history("plan")
        with pytest.raises(ValueError, match="legacy"):
            store.reserve_planning_attempt(
                "plan", *reservation(value, source, stage="review", card=card)
            )
        assert store.load_planning("plan") == before
        assert store.load_planning_history("plan") == history


def test_legacy_planning_provenance_hash_survives_execution_events(tmp_path):
    expected_hash = "f74d97b94e76d526dce7ff4ca6a65bad03a5b367b07313ed5e4a49ad9fbbcd7e"
    path = tmp_path / "legacy-hash.db"
    with PlanningStore(path) as store:
        value, card, _ = ready(store, run_id="legacy")
        original = store.load_planning("legacy").provenance()
        assert original.plan_hash == expected_hash
        store.create_planned_task("legacy-task", "legacy", "bound-at")
        source = planning_source(value)
        store.save_snapshot(source.snapshot_id, source.to_dict())
        store.create_planning("prepared", value, "created")
        pair = reservation(value, source)
        store.reserve_planning_attempt("prepared", *pair)
        assert store.apply_planning("legacy", pair[1]) == Disposition.HISTORY_ONLY
        assert store.load_task_provenance("legacy-task") == original
    with PlanningStore(path) as store:
        assert store.load_planning("legacy").provenance().plan_hash == expected_hash
        assert store.load_task_provenance("legacy-task").plan_hash == expected_hash
        assert "planning_execution_prepared" not in encode_planning_provenance(original)
        assert (
            decode_planning_provenance(encode_planning_provenance(original)).plan_hash
            == expected_hash
        )
        assert store.load_planning("legacy").card == card
        assert store.load_planning_history("legacy")[-1] == (
            pair[1],
            Disposition.HISTORY_ONLY,
        )


def v2_reservation(value, source, **kwargs):
    started, prepared = reservation(value, source, **kwargs)
    return (
        replace(
            started,
            event=replace(
                started.event,
                prompt_template_version=f"planning-{started.stage}-v2",
            ),
        ),
        prepared,
    )


def history_prefix(store):
    history = store.load_planning_history("plan")
    return {
        "expected_history_length": len(history),
        "expected_history_hash": planning_hash(history),
    }


@pytest.mark.parametrize("stage", ["draft", "review"])
@pytest.mark.parametrize("missing", ["both", "length", "hash"])
def test_planning_v2_reservation_requires_complete_history_prefix(
    tmp_path, stage, missing
):
    with PlanningStore(tmp_path / "required-prefix.db") as store:
        value, card, source, pair = create_reservable_plan(store)
        if stage == "review":
            store.reserve_planning_attempt("plan", *pair)
            finish_reserved_draft(store, value, card)
        before = store.load_planning("plan"), store.load_planning_history("plan")
        prefix = history_prefix(store)
        if missing == "both":
            prefix = {}
        else:
            prefix.pop(f"expected_history_{missing}")
        with pytest.raises(ValueError, match="history prefix"):
            store.reserve_planning_attempt(
                "plan",
                *v2_reservation(value, source, stage=stage, card=card),
                **prefix,
            )
        assert (
            store.load_planning("plan"),
            store.load_planning_history("plan"),
        ) == before


@pytest.mark.parametrize("supplied", ["length", "hash"])
def test_planning_v1_reservation_rejects_partial_history_prefix(tmp_path, supplied):
    with PlanningStore(tmp_path / "partial-prefix.db") as store:
        value, _, _, pair = create_reservable_plan(store)
        prefix = history_prefix(store)
        prefix = {
            f"expected_history_{supplied}": prefix[f"expected_history_{supplied}"]
        }
        with pytest.raises(ValueError, match="both history prefix"):
            store.reserve_planning_attempt("plan", *pair, **prefix)
        assert store.load_planning("plan") == PlanningState(value)
        assert store.load_planning_history("plan") == ()


@pytest.mark.parametrize("changed", ["applied", "history_only"])
def test_planning_reservation_rejects_changed_history_atomically(tmp_path, changed):
    path = tmp_path / "changed-history.db"
    with PlanningStore(path) as store:
        value, _, source, pair = create_reservable_plan(store)
        store.reserve_planning_attempt("plan", *pair)
        fail_reserved_attempt(store, "draft", 1)
        prefix = history_prefix(store)
        original = store.load_planning("plan")
        with PlanningStore(path) as concurrent:
            late = PlanningEnvelope(
                "draft",
                1,
                PlanningDiagnostic(Error("later host observation"))
                if changed == "applied"
                else Completed({"late": True}),
                "late-time",
            )
            disposition = concurrent.apply_planning("plan", late)
            assert disposition == Disposition(changed)
        before = store.load_planning("plan"), store.load_planning_history("plan")
        if changed == "history_only":
            assert before[0] == original
        with pytest.raises(ValueError, match="history changed"):
            store.reserve_planning_attempt(
                "plan", *v2_reservation(value, source, attempt=2), **prefix
            )
        assert (
            store.load_planning("plan"),
            store.load_planning_history("plan"),
        ) == before
        assert len(store.load_planning("plan").draft_attempts) == 1
        fresh = history_prefix(store)
        second = v2_reservation(value, source, attempt=2)
        assert store.reserve_planning_attempt("plan", *second, **fresh) is None
        assert store.load_planning_history("plan") == before[1] + tuple(
            (envelope, Disposition.APPLIED) for envelope in second
        )


@pytest.mark.parametrize("changed", ["length", "hash", "disposition", "time", "order"])
def test_planning_reservation_hash_covers_complete_ordered_history(tmp_path, changed):
    with PlanningStore(tmp_path / "complete-history.db") as store:
        value, _, source, pair = create_reservable_plan(store)
        store.reserve_planning_attempt("plan", *pair)
        fail_reserved_attempt(store, "draft", 1)
        late = PlanningEnvelope(
            "draft", 1, Completed({"late": "retained"}), "late-time"
        )
        assert store.apply_planning("plan", late) == Disposition.HISTORY_ONLY
        before = store.load_planning("plan"), store.load_planning_history("plan")
        prefix = history_prefix(store)
        altered = list(before[1])
        if changed == "length":
            prefix["expected_history_length"] += 1
        elif changed == "hash":
            prefix["expected_history_hash"] = "0" * 64
        elif changed == "disposition":
            altered[-1] = (altered[-1][0], Disposition.APPLIED)
        elif changed == "time":
            altered[-1] = (
                replace(altered[-1][0], occurred_at="other-time"),
                altered[-1][1],
            )
        else:
            altered[-2:] = reversed(altered[-2:])
        if changed in {"disposition", "time", "order"}:
            prefix["expected_history_hash"] = planning_hash(tuple(altered))
        with pytest.raises(ValueError, match="history changed"):
            store.reserve_planning_attempt(
                "plan", *v2_reservation(value, source, attempt=2), **prefix
            )
        assert (
            store.load_planning("plan"),
            store.load_planning_history("plan"),
        ) == before


def test_planning_v2_reservation_two_connections_share_one_prefix_winner(tmp_path):
    path = tmp_path / "prefix-race.db"
    with PlanningStore(path) as store:
        value, _, source, _ = create_reservable_plan(store)
        pair = v2_reservation(value, source)
        prefix = history_prefix(store)
    barrier = Barrier(2)

    def reserve(label):
        started, prepared = pair
        prepared = replace(
            prepared,
            event=replace(
                prepared.event,
                attempt_context=replace(prepared.event.attempt_context, token=label),
            ),
        )
        with PlanningStore(path) as store:
            barrier.wait(timeout=5)
            try:
                store.reserve_planning_attempt("plan", started, prepared, **prefix)
            except ValueError as exc:
                assert "history changed" in str(exc)
                return None
            return prepared

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(reserve, ("first-token", "second-token")))
    winners = [item for item in outcomes if item is not None]
    assert len(winners) == 1
    with PlanningStore(path) as store:
        assert len(store.load_planning("plan").draft_attempts) == 1
        assert store.load_planning_history("plan") == (
            (pair[0], Disposition.APPLIED),
            (winners[0], Disposition.APPLIED),
        )


@pytest.mark.parametrize("stage", ["draft", "review"])
def test_planning_environment_observation_persists_without_rewriting_started(
    tmp_path, stage
):
    path = tmp_path / "environment-observation.db"
    with PlanningStore(path) as store:
        value, card, source, pair = create_reservable_plan(store)
        store.reserve_planning_attempt("plan", *pair)
        if stage == "review":
            finish_reserved_draft(store, value, card)
            pair = v2_reservation(value, source, stage="review", card=card)
            store.reserve_planning_attempt("plan", *pair, **history_prefix(store))
        environment = {
            "cli_version": None,
            "logged_in": False,
            "nested": ["actual", None],
        }
        event = PlanningEnvironmentObserved(environment)
        envelope = PlanningEnvelope(stage, 1, event, "observed-at")
        environment["nested"].append("mutated caller")
        assert store.apply_planning("plan", envelope) == Disposition.APPLIED
        assert store.apply_planning("plan", envelope) == Disposition.HISTORY_ONLY
        before = store.load_planning("plan"), store.load_planning_history("plan")
        assert before[0].current(stage).request == pair[0].event
        assert before[0].current(stage).request.environment is None
        assert before[0].current(stage).runtime.reported_model is None
        with pytest.raises(ValueError):
            store.apply_planning(
                "plan",
                replace(
                    envelope, event=PlanningEnvironmentObserved({"logged_in": True})
                ),
            )
        assert (
            store.load_planning("plan"),
            store.load_planning_history("plan"),
        ) == before
    with PlanningStore(path) as store:
        assert (
            store.load_planning("plan"),
            store.load_planning_history("plan"),
        ) == before
        persisted = store.load_planning("plan").current(stage).events[-1]
        assert persisted.environment == {
            "cli_version": None,
            "logged_in": False,
            "nested": ("actual", None),
        }
        with pytest.raises(TypeError):
            persisted.environment["logged_in"] = True


@pytest.mark.parametrize("actual_exit", [None, -15])
def test_planning_evidence_rejection_preserves_actual_runtime_after_restart(
    tmp_path, actual_exit
):
    path = tmp_path / "evidence-rejection.db"
    with PlanningStore(path) as store:
        value, card, source, pair = create_reservable_plan(store)
        store.reserve_planning_attempt("plan", *pair)
        for envelope in sequence(value, card)[1:5]:
            store.apply_planning("plan", envelope)
        if actual_exit is not None:
            store.apply_planning(
                "plan", PlanningEnvelope("draft", 1, ProcessExited(actual_exit))
            )
        completed = store.load_planning("plan").current("draft").runtime
        rejected = PlanningEnvelope(
            "draft",
            1,
            PlanningEvidenceRejected("Log could not be sealed"),
            "rejected-at",
        )
        assert store.apply_planning("plan", rejected) == Disposition.APPLIED
        assert store.apply_planning("plan", rejected) == Disposition.HISTORY_ONLY
        before = store.load_planning("plan"), store.load_planning_history("plan")
        assert before[0].phase == PlanningPhase.FAILED
        assert before[0].validation_error == "Log could not be sealed"
        assert before[0].current("draft").runtime == completed
        assert before[0].current("draft").reconciliation is None
        assert completed.exit_code == actual_exit
        assert planning_value(completed.result) == card.to_dict()
        assert completed.reported_model == "actual-draft"
        assert completed.failure_category is None
        for invalid in (
            replace(rejected, event=PlanningEvidenceRejected("Different rejection")),
            PlanningEnvelope("draft", 1, PlanningDraftValidated(card)),
        ):
            with pytest.raises(ValueError):
                store.apply_planning("plan", invalid)
            assert (
                store.load_planning("plan"),
                store.load_planning_history("plan"),
            ) == before
        with pytest.raises(ValueError):
            store.reserve_planning_attempt(
                "plan",
                *v2_reservation(value, source, attempt=2),
                **history_prefix(store),
            )
        assert (
            store.load_planning("plan"),
            store.load_planning_history("plan"),
        ) == before
    with PlanningStore(path) as store:
        assert (
            store.load_planning("plan"),
            store.load_planning_history("plan"),
        ) == before
        store.apply_planning(
            "plan",
            PlanningEnvelope(
                "draft",
                1,
                PlanningReconciled(True, "All owned processes actually stopped"),
            ),
        )
        assert store.load_planning("plan").phase == PlanningPhase.FAILED
        assert store.load_planning("plan").current("draft").runtime == completed
        store.reserve_planning_attempt(
            "plan", *v2_reservation(value, source, attempt=2), **history_prefix(store)
        )
        fresh = store.load_planning("plan")
        assert store.apply_planning("plan", rejected) == Disposition.HISTORY_ONLY
        assert store.load_planning("plan") == fresh
        assert store.load_planning_history("plan")[-1] == (
            rejected,
            Disposition.HISTORY_ONLY,
        )


@pytest.mark.parametrize("expected", ["matching", "explicit-none", "omitted"])
def test_planning_binding_expected_hash_and_legacy_api_preserve_provenance(
    tmp_path, expected
):
    path = tmp_path / "expected-plan.db"
    with PlanningStore(path) as store:
        value, card, envelopes = ready(store)
        provenance = store.load_planning("plan").provenance()
        keywords = (
            {}
            if expected == "omitted"
            else {
                "expected_plan_hash": provenance.plan_hash
                if expected == "matching"
                else None
            }
        )
        assert store.create_planned_task("task", "plan", "bound-at", **keywords) is None
        bound = store.load_planning("plan")
        assert bound.phase == PlanningPhase.BOUND
        assert bound.bound_task_run_id == "task"
        assert bound.provenance() == provenance
        assert store.load_task_input("task") == TaskInput(
            card, value.frozen_rules, value.requirements, "bound-at"
        )
        assert store.load_planning_history("plan") == tuple(
            (envelope, Disposition.APPLIED) for envelope in envelopes
        ) + (
            (
                PlanningEnvelope("review", 1, PlanningTaskBound("task"), "bound-at"),
                Disposition.APPLIED,
            ),
        )
    with PlanningStore(path) as store:
        assert store.load_planning("plan") == bound
        assert store.load_task_provenance("task") == provenance
        assert store.load_task_provenance("task").plan_hash == provenance.plan_hash
        assert store.load_task("task").phase == TaskPhase.READY
        assert store.load_task_history("task") == ()


@pytest.mark.parametrize("mismatch", ["different", "empty", "case"])
def test_planning_binding_expected_hash_mismatch_performs_no_writes(tmp_path, mismatch):
    with PlanningStore(tmp_path / "mismatch-plan.db") as store:
        ready(store)
        before = store.load_planning("plan"), store.load_planning_history("plan")
        original_hash = before[0].provenance().plan_hash
        expected_hash = {
            "different": "0" * 64,
            "empty": "",
            "case": original_hash.upper(),
        }[mismatch]
        assert expected_hash != original_hash
        database_before = tuple(store._connection.iterdump())
        statements = []
        store._connection.set_trace_callback(statements.append)
        try:
            with pytest.raises(ValueError, match="provenance changed"):
                store.create_planned_task(
                    "task", "plan", "bound-at", expected_plan_hash=expected_hash
                )
        finally:
            store._connection.set_trace_callback(None)
        assert statements[0] == "BEGIN IMMEDIATE"
        assert statements[-1] == "ROLLBACK"
        assert not any(
            statement.lstrip()
            .upper()
            .startswith(("INSERT", "UPDATE", "DELETE", "REPLACE"))
            for statement in statements
        )
        assert tuple(store._connection.iterdump()) == database_before
        assert (
            store.load_planning("plan"),
            store.load_planning_history("plan"),
        ) == before
        with pytest.raises(KeyError):
            store.load_task("task")
        with pytest.raises(KeyError):
            store.load_task_provenance("task")
        assert store.list_recoverable_tasks() == ()
        # A rejected compare does not consume the valid plan or task identifier.
        store.create_planned_task(
            "task", "plan", "bound-at", expected_plan_hash=original_hash
        )
        assert store.load_task_provenance("task").plan_hash == original_hash


@pytest.mark.parametrize(
    "changed", ["draft-manifest", "review-manifest", "diagnostic", "selected-attempts"]
)
def test_planning_binding_rejects_changed_provenance_atomically(tmp_path, changed):
    path = tmp_path / "verified-then-changed.db"
    with PlanningStore(path) as verifier:
        value, card, _ = ready(verifier)
        inspected_hash = verifier.load_planning("plan").provenance().plan_hash
        # The verifier retains its earlier hash while a separate connection writes
        # new evidence. All variants remain READY, so the CAS must reject them.
        with PlanningStore(path) as concurrent:
            if changed.endswith("-manifest"):
                stage = changed.split("-", 1)[0]
                event = PlanningReconciled(
                    True,
                    "New qualifying synthetic manifest after inspection",
                    f"/external/{stage}-replacement/execution.json",
                    "d" * 64,
                )
                assert (
                    concurrent.apply_planning(
                        "plan",
                        PlanningEnvelope(stage, 1, event, "changed-after-inspection"),
                    )
                    == Disposition.APPLIED
                )
            elif changed == "diagnostic":
                assert (
                    concurrent.apply_planning(
                        "plan",
                        PlanningEnvelope(
                            "review",
                            1,
                            PlanningDiagnostic(Error("New host diagnostic")),
                            "changed-after-inspection",
                        ),
                    )
                    == Disposition.APPLIED
                )
            else:
                for envelope in sequence(value, card):
                    event = envelope.event
                    if envelope.stage == "review" and isinstance(
                        event, PlanningAttemptStarted
                    ):
                        event = replace(event, draft_attempt=2)
                    assert (
                        concurrent.apply_planning(
                            "plan", replace(envelope, attempt=2, event=event)
                        )
                        == Disposition.APPLIED
                    )
        before = verifier.load_planning("plan"), verifier.load_planning_history("plan")
        assert before[0].can_bind
        current_hash = before[0].provenance().plan_hash
        assert current_hash != inspected_hash
        database_before = tuple(verifier._connection.iterdump())
        statements = []
        verifier._connection.set_trace_callback(statements.append)
        try:
            with pytest.raises(ValueError, match="provenance changed"):
                verifier.create_planned_task(
                    "task", "plan", "bound-at", expected_plan_hash=inspected_hash
                )
        finally:
            verifier._connection.set_trace_callback(None)
        assert statements[0] == "BEGIN IMMEDIATE"
        assert statements[-1] == "ROLLBACK"
        assert not any(
            statement.lstrip()
            .upper()
            .startswith(("INSERT", "UPDATE", "DELETE", "REPLACE"))
            for statement in statements
        )
        assert tuple(verifier._connection.iterdump()) == database_before
        assert (
            verifier.load_planning("plan"),
            verifier.load_planning_history("plan"),
        ) == before
        assert verifier.list_recoverable_tasks() == ()
        with pytest.raises(KeyError):
            verifier.load_task_provenance("task")
    with PlanningStore(path) as store:
        assert (
            store.load_planning("plan"),
            store.load_planning_history("plan"),
        ) == before
        store.create_planned_task(
            "task", "plan", "bound-at", expected_plan_hash=current_hash
        )
        assert store.load_task_provenance("task") == before[0].provenance()


def test_planning_binding_history_only_events_do_not_invalidate_inspected_provenance(
    tmp_path,
):
    path = tmp_path / "unchanged-provenance.db"
    with PlanningStore(path) as store:
        ready(store)
        inspected = store.load_planning("plan").provenance()
        history = store.load_planning_history("plan")
        late = PlanningEnvelope(
            "draft", 1, Completed({"late": "ignored artifact"}), "late-at"
        )
        with PlanningStore(path) as concurrent:
            assert concurrent.apply_planning("plan", late) == Disposition.HISTORY_ONLY
        assert store.load_planning("plan").provenance() == inspected
        assert store.load_planning_history("plan") == history + (
            (late, Disposition.HISTORY_ONLY),
        )
        store.create_planned_task(
            "task", "plan", "bound-at", expected_plan_hash=inspected.plan_hash
        )
        assert store.load_task_provenance("task") == inspected
        assert store.load_planning_history("plan")[-2] == (
            late,
            Disposition.HISTORY_ONLY,
        )
