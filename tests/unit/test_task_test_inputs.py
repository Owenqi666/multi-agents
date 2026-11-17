"""Explicit test declarations; real candidate provenance and lightweight inputs."""

import builtins
import hashlib
import json
import shutil
import subprocess
import urllib.request
from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_task_candidate_store as compatibility
import test_task_candidates as candidates
import test_task_context as contexts
import test_task_evidence as evidence
import test_task_execution_store as execution_store
import test_task_store as legacy

from multi_agent.cli import task_candidates as candidate_boundary
from multi_agent.cli import task_test_inputs as boundary
from multi_agent.cli import workspaces
from multi_agent.cli.task_inputs import parse_project_rules
from multi_agent.cli.task_test_inputs import prepare_task_test_input
from multi_agent.cli.workspaces import executor_lock
from multi_agent.core import Disposition
from multi_agent.core.events import CancelRequested, Completed, ProcessExited
from multi_agent.core.snapshot import FrozenSnapshot
from multi_agent.core.task_events import (
    TaskCandidateFrozen,
    TaskEnvelope,
    TaskEvidenceRejected,
    TaskReconciled,
)
from multi_agent.core.task_execution import (
    task_execution_hash,
    task_execution_json,
    task_execution_value,
)
from multi_agent.core.task_test_input import TaskTestInput
from multi_agent.core.tasks import NamedCommand
from multi_agent.core.workspaces import WorkspaceDelivery
from multi_agent.store.planning_store import PlanningStore
from multi_agent.store.task_codec import (
    decode_task_attempt,
    decode_task_envelope,
    decode_task_input,
    encode_task_attempt,
    encode_task_envelope,
    encode_task_input,
)

# Register only fixtures and use helpers through module aliases, never test functions.
project = candidates.project
bound = candidates.bound
live = candidates.live
execution = candidates.execution
archive = candidates.archive
store = execution_store.store

LIMIT = 1_048_576
DECLARATION_SOURCE = "  调用方明确选择完整集合\r\nUnverified declaration.\n"


def trusted_candidate():
    """Static trusted values only; this is not actual Git or ownership evidence."""
    return TaskCandidateFrozen(
        WorkspaceDelivery(
            "a" * 40,
            "c" * 40,
            ("src/example.py",),
            "synthetic-final-digest",
            "refs/multi-agent/candidates/synthetic-owner",
            "d" * 40,
        ),
        "/fixed/execution.json",
        "e" * 64,
    )


@pytest.fixture
def light(monkeypatch):
    """Stub only provenance inspection for pure input/budget/ordering tests."""
    value = decode_task_input(OLD_TASK_INPUT)
    candidate = trusted_candidate()
    history = (
        (TaskEnvelope(1, candidate, "fixed-candidate"), Disposition.APPLIED),
        (TaskEnvelope(1, CancelRequested(), "late"), Disposition.HISTORY_ONLY),
    )
    calls = []
    store, lease = object(), object()
    guard = SimpleNamespace(
        value=value,
        state=SimpleNamespace(current_candidate=candidate),
        history=history,
        check=lambda: calls.append("guard-check"),
    )

    def capture(actual_store, run_id, attempt, actual_lease, *, bound):
        assert actual_store is store and actual_lease is lease
        assert run_id == "run" and attempt == 1 and bound is True
        calls.append("capture")
        return guard

    def inspect(actual_store, run_id, attempt, actual_lease):
        assert actual_store is store and actual_lease is lease
        assert run_id == "run" and attempt == 1
        calls.append("inspect-stub")
        return candidate

    monkeypatch.setattr(boundary, "_capture", capture)
    monkeypatch.setattr(boundary, "inspect_task_candidate", inspect)
    return SimpleNamespace(
        store=store, lease=lease, guard=guard, candidate=candidate, calls=calls
    )


def prepare_light(light, **changes):
    arguments = {
        "required_commands": ("unit",),
        "declaration_source": DECLARATION_SOURCE,
    }
    arguments.update(changes)
    return prepare_task_test_input(light.store, "run", 1, light.lease, **arguments)


