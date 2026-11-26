"""Execute the project suite on one fixed integration candidate."""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from multi_agent.adapters.test_runtime import TaskTestRuntimeAdapter, TestCommandResult
from multi_agent.core.integration import IntegrationCandidate
from multi_agent.core.integration_testing import (
    IntegrationTestCheckout,
    IntegrationTestCommand,
    IntegrationTestResult,
    IntegrationTestStatus,
    TestCheckoutLayout,
)
from multi_agent.core.review_input import ExecutedCommand, ExecutedLog
from multi_agent.core.task_execution import task_execution_value
from multi_agent.core.task_test_execution import TaskTestPolicy, TaskTestScratch
from multi_agent.core.tasks import FrozenProjectRules, NamedCommand
from multi_agent.core.workspaces import WorkspaceControl
from multi_agent.job_activity import observe, start_actor

from .task_context import (
    _Database,
    _database,
    _directory,
    _identity,
    _json,
    _json_bytes,
    _new_root,
    _sync_directory,
    _write_control,
)
from .task_test_checkouts import (
    create_test_checkout_layout,
    inspect_task_test_checkout_controls,
)
from .task_test_context import check_test_runtime_paths, create_test_scratch
from .task_test_evidence import (
    _audit_matches,
    _logs,
    _read,
    _result,
    _runtime_qualified,
    _stopped,
    command_process_stopped,
)
from .workspaces import ExecutorLease, _check_lease, _git

_MANIFEST_BYTES = 8 * 1024 * 1024
_COMMAND_FAILURES = (OSError, ValueError, TypeError, RuntimeError)
_PREPARATION = "integration-test.json"


@dataclass(frozen=True, slots=True)
class _Prepared:
    checkout: IntegrationTestCheckout
    scratch: TaskTestScratch
    root: Path
    sha256: str

    @property
    def path(self) -> Path:
        return self.root / "context" / _PREPARATION


def _binding(
    integration_id: str,
    attempt: int,
    candidate: IntegrationCandidate,
    rules: FrozenProjectRules,
    policy: TaskTestPolicy,
    database: _Database,
) -> dict[str, Any]:
    return {
        "version": 1,
        "integration_id": integration_id,
        "test_attempt": attempt,
        "candidate": task_execution_value(candidate),
        "repository": rules.repository,
        "rules_baseline": rules.baseline_commit,
        "rules_blob": rules.blob_oid,
        "rules_hash": rules.rules_hash,
        "commands": task_execution_value(rules.rules.commands),
        "policy": task_execution_value(policy),
        "database": str(database.path),
        "database_identity": list(database.identity),
        "database_parent_identity": list(database.parent_identity),
    }


def _prepare(
    root: Path,
    lease: ExecutorLease,
    database: _Database,
    integration_id: str,
    attempt: int,
    candidate: IntegrationCandidate,
    rules: FrozenProjectRules,
    policy: TaskTestPolicy,
) -> _Prepared:
    _check_lease(lease)
    if rules.repository != str(lease.repository) or not rules.rules.commands:
        raise ValueError("integration requires a frozen nonempty project command suite")
    root = _new_root(root, database, lease)
    check_test_runtime_paths(policy, root, database.path, lease)
    root.mkdir(mode=0o700)
    for name in ("context", "logs"):
        (root / name).mkdir(mode=0o700)
    scratch = create_test_scratch(root)
    object_format = (
        _git(lease.repository, "rev-parse", "--show-object-format").decode().strip()
    )
    layout = create_test_checkout_layout(
        lease,
        candidate=candidate.commit,
        expected_tree=candidate.tree,
        object_format=object_format,
        root=root / "checkout",
        owner=uuid4().hex,
    )
    checkout = IntegrationTestCheckout(
        layout=layout, database_path=str(database.path), commands=rules.rules.commands
    )
    payload = {
        **_binding(integration_id, attempt, candidate, rules, policy, database),
        "root_identity": _identity(_directory(root)),
        "context_identity": _identity(_directory(root / "context")),
        "logs_identity": _identity(_directory(root / "logs")),
        "checkout": task_execution_value(layout),
        "scratch": task_execution_value(scratch),
    }
    path = root / "context" / _PREPARATION
    metadata = _write_control(path, _json_bytes(payload))
    (root / "context").chmod(0o500)
    _sync_directory(root)
    database.check()
    return _Prepared(checkout, scratch, root, metadata["sha256"])


