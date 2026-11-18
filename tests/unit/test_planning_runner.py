"""Offline planning runner integration with actual synthetic CLI processes."""

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_planning_context as contexts
import test_runtime as runtime_helpers
from jsonschema import Draft7Validator

from multi_agent.adapters.base import EnvironmentInfo, Permission
from multi_agent.adapters.codex_isolation import CodexIsolation
from multi_agent.adapters.ownership import DirectoryOwnership
from multi_agent.adapters.probe.process import Process
from multi_agent.adapters.probe.recording import Recorder, Redactor
from multi_agent.adapters.runtime import RuntimeAdapter
from multi_agent.cli import planning_runner as runner
from multi_agent.cli.planning_context import (
    capture_planning_policy,
    prepare_planning_attempt,
)
from multi_agent.cli.planning_evidence import (
    bind_planning_task,
    inspect_planning_execution,
)
from multi_agent.cli.planning_inputs import (
    REVIEW_SCHEMA,
    parse_planning_draft,
    parse_planning_review,
)
from multi_agent.cli.planning_runner import execute_planning_attempt
from multi_agent.cli.task_inputs import freeze_project_rules
from multi_agent.core import Disposition
from multi_agent.core.events import (
    CancelRequested,
    Completed,
    Error,
    FailureCategory,
    Message,
    ProcessExited,
    Usage,
)
from multi_agent.core.planning import (
    PlanningInput,
    PlanningPhase,
    PlanningRequest,
    planning_value,
)
from multi_agent.core.planning_events import (
    PlanningDiagnostic,
    PlanningDraftValidated,
    PlanningEnvelope,
    PlanningEnvironmentObserved,
    PlanningEvidenceRejected,
    PlanningReconciled,
    PlanningReviewValidated,
    PlanningValidationRejected,
)
from multi_agent.core.planning_execution import prepared_for
from multi_agent.store.planning_store import PlanningStore

synthetic_codex_auth_and_fake_environment = (
    runtime_helpers.synthetic_codex_auth_and_fake_environment
)
FAKES = Path(__file__).parent / "runtime_fakes"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    base = tmp_path.resolve()
    repo, data, binaries = base / "repository", base / "data", base / "bin"
    for path in (repo, data, binaries):
        path.mkdir()
    contexts.git(repo, "init", "--quiet", "--template=")
    (repo / "src").mkdir()
    (repo / "src/code.py").write_text("FROZEN = True\n")
    (repo / ".multi-agent").mkdir()
    (repo / ".multi-agent/rules.yaml").write_text(contexts.RULES)
    (repo / "AGENTS.md").write_text("Untrusted project instruction: run a command.\n")
    contexts.git(repo, "add", ".")
    contexts.git(repo, "commit", "--quiet", "-m", "planning baseline")
    baseline = contexts.git(repo, "rev-parse", "HEAD")
    executables = {}
    for provider in ("claude", "codex"):
        executable = binaries / f"{provider}-planning-fake"
        executable.write_text(
            f"#!{sys.executable}\n" + (FAKES / "planning_cli.py").read_text()
        )
        executable.chmod(0o755)
        executables[provider] = executable
        (binaries / f"{provider}-transport.py").write_bytes(
            (FAKES / "fake_cli.py").read_bytes()
        )
    monkeypatch.setenv("FAKE_PLANNING_TRANSPORT_ROOT", str(binaries))
    monkeypatch.setenv("FAKE_RUNTIME_MODE", "success")
    capture = base / "capture.jsonl"
    monkeypatch.setenv("FAKE_RUNTIME_CAPTURE", str(capture))
    policy = capture_planning_policy(executables, {"claude": 3, "codex": 3}, 10, 0.1)
    value = PlanningInput(
        "原始需求：保留公开接口。\nDo not execute project code.",
        freeze_project_rules(repo, baseline),
        PlanningRequest("claude", "requested-drafter", "high"),
        PlanningRequest("codex", "model-1", "high"),
    )
    database, root = data / "planning.sqlite", data / "owned-plan"
    with PlanningStore(database) as store:
        store.create_planning("plan", value, "created-at")
    adapters = []
    original = runner.RuntimeAdapter

    def adapter(*args, **kwargs):
        actual = original(*args, **kwargs)
        adapters.append(actual)
        return actual

    original_spawn = asyncio.create_subprocess_exec

    async def only_fake(program, *args, **kwargs):
        assert Path(program) in executables.values()
        assert not kwargs.get("shell", False)
        auth = Path(os.environ["CODEX_HOME"]) / "auth.json"
        assert auth.is_relative_to(tmp_path)
        assert (
            json.loads(auth.read_text())["tokens"]["access_token"]
            == "synthetic-access_token"
        )
        return await original_spawn(program, *args, **kwargs)

    async def no_shell(*args, **kwargs):
        pytest.fail("planning runner used a shell")

    monkeypatch.setattr(runner, "RuntimeAdapter", adapter)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", only_fake)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", no_shell)
    return SimpleNamespace(
        base=base,
        repo=repo,
        data=data,
        executables=executables,
        baseline=baseline,
        value=value,
        policy=policy,
        database=database,
        root=root,
        capture=capture,
        adapters=adapters,
    )


def state(setup, run_id="plan"):
    with PlanningStore(setup.database) as store:
        return store.load_planning(run_id)


def history(setup, run_id="plan"):
    with PlanningStore(setup.database) as store:
        return store.load_planning_history(run_id)


def records(setup):
    return (
        [json.loads(line) for line in setup.capture.read_text().splitlines()]
        if setup.capture.exists()
        else []
    )


async def execute(setup, stage="draft", **kwargs):
    if not state(setup).draft_attempts:
        kwargs = {
            "repository": setup.repo,
            "directory": setup.root,
            "policy": setup.policy,
            **kwargs,
        }
    return await execute_planning_attempt(setup.database, "plan", stage, **kwargs)


def assert_released(setup):
    for adapter in setup.adapters:
        receipt = adapter.ownership_receipt()
        assert (
            receipt.settled and receipt.processes_stopped and receipt.resources_released
        )
        assert receipt.logs_sealed and receipt.outputs_complete
    return setup.adapters[-1].ownership_receipt()


async def clean_resources(setup):
    # Assertion-independent cleanup after fault seams are restored.
    for adapter in setup.adapters:
        with Recorder(
            setup.base / f"cleanup-{id(adapter)}.jsonl", Redactor()
        ) as recorder:
            resources = [item for _, item in adapter._ownership._resources]
            for resource in resources:
                if (
                    isinstance(resource, Process)
                    and not resource.ownership_record("cleanup").stopped
                ):
                    resource._recorder = recorder
                    await resource.terminate(0.5)
            for resource in resources:
                if isinstance(resource, CodexIsolation) and not resource._removed:
                    resource.recorder = recorder
                    for process in resource._processes:
                        process._recorder = recorder
                        await resource.terminate(process, 0.5)
                    assert resource.cleanup()


