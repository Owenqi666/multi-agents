"""Instance-local ownership facts, never reconstructed from runtime result text."""

from __future__ import annotations

from dataclasses import dataclass
from tempfile import TemporaryDirectory
from types import TracebackType
from typing import Literal, Protocol
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class ProcessOwnership:
    resource_id: str
    label: str
    pid: int
    exit_code: int | None
    owns_group: bool
    group_gone: bool
    watcher_settled: bool
    readers_settled: bool
    stdout_eof: bool
    stderr_eof: bool

    @property
    def stopped(self) -> bool:
        return (
            self.exit_code is not None
            and (not self.owns_group or self.group_gone)
            and self.watcher_settled
            and self.readers_settled
        )

    @property
    def output_complete(self) -> bool:
        return self.stdout_eof and self.stderr_eof


@dataclass(frozen=True, slots=True)
class IsolationOwnership:
    resource_id: str
    root: str
    pending_spawns: int
    unknown_spawns: int
    process_count: int
    unstopped_processes: int
    removed: bool


@dataclass(frozen=True, slots=True)
class LogOwnership:
    resource_id: str
    path: str
    identity: tuple[int, int] | None
    records: int
    closed: bool
    healthy: bool
    byte_count: int | None
    sha256: str | None
    sealed: bool


@dataclass(frozen=True, slots=True)
class DirectoryOwnership:
    resource_id: str
    root: str
    removed: bool


type ResourceOwnership = (
    ProcessOwnership | IsolationOwnership | LogOwnership | DirectoryOwnership
)


class OwnedResource(Protocol):
    def ownership_record(self, resource_id: str) -> ResourceOwnership: ...


@dataclass(frozen=True, slots=True)
class AcquisitionOwnership:
    number: int
    kind: str
    outcome: Literal["pending", "not_created", "published", "unknown"]
    resource_id: str | None


@dataclass(frozen=True, slots=True)
class OperationOwnership:
    number: int
    kind: str
    active: bool


@dataclass(frozen=True, slots=True)
class RuntimeOwnershipReceipt:
    owner_id: str
    operations: tuple[OperationOwnership, ...]
    acquisitions: tuple[AcquisitionOwnership, ...]
    processes: tuple[ProcessOwnership, ...]
    isolations: tuple[IsolationOwnership, ...]
    logs: tuple[LogOwnership, ...]
    directories: tuple[DirectoryOwnership, ...] = ()

    @property
    def active_operations(self) -> int:
        return sum(operation.active for operation in self.operations)

    @property
    def pending_acquisitions(self) -> int:
        return sum(item.outcome == "pending" for item in self.acquisitions)

    @property
    def unknown_acquisitions(self) -> int:
        return sum(item.outcome == "unknown" for item in self.acquisitions)

    @property
    def settled(self) -> bool:
        return (
            bool(self.operations)
            and not self.active_operations
            and not self.pending_acquisitions
            and not self.unknown_acquisitions
            and all(
                not item.pending_spawns and not item.unknown_spawns
                for item in self.isolations
            )
        )

    @property
    def processes_stopped(self) -> bool:
        return (
            self.settled
            and all(item.stopped for item in self.processes)
            and all(not item.unstopped_processes for item in self.isolations)
        )

    @property
    def resources_released(self) -> bool:
        return (
            self.processes_stopped
            and all(item.removed for item in self.isolations)
            and all(item.removed for item in self.directories)
        )

    @property
    def logs_sealed(self) -> bool:
        return (
            self.settled
            and bool(self.logs)
            and all(
                item.outcome == "published"
                for item in self.acquisitions
                if item.kind == "log"
            )
            and all(item.sealed for item in self.logs)
        )

    @property
    def outputs_complete(self) -> bool:
        return self.settled and all(item.output_complete for item in self.processes)


