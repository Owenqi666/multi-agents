"""Task preparation uses sealed synthetic planning and real owned Git workspaces."""

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import threading
from copy import copy, deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_planning_context as contexts
import test_planning_evidence as evidence

from multi_agent.cli import task_context as boundary
from multi_agent.cli.planning_evidence import bind_planning_task
from multi_agent.cli.task_context import (
    capture_task_policy,
    inspect_task_context,
    prepare_task_launch,
)
from multi_agent.cli.task_inputs import freeze_project_rules
from multi_agent.cli.workspaces import executor_lock, inspect_workspace
from multi_agent.core import Disposition
from multi_agent.core.dispatch import ImplementationChoice
from multi_agent.core.events import Completed
from multi_agent.core.planning import PlanningPhase
from multi_agent.core.task_events import (
    TaskEnvelope,
    TaskReconciled,
)
from multi_agent.core.task_execution import (
    TaskDispatchPolicy,
    task_execution_json,
    task_execution_value,
)
from multi_agent.store.planning_store import PlanningStore

# Registration keeps the existing fixture's no-runtime-process guard active.
project = contexts.project


@pytest.fixture
def bound(project):
    """Wrap, never weaken, the older fixture's deliberately unsafe baseline."""
    repo = project.repo
    (repo / "external-link").unlink()
    contexts.git(repo, "rm", "--cached", "-f", "--", "submodule")
    (repo / "src/internal-link").symlink_to("code.py")
    (repo / "src/data.bin").write_bytes(b"\xff\x00binary\r\n")
    rules = repo / ".multi-agent/rules.yaml"
    rules.write_text(
        rules.read_text().replace(
            "    default_effort: medium\n",
            "    default_effort: medium\n"
            "    allowed_models: [claude-sonnet-5-5, claude-opus-4-8]\n",
        )
    )
    contexts.git(repo, "add", "-A")
    contexts.git(repo, "commit", "--quiet", "-m", "safe task baseline")
    baseline = contexts.git(repo, "rev-parse", "HEAD")
    value = replace(
        project.value,
        frozen_rules=freeze_project_rules(repo, baseline),
        dependencies=tuple(
            replace(item, baseline_commit=baseline)
            for item in project.value.dependencies
        ),
    )
    safe = SimpleNamespace(**vars(project))
    safe.baseline, safe.value = baseline, value
    with PlanningStore(safe.database) as store:
        store.create_planning("plan", value, "planning-created")
        draft, review = evidence.pair(store, safe)
        bind_planning_task(store, "plan", "implementation", "task-created")
        assert store.load_planning("plan").phase == PlanningPhase.BOUND
        assert evidence.inspect(store, draft).qualified
        assert evidence.inspect(store, review).qualified
        task_input = store.load_task_input("implementation")
        provenance = store.load_task_provenance("implementation")
    policy = capture_task_policy(
        "claude", safe.executables["claude"], max_attempts=3, timeout=45, grace=1
    )
    dispatch = TaskDispatchPolicy(
        None,
        ImplementationChoice("claude-code", "claude-sonnet-5-5", "medium"),
        (
            ImplementationChoice("codex", "wrong-provider", "high"),
            ImplementationChoice("claude-code", "not-allowed", "low"),
            ImplementationChoice("claude-code", "claude-opus-4-8", "high"),
            ImplementationChoice("claude-code", "claude-sonnet-5-5", "low"),
        ),
    )
    return SimpleNamespace(
        project=safe,
        repo=repo,
        database=safe.database,
        root=safe.directory / "owned-task",
        policy=policy,
        dispatch=dispatch,
        unavailable=frozenset({("claude-code", "claude-sonnet-5-5")}),
        task_input=task_input,
        provenance=provenance,
        draft=draft,
        review=review,
        registry=Path(value.frozen_rules.repository) / "multi-agent-task-preparations",
    )


def launch(bound, **overrides):
    arguments = {
        "repository": bound.repo,
        "directory": bound.root,
        "policy": bound.policy,
        "dispatch_policy": bound.dispatch,
        "unavailable": bound.unavailable,
    }
    arguments.update(overrides)
    return prepare_task_launch(bound.database, "implementation", "plan", **arguments)


def journals(bound):
    return sorted(bound.registry.glob("*.json")) if bound.registry.exists() else []


def load_state(bound):
    with PlanningStore(bound.database) as store:
        return store.load_task("implementation"), store.load_task_history(
            "implementation"
        )