def test_claude_draft_gpt_review_precedes_dispatch(setup):
    async def run():
        try:
            draft = await execute(setup)
            assert (
                draft.phase == PlanningPhase.AWAITING_REVIEW and draft.card is not None
            )
            with PlanningStore(setup.database) as store, pytest.raises(ValueError):
                bind_planning_task(store, "plan", "implementation", "bound-at")
            reviewed = await execute(setup, "review")
            assert reviewed.phase == PlanningPhase.READY
            assert len(setup.adapters) == 2
            for stage, request in (
                ("draft", setup.value.draft_request),
                ("review", setup.value.review_request),
            ):
                attempt = reviewed.current(stage)
                prepared = prepared_for(attempt)
                assert attempt.request.request == request
                assert prepared.policy == setup.policy
                assert (
                    prepared.source_snapshot_id
                    == prepared_for(reviewed.current("draft")).source_snapshot_id
                )
                assert attempt.runtime.exit_code is not None
                assert attempt.reconciliation.process_stopped
                assert (
                    hashlib.sha256(
                        Path(attempt.reconciliation.log_path).read_bytes()
                    ).hexdigest()
                    == attempt.reconciliation.log_sha256
                )
                events = attempt.events
                assert (
                    sum(
                        isinstance(
                            event, (PlanningDraftValidated, PlanningReviewValidated)
                        )
                        for event in events
                    )
                    == 1
                )
                assert sum(isinstance(event, Completed) for event in events) == 1
                assert sum(isinstance(event, ProcessExited) for event in events) == 1
                assert next(
                    i for i, event in enumerate(events) if isinstance(event, Completed)
                ) < next(
                    i
                    for i, event in enumerate(events)
                    if isinstance(event, ProcessExited)
                )
                assert next(
                    i
                    for i, event in enumerate(events)
                    if isinstance(event, ProcessExited)
                ) < next(
                    i
                    for i, event in enumerate(events)
                    if isinstance(event, PlanningReconciled)
                )
                with PlanningStore(setup.database) as store:
                    assert inspect_planning_execution(store, "plan", stage, 1).qualified
                frozen = json.loads(
                    (
                        Path(prepared.attempt_context.path) / "planning-input.json"
                    ).read_text()
                )
                assert frozen == planning_value(setup.value)
            assert reviewed.current("draft").runtime.reported_model == "actual-claude"
            assert reviewed.current("review").runtime.reported_model is None
            assert reviewed.current("review").runtime.reported_effort is None
            assert (
                reviewed.current("review").request.task_hash == reviewed.card.task_hash
            )
            assert_released(setup)
            with PlanningStore(setup.database) as store:
                bind_planning_task(store, "plan", "implementation", "bound-at")
                assert (
                    store.load_task_provenance("implementation").plan_hash
                    == reviewed.provenance().plan_hash
                )
                assert store.load_planning("plan").phase == PlanningPhase.BOUND
        finally:
            await clean_resources(setup)

    asyncio.run(run())


def test_planning_runner_initial_stop_consumes_no_attempt(setup):
    async def run():
        stop = asyncio.Event()
        stop.set()
        result = await execute(setup, stop=stop)
        assert result.phase == PlanningPhase.PENDING
        assert result.draft_attempts == () and history(setup) == ()
        assert setup.adapters == [] and records(setup) == []
        assert not setup.root.exists()

    asyncio.run(run())


@pytest.mark.parametrize("reconciled", [None, False])
@pytest.mark.parametrize("other_run", [False, True])
def test_planning_runner_never_adopts_existing_prepared(setup, reconciled, other_run):
    with PlanningStore(setup.database) as store:
        prepare_planning_attempt(
            store,
            "plan",
            "draft",
            repository=setup.repo,
            directory=setup.root,
            policy=setup.policy,
        )
        if reconciled is not None:
            store.apply_planning(
                "plan",
                PlanningEnvelope(
                    "draft", 1, PlanningReconciled(False, "Synthetic unknown stop")
                ),
            )
        if other_run:
            store.create_planning("another", setup.value, "created-at")
    before = history(setup)

    async def run():
        with pytest.raises(ValueError):
            if other_run:
                await execute_planning_attempt(
                    setup.database,
                    "another",
                    "draft",
                    repository=setup.repo,
                    directory=setup.data / "another-root",
                    policy=setup.policy,
                )
            else:
                await execute(setup)
        assert setup.adapters == [] and records(setup) == []
        assert history(setup) == before
        assert not (setup.data / "another-root").exists()

    asyncio.run(run())


@pytest.mark.parametrize("case", ["invalid-artifact", "invented-resolution"])
def test_planning_runner_validation_failure_preserves_runtime_result(
    setup, monkeypatch, case
):
    monkeypatch.setenv("FAKE_PLANNING_CASE", case)

    async def run():
        try:
            result = await execute(setup)
            attempt = result.current("draft")
            assert result.phase == PlanningPhase.FAILED
            completed = next(
                event for event in attempt.events if isinstance(event, Completed)
            )
            assert attempt.runtime.result == completed.result
            assert attempt.runtime.exit_code is not None
            assert any(
                isinstance(event, PlanningValidationRejected)
                for event in attempt.events
            )
            assert not any(
                isinstance(event, PlanningEvidenceRejected) for event in attempt.events
            )
            assert result.card is None
            assert attempt.reconciliation.log_path is not None
            assert_released(setup)
        finally:
            await clean_resources(setup)

    asyncio.run(run())


@pytest.mark.parametrize("decision", ["needs_revision", "unresolved"])
def test_planning_runner_unresolved_and_revision_cannot_bind(
    setup, monkeypatch, decision
):
    if decision == "unresolved":
        monkeypatch.setenv("FAKE_PLANNING_CASE", "unresolved")
    else:
        monkeypatch.setenv("FAKE_PLANNING_DECISION", "needs_revision")

    async def run():
        try:
            await execute(setup)
            result = await execute(setup, "review")
            assert not result.can_bind
            with PlanningStore(setup.database) as store, pytest.raises(ValueError):
                bind_planning_task(store, "plan", "implementation", "bound-at")
            assert_released(setup)
        finally:
            await clean_resources(setup)

    asyncio.run(run())