def _prepared_from_manifest(
    root: Path,
    result: IntegrationTestResult,
    binding: dict[str, Any],
    rules: FrozenProjectRules,
    candidate: IntegrationCandidate,
    lease: ExecutorLease,
) -> _Prepared:
    path = root / "context" / _PREPARATION
    if result.preparation_path != str(path):
        raise ValueError("integration test preparation path changed")
    metadata, raw = _read(path, _MANIFEST_BYTES)
    if metadata["sha256"] != result.preparation_sha256:
        raise ValueError("integration test preparation digest changed")
    value = _json(raw)
    if any(value.get(key) != item for key, item in binding.items()):
        raise ValueError("integration test preparation belongs to another source")
    _directory(root, identity=tuple(value["root_identity"]))
    _directory(root / "context", identity=tuple(value["context_identity"]), mode=0o500)
    _directory(root / "logs", identity=tuple(value["logs_identity"]))
    layout_data = value["checkout"]
    layout = TestCheckoutLayout(
        **{
            **layout_data,
            "root_identity": tuple(layout_data["root_identity"]),
            "git_identity": tuple(layout_data["git_identity"]),
            "objects": tuple(tuple(item) for item in layout_data["objects"]),
            "controls": tuple(
                WorkspaceControl(
                    item["path"],
                    tuple(item["identity"]) if item["identity"] is not None else None,
                    bytes.fromhex(item["content"]["hex"])
                    if item["content"] is not None
                    else None,
                )
                for item in layout_data["controls"]
            ),
        }
    )
    if layout.root != str(root / "checkout") or (layout.commit, layout.tree) != (
        candidate.commit,
        candidate.tree,
    ):
        raise ValueError("integration test checkout belongs to another candidate")
    inspect_task_test_checkout_controls(lease, layout)
    scratch_data = value["scratch"]
    scratch = TaskTestScratch(
        path=scratch_data["path"],
        identity=tuple(scratch_data["identity"]),
        home_identity=tuple(scratch_data["home_identity"]),
        tmp_identity=tuple(scratch_data["tmp_identity"]),
        cache_identity=tuple(scratch_data["cache_identity"]),
    )
    checkout = IntegrationTestCheckout(
        layout=layout,
        database_path=binding["database"],
        commands=rules.rules.commands,
    )
    return _Prepared(checkout, scratch, root, metadata["sha256"])


async def _run_adapter(
    adapter: TaskTestRuntimeAdapter,
    command: NamedCommand,
    sequence: int,
    stop: asyncio.Event,
) -> TestCommandResult:
    operation = asyncio.create_task(adapter.run(command, sequence=sequence))
    monitor = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait(
            (operation, monitor), return_when=asyncio.FIRST_COMPLETED
        )
        if monitor in done and not operation.done():
            await adapter.cancel()
        return await operation
    finally:
        monitor.cancel()
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, monitor, return_exceptions=True)


def _command_binding(
    prepared: _Prepared,
    integration_id: str,
    attempt: int,
    sequence: int,
    command: NamedCommand,
    policy: TaskTestPolicy,
) -> dict[str, Any]:
    return {
        "version": 1,
        "integration_id": integration_id,
        "test_attempt": attempt,
        "candidate": prepared.checkout.layout.commit,
        "preparation_sha256": prepared.sha256,
        "sequence": sequence,
        "command": task_execution_value(command),
        "stop_scope": policy.required_stop_scope,
        "output_scope": policy.output_scope,
    }


