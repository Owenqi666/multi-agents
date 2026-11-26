"""Validate product task cards and freeze project rules from committed Git blobs.

No project command, filter or model is executed here. The product schema is the
same file used by the development skill, exposed as an installed package resource.
"""

import json
import re
import unicodedata
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, ValidationError
from yaml.nodes import MappingNode

from multi_agent.cli.snapshot import _git, _resolve_commit, _string
from multi_agent.core.tasks import (
    FrozenProjectRules,
    ImplementationConfig,
    ImplementationRule,
    NamedCommand,
    ProjectRules,
    TaskCard,
    TaskTypeRestriction,
)

MAX_INPUT_BYTES = 1024 * 1024
RULES_PATH = ".multi-agent/rules.yaml"
PROVIDERS = frozenset(("codex", "claude-code"))
_NAME = re.compile(r"[a-z][a-z0-9-]*\Z")
_TASK_ID = re.compile(r"T-[0-9]{4}\Z")
_COMMIT_ID = re.compile(r"[0-9a-f]{40}\Z")


class MissingProjectRules(ValueError):
    """The selected baseline has no committed rules file."""


def _input_text(text: str) -> str:
    _string(text, "input", nonempty=True)
    if len(text.encode("utf-8")) > MAX_INPUT_BYTES:
        raise ValueError("input exceeds byte limit")
    return text


def _unique_json(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _json_strings(value: Any) -> None:
    if isinstance(value, str):
        _string(value, "JSON string")
    elif isinstance(value, list):
        for item in value:
            _json_strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            _string(key, "JSON key")
            _json_strings(item)


def load_task_schema() -> dict[str, Any]:
    schema: dict[str, Any] = json.loads(
        files("multi_agent")
        .joinpath("task-card.schema.json")
        .read_text(encoding="utf-8")
    )
    return schema


def validate_path_pattern(value: str) -> str:
    """POSIX paths; '*'/'?' within a segment, '**' only as an entire segment.

    Scope patterns can name future files. Their language is resolved by the
    subsequent dispatch layer, never by enumerating just the current checkout.
    """
    _string(value, "path pattern", nonempty=True)
    parts = value.split("/")
    if (
        "\\" in value
        or any(char in value for char in "[]{}()")
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(
            part in ("", ".", "..")
            or part.rstrip(" .") != part
            or unicodedata.normalize("NFC", part).casefold() == ".git"
            or ("**" in part and part != "**")
            for part in parts
        )
    ):
        raise ValueError(f"unsafe or unsupported path pattern: {value!r}")
    return value


def parse_task_card(text: str) -> TaskCard:
    try:
        value = json.loads(_input_text(text), object_pairs_hook=_unique_json)
        Draft202012Validator(load_task_schema()).validate(value)
        _json_strings(value)
    except (json.JSONDecodeError, ValidationError, RecursionError) as error:
        raise ValueError(f"invalid task card: {error}") from error
    if not _TASK_ID.fullmatch(value["id"]) or any(
        not _TASK_ID.fullmatch(identifier) for identifier in value["depends_on"]
    ):
        raise ValueError("invalid task id")
    if not _COMMIT_ID.fullmatch(value["baseline_commit"]):
        raise ValueError("invalid task baseline commit")
    for name in [
        *value["test_refs"],
        *(item["name"] for item in value.get("proposed_tests", [])),
    ]:
        _name(name)
    for pattern in value["write_scope"]:
        validate_path_pattern(pattern)
    return TaskCard.from_dict(value)


class _RulesLoader(yaml.SafeLoader):
    def construct_mapping(
        self, node: MappingNode, deep: bool = False
    ) -> dict[Any, Any]:
        result: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = _string(
                self.construct_object(key_node, deep=deep), "YAML mapping key"
            )
            if key in result:
                raise ValueError(f"duplicate YAML key: {key}")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


# Do not mutate SafeLoader's shared resolver table. YAML 1.1's on/off/yes/no
# remain strings; true/false are the only implicit boolean spellings in v1.
_RulesLoader.yaml_implicit_resolvers = {
    key: [
        (tag, expression)
        for tag, expression in entries
        if tag != "tag:yaml.org,2002:bool"
    ]
    for key, entries in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_RulesLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|false)$", re.IGNORECASE),
    list("tTfF"),
)


