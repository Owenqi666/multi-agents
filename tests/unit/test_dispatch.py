"""One implementation actor must satisfy every future path and frozen restriction."""

import json
from dataclasses import FrozenInstanceError, asdict, fields

import pytest

from multi_agent.cli.runner import new_review
from multi_agent.cli.task_inputs import parse_project_rules, parse_task_card
from multi_agent.core import SeatConfig
from multi_agent.core import dispatch as dispatch_module
from multi_agent.core.dispatch import (
    ImplementationChoice,
    NoImplementationAvailable,
    ProtectedScopeError,
    RuleConflict,
    resolve_dispatch,
)
from multi_agent.core.path_patterns import PatternBudgetExceeded
from multi_agent.core.snapshot import FrozenSnapshot
from multi_agent.core.tasks import FrozenProjectRules, TaskCard
from multi_agent.store import ReviewStore

BASELINE = "a" * 40
CLAUDE = "claude-code"


def task(*paths: str) -> TaskCard:
    return parse_task_card(
        json.dumps(
            {
                "schema_version": 1,
                "id": "T-0100",
                "goal": "Implement the requested change.",
                "baseline_commit": BASELINE,
                "write_scope": list(paths),
                "depends_on": [],
                "interface_contracts": [],
                "deliverables": ["Updated behavior in the declared paths."],
                "acceptance_criteria": ["Configured tests pass."],
                "test_refs": ["project-unit"],
                "open_questions": [],
            }
        )
    )


def config(
    provider: str = CLAUDE,
    model: str = "model-a",
    models: tuple[str, ...] = ("model-a", "model-b", "model-c"),
    effort: str = "medium",
) -> dict:
    return {
        "provider": provider,
        "default_model": model,
        "default_effort": effort,
        "allowed_models": list(models),
    }


def rule(paths: tuple[str, ...] = ("frontend/**",), **changes) -> dict:
    return {"paths": list(paths), **config(**changes)}


def frozen(
    *rules: dict,
    default: dict | None = None,
    protected: tuple[str, ...] = (),
    task_types: dict | None = None,
) -> FrozenProjectRules:
    value = {
        "version": 1,
        "commands": {"project-unit": {"executable": "pytest", "args": []}},
        "protected_paths": list(protected),
        "implementation": list(rules),
    }
    if default is not None:
        value["default_implementation"] = default
    if task_types is not None:
        value["task_types"] = task_types
    text = json.dumps(value, ensure_ascii=False)
    return FrozenProjectRules(
        "/repo", BASELINE, "b" * 40, text, parse_project_rules(text)
    )


@pytest.mark.parametrize(
    "scope", ["frontend/new/not-created.tsx", "front*/**", "**/*.tsx"]
)
def test_future_frontend_paths_require_claude(scope: str, tmp_path) -> None:
    assert not (tmp_path / "frontend").exists()
    rules = frozen(rule(), default=config())
    decision = resolve_dispatch(task(scope), rules)
    assert decision.choice.provider == CLAUDE
    assert decision.matched_rule_indices == (0,)
    with pytest.raises(RuleConflict):
        resolve_dispatch(
            task(scope),
            rules,
            requested=ImplementationChoice("codex", "model-a", "medium"),
        )
    assert not (tmp_path / "frontend").exists()


@pytest.mark.parametrize(
    "card,rules",
    [
        (
            task("frontend/**", "backend/**"),
            frozen(rule(), rule(("backend/**",), provider="codex")),
        ),
        (
            task("frontend/new.ts"),
            frozen(rule(), rule(("**/*.ts",), provider="codex")),
        ),
        (
            task("frontend/new.ts"),
            frozen(
                rule(models=("model-a",)),
                rule(("**/*.ts",), model="model-b", models=("model-b",)),
            ),
        ),
        (task("other/new.py"), frozen(rule())),
    ],
)
def test_conflicting_rules_require_split(
    card: TaskCard, rules: FrozenProjectRules
) -> None:
    with pytest.raises(RuleConflict):
        resolve_dispatch(card, rules)


