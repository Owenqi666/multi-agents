"""Synthetic implementation ownership and real files; never an actual model CLI."""

import asyncio
import hashlib
import json
import os
import stat
import threading
from copy import copy, deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_planning_evidence as planning_evidence
import test_task_context as contexts

from multi_agent.adapters.base import (
    EnvironmentInfo,
    SessionHandle,
    TurnHandle,
    TurnResult,
)
from multi_agent.adapters.implementation_runtime import ImplementationRuntimeAdapter
from multi_agent.adapters.ownership import (
    DirectoryOwnership,
    IsolationOwnership,
    ProcessOwnership,
)
from multi_agent.adapters.probe.recording import Recorder, Redactor
from multi_agent.adapters.runtime import RuntimeAdapter
from multi_agent.cli import task_evidence as boundary
from multi_agent.cli.task_evidence import (
    bind_task_execution,
    inspect_task_execution,
    seal_task_execution,
)
from multi_agent.cli.workspaces import executor_lock, inspect_workspace
from multi_agent.core import Disposition
from multi_agent.core.events import (
    CancelRequested,
    Completed,
    Error,
    ProcessExited,
    SessionStarted,
    thaw_json,
)
from multi_agent.core.task_events import (
    TaskEnvelope,
    TaskEnvironmentObserved,
    TaskEvidenceRejected,
    TaskReconciled,
    TaskScopeBlocked,
)
from multi_agent.core.task_execution import (
    task_execution_hash,
    task_execution_json,
    task_execution_value,
)
from multi_agent.core.task_state import TaskPhase
from multi_agent.store.planning_store import PlanningStore

# Register both dependencies so the original no-runtime-process guard remains active.
project = contexts.project
bound = contexts.bound