def _archive_command(
    prepared: _Prepared,
    lease: ExecutorLease,
    policy: TaskTestPolicy,
    integration_id: str,
    attempt: int,
    sequence: int,
    command: NamedCommand,
    runtime: TestCommandResult,
    error: str | None,
) -> IntegrationTestCommand:
    stopped = command_process_stopped(runtime)
    if error is None and runtime.problems:
        error = runtime.problems[0].detail
    directory = prepared.root / "logs" / f"command-{sequence}"
    path, digest = None, None
    qualified = False
    try:
        actual, receipt = _result(task_execution_value(runtime))
        if actual["sequence"] != sequence:
            raise ValueError("integration runtime result belongs to another command")
        logs: list[dict[str, Any]] = []
        try:
            logs = _logs(directory, policy, receipt)
            if stopped is True:
                inspect_task_test_checkout_controls(lease, prepared.checkout.layout)
                if not _audit_matches(
                    actual, prepared.checkout, prepared.scratch, policy, receipt
                ):
                    raise ValueError("integration command permission audit differs")
        except _COMMAND_FAILURES as failure:
            if error is None:
                error = str(failure)
        qualified = error is None and _runtime_qualified(actual, receipt)
        if not qualified and error is None:
            error = "Integration command evidence is incomplete"
        manifest = {
            **_command_binding(
                prepared, integration_id, attempt, sequence, command, policy
            ),
            "actual_result": actual,
            "process_stopped": stopped,
            "qualified": qualified,
            "validation_error": error,
            "logs": logs,
        }
        destination = directory / "execution.json"
        metadata = _write_control(destination, _json_bytes(manifest))
        _sync_directory(directory)
        path, digest = str(destination), metadata["sha256"]
    except _COMMAND_FAILURES as failure:
        qualified = False
        if error is None:
            error = str(failure)
    return IntegrationTestCommand(
        sequence=sequence,
        command=command,
        request_attempted=runtime.request_attempted,
        request_settled=runtime.request_settled,
        reported_exit_code=runtime.reported_exit_code,
        exit_code=runtime.exit_code,
        process_stopped=stopped,
        manifest_path=path,
        manifest_sha256=digest,
        qualified=qualified,
        reason=error,
    )


async def execute_integration_tests(
    *,
    integration_id: str,
    attempt: int,
    lease: ExecutorLease,
    database: Path,
    root: Path,
    candidate: IntegrationCandidate,
    rules: FrozenProjectRules,
    policy: TaskTestPolicy,
    stop: asyncio.Event,
) -> IntegrationTestResult:
    """Run the frozen suite in order with one physical checkout and scratch."""
    start_actor("integration", integration_id, "tests", None, attempt)
    prepared = None
    results: list[IntegrationTestCommand] = []
    status: IntegrationTestStatus = "succeeded"
    reason = None
    try:
        if stop.is_set():
            status, reason = "cancelled", "Stopped before test preparation"
        else:
            prepared = _prepare(
                root,
                lease,
                _database(database),
                integration_id,
                attempt,
                candidate,
                rules,
                policy,
            )
            for sequence, command in enumerate(prepared.checkout.commands, 1):
                if stop.is_set():
                    status, reason = "cancelled", "Stopped between test commands"
                    break
                adapter = TaskTestRuntimeAdapter(
                    checkout=prepared.checkout,
                    scratch=prepared.scratch,
                    policy=policy,
                    log_dir=prepared.root / "logs" / f"command-{sequence}",
                )
                error = None
                try:
                    observe("test_command_started")
                    runtime = await _run_adapter(adapter, command, sequence, stop)
                except _COMMAND_FAILURES as failure:
                    error = str(failure)
                    runtime = adapter.snapshot()
                saved = _archive_command(
                    prepared,
                    lease,
                    policy,
                    integration_id,
                    attempt,
                    sequence,
                    command,
                    runtime,
                    error,
                )
                results.append(saved)
                if runtime.cancel_accepted:
                    status, reason = "cancelled", f"Stopped during {command.name}"
                    break
                if not saved.qualified or saved.exit_code != 0:
                    status = "failed"
                    reason = saved.reason or f"{command.name} exited {saved.exit_code}"
                    break
    except _COMMAND_FAILURES as error:
        status, reason = "failed", str(error)
    observe("suite_settled", state=status)
    return IntegrationTestResult(
        status=status,
        process_stopped=results[-1].process_stopped if results else True,
        commands=tuple(results),
        reason=reason,
        preparation_path=str(prepared.path) if prepared else None,
        preparation_sha256=prepared.sha256 if prepared else None,
    )


