"""Planning artifacts preserve user authority and bind review to exact bytes."""

import json
from dataclasses import replace

import pytest
from jsonschema import Draft202012Validator

from multi_agent.cli.planning_inputs import (
    REVIEW_SCHEMA,
    parse_planning_draft,
    parse_planning_review,
    validate_planning_input,
)
from multi_agent.cli.task_inputs import (
    MAX_INPUT_BYTES,
    parse_project_rules,
    validate_task_references,
)
from multi_agent.core.planning import (
    DependencyReceipt,
    PlanningInput,
    PlanningRequest,
    UserDecision,
)
from multi_agent.core.tasks import FrozenProjectRules

BASELINE = "a" * 40
RULES = """version: 1
commands:
  unit:
    executable: uv
    args: [run, pytest, tests/unit]
protected_paths: [".agents/**"]
implementation:
  - paths: ["src/**"]
    provider: claude-code
    default_model: claude-sonnet-5-5
    default_effort: medium
"""


def planning_input(**changes) -> PlanningInput:
    value = PlanningInput(
        "  用户的原始需求\nKeep the exact text.\n",
        FrozenProjectRules(
            "/repository", BASELINE, "b" * 40, RULES, parse_project_rules(RULES)
        ),
        PlanningRequest("claude", "claude-sonnet-5-5", "high"),
        PlanningRequest("codex", "gpt-6.1-sol", "medium"),
    )
    return replace(value, **changes)


def draft_data(**changes) -> dict:
    value = {
        "schema_version": 1,
        "id": "T-0100",
        "goal": "Implement the requested behavior.",
        "baseline_commit": BASELINE,
        "write_scope": ["src/feature.py"],
        "depends_on": [],
        "interface_contracts": [{"name": "feature", "definition": "Return its input."}],
        "deliverables": ["The requested feature."],
        "acceptance_criteria": ["test_feature checks the result."],
        "test_refs": ["unit"],
        "open_questions": [],
    }
    value.update(changes)
    return value


def draft(**changes):
    return parse_planning_draft(json.dumps(draft_data(**changes)), planning_input())


def receipt(**changes) -> DependencyReceipt:
    return replace(
        DependencyReceipt(
            "T-0099", BASELINE, "user_declared", "User confirmed this baseline."
        ),
        **changes,
    )


def user_decision(**changes) -> UserDecision:
    return replace(
        UserDecision(
            "Which output?", "JSON", "user_declared", "User answered in this chat."
        ),
        **changes,
    )


def review_data(card=None, **changes) -> dict:
    card = draft() if card is None else card
    value = {
        "task_hash": card.task_hash,
        "decision": "ready",
        "reason": "The card follows the requested scope.\n",
        "blocking_issues": [],
    }
    value.update(changes)
    return value


def test_planning_boundary_preserves_unresolved_questions() -> None:
    source = planning_input()
    input_hash = source.input_hash
    data = draft_data(
        open_questions=[{"question": "Which output?", "blocks_dispatch": True}],
        proposed_tests=[
            {
                "name": "proposed",
                "executable": "/bin/sh",
                "args": ["-c", "$(untrusted proposal); do not execute"],
                "reason": "Only a model proposal.",
            }
        ],
    )
    card = parse_planning_draft(json.dumps(data), source)

    assert card.open_questions[0].resolution is None
    assert card.proposed_tests[0].args[1] == "$(untrusted proposal); do not execute"
    assert source.input_hash == input_hash
    assert source.requirements == "  用户的原始需求\nKeep the exact text.\n"
    assert tuple(command.name for command in source.frozen_rules.rules.commands) == (
        "unit",
    )
    with pytest.raises(ValueError, match="unresolved blocking"):
        validate_task_references(card, source.frozen_rules)

    data["test_refs"] = ["proposed"]
    with pytest.raises(ValueError, match="unknown test"):
        parse_planning_draft(json.dumps(data), source)