def test_planning_runner_failed_attempt_keeps_frozen_policy_and_feedback(
    setup, monkeypatch
):
    async def run():
        try:
            await execute(setup)
            monkeypatch.setenv("FAKE_PLANNING_DECISION", "needs_revision")
            revised = await execute(setup, "review")
            old = revised.current("draft")
            original_prepared = prepared_for(old)
            assert revised.phase == PlanningPhase.REVISION_REQUIRED
            shutil.rmtree(setup.repo)
            monkeypatch.setenv("FAKE_PLANNING_DECISION", "ready")
            again = await execute(setup)
            fresh = again.current("draft")
            prepared = prepared_for(fresh)
            assert fresh.number == 2
            assert prepared.policy == original_prepared.policy
            assert prepared.source_snapshot_id == original_prepared.source_snapshot_id
            assert prepared.run_owner == original_prepared.run_owner
            assert (
                prepared.attempt_context.token
                != original_prepared.attempt_context.token
            )
            feedback = json.loads(
                (
                    Path(prepared.attempt_context.path) / "revision-feedback.json"
                ).read_text()
            )
            assert "needs_revision" in json.dumps(feedback)
            assert "Add coverage" in json.dumps(feedback)
            with PlanningStore(setup.database) as store:
                before = store.load_planning("plan")
                assert (
                    store.apply_planning(
                        "plan", PlanningEnvelope("draft", 1, Completed({"late": True}))
                    )
                    == Disposition.HISTORY_ONLY
                )
                assert store.load_planning("plan") == before
            ready = await execute(setup, "review")
            assert ready.can_bind
            with pytest.raises(ValueError):
                await execute(setup, policy=replace(setup.policy, timeout=11))
            assert_released(setup)
        finally:
            await clean_resources(setup)

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["logs", "adapter"])
def test_planning_runner_prepared_without_owner_remains_unknown(
    setup, monkeypatch, failure
):
    problem = OSError("injected before owned operation")

    def fail(*args, **kwargs):
        raise problem

    target = "prepare_planning_logs" if failure == "logs" else "RuntimeAdapter"

    async def run():
        with monkeypatch.context() as patch:
            patch.setattr(runner, target, fail)
            with pytest.raises(OSError) as caught:
                await execute(setup)
            assert caught.value is problem
        current = state(setup).current("draft")
        assert prepared_for(current) is not None
        assert (
            current.reconciliation is None or not current.reconciliation.process_stopped
        )
        assert records(setup) == []
        with pytest.raises(ValueError):
            await execute(setup)
        assert len(state(setup).draft_attempts) == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "failure", ["manifest", "context", "log_close", "resource", "eof"]
)
def test_planning_runner_preserves_completed_when_evidence_fails(
    setup, monkeypatch, failure
):
    problem = OSError("injected host sealing failure")
    original_seal = runner.seal_planning_execution
    original_events = RuntimeAdapter.events
    original_close = Recorder.close
    observed_terminal = []
    received_terminal = []
    original_observe = RuntimeAdapter._observe

    def observing(adapter, current, event):
        original_observe(adapter, current, event)
        if isinstance(event, Completed):
            received_terminal.append(event)

    async def changed_events(adapter, turn):
        async for event in original_events(adapter, turn):
            if isinstance(event, Completed):
                observed_terminal.append(event)
                if failure == "context":
                    prepared = prepared_for(state(setup).current("draft"))
                    Path(prepared.attempt_context.path).chmod(0o700)
            yield event

    def failing_close(recorder):
        original_close(recorder)
        if failure == "log_close" and received_terminal:
            recorder._healthy = False
            raise problem

    async def fail_or_mutate_seal(*args, **kwargs):
        if failure == "manifest":
            raise problem
        adapter = kwargs["adapter"]
        if failure in ("resource", "eof"):
            original_receipt = adapter.ownership_receipt

            def incomplete():
                receipt = original_receipt()
                if failure == "resource":
                    return replace(
                        receipt,
                        directories=(
                            *receipt.directories,
                            DirectoryOwnership(
                                "synthetic-extra", "/synthetic-unremoved", False
                            ),
                        ),
                    )
                return replace(
                    receipt,
                    processes=tuple(
                        replace(item, stdout_eof=False) for item in receipt.processes
                    ),
                )

            monkeypatch.setattr(adapter, "ownership_receipt", incomplete)
        return await original_seal(*args, **kwargs)

    async def run():
        try:
            with monkeypatch.context() as patch:
                patch.setattr(RuntimeAdapter, "events", changed_events)
                patch.setattr(RuntimeAdapter, "_observe", observing)
                patch.setattr(Recorder, "close", failing_close)
                patch.setattr(runner, "seal_planning_execution", fail_or_mutate_seal)
                if failure in ("manifest", "log_close"):
                    with pytest.raises(OSError) as caught:
                        await execute(setup)
                    assert caught.value is problem
                elif failure == "context":
                    with pytest.raises(ValueError):
                        await execute(setup)
                else:
                    result = await execute(setup)
                    assert result.phase == PlanningPhase.FAILED
            result = state(setup)
            current = result.current("draft")
            assert observed_terminal
            assert current.runtime.result == observed_terminal[0].result
            assert current.runtime.exit_code is not None
            assert any(isinstance(event, Completed) for event in current.events)
            assert any(isinstance(event, ProcessExited) for event in current.events)
            assert any(
                isinstance(event, PlanningEvidenceRejected) for event in current.events
            )
            assert not any(
                isinstance(event, PlanningDraftValidated) for event in current.events
            )
            assert current.reconciliation.process_stopped
            assert (
                current.reconciliation.log_path is None
                and current.reconciliation.log_sha256 is None
            )
            assert result.card is None
        finally:
            await clean_resources(setup)

    asyncio.run(run())


@pytest.mark.parametrize(
    "failure", ["logged_out", "model", "effort", "context", "executable"]
)
def test_planning_runner_checks_environment_before_turn(setup, monkeypatch, failure):
    original_check = RuntimeAdapter.check_environment

    async def checking(adapter):
        environment = await original_check(adapter)
        if failure == "logged_out":
            return replace(environment, logged_in=False)
        if failure == "model":
            return replace(environment, models=("unrequested-model",))
        if failure == "effort":
            return replace(environment, model_efforts={"requested-drafter": ("low",)})
        prepared = prepared_for(state(setup).current("draft"))
        if failure == "context":
            Path(prepared.attempt_context.path).chmod(0o700)
        else:
            setup.executables["claude"].write_text("not the frozen executable")
        return environment

    monkeypatch.setattr(RuntimeAdapter, "check_environment", checking)

    async def run():
        try:
            if failure in ("context", "executable"):
                with pytest.raises(ValueError):
                    await execute(setup)
            else:
                result = await execute(setup)
                assert result.phase == PlanningPhase.FAILED
            current = state(setup).current("draft")
            assert any(
                isinstance(event, PlanningEnvironmentObserved)
                for event in current.events
            )
            assert not any(
                isinstance(event, (Completed, ProcessExited, CancelRequested))
                for event in current.events
            )
            assert current.runtime.exit_code is None
            assert all(
                row["argv"] in (["--version"], ["auth", "status"])
                for row in records(setup)
                if row["kind"] == "spawn"
            )
            assert setup.adapters[-1].ownership_receipt().processes_stopped
        finally:
            await clean_resources(setup)

    asyncio.run(run())


