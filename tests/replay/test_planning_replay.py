"""Replay identical planning histories in memory and SQLite, twice per backend."""

import json
from pathlib import Path

import pytest

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
    PlanningInput,
    PlanningRequest,
    PlanningReview,
    PlanningState,
)
from multi_agent.core.planning_events import (
    PlanningAttemptStarted,
    PlanningDiagnostic,
    PlanningDraftValidated,
    PlanningEnvelope,
    PlanningReconciled,
    PlanningReviewValidated,
    PlanningValidationRejected,
)
from multi_agent.core.state import Disposition
from multi_agent.core.tasks import (
    FrozenProjectRules,
    ImplementationConfig,
    NamedCommand,
    ProjectRules,
    TaskCard,
)
from multi_agent.core.transitions import apply_planning
from multi_agent.store.planning_store import PlanningStore

SCENARIOS = Path(__file__).parent / "scenarios" / "planning"
REQUIRED_SCENARIOS = {
    "planning-happy",
    "planning-revision-late",
    "planning-missing-exit",
    "planning-cancel-diagnostic",
    "planning-reconcile-revoked",
}


def _card(variant=0):
    return TaskCard(
        1,
        "T-9000",
        "Original scope" if variant == 0 else "Revised concrete scope",
        "a" * 40,
        ("src/**",),
        (),
        (),
        ("A concrete implementation",),
        ("Public interface is preserved",),
        ("unit",),
        (),
    )


def _inputs():
    rules = FrozenProjectRules(
        "/synthetic/repository/.git",
        "a" * 40,
        "b" * 40,
        "Synthetic frozen rule bytes\n",
        ProjectRules(
            1,
            (NamedCommand("unit", "synthetic-test", ()),),
            (".multi-agent/**",),
            (),
            ImplementationConfig("codex", "requested-coder", "medium", ()),
        ),
    )
    return PlanningInput(
        "Preserve the public interface while implementing the requested behavior.",
        rules,
        PlanningRequest("claude", "requested-drafter", "high"),
        PlanningRequest("codex", "requested-reviewer", "high"),
    )


def _review(values):
    fields = dict(values)
    variant = fields.pop("card_variant", 0)
    fields["blocking_issues"] = tuple(fields["blocking_issues"])
    return PlanningReview(_card(variant).task_hash, **fields)


def _event(stage, fields, inputs):
    values = dict(fields)
    kind = values.pop("type")
    if kind == PlanningAttemptStarted.kind:
        variant = values.pop("card_variant", 0)
        request = inputs.draft_request if stage == "draft" else inputs.review_request
        return PlanningAttemptStarted(
            request,
            task_hash=_card(variant).task_hash if stage == "review" else None,
            **values,
        )
    if kind == PlanningDraftValidated.kind:
        return PlanningDraftValidated(_card(values["card_variant"]))
    if kind == PlanningReviewValidated.kind:
        return PlanningReviewValidated(_review(values))
    if kind == Completed.kind:
        if "card_variant" in values:
            values["result"] = _card(values.pop("card_variant")).to_dict()
        elif "review" in values:
            review = _review(values.pop("review"))
            values["result"] = {
                "task_hash": review.task_hash,
                "decision": review.decision,
                "reason": review.reason,
                "blocking_issues": list(review.blocking_issues),
            }
        return Completed(**values)
    if kind == Error.kind:
        if values.get("failure_category") is not None:
            values["failure_category"] = FailureCategory(values["failure_category"])
        return Error(**values)
    if kind == PlanningDiagnostic.kind:
        return PlanningDiagnostic(_event(stage, values["diagnostic"], inputs))
    constructors = {
        item.kind: item
        for item in (
            CancelRequested,
            Message,
            ProcessExited,
            SessionStarted,
            Usage,
            PlanningReconciled,
            PlanningValidationRejected,
        )
    }
    return constructors[kind](**values)