def settle(handle, *, digest=True, stopped=True):
    actual = inspect_workspace(handle.lease, handle.prepared.workspace).digest
    event = TaskReconciled(
        actual if digest else None,
        stopped,
        "Synthetic stop; real fixture workspace scan",
    )
    assert (
        handle.store.apply_task(
            "implementation", TaskEnvelope(handle.attempt, event, "settled")
        )
        == Disposition.APPLIED
    )
    return actual


def replace_bytes(path, raw):
    path.chmod(0o600)
    path.write_bytes(raw)
    path.chmod(0o400)


def test_task_launch_requires_bound_verified_planning(bound, monkeypatch):
    inspections = []
    original = boundary.inspect_planning_execution

    def inspect(store, run_id, stage, attempt):
        inspections.append((run_id, stage, attempt))
        return original(store, run_id, stage, attempt)

    monkeypatch.setattr(boundary, "inspect_planning_execution", inspect)
    with launch(bound) as handle:
        assert handle.attempt == 1
        assert {stage for _, stage, _ in inspections} == {"draft", "review"}
        assert handle.store.load_task_input("implementation") == bound.task_input
        assert (
            handle.prepared.workspace.repository
            == bound.task_input.frozen_rules.repository
        )
        assert handle.claim() == handle.prepared


@pytest.mark.parametrize(
    "gate",
    [
        "draft_log",
        "review_manifest",
        "task_input",
        "unbound",
        "different_task",
        "missing_manifest",
    ],
)
def test_task_launch_requires_bound_verified_planning_rejects(bound, monkeypatch, gate):
    original_input = PlanningStore.load_task_input
    original_plan = PlanningStore.load_planning
    if gate == "draft_log":
        log = Path(bound.draft.logs.path) / "turn.jsonl"
        log.write_bytes(log.read_bytes() + b"tampered\n")
    elif gate in {"review_manifest", "missing_manifest"}:
        path = Path(bound.review.manifest.path)
        if gate == "missing_manifest":
            path.unlink()
        else:
            replace_bytes(path, b"{}\n")
    elif gate == "task_input":

        def changed_input(self, run_id):
            return replace(
                original_input(self, run_id), requirements="replacement input"
            )

        monkeypatch.setattr(PlanningStore, "load_task_input", changed_input)
    else:

        def changed_plan(self, run_id):
            state = original_plan(self, run_id)
            return replace(
                state,
                **(
                    {"phase": PlanningPhase.READY}
                    if gate == "unbound"
                    else {"bound_task_run_id": "other"}
                ),
            )

        monkeypatch.setattr(PlanningStore, "load_planning", changed_plan)
    creations = []
    monkeypatch.setattr(
        boundary, "create_workspace", lambda *args, **kwargs: creations.append(args)
    )
    with pytest.raises((ValueError, OSError)), launch(bound):
        pytest.fail("invalid source granted a live capability")
    assert not creations and not bound.root.exists()
    assert load_state(bound)[1] == ()


def test_task_launch_holds_executor_lease_until_scope_exit(bound):
    with launch(bound) as handle:
        for repository in (bound.repo, Path(handle.prepared.workspace.root)):
            with pytest.raises(ValueError), executor_lock(repository):
                pytest.fail("parallel repository executor acquired")
        lease = handle.lease
    assert not lease.active
    with executor_lock(bound.repo) as available:
        assert available.active
    with (
        pytest.raises(ValueError),
        launch(bound, directory=bound.root.with_name("another-root")),
    ):
        pytest.fail("released lock was treated as stop proof")


def test_task_launch_capability_is_fresh_single_use(bound):
    with launch(bound) as handle:
        assert handle.check() is None
        assert handle.claim() == handle.prepared
        with pytest.raises(ValueError):
            handle.claim()
    for operation in (handle.check, handle.claim):
        with pytest.raises(ValueError):
            operation()
    with PlanningStore(bound.database) as reopened, executor_lock(bound.repo) as lease:
        assert (
            inspect_task_context(reopened, "implementation", 1, lease)
            == handle.prepared
        )
    with (
        pytest.raises(ValueError),
        launch(bound, directory=bound.root.with_name("restart")),
    ):
        pytest.fail("old Prepared was adopted")
    state, history = load_state(bound)
    assert state.current_attempt.reconciliation is None
    assert len(history) == 2