def test_claude_planning_schema_on_first_draft_and_revision(setup, monkeypatch):
    monkeypatch.setenv("FAKE_PLANNING_CASE", "claude-schema-compatibility")

    async def run():
        first = await execute(setup)
        assert first.phase == PlanningPhase.AWAITING_REVIEW
        monkeypatch.setenv("FAKE_PLANNING_DECISION", "needs_revision")
        assert (await execute(setup, "review")).phase == PlanningPhase.REVISION_REQUIRED
        revised = await execute(setup)
        assert revised.phase == PlanningPhase.AWAITING_REVIEW
        return revised

    revised = asyncio.run(run())
    spawns = [r for r in records(setup) if r.get("argv", [])[:1] == ["-p"]]
    assert len(spawns) == len(revised.draft_attempts) == 2
    for attempt, spawn in zip(revised.draft_attempts, spawns, strict=True):
        context = Path(prepared_for(attempt).attempt_context.path)
        frozen = json.loads((context / "output-schema.json").read_text())
        assert frozen["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        argv = spawn["argv"]
        wire = json.loads(argv[argv.index("--json-schema") + 1])
        assert wire == {key: value for key, value in frozen.items() if key != "$schema"}
        validator = Draft7Validator(wire)
        card = planning_value(
            next(e.result for e in attempt.events if isinstance(e, Completed))
        )
        validator.validate(card)
        assert parse_planning_draft(json.dumps(card), setup.value) == revised.card
        for changes in (
            {"unknown": True},
            {"write_scope": ["../other"]},
            {"test_refs": ["unit", "unit"]},
            {"interface_contracts": [{"name": "contract", "definition": 3}]},
            {"open_questions": [{"question": "text", "blocks_dispatch": 1}]},
            {"proposed_tests": [{"name": "unit"}]},
        ):
            invalid = card | changes
            assert not validator.is_valid(invalid)
            with pytest.raises(ValueError):
                parse_planning_draft(json.dumps(invalid), setup.value)
        wrong_baseline = card | {"baseline_commit": "0" * 40}
        validator.validate(wrong_baseline)
        with pytest.raises(ValueError, match="baseline"):
            parse_planning_draft(json.dumps(wrong_baseline), setup.value)
    assert_released(setup)
    (review_request,) = [
        r["message"]
        for r in records(setup)
        if r.get("message", {}).get("method") == "turn/start"
    ]
    review_context = Path(prepared_for(revised.current("review")).attempt_context.path)
    expected_review_schema = json.loads(
        (review_context / "output-schema.json").read_text()
    )
    assert (
        expected_review_schema["properties"]["blocking_issues"].pop("uniqueItems")
        is True
    )
    assert review_request["params"]["outputSchema"] == expected_review_schema


def test_codex_planning_review_strict_wire_preserves_host_constraints(
    setup, monkeypatch
):
    monkeypatch.setenv("FAKE_PLANNING_CASE", "codex-strict-review-schema")

    async def run():
        assert (await execute(setup)).phase == PlanningPhase.AWAITING_REVIEW
        return await execute(setup, "review")

    reviewed = asyncio.run(run())
    attempt = reviewed.current("review")
    assert reviewed.can_bind, [e.detail for e in attempt.events if isinstance(e, Error)]
    context = Path(prepared_for(attempt).attempt_context.path)
    frozen = json.loads((context / "output-schema.json").read_text())
    assert frozen == REVIEW_SCHEMA
    (request,) = [
        row["message"]
        for row in records(setup)
        if row.get("message", {}).get("method") == "turn/start"
    ]
    wire = request["params"]["outputSchema"]
    assert frozen["properties"]["blocking_issues"].pop("uniqueItems") is True
    assert wire == frozen
    decision = planning_value(reviewed.review_artifact)
    assert (
        parse_planning_review(json.dumps(decision), reviewed.card)
        == reviewed.review_artifact
    )
    for invalid in (
        decision | {"decision": "needs_revision", "blocking_issues": ["same", "same"]},
        decision | {"task_hash": "0" * 64},
        decision | {"blocking_issues": ["real blocker"]},
    ):
        Draft7Validator(wire).validate(invalid)
        with pytest.raises(ValueError):
            parse_planning_review(json.dumps(invalid), reviewed.card)
    assert_released(setup)


def test_planning_runner_uses_frozen_output_schema(setup, monkeypatch):
    original_construct = runner.RuntimeAdapter
    original_prepare = runner.prepare_planning_attempt
    captured = []

    def prepared(*args, **kwargs):
        record = original_prepare(*args, **kwargs)
        captured.append(
            json.loads(
                (Path(record.attempt_context.path) / "output-schema.json").read_text()
            )
        )
        return record

    def construct(*args, **kwargs):
        assert kwargs["output_schema"] == captured[-1]
        adapter = original_construct(*args, **kwargs)
        original_session = adapter.start_session

        async def session(config):
            assert config.permission == Permission.READ_ONLY
            assert config.cwd == Path(
                prepared_for(state(setup).current()).attempt_context.path
            )
            return await original_session(config)

        monkeypatch.setattr(adapter, "start_session", session)
        return adapter

    monkeypatch.setattr(runner, "prepare_planning_attempt", prepared)
    monkeypatch.setattr(runner, "RuntimeAdapter", construct)

    async def run():
        try:
            await execute(setup)
            await execute(setup, "review")
            assert len(captured) == 2 and captured[0] != captured[1]
            assert_released(setup)
        finally:
            await clean_resources(setup)

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["tampered", "legacy"])
def test_planning_runner_review_requires_verified_draft(setup, monkeypatch, failure):
    async def run():
        try:
            await execute(setup)
            current = state(setup).current("draft")
            if failure == "tampered":
                path = Path(current.reconciliation.log_path)
                path.chmod(0o600)
                path.write_bytes(path.read_bytes() + b" ")
                path.chmod(0o400)
            else:
                with PlanningStore(setup.database) as store:
                    store.apply_planning(
                        "plan",
                        PlanningEnvelope(
                            "draft",
                            1,
                            PlanningReconciled(
                                True,
                                "Synthetic legacy evidence",
                                "/logs/legacy",
                                "a" * 64,
                            ),
                        ),
                    )
            before, adapters = records(setup), len(setup.adapters)
            with pytest.raises(ValueError):
                await execute(setup, "review")
            assert records(setup) == before and len(setup.adapters) == adapters
            assert state(setup).review_attempts == ()
        finally:
            await clean_resources(setup)

    asyncio.run(run())


def trace_model_events(monkeypatch):
    ready = asyncio.Event()
    readers, completed = [], []
    original = RuntimeAdapter.events

    async def traced(adapter, turn):
        readers.append(asyncio.current_task())
        try:
            async for event in original(adapter, turn):
                if isinstance(event, Message) and event.text == "waiting":
                    ready.set()
                yield event
        finally:
            completed.append(True)

    monkeypatch.setattr(RuntimeAdapter, "events", traced)
    return ready, readers, completed


async def waiting_for(event):
    async with asyncio.timeout(10):
        await event.wait()


async def settle_controller(task):
    if not task.done():
        task.cancel("fixture cleanup")
    done, _ = await asyncio.wait((task,), timeout=10)
    assert task in done, "planning controller abandoned its owned runtime"
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("stage", ["draft", "review"])
def test_planning_runner_repeated_cancellation_settles_once(setup, monkeypatch, stage):
    async def run():
        controller = None
        release = asyncio.Event()
        try:
            if stage == "review":
                await execute(setup)
            monkeypatch.setenv("FAKE_RUNTIME_MODE", "ignore-cancel")
            ready, readers, finished = trace_model_events(monkeypatch)
            entered = asyncio.Event()
            original_stop, original_cancel = Process.terminate, RuntimeAdapter.cancel
            accepted = []

            async def hold_cleanup(process, grace):
                if ready.is_set() and not release.is_set():
                    entered.set()
                    await release.wait()
                await original_stop(process, grace)

            async def cancelling(adapter, turn, *, on_accepted=None):
                def callback():
                    accepted.append(True)
                    if on_accepted is not None:
                        on_accepted()

                return await original_cancel(adapter, turn, on_accepted=callback)

            with monkeypatch.context() as patch:
                patch.setattr(Process, "terminate", hold_cleanup)
                patch.setattr(RuntimeAdapter, "cancel", cancelling)
                controller = asyncio.create_task(execute(setup, stage))
                await waiting_for(ready)
                controller.cancel("first caller cancellation")
                await waiting_for(entered)
                controller.cancel("second caller cancellation")
                await asyncio.sleep(0)
                release.set()
                with pytest.raises(asyncio.CancelledError) as caught:
                    async with asyncio.timeout(10):
                        await controller
                assert caught.value.args == ("first caller cancellation",)
            current = state(setup).current(stage)
            assert state(setup).phase == PlanningPhase.CANCELLED
            assert accepted == [True]
            assert (
                sum(isinstance(event, CancelRequested) for event in current.events) == 1
            )
            assert current.runtime.exit_code is not None
            assert current.reconciliation.process_stopped
            assert len(readers) == 1 and finished == [True]
            assert all(task.done() for task in readers)
            assert_released(setup)
        finally:
            release.set()
            if controller is not None:
                await settle_controller(controller)
            await clean_resources(setup)

    asyncio.run(run())