@pytest.mark.parametrize("requirements", ["", " \n\t", "bad\0text", "\ud800", 7])
def test_planning_requirements_must_be_nonempty_utf8(requirements) -> None:
    with pytest.raises(ValueError):
        validate_planning_input(planning_input(requirements=requirements))


def test_planning_input_limit_counts_utf8_bytes() -> None:
    with pytest.raises(ValueError, match="byte limit"):
        validate_planning_input(
            planning_input(requirements="界" * (MAX_INPUT_BYTES // 3 + 1))
        )


@pytest.mark.parametrize(
    ("field", "configuration"),
    [
        ("draft_request", PlanningRequest("codex", "gpt-6.1-sol", "high")),
        ("draft_request", PlanningRequest("claude-code", "claude-sonnet-5-5", "high")),
        ("review_request", PlanningRequest("claude", "claude-sonnet-5-5", "high")),
        ("review_request", PlanningRequest("gpt", "gpt-6.1-sol", "high")),
        ("draft_request", PlanningRequest("claude", "gpt-6.1-sol\n", "high")),
        ("review_request", PlanningRequest("codex", "$(model)", "high")),
        ("review_request", PlanningRequest("codex", "", "high")),
        ("review_request", PlanningRequest("codex", 7, "high")),
        ("draft_request", PlanningRequest("claude", "claude-sonnet-5-5", "ultra")),
        ("review_request", PlanningRequest("codex", "gpt-6.1-sol", "HIGH")),
        ("review_request", PlanningRequest("codex", "gpt-6.1-sol", "high\n")),
        ("review_request", PlanningRequest("codex", "gpt-6.1-sol", [])),
    ],
)
def test_planning_requests_use_fixed_roles_and_explicit_configuration(
    field, configuration
) -> None:
    with pytest.raises(ValueError):
        validate_planning_input(planning_input(**{field: configuration}))


def test_planning_provider_role_does_not_infer_model_identity() -> None:
    value = planning_input(
        draft_request=PlanningRequest("claude", "explicit-model-alias", "max"),
        review_request=PlanningRequest("codex", "explicit-model-alias", "ultra"),
    )
    validate_planning_input(value)
    assert value.draft_request.model == value.review_request.model


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("dependencies", []),
        ("decisions", []),
        ("dependencies", ({"task_id": "T-0099"},)),
        ("decisions", ({"question": "Which output?"},)),
        ("draft_request", {"provider": "claude"}),
        ("review_request", ("codex", "gpt-6.1-sol", "high")),
    ],
)
def test_planning_rejects_mutable_or_untyped_declarations(field, value) -> None:
    with pytest.raises(TypeError):
        validate_planning_input(planning_input(**{field: value}))


@pytest.mark.parametrize(
    "changes",
    [
        {"task_id": "T-0099\n"},
        {"task_id": "T-99"},
        {"task_id": []},
        {"baseline_commit": "b" * 40},
        {"baseline_commit": BASELINE + "\n"},
        {"source": "model_reported"},
        {"source": "user_declared\n"},
        {"detail": " "},
        {"detail": "\ud800"},
        {"detail": []},
    ],
)
def test_dependency_receipts_require_exact_user_declared_baseline(changes) -> None:
    with pytest.raises(ValueError):
        validate_planning_input(planning_input(dependencies=(receipt(**changes),)))


def test_dependency_receipts_are_unique_by_task_id() -> None:
    with pytest.raises(ValueError, match="duplicate dependency"):
        validate_planning_input(
            planning_input(
                dependencies=(receipt(), receipt(detail="A different declaration."))
            )
        )


@pytest.mark.parametrize("baseline", ["bad", BASELINE + "\n", "A" * 40, 7])
def test_planning_rejects_invalid_rules_baseline(baseline) -> None:
    value = planning_input()
    with pytest.raises(ValueError):
        validate_planning_input(
            replace(
                value,
                frozen_rules=replace(value.frozen_rules, baseline_commit=baseline),
            )
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"question": ""},
        {"question": []},
        {"question": "\ud800"},
        {"resolution": " \n"},
        {"resolution": "JSON\0"},
        {"resolution": []},
        {"source": "model_reported"},
        {"source": "user_declared\n"},
        {"detail": ""},
        {"detail": []},
    ],
)
def test_user_decisions_require_complete_explicit_provenance(changes) -> None:
    with pytest.raises(ValueError):
        validate_planning_input(planning_input(decisions=(user_decision(**changes),)))