@pytest.fixture
def actual(archive):
    archive.candidate = candidates.freeze(archive)
    return archive


def prepare_actual(actual, **changes):
    arguments = {
        "required_commands": ("unit",),
        "declaration_source": DECLARATION_SOURCE,
    }
    arguments.update(changes)
    return prepare_task_test_input(
        actual.store, "implementation", actual.launch.attempt, actual.lease, **arguments
    )


def controls(launch):
    """Observe immutable controls, raw logs and ref without changing them."""
    paths = [
        *Path(launch.prepared.context.path).iterdir(),
        *launch.log_dir.iterdir(),
        launch.lease.repository
        / "refs/multi-agent/candidates"
        / launch.prepared.workspace.owner_token,
        launch.lease.repository / "index",
    ]
    return tuple(
        (str(path), path.stat().st_ino, path.stat().st_mode, path.read_bytes())
        for path in sorted(paths)
    )


def forbid_access(*args, **kwargs):
    pytest.fail("invalid declaration reached database/Git/command access")


def test_task_test_input_public_interfaces():
    assert callable(prepare_task_test_input)
    assert TaskTestInput.__name__ == "TaskTestInput"


@pytest.mark.parametrize(
    "commands",
    [
        (),
        [],
        ["unit"],
        {"unit"},
        {"unit": True},
        "unit",
        b"unit",
        None,
        ("unit", "unit"),
        ("",),
        ("Unit",),
        ("unit_test",),
        ("unit/test",),
        ("汉",),
        ("unit\n",),
        ("unit\0",),
        (None,),
        (1,),
        (True,),
        (("unit",),),
    ],
)
def test_required_commands_are_explicit_ordered_unique(monkeypatch, commands):
    monkeypatch.setattr(boundary, "_capture", forbid_access)
    monkeypatch.setattr(boundary, "inspect_task_candidate", forbid_access)
    monkeypatch.setattr(subprocess, "Popen", forbid_access)
    with pytest.raises(ValueError):
        prepare_task_test_input(
            object(),
            "run",
            1,
            object(),
            required_commands=commands,
            declaration_source="Caller declared this complete suite",
        )


@pytest.mark.parametrize("missing", ["required_commands", "declaration_source", "both"])
def test_required_declaration_keywords_have_no_defaults(monkeypatch, missing):
    monkeypatch.setattr(boundary, "_capture", forbid_access)
    arguments = {"required_commands": ("unit",), "declaration_source": "User choice"}
    if missing == "both":
        arguments.clear()
    else:
        del arguments[missing]
    with pytest.raises(TypeError):
        prepare_task_test_input(object(), "run", 1, object(), **arguments)


@pytest.mark.parametrize(
    "source",
    [None, 3, True, b"source", "", " \t\r\n", "source\0", chr(0xD800), chr(0xDFFF)],
)
def test_declared_source_rejects_invalid_text_before_capture(monkeypatch, source):
    monkeypatch.setattr(boundary, "_capture", forbid_access)
    monkeypatch.setattr(boundary, "inspect_task_candidate", forbid_access)
    with pytest.raises(ValueError):
        prepare_task_test_input(
            object(),
            "run",
            1,
            object(),
            required_commands=("unit",),
            declaration_source=source,
        )


@pytest.mark.parametrize(
    "run_id", [None, 1, True, b"run", "", " \t\n", "run\0", "\ud800"]
)
def test_test_input_run_id_shape_precedes_capture(monkeypatch, run_id):
    monkeypatch.setattr(boundary, "_capture", forbid_access)
    with pytest.raises(ValueError):
        prepare_task_test_input(
            object(),
            run_id,
            1,
            object(),
            required_commands=("unit",),
            declaration_source="declared",
        )