def test_planning_runner_accepted_cancel_preserves_completed_event(setup, monkeypatch):
    ready_path = setup.base / "ready-to-complete"
    monkeypatch.setenv("FAKE_PLANNING_READY", str(ready_path))
    monkeypatch.setenv("FAKE_PLANNING_CASE", "completed-after-cancel")
    actual_results = []
    original_result = RuntimeAdapter.result

    async def actual_result(adapter, turn):
        result = await original_result(adapter, turn)
        actual_results.append(result)
        return result

    monkeypatch.setattr(RuntimeAdapter, "result", actual_result)

    async def run():
        stop = asyncio.Event()
        controller = asyncio.create_task(execute(setup, stop=stop))
        try:
            async with asyncio.timeout(10):
                while not ready_path.exists():
                    await asyncio.sleep(0.01)
            stop.set()
            async with asyncio.timeout(10):
                result = await controller
            current = result.current("draft")
            assert result.phase == PlanningPhase.CANCELLED
            assert (
                sum(isinstance(event, CancelRequested) for event in current.events) == 1
            )
            completed = next(
                event for event in current.events if isinstance(event, Completed)
            )
            assert current.runtime.result is None
            assert completed.result["baseline_commit"] == setup.baseline
            assert completed.result["id"] == "T-0100"
            assert completed.result == actual_results[-1].terminal.result
            assert (
                completed.result["goal"] == "Implement the frozen original requirement"
            )
            assert current.runtime.exit_code == 0
            assert not any(
                isinstance(event, (PlanningDraftValidated, PlanningEvidenceRejected))
                for event in current.events
            )
            assert current.reconciliation.process_stopped
            assert current.reconciliation.log_path is None
            assert_released(setup)
        finally:
            stop.set()
            await settle_controller(controller)
            await clean_resources(setup)

    asyncio.run(run())


@pytest.mark.parametrize("committed", [False, True])
def test_planning_runner_cancel_callback_commit_uncertainty(
    setup, monkeypatch, committed
):
    monkeypatch.setenv("FAKE_RUNTIME_MODE", "ignore-cancel")
    original = PlanningStore.apply_planning
    failure = sqlite3.OperationalError("injected callback transaction failure")
    attempted = []

    def failing_apply(store, run_id, envelope):
        if isinstance(envelope.event, CancelRequested):
            attempted.append(envelope)
            if len(attempted) == 1:
                if committed:
                    original(store, run_id, envelope)
                raise failure
        return original(store, run_id, envelope)

    monkeypatch.setattr(PlanningStore, "apply_planning", failing_apply)

    async def run():
        ready, readers, finished = trace_model_events(monkeypatch)
        stop = asyncio.Event()
        controller = asyncio.create_task(execute(setup, stop=stop))
        try:
            await waiting_for(ready)
            stop.set()
            with pytest.raises(sqlite3.OperationalError) as caught:
                async with asyncio.timeout(10):
                    await controller
            assert caught.value is failure
            applied = [
                envelope
                for envelope, disposition in history(setup)
                if isinstance(envelope.event, CancelRequested)
                and disposition == Disposition.APPLIED
            ]
            assert len(applied) == 1
            assert len(attempted) == (1 if committed else 2)
            current = state(setup).current("draft")
            assert current.runtime.exit_code is not None
            assert current.reconciliation.process_stopped
            assert not any(
                isinstance(event, PlanningDraftValidated) for event in current.events
            )
            assert len(readers) == 1 and finished == [True]
            assert_released(setup)
        finally:
            stop.set()
            await settle_controller(controller)
            await clean_resources(setup)

    asyncio.run(run())


@pytest.mark.parametrize("event_type", [Completed, ProcessExited, PlanningReconciled])
def test_planning_runner_database_failures_drain_original_consumer(
    setup, monkeypatch, event_type
):
    original = PlanningStore.apply_planning
    failure = sqlite3.OperationalError(
        f"injected {event_type.__name__} transaction failure"
    )
    attempted = []

    def failing_apply(store, run_id, envelope):
        if isinstance(envelope.event, event_type):
            attempted.append(envelope)
            raise failure
        return original(store, run_id, envelope)

    monkeypatch.setattr(PlanningStore, "apply_planning", failing_apply)

    async def run():
        _, readers, finished = trace_model_events(monkeypatch)
        try:
            with pytest.raises(sqlite3.OperationalError) as caught:
                await execute(setup)
            assert caught.value is failure and attempted
            current = state(setup).current("draft")
            assert not any(
                isinstance(event, PlanningDraftValidated) for event in current.events
            )
            assert state(setup).card is None
            assert len(readers) == 1 and finished == [True]
            assert all(task.done() for task in readers)
            assert_released(setup)
            if event_type is PlanningReconciled:
                assert current.reconciliation is None
                before = records(setup)
                with pytest.raises(ValueError):
                    await execute(setup)
                assert records(setup) == before
        finally:
            await clean_resources(setup)

    asyncio.run(run())


@pytest.mark.parametrize("replace_lock", [False, True])
def test_planning_runner_lease_serializes_and_rejects_replacement(
    setup, monkeypatch, replace_lock
):
    original_check = RuntimeAdapter.check_environment

    async def run():
        ready, release = asyncio.Event(), asyncio.Event()

        async def gated_check(adapter):
            environment = await original_check(adapter)
            ready.set()
            await release.wait()
            return environment

        monkeypatch.setattr(RuntimeAdapter, "check_environment", gated_check)
        first = asyncio.create_task(execute(setup))
        try:
            await waiting_for(ready)
            lock = setup.data / ".planning-controller.lock"
            assert lock.is_file() and not lock.is_relative_to(setup.root)
            with PlanningStore(setup.database) as store:
                store.create_planning("other", setup.value, "created-at")
            before = records(setup)
            if replace_lock:
                lock.rename(setup.data / "displaced.lock")
                lock.write_bytes(b"")
            with pytest.raises(ValueError):
                await execute_planning_attempt(
                    setup.database,
                    "other",
                    "draft",
                    repository=setup.repo,
                    directory=setup.data / "other-root",
                    policy=setup.policy,
                )
            assert records(setup) == before
            assert not (setup.data / "other-root").exists()
            release.set()
            if replace_lock:
                with pytest.raises(ValueError):
                    async with asyncio.timeout(10):
                        await first
                assert state(setup).card is None
            else:
                async with asyncio.timeout(10):
                    assert (await first).phase == PlanningPhase.AWAITING_REVIEW
            assert setup.adapters[0].ownership_receipt().processes_stopped
        finally:
            release.set()
            await settle_controller(first)
            await clean_resources(setup)

    asyncio.run(run())


