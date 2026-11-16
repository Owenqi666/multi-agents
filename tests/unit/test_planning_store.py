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
    planning_value,
)
from multi_agent.core.planning_events import (
    PlanningAttemptStarted,
    PlanningDiagnostic,
    PlanningDraftValidated,
    PlanningEnvelope,
    PlanningReconciled,
    PlanningReviewValidated,
    PlanningTaskBound,
    PlanningValidationRejected,
)
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
        assert store._connection.execute("PRAGMA user_version").fetchone()[0] == 4
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