@pytest.mark.parametrize("attempt", [None, 0, -1, True, False, 1.0, "1", (1,)])
def test_test_input_attempt_shape_precedes_capture(monkeypatch, attempt):
    monkeypatch.setattr(boundary, "_capture", forbid_access)
    with pytest.raises(ValueError):
        prepare_task_test_input(
            object(),
            "run",
            attempt,
            object(),
            required_commands=("unit",),
            declaration_source="declared",
        )


@pytest.mark.parametrize("name", ["unknown", "new-check"])
def test_unapproved_names_are_not_inferred_from_proposals(light, name):
    assert any(
        item.name == "new-check" for item in light.guard.value.card.proposed_tests
    )
    with pytest.raises(ValueError):
        prepare_light(light, required_commands=(name,))
    assert light.calls == ["capture"]


def test_state_error_precedes_unknown_registry_name(light, monkeypatch):
    failure = ValueError("actual state is not a current delivery")

    def failed_capture(*args, **kwargs):
        raise failure

    monkeypatch.setattr(boundary, "_capture", failed_capture)
    with pytest.raises(ValueError) as caught:
        prepare_light(light, required_commands=("unknown",))
    assert caught.value is failure and light.calls == []


@pytest.mark.parametrize("empty_refs", [False, True])
def test_complete_suite_is_never_inferred_from_registry_or_card(light, empty_refs):
    original = light.guard.value
    if empty_refs:
        light.guard.value = replace(original, card=replace(original.card, test_refs=()))
    result = prepare_light(light, required_commands=("unit-alias",))
    assert tuple(command.name for command in result.commands) == ("unit-alias",)
    assert len(light.guard.value.frozen_rules.rules.commands) == 2
    assert "new-check" not in tuple(command.name for command in result.commands)
    assert light.calls == ["capture", "inspect-stub", "guard-check"]


def test_distinct_names_with_identical_argv_are_preserved(light):
    first = prepare_light(light, required_commands=("unit-alias", "unit"))
    second = prepare_light(light, required_commands=("unit", "unit-alias"))
    assert first.commands[0].argv == first.commands[1].argv
    assert tuple(command.name for command in first.commands) == ("unit-alias", "unit")
    assert first.commands == tuple(reversed(second.commands))
    assert first.input_hash != second.input_hash
    assert first.candidate_hash == second.candidate_hash


@pytest.mark.parametrize(
    "source",
    [
        DECLARATION_SOURCE,
        "https://example.invalid/approval?token=unverified",
        "/external/not-to-read/approval.txt",
    ],
)
def test_declared_source_is_unverified_text(light, monkeypatch, source):
    monkeypatch.setattr(builtins, "open", forbid_access)
    monkeypatch.setattr(Path, "read_bytes", forbid_access)
    monkeypatch.setattr(Path, "read_text", forbid_access)
    monkeypatch.setattr(urllib.request, "urlopen", forbid_access)
    monkeypatch.setattr(subprocess, "Popen", forbid_access)
    result = prepare_light(light, declaration_source=source)
    assert result.declaration_source == source
    assert result.source_kind == "caller_declared"
    assert result.version == 1
    assert result.commands[0] == light.guard.value.frozen_rules.rules.commands[0]