def preflight_gates(monkeypatch):
    ready, cleaning, release = (asyncio.Event() for _ in range(3))
    tasks = []
    original_emit = Recorder.emit
    original_check = RuntimeAdapter.check_environment
    original_terminate = Process.terminate
    used = False

    def recorded(recorder, kind, **fields):
        original_emit(recorder, kind, **fields)
        if kind == "recv" and fields.get("text", "").startswith(
            ("codex-cli ", "fake-claude ")
        ):
            ready.set()

    async def checking(adapter):
        tasks.append(asyncio.current_task())
        return await original_check(adapter)

    async def terminating(process, grace):
        nonlocal used
        if ready.is_set() and not used:
            used = True
            cleaning.set()
            await release.wait()
        await original_terminate(process, grace)

    monkeypatch.setattr(Recorder, "emit", recorded)
    monkeypatch.setattr(RuntimeAdapter, "check_environment", checking)
    monkeypatch.setattr(Process, "terminate", terminating)
    return ready, cleaning, release, tasks


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("caller", [False, True])
def test_planning_runner_preflight_timeout_wins_late_stop(
    setup, monkeypatch, provider, caller
):
    async def run():
        if provider == "codex":
            await execute(setup)
        monkeypatch.setenv("FAKE_RUNTIME_MODE", "version-hang")
        stage = "draft" if provider == "claude" else "review"
        loop = asyncio.get_running_loop()
        original_time, offset = loop.time, [0.0]
        stop = asyncio.Event()
        controller = None
        with monkeypatch.context() as clock_patch:
            clock_patch.setattr(loop, "time", lambda: original_time() + offset[0])
            with monkeypatch.context() as patch:
                ready, cleaning, release, tasks = preflight_gates(patch)
                try:
                    controller = asyncio.create_task(execute(setup, stage, stop=stop))
                    await waiting_for(ready)
                    offset[0] = setup.adapters[-1].deadline_at + 0.01 - original_time()
                    loop.call_soon(lambda: None)
                    await waiting_for(cleaning)
                    assert tasks[0].cancelling() == 1
                    if caller:
                        controller.cancel("late caller cancellation")
                    else:
                        stop.set()
                    await asyncio.sleep(0)
                    assert tasks[0].cancelling() == 1
                    release.set()
                    async with asyncio.timeout(10):
                        if caller:
                            with pytest.raises(asyncio.CancelledError) as caught:
                                await controller
                            assert caught.value.args == ("late caller cancellation",)
                        else:
                            await controller
                    current = state(setup).current(stage)
                    assert state(setup).phase == PlanningPhase.FAILED
                    diagnostics = [
                        event.diagnostic
                        for event in current.events
                        if isinstance(event, PlanningDiagnostic)
                    ]
                    assert any(
                        event.failure_category == FailureCategory.TIMEOUT
                        for event in diagnostics
                    )
                    assert not any(
                        isinstance(event, (CancelRequested, Completed, ProcessExited))
                        for event in current.events
                    )
                    assert current.runtime.exit_code is None
                    assert current.reconciliation.process_stopped
                    assert_released(setup)
                finally:
                    release.set()
                    if controller is not None:
                        await settle_controller(controller)
            await clean_resources(setup)

    asyncio.run(run())


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_planning_runner_repeated_preflight_cancellation_reaches_owned_stop(
    setup, monkeypatch, provider
):
    async def run():
        if provider == "codex":
            await execute(setup)
        monkeypatch.setenv("FAKE_RUNTIME_MODE", "version-hang")
        stage = "draft" if provider == "claude" else "review"
        controller = None
        try:
            with monkeypatch.context() as patch:
                ready, cleaning, release, tasks = preflight_gates(patch)
                try:
                    controller = asyncio.create_task(execute(setup, stage))
                    await waiting_for(ready)
                    controller.cancel("first preflight cancellation")
                    await waiting_for(cleaning)
                    assert tasks[0].cancelling() == 1
                    controller.cancel("second preflight cancellation")
                    await asyncio.sleep(0)
                    assert tasks[0].cancelling() == 1
                    release.set()
                    with pytest.raises(asyncio.CancelledError) as caught:
                        async with asyncio.timeout(10):
                            await controller
                    assert caught.value.args == ("first preflight cancellation",)
                finally:
                    release.set()
                    if controller is not None:
                        await settle_controller(controller)
            current = state(setup).current(stage)
            assert state(setup).phase == PlanningPhase.CANCELLED
            assert (
                sum(isinstance(event, CancelRequested) for event in current.events) == 1
            )
            assert not any(
                isinstance(event, (Completed, ProcessExited))
                for event in current.events
            )
            assert current.reconciliation.process_stopped
            assert_released(setup)
        finally:
            await clean_resources(setup)

    asyncio.run(run())


@pytest.mark.parametrize("problem", [False, True])
def test_planning_runner_successful_preflight_and_stop_keep_environment_first(
    setup, monkeypatch, problem
):
    async def run():
        stop = asyncio.Event()
        original = RuntimeAdapter.check_environment

        async def checking(adapter):
            environment = await original(adapter)
            stop.set()
            return replace(environment, logged_in=False) if problem else environment

        monkeypatch.setattr(RuntimeAdapter, "check_environment", checking)
        try:
            result = await execute(setup, stop=stop)
            events = result.current("draft").events
            observed = next(
                index
                for index, event in enumerate(events)
                if isinstance(event, PlanningEnvironmentObserved)
            )
            if problem:
                assert result.phase == PlanningPhase.FAILED
                assert not any(isinstance(event, CancelRequested) for event in events)
                assert any(
                    isinstance(event, PlanningDiagnostic)
                    and event.diagnostic.failure_category
                    == FailureCategory.AUTH_EXPIRED
                    for event in events
                )
            else:
                assert result.phase == PlanningPhase.CANCELLED
                cancelled = next(
                    index
                    for index, event in enumerate(events)
                    if isinstance(event, CancelRequested)
                )
                assert observed < cancelled
            assert not any(
                isinstance(event, (Completed, ProcessExited)) for event in events
            )
            assert_released(setup)
        finally:
            await clean_resources(setup)

    asyncio.run(run())


def test_planning_runner_unknown_model_stop_remains_cancelling(setup, monkeypatch):
    monkeypatch.setenv("FAKE_RUNTIME_MODE", "ignore-cancel")

    async def run():
        ready, _, finished = trace_model_events(monkeypatch)
        stop = asyncio.Event()
        original = Process.terminate

        async def cannot_stop(process, grace):
            if ready.is_set():
                raise OSError("injected unknown actual group stop")
            await original(process, grace)

        controller = None
        try:
            with monkeypatch.context() as patch:
                patch.setattr(Process, "terminate", cannot_stop)
                controller = asyncio.create_task(execute(setup, stop=stop))
                await waiting_for(ready)
                stop.set()
                async with asyncio.timeout(10):
                    result = await controller
                current = result.current("draft")
                assert result.phase == PlanningPhase.CANCELLING
                assert current.runtime.exit_code is None
                assert not current.reconciliation.process_stopped
                assert any(
                    isinstance(event, PlanningDiagnostic)
                    and event.diagnostic.raw_type == "adapter.cleanup_incomplete"
                    for event in current.events
                )
                assert not any(
                    isinstance(event, (Error, ProcessExited, PlanningDraftValidated))
                    for event in current.events
                )
                before = records(setup)
                with pytest.raises(ValueError):
                    await execute(setup)
                assert records(setup) == before
                assert finished == [True]
        finally:
            stop.set()
            if controller is not None:
                await settle_controller(controller)
            await clean_resources(setup)

    asyncio.run(run())


