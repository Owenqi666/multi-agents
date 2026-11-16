"""Immutable task and project-rule inputs; validation belongs to the CLI boundary."""

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class InterfaceContract:
    name: str
    definition: str


@dataclass(frozen=True, slots=True)
class OpenQuestion:
    question: str
    blocks_dispatch: bool
    resolution: str | None = None


@dataclass(frozen=True, slots=True)
class ProposedTest:
    name: str
    executable: str
    args: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class TaskCard:
    schema_version: int
    id: str
    goal: str
    baseline_commit: str
    write_scope: tuple[str, ...]
    depends_on: tuple[str, ...]
    interface_contracts: tuple[InterfaceContract, ...]
    deliverables: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    test_refs: tuple[str, ...]
    open_questions: tuple[OpenQuestion, ...]
    proposed_tests: tuple[ProposedTest, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-native independent containers, including explicit empty arrays."""
        result: dict[str, Any] = json.loads(json.dumps(asdict(self)))
        for question in result["open_questions"]:
            if question["resolution"] is None:
                del question["resolution"]
        return result

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TaskCard":
        """Decode trusted stored or boundary-validated data."""
        return cls(
            schema_version=value["schema_version"],
            id=value["id"],
            goal=value["goal"],
            baseline_commit=value["baseline_commit"],
            write_scope=tuple(value["write_scope"]),
            depends_on=tuple(value["depends_on"]),
            interface_contracts=tuple(
                InterfaceContract(**item) for item in value["interface_contracts"]
            ),
            deliverables=tuple(value["deliverables"]),
            acceptance_criteria=tuple(value["acceptance_criteria"]),
            test_refs=tuple(value["test_refs"]),
            open_questions=tuple(
                OpenQuestion(**item) for item in value["open_questions"]
            ),
            proposed_tests=tuple(
                ProposedTest(
                    item["name"],
                    item["executable"],
                    tuple(item["args"]),
                    item["reason"],
                )
                for item in value.get("proposed_tests", [])
            ),
        )

    @property
    def task_hash(self) -> str:
        encoded = json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class NamedCommand:
    name: str
    executable: str
    args: tuple[str, ...]

    @property
    def argv(self) -> tuple[str, ...]:
        return (self.executable, *self.args)


@dataclass(frozen=True, slots=True)
class ImplementationConfig:
    provider: str
    default_model: str
    default_effort: str
    allowed_models: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ImplementationRule:
    paths: tuple[str, ...]
    config: ImplementationConfig


@dataclass(frozen=True, slots=True)
class TaskTypeRestriction:
    name: str
    provider: str
    allowed_models: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProjectRules:
    version: int
    commands: tuple[NamedCommand, ...]
    protected_paths: tuple[str, ...]
    implementation: tuple[ImplementationRule, ...]
    default_implementation: ImplementationConfig | None
    task_types: tuple[TaskTypeRestriction, ...] = ()


@dataclass(frozen=True, slots=True)
class FrozenProjectRules:
    repository: str
    baseline_commit: str
    blob_oid: str
    raw_text: str
    rules: ProjectRules

    @property
    def rules_hash(self) -> str:
        """Hash the exact baseline UTF-8 bytes, including comments and whitespace."""
        return hashlib.sha256(self.raw_text.encode("utf-8")).hexdigest()