@pytest.mark.parametrize("unicode", [False, True])
@pytest.mark.parametrize("extra", [0, 1])
def test_declaration_budget_uses_exact_canonical_utf8_bytes(light, unicode, extra):
    overhead = len(b'{"declaration_source":"","required_commands":["unit"]}')
    remaining = LIMIT - overhead
    source = (
        "汉" * (remaining // 3) + "a" * (remaining % 3) if unicode else "a" * remaining
    )
    source += "a" * extra
    independent = json.dumps(
        {"declaration_source": source, "required_commands": ["unit"]},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    assert len(independent) == LIMIT + extra and not independent.endswith(b"\n")
    if unicode:
        assert len(source) < LIMIT - overhead
    if extra:
        with pytest.raises(ValueError):
            prepare_light(light, declaration_source=source)
        assert light.calls == []
    else:
        result = prepare_light(light, declaration_source=source)
        assert result.declaration_source == source
        assert light.calls == ["capture", "inspect-stub", "guard-check"]


def test_declaration_limit_does_not_limit_nested_task_input(light):
    light.guard.value = replace(light.guard.value, requirements="原" * LIMIT)
    assert len(task_execution_json(light.guard.value).encode()) > LIMIT
    result = prepare_light(light)
    assert result.task_input_hash == task_execution_hash(light.guard.value)


@pytest.mark.parametrize(
    "field",
    [
        "version",
        "source_kind",
        "task_run_id",
        "implementation_attempt",
        "task_id",
        "task_hash",
        "rules_hash",
        "task_input_hash",
        "task_history_length",
        "task_history_hash",
        "declaration_source",
        "command-name",
        "command-executable",
        "command-args",
        "command-order",
        "candidate-baseline",
        "candidate-commit",
        "candidate-paths",
        "candidate-digest",
        "candidate-ref",
        "candidate-tree",
        "candidate-log-path",
        "candidate-log-hash",
    ],
)
def test_test_input_hash_covers_provenance(light, field):
    value = prepare_light(light, required_commands=("unit", "unit-alias"))
    original_hash = value.input_hash
    if field.startswith("command-"):
        command = value.commands[0]
        attribute = field.removeprefix("command-")
        if attribute == "order":
            changed = replace(value, commands=tuple(reversed(value.commands)))
        else:
            replacement = {
                "name": "another-name",
                "executable": "another-python",
                "args": ("literal spaces", "$(never execute)", "; not a shell"),
            }[attribute]
            changed = replace(
                value,
                commands=(
                    replace(command, **{attribute: replacement}),
                    *value.commands[1:],
                ),
            )
    elif field.startswith("candidate-"):
        attribute = field.removeprefix("candidate-")
        if attribute in {"log-path", "log-hash"}:
            key, replacement = (
                ("execution_log_path", "/another/execution.json")
                if attribute == "log-path"
                else ("execution_log_sha256", "f" * 64)
            )
            candidate = replace(value.candidate, **{key: replacement})
            assert candidate.delivery.candidate == value.candidate.delivery.candidate
        else:
            key = {"commit": "candidate", "paths": "changed_paths"}.get(
                attribute, attribute
            )
            replacement = (
                ("src/another.py",) if key == "changed_paths" else "another-value"
            )
            candidate = replace(
                value.candidate,
                delivery=replace(value.candidate.delivery, **{key: replacement}),
            )
        changed = replace(value, candidate=candidate)
        assert changed.candidate_hash != value.candidate_hash
    else:
        old = getattr(value, field)
        changed = replace(
            value, **{field: old + 1 if isinstance(old, int) else old + "changed"}
        )
    assert value.input_hash == original_hash
    assert changed.input_hash != original_hash
    assert replace(value).input_hash == original_hash
    assert (
        value.input_hash
        == hashlib.sha256(task_execution_json(value).encode("utf-8")).hexdigest()
    )
    assert value.candidate_hash == task_execution_hash(value.candidate)


def test_test_input_is_deeply_immutable_and_has_only_declared_fields(light):
    value = prepare_light(light, required_commands=("unit", "unit-alias"))
    assert type(value.commands) is tuple and type(value.commands[0].args) is tuple
    assert (
        set(task_execution_value(value))
        == {field.name for field in fields(value)}
        == {
            "version",
            "task_run_id",
            "implementation_attempt",
            "task_id",
            "task_hash",
            "rules_hash",
            "task_input_hash",
            "candidate",
            "commands",
            "declaration_source",
            "source_kind",
            "task_history_length",
            "task_history_hash",
        }
    )
    for item, name, changed in (
        (value, "version", 2),
        (value, "commands", ()),
        (value.commands[0], "args", ("changed",)),
        (value.candidate, "execution_log_path", "changed"),
        (value.candidate.delivery, "tree", "changed"),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(item, name, changed)
        assert not hasattr(item, "__dict__")


def test_full_task_input_hash_includes_requirements_and_created_at(light):
    original = prepare_light(light)
    first = light.guard.value
    light.guard.value = replace(first, requirements=first.requirements + "different")
    changed_requirement = prepare_light(light)
    light.guard.value = replace(first, created_at="different-created-at")
    changed_created = prepare_light(light)
    assert (
        len(
            {
                original.task_input_hash,
                changed_requirement.task_input_hash,
                changed_created.task_input_hash,
            }
        )
        == 3
    )
    assert (
        original.task_hash == changed_requirement.task_hash == changed_created.task_hash
    )


def test_approved_argv_is_preserved_as_literal_data(light, monkeypatch):
    argv = ("argument with spaces", "$(never run)", "; not a shell", "Unicode 参数")
    original = light.guard.value
    command = NamedCommand("literal", "/fixed/command with spaces", argv)
    rules = replace(original.frozen_rules.rules, commands=(command,))
    light.guard.value = replace(
        original, frozen_rules=replace(original.frozen_rules, rules=rules)
    )
    monkeypatch.setattr(subprocess, "Popen", forbid_access)
    result = prepare_light(light, required_commands=("literal",))
    assert result.commands == (command,)
    assert result.commands[0].argv == (command.executable, *argv)


def test_prepare_task_test_input_binds_actual_candidate(actual, monkeypatch):
    # A pre-existing legitimate HISTORY_ONLY suffix is part of this preparation's
    # checkpoint, rather than silently being excluded from the hash.
    candidates.append_history(actual)
    before_state, before_history = candidates.persisted(actual)
    original_input = actual.store.load_task_input("implementation")
    before_controls = controls(actual.launch)
    original = boundary.inspect_task_candidate
    inspected = []

    def real_inspection(*args):
        result = original(*args)
        inspected.append(result)
        return result

    monkeypatch.setattr(boundary, "inspect_task_candidate", real_inspection)
    result = prepare_actual(actual)
    assert inspected == [actual.candidate]
    assert result.task_run_id == "implementation" and result.implementation_attempt == 1
    assert result.task_id == original_input.card.id
    assert result.task_hash == original_input.card.task_hash
    assert result.rules_hash == original_input.frozen_rules.rules_hash
    assert result.task_input_hash == task_execution_hash(original_input)
    assert result.candidate == actual.candidate
    assert result.candidate_hash == task_execution_hash(actual.candidate)
    assert result.commands == tuple(
        command
        for command in original_input.frozen_rules.rules.commands
        if command.name == "unit"
    )
    assert result.declaration_source == DECLARATION_SOURCE
    assert result.source_kind == "caller_declared" and result.version == 1
    assert result.task_history_length == len(before_history)
    assert result.task_history_hash == task_execution_hash(before_history)
    assert candidates.persisted(actual) == (before_state, before_history)
    assert controls(actual.launch) == before_controls
    assert actual.lease.active


def test_test_input_readonly_cold_reopen(bound):
    with contexts.launch(bound) as launch:
        execution = evidence.make(launch)
        candidates.rich_changes(launch)
        manifest = evidence.seal(execution)
        evidence.reconcile(execution, manifest)
        candidate = candidate_boundary.freeze_task_candidate(
            launch.store, "implementation", 1, launch.lease
        )
        expected_state = launch.store.load_task("implementation")
        expected_history = launch.store.load_task_history("implementation")
        expected_controls = controls(launch)
        first = prepare_task_test_input(
            launch.store,
            "implementation",
            1,
            launch.lease,
            required_commands=("unit",),
            declaration_source=DECLARATION_SOURCE,
        )
    with PlanningStore(bound.database) as reopened, executor_lock(bound.repo) as lease:
        second = prepare_task_test_input(
            reopened,
            "implementation",
            1,
            lease,
            required_commands=("unit",),
            declaration_source=DECLARATION_SOURCE,
        )
        third = prepare_task_test_input(
            reopened,
            "implementation",
            1,
            lease,
            required_commands=("unit",),
            declaration_source=DECLARATION_SOURCE,
        )
        assert first == second == third and second.candidate == candidate
        assert reopened.load_task("implementation") == expected_state
        assert reopened.load_task_history("implementation") == expected_history
        assert controls(launch) == expected_controls
        assert expected_state.current_attempt.runtime.reported_model is None
        assert expected_state.current_attempt.runtime.reported_effort is None
        assert lease.active


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("applied", [False, True])
def test_prepare_test_input_keeps_original_history(actual, monkeypatch, when, applied):
    original = boundary.inspect_task_candidate
    old_history = actual.store.load_task_history("implementation")
    added = []
    calls = []

    def changed(*args):
        calls.append(args)
        if when == "before":
            added.append(candidates.append_history(actual, applied))
        result = original(*args)
        if when == "after":
            added.append(candidates.append_history(actual, applied))
        return result

    monkeypatch.setattr(boundary, "inspect_task_candidate", changed)
    with pytest.raises(ValueError):
        prepare_actual(actual)
    assert len(calls) == len(added) == 1
    assert actual.store.load_task_history("implementation") == (*old_history, added[0])
    assert (
        actual.store.load_task("implementation").current_candidate == actual.candidate
    )
    assert actual.lease.active


@pytest.mark.parametrize("field", ["execution_log_path", "execution_log_sha256"])
def test_same_commit_does_not_replace_original_candidate_anchor(
    light, monkeypatch, field
):
    changed = replace(light.candidate, **{field: "different-anchor"})
    assert changed.delivery.candidate == light.candidate.delivery.candidate
    monkeypatch.setattr(boundary, "inspect_task_candidate", lambda *args: changed)
    with pytest.raises(ValueError):
        prepare_light(light)
    assert light.calls == ["capture"]


@pytest.mark.parametrize(
    "gate",
    [
        "no-current",
        "legacy",
        "old",
        "future",
        "no-candidate",
        "stop",
        "digest",
        "anchor",
        "failed",
    ],
)
def test_test_input_requires_current_actual_candidate_before_git(
    store, monkeypatch, gate
):
    number = 1
    if gate == "legacy":
        for event in (
            legacy.started(),
            Completed({"synthetic": True}),
            ProcessExited(-15),
            TaskReconciled("digest", True, "synthetic", "/fixed/log", "a" * 64),
        ):
            store.apply_task("run", TaskEnvelope(1, event, "legacy"))
        assert store.load_task("run").delivery_ready
    elif gate != "no-current":
        candidate = compatibility.qualify(store)
        if gate != "no-candidate":
            assert compatibility.record(store, candidate) == Disposition.APPLIED
        current = store.load_task("run").current_attempt
        if gate in {"stop", "digest", "anchor"}:
            changes = {
                "stop": {"process_stopped": False},
                "digest": {"worktree_digest": None},
                "anchor": {"log_path": None, "log_sha256": None},
            }[gate]
            store.apply_task(
                "run",
                TaskEnvelope(1, replace(current.reconciliation, **changes), "revoked"),
            )
        elif gate in {"failed", "old"}:
            store.apply_task(
                "run",
                TaskEnvelope(
                    1, TaskEvidenceRejected("actual host rejection"), "rejected"
                ),
            )
            if gate == "old":
                execution_store.reserve(store, 2)
        elif gate == "future":
            number = 2
    before = store.load_task("run"), store.load_task_history("run")
    monkeypatch.setattr(boundary, "inspect_task_candidate", forbid_access)
    monkeypatch.setattr(candidate_boundary, "_git", forbid_access)
    monkeypatch.setattr(candidate_boundary, "_baseline_files", forbid_access)
    monkeypatch.setattr(workspaces, "inspect_workspace", forbid_access)
    # Real SQLite/pure-state setup; no valid lease is needed because every case
    # must fail its current managed candidate gate before filesystem/Git access.
    with pytest.raises(ValueError):
        prepare_task_test_input(
            store,
            "run",
            number,
            object(),
            required_commands=("checks",),
            declaration_source="declared complete",
        )
    assert (store.load_task("run"), store.load_task_history("run")) == before


def test_unknown_run_preserves_store_key_error_before_git(store, monkeypatch):
    monkeypatch.setattr(boundary, "inspect_task_candidate", forbid_access)
    with pytest.raises(KeyError) as caught:
        prepare_task_test_input(
            store,
            "missing",
            1,
            object(),
            required_commands=("checks",),
            declaration_source="declared",
        )
    assert caught.value.args == ("missing",)
    assert store.load_task_history("run") == ()


@pytest.mark.parametrize("tamper", ["archive", "ref"])
def test_test_input_requires_current_actual_candidate(actual, tamper):
    if tamper == "archive":
        contexts.replace_bytes(Path(actual.manifest.path), b"{}")
    else:
        workspaces._git(
            actual.lease.repository,
            "update-ref",
            actual.candidate.delivery.ref,
            actual.candidate.delivery.baseline,
        )
    before = candidates.persisted(actual)
    with pytest.raises(ValueError):
        prepare_actual(actual)
    assert candidates.persisted(actual) == before
    assert actual.lease.active


@pytest.mark.parametrize(
    "change",
    ["inactive", "wrong-repository", "database-during-inspect", "lease-during-inspect"],
)
def test_test_input_rejects_database_and_lease_changes(
    actual, monkeypatch, tmp_path, change
):
    original = boundary.inspect_task_candidate
    before = candidates.persisted(actual)
    if change == "inactive":
        actual.lease.active = False
        try:
            with pytest.raises(ValueError):
                prepare_actual(actual)
        finally:
            actual.lease.active = True
    elif change == "wrong-repository":
        other = tmp_path / "different-repository"
        other.mkdir()
        contexts.contexts.git(other, "init", "--quiet", "--template=")
        with executor_lock(other) as lease, pytest.raises(ValueError):
            prepare_task_test_input(
                actual.store,
                "implementation",
                1,
                lease,
                required_commands=("unit",),
                declaration_source="declared",
            )
    else:
        calls = []

        def inspected_then_replace(*args):
            result = original(*args)
            calls.append(result)
            path = (
                Path(
                    actual.store._connection.execute("PRAGMA database_list").fetchone()[
                        2
                    ]
                )
                if change == "database-during-inspect"
                else actual.lease.repository / "multi-agent-executor.lock"
            )
            retained = path.with_name(path.name + "-retained")
            identity = path.stat().st_ino
            path.rename(retained)
            shutil.copyfile(retained, path)
            assert path.stat().st_ino != identity
            return result

        monkeypatch.setattr(boundary, "inspect_task_candidate", inspected_then_replace)
        with pytest.raises(ValueError):
            prepare_actual(actual)
        assert calls == [actual.candidate]
    assert candidates.persisted(actual) == before


def test_test_input_preserves_original_inspection_failure(actual, monkeypatch):
    failure = OSError("actual archive access failed")
    calls = []

    def failed_inspection(*args):
        calls.append(args)
        raise failure

    monkeypatch.setattr(boundary, "inspect_task_candidate", failed_inspection)
    before = candidates.persisted(actual)
    with pytest.raises(OSError) as caught:
        prepare_actual(actual)
    assert caught.value is failure
    assert len(calls) == 1 and candidates.persisted(actual) == before
    assert actual.lease.active


def test_task_test_input_keeps_legacy_rules_task_input_and_snapshot_bytes():
    value = decode_task_input(OLD_TASK_INPUT)
    assert encode_task_input(value).encode("utf-8") == OLD_TASK_INPUT.encode("utf-8")
    assert (
        hashlib.sha256(OLD_TASK_INPUT.encode("utf-8")).hexdigest()
        == OLD_TASK_INPUT_ENCODED_HASH
    )
    assert task_execution_hash(value) == OLD_TASK_INPUT_HASH
    assert value.card.task_hash == OLD_TASK_HASH
    assert value.frozen_rules.rules_hash == OLD_RULES_HASH
    assert parse_project_rules(value.frozen_rules.raw_text) == value.frozen_rules.rules
    assert value.requirements == "  原始需求\r\nKeep Unicode and whitespace.\n"
    assert "# 原始规则\r\nversion: 1\r\n" in value.frozen_rules.raw_text
    assert (
        value.frozen_rules.rules.commands[0].argv
        == value.frozen_rules.rules.commands[1].argv
    )
    assert value.card.proposed_tests[0].name == "new-check"
    assert value.card.open_questions[0].resolution is None
    for name, encoded in compatibility.OLD_GOLDENS.items():
        assert (
            hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            == compatibility.OLD_PAYLOAD_HASHES[name]
        )
        if name.endswith("envelope"):
            assert encode_task_envelope(decode_task_envelope(encoded)) == encoded
        elif name.endswith("attempt"):
            assert encode_task_attempt(decode_task_attempt(encoded)) == encoded
    snapshot = FrozenSnapshot.from_dict(
        json.loads(compatibility.OLD_GOLDENS["snapshot"])
    )
    assert snapshot.snapshot_id == compatibility.OLD_SNAPSHOT_ID
    assert (
        task_execution_json(snapshot.to_dict()) == compatibility.OLD_GOLDENS["snapshot"]
    )
    assert snapshot.tests[0].log == "Supplied record only; not executed.\n"


# Captured from the unchanged legacy codec before T48 implementation.
# Fixed paths and literals: no external acceptance file is read by tests.
OLD_TASK_INPUT = '{"card":{"acceptance_criteria":["The declared tests pass."],"baseline_commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","deliverables":["Frozen source."],"depends_on":[],"goal":"保留原任务 API。","id":"T-0100","interface_contracts":[{"definition":"Preserve exact bytes.","name":"input"}],"open_questions":[{"blocks_dispatch":false,"question":"Optional label?"}],"proposed_tests":[{"args":["check.py"],"executable":"python","name":"new-check","reason":"Caller has not approved this command."}],"schema_version":1,"test_refs":["unit"],"write_scope":["src/**"]},"created_at":"2026-01-02T03:04:05+00:00","frozen_rules":{"baseline_commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","blob_oid":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","raw_text":"# 原始规则\\r\\nversion: 1\\r\\ncommands:\\r\\n  unit:\\r\\n    executable: python\\r\\n    args: [-m, pytest, tests/unit]\\r\\n  unit-alias:\\r\\n    executable: python\\r\\n    args: [-m, pytest, tests/unit]\\r\\nprotected_paths: [\\".multi-agent/**\\"]\\r\\nimplementation:\\r\\n  - paths: [\\"src/**\\"]\\r\\n    provider: codex\\r\\n    default_model: fixture-model\\r\\n    default_effort: medium\\r\\n","repository":"/fixed/repository/.git","rules":{"commands":[{"args":["-m","pytest","tests/unit"],"executable":"python","name":"unit"},{"args":["-m","pytest","tests/unit"],"executable":"python","name":"unit-alias"}],"default_implementation":null,"implementation":[{"config":{"allowed_models":["fixture-model"],"default_effort":"medium","default_model":"fixture-model","provider":"codex"},"paths":["src/**"]}],"protected_paths":[".multi-agent/**"],"task_types":[],"version":1}},"requirements":"  原始需求\\r\\nKeep Unicode and whitespace.\\n"}'
OLD_TASK_INPUT_ENCODED_HASH = (
    "64a50964211f4b22f958325815f68c531de201d425c0dbede7312137fb1d0ee0"
)
OLD_TASK_INPUT_HASH = "611dc853d33d8b78caee1c4ef5acc46a594de4653703b4f17721b62e94eacb20"
OLD_TASK_HASH = "bfa0a253b13c4cc888d107351adba7fcf2c1ebb12a8028fefe755bcf3c6ff1e5"
OLD_RULES_HASH = "e33797e7c44b0ef88a82b9f303268cab1fcf65318b05e2fa4636cfdb2c2c1d09"