class SyntheticImplementationRuntime(ImplementationRuntimeAdapter):
    """An empty real adapter ledger until bind; invented processes are labelled."""

    def __init__(self, launch):
        prepared = launch.prepared
        super().__init__(
            prepared.policy.provider,
            workspace=prepared.workspace,
            executable=prepared.policy.executable,
            timeout=prepared.policy.timeout,
            grace=prepared.policy.grace,
            log_dir=launch.log_dir,
            output_schema=thaw_json(prepared.output_schema),
        )
        self.turn = TurnHandle("synthetic-turn", SessionHandle("synthetic-session"))
        self.environment = EnvironmentInfo(
            "synthetic-no-cli",
            True,
            ("reported-capability",),
            {"reported-capability": ("medium",)},
        )
        self.actual_result = TurnResult()
        self.recorders = []
        self.result_calls = []
        self.before_result = None
        self.transform_receipt = lambda value: value
        self.process = None

    def populate(
        self,
        *,
        stopped=True,
        closed=True,
        eof=True,
        processes=True,
        operations=("environment", "turn"),
        extra_log_bytes=0,
    ):
        if processes:
            self.process = planning_evidence.SyntheticResource(
                ProcessOwnership(
                    "",
                    "synthetic-not-a-process",
                    987654321,
                    -15,
                    True,
                    stopped,
                    True,
                    True,
                    eof,
                    eof,
                )
            )
            ticket = self._ownership.acquire("process")
            ticket.publish(self.process)
            ticket.finish()
        for kind in operations:
            operation = self._ownership.begin_operation(kind)
            recorder = Recorder(
                self.log_dir / f"{kind}.jsonl",
                Redactor(),
                acquisition=self._ownership.acquire("log"),
            )
            self.recorders.append(recorder)
            recorder.emit("synthetic_task_evidence", operation=kind, actual_cli=False)
            if extra_log_bytes and kind == "turn":
                for _ in range(extra_log_bytes // (1024 * 1024) + 1):
                    recorder.emit("synthetic_chunk", data="x" * (1024 * 1024))
            if closed:
                recorder.close()
            self._ownership.finish_operation(operation)

    async def result(self, turn):
        assert turn is self.turn
        self.result_calls.append(turn)
        if self.before_result is not None:
            await self.before_result()
        return self.actual_result

    def ownership_receipt(self):
        return self.transform_receipt(super().ownership_receipt())

    async def check_environment(self):
        pytest.fail("evidence must not run preflight")

    async def start_session(self, config):
        pytest.fail("evidence must not start a session")

    async def submit_turn(self, session, text):
        pytest.fail("evidence must not submit a turn")

    def events(self, turn):
        pytest.fail("evidence must not consume runtime events")

    async def cancel(self, turn, *, on_accepted=None):
        pytest.fail("evidence must not cancel or clean up runtime ownership")


@pytest.fixture
def live(bound):
    with contexts.launch(bound) as launch:
        yield launch


def emit(launch, event, *, attempt=None):
    number = launch.attempt if attempt is None else attempt
    return launch.store.apply_task(
        "implementation", TaskEnvelope(number, event, "synthetic-observed")
    )


def make(
    live,
    *,
    observe=True,
    record=True,
    outcome="completed",
    reported_model=None,
    stopped=True,
    closed=True,
    eof=True,
    processes=True,
    operations=("environment", "turn"),
    extra_log_bytes=0,
):
    adapter = SyntheticImplementationRuntime(live)
    empty = adapter.ownership_receipt()
    assert empty.owner_id and not any(
        (
            empty.operations,
            empty.acquisitions,
            empty.processes,
            empty.isolations,
            empty.logs,
            empty.directories,
        )
    )
    binding = bind_task_execution(live, "implementation", adapter)
    report = {
        "task_hash": live.store.load_task_input("implementation").card.task_hash,
        "outcome": outcome,
        "summary": "Synthetic model report",
        "required_paths": [] if outcome == "completed" else ["shared/[id]/api.py"],
    }
    terminal = Completed(
        report, reported_model=reported_model, raw_type="synthetic.completed"
    )
    process_exit = ProcessExited(-15, raw_type="synthetic.model.exit")
    adapter.actual_result = TurnResult(terminal, process_exit)
    adapter.populate(
        stopped=stopped,
        closed=closed,
        eof=eof,
        processes=processes,
        operations=operations,
        extra_log_bytes=extra_log_bytes,
    )
    if observe:
        assert (
            emit(
                live, TaskEnvironmentObserved(task_execution_value(adapter.environment))
            )
            == Disposition.APPLIED
        )
    if record:
        assert emit(live, terminal) == Disposition.APPLIED
        assert emit(live, process_exit) == Disposition.APPLIED
    return SimpleNamespace(
        launch=live,
        adapter=adapter,
        binding=binding,
        terminal=terminal,
        process_exit=process_exit,
        report=report,
    )


@pytest.fixture
def execution(live):
    value = make(live)
    try:
        yield value
    finally:
        for recorder in value.adapter.recorders:
            recorder.close()


def seal(execution, **overrides):
    values = {
        "turn": execution.adapter.turn,
        "environment": execution.adapter.environment,
    }
    values.update(overrides)
    return asyncio.run(seal_task_execution(execution.binding, **values))


def reconcile(
    execution, manifest, *, path=None, digest=None, stopped=None, final_digest=None
):
    return emit(
        execution.launch,
        TaskReconciled(
            manifest.final_digest if final_digest is None else final_digest,
            manifest.receipt.processes_stopped if stopped is None else stopped,
            "Synthetic ownership; real file inspection",
            manifest.path if path is None else path,
            manifest.sha256 if digest is None else digest,
        ),
    )


def inspect(execution):
    launch = execution.launch
    return inspect_task_execution(
        launch.store, "implementation", launch.attempt, launch.lease
    )


def payload(path):
    return json.loads(Path(path).read_bytes())


def test_task_execution_requires_fresh_live_binding(live):
    adapter = SyntheticImplementationRuntime(live)
    original = adapter.ownership_receipt()
    binding = bind_task_execution(live, "implementation", adapter)
    assert binding.launch is live and binding.adapter is adapter
    assert adapter.ownership_receipt() == original
    with pytest.raises(ValueError):
        bind_task_execution(live, "implementation", adapter)
    with pytest.raises(ValueError):
        live.claim()
    assert list(live.log_dir.iterdir()) == []


@pytest.mark.parametrize(
    "resource",
    [
        "operation",
        "finished_operation",
        "acquisition",
        "failed_acquisition",
        "process",
        "isolation",
        "directory",
        "log",
    ],
)
def test_task_execution_rejects_preexisting_ownership(live, resource):
    adapter = SyntheticImplementationRuntime(live)
    if resource in {"operation", "finished_operation"}:
        operation = adapter._ownership.begin_operation("environment")
        if resource == "finished_operation":
            adapter._ownership.finish_operation(operation)
    elif resource in {"acquisition", "failed_acquisition"}:
        ticket = adapter._ownership.acquire("process")
        if resource == "failed_acquisition":
            ticket.not_created()
            ticket.finish()
    elif resource == "log":
        recorder = Recorder(
            live.log_dir / "already-created.jsonl",
            Redactor(),
            acquisition=adapter._ownership.acquire("log"),
        )
        recorder.close()
    else:
        records = {
            "process": ProcessOwnership(
                "", "synthetic", 987654321, 0, True, True, True, True, True, True
            ),
            "isolation": IsolationOwnership("", "/synthetic", 0, 0, 0, 0, True),
            "directory": DirectoryOwnership("", "/synthetic", True),
        }
        ticket = adapter._ownership.acquire(resource)
        ticket.publish(planning_evidence.SyntheticResource(records[resource]))
        ticket.finish()
    with pytest.raises(ValueError):
        bind_task_execution(live, "implementation", adapter)
    # Validation precedes consumption of the fresh launch capability.
    assert live.claim() == live.prepared


@pytest.mark.parametrize(
    "changed",
    [
        "readonly",
        "run_id",
        "provider",
        "workspace",
        "schema",
        "log_dir",
        "executable",
        "timeout",
        "grace",
        "bool_timeout",
        "nan_grace",
        "deadline",
    ],
)
def test_task_execution_public_configuration_is_frozen_at_bind(live, changed):
    adapter = SyntheticImplementationRuntime(live)
    run_id = "implementation"
    if changed == "readonly":
        adapter = RuntimeAdapter(
            adapter.provider, executable=adapter.executable, log_dir=adapter.log_dir
        )
    elif changed == "run_id":
        run_id = "other"
    else:
        values = {
            "provider": "codex",
            "workspace": replace(adapter.workspace, owner_token="other"),
            "schema": {},
            "log_dir": live.log_dir.with_name("wrong-logs"),
            "executable": "/missing-executable",
            "timeout": adapter.timeout + 1,
            "grace": adapter.grace + 1,
            "bool_timeout": True,
            "nan_grace": float("nan"),
            "deadline": float("inf"),
        }
        field = {
            "schema": "output_schema",
            "bool_timeout": "timeout",
            "nan_grace": "grace",
            "deadline": "deadline_at",
        }.get(changed, changed)
        setattr(adapter, field, values[changed])
    with pytest.raises((ValueError, TypeError, OSError)):
        bind_task_execution(live, run_id, adapter)


def test_task_execution_binding_cannot_be_reconstructed_after_scope(bound):
    with contexts.launch(bound) as launch:
        adapter = SyntheticImplementationRuntime(launch)
    with pytest.raises(ValueError):
        bind_task_execution(launch, "implementation", adapter)


def test_task_execution_preserves_completed_with_nonzero_exit(execution):
    launch = execution.launch
    before = launch.store.load_task_history("implementation")
    context_hash = hashlib.sha256(
        (Path(launch.prepared.context.path) / ".task-context.json").read_bytes()
    ).hexdigest()
    manifest = seal(execution)
    assert manifest.qualified and manifest.delivery_eligible
    assert manifest.actual_result == execution.adapter.actual_result
    assert manifest.actual_result.process_exit.exit_code == -15
    assert manifest.environment == execution.adapter.environment
    assert manifest.receipt == execution.adapter.ownership_receipt()
    assert manifest.report.task_hash == execution.report["task_hash"]
    assert manifest.report.required_paths == () and manifest.validation_error is None
    assert launch.store.load_task_history("implementation") == before
    assert not launch.store.load_task("implementation").delivery_ready
    assert (
        hashlib.sha256(
            (Path(launch.prepared.context.path) / ".task-context.json").read_bytes()
        ).hexdigest()
        == context_hash
    )
    assert manifest.path == str(launch.log_dir / "execution.json")
    assert (
        manifest.sha256 == hashlib.sha256(Path(manifest.path).read_bytes()).hexdigest()
    )
    assert stat.S_IMODE(Path(manifest.path).stat().st_mode) == 0o400
    assert stat.S_IMODE((launch.log_dir / ".seal-attempt.json").stat().st_mode) == 0o400
    reconcile(execution, manifest)
    assert launch.store.load_task("implementation").delivery_ready
    assert inspect(execution) == manifest


@pytest.mark.parametrize(
    "order",
    ["completed_then_exit", "exit_then_completed", "evidence_rejected", "late_cancel"],
)
def test_task_execution_cancelled_completed_never_qualifies(live, order):
    execution = make(live, record=False)
    if order.startswith(("completed_", "exit_")):
        assert emit(live, CancelRequested()) == Disposition.APPLIED
        if order == "completed_then_exit":
            assert emit(live, execution.terminal) == Disposition.APPLIED
            assert emit(live, execution.process_exit) == Disposition.APPLIED
        else:
            assert emit(live, execution.process_exit) == Disposition.APPLIED
            assert emit(live, execution.terminal) == Disposition.HISTORY_ONLY
    else:
        assert emit(live, execution.terminal) == Disposition.APPLIED
        event = (
            TaskEvidenceRejected("Host rejected evidence")
            if order == "evidence_rejected"
            else CancelRequested()
        )
        assert emit(live, event) == (
            Disposition.APPLIED
            if order == "evidence_rejected"
            else Disposition.HISTORY_ONLY
        )
        assert emit(live, execution.process_exit) == Disposition.APPLIED
    before = live.store.load_task("implementation")
    manifest = seal(execution)
    assert manifest.actual_result == execution.adapter.actual_result
    assert manifest.actual_result.terminal.result == execution.terminal.result
    assert manifest.qualified == (order == "late_cancel")
    assert manifest.delivery_eligible == (order == "late_cancel")
    assert live.store.load_task("implementation") == before
    if order != "late_cancel":
        assert not manifest.qualified
        with pytest.raises(ValueError):
            inspect(execution)
    else:
        reconcile(execution, manifest)
        assert inspect(execution).qualified


@pytest.mark.parametrize("changed", ["none", "mismatch", "unobserved"])
def test_task_execution_environment_and_reported_unknowns_are_exact(live, changed):
    execution = make(live, observe=changed != "unobserved")
    environment = execution.adapter.environment
    if changed == "none":
        environment = None
    elif changed == "mismatch":
        environment = replace(environment, cli_version="different-actual-version")
    manifest = seal(execution, environment=environment)
    assert not manifest.qualified and not manifest.delivery_eligible
    assert manifest.environment == environment
    assert manifest.actual_result.terminal.reported_model is None
    observations = payload(manifest.path)["runtime_observations"]
    assert (
        observations["reported_model"] is None
        and observations["reported_effort"] is None
    )
    assert (
        observations["reported_model_source"] is None
        and observations["reported_effort_source"] is None
    )


def test_task_execution_runtime_observations_do_not_replace_actual_result(live):
    execution = make(live, record=False)
    reported = SessionStarted(
        "actual-session", "actual-model", "actual-effort", raw_type="synthetic.session"
    )
    emit(live, reported)
    emit(live, execution.terminal)
    emit(live, execution.process_exit)
    manifest = seal(execution)
    assert manifest.qualified
    assert manifest.actual_result.terminal.reported_model is None
    observations = payload(manifest.path)["runtime_observations"]
    assert observations["reported_model"] == "actual-model"
    assert observations["reported_effort"] == "actual-effort"
    assert observations["reported_model_source"] == {
        "event_type": "session_started",
        "raw_type": "synthetic.session",
    }
    assert (
        observations["reported_effort_source"] == observations["reported_model_source"]
    )


def test_task_execution_preflight_only_never_invents_model_result(live):
    execution = make(live, record=False, operations=("environment",))
    manifest = seal(execution, turn=None)
    assert execution.adapter.result_calls == []
    assert manifest.actual_result == TurnResult()
    assert not manifest.qualified and not manifest.delivery_eligible
    assert (
        live.store.load_task("implementation").current_attempt.runtime.exit_code is None
    )


@pytest.mark.parametrize(
    "gate",
    [
        "no_operations",
        "missing_turn",
        "extra_operation",
        "active_operation",
        "pending",
        "unknown",
        "empty_processes",
        "group_unknown",
        "eof_incomplete",
        "directory_retained",
        "isolation_retained",
    ],
)
def test_task_execution_keeps_stop_digest_and_log_qualification_independent(
    live, monkeypatch, gate
):
    execution = make(
        live,
        operations=()
        if gate == "no_operations"
        else ("environment",)
        if gate == "missing_turn"
        else ("environment", "turn"),
        processes=gate != "empty_processes",
        stopped=gate != "group_unknown",
        eof=gate != "eof_incomplete",
    )
    ledger = execution.adapter._ownership
    if gate in {"extra_operation", "active_operation"}:
        operation = ledger.begin_operation("extra")
        if gate == "extra_operation":
            ledger.finish_operation(operation)
    elif gate in {"pending", "unknown"}:
        ticket = ledger.acquire("process")
        if gate == "unknown":
            ticket.finish()
    elif gate in {"directory_retained", "isolation_retained"}:
        kind = "directory" if gate == "directory_retained" else "isolation"
        record = (
            DirectoryOwnership("", "/synthetic-retained", False)
            if kind == "directory"
            else IsolationOwnership("", "/synthetic-retained", 0, 0, 0, 0, False)
        )
        ticket = ledger.acquire(kind)
        ticket.publish(planning_evidence.SyntheticResource(record))
        ticket.finish()
    actual_receipt = execution.adapter.ownership_receipt()
    if not actual_receipt.processes_stopped:

        def forbidden(*args, **kwargs):
            pytest.fail("unknown-stop branch scanned workspace or full context")

        monkeypatch.setattr(type(live), "check", forbidden)
        monkeypatch.setattr(boundary, "inspect_task_context", forbidden)
        monkeypatch.setattr(boundary, "inspect_workspace", forbidden)
    manifest = seal(execution)
    assert not manifest.qualified and not manifest.delivery_eligible
    assert manifest.receipt == actual_receipt
    if not actual_receipt.processes_stopped:
        assert manifest.final_digest is None
    else:
        assert (
            manifest.final_digest
            == inspect_workspace(live.lease, live.prepared.workspace).digest
        )


def test_task_execution_unsealed_log_is_never_opened_for_verification(
    live, monkeypatch
):
    execution = make(live, closed=False)
    original = boundary._read_owned_file

    def no_unsealed_read(directory, logs, name, **kwargs):
        assert not name.endswith(".jsonl"), "unsealed raw log was opened for inspection"
        return original(directory, logs, name, **kwargs)

    monkeypatch.setattr(boundary, "_read_owned_file", no_unsealed_read)
    try:
        manifest = seal(execution)
        assert manifest.receipt.processes_stopped and not manifest.receipt.logs_sealed
        assert not manifest.qualified
        assert all(
            not log.closed
            and not log.sealed
            and log.sha256 is None
            and log.byte_count is None
            for log in manifest.receipt.logs
        )
    finally:
        for recorder in execution.adapter.recorders:
            recorder.close()
    with pytest.raises(ValueError):
        seal(execution)


@pytest.mark.parametrize("failure", ["context", "workspace"])
def test_task_execution_stopped_scan_failure_retains_true_receipt(
    execution, monkeypatch, failure
):
    problem = OSError("synthetic scan boundary failure")

    def failed(*args, **kwargs):
        raise problem

    monkeypatch.setattr(
        boundary,
        "inspect_task_context" if failure == "context" else "inspect_workspace",
        failed,
    )
    before = execution.launch.store.load_task_history("implementation")
    with pytest.raises(OSError) as caught:
        seal(execution)
    assert caught.value is problem
    assert execution.adapter.ownership_receipt().processes_stopped
    assert execution.launch.store.load_task_history("implementation") == before
    assert (execution.launch.log_dir / ".seal-attempt.json").is_file()
    assert not (execution.launch.log_dir / "execution.json").exists()
    with pytest.raises(ValueError):
        seal(execution)


def test_task_execution_final_digest_is_observed_not_initial(execution):
    launch = execution.launch
    (Path(launch.prepared.workspace.root) / "src/code.py").write_bytes(
        b"actual changed fixture\n"
    )
    expected = inspect_workspace(launch.lease, launch.prepared.workspace).digest
    assert expected != launch.prepared.initial_workspace_digest
    manifest = seal(execution)
    assert manifest.qualified and manifest.final_digest == expected
    reconcile(execution, manifest)
    assert inspect(execution).final_digest == expected
    (Path(launch.prepared.workspace.root) / "src/code.py").write_bytes(
        b"changed after sealing\n"
    )
    with pytest.raises(ValueError):
        inspect(execution)


def test_task_execution_scope_blocked_is_report_not_candidate(live):
    execution = make(live, outcome="scope_blocked")
    before = live.store.load_task("implementation").current_attempt.runtime
    manifest = seal(execution)
    assert manifest.qualified and not manifest.delivery_eligible
    reconcile(execution, manifest)
    handback = TaskScopeBlocked(manifest.report.summary, manifest.report.required_paths)
    assert emit(live, handback) == Disposition.APPLIED
    state = live.store.load_task("implementation")
    assert state.phase == TaskPhase.PLANNING_REQUIRED
    assert state.current_attempt.runtime == before
    assert state.scope_block == handback
    assert inspect(execution).qualified and not inspect(execution).delivery_eligible


@pytest.mark.parametrize("wrong", ["reason", "paths", "completed"])
def test_task_execution_archive_rejects_invented_scope_handback(live, wrong):
    execution = make(
        live, outcome="completed" if wrong == "completed" else "scope_blocked"
    )
    manifest = seal(execution)
    reconcile(execution, manifest)
    report = manifest.report
    reason = "invented" if wrong in {"reason", "completed"} else report.summary
    paths = (
        ("other/path.py",) if wrong in {"paths", "completed"} else report.required_paths
    )
    assert emit(live, TaskScopeBlocked(reason, paths)) == Disposition.APPLIED
    with pytest.raises(ValueError):
        inspect(execution)


@pytest.mark.parametrize(
    "bad", ["wrong_hash", "extra", "completed_paths", "invalid_scope"]
)
def test_task_execution_invalid_artifact_stays_unqualified(live, bad):
    execution = make(live, record=False)
    report = dict(execution.report)
    if bad == "wrong_hash":
        report["task_hash"] = "0" * 64
    elif bad == "extra":
        report["invented_test_pass"] = True
    elif bad == "completed_paths":
        report["required_paths"] = ["src/code.py"]
    else:
        report.update(outcome="scope_blocked", required_paths=["../escape"])
    terminal = replace(execution.terminal, result=report)
    execution.adapter.actual_result = TurnResult(terminal, execution.process_exit)
    emit(live, terminal)
    emit(live, execution.process_exit)
    manifest = seal(execution)
    assert not manifest.qualified and not manifest.delivery_eligible
    assert manifest.report is None and manifest.validation_error
    assert manifest.actual_result.terminal.result == terminal.result


@pytest.mark.parametrize(
    "duplicate", [copy, replace, deepcopy], ids=["copy", "replace", "deepcopy"]
)
def test_task_execution_seal_is_exclusive_across_binding_copies(execution, duplicate):
    other = duplicate(execution.binding)
    result = asyncio.run(
        seal_task_execution(
            other,
            turn=execution.adapter.turn,
            environment=execution.adapter.environment,
        )
    )
    assert result.qualified
    with pytest.raises(ValueError):
        seal(execution)
    assert len(execution.adapter.result_calls) == 1
    assert payload(result.path)["qualified"] is True


@pytest.mark.parametrize(
    "changed,when",
    [
        (changed, when)
        for changed in (
            "provider",
            "executable",
            "workspace",
            "schema",
            "log_dir",
            "timeout",
            "grace",
            "deadline",
            "owner",
            "adapter",
        )
        for when in ("before", "during_result")
        if (changed, when) != ("adapter", "during_result")
    ],
)
def test_task_execution_public_configuration_is_frozen(execution, changed, when):
    adapter = execution.adapter
    launch = execution.launch

    def mutate():
        if changed == "owner":
            adapter._ownership.owner_id = "different-owner"
        elif changed == "adapter":
            replacement = SyntheticImplementationRuntime(launch)
            execution.binding = replace(execution.binding, adapter=replacement)
        else:
            fields = {
                "provider": "codex",
                "executable": "/different-executable",
                "workspace": replace(adapter.workspace, owner_token="different"),
                "schema": {},
                "log_dir": launch.log_dir.with_name("different"),
                "timeout": adapter.timeout + 1,
                "grace": adapter.grace + 1,
                "deadline": 12345.0,
            }
            setattr(
                adapter,
                {"schema": "output_schema", "deadline": "deadline_at"}.get(
                    changed, changed
                ),
                fields[changed],
            )

    if when == "before":
        mutate()
    else:

        async def changing_result():
            mutate()

        adapter.before_result = changing_result
    before = launch.store.load_task_history("implementation")
    with pytest.raises((ValueError, OSError)):
        seal(execution)
    assert launch.store.load_task_history("implementation") == before
    with pytest.raises(ValueError):
        seal(execution)


@pytest.mark.parametrize("changed", ["missing", "replacement", "mode"])
def test_task_execution_uses_t43_logs_without_recreating_or_adopting(live, changed):
    execution = make(live)
    path = live.log_dir
    if changed == "missing":
        path.rename(path.with_name("retained-logs"))
    elif changed == "replacement":
        path.rename(path.with_name("retained-logs"))
        path.mkdir(mode=0o700)
    else:
        path.chmod(0o755)
    with pytest.raises((ValueError, OSError)):
        seal(execution)
    if changed == "missing":
        assert not path.exists()
    else:
        assert not (path / "execution.json").exists()


def test_task_execution_expired_binding_cannot_seal(bound):
    with contexts.launch(bound) as launch:
        execution = make(launch)
    with pytest.raises(ValueError):
        seal(execution)
    assert execution.adapter.result_calls == []


def test_task_execution_inspection_is_archive_only(bound):
    with contexts.launch(bound) as launch:
        execution = make(launch)
        manifest = seal(execution)
        with pytest.raises(ValueError):
            inspect(execution)
        reconcile(execution, manifest)
    with PlanningStore(bound.database) as store, executor_lock(bound.repo) as lease:
        before = store.load_task_history("implementation")
        restored = inspect_task_execution(store, "implementation", 1, lease)
        assert restored == manifest
        assert store.load_task_history("implementation") == before
        assert not hasattr(restored, "adapter") and not hasattr(restored, "claim")
    with pytest.raises(ValueError):
        seal(execution)


@pytest.mark.parametrize(
    "anchor",
    [
        "missing",
        "stop_false",
        "digest_none",
        "wrong_digest",
        "wrong_path",
        "context_hash",
        "wrong_hash",
        "evidence_rejected",
    ],
)
def test_task_execution_archive_requires_positive_exact_anchor(
    execution, monkeypatch, anchor
):
    manifest = seal(execution)
    launch = execution.launch
    if anchor != "missing":
        event = TaskReconciled(
            None
            if anchor == "digest_none"
            else "wrong-final"
            if anchor == "wrong_digest"
            else manifest.final_digest,
            anchor != "stop_false",
            "synthetic anchor",
            str(launch.log_dir / "different.json")
            if anchor == "wrong_path"
            else manifest.path,
            launch.prepared.context.manifest_sha256
            if anchor == "context_hash"
            else "0" * 64
            if anchor == "wrong_hash"
            else manifest.sha256,
        )
        emit(launch, event)
    if anchor == "evidence_rejected":
        emit(launch, TaskEvidenceRejected("permanent evidence revocation"))
    if anchor in {"missing", "stop_false", "digest_none"}:
        monkeypatch.setattr(
            boundary,
            "inspect_workspace",
            lambda *args: pytest.fail("negative archive scanned workspace"),
        )
        monkeypatch.setattr(
            boundary,
            "inspect_task_context",
            lambda *args: pytest.fail("negative archive checked full context"),
        )
    with pytest.raises(ValueError):
        inspect(execution)


@pytest.mark.parametrize(
    "tamper",
    [
        "marker_bytes",
        "marker_identity",
        "marker_mode",
        "manifest_bytes",
        "manifest_identity",
        "manifest_mode",
        "raw_bytes",
        "raw_identity",
        "raw_mode",
        "context",
        "workspace",
    ],
)
def test_task_execution_archive_rejects_changed_files(execution, tamper):
    manifest = seal(execution)
    reconcile(execution, manifest)
    launch = execution.launch
    selected = {
        "marker": launch.log_dir / ".seal-attempt.json",
        "manifest": Path(manifest.path),
        "raw": launch.log_dir / "turn.jsonl",
    }
    if tamper == "context":
        path = Path(launch.prepared.context.path) / "prompt.txt"
        contexts.replace_bytes(path, path.read_bytes() + b"changed")
    elif tamper == "workspace":
        (Path(launch.prepared.workspace.root) / "src/code.py").unlink()
    else:
        kind, change = tamper.split("_")
        path = selected[kind]
        if change == "bytes":
            path.chmod(0o600)
            path.write_bytes(path.read_bytes() + b"changed")
            path.chmod(0o400 if kind != "raw" else 0o644)
        elif change == "identity":
            mode, raw = stat.S_IMODE(path.stat().st_mode), path.read_bytes()
            path.rename(path.with_name(path.name + ".retained"))
            path.write_bytes(raw)
            path.chmod(mode)
        else:
            path.chmod(0o600 if kind != "raw" else 0o400)
    with pytest.raises((ValueError, OSError)):
        inspect(execution)


def test_task_execution_late_history_only_does_not_pollute_current(
    execution, monkeypatch
):
    original = boundary._raw_logs
    appended = []

    def late_cancel(*args, **kwargs):
        result = original(*args, **kwargs)
        if not appended:
            appended.append(True)
            assert emit(execution.launch, CancelRequested()) == Disposition.HISTORY_ONLY
            assert (
                emit(execution.launch, execution.terminal) == Disposition.HISTORY_ONLY
            )
        return result

    monkeypatch.setattr(boundary, "_raw_logs", late_cancel)
    manifest = seal(execution)
    assert manifest.qualified and manifest.delivery_eligible
    reconcile(execution, manifest)
    assert inspect(execution).qualified


def test_task_execution_old_attempt_append_after_sample_is_harmless(bound, monkeypatch):
    with contexts.launch(bound) as first:
        contexts.settle(first)
    with contexts.launch(bound, directory=bound.root.with_name("retry")) as second:
        execution = make(second)
        original = boundary._raw_logs
        appended = []

        def old_result(*args, **kwargs):
            result = original(*args, **kwargs)
            if not appended:
                appended.append(True)
                assert (
                    emit(second, Completed({"old": True}), attempt=1)
                    == Disposition.HISTORY_ONLY
                )
            return result

        monkeypatch.setattr(boundary, "_raw_logs", old_result)
        manifest = seal(execution)
        assert manifest.qualified
        reconcile(execution, manifest)
        assert inspect(execution).qualified


@pytest.mark.parametrize(
    "change",
    [
        "rejected",
        "conflicting_terminal",
        "conflicting_exit",
        "prefix_rewrite",
        "current_attempt",
    ],
)
def test_task_execution_rechecks_current_history_after_sampling(
    execution, monkeypatch, change
):
    launch = execution.launch
    original = boundary._raw_logs
    changed = []

    def mutate_after_sample(*args, **kwargs):
        result = original(*args, **kwargs)
        if changed:
            return result
        changed.append(True)
        if change == "rejected":
            emit(launch, TaskEvidenceRejected("rejected during seal"))
        elif change == "conflicting_terminal":
            assert (
                emit(launch, replace(execution.terminal, raw_type="conflicting.raw"))
                == Disposition.HISTORY_ONLY
            )
        elif change == "conflicting_exit":
            assert (
                emit(launch, replace(execution.process_exit, exit_code=99))
                == Disposition.HISTORY_ONLY
            )
        elif change == "prefix_rewrite":
            original_history = launch.store.load_task_history

            def rewritten(run_id):
                history = original_history(run_id)
                return (
                    (replace(history[0][0], occurred_at="rewritten"), history[0][1]),
                    *history[1:],
                )

            monkeypatch.setattr(launch.store, "load_task_history", rewritten)
        else:
            original_state = launch.store.load_task

            def different_attempt(run_id):
                state = original_state(run_id)
                current = state.current_attempt
                return replace(
                    state,
                    attempts=(
                        *state.attempts[:-1],
                        replace(
                            current,
                            runtime=replace(current.runtime, number=current.number + 1),
                        ),
                    ),
                )

            monkeypatch.setattr(launch.store, "load_task", different_attempt)
        return result

    monkeypatch.setattr(boundary, "_raw_logs", mutate_after_sample)
    try:
        manifest = seal(execution)
    except ValueError:
        manifest = None
    assert manifest is None or not manifest.qualified
    assert (launch.log_dir / ".seal-attempt.json").exists()
    with pytest.raises(ValueError):
        seal(execution)


@pytest.mark.parametrize(
    "change",
    [
        "outside",
        "relative",
        "nested",
        "duplicate",
        "marker",
        "manifest",
        "identity",
        "byte_count",
        "sha256",
    ],
)
def test_task_execution_rejects_raw_log_escape_symlink_fifo_and_changed_receipt(
    execution, monkeypatch, change
):
    logs = execution.adapter.ownership_receipt().logs
    outside = execution.launch.log_dir.parent / "outside-canary"
    outside.write_text("must not be read")

    def changed(receipt):
        last = receipt.logs[-1]
        modifications = {
            "outside": {"path": str(outside)},
            "relative": {"path": "turn.jsonl"},
            "nested": {"path": str(execution.launch.log_dir / "nested/turn.jsonl")},
            "duplicate": {"path": logs[0].path},
            "marker": {"path": str(execution.launch.log_dir / ".seal-attempt.json")},
            "manifest": {"path": str(execution.launch.log_dir / "execution.json")},
            "identity": {"identity": (last.identity[0], last.identity[1] + 1)},
            "byte_count": {"byte_count": last.byte_count + 1},
            "sha256": {"sha256": "0" * 64},
        }[change]
        return replace(
            receipt, logs=(*receipt.logs[:-1], replace(last, **modifications))
        )

    execution.adapter.transform_receipt = changed
    original = boundary._read_owned_file
    reads = []

    def inspected(directory, owned, name, **kwargs):
        if name.endswith(".jsonl"):
            reads.append(name)
        assert name != outside.name, "opened a receipt-supplied external file"
        return original(directory, owned, name, **kwargs)

    monkeypatch.setattr(boundary, "_read_owned_file", inspected)
    before = execution.launch.store.load_task_history("implementation")
    with pytest.raises(ValueError):
        seal(execution)
    if change in {"outside", "relative", "nested", "duplicate", "marker", "manifest"}:
        assert reads == [], "all path bindings must be checked before any raw log read"
    assert execution.launch.store.load_task_history("implementation") == before
    assert not (execution.launch.log_dir / "execution.json").exists()


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_task_execution_special_raw_log_rejection_is_bounded(
    execution, monkeypatch, kind
):
    # A stale synthetic receipt exercises the host file boundary after replacement.
    sampled = execution.adapter.ownership_receipt()
    path = execution.launch.log_dir / "turn.jsonl"
    path.unlink()
    if kind == "symlink":
        target = path.parent.parent / "external-canary"
        target.write_text("must not be followed")
        path.symlink_to(target)
    else:
        os.mkfifo(path)
    execution.adapter.transform_receipt = lambda receipt: sampled
    original = boundary._read_owned_file
    rescues, rescue_errors = [], []

    def guarded(directory, logs, name, **kwargs):
        if name != path.name or kind != "fifo":
            return original(directory, logs, name, **kwargs)
        finished = threading.Event()

        def release_blocked_reader():
            if finished.wait(2):
                return
            try:
                fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
                try:
                    rescues.append(True)
                    os.write(fd, b"{}")
                finally:
                    os.close(fd)
            except OSError as error:
                rescue_errors.append(error)

        rescue = threading.Thread(target=release_blocked_reader, daemon=True)
        rescue.start()
        try:
            return original(directory, logs, name, **kwargs)
        finally:
            finished.set()
            rescue.join(timeout=3)
            assert not rescue.is_alive()

    monkeypatch.setattr(boundary, "_read_owned_file", guarded)
    with pytest.raises((ValueError, OSError)):
        seal(execution)
    assert rescues == [] and rescue_errors == []
    assert (path.parent / ".seal-attempt.json").exists()
    assert not (path.parent / "execution.json").exists()


@pytest.mark.parametrize("original_stopped", [False, True])
def test_task_execution_later_receipt_cannot_replace_first_sample(
    live, monkeypatch, original_stopped
):
    execution = make(live, stopped=original_stopped)
    first = execution.adapter.ownership_receipt()
    original = boundary._raw_logs
    changed = []

    def changed_after_sample(*args, **kwargs):
        result = original(*args, **kwargs)
        if not changed:
            changed.append(True)
            resource = execution.adapter.process
            resource.record = replace(resource.record, group_gone=not original_stopped)
        return result

    monkeypatch.setattr(boundary, "_raw_logs", changed_after_sample)
    with pytest.raises(ValueError):
        seal(execution)
    marker = payload(live.log_dir / ".seal-attempt.json")
    assert task_execution_hash(marker["receipt"]) == task_execution_hash(first)
    assert marker["facts"]["processes_stopped"] == original_stopped
    with pytest.raises(ValueError):
        seal(execution)


def test_task_execution_raw_log_read_stops_at_reported_byte_count(
    execution, monkeypatch
):
    original = boundary._read_owned_file
    actual_read = os.read
    grew, counts = [], []
    target = execution.launch.log_dir / "turn.jsonl"
    expected = target.stat().st_size

    def bounded(directory, logs, name, **kwargs):
        if name != target.name:
            return original(directory, logs, name, **kwargs)
        identity = target.stat().st_dev, target.stat().st_ino

        def growing_read(fd, count):
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != identity:
                return actual_read(fd, count)
            counts.append(count)
            if len(counts) > 1:
                pytest.fail("raw log reader chased appended bytes or EOF")
            block = actual_read(fd, count)
            with target.open("ab") as writer:
                writer.write(b"unexpected concurrent growth\n")
            grew.append(True)
            return block

        with monkeypatch.context() as patch:
            patch.setattr(os, "read", growing_read)
            return original(directory, logs, name, **kwargs)

    monkeypatch.setattr(boundary, "_read_owned_file", bounded)
    with pytest.raises(ValueError):
        seal(execution)
    assert grew == [True] and counts == [expected]
    assert not (execution.launch.log_dir / "execution.json").exists()


def test_task_execution_large_raw_logs_are_streamed_without_control_limit(live):
    execution = make(live, extra_log_bytes=17 * 1024 * 1024)
    manifest = seal(execution)
    assert manifest.qualified
    assert any(log.byte_count > 16 * 1024 * 1024 for log in manifest.receipt.logs)
    assert manifest.byte_count < 16 * 1024 * 1024
    reconcile(execution, manifest)
    assert inspect(execution).qualified


def test_task_execution_control_manifest_limit_consumes_binding(execution):
    actual = replace(execution.terminal, result="x" * (16 * 1024 * 1024 + 1))
    execution.adapter.actual_result = TurnResult(actual, execution.process_exit)
    before = execution.launch.store.load_task_history("implementation")
    with pytest.raises(ValueError):
        seal(execution)
    assert (execution.launch.log_dir / ".seal-attempt.json").exists()
    assert not (execution.launch.log_dir / "execution.json").exists()
    assert execution.launch.store.load_task_history("implementation") == before
    execution.adapter.actual_result = TurnResult(
        execution.terminal, execution.process_exit
    )
    with pytest.raises(ValueError):
        seal(execution)


@pytest.mark.parametrize("name", [".seal-attempt.json", "execution.json"])
def test_task_execution_existing_control_file_is_never_overwritten(execution, name):
    path = execution.launch.log_dir / name
    path.write_bytes(b"existing owner bytes\n")
    path.chmod(0o400)
    with pytest.raises(FileExistsError):
        seal(execution)
    assert path.read_bytes() == b"existing owner bytes\n"
    with pytest.raises(ValueError):
        seal(execution)


@pytest.mark.parametrize(
    "failure",
    [
        "marker_write",
        "index_write",
        "marker_fsync",
        "index_fsync",
        "marker_close",
        "index_close",
        "directory_close",
        "write_and_close",
    ],
)
def test_task_execution_file_failure_retains_marker_and_never_reseals(
    execution, monkeypatch, failure
):
    directory = execution.launch.log_dir
    marker, index = directory / ".seal-attempt.json", directory / "execution.json"
    problem, closing = (
        OSError("first injected evidence I/O failure"),
        OSError("secondary close failure"),
    )
    original_write, original_fsync, original_close = os.write, os.fsync, os.close
    raised, close_raised = [], []

    def matches(fd, path):
        if not path.exists():
            return False
        before, current = os.fstat(fd), path.stat()
        return (before.st_dev, before.st_ino) == (current.st_dev, current.st_ino)

    def failed_write(fd, raw):
        target = marker if failure in {"marker_write", "write_and_close"} else index
        if (
            failure in {"marker_write", "index_write", "write_and_close"}
            and matches(fd, target)
            and not raised
        ):
            original_write(fd, raw[:5])
            raised.append(True)
            raise problem
        return original_write(fd, raw)

    def failed_fsync(fd):
        original_fsync(fd)
        target = marker if failure == "marker_fsync" else index
        if (
            failure in {"marker_fsync", "index_fsync"}
            and matches(fd, target)
            and not raised
        ):
            raised.append(True)
            raise problem

    def failed_close(fd):
        target = (
            directory
            if failure == "directory_close"
            else marker
            if failure in {"marker_close", "write_and_close"}
            else index
        )
        fail = (
            failure
            in {"marker_close", "index_close", "directory_close", "write_and_close"}
            and matches(fd, target)
            and not close_raised
        )
        original_close(fd)
        if fail:
            close_raised.append(True)
            raise closing if failure == "write_and_close" else problem

    before = execution.launch.store.load_task_history("implementation")
    with monkeypatch.context() as patch:
        patch.setattr(os, "write", failed_write)
        patch.setattr(os, "fsync", failed_fsync)
        patch.setattr(os, "close", failed_close)
        with pytest.raises(OSError) as caught:
            seal(execution)
    assert caught.value is problem
    assert marker.is_file()
    notes = "\n".join(getattr(problem, "__notes__", ()))
    assert str(marker) in notes and str(index) in notes
    if failure == "write_and_close":
        assert str(closing) in notes
    assert execution.launch.store.load_task_history("implementation") == before
    assert execution.adapter.ownership_receipt().processes_stopped
    with pytest.raises(ValueError):
        seal(execution)


def test_task_execution_host_input_read_failure_is_not_model_validation(
    execution, monkeypatch
):
    problem = ValueError("host task input decoding failed")
    original = boundary._report

    def failing_host_input(actual, store, run_id):
        def failed(*args):
            raise problem

        with monkeypatch.context() as patch:
            patch.setattr(store, "load_task_input", failed)
            return original(actual, store, run_id)

    monkeypatch.setattr(boundary, "_report", failing_host_input)
    before = execution.launch.store.load_task_history("implementation")
    with pytest.raises(ValueError) as caught:
        seal(execution)
    assert caught.value is problem
    assert (execution.launch.log_dir / ".seal-attempt.json").is_file()
    assert not (execution.launch.log_dir / "execution.json").exists()
    assert execution.launch.store.load_task_history("implementation") == before
    with pytest.raises(ValueError):
        seal(execution)


@pytest.mark.parametrize(
    "missing", ["terminal", "exit", "different_result", "raw_type", "failed"]
)
def test_task_execution_actual_result_must_match_complete_history(live, missing):
    execution = make(live, record=False)
    terminal = execution.terminal
    if missing == "different_result":
        terminal = replace(
            terminal, result={**execution.report, "summary": "different report"}
        )
    elif missing == "raw_type":
        terminal = replace(terminal, raw_type="different.raw.type")
    elif missing == "failed":
        terminal = Error("actual model failure", raw_type="synthetic.error")
        execution.adapter.actual_result = TurnResult(terminal, execution.process_exit)
    if missing != "terminal":
        emit(live, terminal)
    if missing != "exit":
        emit(live, execution.process_exit)
    manifest = seal(execution)
    assert not manifest.qualified
    assert manifest.actual_result == execution.adapter.actual_result


def test_task_execution_archive_checks_context_identity_before_sibling_open(
    execution, monkeypatch
):
    manifest = seal(execution)
    reconcile(execution, manifest)
    launch = execution.launch
    original_state = launch.store.load_task("implementation")
    original_history = launch.store.load_task_history("implementation")
    sibling = launch.log_dir.parent.with_name("sibling-canary")
    sibling.mkdir(mode=0o700)
    (sibling / "context").mkdir(mode=0o500)
    (sibling / "logs").mkdir(mode=0o700)
    (sibling / "logs/execution.json").write_bytes(b"must not be opened")
    prepared = replace(
        launch.prepared,
        workspace=replace(launch.prepared.workspace, root=str(sibling / "workspace")),
        context=replace(launch.prepared.context, path=str(sibling / "context")),
    )
    current = original_state.current_attempt
    request = replace(current.request, worktree=str(sibling / "workspace"))
    reconciliation = replace(
        current.reconciliation, log_path=str(sibling / "logs/execution.json")
    )

    def changed(event):
        return {
            "task_attempt_started": request,
            "task_execution_prepared": prepared,
            "task_reconciled": reconciliation,
        }.get(event.kind, event)

    selected = replace(
        current,
        request=request,
        reconciliation=reconciliation,
        events=tuple(changed(event) for event in current.events),
    )
    state = replace(original_state, attempts=(*original_state.attempts[:-1], selected))
    history = tuple(
        (replace(envelope, event=changed(envelope.event)), disposition)
        for envelope, disposition in original_history
    )
    monkeypatch.setattr(launch.store, "load_task", lambda run_id: state)
    monkeypatch.setattr(launch.store, "load_task_history", lambda run_id: history)
    original_open = os.open
    opened = []

    def no_sibling_open(path, *args, **kwargs):
        if isinstance(path, (str, bytes, os.PathLike)) and Path(
            os.fsdecode(path)
        ).is_relative_to(sibling):
            opened.append(path)
            pytest.fail("archive opened a sibling before validating context identity")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", no_sibling_open)
    monkeypatch.setattr(
        boundary,
        "inspect_workspace",
        lambda *args: pytest.fail("invalid archive scanned workspace"),
    )
    with pytest.raises(ValueError):
        inspect(execution)
    assert opened == []


@pytest.mark.parametrize(
    "forged", ["qualified", "report", "runtime_observations", "facts_boolean"]
)
def test_task_execution_archive_recomputes_canonical_facts_after_reanchoring(
    execution, forged
):
    manifest = (
        seal(execution, environment=None) if forged == "qualified" else seal(execution)
    )
    value = payload(manifest.path)
    if forged == "qualified":
        assert not manifest.qualified
        value["qualified"] = True
        value["delivery_eligible"] = True
    elif forged == "report":
        value["report"]["summary"] = "invented model report"
    elif forged == "runtime_observations":
        requested = execution.launch.store.load_task(
            "implementation"
        ).current_attempt.request.decision.choice
        value["runtime_observations"]["reported_model"] = requested.model
        value["runtime_observations"]["reported_effort"] = requested.effort
    else:
        assert value["facts"]["processes_stopped"] is True
        value["facts"]["processes_stopped"] = 1
    raw = (task_execution_json(value) + "\n").encode("utf-8")
    path = Path(manifest.path)
    identity = path.stat().st_dev, path.stat().st_ino
    contexts.replace_bytes(path, raw)
    assert (path.stat().st_dev, path.stat().st_ino) == identity
    digest = hashlib.sha256(raw).hexdigest()
    assert digest != manifest.sha256
    reconcile(execution, manifest, digest=digest)
    assert execution.launch.store.load_task("implementation").delivery_ready
    before = execution.launch.store.load_task_history("implementation")
    with pytest.raises(ValueError):
        inspect(execution)
    assert execution.launch.store.load_task_history("implementation") == before
