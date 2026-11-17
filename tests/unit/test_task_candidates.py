"""Real Git/SQLite/Recorder boundaries; synthetic owners and actual fake children."""

import asyncio
import hashlib
import os
import shutil
import sqlite3
import zlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_planning_context as planning_contexts
import test_task_context as contexts
import test_task_evidence as evidence
import test_task_runner as runner_helpers

from multi_agent.cli import task_candidates as boundary
from multi_agent.cli import workspaces
from multi_agent.cli.task_candidates import (
    freeze_task_candidate,
    inspect_task_candidate,
)
from multi_agent.cli.task_evidence import inspect_task_execution
from multi_agent.cli.workspaces import DeliveryRejected, executor_lock
from multi_agent.core import Disposition
from multi_agent.core.events import CancelRequested, Error
from multi_agent.core.task_events import (
    TaskCandidateFrozen,
    TaskDiagnostic,
    TaskEnvelope,
    TaskEvidenceRejected,
    TaskExecutionPrepared,
    TaskScopeBlocked,
)
from multi_agent.core.task_execution import task_execution_hash
from multi_agent.core.task_state import TaskPhase
from multi_agent.store.planning_store import PlanningStore

# Register old fixtures explicitly, without collecting their test functions.
project = contexts.project
bound = contexts.bound
live = evidence.live
execution = evidence.execution
setup = runner_helpers.setup
synthetic_codex_auth_and_fake_environment = (
    runner_helpers.synthetic_codex_auth_and_fake_environment
)


def rich_changes(launch):
    root = Path(launch.prepared.workspace.root)
    (root / "src/code.py").rename(root / "src/renamed.py")
    (root / "src/renamed.py").write_bytes(b"RENAMED = True\r\n")
    (root / "src/data.bin").write_bytes(b"\x00\xff\x80changed\r\n")
    (root / "src/do_not_run.py").chmod(0o644)
    (root / "src/internal-link").unlink()
    (root / "src/internal-link").symlink_to("renamed.py")
    (root / "src/nested").mkdir()
    (root / "src/nested/new.bin").write_bytes(b"\x00\xfe\n")
    (root / "src/safe-link").symlink_to("nested/new.bin")
    return (
        "src/code.py",
        "src/data.bin",
        "src/do_not_run.py",
        "src/internal-link",
        "src/nested/new.bin",
        "src/renamed.py",
        "src/safe-link",
    )


@pytest.fixture
def archive(execution):
    launch = execution.launch
    changed = rich_changes(launch)
    files = workspaces.inspect_workspace(launch.lease, launch.prepared.workspace)
    manifest = evidence.seal(execution)
    assert manifest.qualified and manifest.delivery_eligible
    assert evidence.reconcile(execution, manifest) == Disposition.APPLIED
    return SimpleNamespace(
        execution=execution,
        launch=launch,
        store=launch.store,
        lease=launch.lease,
        manifest=manifest,
        files=files,
        changed=changed,
    )


def freeze(value):
    return freeze_task_candidate(
        value.store, "implementation", value.launch.attempt, value.lease
    )


def inspect(value):
    return inspect_task_candidate(
        value.store, "implementation", value.launch.attempt, value.lease
    )


def persisted(value):
    return (
        value.store.load_task("implementation"),
        value.store.load_task_history("implementation"),
    )


def ref_path(value):
    return (
        value.lease.repository
        / "refs/multi-agent/candidates"
        / value.launch.prepared.workspace.owner_token
    )


def object_body(repo, kind, oid):
    return workspaces._git(repo, "cat-file", kind, oid)


def object_hash(kind, body, object_format):
    return hashlib.new(
        object_format, f"{kind} {len(body)}\0".encode("ascii") + body
    ).hexdigest()


def append_history(value, applied=False):
    event = (
        TaskDiagnostic(Error("independent append", raw_type="synthetic.history"))
        if applied
        else CancelRequested()
    )
    envelope = TaskEnvelope(value.launch.attempt, event, "history-race")
    disposition = value.store.apply_task("implementation", envelope)
    assert disposition == (Disposition.APPLIED if applied else Disposition.HISTORY_ONLY)
    return envelope, disposition


def assert_retained(value):
    assert ref_path(value).is_file()
    assert Path(value.manifest.path).is_file()
    assert Path(value.launch.prepared.workspace.root).is_dir()
    assert Path(value.launch.prepared.context.path).is_dir()
    assert value.lease.active


def test_candidate_boundary_public_interfaces():
    assert callable(freeze_task_candidate)
    assert callable(inspect_task_candidate)