def test_compatible_model_limits_intersect() -> None:
    rules = frozen(
        rule(models=("model-a", "shared-second", "shared-first")),
        rule(
            ("**/*.ts",),
            model="model-b",
            models=("model-b", "shared-first", "shared-second"),
            effort="high",
        ),
    )
    decision = resolve_dispatch(task("frontend/new.ts"), rules)
    assert decision.choice == ImplementationChoice(CLAUDE, "shared-second", "medium")
    assert decision.reason == "first_common_configured_model"
    assert decision.allowed_models == ("shared-first", "shared-second")
    assert decision.matched_rule_indices == (0, 1)


def test_model_limits_intersect_across_separate_write_paths() -> None:
    rules = frozen(
        rule(models=("model-a", "model-b")),
        rule(("backend/**",), model="model-c", models=("model-b", "model-c")),
    )
    decision = resolve_dispatch(task("frontend/a.ts", "backend/b.py"), rules)
    assert decision.choice.model == "model-b"
    assert decision.allowed_models == ("model-b",)
    assert decision.matched_rule_indices == (0, 1)


def test_first_compatible_default_supplies_its_own_effort() -> None:
    rules = frozen(
        rule(models=("model-a", "model-b"), effort="medium"),
        rule(("**/*.ts",), model="model-b", models=("model-b",), effort="high"),
    )
    decision = resolve_dispatch(task("frontend/a.ts"), rules)
    assert decision.choice == ImplementationChoice(CLAUDE, "model-b", "high")
    assert decision.reason == "first_compatible_default"


def test_default_applies_only_to_uncovered_paths() -> None:
    rules = frozen(rule(), default=config(provider="codex"))
    frontend = resolve_dispatch(task("frontend/a.ts"), rules)
    assert frontend.choice.provider == CLAUDE
    assert not frontend.used_default
    assert frontend.matched_rule_indices == (0,)
    other = resolve_dispatch(task("other/a.py"), rules)
    assert other.choice.provider == "codex"
    assert other.used_default
    assert other.matched_rule_indices == ()
    with pytest.raises(RuleConflict):
        resolve_dispatch(task("frontend/a.ts", "other/a.py"), rules)


def test_uncovered_paths_add_default_model_limits_to_the_intersection() -> None:
    rules = frozen(
        rule(models=("model-a", "model-b")),
        default=config(model="model-b", models=("model-b", "model-c"), effort="high"),
    )
    decision = resolve_dispatch(task("frontend/a.ts", "other/a.py"), rules)
    assert decision.used_default
    assert decision.choice == ImplementationChoice(CLAUDE, "model-b", "high")
    assert decision.allowed_models == ("model-b",)


def test_union_of_rules_covers_scope() -> None:
    rules = frozen(
        rule(("frontend", "frontend/*")),
        rule(("frontend/*/*/**",)),
        default=config(provider="codex"),
    )
    decision = resolve_dispatch(task("frontend/**"), rules)
    assert decision.choice.provider == CLAUDE
    assert decision.matched_rule_indices == (0, 1)
    assert not decision.used_default


def test_task_type_limits_apply() -> None:
    rules = frozen(
        rule(),
        task_types={
            "narrow": {"provider": CLAUDE, "allowed_models": ["model-b"]},
            "other-provider": {"provider": "codex", "allowed_models": ["model-a"]},
            "empty-intersection": {"provider": CLAUDE, "allowed_models": ["model-z"]},
        },
    )
    decision = resolve_dispatch(task("frontend/a.ts"), rules, task_type="narrow")
    assert decision.choice.model == "model-b"
    assert decision.allowed_models == ("model-b",)
    assert decision.task_type == "narrow"
    assert resolve_dispatch(task("frontend/a.ts"), rules).choice.model == "model-a"
    for name in ("other-provider", "empty-intersection", "missing"):
        with pytest.raises(RuleConflict):
            resolve_dispatch(task("frontend/a.ts"), rules, task_type=name)