def test_user_decisions_cannot_conflict_for_one_question() -> None:
    with pytest.raises(ValueError, match="duplicate user decision"):
        validate_planning_input(
            planning_input(decisions=(user_decision(), user_decision(resolution="XML")))
        )


def test_draft_dependencies_use_baseline_receipts_and_reject_self_cycles() -> None:
    data = draft_data(depends_on=["T-0099"])
    with pytest.raises(ValueError, match="unknown dependency"):
        parse_planning_draft(json.dumps(data), planning_input())
    value = planning_input(dependencies=(receipt(),))
    card = parse_planning_draft(json.dumps(data), value)
    assert card.depends_on == ("T-0099",)
    assert value.dependencies[0].source == "user_declared"

    data["depends_on"] = ["T-0100"]
    value = planning_input(dependencies=(receipt(task_id="T-0100"),))
    with pytest.raises(ValueError, match="dependency cycle"):
        parse_planning_draft(json.dumps(data), value)


@pytest.mark.parametrize(
    "changes", [{"baseline_commit": "b" * 40}, {"test_refs": ["invented"]}]
)
def test_draft_must_match_baseline_and_existing_named_commands(changes) -> None:
    with pytest.raises(ValueError):
        draft(**changes)


@pytest.mark.parametrize(
    "question,resolution",
    [("Which output?", "XML"), ("Which output? ", "JSON"), ("Which output?", "JSON\n")],
)
def test_draft_cannot_invent_or_rewrite_user_resolution(question, resolution) -> None:
    data = draft_data(
        open_questions=[
            {"question": question, "blocks_dispatch": True, "resolution": resolution}
        ]
    )
    with pytest.raises(ValueError, match="user decision"):
        parse_planning_draft(
            json.dumps(data), planning_input(decisions=(user_decision(),))
        )


def test_blocking_resolution_needs_separate_matching_user_decision() -> None:
    data = draft_data(
        open_questions=[
            {"question": "Which output?", "blocks_dispatch": True, "resolution": "JSON"}
        ]
    )
    with pytest.raises(ValueError, match="user decision"):
        parse_planning_draft(json.dumps(data), planning_input())

    value = planning_input(decisions=(user_decision(),))
    card = parse_planning_draft(json.dumps(data), value)
    validate_task_references(card, value.frozen_rules)
    assert card.open_questions[0].resolution == "JSON"
    assert value.input_hash != planning_input().input_hash


def test_draft_does_not_silently_fill_user_answers() -> None:
    data = draft_data(
        open_questions=[{"question": "Which output?", "blocks_dispatch": True}]
    )
    value = planning_input(decisions=(user_decision(),))
    card = parse_planning_draft(json.dumps(data), value)
    assert card.open_questions[0].resolution is None


@pytest.mark.parametrize(
    "text",
    [
        "{}",
        "[]",
        "not JSON",
        json.dumps(draft_data(unknown=True)),
        json.dumps(draft_data(goal=float("nan"))),
        json.dumps(draft_data(goal="\ud800")),
        json.dumps(draft_data(id="T-0100\n")),
        json.dumps(draft_data()).replace(
            '"id": "T-0100"', '"id": "T-0100", "id": "T-0101"'
        ),
        json.dumps(draft_data()).replace(
            '"name": "feature"', '"name": "feature", "name": "other"'
        ),
    ],
)
def test_planning_draft_uses_strict_task_card_parser(text) -> None:
    with pytest.raises(ValueError):
        parse_planning_draft(text, planning_input())


