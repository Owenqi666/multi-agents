"""Physical test preparation and immutable integration command observations."""

from dataclasses import dataclass
from typing import Any, Literal

from .tasks import NamedCommand
from .workspaces import WorkspaceControl

type IntegrationTestStatus = Literal["succeeded", "failed", "cancelled"]


@dataclass(frozen=True, slots=True, kw_only=True)
class TestCheckoutLayout:
    repository: str
    root: str
    root_identity: tuple[int, int]
    git_directory: str
    git_identity: tuple[int, int]
    owner_token: str
    object_format: str
    initial_digest: str
    controls: tuple[WorkspaceControl, ...]
    objects: tuple[tuple[str, str], ...]
    commit: str
    tree: str


@dataclass(frozen=True, slots=True, kw_only=True)
class IntegrationTestCheckout:
    layout: TestCheckoutLayout
    database_path: str
    commands: tuple[NamedCommand, ...]

    @property
    def repository(self) -> str:
        return self.layout.repository

    @property
    def root(self) -> str:
        return self.layout.root

    @property
    def root_identity(self) -> tuple[int, int]:
        return self.layout.root_identity

    @property
    def git_directory(self) -> str:
        return self.layout.git_directory

    @property
    def git_identity(self) -> tuple[int, int]:
        return self.layout.git_identity


@dataclass(frozen=True, slots=True, kw_only=True)
class IntegrationTestCommand:
    sequence: int
    command: NamedCommand
    request_attempted: bool
    request_settled: bool
    reported_exit_code: int | None
    exit_code: int | None
    process_stopped: bool | None
    manifest_path: str | None
    manifest_sha256: str | None
    qualified: bool
    reason: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class IntegrationTestResult:
    status: IntegrationTestStatus
    process_stopped: bool | None
    commands: tuple[IntegrationTestCommand, ...]
    reason: str | None
    preparation_path: str | None
    preparation_sha256: str | None


def decode_integration_test_result(value: dict[str, Any]) -> IntegrationTestResult:
    commands = []
    for item in value["commands"]:
        command = item["command"]
        commands.append(
            IntegrationTestCommand(
                sequence=item["sequence"],
                command=NamedCommand(
                    command["name"], command["executable"], tuple(command["args"])
                ),
                request_attempted=item["request_attempted"],
                request_settled=item["request_settled"],
                reported_exit_code=item["reported_exit_code"],
                exit_code=item["exit_code"],
                process_stopped=item["process_stopped"],
                manifest_path=item["manifest_path"],
                manifest_sha256=item["manifest_sha256"],
                qualified=item["qualified"],
                reason=item["reason"],
            )
        )
    return IntegrationTestResult(
        status=value["status"],
        process_stopped=value["process_stopped"],
        commands=tuple(commands),
        reason=value["reason"],
        preparation_path=value["preparation_path"],
        preparation_sha256=value["preparation_sha256"],
    )