def test_override_and_fallback_obey_rules() -> None:
    rules = frozen(rule())
    explicit = ImplementationChoice(CLAUDE, "model-c", "not-yet-runtime-confirmed")
    decision = resolve_dispatch(task("frontend/a.ts"), rules, requested=explicit)
    assert decision.choice == explicit
    assert decision.requested == explicit
    assert decision.reason == "explicit_request"
    primary = ImplementationChoice(CLAUDE, "model-a", "medium")
    candidates = (
        ImplementationChoice("codex", "model-c", "medium"),
        ImplementationChoice(CLAUDE, "forbidden", "medium"),
        ImplementationChoice(CLAUDE, "model-b", "high"),
        ImplementationChoice(CLAUDE, "model-c", "high"),
    )
    unavailable = frozenset({(CLAUDE, "model-a"), (CLAUDE, "model-b")})
    decision = resolve_dispatch(
        task("frontend/a.ts"), rules, fallbacks=candidates, unavailable=unavailable
    )
    assert decision.choice == candidates[-1]
    assert decision.reason == "enabled_ordered_fallback"
    assert tuple(item.choice for item in decision.selection_history) == (
        primary,
        *candidates,
    )
    assert tuple(item.outcome for item in decision.selection_history) == (
        "known_unavailable",
        "disallowed_by_rules",
        "disallowed_by_rules",
        "known_unavailable",
        "selected_request",
    )
    assert decision.unavailable_models == tuple(sorted(unavailable))


@pytest.mark.parametrize(
    "requested",
    [
        ImplementationChoice("codex", "model-a", "medium"),
        ImplementationChoice(CLAUDE, "forbidden", "medium"),
    ],
)
def test_disallowed_override_is_not_silently_replaced(
    requested: ImplementationChoice,
) -> None:
    with pytest.raises(RuleConflict):
        resolve_dispatch(
            task("frontend/a.ts"),
            frozen(rule()),
            requested=requested,
            fallbacks=(ImplementationChoice(CLAUDE, "model-a", "medium"),),
            unavailable=frozenset({(requested.provider, requested.model)}),
        )


def test_fallback_uses_user_order_and_preserves_the_original_request() -> None:
    requested = ImplementationChoice(CLAUDE, "model-c", "high")
    first = ImplementationChoice(CLAUDE, "model-b", "medium")
    second = ImplementationChoice(CLAUDE, "model-a", "medium")
    decision = resolve_dispatch(
        task("frontend/a.ts"),
        frozen(rule()),
        requested=requested,
        fallbacks=(first, second),
        unavailable=frozenset({(CLAUDE, "model-c")}),
    )
    assert decision.choice == first
    assert decision.requested == requested
    assert tuple(record.choice for record in decision.selection_history) == (
        requested,
        first,
    )


def test_unknown_availability_does_not_trigger_fallback() -> None:
    decision = resolve_dispatch(
        task("frontend/a.ts"),
        frozen(rule()),
        fallbacks=(ImplementationChoice(CLAUDE, "model-b", "high"),),
        unavailable=frozenset({("codex", "model-a")}),
    )
    assert decision.choice == ImplementationChoice(CLAUDE, "model-a", "medium")
    assert len(decision.selection_history) == 1
    assert decision.selection_history[0].outcome == "selected_request"
    assert decision.reason == "first_compatible_default"


def test_unavailable_primary_does_not_implicitly_try_other_allowed_models() -> None:
    with pytest.raises(NoImplementationAvailable) as failure:
        resolve_dispatch(
            task("frontend/a.ts"),
            frozen(rule()),
            unavailable=frozenset({(CLAUDE, "model-a")}),
        )
    history = failure.value.selection_history
    assert len(history) == 1
    assert history[0].choice.model == "model-a"
    assert history[0].outcome == "known_unavailable"


def test_exhausted_fallbacks_preserve_every_rejection() -> None:
    first = ImplementationChoice("codex", "model-a", "medium")
    second = ImplementationChoice(CLAUDE, "model-b", "high")
    with pytest.raises(NoImplementationAvailable) as failure:
        resolve_dispatch(
            task("frontend/a.ts"),
            frozen(rule()),
            fallbacks=(first, second),
            unavailable=frozenset({(CLAUDE, "model-a"), (CLAUDE, "model-b")}),
        )
    history = failure.value.selection_history
    assert isinstance(history, tuple)
    assert tuple(item.choice.model for item in history) == (
        "model-a",
        "model-a",
        "model-b",
    )
    assert tuple(item.outcome for item in history) == (
        "known_unavailable",
        "disallowed_by_rules",
        "known_unavailable",
    )


