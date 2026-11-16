"""Frozen planning inputs remain independent of mutable caller-owned containers."""

import hashlib
from dataclasses import FrozenInstanceError

import pytest

from multi_agent.core.tasks import (
    FrozenProjectRules,
    ImplementationConfig,
    ImplementationRule,
    NamedCommand,
    ProjectRules,
    TaskCard,
    TaskTypeRestriction,
)


def task_value() -> dict:
    return {
        "schema_version": 1,
        "id": "T-0100",
        "goal": "Implement the specified change.",
        "baseline_commit": "a" * 40,
        "write_scope": ["frontend/**"],
        "depends_on": ["T-0099"],
        "interface_contracts": [{"name": "input", "definition": "Accept a string."}],
        "deliverables": ["The documented behavior."],
        "acceptance_criteria": ["The configured test passes."],
        "test_refs": ["frontend-unit"],
        "open_questions": [{"question": "Which label?", "blocks_dispatch": False}],
        "proposed_tests": [
            {
                "name": "new-check",
                "executable": "python",
                "args": ["check.py"],
                "reason": "Check the new behavior.",
            }
        ],
    }


def test_frozen_contracts_are_immutable() -> None:
    source = task_value()
    task = TaskCard.from_dict(source)
    config = ImplementationConfig("claude-code", "model-a", "high", ("model-a",))
    command = NamedCommand("frontend-unit", "pnpm", ("--filter", "frontend", "test"))
    rule = ImplementationRule(("frontend/**",), config)
    restriction = TaskTypeRestriction("frontend", "claude-code", ("model-a",))
    rules = ProjectRules(
        1, (command,), (".multi-agent/**",), (rule,), None, (restriction,)
    )
    frozen = FrozenProjectRules("/repo", "a" * 40, "b" * 40, "version: 1\n", rules)
    for instance, field, value in (
        (task, "goal", "changed"),
        (task.interface_contracts[0], "definition", "changed"),
        (task.open_questions[0], "resolution", "changed"),
        (task.proposed_tests[0], "args", ("changed",)),
        (command, "args", ("changed",)),
        (config, "allowed_models", ("changed",)),
        (rule, "paths", ("changed",)),
        (restriction, "provider", "codex"),
        (rules, "commands", ()),
        (frozen, "raw_text", "changed"),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(instance, field, value)

    source["write_scope"].append("backend/**")
    source["interface_contracts"][0]["definition"] = "changed"
    source["proposed_tests"][0]["args"].append("--changed")
    assert task.write_scope == ("frontend/**",)
    assert task.interface_contracts[0].definition == "Accept a string."
    assert task.proposed_tests[0].args == ("check.py",)
    assert command.argv == ("pnpm", "--filter", "frontend", "test")


def test_task_serialization_and_hash_are_independent_of_caller_mutations() -> None:
    task = TaskCard.from_dict(task_value())
    digest = task.task_hash
    serialized = task.to_dict()
    assert TaskCard.from_dict(serialized) == task
    assert "resolution" not in serialized["open_questions"][0]
    serialized["write_scope"].append("backend/**")
    serialized["interface_contracts"][0]["definition"] = "changed"
    serialized["open_questions"][0]["resolution"] = "changed"
    serialized["proposed_tests"][0]["args"].append("--changed")
    assert task.task_hash == digest
    assert TaskCard.from_dict(serialized).task_hash != digest
    reordered = dict(reversed(list(task_value().items())))
    assert TaskCard.from_dict(reordered).task_hash == digest


def test_frozen_rules_hash_preserves_original_utf8_bytes() -> None:
    rules = ProjectRules(1, (), (), (), None)
    raw = "# 原始规则\r\nversion: 1\r\n"
    frozen = FrozenProjectRules("/repo", "a" * 40, "b" * 40, raw, rules)
    assert frozen.rules_hash == hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert (
        frozen.rules_hash
        != hashlib.sha256(raw.replace("\r\n", "\n").encode()).hexdigest()
    )