def test_planning_draft_deep_malformed_json_preserves_error_cause() -> None:
    with pytest.raises(ValueError) as raised:
        parse_planning_draft("[" * 1500 + "]" * 1500, planning_input())
    assert raised.value.__cause__ is not None


def test_planning_review_schema_and_valid_artifacts_preserve_exact_text() -> None:
    card = draft()
    for decision, issues in [
        ("ready", []),
        ("needs_revision", []),
        ("needs_revision", ["Specify output."]),
    ]:
        value = review_data(card, decision=decision, blocking_issues=issues)
        Draft202012Validator(REVIEW_SCHEMA).validate(value)
        parsed = parse_planning_review(json.dumps(value), card)
        assert parsed.task_hash == card.task_hash
        assert parsed.decision == decision
        assert parsed.reason == value["reason"]
        assert parsed.blocking_issues == tuple(issues)
        assert parsed.artifact_hash != parsed.task_hash


def test_planning_review_cannot_cross_task_revision_hash() -> None:
    previous = draft()
    revised = draft(goal="A changed outcome.")
    with pytest.raises(ValueError, match="task hash"):
        parse_planning_review(json.dumps(review_data(previous)), revised)


@pytest.mark.parametrize(
    "changes",
    [
        {"task_hash": "b" * 64},
        {"task_hash": "A" * 64},
        {"task_hash": []},
        {"decision": "approved"},
        {"decision": "ready\n"},
        {"reason": " \n"},
        {"reason": 5},
        {"reason": "\ud800"},
        {"reason": "okay\0"},
        {"blocking_issues": "none"},
        {"blocking_issues": ["Still blocked"]},
        {"decision": "needs_revision", "blocking_issues": [""]},
        {"decision": "needs_revision", "blocking_issues": [" "]},
        {"decision": "needs_revision", "blocking_issues": ["same", "same"]},
        {"decision": "needs_revision", "blocking_issues": [{"reason": "no"}]},
        {"decision": "needs_revision", "blocking_issues": ["\ud800"]},
        {"unknown": True},
    ],
)
def test_planning_review_rejects_invalid_artifacts(changes) -> None:
    card = draft()
    with pytest.raises(ValueError):
        parse_planning_review(json.dumps(review_data(card, **changes)), card)


def test_review_hash_must_not_have_trailing_newline() -> None:
    card = draft()
    with pytest.raises(ValueError, match="task hash"):
        parse_planning_review(
            json.dumps(review_data(card, task_hash=card.task_hash + "\n")), card
        )


@pytest.mark.parametrize(
    "field", ["task_hash", "decision", "reason", "blocking_issues"]
)
def test_planning_review_requires_every_field(field) -> None:
    card = draft()
    value = review_data(card)
    del value[field]
    with pytest.raises(ValueError):
        parse_planning_review(json.dumps(value), card)


@pytest.mark.parametrize(
    "text",
    [
        "",
        " ",
        "[]",
        "null",
        "42",
        "{} trailing",
        '{"reason":"one","reason":"two"}',
        '{"unknown":{"nested":1,"nested":2}}',
        "[" * 1500 + "]" * 1500,
        "\ud800",
    ],
)
def test_planning_review_rejects_malformed_and_duplicate_json(text) -> None:
    with pytest.raises(ValueError):
        parse_planning_review(text, draft())


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_planning_review_rejects_nonfinite_numbers(constant) -> None:
    with pytest.raises(ValueError, match="nonfinite"):
        parse_planning_review('{"reason":' + constant + "}", draft())


def test_planning_review_respects_utf8_byte_limit() -> None:
    card = draft()
    value = review_data(card, reason="界" * (MAX_INPUT_BYTES // 3 + 1))
    with pytest.raises(ValueError, match="byte limit"):
        parse_planning_review(json.dumps(value, ensure_ascii=False), card)


def test_planning_review_retains_schema_error_as_cause() -> None:
    with pytest.raises(ValueError) as raised:
        parse_planning_review("{}", draft())
    assert raised.value.__cause__ is not None