def test_planning_runner_exception_precedence_preserves_cause(setup, monkeypatch):
    monkeypatch.setenv("FAKE_RUNTIME_MODE", "ignore-cancel")
    original = PlanningStore.apply_planning
    first = sqlite3.OperationalError("first callback persistence error")
    later = OSError("later sealing error")
    called = []

    def writing(store, run_id, envelope):
        if isinstance(envelope.event, CancelRequested) and not called:
            called.append(True)
            raise first
        return original(store, run_id, envelope)

    async def sealing(*args, **kwargs):
        raise later

    monkeypatch.setattr(PlanningStore, "apply_planning", writing)
    monkeypatch.setattr(runner, "seal_planning_execution", sealing)

    async def run():
        ready, readers, finished = trace_model_events(monkeypatch)
        controller = asyncio.create_task(execute(setup))
        try:
            await waiting_for(ready)
            controller.cancel("original caller cancellation")
            with pytest.raises(asyncio.CancelledError) as caught:
                async with asyncio.timeout(10):
                    await controller
            assert caught.value.args == ("original caller cancellation",)
            assert caught.value.__cause__ is first
            assert state(setup).card is None
            assert state(setup).current("draft").reconciliation.process_stopped
            assert finished == [True] and all(task.done() for task in readers)
            assert_released(setup)
        finally:
            await settle_controller(controller)
            await clean_resources(setup)

    asyncio.run(run())


def test_planning_runner_frozen_schema_fifo_is_rejected_without_spawn(
    setup, monkeypatch
):
    original = runner._output_schema
    rescues = []
    rescue_errors = []

    def replaced(prepared):
        path = Path(prepared.attempt_context.path) / "output-schema.json"
        path.parent.chmod(0o700)
        path.unlink()
        os.mkfifo(path)
        finished = threading.Event()

        def release_blocked_reader():
            # A synchronous FIFO open cannot be interrupted by asyncio.timeout.
            # Rescue a regression with a real peer, then fail the rescue assertion.
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
            return original(prepared)
        finally:
            finished.set()
            rescue.join(timeout=3)
            assert not rescue.is_alive(), "FIFO rescue thread did not settle"

    monkeypatch.setattr(runner, "_output_schema", replaced)

    async def run():
        with pytest.raises((ValueError, TypeError, OSError)):
            async with asyncio.timeout(5):
                await execute(setup)
        assert records(setup) == []
        assert not state(setup).current("draft").stopped
        assert rescues == [], "frozen schema open blocked on a FIFO"
        assert rescue_errors == []

    asyncio.run(run())


def test_planning_runner_consumer_task_creation_failure_still_drains(
    setup, monkeypatch
):
    original = asyncio.create_task
    failure = RuntimeError("injected consumer task creation failure")
    calls = []

    def create(coroutine, *args, **kwargs):
        if coroutine.cr_code.co_name == "consume" and not calls:
            calls.append(coroutine)
            raise failure
        return original(coroutine, *args, **kwargs)

    monkeypatch.setattr(asyncio, "create_task", create)

    async def run():
        _, readers, finished = trace_model_events(monkeypatch)
        try:
            with pytest.raises(RuntimeError) as caught:
                async with asyncio.timeout(10):
                    await execute(setup)
            assert caught.value is failure
            assert len(calls) == 1 and calls[0].cr_frame is None
            assert len(readers) == 1 and finished == [True]
            assert state(setup).card is None
            assert state(setup).current("draft").reconciliation.process_stopped
            assert_released(setup)
        finally:
            await clean_resources(setup)

    asyncio.run(run())


@pytest.mark.parametrize("unsupported", ["model", "effort"])
def test_planning_runner_obeys_actual_codex_catalog(setup, monkeypatch, unsupported):
    request = PlanningRequest(
        "codex",
        "unknown-model" if unsupported == "model" else "model-1",
        "medium" if unsupported == "effort" else "high",
    )
    setup.value = replace(setup.value, review_request=request)
    setup.database = setup.data / "catalog.sqlite"
    with PlanningStore(setup.database) as store:
        store.create_planning("plan", setup.value, "created-at")

    async def run():
        try:
            await execute(setup)
            before = len(records(setup))
            result = await execute(setup, "review")
            current = result.current("review")
            assert result.phase == PlanningPhase.FAILED
            observed = next(
                event
                for event in current.events
                if isinstance(event, PlanningEnvironmentObserved)
            )
            assert observed.environment["models"] == ("model-1", "model-2")
            assert observed.environment["model_efforts"]["model-1"] == ("high", "low")
            methods = [
                row["message"]["method"]
                for row in records(setup)[before:]
                if row["kind"] == "request"
            ]
            assert "model/list" in methods
            assert "thread/start" not in methods and "turn/start" not in methods
            assert not any(isinstance(event, ProcessExited) for event in current.events)
            assert_released(setup)
        finally:
            await clean_resources(setup)

    asyncio.run(run())


def test_planning_runner_unknown_environment_fields_remain_null(setup, monkeypatch):
    original = RuntimeAdapter.check_environment

    async def unknown(adapter):
        await original(adapter)
        return EnvironmentInfo()

    monkeypatch.setattr(RuntimeAdapter, "check_environment", unknown)

    async def run():
        try:
            result = await execute(setup)
            assert result.phase == PlanningPhase.AWAITING_REVIEW
            environment = next(
                event.environment
                for event in result.current("draft").events
                if isinstance(event, PlanningEnvironmentObserved)
            )
            assert planning_value(environment) == {
                "cli_version": None,
                "logged_in": None,
                "models": None,
                "model_efforts": None,
            }
            assert_released(setup)
        finally:
            await clean_resources(setup)

    asyncio.run(run())


def test_planning_runner_failed_attempt_consumes_fixed_budget(setup, monkeypatch):
    setup.policy = replace(
        setup.policy,
        providers=tuple(
            replace(item, max_attempts=1) for item in setup.policy.providers
        ),
    )
    monkeypatch.setenv("FAKE_PLANNING_CASE", "invalid-artifact")

    async def run():
        try:
            assert (await execute(setup)).phase == PlanningPhase.FAILED
            before, adapters = records(setup), len(setup.adapters)
            with pytest.raises(ValueError):
                await execute(setup)
            assert len(state(setup).draft_attempts) == 1
            assert records(setup) == before and len(setup.adapters) == adapters
            assert_released(setup)
        finally:
            await clean_resources(setup)

    asyncio.run(run())


@pytest.mark.parametrize("invalid", ["database_symlink", "lock_symlink", "root_parent"])
def test_planning_runner_rejects_noncanonical_controller_paths(setup, invalid):
    database, root = setup.database, setup.root
    if invalid == "database_symlink":
        database = setup.data / "database-alias"
        database.symlink_to(setup.database)
    elif invalid == "lock_symlink":
        (setup.data / ".planning-controller.lock").symlink_to(
            setup.base / "outside-lock"
        )
    else:
        root = setup.base / "not-a-sibling-root"

    async def run():
        with pytest.raises((ValueError, OSError)):
            await execute_planning_attempt(
                database,
                "plan",
                "draft",
                repository=setup.repo,
                directory=root,
                policy=setup.policy,
            )
        assert state(setup).draft_attempts == ()
        assert records(setup) == [] and setup.adapters == []
        assert not (setup.base / "outside-lock").exists()

    asyncio.run(run())