def _mapping(
    value: Any,
    name: str,
    required: set[str] | None = None,
    optional: set[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be a mapping with string keys")
    if required is not None and (
        not required <= value.keys() or value.keys() - required - (optional or set())
    ):
        raise ValueError(f"{name} has missing or unknown fields")
    return value


def _sequence(value: Any, name: str, *, nonempty: bool = False) -> list[Any]:
    if not isinstance(value, list) or (nonempty and not value):
        raise ValueError(f"{name} must be {'a nonempty' if nonempty else 'an'} array")
    return value


def _texts(value: Any, name: str, *, nonempty: bool = False) -> tuple[str, ...]:
    items = tuple(
        _string(item, name, nonempty=True)
        for item in _sequence(value, name, nonempty=nonempty)
    )
    if len(items) != len(set(items)):
        raise ValueError(f"{name} must be unique")
    return items


def _provider(value: Any) -> str:
    result = _string(value, "provider", nonempty=True)
    if result not in PROVIDERS:
        raise ValueError(f"unknown provider: {result}")
    return result


def _name(value: Any) -> str:
    result = _string(value, "name", nonempty=True)
    if not _NAME.fullmatch(result):
        raise ValueError(f"invalid name: {result}")
    return result


def _implementation(value: Any) -> ImplementationConfig:
    data = _mapping(
        value,
        "implementation config",
        {"provider", "default_model", "default_effort"},
        {"allowed_models"},
    )
    model = _string(data["default_model"], "default_model", nonempty=True)
    models = _texts(
        data.get("allowed_models", [model]), "allowed_models", nonempty=True
    )
    if model not in models:
        raise ValueError("default_model must be in allowed_models")
    return ImplementationConfig(
        _provider(data["provider"]),
        model,
        _string(data["default_effort"], "default_effort", nonempty=True),
        models,
    )


def parse_project_rules(text: str) -> ProjectRules:
    try:
        _input_text(text)
        if any(isinstance(event, yaml.AliasEvent) for event in yaml.parse(text)):
            raise ValueError("YAML aliases and merge keys are not supported")
        value = yaml.load(text, Loader=_RulesLoader)
    except (yaml.YAMLError, RecursionError) as error:
        raise ValueError(f"invalid rules YAML: {error}") from error
    data = _mapping(
        value,
        "rules",
        {"version", "commands", "protected_paths", "implementation"},
        {"default_implementation", "task_types"},
    )
    if type(data["version"]) is not int or data["version"] != 1:
        raise ValueError("rules version must be 1")
    commands_data = _mapping(data["commands"], "commands")
    commands: list[NamedCommand] = []
    for name, command in commands_data.items():
        command = _mapping(command, "command", {"executable", "args"})
        commands.append(
            NamedCommand(
                _name(name),
                _string(command["executable"], "executable", nonempty=True),
                tuple(
                    _string(arg, "argument")
                    for arg in _sequence(command["args"], "args")
                ),
            )
        )
    protected = tuple(
        validate_path_pattern(item)
        for item in _texts(data["protected_paths"], "protected_paths")
    )
    implementation: list[ImplementationRule] = []
    for item in _sequence(data["implementation"], "implementation"):
        item = _mapping(
            item,
            "implementation rule",
            {"paths", "provider", "default_model", "default_effort"},
            {"allowed_models"},
        )
        paths = tuple(
            validate_path_pattern(path)
            for path in _texts(item["paths"], "paths", nonempty=True)
        )
        implementation.append(
            ImplementationRule(
                paths,
                _implementation(
                    {key: val for key, val in item.items() if key != "paths"}
                ),
            )
        )
    default = (
        _implementation(data["default_implementation"])
        if "default_implementation" in data
        else None
    )
    restrictions = _mapping(data.get("task_types", {}), "task_types")
    task_types: list[TaskTypeRestriction] = []
    for name, restriction in restrictions.items():
        restriction = _mapping(restriction, "task type", {"provider", "allowed_models"})
        task_types.append(
            TaskTypeRestriction(
                _name(name),
                _provider(restriction["provider"]),
                _texts(restriction["allowed_models"], "allowed_models", nonempty=True),
            )
        )
    return ProjectRules(
        1, tuple(commands), protected, tuple(implementation), default, tuple(task_types)
    )


def freeze_project_rules(repository: Path, baseline: str) -> FrozenProjectRules:
    try:
        repository = repository.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"repository cannot be opened: {error}") from error
    if not repository.is_dir() or any(char in str(repository) for char in "\n\r"):
        raise ValueError("repository must be a directory without line endings")
    commit = _resolve_commit(repository, baseline)
    record = _git(repository, "ls-tree", "--full-tree", "-z", commit, "--", RULES_PATH)
    if not record:
        raise MissingProjectRules("baseline has no .multi-agent/rules.yaml")
    metadata, path = record.removesuffix(b"\0").split(b"\t", 1)
    mode, kind, oid = metadata.decode("ascii").split()
    if (
        path != RULES_PATH.encode()
        or kind != "blob"
        or mode not in ("100644", "100755")
    ):
        raise ValueError("rules must be a regular Git blob")
    if int(_git(repository, "cat-file", "-s", oid)) > MAX_INPUT_BYTES:
        raise ValueError("rules blob exceeds byte limit")
    try:
        text = _git(repository, "cat-file", "blob", oid).decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("rules must be UTF-8") from error
    identity = (
        _git(repository, "rev-parse", "--path-format=absolute", "--git-common-dir")
        .decode("utf-8")
        .removesuffix("\n")
    )
    return FrozenProjectRules(
        str(Path(identity).resolve()), commit, oid, text, parse_project_rules(text)
    )


def validate_task_graph(
    cards: tuple[TaskCard, ...], integrated_ids: frozenset[str] = frozenset()
) -> None:
    """Validate known dependency IDs and cycles, without claiming integration."""
    if any(not _TASK_ID.fullmatch(item) for item in integrated_ids):
        raise ValueError("invalid integrated task id")
    by_id = {card.id: card for card in cards}
    if len(by_id) != len(cards):
        raise ValueError("duplicate task id")
    known = set(by_id) | integrated_ids
    for card in cards:
        if set(card.depends_on) - known:
            raise ValueError(f"unknown dependency for {card.id}")
    remaining = {card.id: set(card.depends_on) & by_id.keys() for card in cards}
    while remaining:
        roots = {
            identifier
            for identifier, dependencies in remaining.items()
            if not dependencies
        }
        if not roots:
            raise ValueError("task dependency cycle")
        remaining = {
            identifier: dependencies - roots
            for identifier, dependencies in remaining.items()
            if identifier not in roots
        }


def validate_task_references(card: TaskCard, rules: FrozenProjectRules) -> None:
    if card.baseline_commit != rules.baseline_commit:
        raise ValueError("task baseline does not match frozen rules")
    if set(card.test_refs) - {command.name for command in rules.rules.commands}:
        raise ValueError("task references an unknown test command")
    if any(
        question.blocks_dispatch and question.resolution is None
        for question in card.open_questions
    ):
        raise ValueError("unresolved blocking question prevents dispatch")