@pytest.mark.parametrize("scope", [".multi-agent/rules.yaml", "**", "**/*.yaml"])
def test_protected_scope_is_rejected(scope: str) -> None:
    rules = frozen(rule(), default=config(), protected=(".multi-agent/**",))
    with pytest.raises(ProtectedScopeError):
        resolve_dispatch(task(scope), rules)


@pytest.mark.parametrize(
    "scope,protected",
    [
        ("PRIVATE/STRASSE/key", "private/straße/**"),
        ("ASSETS/FILE", "assets/ﬁle"),
        ("cafe\u0301/key", "café/**"),
    ],
)
def test_case_and_unicode_aliases_are_checked(scope: str, protected: str) -> None:
    rules = frozen(default=config(), protected=(protected,))
    with pytest.raises(ProtectedScopeError):
        resolve_dispatch(task(scope), rules)


def test_casefolded_implementation_paths_still_require_claude() -> None:
    rules = frozen(rule(), default=config(provider="codex"))
    decision = resolve_dispatch(task("FRONTEND/New.ts"), rules)
    assert decision.choice.provider == CLAUDE
    assert not decision.used_default


@pytest.mark.parametrize("path", ["lib/ß.py", "lib/ﬁ.py"])
def test_question_mark_counts_normalized_codepoints(path: str) -> None:
    rules = frozen(rule(("lib/?.py",)), default=config(provider="codex"))
    assert resolve_dispatch(task(path), rules).choice.provider == "codex"
    assert resolve_dispatch(task("lib/e\u0301.py"), rules).choice.provider == CLAUDE


@pytest.mark.parametrize("operation", ["patterns_intersect", "patterns_cover"])
def test_symbolic_budget_failure_never_allows_dispatch(
    monkeypatch, operation: str
) -> None:
    original = getattr(dispatch_module, operation)

    def exhausted(*args):
        return original(*args, state_budget=0)

    monkeypatch.setattr(dispatch_module, operation, exhausted)
    with pytest.raises(PatternBudgetExceeded):
        resolve_dispatch(
            task("frontend/a.ts"), frozen(rule(), protected=("secrets/**",))
        )


def test_decision_is_immutable_and_contains_only_selection_evidence() -> None:
    card = task("frontend/a.ts")
    rules = frozen(rule())
    decision = resolve_dispatch(card, rules)
    assert decision.task_hash == card.task_hash
    assert decision.rules_hash == rules.rules_hash
    for value, field, replacement in (
        (decision, "used_default", True),
        (decision.choice, "model", "changed"),
        (decision.selection_history[0], "outcome", "changed"),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(value, field, replacement)
    for value in (decision, decision.choice, *decision.selection_history):
        assert not any(
            field.name.startswith(("reported_", "actual_", "effective_"))
            for field in fields(value)
        )
    exported = asdict(decision)
    exported["choice"]["model"] = "changed"
    assert decision.choice.model == "model-a"


def test_frontend_dispatch_does_not_restrict_review(tmp_path) -> None:
    decision = resolve_dispatch(task("frontend/a.ts"), frozen(rule()))
    assert decision.choice.provider == CLAUDE
    snapshot = FrozenSnapshot(
        None,
        BASELINE,
        "c" * 40,
        "frontend change",
        "Review the frontend behavior.",
        ("Configured behavior is correct.",),
        (),
        (),
    )
    gpt = SeatConfig("gpt", "codex", "gpt-review-model", "high")
    with ReviewStore(tmp_path / "review.sqlite") as store:
        store.save_snapshot(snapshot.snapshot_id, snapshot.to_dict())
        identifier = new_review(
            store,
            snapshot.snapshot_id,
            (gpt,),
            timeout=60,
            grace=2,
            executables={"codex": "unused-fake-codex"},
        )
        stored = store.load_round(identifier)
        assert stored.seats[0].config == gpt
        assert stored.seats[0].attempts == ()
        assert stored.snapshot_id == snapshot.snapshot_id