def test_task_preparation_freezes_inline_prompt_schema_and_policy(bound):
    with launch(bound) as handle:
        prepared = handle.prepared
        current = handle.store.load_task("implementation").current_attempt
        assert (
            prepared.policy == bound.policy
            and prepared.dispatch_policy == bound.dispatch
        )
        assert current.request.decision.choice == bound.dispatch.fallbacks[2]
        assert current.request.environment is None
        assert (
            current.runtime.reported_model is None
            and current.runtime.reported_effort is None
        )
        assert task_execution_json(bound.task_input) in prepared.prompt
        assert bound.provenance.plan_hash in prepared.prompt
        for item in (*bound.project.value.dependencies, *bound.project.value.decisions):
            assert task_execution_json(item) in prepared.prompt
        assert prepared.output_schema["additionalProperties"] is False
        assert set(prepared.output_schema["properties"]) == {
            "task_hash",
            "outcome",
            "summary",
            "required_paths",
        }
        context = Path(prepared.context.path)
        assert (context / "prompt.txt").read_text() == prepared.prompt
        assert json.loads(
            (context / "output-schema.json").read_text()
        ) == task_execution_value(prepared.output_schema)
        for path, mode in (
            (bound.root, 0o700),
            (context, 0o500),
            (handle.log_dir, 0o700),
        ):
            assert stat.S_IMODE(path.stat().st_mode) == mode
        for name in (".task-context.json", "prompt.txt", "output-schema.json"):
            assert stat.S_IMODE((context / name).stat().st_mode) == 0o400
        assert len(journals(bound)) == 1
        assert stat.S_IMODE(journals(bound)[0].stat().st_mode) == 0o600


def test_task_workspace_uses_frozen_baseline_not_head_or_dirty_files(bound):
    baseline_bytes = (bound.repo / "src/code.py").read_bytes()
    (bound.repo / "src/code.py").write_text("NEW HEAD\n")
    contexts.git(bound.repo, "add", "src/code.py")
    contexts.git(bound.repo, "commit", "--quiet", "-m", "unrelated later head")
    head = contexts.git(bound.repo, "rev-parse", "HEAD")
    (bound.repo / "src/code.py").write_text("DIRTY\n")
    index_before = (bound.repo / ".git/index").read_bytes()
    hook = bound.repo / ".git/hooks/post-checkout"
    hook.parent.mkdir(exist_ok=True)
    hook.write_bytes(bound.project.executables["claude"].read_bytes())
    hook.chmod(0o755)
    contexts.git(
        bound.repo,
        "config",
        "filter.tripwire.smudge",
        str(bound.project.executables["claude"]),
    )
    contexts.git(
        bound.repo,
        "config",
        "filter.tripwire.clean",
        str(bound.project.executables["claude"]),
    )
    contexts.git(bound.repo, "config", "filter.tripwire.required", "true")
    with launch(bound) as handle:
        workspace = Path(handle.prepared.workspace.root)
        assert (workspace / "src/code.py").read_bytes() == baseline_bytes
        assert (workspace / "src/data.bin").read_bytes() == b"\xff\x00binary\r\n"
        assert (workspace / "src/internal-link").is_symlink()
        assert os.readlink(workspace / "src/internal-link") == "code.py"
        assert (workspace / "src/do_not_run.py").stat().st_mode & 0o111
        assert handle.prepared.workspace.start_commit == bound.project.baseline
        assert contexts.git(bound.repo, "rev-parse", "HEAD") == head
        assert (bound.repo / ".git/index").read_bytes() == index_before
        assert (bound.repo / "src/code.py").read_text() == "DIRTY\n"
        assert not bound.project.marker.exists()
        control = next(
            item
            for item in handle.prepared.workspace.controls
            if item.path == str(workspace / ".git")
        )
        assert control.content == (workspace / ".git").read_bytes()


def swap_identity(path, backup):
    mode = stat.S_IMODE(path.lstat().st_mode)
    if path.is_dir():
        path.chmod(0o700)
        path.rename(backup)
        shutil.copytree(backup, path, symlinks=True)
        backup.chmod(mode)
    else:
        path.rename(backup)
        shutil.copy2(backup, path)
    path.chmod(mode)


