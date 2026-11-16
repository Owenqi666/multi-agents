"""Synthetic planning evidence uses real files and ledgers, never a model CLI."""

import asyncio
import hashlib
import json
import os
import stat
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_planning_context as contexts

from multi_agent.adapters.base import (
    EnvironmentInfo,
    SessionHandle,
    TurnHandle,
    TurnResult,
)
from multi_agent.adapters.ownership import (
    DirectoryOwnership,
    IsolationOwnership,
    OwnershipLedger,
    ProcessOwnership,
)
from multi_agent.adapters.probe.process import Process
from multi_agent.adapters.probe.recording import Recorder, Redactor
from multi_agent.adapters.runtime import RuntimeAdapter
from multi_agent.cli import planning_evidence as boundary
from multi_agent.cli.planning_context import prepare_planning_attempt
from multi_agent.cli.planning_evidence import (
    bind_planning_task,
    inspect_planning_execution,
    prepare_planning_logs,
    seal_planning_execution,
)
from multi_agent.core.events import Completed, Error, ProcessExited
from multi_agent.core.planning import (
    PlanningPhase,
    PlanningReview,
    planning_hash,
    planning_json,
    planning_value,
)
from multi_agent.core.planning_events import (
    PlanningDiagnostic,
    PlanningDraftValidated,
    PlanningEnvironmentObserved,
    PlanningEvidenceRejected,
    PlanningReconciled,
    PlanningReviewValidated,
)

# Explicit registration preserves these fixtures' no-runtime-process guards.
project = contexts.project
store = contexts.store
REAL_SUBPROCESS_EXEC = asyncio.create_subprocess_exec
REAL_POPEN = subprocess.Popen


class SyntheticResource:
    """Invented ownership facts, never represented as a real process."""

    def __init__(self, record):
        self.record = record

    def ownership_record(self, resource_id):
        return replace(self.record, resource_id=resource_id)