def inspect_integration_test_result(
    result: IntegrationTestResult,
    *,
    integration_id: str,
    attempt: int,
    lease: ExecutorLease,
    database: Path,
    root: Path,
    candidate: IntegrationCandidate,
    rules: FrozenProjectRules,
    policy: TaskTestPolicy,
) -> tuple[ExecutedCommand, ...]:
    """Reopen every bound command archive and verify its actual binary logs."""
    from .review_inputs import MAX_REVIEW_INPUT_BYTES

    _check_lease(lease)
    if (
        result.status != "succeeded"
        or result.process_stopped is not True
        or result.reason is not None
        or tuple(item.command for item in result.commands) != rules.rules.commands
        or not result.commands
    ):
        raise ValueError(
            "integration tests did not complete the frozen suite successfully"
        )
    db = _database(database)
    prepared = _prepared_from_manifest(
        root,
        result,
        _binding(integration_id, attempt, candidate, rules, policy, db),
        rules,
        candidate,
        lease,
    )
    executed = []
    raw_bytes = 0
    for sequence, saved in enumerate(result.commands, 1):
        directory = prepared.root / "logs" / f"command-{sequence}"
        path = directory / "execution.json"
        if (
            saved.sequence != sequence
            or saved.manifest_path != str(path)
            or saved.exit_code != 0
            or saved.reported_exit_code != 0
            or saved.process_stopped is not True
            or not saved.qualified
            or saved.reason is not None
        ):
            raise ValueError("integration test database did not record a clean command")
        metadata, raw = _read(path, _MANIFEST_BYTES)
        if metadata["sha256"] != saved.manifest_sha256:
            raise ValueError("integration command archive digest changed")
        manifest = _json(raw)
        actual, receipt = _result(manifest["actual_result"])
        raw_bytes += actual["stdout_received_bytes"] + actual["stderr_received_bytes"]
        if raw_bytes * 2 > MAX_REVIEW_INPUT_BYTES:
            raise ValueError(
                "executed integration test output exceeds the review budget"
            )
        outputs: dict[str, bytes] = {}
        logs = _logs(directory, policy, receipt, outputs=outputs)
        expected = {
            **_command_binding(
                prepared, integration_id, attempt, sequence, saved.command, policy
            ),
            "actual_result": actual,
            "process_stopped": True,
            "qualified": True,
            "validation_error": None,
            "logs": task_execution_value(logs),
        }
        if (
            manifest != expected
            or actual["sequence"] != sequence
            or actual["exit_code"] != 0
            or _stopped(actual, receipt) is not True
            or not _runtime_qualified(actual, receipt)
            or not _audit_matches(
                actual, prepared.checkout, prepared.scratch, policy, receipt
            )
        ):
            raise ValueError(
                "integration command evidence differs from the recorded execution"
            )
        executed.append(
            ExecutedCommand(
                manifest=manifest,
                stdout=ExecutedLog(content=outputs["stdout.bin"]),
                stderr=ExecutedLog(content=outputs["stderr.bin"]),
            )
        )
    _check_lease(lease)
    db.check()
    return tuple(executed)