@pytest.mark.parametrize(
    "tamper",
    [
        "root_identity",
        "root_mode",
        "context_identity",
        "context_mode",
        "context_extra",
        "prompt_identity",
        "prompt_bytes",
        "prompt_mode",
        "schema_bytes",
        "manifest_bytes",
        "logs_identity",
        "logs_mode",
        "git_control",
        "executable_bytes",
        "executable_identity",
        "executable_mode",
        "workspace_bytes",
        "prompt_symlink",
    ],
)
def test_task_context_and_executable_tampering_blocks_claim(bound, tamper):
    with launch(bound) as handle:
        context = Path(handle.prepared.context.path)
        workspace = Path(handle.prepared.workspace.root)
        executable = Path(bound.policy.executable)
        backup = bound.root.with_name("retained-original")
        if tamper.endswith("_identity"):
            paths = {
                "root_identity": bound.root,
                "context_identity": context,
                "prompt_identity": context / "prompt.txt",
                "logs_identity": handle.log_dir,
                "executable_identity": executable,
            }
            if tamper == "prompt_identity":
                context.chmod(0o700)
            swap_identity(paths[tamper], backup)
            if tamper == "prompt_identity":
                context.chmod(0o500)
        elif tamper.endswith("_mode"):
            paths = {
                "root_mode": bound.root,
                "context_mode": context,
                "prompt_mode": context / "prompt.txt",
                "logs_mode": handle.log_dir,
                "executable_mode": executable,
            }
            paths[tamper].chmod(
                0o600 if tamper in {"prompt_mode", "executable_mode"} else 0o755
            )
        elif tamper == "context_extra":
            context.chmod(0o700)
            (context / "unexpected").write_text("extra control")
            context.chmod(0o500)
        elif tamper in {"prompt_bytes", "schema_bytes", "manifest_bytes"}:
            name = {
                "prompt_bytes": "prompt.txt",
                "schema_bytes": "output-schema.json",
                "manifest_bytes": ".task-context.json",
            }[tamper]
            path = context / name
            replace_bytes(path, path.read_bytes() + b"\n")
        elif tamper == "git_control":
            path = workspace / ".git"
            path.write_bytes(path.read_bytes() + b"tampered\n")
        elif tamper == "executable_bytes":
            executable.write_bytes(executable.read_bytes() + b"# changed\n")
        elif tamper == "workspace_bytes":
            (workspace / "src/code.py").write_text("legitimate implementation edit\n")
            # Historical inspection verifies ownership, not an obsolete initial tree.
            assert (
                inspect_task_context(handle.store, "implementation", 1, handle.lease)
                == handle.prepared
            )
        else:
            context.chmod(0o700)
            path = context / "prompt.txt"
            target = bound.root.with_name("external-prompt")
            target.write_bytes(path.read_bytes())
            path.unlink()
            path.symlink_to(target)
            context.chmod(0o500)
        with pytest.raises((ValueError, OSError)):
            handle.claim()
        state = handle.store.load_task("implementation")
        assert state.current_attempt.reconciliation is None
        assert not state.delivery_ready


@pytest.mark.parametrize(
    "location",
    [
        "existing",
        "different_parent",
        "repository",
        "git",
        "symlink_parent",
        "database_symlink",
        "database_missing",
        "database_directory",
    ],
)
def test_task_context_rejects_noncanonical_or_internal_locations(bound, location):
    database, directory = bound.database, bound.root
    if location == "existing":
        directory.mkdir()
    elif location == "different_parent":
        parent = bound.root.parent / "other-parent"
        parent.mkdir()
        directory = parent / "task"
    elif location in {"repository", "git"}:
        directory = (
            bound.repo if location == "repository" else bound.repo / ".git"
        ) / "task"
    elif location == "symlink_parent":
        alias = bound.root.parent / "alias"
        alias.symlink_to(bound.root.parent, target_is_directory=True)
        directory = alias / "task"
    elif location == "database_symlink":
        database = bound.root.parent / "database-alias"
        database.symlink_to(bound.database)
    elif location == "database_missing":
        database = bound.root.parent / "absent.sqlite"
    else:
        database = bound.root.parent / "db-directory"
        database.mkdir()
    with (
        pytest.raises((ValueError, OSError)),
        prepare_task_launch(
            database,
            "implementation",
            "plan",
            repository=bound.repo,
            directory=directory,
            policy=bound.policy,
            dispatch_policy=bound.dispatch,
        ),
    ):
        pytest.fail("invalid path granted a live launch")
    assert load_state(bound)[1] == ()
    if location == "database_missing":
        assert not database.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider", "claude-code"),
        ("provider", "unknown"),
        ("max_attempts", True),
        ("max_attempts", 0),
        ("max_attempts", -1),
        ("max_attempts", 1.5),
        ("timeout", True),
        ("timeout", 0),
        ("timeout", -1),
        ("timeout", float("inf")),
        ("timeout", float("nan")),
        ("grace", True),
        ("grace", 0),
        ("grace", -1),
        ("grace", float("inf")),
        ("grace", float("nan")),
    ],
)
def test_task_policy_capture_validates_limits_and_executable(project, field, value):
    kwargs = {
        "provider": "claude",
        "executable": project.executables["claude"],
        "max_attempts": 2,
        "timeout": 10,
        "grace": 1,
    }
    kwargs[field] = value
    with pytest.raises((TypeError, ValueError)):
        capture_task_policy(**kwargs)
    assert not project.marker.exists()


