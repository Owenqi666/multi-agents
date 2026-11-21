"""Immutable review inputs and their canonical, content-addressed identity.

Construction and decoding trust the CLI boundary. Test records are supplied
evidence; neither this module nor the phase-one CLI executes their commands.
"""

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class FrozenFile:
    side: str
    path: str
    text: str | None
    oid: str
    mode: str


@dataclass(frozen=True, slots=True)
class TestEvidence:
    commit: str
    command: tuple[str, ...]
    exit_code: int
    log: str


@dataclass(frozen=True, slots=True)
class FrozenSnapshot:
    repository: str | None
    baseline: str | None
    candidate: str | None
    diff: str
    requirements: str
    acceptance: tuple[str, ...]
    tests: tuple[TestEvidence, ...]
    files: tuple[FrozenFile, ...]
    required_tests: tuple[tuple[str, ...], ...] = ()
    bound_repository: str | None = None

    @property
    def snapshot_id(self) -> str:
        encoded = json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """Return independent, JSON-native containers for the entire input."""
        result = {
            "repository": self.repository,
            "baseline": self.baseline,
            "candidate": self.candidate,
            "diff": self.diff,
            "requirements": self.requirements,
            "acceptance": list(self.acceptance),
            "tests": [
                {**asdict(item), "command": list(item.command)} for item in self.tests
            ],
            "files": [asdict(item) for item in self.files],
            "required_tests": [list(command) for command in self.required_tests],
        }
        if self.bound_repository is not None:
            result["bound_repository"] = self.bound_repository
        return result

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "FrozenSnapshot":
        """Decode a trusted stored value; validation belongs at the CLI boundary."""
        return cls(
            repository=value["repository"],
            baseline=value["baseline"],
            candidate=value["candidate"],
            diff=value["diff"],
            requirements=value["requirements"],
            acceptance=tuple(value["acceptance"]),
            tests=tuple(
                TestEvidence(
                    item["commit"],
                    tuple(item["command"]),
                    item["exit_code"],
                    item["log"],
                )
                for item in value["tests"]
            ),
            files=tuple(FrozenFile(**item) for item in value["files"]),
            required_tests=tuple(tuple(command) for command in value["required_tests"]),
            bound_repository=value.get("bound_repository"),
        )