def test_freeze_task_candidate_binds_exact_archive_and_git_objects(
    archive, monkeypatch
):
    original = boundary.freeze_delivery
    calls = []

    def actual(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(boundary, "freeze_delivery", actual)
    repo = archive.lease.repository
    source_head = workspaces._git(repo, "rev-parse", "HEAD")
    source_index = (repo / "index").read_bytes()
    before_state, before_history = persisted(archive)
    raw_manifest = Path(archive.manifest.path).read_bytes()
    event = freeze(archive)
    delivery = event.delivery
    assert len(calls) == 1
    assert calls[0][1] == archive.launch.prepared.workspace
    original_input = archive.store.load_task_input("implementation")
    assert calls[0][2:] == (original_input.card, original_input.frozen_rules)
    state, history = persisted(archive)
    assert state.phase == TaskPhase.AWAITING_DELIVERY and state.candidate_ready
    assert state.current_candidate == event
    assert state.current_attempt.runtime == before_state.current_attempt.runtime
    assert history[:-1] == before_history
    assert history[-1][0].event == event and history[-1][1] == Disposition.APPLIED
    assert delivery.changed_paths == archive.changed
    assert delivery.digest == archive.files.digest == archive.manifest.final_digest
    assert delivery.baseline == before_state.baseline_commit
    assert event.execution_log_path == archive.manifest.path
    assert event.execution_log_sha256 == archive.manifest.sha256
    assert (
        delivery.ref
        == f"refs/multi-agent/candidates/{archive.launch.prepared.workspace.owner_token}"
    )
    assert (
        workspaces._git(repo, "rev-parse", "--verify", delivery.ref).strip().decode()
        == delivery.candidate
    )
    assert workspaces._git(repo, "cat-file", "-t", delivery.candidate) == b"commit\n"
    body = object_body(repo, "commit", delivery.candidate)
    headers = body.split(b"\n\n", 1)[0].splitlines()
    assert [line for line in headers if line.startswith(b"parent ")] == [
        b"parent " + archive.launch.prepared.workspace.start_commit.encode()
    ]
    assert b"tree " + delivery.tree.encode() in headers
    object_format = archive.launch.prepared.workspace.object_format
    assert object_hash("commit", body, object_format) == delivery.candidate
    assert len(delivery.candidate) == (40 if object_format == "sha1" else 64)
    files = workspaces._baseline_files(repo, delivery.candidate)
    assert files == archive.files.files
    assert workspaces._inspection(files).digest == delivery.digest
    by_path = {item.path: item for item in files}
    assert by_path["src/data.bin"].content == b"\x00\xff\x80changed\r\n"
    assert by_path["src/do_not_run.py"].mode == "100644"
    assert by_path["src/safe-link"].mode == "120000"
    assert by_path["src/safe-link"].content == b"nested/new.bin"
    assert all(
        object_hash("blob", item.content, object_format) == item.oid for item in files
    )
    assert workspaces._git(repo, "rev-parse", "HEAD") == source_head
    assert (repo / "index").read_bytes() == source_index
    assert Path(archive.manifest.path).read_bytes() == raw_manifest
    assert inspect(archive) == event


@pytest.mark.parametrize("setup", ["claude", "codex"], indirect=True)
def test_task_runner_to_candidate_freeze(setup):
    result = asyncio.run(runner_helpers.execute(setup))
    assert result.delivery_ready
    assert len(setup.adapters) == len(setup.seals) == 1
    runner_helpers.assert_finished(setup)
    capture = setup.capture.read_bytes()
    with PlanningStore(setup.database) as store, executor_lock(setup.repo) as lease:
        manifest = inspect_task_execution(store, "implementation", 1, lease)
        event = freeze_task_candidate(store, "implementation", 1, lease)
        assert inspect_task_candidate(store, "implementation", 1, lease) == event
        assert event.delivery.changed_paths == ("src/implemented.txt",)
        assert event.delivery.digest == manifest.final_digest
        actual = workspaces._baseline_files(lease.repository, event.delivery.candidate)
        assert (
            next(item for item in actual if item.path == "src/implemented.txt").content
            == b"synthetic implementation\n"
        )
        assert (
            store.load_task("implementation").current_attempt.runtime
            == result.current_attempt.runtime
        )
    assert setup.capture.read_bytes() == capture
    assert len(setup.adapters) == len(setup.seals) == 1
    assert not (setup.repo / "src/implemented.txt").exists()
    assert planning_contexts.git(setup.repo, "status", "--porcelain") == ""
    assert planning_contexts.git(setup.repo, "rev-parse", "HEAD") == setup.baseline


def test_candidate_suffix_keeps_execution_archive_inspectable(bound):
    with contexts.launch(bound) as launch:
        execution = evidence.make(launch)
        rich_changes(launch)
        manifest = evidence.seal(execution)
        evidence.reconcile(execution, manifest)
        event = freeze_task_candidate(launch.store, "implementation", 1, launch.lease)
        assert evidence.inspect(execution) == manifest
        before = launch.store.load_task("implementation")
        before_history = launch.store.load_task_history("implementation")
        raw_logs = {p.name: p.read_bytes() for p in launch.log_dir.iterdir()}
    with PlanningStore(bound.database) as store, executor_lock(bound.repo) as lease:
        assert inspect_task_candidate(store, "implementation", 1, lease) == event
        assert inspect_task_execution(store, "implementation", 1, lease) == manifest
        assert store.load_task("implementation") == before
        assert store.load_task_history("implementation") == before_history
        assert {p.name: p.read_bytes() for p in launch.log_dir.iterdir()} == raw_logs
        assert before.current_attempt.runtime.reported_model is None
        assert before.current_attempt.runtime.reported_effort is None
        assert before.current_attempt.runtime.exit_code == -15
        assert lease.active


@pytest.mark.parametrize(
    "gate",
    [
        "legacy",
        "no-prepared",
        "unknown-stop",
        "no-digest",
        "no-anchor",
        "failed",
        "scope-blocked",
        "old-attempt",
        "future-attempt",
    ],
)
def test_unknown_stop_rejects_before_workspace_scan(archive, monkeypatch, gate):
    state = archive.store.load_task("implementation")
    current = state.current_attempt
    number = 1
    if gate == "legacy":
        current = replace(current, request=replace(current.request, managed=False))
    elif gate == "no-prepared":
        current = replace(
            current,
            events=tuple(
                e for e in current.events if not isinstance(e, TaskExecutionPrepared)
            ),
        )
    elif gate in {"unknown-stop", "no-digest", "no-anchor"}:
        changes = {
            "unknown-stop": {"process_stopped": False},
            "no-digest": {"worktree_digest": None},
            "no-anchor": {"log_path": None, "log_sha256": None},
        }[gate]
        evidence.emit(archive.launch, replace(current.reconciliation, **changes))
        state = archive.store.load_task("implementation")
        current = state.current_attempt
    elif gate == "failed":
        evidence.emit(archive.launch, TaskEvidenceRejected("archive rejected"))
        state = archive.store.load_task("implementation")
        current = state.current_attempt
    elif gate == "scope-blocked":
        evidence.emit(
            archive.launch, TaskScopeBlocked("wider scope", ("shared/api.py",))
        )
        state = archive.store.load_task("implementation")
        current = state.current_attempt
    elif gate == "old-attempt":
        current = replace(current, runtime=replace(current.runtime, number=2))
    else:
        number = 2
    state = replace(state, attempts=(current,))
    # Synthetic impossible states isolate early validation; no DB rewrite is claimed.
    monkeypatch.setattr(archive.store, "load_task", lambda run: state)

    def forbidden(*args, **kwargs):
        pytest.fail("ineligible state reached Git/workspace/archive reads")

    for name in (
        "_git",
        "_baseline_files",
        "freeze_delivery",
        "inspect_task_execution",
    ):
        monkeypatch.setattr(boundary, name, forbidden)
    before = archive.store.load_task_history("implementation")
    with pytest.raises(ValueError):
        freeze_task_candidate(archive.store, "implementation", number, archive.lease)
    assert archive.store.load_task_history("implementation") == before
    assert not ref_path(archive).exists()


@pytest.mark.parametrize(
    "gate", ["qualified", "delivery-eligible", "scope-report", "missing-report"]
)
def test_candidate_requires_actual_completed_archive(archive, monkeypatch, gate):
    actual = boundary.inspect_task_execution
    changes = {
        "qualified": {"qualified": False},
        "delivery-eligible": {"delivery_eligible": False},
        "scope-report": {
            "report": replace(archive.manifest.report, outcome="scope_blocked")
        },
        "missing-report": {"report": None},
    }[gate]

    def changed(*args):
        return replace(actual(*args), **changes)

    monkeypatch.setattr(boundary, "inspect_task_execution", changed)
    before = persisted(archive)
    with pytest.raises(ValueError):
        freeze(archive)
    assert persisted(archive) == before
    assert not ref_path(archive).exists()


@pytest.mark.parametrize(
    "when", ["archive-before", "archive-after", "freeze", "record"]
)
@pytest.mark.parametrize("applied", [False, True])
def test_candidate_freeze_uses_original_history_cas(
    archive, monkeypatch, when, applied
):
    original_history = archive.store.load_task_history("implementation")
    inserted = []

    def add_once():
        if not inserted:
            inserted.append(append_history(archive, applied))

    if when.startswith("archive"):
        actual = boundary.inspect_task_execution

        def inspect_changed(*args):
            if when == "archive-before":
                add_once()
            result = actual(*args)
            if when == "archive-after":
                add_once()
            return result

        monkeypatch.setattr(boundary, "inspect_task_execution", inspect_changed)
    elif when == "freeze":
        actual = boundary.freeze_delivery

        def freeze_changed(*args):
            add_once()
            return actual(*args)

        monkeypatch.setattr(boundary, "freeze_delivery", freeze_changed)
    else:
        actual = archive.store.record_task_candidate

        def record_changed(*args, **kwargs):
            assert kwargs["expected_history_length"] == len(original_history)
            assert kwargs["expected_history_hash"] == task_execution_hash(
                original_history
            )
            add_once()
            return actual(*args, **kwargs)

        monkeypatch.setattr(archive.store, "record_task_candidate", record_changed)
    with pytest.raises(ValueError):
        freeze(archive)
    assert len(inserted) == 1
    assert archive.store.load_task_history("implementation") == (
        *original_history,
        inserted[0],
    )
    assert archive.store.load_task("implementation").current_candidate is None
    assert ref_path(archive).exists() is (when in {"freeze", "record"})
    assert archive.lease.active


def test_candidate_final_inspection_cannot_refresh_original_history(
    archive, monkeypatch
):
    actual = boundary.inspect_task_candidate
    calls = []

    def append_before_public_inspect(*args):
        committed = archive.store.load_task("implementation").current_candidate
        assert committed is not None
        calls.append(append_history(archive))
        # The public inspector can verify its own fresh H; the outer freezer
        # must still reject the change from its original H plus exact record.
        return actual(*args)

    monkeypatch.setattr(
        boundary, "inspect_task_candidate", append_before_public_inspect
    )
    with pytest.raises(ValueError):
        freeze(archive)
    assert len(calls) == 1
    current, history = persisted(archive)
    assert current.current_candidate is not None and current.candidate_ready
    assert history[-1] == calls[0]
    assert isinstance(history[-2][0].event, TaskCandidateFrozen)
    assert history[-2][1] == Disposition.APPLIED
    assert_retained(archive)


@pytest.mark.parametrize(
    "failure_at", ["precommit", "cas", "history-only", "final-inspect"]
)
def test_candidate_git_success_database_failure_retains_orphan(
    archive, monkeypatch, failure_at
):
    failure = sqlite3.OperationalError("candidate recording host failure")
    original = archive.store.record_task_candidate
    seen = []

    def changed(*args, **kwargs):
        seen.append(args[1])
        assert ref_path(archive).is_file()
        if failure_at == "precommit":
            raise failure
        if failure_at == "cas":
            append_history(archive)
            return original(*args, **kwargs)
        if failure_at == "history-only":
            return Disposition.HISTORY_ONLY
        return original(*args, **kwargs)

    def fail_inspect(*args):
        raise failure

    monkeypatch.setattr(archive.store, "record_task_candidate", changed)
    if failure_at == "final-inspect":
        monkeypatch.setattr(boundary, "inspect_task_candidate", fail_inspect)
    expected = (
        sqlite3.OperationalError
        if failure_at in {"precommit", "final-inspect"}
        else ValueError
    )
    with pytest.raises(expected) as caught:
        freeze(archive)
    if expected is sqlite3.OperationalError:
        assert caught.value is failure
    assert len(seen) == 1
    assert_retained(archive)
    current = archive.store.load_task("implementation")
    assert (current.current_candidate is not None) is (failure_at == "final-inspect")
    before = persisted(archive)
    ref = ref_path(archive).read_bytes()
    with pytest.raises(ValueError):
        freeze(archive)
    assert persisted(archive) == before and ref_path(archive).read_bytes() == ref
    assert len(seen) == 1


@pytest.mark.parametrize(
    "committed,read_fails", [(True, False), (False, False), (True, True)]
)
def test_candidate_commit_then_raise_reports_actual_binding_and_original_error(
    archive, monkeypatch, committed, read_fails
):
    first = sqlite3.OperationalError("original candidate commit exception")
    second = OSError("history readback failed")
    original_record = archive.store.record_task_candidate
    original_history = archive.store.load_task_history
    post_error = False
    reads = []
    attempted = []

    def history(run_id):
        if post_error:
            reads.append(run_id)
            if read_fails:
                raise second
        return original_history(run_id)

    def record(*args, **kwargs):
        nonlocal post_error
        attempted.append(args[1])
        if committed:
            assert original_record(*args, **kwargs) == Disposition.APPLIED
        post_error = True
        raise first

    with monkeypatch.context() as patch:
        patch.setattr(archive.store, "record_task_candidate", record)
        patch.setattr(archive.store, "load_task_history", history)
        with pytest.raises(sqlite3.OperationalError) as caught:
            freeze(archive)
        assert caught.value is first
    assert reads == ["implementation"]
    assert len(attempted) == 1
    stored = archive.store.load_task_history("implementation")
    assert ((attempted[0], Disposition.APPLIED) in stored) is committed
    notes = "\n".join(getattr(first, "__notes__", ())).lower()
    assert notes
    if read_fails:
        assert "unknown" in notes and "history readback failed" in notes
    elif committed:
        assert "applied" in notes and ("bound" in notes or "binding" in notes)
    else:
        assert "unconfirmed" in notes or "not confirmed" in notes
    assert_retained(archive)


@pytest.mark.parametrize("existing", ["bound", "revoked", "orphan"])
def test_candidate_existing_binding_or_orphan_never_refreezes(
    archive, monkeypatch, existing
):
    if existing == "orphan":
        value = archive.store.load_task_input("implementation")
        workspaces.freeze_delivery(
            archive.lease,
            archive.launch.prepared.workspace,
            value.card,
            value.frozen_rules,
        )
    else:
        freeze(archive)
        if existing == "revoked":
            current = archive.store.load_task("implementation").current_attempt
            evidence.emit(
                archive.launch, replace(current.reconciliation, process_stopped=False)
            )
    before = persisted(archive)
    ref = ref_path(archive).read_bytes()

    def forbidden(*args):
        pytest.fail("existing binding/ref reached freeze_delivery")

    monkeypatch.setattr(boundary, "freeze_delivery", forbidden)
    with pytest.raises(ValueError):
        freeze(archive)
    assert persisted(archive) == before and ref_path(archive).read_bytes() == ref
    if existing == "bound":
        assert inspect(archive) == before[0].current_candidate


@pytest.mark.parametrize(
    "tamper", ["workspace", "context", "rawlog", "manifest", "reconciliation", "ref"]
)
def test_candidate_inspect_rechecks_every_consumption(archive, tamper):
    event = freeze(archive)
    assert inspect(archive) == event
    if tamper == "workspace":
        (Path(archive.launch.prepared.workspace.root) / "src/data.bin").write_bytes(
            b"changed after freeze"
        )
    elif tamper == "context":
        contexts.replace_bytes(
            Path(archive.launch.prepared.context.path) / "prompt.txt", b"changed prompt"
        )
    elif tamper == "rawlog":
        with (archive.launch.log_dir / "turn.jsonl").open("ab") as stream:
            stream.write(b"{}\n")
    elif tamper == "manifest":
        contexts.replace_bytes(Path(archive.manifest.path), b"{}")
    elif tamper == "reconciliation":
        current = archive.store.load_task("implementation").current_attempt
        evidence.emit(
            archive.launch, replace(current.reconciliation, process_stopped=False)
        )
    else:
        workspaces._git(
            archive.lease.repository,
            "update-ref",
            event.delivery.ref,
            event.delivery.baseline,
        )
    before = persisted(archive)
    with pytest.raises(ValueError):
        inspect(archive)
    assert persisted(archive) == before
    assert Path(archive.manifest.path).exists() and archive.lease.active


@pytest.mark.parametrize(
    "tamper",
    [
        "closed-lease",
        "replaced-lock",
        "wrong-run",
        "database",
        "parent",
        "context",
        "database-after-archive",
    ],
)
def test_candidate_database_context_and_lease_replacement_are_rejected(
    archive, monkeypatch, tamper
):
    database = Path(
        archive.store._connection.execute("PRAGMA database_list").fetchone()[2]
    )
    run_id = "implementation"
    if tamper == "closed-lease":
        archive.lease.active = False
    elif tamper == "replaced-lock":
        lock = archive.lease.repository / "multi-agent-executor.lock"
        lock.rename(lock.with_name("retained-lock"))
        lock.write_bytes(b"replacement")
    elif tamper == "wrong-run":
        run_id = "missing"
    elif tamper == "database":
        database.rename(database.with_suffix(".retained"))
        shutil.copyfile(database.with_suffix(".retained"), database)
    elif tamper == "parent":
        # Keep every child inode, including the live DB and Git common-dir,
        # while replacing only their parent directory's identity.
        parent = database.parent
        retained = parent.with_name(parent.name + "-retained")
        children = {
            child.name: (child.stat().st_dev, child.stat().st_ino)
            for child in parent.iterdir()
        }
        parent.rename(retained)
        parent.mkdir()
        for child in retained.iterdir():
            child.rename(parent / child.name)
        assert {
            child.name: (child.stat().st_dev, child.stat().st_ino)
            for child in parent.iterdir()
        } == children
    elif tamper == "context":
        context = Path(archive.launch.prepared.context.path)
        context.rename(context.with_name("retained-context"))
        shutil.copytree(context.with_name("retained-context"), context)
    else:
        original = boundary.inspect_task_execution
        changed = False

        def inspect_then_replace(*args):
            nonlocal changed
            result = original(*args)
            if not changed:
                changed = True
                database.rename(database.with_suffix(".retained"))
                shutil.copyfile(database.with_suffix(".retained"), database)
            return result

        monkeypatch.setattr(boundary, "inspect_task_execution", inspect_then_replace)
    before = persisted(archive)
    expected = KeyError if tamper == "wrong-run" else ValueError
    try:
        with pytest.raises(expected):
            freeze_task_candidate(archive.store, run_id, 1, archive.lease)
        assert persisted(archive) == before
    finally:
        if tamper == "closed-lease":
            archive.lease.active = True


@pytest.mark.parametrize(
    "representation", ["direct", "packed", "packed-missing-namespace"]
)
def test_candidate_git_ref_tree_parent_and_paths_are_exact_valid_refs(
    archive, representation
):
    event = freeze(archive)
    if representation != "direct":
        workspaces._git(archive.lease.repository, "pack-refs", "--all", "--prune")
        assert not ref_path(archive).exists()
        if representation == "packed-missing-namespace":
            for directory in (
                ref_path(archive).parent,
                ref_path(archive).parent.parent,
            ):
                if directory.exists():
                    directory.rmdir()
                assert not directory.exists()
    before = persisted(archive)
    assert inspect(archive) == event
    assert persisted(archive) == before
    if representation == "packed-missing-namespace":
        assert not ref_path(archive).parent.exists()
        assert not ref_path(archive).parent.parent.exists()


@pytest.mark.parametrize(
    "tamper",
    [
        "symbolic",
        "dangling",
        "symlink",
        "ancestor-symlink",
        "packed-symlink",
        "garbage-loose-over-packed",
        "prefix",
        "extra-line",
        "missing-ref",
        "different-commit",
        "tag",
    ],
)
def test_candidate_git_ref_tree_parent_and_paths_are_exact_refs(
    archive, monkeypatch, tamper
):
    event = freeze(archive)
    repo = archive.lease.repository
    path = ref_path(archive)
    external = (
        Path(archive.launch.prepared.context.path).parent.parent / "external-ref-canary"
    )
    external.write_bytes(event.delivery.candidate.encode() + b"\n")
    if tamper == "symbolic":
        path.write_text("ref: refs/heads/master\n")
    elif tamper == "dangling":
        path.write_text("ref: refs/heads/missing\n")
    elif tamper == "symlink":
        path.unlink()
        path.symlink_to(external)
    elif tamper == "ancestor-symlink":
        parent = path.parent
        parent.rename(parent.with_name("candidate-ref-backup"))
        parent.symlink_to(
            parent.with_name("candidate-ref-backup"), target_is_directory=True
        )
    elif tamper == "packed-symlink":
        workspaces._git(repo, "pack-refs", "--all", "--prune")
        packed = repo / "packed-refs"
        external.write_bytes(packed.read_bytes())
        packed.unlink()
        packed.symlink_to(external)
    elif tamper == "garbage-loose-over-packed":
        workspaces._git(repo, "pack-refs", "--all", "--prune")
        assert not path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not-a-reference\n")
    elif tamper == "prefix":
        path.write_text(event.delivery.candidate[:12] + "\n")
    elif tamper == "extra-line":
        path.write_text(event.delivery.candidate + "\nextra-content\n")
    elif tamper == "missing-ref":
        path.unlink()
    elif tamper == "different-commit":
        workspaces._git(repo, "update-ref", event.delivery.ref, event.delivery.baseline)
    else:
        body = (
            f"object {event.delivery.candidate}\ntype commit\ntag candidate-tag\n"
            "tagger Fixture <fixture@example.invalid> 1 +0000\n\nnot a direct commit\n"
        ).encode()
        oid = workspaces._git(repo, "mktag", input_bytes=body).strip().decode()
        workspaces._git(repo, "update-ref", event.delivery.ref, oid)
    original_open = os.open

    def no_external(file, *args, **kwargs):
        if not isinstance(file, int) and Path(file).is_absolute():
            assert Path(file) != external, "boundary opened the external canary"
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr(os, "open", no_external)
    before = persisted(archive)
    with pytest.raises(ValueError):
        inspect(archive)
    assert persisted(archive) == before
    assert external.is_file()


@pytest.mark.parametrize(
    "field",
    [
        "ref",
        "candidate-prefix",
        "object-type",
        "tree",
        "parent",
        "two-parents",
        "digest",
        "changed-paths",
        "baseline",
    ],
)
def test_candidate_git_ref_tree_parent_and_paths_are_exact_delivery(
    archive, monkeypatch, field
):
    original = boundary.freeze_delivery

    def changed(*args):
        result = original(*args)
        if field == "ref":
            return replace(result, ref=result.ref + "-other")
        if field == "candidate-prefix":
            return replace(result, candidate=result.candidate[:12])
        if field == "object-type":
            body = (
                f"object {result.candidate}\ntype commit\ntag candidate-tag\n"
                "tagger Fixture <fixture@example.invalid> 1 +0000\n\nnot a commit\n"
            ).encode()
            oid = (
                workspaces._git(archive.lease.repository, "mktag", input_bytes=body)
                .strip()
                .decode()
            )
            workspaces._git(
                archive.lease.repository,
                "update-ref",
                result.ref,
                oid,
                result.candidate,
            )
            return replace(result, candidate=oid)
        if field == "tree":
            tree = (
                workspaces._git(
                    archive.lease.repository, "rev-parse", f"{result.baseline}^{{tree}}"
                )
                .strip()
                .decode()
            )
            return replace(result, tree=tree)
        if field in {"parent", "two-parents"}:
            parent_args = (
                ()
                if field == "parent"
                else ("-p", result.baseline, "-p", result.candidate)
            )
            oid = (
                workspaces._git(
                    archive.lease.repository,
                    "commit-tree",
                    result.tree,
                    *parent_args,
                    "-m",
                    "wrong parent shape",
                )
                .strip()
                .decode()
            )
            workspaces._git(
                archive.lease.repository,
                "update-ref",
                result.ref,
                oid,
                result.candidate,
            )
            return replace(result, candidate=oid)
        if field == "digest":
            return replace(result, digest="different")
        if field == "changed-paths":
            return replace(result, changed_paths=result.changed_paths[:-1])
        return replace(result, baseline=result.candidate)

    monkeypatch.setattr(boundary, "freeze_delivery", changed)
    before = persisted(archive)
    with pytest.raises(ValueError):
        freeze(archive)
    assert persisted(archive) == before
    assert_retained(archive)


@pytest.mark.parametrize("path", ["shared/outside.py", ".agents/protected.json"])
def test_candidate_git_ref_tree_parent_and_paths_are_exact_scope(
    execution, monkeypatch, path
):
    launch = execution.launch
    target = Path(launch.prepared.workspace.root) / path
    target.parent.mkdir(parents=True)
    target.write_bytes(b"forbidden implementation change\n")
    manifest = evidence.seal(execution)
    assert manifest.qualified and manifest.delivery_eligible
    evidence.reconcile(execution, manifest)
    value = SimpleNamespace(
        launch=launch, store=launch.store, lease=launch.lease, manifest=manifest
    )
    original = boundary.freeze_delivery

    def permissive_freezer(lease, workspace, card, rules):
        # Simulate a faulty freezer while preserving real objects and the exact
        # archive digest. The candidate boundary must independently apply the
        # original frozen card and protected paths to the actual changed files.
        permissive_card = replace(card, write_scope=("**",))
        permissive_rules = replace(
            rules, rules=replace(rules.rules, protected_paths=())
        )
        return original(lease, workspace, permissive_card, permissive_rules)

    monkeypatch.setattr(boundary, "freeze_delivery", permissive_freezer)
    before = persisted(value)
    with pytest.raises(DeliveryRejected) as caught:
        freeze(value)
    assert caught.value.paths == (path,)
    assert persisted(value) == before
    assert_retained(value)


def reverse_tree_entries(body, oid_bytes):
    entries = []
    offset = 0
    while offset < len(body):
        separator = body.index(b"\0", offset)
        end = separator + 1 + oid_bytes
        entries.append(body[offset:end])
        offset = end
    assert len(entries) > 1 and offset == len(body)
    result = b"".join(reversed(entries))
    assert result != body
    return result


@pytest.mark.parametrize(
    "tamper",
    [
        "candidate-message",
        "candidate-author",
        "baseline-message",
        "baseline-blob",
        "candidate-blob",
        "nested-tree",
        "baseline-tree",
    ],
)
def test_candidate_object_content_addressing_is_verified(archive, tamper):
    event = freeze(archive)
    repo = archive.lease.repository
    object_format = archive.launch.prepared.workspace.object_format
    if tamper.startswith("candidate-") and tamper != "candidate-blob":
        kind, oid = "commit", event.delivery.candidate
        body = object_body(repo, kind, oid)
        if tamper == "candidate-message":
            headers, message = body.split(b"\n\n", 1)
            changed = headers + b"\n\n" + message + b"modified only message\n"
        else:
            changed = body.replace(b"author Multi-agent", b"author Other-agent", 1)
            assert changed != body
    elif tamper == "baseline-message":
        kind, oid = "commit", event.delivery.baseline
        body = object_body(repo, kind, oid)
        changed = body + b"modified baseline message\n"
    elif tamper in {"baseline-blob", "candidate-blob"}:
        revision, path = (
            (event.delivery.baseline, "src/code.py")
            if tamper == "baseline-blob"
            else (event.delivery.candidate, "src/data.bin")
        )
        kind = "blob"
        oid = workspaces._git(repo, "rev-parse", f"{revision}:{path}").strip().decode()
        body = object_body(repo, kind, oid)
        changed = body + b"modified raw blob\n"
    else:
        revision = (
            event.delivery.candidate
            if tamper == "nested-tree"
            else event.delivery.baseline
        )
        oid = workspaces._git(repo, "rev-parse", f"{revision}:src").strip().decode()
        kind = "tree"
        body = object_body(repo, kind, oid)
        changed = reverse_tree_entries(body, hashlib.new(object_format).digest_size)
    assert object_hash(kind, body, object_format) == oid
    assert object_hash(kind, changed, object_format) != oid
    path = repo / "objects" / oid[:2] / oid[2:]
    assert path.is_file()
    path.chmod(0o600)
    path.write_bytes(zlib.compress(f"{kind} {len(changed)}\0".encode() + changed))
    path.chmod(0o444)
    # cat-file can still read well-formed bytes under the old filename. Merely
    # trusting its echoed requested OID is insufficient evidence of identity.
    assert object_body(repo, kind, oid) == changed
    before = persisted(archive)
    with pytest.raises(ValueError):
        inspect(archive)
    assert persisted(archive) == before
    assert path.is_file() and ref_path(archive).is_file()


def test_candidate_verifies_actual_empty_tree_objects(archive, monkeypatch):
    original = boundary.freeze_delivery
    repo = archive.lease.repository
    empty = workspaces._git(repo, "mktree", input_bytes=b"").strip().decode()

    def with_empty_tree(*args):
        delivery = original(*args)
        listing = workspaces._git(repo, "ls-tree", delivery.tree)
        listing += f"040000 tree {empty}\tempty-tree\n".encode()
        tree = workspaces._git(repo, "mktree", input_bytes=listing).strip().decode()
        candidate = (
            workspaces._git(
                repo,
                "commit-tree",
                tree,
                "-p",
                delivery.baseline,
                "-m",
                "real empty subtree",
            )
            .strip()
            .decode()
        )
        workspaces._git(repo, "update-ref", delivery.ref, candidate, delivery.candidate)
        return replace(delivery, tree=tree, candidate=candidate)

    monkeypatch.setattr(boundary, "freeze_delivery", with_empty_tree)
    event = freeze(archive)
    assert inspect(archive) == event
    assert (
        workspaces._git(repo, "rev-parse", f"{event.delivery.candidate}:empty-tree")
        .strip()
        .decode()
        == empty
    )
    assert object_body(repo, "tree", empty) == b""


def test_candidate_ambiguous_commit_note_requires_exact_envelope(archive, monkeypatch):
    failure = sqlite3.OperationalError("committed a different occurrence")
    original = archive.store.record_task_candidate
    attempted = []

    def different_occurrence(run_id, envelope, **kwargs):
        attempted.append(envelope)
        changed = replace(envelope, occurred_at="a different exact occurrence")
        assert original(run_id, changed, **kwargs) == Disposition.APPLIED
        raise failure

    monkeypatch.setattr(archive.store, "record_task_candidate", different_occurrence)
    with pytest.raises(sqlite3.OperationalError) as caught:
        freeze(archive)
    assert caught.value is failure
    history = archive.store.load_task_history("implementation")
    assert history[-1][0].event == attempted[0].event
    assert history[-1][0] != attempted[0]
    notes = "\n".join(getattr(failure, "__notes__", ())).lower()
    assert "not confirmed" in notes and "exact" in notes
    assert "confirmed committed" not in notes
    assert archive.store.load_task("implementation").current_candidate is not None
    assert_retained(archive)


@pytest.mark.parametrize(
    "control", ["packed-symlink", "loose-symlink", "namespace-symlink"]
)
def test_candidate_ref_controls_are_checked_before_actual_archive(
    archive, monkeypatch, control
):
    repo = archive.lease.repository
    target = repo.parent / "external-ref-control"
    target.write_bytes(b"must not be followed\n")
    if control == "packed-symlink":
        (repo / "packed-refs").symlink_to(target)
    elif control == "loose-symlink":
        ref_path(archive).parent.mkdir(parents=True)
        ref_path(archive).symlink_to(target)
    else:
        namespace = ref_path(archive).parent
        namespace.parent.mkdir(parents=True)
        namespace.symlink_to(repo.parent, target_is_directory=True)

    def no_archive(*args):
        pytest.fail("unsafe refs reached actual execution archive Git reads")

    monkeypatch.setattr(boundary, "inspect_task_execution", no_archive)
    before = persisted(archive)
    with pytest.raises(ValueError):
        freeze(archive)
    assert persisted(archive) == before
    assert target.read_bytes() == b"must not be followed\n"


@pytest.mark.parametrize("operation", ["freeze", "inspect"])
@pytest.mark.parametrize("control", ["packed", "namespace"])
def test_candidate_first_archive_cannot_refresh_ref_identity(
    archive, monkeypatch, operation, control
):
    if operation == "inspect":
        freeze(archive)
    workspaces._git(archive.lease.repository, "pack-refs", "--all", "--prune")
    if control == "namespace":
        ref_path(archive).parent.mkdir(parents=True, exist_ok=True)
    original = boundary.inspect_task_execution
    changed = False

    def inspect_then_replace(*args):
        nonlocal changed
        result = original(*args)
        if not changed:
            changed = True
            path = (
                archive.lease.repository / "packed-refs"
                if control == "packed"
                else ref_path(archive).parent
            )
            retained = path.with_name(path.name + "-retained")
            identity = (path.stat().st_dev, path.stat().st_ino)
            path.rename(retained)
            if control == "packed":
                shutil.copyfile(retained, path)
                assert path.read_bytes() == retained.read_bytes()
            else:
                shutil.copytree(retained, path)
            assert (path.stat().st_dev, path.stat().st_ino) != identity
        return result

    monkeypatch.setattr(boundary, "inspect_task_execution", inspect_then_replace)
    before = persisted(archive)
    with pytest.raises(ValueError):
        (freeze if operation == "freeze" else inspect)(archive)
    assert changed and persisted(archive) == before


@pytest.mark.parametrize("resource", ["database", "lease"])
def test_candidate_last_business_read_cannot_hide_identity_replacement(
    archive, monkeypatch, resource
):
    actual_inspect = boundary.inspect_task_candidate
    actual_history = archive.store.load_task_history
    armed = False
    replaced = False

    def inspected(*args):
        nonlocal armed
        result = actual_inspect(*args)
        armed = True
        return result

    def history_then_replace(run_id):
        nonlocal replaced
        result = actual_history(run_id)
        if armed and not replaced:
            replaced = True
            path = (
                Path(
                    archive.store._connection.execute(
                        "PRAGMA database_list"
                    ).fetchone()[2]
                )
                if resource == "database"
                else archive.lease.repository / "multi-agent-executor.lock"
            )
            retained = path.with_name(path.name + "-retained")
            path.rename(retained)
            shutil.copyfile(retained, path)
        return result

    monkeypatch.setattr(boundary, "inspect_task_candidate", inspected)
    monkeypatch.setattr(archive.store, "load_task_history", history_then_replace)
    with pytest.raises(ValueError):
        freeze(archive)
    assert armed and replaced
    assert archive.store.load_task("implementation").current_candidate is not None
    assert_retained(archive)


@pytest.mark.parametrize(
    "tamper", [None, "commit", "tree", "blob", "prefix", "wrong-format"]
)
def test_candidate_sha256_git_object_layer(tmp_path, tamper):
    """Real SHA256 objects, not a claim that the 40-digit planning schema supports them."""
    repo = tmp_path / "sha256-repository"
    repo.mkdir()
    planning_contexts.git(
        repo, "init", "--quiet", "--template=", "--object-format=sha256"
    )
    (repo / "nested").mkdir()
    (repo / "nested/data.bin").write_bytes(b"\x00\xffactual sha256 blob\n")
    (repo / "nested/second.txt").write_bytes(b"second entry\n")
    (repo / "safe-link").symlink_to("nested/data.bin")
    planning_contexts.git(repo, "add", ".")
    planning_contexts.git(repo, "commit", "--quiet", "-m", "actual sha256 baseline")
    baseline = planning_contexts.git(repo, "rev-parse", "HEAD")
    (repo / "nested/second.txt").write_bytes(b"actual candidate bytes\n")
    planning_contexts.git(repo, "add", ".")
    planning_contexts.git(repo, "commit", "--quiet", "-m", "actual sha256 candidate")
    candidate = planning_contexts.git(repo, "rev-parse", "HEAD")
    assert len(baseline) == len(candidate) == 64
    tree, parents, files = boundary._revision(repo, candidate, "sha256")
    assert parents == (baseline,)
    assert files == workspaces._baseline_files(repo, candidate)
    assert all(
        object_hash("blob", item.content, "sha256") == item.oid for item in files
    )
    for kind, oid in (("commit", candidate), ("tree", tree)):
        body = object_body(repo, kind, oid)
        assert object_hash(kind, body, "sha256") == oid
        assert boundary._objects(repo, (oid,), kind, "sha256") == (body,)
    if tamper is None:
        return
    if tamper in {"prefix", "wrong-format"}:
        with pytest.raises(ValueError):
            boundary._revision(
                repo,
                candidate[:40] if tamper == "prefix" else candidate,
                "sha256" if tamper == "prefix" else "sha1",
            )
        return
    kind = tamper
    oid = (
        candidate
        if kind == "commit"
        else tree
        if kind == "tree"
        else next(item.oid for item in files if item.path == "nested/data.bin")
    )
    body = object_body(repo, kind, oid)
    changed = (
        reverse_tree_entries(body, 32)
        if kind == "tree"
        else body + b"changed actual bytes\n"
    )
    path = repo / ".git/objects" / oid[:2] / oid[2:]
    path.chmod(0o600)
    path.write_bytes(zlib.compress(f"{kind} {len(changed)}\0".encode() + changed))
    path.chmod(0o444)
    assert object_body(repo, kind, oid) == changed
    assert object_hash(kind, changed, "sha256") != oid
    with pytest.raises(ValueError):
        boundary._revision(repo, candidate, "sha256")