@pytest.mark.parametrize("kind", ["missing", "directory", "nonexecutable"])
def test_task_policy_capture_rejects_nonexecutable_paths(project, kind):
    path = project.directory / "policy-executable"
    if kind == "directory":
        path.mkdir()
    elif kind == "nonexecutable":
        path.write_bytes(b"not executable")
        path.chmod(0o600)
    with pytest.raises((ValueError, OSError)):
        capture_task_policy("claude", path, max_attempts=2, timeout=10, grace=1)


def test_task_policy_capture_records_real_identity_and_bytes_without_execution(project):
    path = project.executables["codex"]
    result = capture_task_policy("codex", path, max_attempts=2, timeout=10, grace=1)
    info = path.stat()
    assert result.executable == str(path)
    assert result.executable_identity == (info.st_dev, info.st_ino)
    assert result.executable_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert not project.marker.exists()


@pytest.mark.parametrize("missing", ["policy", "dispatch_policy", "both"])
def test_task_retry_reuses_exact_policy_and_dispatch_requires_first_values(
    bound, missing
):
    overrides = (
        {"policy": None, "dispatch_policy": None}
        if missing == "both"
        else {missing: None}
    )
    with pytest.raises((TypeError, ValueError)), launch(bound, **overrides):
        pytest.fail("first launch omitted explicit policy")
    assert load_state(bound)[1] == () and not bound.root.exists()


def test_task_retry_reuses_exact_policy_and_dispatch(bound):
    with launch(bound) as first:
        assert first.prepared.dispatch_policy.fallbacks == bound.dispatch.fallbacks
        decision = first.store.load_task(
            "implementation"
        ).current_attempt.request.decision
        assert decision.choice == bound.dispatch.fallbacks[2]
        assert decision.selection_history[1].choice.provider == "codex"
        assert decision.selection_history[1].outcome == "disallowed_by_rules"
        assert (
            first.store.load_task("implementation").current_attempt.request.environment
            is None
        )
        settle(first)
        first_workspace = first.prepared.workspace
    with launch(
        bound,
        directory=bound.root.with_name("retry"),
        policy=None,
        dispatch_policy=None,
    ) as second:
        assert second.attempt == 2
        assert (
            second.prepared.policy == bound.policy
            and second.prepared.dispatch_policy == bound.dispatch
        )
        assert second.prepared.workspace.start_commit == bound.project.baseline
        assert second.prepared.workspace.root != first_workspace.root
        assert second.prepared.workspace.owner_token != first_workspace.owner_token
        assert second.claim() == second.prepared


@pytest.mark.parametrize(
    "changed", ["timeout", "fallback_order", "fallback_tail", "requested"]
)
def test_task_retry_rejects_changed_frozen_policy(bound, changed):
    with launch(bound) as first:
        settle(first)
    overrides = {}
    if changed == "timeout":
        overrides["policy"] = replace(bound.policy, timeout=46)
    else:
        dispatch = bound.dispatch
        values = {
            "fallback_order": {"fallbacks": tuple(reversed(dispatch.fallbacks))},
            "fallback_tail": {"fallbacks": dispatch.fallbacks[:-1]},
            "requested": {"requested": None},
        }[changed]
        overrides["dispatch_policy"] = replace(dispatch, **values)
    before = load_state(bound)
    target = bound.root.with_name("changed-policy")
    with pytest.raises(ValueError), launch(bound, directory=target, **overrides):
        pytest.fail("changed retry policy accepted")
    assert load_state(bound) == before and not target.exists()


def test_task_context_manifest_is_not_execution_receipt(bound):
    with launch(bound) as handle:
        assert handle.check() is None
        assert (
            inspect_task_context(handle.store, "implementation", 1, handle.lease)
            == handle.prepared
        )
        current = handle.store.load_task("implementation").current_attempt
        assert current.runtime.result is None and current.runtime.exit_code is None
        assert current.reconciliation is None
        assert not handle.store.load_task("implementation").delivery_ready
        assert list(handle.log_dir.iterdir()) == []
        assert not (handle.log_dir / "execution.json").exists()