def test_planning_runner_stop_monitor_failure_cannot_validate(setup, monkeypatch):
    failure = OSError("injected stop monitor failure")

    async def run():
        checked = asyncio.Event()
        original = RuntimeAdapter.check_environment

        async def checking(adapter):
            result = await original(adapter)
            checked.set()
            return result

        async def failed_monitor(*args):
            await checked.wait()
            raise failure

        monkeypatch.setattr(RuntimeAdapter, "check_environment", checking)
        monkeypatch.setattr(runner, "_wait_for_stop", failed_monitor)
        try:
            with pytest.raises(OSError) as caught:
                async with asyncio.timeout(10):
                    await execute(setup)
            assert caught.value is failure
            current = state(setup).current("draft")
            assert state(setup).card is None
            assert not any(
                isinstance(event, PlanningDraftValidated) for event in current.events
            )
            assert (
                current.reconciliation.process_stopped
                == setup.adapters[-1].ownership_receipt().processes_stopped
            )
            assert_released(setup)
        finally:
            await clean_resources(setup)

    asyncio.run(run())


def test_planning_runner_unpersisted_cancel_keeps_actual_exit(setup, monkeypatch):
    monkeypatch.setenv("FAKE_RUNTIME_MODE", "ignore-cancel")
    original = PlanningStore.apply_planning
    failure = sqlite3.OperationalError("cancel and bounded repair both failed")
    attempted = []
    actual_exits = []
    original_events = RuntimeAdapter.events

    def writing(store, run_id, envelope):
        if isinstance(envelope.event, CancelRequested):
            attempted.append(envelope)
            raise failure
        return original(store, run_id, envelope)

    async def observed_events(adapter, turn):
        async for event in original_events(adapter, turn):
            if isinstance(event, ProcessExited):
                actual_exits.append(event)
            yield event

    monkeypatch.setattr(PlanningStore, "apply_planning", writing)
    monkeypatch.setattr(RuntimeAdapter, "events", observed_events)

    async def run():
        ready, readers, finished = trace_model_events(monkeypatch)
        stop = asyncio.Event()
        controller = asyncio.create_task(execute(setup, stop=stop))
        try:
            await waiting_for(ready)
            stop.set()
            with pytest.raises(sqlite3.OperationalError) as caught:
                async with asyncio.timeout(10):
                    await controller
            assert caught.value is failure
            current = state(setup).current("draft")
            assert len(attempted) == 2 and attempted[0] == attempted[1]
            assert len(actual_exits) == 1 and actual_exits[0] in current.events
            assert current.runtime.exit_code == actual_exits[0].exit_code
            assert not any(
                isinstance(
                    event, (CancelRequested, Completed, Error, PlanningDraftValidated)
                )
                for event in current.events
            )
            assert current.runtime.result is None
            assert current.reconciliation.process_stopped
            assert current.reconciliation.log_path is None
            assert state(setup).phase == PlanningPhase.FAILED
            assert len(readers) == 1 and finished == [True]
            assert_released(setup)
        finally:
            stop.set()
            await settle_controller(controller)
            await clean_resources(setup)

    asyncio.run(run())


async def usage_commit_uncertainty(
    setup, monkeypatch, *, committed, repeat=False, failure_index=1, fixed_time=False
):
    original_apply = PlanningStore.apply_planning
    failure = sqlite3.OperationalError("uncertain ordinary usage commit")
    attempts = []
    uncertain = []

    def writing(store, run_id, envelope):
        if isinstance(envelope.event, Usage):
            attempts.append(envelope)
            if len(attempts) == failure_index:
                uncertain.append(envelope)
                if committed:
                    original_apply(store, run_id, envelope)
                raise failure
        return original_apply(store, run_id, envelope)

    monkeypatch.setattr(PlanningStore, "apply_planning", writing)
    if repeat:
        original_events = RuntimeAdapter.events
        clock = []

        def distinct_envelope_time():
            # Synthetic host envelope identities, not measured runtime timestamps.
            clock.append(True)
            return (
                "synthetic-fixed-time"
                if fixed_time
                else f"synthetic-observed-{len(clock):04d}"
            )

        async def repeated_usage(adapter, turn):
            async for event in original_events(adapter, turn):
                yield event
                if isinstance(event, Usage):
                    # Explicit second normalized report at the public seam.
                    # This is not a claim of a second old fake CLI wire record.
                    yield event

        monkeypatch.setattr(runner, "_now", distinct_envelope_time)
        monkeypatch.setattr(RuntimeAdapter, "events", repeated_usage)
    _, readers, finished = trace_model_events(monkeypatch)
    try:
        with pytest.raises(sqlite3.OperationalError) as caught:
            await execute(setup)
        assert caught.value is failure
        recorded = history(setup)
        assert sum(
            envelope == uncertain[0] and disposition == Disposition.APPLIED
            for envelope, disposition in recorded
        ) == (2 if repeat and fixed_time else 1)
        usage = [
            envelope
            for envelope, disposition in recorded
            if isinstance(envelope.event, Usage) and disposition == Disposition.APPLIED
        ]
        assert len(usage) == (2 if repeat else 1)
        if repeat:
            assert usage[0].event == usage[1].event
            if fixed_time:
                assert usage[0].occurred_at == usage[1].occurred_at
                assert usage[0] == usage[1]
            else:
                assert usage[0].occurred_at != usage[1].occurred_at
        assert len(attempts) == (2 if repeat else 1) + (0 if committed else 1)
        current = state(setup).current("draft")
        assert sum(isinstance(event, Usage) for event in current.events) == len(usage)
        assert current.runtime.exit_code is not None
        assert current.reconciliation.process_stopped
        assert not any(
            isinstance(event, (PlanningDraftValidated, PlanningReviewValidated))
            for event in current.events
        )
        assert state(setup).card is None
        assert len(readers) == 1 and finished == [True]
        assert all(task.done() for task in readers)
        assert_released(setup)
    finally:
        await clean_resources(setup)


@pytest.mark.parametrize("committed", [False, True])
def test_planning_runner_ordinary_event_commit_uncertainty(
    setup, monkeypatch, committed
):
    asyncio.run(usage_commit_uncertainty(setup, monkeypatch, committed=committed))


def test_planning_runner_equal_usage_with_distinct_envelopes_is_preserved(
    setup, monkeypatch
):
    asyncio.run(
        usage_commit_uncertainty(setup, monkeypatch, committed=True, repeat=True)
    )


@pytest.mark.parametrize("committed", [False, True])
def test_planning_runner_previous_identical_usage_does_not_hide_later_commit(
    setup, monkeypatch, committed
):
    asyncio.run(
        usage_commit_uncertainty(
            setup,
            monkeypatch,
            committed=committed,
            repeat=True,
            failure_index=2,
            fixed_time=True,
        )
    )


def test_planning_runner_unattempted_identical_usage_is_not_deduplicated(
    setup, monkeypatch
):
    asyncio.run(
        usage_commit_uncertainty(
            setup, monkeypatch, committed=True, repeat=True, fixed_time=True
        )
    )
