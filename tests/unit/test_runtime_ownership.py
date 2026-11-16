"""Ownership receipts describe actual local resources without repairing them."""

import hashlib
import json
import os
from dataclasses import FrozenInstanceError, asdict, replace
from pathlib import Path

import pytest

from multi_agent.adapters import ownership
from multi_agent.adapters.ownership import (
    DirectoryOwnership,
    IsolationOwnership,
    LogOwnership,
    OwnedTemporaryDirectory,
    OwnershipLedger,
    ProcessOwnership,
    RuntimeOwnershipReceipt,
)
from multi_agent.adapters.probe import recording
from multi_agent.adapters.probe.recording import Recorder, Redactor

SENTINEL = "private-payload-and-error-sentinel"


def make_recorder(tmp_path, monkeypatch, *, ledger=None):
    monkeypatch.setattr(recording, "monotonic", lambda: 100.0)
    path = tmp_path / "runtime.jsonl"
    redactor = Redactor()
    redactor.add_secret(SENTINEL)
    acquisition = ledger.acquire("log") if ledger is not None else None
    recorder = Recorder(path, redactor, acquisition=acquisition)
    return path, recorder


def test_recorder_receipt_rejects_unsealed_or_changed_logs(tmp_path, monkeypatch):
    path, recorder = make_recorder(tmp_path, monkeypatch)
    recorder.emit("recv", text="中文\r\n" + SENTINEL, proc="synthetic")
    expected = (
        json.dumps(
            {
                "text": "中文\r\n<REDACTED>",
                "proc": "synthetic",
                "seq": 1,
                "t": 0.0,
                "kind": "recv",
            },
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")
    assert path.read_bytes() == expected

    def forbidden_read(*args, **kwargs):
        pytest.fail("ownership receipt must not open or hash a log still being written")

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", forbidden_read)
        patch.setattr(os, "read", forbidden_read)
        opened = recorder.ownership_record("log-1")
    assert opened.healthy and not opened.closed and not opened.sealed
    assert opened.byte_count is None and opened.sha256 is None
    recorder.close()
    sealed = recorder.ownership_record("log-1")
    assert sealed.closed and sealed.healthy and sealed.sealed
    assert sealed.records == recorder.count == 1
    assert sealed.identity == (path.stat().st_dev, path.stat().st_ino)
    assert sealed.byte_count == len(expected)
    assert sealed.sha256 == hashlib.sha256(expected).hexdigest()
    assert SENTINEL not in repr(asdict(sealed))
    assert set(asdict(sealed)) == {
        "resource_id",
        "path",
        "identity",
        "records",
        "closed",
        "healthy",
        "byte_count",
        "sha256",
        "sealed",
    }
    assert not opened.closed and opened.byte_count is None


@pytest.mark.parametrize(
    "change",
    [
        "append",
        "truncate",
        "replace",
        "symlink",
        "same-inode",
        "mode",
        "missing",
        "fifo",
    ],
)
def test_recorder_receipt_detects_each_closed_file_tamper(
    tmp_path, monkeypatch, change
):
    path, recorder = make_recorder(tmp_path, monkeypatch)
    recorder.emit("event", text="original bytes")
    recorder.close()
    original = path.read_bytes()
    initial_identity = (path.stat().st_dev, path.stat().st_ino)
    mode = path.stat().st_mode & 0o777
    if change == "append":
        with path.open("ab") as stream:
            stream.write(b"extra bytes")
    elif change == "truncate":
        path.write_bytes(original[:-1])
    elif change == "replace":
        replacement = tmp_path / "replacement"
        replacement.write_bytes(original)
        replacement.chmod(mode)
        replacement.replace(path)
    elif change == "symlink":
        replacement = tmp_path / "replacement"
        replacement.write_bytes(original)
        path.unlink()
        path.symlink_to(replacement)
    elif change == "same-inode":
        path.write_bytes(original.replace(b"original", b"modified"))
        assert (path.stat().st_dev, path.stat().st_ino) == initial_identity
        assert path.stat().st_size == len(original)
    elif change == "mode":
        path.chmod(mode ^ 0o100)
    else:
        path.unlink()
        if change == "fifo":
            os.mkfifo(path)
    receipt = recorder.ownership_record("log")
    assert receipt.closed and not receipt.sealed and not receipt.healthy
    assert receipt.identity == initial_identity
    assert receipt.byte_count is None and receipt.sha256 is None
    if change == "same-inode":
        path.write_bytes(original)
        recorder.close()
        assert not recorder.ownership_record("log").healthy


def test_recorder_receipt_rejects_mutation_during_closed_file_read(
    tmp_path, monkeypatch
):
    path, recorder = make_recorder(tmp_path, monkeypatch)
    recorder.emit("event", text="observed original")
    recorder.close()
    original = path.read_bytes()
    read = os.read
    changed = False

    def mutate_after_read(fd, size):
        nonlocal changed
        block = read(fd, size)
        if block and not changed:
            changed = True
            path.write_bytes(original + b"appended while reading")
        return block

    monkeypatch.setattr(os, "read", mutate_after_read)
    receipt = recorder.ownership_record("log")
    assert changed and not receipt.sealed and not receipt.healthy


class FailingStream:
    def __init__(self, stream, failure):
        self.stream = stream
        self.failure = failure

    @property
    def closed(self):
        return self.stream.closed

    def write(self, data):
        if self.failure == "write":
            self.stream.write(data[:3])
            raise OSError(SENTINEL)
        if self.failure == "short-write":
            self.stream.write(data[:-1])
            return len(data) - 1
        return self.stream.write(data)

    def flush(self):
        if self.failure == "flush":
            raise OSError(SENTINEL)
        self.stream.flush()

    def close(self):
        if self.failure == "close-before":
            raise OSError(SENTINEL)
        self.stream.close()
        if self.failure == "close-after":
            raise OSError(SENTINEL)


@pytest.mark.parametrize(
    "failure", ["write", "short-write", "flush", "close-before", "close-after"]
)
def test_recorder_io_failure_is_sticky_after_close_or_file_restoration(
    tmp_path, monkeypatch, failure
):
    path, recorder = make_recorder(tmp_path, monkeypatch)
    original_stream = recorder._stream
    if failure.startswith("close"):
        recorder.emit("event", text=SENTINEL)
    recorder._stream = FailingStream(original_stream, failure)
    with pytest.raises(OSError):
        if failure.startswith("close"):
            recorder.close()
        else:
            recorder.emit("event", text=SENTINEL)
    failed = recorder.ownership_record("log")
    assert not failed.healthy and not failed.sealed
    assert failed.records == (1 if failure.startswith("close") else 0)
    assert failed.closed == (failure == "close-after")
    assert SENTINEL not in repr(asdict(failed))
    recorder._stream = original_stream
    recorder.close()
    if not failure.startswith("close"):
        path.write_bytes(b"")
    recovered_file = recorder.ownership_record("log")
    assert (
        recovered_file.closed
        and not recovered_file.healthy
        and not recovered_file.sealed
    )
    assert recovered_file.byte_count is None and recovered_file.sha256 is None


@pytest.mark.parametrize("payload", [object(), "\ud800"])
def test_recorder_serialization_failure_cannot_be_erased_by_later_success(
    tmp_path, monkeypatch, payload
):
    path, recorder = make_recorder(tmp_path, monkeypatch)
    with pytest.raises((TypeError, UnicodeEncodeError)):
        recorder.emit("bad", value=payload, text=SENTINEL)
    assert recorder.count == 0 and path.read_bytes() == b""
    recorder.emit("later", text="valid log line")
    recorder.close()
    receipt = recorder.ownership_record("log")
    assert receipt.closed and not receipt.healthy and not receipt.sealed
    assert receipt.records == 1
    assert SENTINEL not in repr(asdict(receipt))


def test_recorder_emit_after_close_invalidates_future_receipts(tmp_path, monkeypatch):
    _, recorder = make_recorder(tmp_path, monkeypatch)
    recorder.close()
    original = recorder.ownership_record("log")
    assert original.sealed and original.byte_count == 0
    assert original.sha256 == hashlib.sha256(b"").hexdigest()
    with pytest.raises(ValueError):
        recorder.emit("late", text="cannot write")
    assert not recorder.ownership_record("log").healthy
    assert original.healthy and original.sealed


def test_recorder_publishes_open_resource_before_initial_metadata_failure(
    tmp_path, monkeypatch
):
    ledger = OwnershipLedger()
    operation = ledger.begin_operation("preflight")
    ticket = ledger.acquire("log")
    publication = []
    original_publish = ticket.publish

    def publish(resource):
        publication.append(resource.ownership_record("during-publication"))
        original_publish(resource)

    def fail_fstat(fd):
        raise OSError(SENTINEL)

    monkeypatch.setattr(ticket, "publish", publish)
    with monkeypatch.context() as patch:
        patch.setattr(os, "fstat", fail_fstat)
        with pytest.raises(OSError, match=SENTINEL):
            Recorder(tmp_path / "log.jsonl", Redactor(), acquisition=ticket)
    ledger.finish_operation(operation)
    assert len(publication) == 1
    assert publication[0].identity is None and not publication[0].closed
    receipt = ledger.snapshot()
    assert receipt.acquisitions[0].outcome == "published"
    assert len(receipt.logs) == 1
    assert receipt.logs[0].identity is None
    assert receipt.logs[0].closed and not receipt.logs[0].healthy
    assert not receipt.logs_sealed
    assert SENTINEL not in repr(asdict(receipt))


@pytest.mark.parametrize("published", [False, True])
def test_recorder_publication_failure_closes_known_file_and_retains_ticket(
    tmp_path, monkeypatch, published
):
    ledger = OwnershipLedger()
    operation = ledger.begin_operation("preflight")
    ticket = ledger.acquire("log")
    original_publish = ticket.publish
    known = []

    def fail(resource):
        known.append(resource)
        if published:
            original_publish(resource)
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(ticket, "publish", fail)
    with pytest.raises(RuntimeError, match=SENTINEL):
        Recorder(tmp_path / "log.jsonl", Redactor(), acquisition=ticket)
    ledger.finish_operation(operation)
    record = known[0].ownership_record("known")
    assert record.closed and not record.healthy and not record.sealed
    receipt = ledger.snapshot()
    assert receipt.acquisitions[0].outcome == ("published" if published else "unknown")
    assert not receipt.logs_sealed
    assert SENTINEL not in repr(asdict(receipt))


def test_recorder_later_open_failure_cannot_borrow_successful_preflight_log(
    tmp_path, monkeypatch
):
    ledger = OwnershipLedger()
    preflight = ledger.begin_operation("preflight")
    path, recorder = make_recorder(tmp_path, monkeypatch, ledger=ledger)
    recorder.emit("environment", logged_in=True)
    recorder.close()
    ledger.finish_operation(preflight)
    before = ledger.snapshot()
    assert before.logs_sealed
    turn = ledger.begin_operation("turn")
    ticket = ledger.acquire("log")
    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        Recorder(path, Redactor(), acquisition=ticket)
    ledger.finish_operation(turn)
    after = ledger.snapshot()
    assert path.read_bytes() == original
    assert after.acquisitions[-1].outcome == "not_created"
    assert len(after.logs) == 1 and after.logs[0].sealed
    assert after.settled and not after.logs_sealed
    assert before.logs_sealed


class MutableResource:
    def __init__(self, record):
        self.record = record

    def ownership_record(self, resource_id):
        return replace(self.record, resource_id=resource_id)


def complete_ledger():
    ledger = OwnershipLedger()
    operation = ledger.begin_operation("synthetic")
    process = MutableResource(
        ProcessOwnership("", "synthetic", 123, 0, True, True, True, True, True, True)
    )
    isolation = MutableResource(
        IsolationOwnership("", "/synthetic/owner", 0, 0, 1, 0, True)
    )
    log = MutableResource(
        LogOwnership("", "/synthetic/log", (1, 2), 1, True, True, 7, "a" * 64, True)
    )
    for kind, resource in (
        ("process", process),
        ("isolation", isolation),
        ("log", log),
    ):
        ticket = ledger.acquire(kind)
        ticket.publish(resource)
        ticket.finish()
    ledger.finish_operation(operation)
    return ledger, process, isolation, log


def test_runtime_ownership_receipt_is_instance_local_and_immutable():
    ledger, process, _, _ = complete_ledger()
    before = ledger.snapshot()
    assert before.settled and before.processes_stopped
    assert before.resources_released and before.logs_sealed and before.outputs_complete
    with pytest.raises(FrozenInstanceError):
        before.owner_id = "another-owner"
    with pytest.raises(FrozenInstanceError):
        before.processes[0].exit_code = 7
    process.record = replace(process.record, exit_code=None, stdout_eof=False)
    after = ledger.snapshot()
    assert after.owner_id == before.owner_id
    assert not after.processes_stopped and not after.outputs_complete
    assert before.processes_stopped and before.outputs_complete
    empty = OwnershipLedger().snapshot()
    assert empty.owner_id != before.owner_id
    assert empty.operations == empty.acquisitions == empty.processes == empty.logs == ()
    assert not empty.settled and not empty.processes_stopped
    assert (
        not empty.resources_released
        and not empty.logs_sealed
        and not empty.outputs_complete
    )


@pytest.mark.parametrize(
    "pending",
    [
        "active-operation",
        "pending-acquisition",
        "unknown-acquisition",
        "isolation-pending",
        "isolation-unknown",
    ],
)
def test_runtime_ownership_active_pending_or_unknown_blocks_settlement(pending):
    ledger, _, isolation, _ = complete_ledger()
    if pending == "active-operation":
        ledger.begin_operation("still cleaning")
    elif pending in ("pending-acquisition", "unknown-acquisition"):
        ticket = ledger.acquire("process")
        if pending == "unknown-acquisition":
            ticket.finish()
    else:
        field = "pending_spawns" if pending == "isolation-pending" else "unknown_spawns"
        isolation.record = replace(isolation.record, **{field: 1})
    receipt = ledger.snapshot()
    assert not receipt.settled and not receipt.processes_stopped
    assert (
        not receipt.resources_released
        and not receipt.logs_sealed
        and not receipt.outputs_complete
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("exit_code", None),
        ("group_gone", False),
        ("watcher_settled", False),
        ("readers_settled", False),
    ],
)
def test_runtime_ownership_parent_exit_alone_does_not_prove_process_stopped(
    field, value
):
    ledger, process, _, _ = complete_ledger()
    process.record = replace(process.record, **{field: value})
    receipt = ledger.snapshot()
    assert receipt.settled
    assert not receipt.processes_stopped and not receipt.resources_released
    assert receipt.logs_sealed


@pytest.mark.parametrize("field", ["stdout_eof", "stderr_eof"])
def test_runtime_ownership_eof_is_independent_of_stopped_readers(field):
    ledger, process, _, _ = complete_ledger()
    process.record = replace(process.record, **{field: False})
    receipt = ledger.snapshot()
    assert (
        receipt.processes_stopped and receipt.resources_released and receipt.logs_sealed
    )
    assert not receipt.outputs_complete


def test_runtime_ownership_isolation_removal_is_independent_of_process_stop():
    ledger, _, isolation, _ = complete_ledger()
    isolation.record = replace(isolation.record, removed=False)
    receipt = ledger.snapshot()
    assert (
        receipt.processes_stopped and receipt.outputs_complete and receipt.logs_sealed
    )
    assert not receipt.resources_released


def test_runtime_ownership_known_uncreated_process_does_not_invent_an_exit():
    ledger = OwnershipLedger()
    operation = ledger.begin_operation("failed launch")
    ticket = ledger.acquire("process")
    ticket.not_created()
    ticket.finish()
    ledger.finish_operation(operation)
    receipt = ledger.snapshot()
    assert receipt.processes == ()
    assert receipt.processes_stopped and receipt.resources_released
    assert not receipt.logs_sealed
    assert receipt.acquisitions[0].outcome == "not_created"


def test_runtime_ownership_repeat_publication_does_not_duplicate_resource():
    ledger, process, _, _ = complete_ledger()
    ticket = ledger.acquire("process")
    ticket.publish(process)
    ticket.publish(process)
    ticket.finish()
    receipt = ledger.snapshot()
    assert len(receipt.processes) == 1
    assert receipt.acquisitions[0].resource_id == receipt.acquisitions[-1].resource_id
    with pytest.raises(RuntimeError):
        ticket.not_created()


def test_owned_temporary_directory_requires_actual_cleanup_and_settled_operation(
    tmp_path,
):
    ledger = OwnershipLedger()
    operation = ledger.begin_operation("preflight")
    owner = OwnedTemporaryDirectory(ledger, prefix="environment-", dir=str(tmp_path))
    before_enter = ledger.snapshot()
    assert before_enter.active_operations == 1
    assert before_enter.acquisitions[0].kind == "directory"
    assert before_enter.acquisitions[0].outcome == "published"
    assert before_enter.directories == (
        DirectoryOwnership("directory-1", owner.name, False),
    )
    assert not before_enter.resources_released
    with owner as directory:
        assert directory == owner.name
        assert Path(directory).is_dir()
        (Path(directory) / "synthetic.txt").write_text("preflight data")
        assert not ledger.snapshot().directories[0].removed
    assert not Path(owner.name).exists()
    assert ledger.snapshot().directories[0].removed
    assert not ledger.snapshot().resources_released
    ledger.finish_operation(operation)
    after = ledger.snapshot()
    assert after.processes_stopped and after.resources_released
    assert not after.logs_sealed
    assert not before_enter.directories[0].removed
    with pytest.raises(FrozenInstanceError):
        after.directories[0].removed = False


def test_owned_temporary_directory_unentered_owner_is_not_released(
    tmp_path, monkeypatch
):
    ledger = OwnershipLedger()
    operation = ledger.begin_operation("allocate")
    owner = OwnedTemporaryDirectory(ledger, dir=str(tmp_path))
    ledger.finish_operation(operation)

    def forbidden_cleanup():
        pytest.fail("snapshot must not attempt temporary directory cleanup")

    with monkeypatch.context() as patch:
        patch.setattr(owner._directory, "cleanup", forbidden_cleanup)
        before = ledger.snapshot()
    assert before.processes_stopped and not before.resources_released
    assert Path(owner.name).is_dir()
    owner.cleanup()
    assert ledger.snapshot().resources_released
    assert not before.resources_released


def test_owned_temporary_directory_body_failure_still_performs_real_cleanup(tmp_path):
    ledger = OwnershipLedger()
    operation = ledger.begin_operation("preflight")
    owner = OwnedTemporaryDirectory(ledger, dir=str(tmp_path))
    with pytest.raises(ValueError, match="body failed"), owner as directory:
        assert Path(directory).exists()
        raise ValueError("body failed")
    ledger.finish_operation(operation)
    receipt = ledger.snapshot()
    assert receipt.resources_released
    assert receipt.directories[0].removed
    assert not Path(owner.name).exists()


def test_owned_temporary_directory_cleanup_failure_retains_owner_until_real_retry(
    tmp_path, monkeypatch
):
    ledger = OwnershipLedger()
    operation = ledger.begin_operation("preflight")
    owner = OwnedTemporaryDirectory(ledger, dir=str(tmp_path))

    def fail_cleanup():
        raise OSError("synthetic directory removal failure")

    with monkeypatch.context() as patch:
        patch.setattr(owner._directory, "cleanup", fail_cleanup)
        with pytest.raises(OSError, match="removal failure"), owner:
            pass
    ledger.finish_operation(operation)
    failed = ledger.snapshot()
    assert failed.processes_stopped and not failed.resources_released
    assert not failed.directories[0].removed
    assert Path(owner.name).exists()
    assert ledger._acquisitions[0].resource is owner
    owner.cleanup()
    assert not Path(owner.name).exists()
    assert ledger.snapshot().resources_released
    assert not failed.directories[0].removed


@pytest.mark.parametrize("allocated", [False, True])
def test_owned_temporary_directory_constructor_failure_remains_unknown(
    tmp_path, monkeypatch, allocated
):
    ledger = OwnershipLedger()
    operation = ledger.begin_operation("preflight")
    original = ownership.TemporaryDirectory
    objects = []
    pending = []

    def fail_construction(**kwargs):
        pending.append(ledger.snapshot())
        if allocated:
            objects.append(original(**kwargs))
        raise OSError("temporary directory construction failed")

    monkeypatch.setattr(ownership, "TemporaryDirectory", fail_construction)
    try:
        with pytest.raises(OSError, match="construction failed"):
            OwnedTemporaryDirectory(ledger, dir=str(tmp_path))
        ledger.finish_operation(operation)
        receipt = ledger.snapshot()
        assert pending[0].pending_acquisitions == 1
        assert receipt.unknown_acquisitions == 1
        assert receipt.directories == ()
        assert not receipt.settled and not receipt.resources_released
        if allocated:
            assert Path(objects[0].name).is_dir()
    finally:
        for directory in objects:
            directory.cleanup()


@pytest.mark.parametrize("registered", [False, True])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_owned_temporary_directory_registration_failure_retains_known_object(
    tmp_path, monkeypatch, registered, cleanup_fails
):
    ledger = OwnershipLedger()
    operation = ledger.begin_operation("preflight")
    original_directory = ownership.TemporaryDirectory
    original_register = ledger._register
    created = []
    cleanup_functions = []

    def fail_cleanup():
        raise OSError("directory cleanup failed")

    def construct(**kwargs):
        directory = original_directory(**kwargs)
        created.append(directory)
        cleanup_functions.append(directory.cleanup)
        if cleanup_fails:
            monkeypatch.setattr(directory, "cleanup", fail_cleanup)
        return directory

    def fail_registration(kind, resource):
        assert kind == "directory"
        assert Path(resource.name).is_dir()
        if registered:
            original_register(kind, resource)
        raise RuntimeError("directory publication failed")

    monkeypatch.setattr(ownership, "TemporaryDirectory", construct)
    monkeypatch.setattr(ledger, "_register", fail_registration)
    try:
        expected = OSError if cleanup_fails else RuntimeError
        with pytest.raises(expected):
            OwnedTemporaryDirectory(ledger, dir=str(tmp_path))
        ledger.finish_operation(operation)
        ticket = ledger._acquisitions[0]
        assert ticket.resource is not None
        record = ticket.resource.ownership_record("known-directory")
        assert record.removed == (not cleanup_fails)
        assert Path(record.root).exists() == cleanup_fails
        receipt = ledger.snapshot()
        assert receipt.unknown_acquisitions == 1
        assert not receipt.resources_released
        assert len(receipt.directories) == int(registered)
        if registered:
            assert receipt.directories[0].removed == (not cleanup_fails)
    finally:
        for cleanup in cleanup_functions:
            cleanup()


def test_directory_ownership_default_keeps_old_receipt_constructor_compatible():
    ledger, _, _, _ = complete_ledger()
    previous = ledger.snapshot()
    legacy = RuntimeOwnershipReceipt(
        previous.owner_id,
        previous.operations,
        previous.acquisitions,
        previous.processes,
        previous.isolations,
        previous.logs,
    )
    assert legacy.directories == ()
    assert legacy.resources_released
    allocated = replace(
        legacy, directories=(DirectoryOwnership("directory-1", "/known", False),)
    )
    assert allocated.processes_stopped and not allocated.resources_released
    assert replace(
        allocated, directories=(DirectoryOwnership("directory-1", "/known", True),)
    ).resources_released