@pytest.mark.parametrize(
    "failure",
    [
        "marker_initial",
        "marker_after_root",
        "marker_after_workspace",
        "root_after",
        "workspace_before",
        "workspace_after",
        "control",
        "reserve_before",
        "reserve_transaction",
        "reserve_after",
    ],
)
def test_task_preparation_retains_partial_creation_journal(bound, monkeypatch, failure):
    problem = OSError(f"injected {failure}")
    before = load_state(bound)
    with monkeypatch.context() as patch:
        if failure.startswith("marker_"):
            original = boundary._write_marker
            calls = []

            def failed_marker(fd, path, value):
                calls.append(value)
                selected = (
                    failure == "marker_initial"
                    or failure == "marker_after_root"
                    and len(calls) == 2
                    or failure == "marker_after_workspace"
                    and value.get("workspace") is not None
                )
                if selected:
                    raise problem
                return original(fd, path, value)

            patch.setattr(boundary, "_write_marker", failed_marker)
        elif failure == "root_after":
            original = Path.mkdir

            def failed_mkdir(path, *args, **kwargs):
                original(path, *args, **kwargs)
                if path == bound.root:
                    raise problem

            patch.setattr(Path, "mkdir", failed_mkdir)
        elif failure.startswith("workspace_"):
            original = boundary.create_workspace

            def failed_workspace(*args, **kwargs):
                if failure == "workspace_after":
                    original(*args, **kwargs)
                raise problem

            patch.setattr(boundary, "create_workspace", failed_workspace)
        elif failure == "control":
            original = boundary._write_control

            def failed_control(*args, **kwargs):
                original(*args, **kwargs)
                raise problem

            patch.setattr(boundary, "_write_control", failed_control)
        elif failure == "reserve_transaction":
            original = PlanningStore._append_task_history

            def failed_history(self, run_id, envelope, disposition):
                original(self, run_id, envelope, disposition)
                if envelope.event.kind == "task_execution_prepared":
                    raise problem

            patch.setattr(PlanningStore, "_append_task_history", failed_history)
        else:
            original = PlanningStore.reserve_task_attempt

            def failed_reserve(*args, **kwargs):
                if failure == "reserve_after":
                    original(*args, **kwargs)
                raise problem

            patch.setattr(PlanningStore, "reserve_task_attempt", failed_reserve)
        with pytest.raises((ValueError, OSError)) as caught, launch(bound):
            pytest.fail("creation failure yielded a capability")
    causes = []
    error = caught.value
    while error is not None:
        causes.append(error)
        error = error.__cause__
    assert problem in causes
    paths = journals(bound)
    assert len(paths) == 1 and paths[0].is_file()
    notes = "\n".join(
        note for error in causes for note in getattr(error, "__notes__", ())
    )
    assert str(paths[0]) in notes and str(bound.root) in notes
    after = load_state(bound)
    if failure == "reserve_after":
        assert len(after[1]) == 2
        assert [item[0].event.kind for item in after[1]] == [
            "task_attempt_started",
            "task_execution_prepared",
        ]
        assert after[0].current_attempt.reconciliation is None
    else:
        assert after == before
    with (
        pytest.raises((ValueError, OSError)),
        launch(bound, directory=bound.root.with_name("bypass-partial")),
    ):
        pytest.fail("partial marker was bypassed")
    assert load_state(bound) == after


@pytest.mark.parametrize("foreign", ["regular", "fifo", "symlink"])
def test_task_registry_rejects_foreign_database_without_opening_it(
    bound, monkeypatch, foreign
):
    with launch(bound):
        pass
    database = bound.root.parent / "foreign-controller.sqlite"
    with sqlite3.connect(bound.database) as source, sqlite3.connect(database) as target:
        source.backup(target)
    if foreign != "regular":
        bound.database.rename(bound.database.with_suffix(".retained"))
        if foreign == "fifo":
            os.mkfifo(bound.database)
        else:
            bound.database.symlink_to(database)
    original_store = boundary.PlanningStore
    original_open = os.open
    opened = []

    def only_local_store(path, *args, **kwargs):
        assert Path(path) != bound.database, "followed foreign registry DB pointer"
        opened.append(Path(path))
        return original_store(path, *args, **kwargs)

    def no_foreign_open(path, *args, **kwargs):
        if isinstance(path, (str, bytes, os.PathLike)):
            assert Path(os.fsdecode(path)) != bound.database, "opened foreign DB path"
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(boundary, "PlanningStore", only_local_store)
    monkeypatch.setattr(os, "open", no_foreign_open)
    with (
        pytest.raises(ValueError),
        prepare_task_launch(
            database,
            "implementation",
            "plan",
            repository=bound.repo,
            directory=bound.root.with_name("foreign-root"),
            policy=bound.policy,
            dispatch_policy=bound.dispatch,
            unavailable=bound.unavailable,
        ),
    ):
        pytest.fail("foreign DB bypassed registry")
    assert opened == [database]