class Acquisition:
    """A factory ticket; publishing keeps the actual object, even after failure."""

    def __init__(self, ledger: OwnershipLedger, number: int, kind: str) -> None:
        self._ledger = ledger
        self.number = number
        self.kind = kind
        self.outcome: Literal["pending", "not_created", "published", "unknown"] = (
            "pending"
        )
        self.resource: OwnedResource | None = None
        self.resource_id: str | None = None

    def publish(self, resource: OwnedResource) -> None:
        if self.resource is not None and self.resource is not resource:
            raise RuntimeError("acquisition already owns a different resource")
        if self.outcome == "not_created":
            raise RuntimeError("a failed launch cannot acquire a resource")
        self.resource = resource
        self.resource_id = self._ledger._register(self.kind, resource)
        self.outcome = "published"

    def not_created(self) -> None:
        if self.resource is not None:
            raise RuntimeError("a published resource cannot be declared uncreated")
        self.outcome = "not_created"

    def finish(self) -> None:
        if self.outcome == "pending":
            self.outcome = "unknown"

    def record(self) -> AcquisitionOwnership:
        return AcquisitionOwnership(
            self.number, self.kind, self.outcome, self.resource_id
        )


class OwnershipLedger:
    """In-memory registration; no loading from receipts or adoption of old owners."""

    def __init__(self) -> None:
        self.owner_id = uuid4().hex
        self._operations: list[OperationOwnership] = []
        self._acquisitions: list[Acquisition] = []
        self._resources: list[tuple[str, OwnedResource]] = []

    def begin_operation(self, kind: str) -> int:
        number = len(self._operations) + 1
        self._operations.append(OperationOwnership(number, kind, True))
        return number

    def finish_operation(self, number: int) -> None:
        operation = self._operations[number - 1]
        if not operation.active:
            raise RuntimeError("ownership operation already finished")
        self._operations[number - 1] = OperationOwnership(number, operation.kind, False)

    def acquire(self, kind: str) -> Acquisition:
        ticket = Acquisition(self, len(self._acquisitions) + 1, kind)
        self._acquisitions.append(ticket)
        return ticket

    def _register(self, kind: str, resource: OwnedResource) -> str:
        for identifier, existing in self._resources:
            if existing is resource:
                return identifier
        identifier = f"{kind}-{len(self._resources) + 1}"
        self._resources.append((identifier, resource))
        return identifier

    def snapshot(self) -> RuntimeOwnershipReceipt:
        resources = tuple(
            resource.ownership_record(identifier)
            for identifier, resource in self._resources
        )
        return RuntimeOwnershipReceipt(
            self.owner_id,
            tuple(self._operations),
            tuple(item.record() for item in self._acquisitions),
            tuple(item for item in resources if isinstance(item, ProcessOwnership)),
            tuple(item for item in resources if isinstance(item, IsolationOwnership)),
            tuple(item for item in resources if isinstance(item, LogOwnership)),
            tuple(item for item in resources if isinstance(item, DirectoryOwnership)),
        )


class OwnedTemporaryDirectory:
    """Publish temporary storage before use and retain unsuccessful cleanup."""

    def __init__(
        self,
        ledger: OwnershipLedger,
        *,
        prefix: str = "tmp",
        dir: str | None = None,
    ) -> None:
        ticket = ledger.acquire("directory")
        self._removed = False
        try:
            # A constructor exception does not prove that allocation never
            # happened. The unfinished acquisition remains explicitly unknown.
            self._directory = TemporaryDirectory(prefix=prefix, dir=dir)
            self.name = self._directory.name
            try:
                ticket.publish(self)
            except BaseException:
                self.cleanup()
                raise
        finally:
            ticket.finish()

    def cleanup(self) -> None:
        if not self._removed:
            self._directory.cleanup()
            self._removed = True

    def ownership_record(self, resource_id: str) -> DirectoryOwnership:
        return DirectoryOwnership(resource_id, self.name, self._removed)

    def __enter__(self) -> str:
        return self.name

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.cleanup()
