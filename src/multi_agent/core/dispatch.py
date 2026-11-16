"""Resolve one implementation actor from immutable, boundary-validated inputs.

Decisions describe requests and their policy justification, never runtime model
or effort evidence. Review seats do not pass through this implementation policy.
"""

from dataclasses import dataclass

from multi_agent.core.path_patterns import patterns_cover, patterns_intersect
from multi_agent.core.tasks import FrozenProjectRules, ImplementationConfig, TaskCard


@dataclass(frozen=True, slots=True)
class ImplementationChoice:
    provider: str
    model: str
    effort: str


@dataclass(frozen=True, slots=True)
class SelectionRecord:
    choice: ImplementationChoice
    outcome: str


@dataclass(frozen=True, slots=True)
class DispatchDecision:
    choice: ImplementationChoice
    reason: str
    selection_history: tuple[SelectionRecord, ...]
    matched_rule_indices: tuple[int, ...]
    used_default: bool
    task_hash: str
    rules_hash: str
    task_type: str | None
    requested: ImplementationChoice | None
    unavailable_models: tuple[tuple[str, str], ...]
    allowed_models: tuple[str, ...]


class RuleConflict(ValueError):
    """The task requires a split, a narrower scope, or a compatible selection."""


class ProtectedScopeError(ValueError):
    """The declared write language overlaps a protected path language."""


class NoImplementationAvailable(ValueError):
    def __init__(self, selection_history: tuple[SelectionRecord, ...]) -> None:
        super().__init__(
            "primary and enabled fallback choices are unavailable or disallowed"
        )
        self.selection_history = selection_history


def resolve_dispatch(
    card: TaskCard,
    frozen: FrozenProjectRules,
    *,
    task_type: str | None = None,
    requested: ImplementationChoice | None = None,
    fallbacks: tuple[ImplementationChoice, ...] = (),
    unavailable: frozenset[tuple[str, str]] = frozenset(),
) -> DispatchDecision:
    """Intersect all relevant rules and select one actor; never invoke a provider.

    `unavailable` contains only caller-supplied known provider/model failures.
    Absence from that set is not evidence of availability; an actual runtime
    preflight is still required by the execution boundary.
    """
    rules = frozen.rules
    for scope in card.write_scope:
        for protected in rules.protected_paths:
            if patterns_intersect(scope, protected):
                raise ProtectedScopeError(
                    f"write scope {scope!r} overlaps {protected!r}"
                )
    matched = tuple(
        index
        for index, rule in enumerate(rules.implementation)
        if any(
            patterns_intersect(scope, pattern)
            for scope in card.write_scope
            for pattern in rule.paths
        )
    )
    all_patterns = tuple(
        pattern for rule in rules.implementation for pattern in rule.paths
    )
    used_default = any(
        not patterns_cover(scope, all_patterns) for scope in card.write_scope
    )
    configs: list[ImplementationConfig] = [
        rules.implementation[index].config for index in matched
    ]
    if used_default:
        if rules.default_implementation is None:
            raise RuleConflict("uncovered write scope requires default_implementation")
        configs.append(rules.default_implementation)
    providers = {config.provider for config in configs}
    limits = [set(config.allowed_models) for config in configs]
    if task_type is not None:
        restrictions = {
            restriction.name: restriction for restriction in rules.task_types
        }
        if task_type not in restrictions:
            raise RuleConflict(f"unknown task type: {task_type}")
        restriction = restrictions[task_type]
        providers.add(restriction.provider)
        limits.append(set(restriction.allowed_models))
    if len(providers) != 1 or not limits:
        raise RuleConflict(
            "write scope and task type require incompatible providers; split task"
        )
    allowed = set.intersection(*limits)
    if not allowed:
        raise RuleConflict(
            "implementation model restrictions have an empty intersection"
        )
    provider = next(iter(providers))

    def permitted(choice: ImplementationChoice) -> bool:
        return choice.provider == provider and choice.model in allowed

    if requested is not None:
        if not permitted(requested):
            raise RuleConflict("requested implementation is disallowed by frozen rules")
        primary = requested
        primary_reason = "explicit_request"
    else:
        defaults = [config for config in configs if config.default_model in allowed]
        if defaults:
            selected = defaults[0]
            model = selected.default_model
            primary_reason = "first_compatible_default"
        else:
            selected = configs[0]
            model = next(model for model in selected.allowed_models if model in allowed)
            primary_reason = "first_common_configured_model"
        primary = ImplementationChoice(provider, model, selected.default_effort)
    history: list[SelectionRecord] = []
    for index, choice in enumerate((primary, *fallbacks)):
        if not permitted(choice):
            history.append(SelectionRecord(choice, "disallowed_by_rules"))
            continue
        if (choice.provider, choice.model) in unavailable:
            history.append(SelectionRecord(choice, "known_unavailable"))
            continue
        history.append(SelectionRecord(choice, "selected_request"))
        return DispatchDecision(
            choice=choice,
            reason=primary_reason if index == 0 else "enabled_ordered_fallback",
            selection_history=tuple(history),
            matched_rule_indices=matched,
            used_default=used_default,
            task_hash=card.task_hash,
            rules_hash=frozen.rules_hash,
            task_type=task_type,
            requested=requested,
            unavailable_models=tuple(sorted(unavailable)),
            allowed_models=tuple(sorted(allowed)),
        )
    raise NoImplementationAvailable(tuple(history))