class SyntheticRuntime(RuntimeAdapter):
    """Real ledger/Recorder; no CLI, request, or permission claim."""

    def __init__(self, logs, executable, terminal, process_exit, *, extra_log_bytes=0):
        super().__init__(
            "claude" if logs.stage == "draft" else "codex",
            executable=str(executable),
            log_dir=Path(logs.path),
        )
        self.turn = TurnHandle("synthetic-turn", SessionHandle("synthetic-session"))
        self.actual_result = TurnResult(terminal, process_exit)
        self.environment = EnvironmentInfo(
            "synthetic-no-cli",
            True,
            ("reported-model",),
            {"reported-model": ("medium",)},
        )
        self.result_calls = []
        self.transform_receipt = lambda receipt: receipt
        self.recorders = []
        self.synthetic_process = SyntheticResource(
            ProcessOwnership(
                "",
                "synthetic-not-a-process",
                987654321,
                -15,
                True,
                True,
                True,
                True,
                True,
                True,
            )
        )
        ticket = self._ownership.acquire("process")
        ticket.publish(self.synthetic_process)
        ticket.finish()
        for kind in ("environment", "turn"):
            operation = self._ownership.begin_operation(kind)
            recorder = Recorder(
                self.log_dir / f"{kind}.jsonl",
                Redactor(),
                acquisition=self._ownership.acquire("log"),
            )
            self.recorders.append(recorder)
            recorder.emit("synthetic_evidence", operation=kind, actual_cli=False)
            if extra_log_bytes and kind == "turn":
                for _ in range(extra_log_bytes // (1024 * 1024) + 1):
                    recorder.emit("synthetic_chunk", data="x" * (1024 * 1024))
            recorder.close()
            self._ownership.finish_operation(operation)

    async def result(self, turn):
        assert turn is self.turn
        self.result_calls.append(turn)
        return self.actual_result

    def ownership_receipt(self):
        return self.transform_receipt(super().ownership_receipt())


def emit(store, event, stage="draft"):
    contexts.emit(store, event, stage=stage)


def make_attempt(
    store,
    project,
    *,
    stage="draft",
    card=None,
    decision="ready",
    unresolved=False,
    observe_environment=True,
    extra_log_bytes=0,
    record_runtime=True,
    reported_model="reported-synthetic-model",
    environment=None,
):
    if stage == "draft":
        prepared = contexts.prepare(store, project)
        card = contexts.draft_card(project)
        if not unresolved:
            card = replace(card, open_questions=card.open_questions[:1])
        artifact, result = card, card.to_dict()
    else:
        prepared = prepare_planning_attempt(store, "plan", "review")
        artifact = PlanningReview(card.task_hash, decision, "Synthetic reviewer", ())
        result = planning_value(artifact)
    current = store.load_planning("plan").current(stage)
    logs = prepare_planning_logs(store, "plan", stage, current.number)
    terminal = Completed(
        result,
        reported_model=reported_model,
        raw_type="synthetic.completed",
    )
    process_exit = ProcessExited(-15, raw_type="synthetic.process_exit")
    executable = project.executables["claude" if stage == "draft" else "codex"]
    adapter = SyntheticRuntime(
        logs, executable, terminal, process_exit, extra_log_bytes=extra_log_bytes
    )
    if environment is not None:
        adapter.environment = environment
    if observe_environment:
        emit(
            store,
            PlanningEnvironmentObserved(planning_value(adapter.environment)),
            stage,
        )
    if record_runtime:
        emit(store, terminal, stage)
        emit(store, process_exit, stage)
    return SimpleNamespace(
        stage=stage,
        attempt=current.number,
        prepared=prepared,
        logs=logs,
        adapter=adapter,
        environment=adapter.environment,
        terminal=terminal,
        process_exit=process_exit,
        card=card,
        artifact=artifact,
    )


def seal(store, attempt, **overrides):
    values = {
        "logs": attempt.logs,
        "adapter": attempt.adapter,
        "turn": attempt.adapter.turn,
        "environment": attempt.environment,
    }
    values.update(overrides)
    return asyncio.run(
        seal_planning_execution(store, "plan", attempt.stage, attempt.attempt, **values)
    )


def reconcile(store, attempt, manifest, *, stopped=True, path=None, digest=None):
    emit(
        store,
        PlanningReconciled(
            stopped,
            "Synthetic evidence fixture; no actual CLI",
            path if path is not None else manifest.path,
            digest if digest is not None else manifest.sha256,
        ),
        attempt.stage,
    )


def validate(store, attempt):
    event = (
        PlanningDraftValidated(attempt.artifact)
        if attempt.stage == "draft"
        else PlanningReviewValidated(attempt.artifact)
    )
    emit(store, event, attempt.stage)


def finish(store, attempt):
    manifest = seal(store, attempt)
    assert manifest.qualified
    reconcile(store, attempt, manifest)
    validate(store, attempt)
    return manifest


def pair(store, project, *, unresolved=False, decision="ready"):
    draft = make_attempt(store, project, unresolved=unresolved)
    draft.manifest = finish(store, draft)
    review = make_attempt(
        store, project, stage="review", card=draft.card, decision=decision
    )
    review.manifest = finish(store, review)
    return draft, review


def read_payload(path):
    return json.loads(Path(path).read_bytes())


def rewrite_payload(path, payload):
    file = Path(path)
    file.chmod(0o600)
    raw = (planning_json(payload) + "\n").encode()
    file.write_bytes(raw)
    file.chmod(0o400)
    return hashlib.sha256(raw).hexdigest()


def inspect(store, attempt):
    return inspect_planning_execution(store, "plan", attempt.stage, attempt.attempt)


def test_planning_execution_manifest_binds_exact_attempt(store, project):
    attempt = make_attempt(store, project)
    before = store.load_planning_history("plan")
    manifest = seal(store, attempt)
    assert store.load_planning_history("plan") == before
    assert (
        manifest.qualified and manifest.receipt == attempt.adapter.ownership_receipt()
    )
    assert attempt.adapter.result_calls == [attempt.adapter.turn]
    path = Path(manifest.path)
    raw, payload = path.read_bytes(), read_payload(path)
    current = store.load_planning("plan").current("draft")
    binding = payload["binding"]
    assert binding["run_id"] == "plan"
    assert binding["input_hash"] == project.value.input_hash
    assert binding["stage"] == "draft" and binding["attempt"] == 1
    assert binding["started_hash"] == planning_hash(current.request)
    assert binding["prepared_hash"] == planning_hash(attempt.prepared)
    assert binding["source_snapshot_id"] == attempt.prepared.source_snapshot_id
    assert binding["owner"] == planning_value(attempt.prepared.run_owner)
    assert binding["context"] == planning_value(attempt.prepared.attempt_context)
    assert binding["logs"] == planning_value(attempt.logs)
    assert payload["result"]["terminal"] == planning_value(attempt.terminal)
    assert payload["result"]["process_exit"] == planning_value(attempt.process_exit)
    assert payload["environment"] == planning_value(attempt.environment)
    assert (
        payload["result"]["terminal"]["reported_model"] != current.request.request.model
    )
    assert raw == (planning_json(payload) + "\n").encode()
    assert manifest.byte_count == len(raw)
    assert manifest.sha256 == hashlib.sha256(raw).hexdigest()
    assert manifest.identity == (path.stat().st_dev, path.stat().st_ino)
    assert stat.S_IMODE(path.stat().st_mode) == 0o400
    assert path.name == "execution.json"
    assert len(payload["raw_logs"]) == 2
    assert all(
        Path(item["path"]).parent == Path(attempt.logs.path)
        for item in payload["raw_logs"]
    )
    assert all(item["path"] != manifest.path for item in payload["raw_logs"])
    with pytest.raises(FrozenInstanceError):
        manifest.qualified = False
    reconcile(store, attempt, manifest)
    assert inspect(store, attempt) == manifest
    with pytest.raises(ValueError):
        inspect_planning_execution(store, "plan", "draft", 2)


@pytest.mark.parametrize(
    "condition",
    [
        "active",
        "pending",
        "unknown",
        "empty",
        "missing_environment",
        "extra_turn",
        "process_exit",
        "group",
        "watcher",
        "readers",
        "stdout",
        "stderr",
        "isolation_pending",
        "isolation_unknown",
        "isolation_unstopped",
        "isolation_remaining",
        "directory_remaining",
        "log_unclosed",
        "log_unhealthy",
        "log_unsealed",
    ],
)
def test_planning_execution_requires_full_owned_receipt(store, project, condition):
    attempt = make_attempt(store, project)
    adapter, ledger = attempt.adapter, attempt.adapter._ownership
    if condition == "active":
        ledger.begin_operation("turn")
    elif condition in ("pending", "unknown"):
        ticket = ledger.acquire("process")
        if condition == "unknown":
            ticket.finish()
    elif condition == "empty":
        adapter._ownership = OwnershipLedger()
    elif condition == "missing_environment":
        adapter.transform_receipt = lambda receipt: replace(
            receipt, operations=receipt.operations[1:]
        )
    elif condition == "extra_turn":
        ledger.finish_operation(ledger.begin_operation("turn"))
    elif condition in (
        "process_exit",
        "group",
        "watcher",
        "readers",
        "stdout",
        "stderr",
    ):
        field = {
            "process_exit": ("exit_code", None),
            "group": ("group_gone", False),
            "watcher": ("watcher_settled", False),
            "readers": ("readers_settled", False),
            "stdout": ("stdout_eof", False),
            "stderr": ("stderr_eof", False),
        }[condition]
        adapter.synthetic_process.record = replace(
            adapter.synthetic_process.record, **{field[0]: field[1]}
        )
    elif condition.startswith("isolation_"):
        resource = SyntheticResource(
            IsolationOwnership(
                "",
                "/synthetic-removed",
                int(condition == "isolation_pending"),
                int(condition == "isolation_unknown"),
                1,
                int(condition == "isolation_unstopped"),
                condition != "isolation_remaining",
            )
        )
        ticket = ledger.acquire("isolation")
        ticket.publish(resource)
        ticket.finish()
    elif condition == "directory_remaining":
        ticket = ledger.acquire("directory")
        ticket.publish(
            SyntheticResource(DirectoryOwnership("", "/synthetic-remaining", False))
        )
        ticket.finish()
    else:
        field = {
            "log_unclosed": "closed",
            "log_unhealthy": "healthy",
            "log_unsealed": "sealed",
        }[condition]
        adapter.transform_receipt = lambda receipt: replace(
            receipt,
            logs=(replace(receipt.logs[0], **{field: False}), *receipt.logs[1:]),
        )
    original = adapter.ownership_receipt()
    manifest = seal(store, attempt)
    assert not manifest.qualified
    assert manifest.receipt == original
    assert (
        read_payload(manifest.path)["facts"]["processes_stopped"]
        is original.processes_stopped
    )
    if condition in (
        "stdout",
        "stderr",
        "isolation_remaining",
        "directory_remaining",
        "log_unsealed",
    ):
        assert original.processes_stopped


@pytest.mark.parametrize(
    "mismatch",
    [
        "terminal",
        "exit",
        "model",
        "raw_type",
        "environment",
        "missing_observation",
        "no_turn",
        "empty_result",
    ],
)
def test_planning_execution_refuses_legacy_synthetic_and_unsettled_attempts(
    store, project, mismatch
):
    attempt = make_attempt(
        store, project, observe_environment=mismatch != "missing_observation"
    )
    overrides = {}
    if mismatch == "terminal":
        attempt.adapter.actual_result = replace(
            attempt.adapter.actual_result, terminal=Completed({"different": True})
        )
    elif mismatch == "exit":
        attempt.adapter.actual_result = replace(
            attempt.adapter.actual_result, process_exit=ProcessExited(0)
        )
    elif mismatch in ("model", "raw_type"):
        keyword = "reported_model" if mismatch == "model" else "raw_type"
        attempt.adapter.actual_result = replace(
            attempt.adapter.actual_result,
            terminal=replace(attempt.terminal, **{keyword: "different-actual-report"}),
        )
    elif mismatch == "environment":
        overrides["environment"] = replace(attempt.environment, logged_in=False)
    elif mismatch == "no_turn":
        overrides["turn"] = None
    elif mismatch == "empty_result":
        attempt.adapter.actual_result = TurnResult()
    manifest = seal(store, attempt, **overrides)
    assert not manifest.qualified
    assert manifest.receipt.processes_stopped
    assert (
        store.load_planning("plan").current("draft").runtime.result
        == attempt.terminal.result
    )


@pytest.mark.parametrize("kind", ["existing", "symlink"])
def test_planning_logs_reject_existing_or_symlink_directory(store, project, kind):
    prepared = contexts.prepare(store, project)
    destination = (
        Path(prepared.run_owner.root)
        / "logs"
        / f"draft-1-{prepared.attempt_context.token}"
    )
    destination.parent.mkdir(exist_ok=True)
    if kind == "existing":
        destination.mkdir()
    else:
        outside = project.directory / "outside-logs"
        outside.mkdir()
        destination.symlink_to(outside, target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        prepare_planning_logs(store, "plan", "draft", 1)
    assert not list(destination.iterdir())


def test_planning_logs_are_private_immutable_and_require_managed_context(
    store, project
):
    with pytest.raises(ValueError):
        prepare_planning_logs(store, "plan", "draft", 1)
    prepared = contexts.prepare(store, project)
    logs = prepare_planning_logs(store, "plan", "draft", 1)
    path = Path(logs.path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert logs.identity == (path.stat().st_dev, path.stat().st_ino)
    assert logs.context_token == prepared.attempt_context.token
    assert path.parent == project.root / "logs"
    assert not path.is_relative_to(project.repo)
    assert not path.is_relative_to(Path(prepared.attempt_context.path))
    with pytest.raises(FrozenInstanceError):
        logs.path = str(project.repo)
    with pytest.raises((ValueError, OSError)):
        prepare_planning_logs(store, "plan", "draft", 1)


@pytest.mark.parametrize("target", ["manifest", "marker", "raw"])
@pytest.mark.parametrize(
    "mutation", ["replace", "symlink", "mode", "append", "truncate", "same_inode"]
)
def test_planning_execution_manifest_rejects_tampering(
    store, project, target, mutation
):
    attempt = make_attempt(store, project)
    manifest = seal(store, attempt)
    reconcile(store, attempt, manifest)
    payload = read_payload(manifest.path)
    path = Path(
        {
            "manifest": manifest.path,
            "marker": payload["marker"]["path"],
            "raw": payload["raw_logs"][0]["path"],
        }[target]
    )
    raw, original_mode = path.read_bytes(), stat.S_IMODE(path.stat().st_mode)
    if mutation in ("replace", "symlink"):
        saved = path.with_name(path.name + ".original")
        path.rename(saved)
        if mutation == "replace":
            path.write_bytes(raw)
            path.chmod(original_mode)
        else:
            path.symlink_to(saved)
    elif mutation == "mode":
        path.chmod(0o600 if original_mode != 0o600 else 0o644)
    else:
        original_identity = path.stat().st_dev, path.stat().st_ino
        path.chmod(0o600)
        path.write_bytes(
            {
                "append": raw + b" ",
                "truncate": raw[:-1],
                "same_inode": bytes([raw[0] ^ 1]) + raw[1:],
            }[mutation]
        )
        path.chmod(original_mode)
        assert (path.stat().st_dev, path.stat().st_ino) == original_identity
    with pytest.raises((ValueError, OSError)):
        inspect(store, attempt)


@pytest.mark.parametrize(
    "mutation",
    ["directory_identity", "directory_mode", "context", "executable", "owner"],
)
def test_planning_execution_rechecks_owned_files(store, project, mutation):
    attempt = make_attempt(store, project)
    manifest = seal(store, attempt)
    reconcile(store, attempt, manifest)
    logs = Path(attempt.logs.path)
    if mutation == "directory_identity":
        moved = logs.with_name("displaced-logs")
        logs.rename(moved)
        logs.mkdir(mode=0o700)
        for path in moved.iterdir():
            path.rename(logs / path.name)
    elif mutation == "directory_mode":
        logs.chmod(0o755)
    elif mutation == "context":
        Path(attempt.prepared.attempt_context.path).chmod(0o700)
    elif mutation == "executable":
        project.executables["claude"].write_text("different executable bytes")
    else:
        owner = project.root / ".planning-owner.json"
        owner.chmod(0o600)
        owner.write_bytes(owner.read_bytes() + b" ")
        owner.chmod(0o400)
    with pytest.raises(ValueError):
        inspect(store, attempt)


def test_planning_execution_rejects_external_log_before_open(
    store, project, monkeypatch
):
    attempt = make_attempt(store, project)
    outside = project.directory / "external-secret"
    original_open, opened = os.open, []

    def forbidden_open(path, *args, **kwargs):
        if not isinstance(path, int) and Path(path) == outside:
            opened.append(path)
            pytest.fail("evidence boundary opened an external log path")
        return original_open(path, *args, **kwargs)

    attempt.adapter.transform_receipt = lambda receipt: replace(
        receipt, logs=(replace(receipt.logs[0], path=str(outside)), *receipt.logs[1:])
    )
    monkeypatch.setattr(os, "open", forbidden_open)
    with pytest.raises(ValueError):
        seal(store, attempt)
    assert opened == []


def test_planning_execution_manifest_detects_change_during_read(
    store, project, monkeypatch
):
    attempt = make_attempt(store, project)
    manifest = seal(store, attempt)
    reconcile(store, attempt, manifest)
    target = Path(read_payload(manifest.path)["raw_logs"][0]["path"])
    identity = target.stat().st_dev, target.stat().st_ino
    original_read, changed = os.read, []

    def race_read(fd, amount):
        block = original_read(fd, amount)
        info = os.fstat(fd)
        if block and (info.st_dev, info.st_ino) == identity and not changed:
            changed.append(True)
            with target.open("ab") as stream:
                stream.write(b"race")
        return block

    monkeypatch.setattr(os, "read", race_read)
    with pytest.raises(ValueError):
        inspect(store, attempt)
    assert changed == [True]


def test_planning_execution_recomputes_receipt_qualification(store, project):
    attempt = make_attempt(store, project)
    attempt.adapter.synthetic_process.record = replace(
        attempt.adapter.synthetic_process.record, stdout_eof=False
    )
    manifest = seal(store, attempt)
    assert not manifest.qualified and manifest.receipt.processes_stopped
    payload = read_payload(manifest.path)
    payload["qualified"] = True
    payload["facts"]["outputs_complete"] = True
    digest = rewrite_payload(manifest.path, payload)
    reconcile(store, attempt, manifest, digest=digest)
    with pytest.raises(ValueError):
        inspect(store, attempt)


def test_planning_execution_raw_log_larger_than_manifest_limit_is_streamed(
    store, project, monkeypatch
):
    attempt = make_attempt(store, project, extra_log_bytes=16 * 1024 * 1024)
    assert (attempt.adapter.log_dir / "turn.jsonl").stat().st_size > 16 * 1024 * 1024
    original_read, requests = os.read, []

    def streaming_read(fd, amount):
        requests.append(amount)
        assert 0 < amount <= 1024 * 1024
        return original_read(fd, amount)

    monkeypatch.setattr(os, "read", streaming_read)
    manifest = seal(store, attempt)
    assert manifest.qualified and manifest.byte_count < 16 * 1024 * 1024
    reconcile(store, attempt, manifest)
    assert inspect(store, attempt) == manifest
    assert len(requests) > 16


def test_planning_execution_manifest_size_limit_is_explicit(store, project):
    attempt = make_attempt(store, project)
    huge = EnvironmentInfo("x" * (16 * 1024 * 1024), True)
    before = store.load_planning_history("plan")
    with pytest.raises(ValueError, match="limit|large|size|MiB"):
        seal(store, attempt, environment=huge)
    assert store.load_planning_history("plan") == before
    assert (Path(attempt.logs.path) / ".seal-attempt.json").exists()
    with pytest.raises((ValueError, OSError)):
        seal(store, attempt)


def test_planning_execution_success_cannot_be_resealed(store, project):
    attempt = make_attempt(store, project)
    manifest = seal(store, attempt)
    files = {path.name: path.read_bytes() for path in Path(attempt.logs.path).iterdir()}
    with pytest.raises((ValueError, OSError)):
        seal(store, attempt)
    assert {
        path.name: path.read_bytes() for path in Path(attempt.logs.path).iterdir()
    } == files
    assert manifest.qualified


def test_planning_execution_rejected_attempt_cannot_be_sealed(store, project):
    attempt = make_attempt(store, project)
    emit(store, PlanningEvidenceRejected("Synthetic host evidence failure"))
    before = store.load_planning_history("plan")
    with pytest.raises(ValueError):
        seal(store, attempt)
    assert store.load_planning_history("plan") == before
    assert not (Path(attempt.logs.path) / "execution.json").exists()


def test_planning_execution_rechecks_evidence_rejection_after_result_await(
    store, project, monkeypatch
):
    attempt = make_attempt(store, project)
    before = store.load_planning("plan").current("draft")
    history = store.load_planning_history("plan")
    original_result = attempt.adapter.result
    rejection = PlanningEvidenceRejected("Host rejected while runtime result was read")

    async def rejected_result(turn):
        emit(store, rejection)
        return await original_result(turn)

    monkeypatch.setattr(attempt.adapter, "result", rejected_result)
    with pytest.raises(ValueError):
        seal(store, attempt)
    current = store.load_planning("plan").current("draft")
    assert current.runtime == before.runtime
    assert current.reconciliation == before.reconciliation
    assert current.events == (*before.events, rejection)
    after = store.load_planning_history("plan")
    assert after[:-1] == history and after[-1][0].event == rejection
    assert store.load_planning("plan").phase == PlanningPhase.FAILED
    assert attempt.adapter.ownership_receipt().processes_stopped
    assert (Path(attempt.logs.path) / ".seal-attempt.json").is_file()
    assert not (Path(attempt.logs.path) / "execution.json").exists()


@pytest.mark.parametrize("mismatch", ["legacy_path", "wrong_hash", "not_stopped"])
def test_planning_execution_inspection_requires_exact_reconciliation(
    store, project, mismatch
):
    attempt = make_attempt(store, project)
    manifest = seal(store, attempt)
    reconcile(
        store,
        attempt,
        manifest,
        stopped=mismatch != "not_stopped",
        path="/logs/synthetic-legacy" if mismatch == "legacy_path" else None,
        digest="0" * 64 if mismatch == "wrong_hash" else None,
    )
    with pytest.raises(ValueError):
        inspect(store, attempt)


def test_planning_binding_success_uses_current_verified_pair(
    store, project, monkeypatch
):
    draft, review = pair(store, project)
    state = store.load_planning("plan")
    assert state.can_bind
    expected, supplied = state.provenance().plan_hash, []
    original_create = store.create_planned_task

    def create(task_run_id, planning_run_id, created_at, *, expected_plan_hash=None):
        supplied.append(expected_plan_hash)
        return original_create(
            task_run_id,
            planning_run_id,
            created_at,
            expected_plan_hash=expected_plan_hash,
        )

    monkeypatch.setattr(store, "create_planned_task", create)
    assert bind_planning_task(store, "plan", "implementation", "bound-at") is None
    assert supplied == [expected]
    provenance = store.load_task_provenance("implementation")
    assert provenance.plan_hash == expected
    assert provenance.draft.reconciliation.log_sha256 == draft.manifest.sha256
    assert provenance.review.reconciliation.log_sha256 == review.manifest.sha256
    for selected in ("draft", "review"):
        old = getattr(provenance, selected)
        updated = replace(
            old, reconciliation=replace(old.reconciliation, log_sha256="f" * 64)
        )
        assert replace(provenance, **{selected: updated}).plan_hash != expected
    assert store.load_planning("plan").phase == PlanningPhase.BOUND
    assert store.load_task_input("implementation").card == draft.card
    assert inspect(store, draft) == draft.manifest
    assert inspect(store, review) == review.manifest


def test_planning_binding_rejects_changed_provenance_atomically(
    store, project, monkeypatch
):
    _, review = pair(store, project)
    original_create, captured = store.create_planned_task, []

    def concurrent_change(
        task_run_id, planning_run_id, created_at, *, expected_plan_hash=None
    ):
        captured.append(expected_plan_hash)
        emit(
            store,
            PlanningDiagnostic(Error("Concurrent appended provenance event")),
            "review",
        )
        captured.append(store.load_planning("plan").provenance().plan_hash)
        return original_create(
            task_run_id,
            planning_run_id,
            created_at,
            expected_plan_hash=expected_plan_hash,
        )

    before = store.load_planning_history("plan")
    monkeypatch.setattr(store, "create_planned_task", concurrent_change)
    with pytest.raises(ValueError):
        bind_planning_task(store, "plan", "implementation", "bound-at")
    assert captured[0] != captured[1]
    assert store.load_planning("plan").phase == PlanningPhase.READY
    history = store.load_planning_history("plan")
    assert len(history) == len(before) + 1
    assert isinstance(history[-1][0].event, PlanningDiagnostic)
    with pytest.raises(KeyError):
        store.load_task("implementation")
    with pytest.raises(KeyError):
        store.load_task_provenance("implementation")
    assert inspect(store, review) == review.manifest


@pytest.mark.parametrize("gate", ["unresolved", "needs_revision"])
def test_planning_binding_retains_core_ready_and_question_gates(store, project, gate):
    pair(
        store,
        project,
        unresolved=gate == "unresolved",
        decision="needs_revision" if gate == "needs_revision" else "ready",
    )
    before = store.load_planning_history("plan")
    with pytest.raises(ValueError):
        bind_planning_task(store, "plan", "implementation", "bound-at")
    assert store.load_planning_history("plan") == before
    with pytest.raises(KeyError):
        store.load_task("implementation")


def test_planning_execution_preserves_unknown_actual_fields(store, project):
    attempt = make_attempt(
        store, project, reported_model=None, environment=EnvironmentInfo()
    )
    manifest = seal(store, attempt)
    assert manifest.qualified
    payload = read_payload(manifest.path)
    assert payload["result"]["terminal"]["reported_model"] is None
    assert payload["environment"] == {
        "cli_version": None,
        "logged_in": None,
        "models": None,
        "model_efforts": None,
    }
    reconcile(store, attempt, manifest)
    assert inspect(store, attempt) == manifest


@pytest.mark.parametrize("field", ["identity", "byte_count", "sha256"])
def test_planning_execution_sealed_log_requires_complete_metadata(
    store, project, field
):
    attempt = make_attempt(store, project)
    attempt.adapter.transform_receipt = lambda receipt: replace(
        receipt, logs=(replace(receipt.logs[0], **{field: None}), *receipt.logs[1:])
    )
    before = store.load_planning_history("plan")
    if field == "identity":
        manifest = seal(store, attempt)
        assert not manifest.qualified
        assert manifest.receipt.processes_stopped
    else:
        with pytest.raises(ValueError, match="bytes differ"):
            seal(store, attempt)
    assert store.load_planning_history("plan") == before
    assert attempt.adapter.ownership_receipt().processes_stopped
    assert (Path(attempt.logs.path) / ".seal-attempt.json").exists()
    with pytest.raises((ValueError, OSError)):
        seal(store, attempt)


def test_planning_execution_marker_requires_canonical_types(store, project):
    attempt = make_attempt(store, project)
    manifest = seal(store, attempt)
    payload = read_payload(manifest.path)
    marker_path = payload["marker"]["path"]
    marker = read_payload(marker_path)
    marker["version"] = True  # Python dict equality would accept True == 1.
    marker_hash = rewrite_payload(marker_path, marker)
    payload["marker"]["sha256"] = marker_hash
    payload["marker"]["byte_count"] = Path(marker_path).stat().st_size
    digest = rewrite_payload(manifest.path, payload)
    reconcile(store, attempt, manifest, digest=digest)
    with pytest.raises(ValueError):
        inspect(store, attempt)


def test_planning_execution_changed_live_receipt_cannot_replace_initial_snapshot(
    store, project
):
    attempt = make_attempt(store, project)
    calls = []

    def change_after_capture(receipt):
        calls.append(True)
        if len(calls) == 1:
            return receipt
        return replace(
            receipt, processes=(replace(receipt.processes[0], stdout_eof=False),)
        )

    attempt.adapter.transform_receipt = change_after_capture
    before = store.load_planning_history("plan")
    with pytest.raises(ValueError):
        seal(store, attempt)
    assert len(calls) >= 2
    assert store.load_planning_history("plan") == before
    marker = read_payload(Path(attempt.logs.path) / ".seal-attempt.json")
    assert marker["receipt"]["processes"][0]["stdout_eof"] is True


@pytest.mark.parametrize(
    "fault", ["open", "write", "short_write", "flush", "close", "chmod", "read"]
)
def test_planning_execution_manifest_failure_does_not_change_runtime_state(
    store, project, monkeypatch, fault
):
    attempt = make_attempt(store, project)
    emit(store, PlanningReconciled(True, "Synthetic stopped; no log evidence yet"))
    before_state = store.load_planning("plan")
    before_history = store.load_planning_history("plan")
    before_receipt = attempt.adapter.ownership_receipt()
    original_open, original_fdopen = os.open, os.fdopen
    original_chmod, original_read = os.fchmod, os.read
    descriptors, failures = {}, []

    def fail():
        failures.append(fault)
        raise OSError(f"injected manifest {fault} failure")

    def opened(path, flags, *args, **kwargs):
        if path == "execution.json" and flags & os.O_CREAT and fault == "open":
            fail()
        fd = original_open(path, flags, *args, **kwargs)
        descriptors[fd] = path
        return fd

    class FaultyStream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            result = self.stream.__exit__(*args)
            if fault == "close":
                fail()
            return result

        def write(self, raw):
            if fault == "write":
                self.stream.write(raw[:7])
                fail()
            if fault == "short_write":
                failures.append(fault)
                return self.stream.write(raw[:7])
            return self.stream.write(raw)

        def flush(self):
            if fault == "flush":
                fail()
            return self.stream.flush()

    def fdopen(fd, *args, **kwargs):
        stream = original_fdopen(fd, *args, **kwargs)
        return (
            FaultyStream(stream) if descriptors.get(fd) == "execution.json" else stream
        )

    def chmod(fd, mode):
        if descriptors.get(fd) == "execution.json" and fault == "chmod":
            fail()
        return original_chmod(fd, mode)

    def read(fd, size):
        if descriptors.get(fd) == "execution.json" and fault == "read":
            fail()
        return original_read(fd, size)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", opened)
        patch.setattr(os, "fdopen", fdopen)
        patch.setattr(os, "fchmod", chmod)
        patch.setattr(os, "read", read)
        with pytest.raises(OSError):
            seal(store, attempt)
    assert failures == [fault]
    assert store.load_planning("plan") == before_state
    assert store.load_planning_history("plan") == before_history
    assert attempt.adapter.ownership_receipt() == before_receipt
    assert (Path(attempt.logs.path) / ".seal-attempt.json").exists()
    residual = {
        path.name: path.read_bytes() for path in Path(attempt.logs.path).iterdir()
    }
    with pytest.raises((ValueError, OSError)):
        seal(store, attempt)
    assert {
        path.name: path.read_bytes() for path in Path(attempt.logs.path).iterdir()
    } == residual


def test_planning_execution_failed_seal_cannot_be_repeated(store, project, monkeypatch):
    attempt = make_attempt(store, project)
    original_write = boundary._write_owned_json

    def partial_marker(directory, logs, name, payload):
        if name == ".seal-attempt.json":
            fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            try:
                os.write(fd, b'{"partial":')
            finally:
                os.close(fd)
            raise OSError("injected incomplete seal marker")
        return original_write(directory, logs, name, payload)

    before = store.load_planning_history("plan")
    with monkeypatch.context() as patch:
        patch.setattr(boundary, "_write_owned_json", partial_marker)
        with pytest.raises(OSError, match="incomplete seal marker"):
            seal(store, attempt)
    marker = Path(attempt.logs.path) / ".seal-attempt.json"
    assert marker.read_bytes() == b'{"partial":'
    with pytest.raises((ValueError, OSError)):
        seal(store, attempt)
    assert marker.read_bytes() == b'{"partial":'
    assert not (Path(attempt.logs.path) / "execution.json").exists()
    assert store.load_planning_history("plan") == before


def test_planning_execution_local_python_process_roundtrip(store, project, monkeypatch):
    """Actual Python child lifecycle; environment/model artifacts stay synthetic."""
    attempt = make_attempt(store, project, record_runtime=False)
    adapter = attempt.adapter
    adapter._ownership = OwnershipLedger()
    for recorder in adapter.recorders:
        recorder._path.unlink()
    original_popen = REAL_POPEN
    launches = []

    def only_python(argv, *args, **kwargs):
        assert isinstance(argv, (list, tuple)) and argv[0] == sys.executable
        assert not kwargs.get("shell", False)
        launches.append(tuple(argv))
        return original_popen(argv, *args, **kwargs)

    async def run():
        environment_operation = adapter._ownership.begin_operation("environment")
        with Recorder(
            adapter.log_dir / "environment.jsonl",
            Redactor(),
            acquisition=adapter._ownership.acquire("log"),
        ) as recorder:
            recorder.emit("synthetic_environment", cli_called=False)
        adapter._ownership.finish_operation(environment_operation)
        turn_operation = adapter._ownership.begin_operation("turn")
        process = None
        with Recorder(
            adapter.log_dir / "turn.jsonl",
            Redactor(),
            acquisition=adapter._ownership.acquire("log"),
        ) as recorder:
            try:
                async with asyncio.timeout(10):
                    process = await Process.start(
                        "actual-local-python-not-a-model",
                        [
                            sys.executable,
                            "-I",
                            "-c",
                            "print('{\"local_process\":true}')",
                        ],
                        project.directory,
                        recorder,
                        grace=0.5,
                        env={"PATH": ""},
                        start_new_session=True,
                        acquisition=adapter._ownership.acquire("process"),
                    )
                    assert await process.read() == {"local_process": True}
                    assert await process.read() is None
                    exit_code = await process.wait()
                    await process.close_finished(0.5)
                    assert exit_code == 0
                    actual_exit = ProcessExited(
                        exit_code, raw_type="local_python.process_exit"
                    )
                    adapter.actual_result = TurnResult(attempt.terminal, actual_exit)
                    emit(store, attempt.terminal)
                    emit(store, actual_exit)
            finally:
                if process is not None:
                    await process.terminate(0.5)
        adapter._ownership.finish_operation(turn_operation)
        manifest = await seal_planning_execution(
            store,
            "plan",
            "draft",
            1,
            logs=attempt.logs,
            adapter=adapter,
            turn=adapter.turn,
            environment=attempt.environment,
        )
        assert manifest.qualified
        assert len(manifest.receipt.processes) == 1
        record = manifest.receipt.processes[0]
        assert record.pid == process.child.pid and record.exit_code == 0
        assert record.stopped and record.output_complete
        reconcile(store, attempt, manifest)
        assert inspect(store, attempt) == manifest

    with monkeypatch.context() as patch:
        patch.setattr(asyncio, "create_subprocess_exec", REAL_SUBPROCESS_EXEC)
        patch.setattr(subprocess, "Popen", only_python)
        asyncio.run(run())
    assert len(launches) == 1