@pytest.mark.parametrize(
    "unknown",
    [
        "no_reconciliation",
        "stop_false",
        "digest_none",
        "marker_missing",
        "legacy_other_run",
        "legacy_settled_other_run",
    ],
)
def test_task_registry_blocks_unknown_and_unmatched_workspace_history(bound, unknown):
    with launch(bound) as first:
        if unknown == "stop_false":
            settle(first, stopped=False)
        elif unknown == "digest_none":
            settle(first, digest=False)
        elif unknown.startswith("legacy"):
            settle(first)
            value = bound.task_input
            first.store.create_task(
                "other",
                value.card,
                value.frozen_rules,
                value.requirements,
                "created-other",
            )
            request = first.store.load_task("implementation").current_attempt.request
            first.store.apply_task(
                "other",
                TaskEnvelope(
                    1,
                    replace(
                        request, managed=False, worktree="/unverified/old-workspace"
                    ),
                ),
            )
            if unknown == "legacy_settled_other_run":
                first.store.apply_task(
                    "other",
                    TaskEnvelope(
                        1, TaskReconciled("unverified", True, "legacy assertion")
                    ),
                )
    if unknown == "marker_missing":
        journals(bound)[0].unlink()
    before = load_state(bound)
    target = bound.root.with_name("bypass-unknown")
    with pytest.raises(ValueError), launch(bound, directory=target):
        pytest.fail("unknown workspace history was bypassed")
    assert load_state(bound) == before and not target.exists()


@pytest.mark.parametrize("tamper", ["bad_json", "mode", "symlink", "extra_marker"])
def test_task_registry_rejects_unmatched_or_changed_markers(bound, tamper):
    with launch(bound) as first:
        settle(first)
    path = journals(bound)[0]
    if tamper == "bad_json":
        path.write_bytes(b"{broken")
    elif tamper == "mode":
        path.chmod(0o644)
    elif tamper == "symlink":
        target = bound.root.with_name("foreign-marker")
        path.rename(target)
        path.symlink_to(target)
    else:
        (bound.registry / "unmatched.json").write_text("{}\n")
        (bound.registry / "unmatched.json").chmod(0o600)
    with (
        pytest.raises((ValueError, OSError)),
        launch(bound, directory=bound.root.with_name("bypass-marker")),
    ):
        pytest.fail("invalid marker was ignored")


@pytest.mark.parametrize("tamper", [False, True])
def test_task_registry_rechecks_settled_workspace_digest(bound, tamper):
    with launch(bound) as first:
        workspace = Path(first.prepared.workspace.root)
        (workspace / "src/code.py").write_text("legitimate synthetic implementation\n")
        final_digest = settle(first)
        assert final_digest != first.prepared.initial_workspace_digest
        assert (
            inspect_task_context(first.store, "implementation", 1, first.lease)
            == first.prepared
        )
    if tamper:
        (workspace / "src/code.py").write_text("changed after stopped scan\n")
        with (
            pytest.raises(ValueError),
            launch(bound, directory=bound.root.with_name("retry")),
        ):
            pytest.fail("stale final workspace digest accepted")
    else:
        with launch(bound, directory=bound.root.with_name("retry")) as second:
            assert second.attempt == 2
            assert (
                Path(second.prepared.workspace.root) / "src/code.py"
            ).read_bytes() == b"ORIGINAL = True\r\n"