class CoreBackend:
    def __init__(self, inputs):
        self.state = PlanningState(inputs)
        self.history = []

    def apply(self, envelope):
        self.state, disposition = apply_planning(self.state, envelope)
        self.history.append((envelope, disposition))
        return disposition

    def load(self):
        return self.state

    def load_history(self):
        return tuple(self.history)

    def reopen(self):
        pass

    def close(self):
        pass


class StoreBackend:
    def __init__(self, path, inputs):
        self.path = path
        self.inputs = inputs
        self.store = PlanningStore(path)
        try:
            self.store.create_planning("replay", inputs, "2026-11-16T00:00:00Z")
        except BaseException:
            self.store.close()
            raise

    def apply(self, envelope):
        return self.store.apply_planning("replay", envelope)

    def load(self):
        return self.store.load_planning("replay")

    def load_history(self):
        return self.store.load_planning_history("replay")

    def reopen(self):
        self.store.close()
        self.store = PlanningStore(self.path)
        assert self.store.load_planning_input("replay") == self.inputs

    def close(self):
        self.store.close()


def _observation(state):
    draft = state.current("draft")
    review = state.current("review")
    return {
        "phase": state.phase,
        "stage": state.stage,
        "draft_attempt": draft.number if draft else None,
        "review_attempt": review.number if review else None,
        "can_bind": state.can_bind,
    }


def _final(state):
    attempts = {}
    for stage in ("draft", "review"):
        attempts[stage] = [
            {
                "number": attempt.number,
                "status": attempt.runtime.status,
                "failure_category": attempt.runtime.failure_category,
                "reported_model": attempt.runtime.reported_model,
                "reported_effort": attempt.runtime.reported_effort,
                "exit_code": attempt.runtime.exit_code,
                "process_stopped": attempt.stopped,
                "usable": attempt.usable,
            }
            for attempt in state.attempts(stage)
        ]
    return {
        **_observation(state),
        "card_goal": state.card.goal if state.card else None,
        "review_decision": state.review_artifact.decision
        if state.review_artifact
        else None,
        "attempts": attempts,
    }


def _replay(backend, scenario, inputs):
    history = []
    trace = []
    for step in scenario["steps"]:
        before = backend.load(), backend.load_history()
        if step["op"] == "reopen":
            backend.reopen()
            assert (backend.load(), backend.load_history()) == before
        else:
            assert step["op"] == "event"
            fields = step["envelope"]
            envelope = PlanningEnvelope(
                fields["stage"],
                fields["attempt"],
                _event(fields["stage"], fields["event"], inputs),
                fields["occurred_at"],
            )
            if step.get("error") == "ValueError":
                with pytest.raises(ValueError):
                    backend.apply(envelope)
                assert (backend.load(), backend.load_history()) == before
            else:
                disposition = Disposition(step["disposition"])
                assert backend.apply(envelope) == disposition
                history.append((envelope, disposition))
                if disposition == Disposition.HISTORY_ONLY:
                    assert backend.load() == before[0]
        assert _observation(backend.load()) == step["expect"]
        assert backend.load_history() == tuple(history)
        trace.append((backend.load(), backend.load_history()))
    assert _final(backend.load()) == scenario["final"]
    backend.reopen()
    assert _final(backend.load()) == scenario["final"]
    assert backend.load_history() == tuple(history)
    return tuple(trace)


@pytest.mark.parametrize(
    "name",
    sorted(REQUIRED_SCENARIOS | {path.stem for path in SCENARIOS.glob("*.json")}),
)
def test_planning_scenario(name, tmp_path):
    scenario = json.loads((SCENARIOS / f"{name}.json").read_text())
    inputs = _inputs()
    expected = _replay(CoreBackend(inputs), scenario, inputs)
    assert _replay(CoreBackend(inputs), scenario, inputs) == expected
    for index in range(2):
        backend = StoreBackend(tmp_path / f"{name}-{index}.sqlite", inputs)
        try:
            assert _replay(backend, scenario, inputs) == expected
        finally:
            backend.close()