def test_task_reservation_cas_failure_leaves_no_half_attempt_and_no_launch(
    bound, monkeypatch
):
    with launch(bound) as first:
        settle(first)
    before_state, before_history = load_state(bound)
    original = PlanningStore.reserve_task_attempt
    late = TaskEnvelope(1, Completed({"late": True}), "late-history-race")

    def append_history_then_reserve(self, *args, **kwargs):
        with PlanningStore(bound.database) as concurrent:
            assert (
                concurrent.apply_task("implementation", late)
                == Disposition.HISTORY_ONLY
            )
        return original(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(
            PlanningStore, "reserve_task_attempt", append_history_then_reserve
        )
        with (
            pytest.raises(ValueError),
            launch(bound, directory=bound.root.with_name("raced-root")),
        ):
            pytest.fail("stale history CAS yielded a launch")
    state, history = load_state(bound)
    assert state == before_state
    assert history == (*before_history, (late, Disposition.HISTORY_ONLY))
    assert len(journals(bound)) == 2
    with (
        pytest.raises(ValueError),
        launch(bound, directory=bound.root.with_name("bypass-race")),
    ):
        pytest.fail("unmatched failed reservation was adopted")


@pytest.mark.parametrize("changed", ["timestamp", "missing_prepared", "attempt_events"])
def test_task_context_inspection_requires_exact_committed_pair(
    bound, monkeypatch, changed
):
    with launch(bound) as handle:
        if changed == "attempt_events":
            original = handle.store.load_task

            def changed_state(run_id):
                state = original(run_id)
                attempt = state.current_attempt
                return replace(
                    state,
                    attempts=(
                        *state.attempts[:-1],
                        replace(attempt, events=attempt.events[:-1]),
                    ),
                )

            monkeypatch.setattr(handle.store, "load_task", changed_state)
        else:
            original = handle.store.load_task_history

            def changed_history(run_id):
                history = original(run_id)
                if changed == "missing_prepared":
                    return history[:-1]
                return (
                    (replace(history[0][0], occurred_at="different"), history[0][1]),
                    *history[1:],
                )

            monkeypatch.setattr(handle.store, "load_task_history", changed_history)
        with pytest.raises(ValueError):
            inspect_task_context(handle.store, "implementation", 1, handle.lease)
        with pytest.raises(ValueError):
            handle.claim()


def test_task_registry_uses_latest_stop_observation(bound):
    with launch(bound) as first:
        settle(first)
        settle(first, digest=False, stopped=False)
    with (
        pytest.raises(ValueError),
        launch(bound, directory=bound.root.with_name("revoked-stop")),
    ):
        pytest.fail("older stop receipt bypassed latest unknown observation")


@pytest.mark.parametrize(
    "duplicate", [copy, replace, deepcopy], ids=["copy", "replace", "deepcopy"]
)
def test_task_launch_copies_share_single_use_and_expiry(bound, duplicate):
    with launch(bound) as handle:
        copied = duplicate(handle)
        assert copied.claim() == handle.prepared
        with pytest.raises(ValueError):
            handle.claim()
        with pytest.raises(ValueError):
            duplicate(handle).claim()
    for candidate in (handle, copied):
        with pytest.raises(ValueError):
            candidate.check()
        with pytest.raises(ValueError):
            candidate.claim()


@pytest.mark.parametrize("location", ["registry", "manifest"])
def test_task_context_fifo_rejection_is_bounded_without_launch(
    bound, monkeypatch, location
):
    with launch(bound) as first:
        settle(first)
        context = Path(first.prepared.context.path)
    path = (
        journals(bound)[0] if location == "registry" else context / ".task-context.json"
    )
    mode = 0o600 if location == "registry" else 0o400
    if location == "manifest":
        context.chmod(0o700)
    path.unlink()
    os.mkfifo(path, mode)
    if location == "manifest":
        context.chmod(0o500)
    original = boundary._read_file
    reads, rescues, rescue_errors = [], [], []

    def guarded_read(selected, **kwargs):
        if selected != path:
            return original(selected, **kwargs)
        reads.append(selected)
        finished = threading.Event()

        def release_blocked_reader():
            # A real writer releases a blocking-open regression, then fails below.
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
            return original(selected, **kwargs)
        finally:
            finished.set()
            rescue.join(timeout=3)
            assert not rescue.is_alive(), "FIFO rescue thread did not settle"

    monkeypatch.setattr(boundary, "_read_file", guarded_read)
    before = load_state(bound)
    target = bound.root.with_name("fifo-bypass")
    with pytest.raises((ValueError, OSError)), launch(bound, directory=target):
        pytest.fail("FIFO control granted a new launch")
    assert reads == [path]
    assert rescues == [] and rescue_errors == []
    assert load_state(bound) == before and not target.exists()


@pytest.mark.parametrize("earlier_failure", [False, True])
def test_task_preparation_marker_close_failure_preserves_original_and_paths(
    bound, monkeypatch, earlier_failure
):
    first = OSError("initial marker creation failed")
    closing = OSError("marker close failed after real close")
    original_write, original_close = boundary._write_marker, os.close
    marker_fds, closed = [], []

    def capture_marker(fd, path, value):
        marker_fds.append(fd)
        if earlier_failure:
            raise first
        return original_write(fd, path, value)

    def close_then_fail(fd):
        original_close(fd)
        if fd in marker_fds and not closed:
            closed.append(fd)
            raise closing

    with monkeypatch.context() as patch:
        patch.setattr(boundary, "_write_marker", capture_marker)
        patch.setattr(os, "close", close_then_fail)
        with pytest.raises(OSError) as caught, launch(bound):
            pytest.fail("unconfirmed marker close yielded a launch")
    assert caught.value is (first if earlier_failure else closing)
    assert len(closed) == 1
    paths = journals(bound)
    assert len(paths) == 1
    notes = "\n".join(getattr(caught.value, "__notes__", ()))
    assert str(paths[0]) in notes and str(bound.root) in notes
    if earlier_failure:
        assert str(closing) in notes
    state, history = load_state(bound)
    if earlier_failure:
        assert history == () and state.current_attempt is None
    else:
        assert len(history) == 2 and state.current_attempt.reconciliation is None
    with (
        pytest.raises((ValueError, OSError)),
        launch(bound, directory=bound.root.with_name("bypass-close")),
    ):
        pytest.fail("marker close uncertainty was treated as stopped")
