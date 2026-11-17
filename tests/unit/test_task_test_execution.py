"""Managed test preparations and reports use synthetic facts, never a command."""

from dataclasses import FrozenInstanceError, replace

import pytest
import test_task_tests as legacy

from multi_agent.core import Disposition
from multi_agent.core.task_execution import task_execution_hash
from multi_agent.core.task_test_events import (
    TaskTestAttemptFinished,
    TaskTestAttemptStarted,
    TaskTestCancelRequested,
    TaskTestCommandExited,
    TaskTestCommandReported,
    TaskTestCommandStarted,
    TaskTestEnvelope,
    TaskTestExecutionPrepared,
    TaskTestProblem,
)
from multi_agent.core.task_test_execution import (
    TaskTestCheckout,
    TaskTestContext,
    TaskTestPolicy,
    TaskTestScratch,
    prepared_for_test,
    reserve_task_test_execution,
)
from multi_agent.core.task_test_state import TaskTestPhase
from multi_agent.core.workspaces import WorkspaceControl


def preparation(
    value, *, number=1, history=(), repository="/synthetic/repository/.git"
):
    root = f"/synthetic/test-{number}"
    identity = number * 100
    return TaskTestExecutionPrepared(
        TaskTestCheckout(
            input=value,
            repository=repository,
            database_path="/synthetic/tests.sqlite",
            database_identity=(1, 4),
            database_parent_identity=(1, 5),
            root=f"{root}/checkout",
            root_identity=(1, identity),
            git_directory=f"{root}/checkout/.git",
            git_identity=(1, identity + 1),
            owner_token=f"owner-{number}",
            object_format="sha1",
            initial_digest=value.candidate.delivery.digest,
            controls=(
                WorkspaceControl("HEAD", (1, identity + 2), b"\xff\x00\r\n"),
                WorkspaceControl("absent", None, None),
            ),
            objects=(
                ("commit", value.candidate.delivery.candidate),
                ("tree", value.candidate.delivery.tree),
            ),
            observed_test_attempt=number - 1 or None,
            test_history_length=len(history),
            test_history_hash=task_execution_hash(history),
        ),
        TaskTestContext(
            path=f"{root}/context",
            identity=(1, identity + 3),
            token=f"context-{number}",
            manifest_sha256="a" * 64,
        ),
        TaskTestPolicy(
            executable="/synthetic/codex",
            executable_identity=(1, 9),
            executable_sha256="b" * 64,
            timeout=30,
            grace=2,
            output_bytes_limit=8192,
        ),
        TaskTestScratch(
            path=f"{root}/scratch",
            identity=(1, identity + 4),
            home_identity=(1, identity + 5),
            tmp_identity=(1, identity + 6),
            cache_identity=(1, identity + 7),
        ),
    )


def pair(state, *, number=1, prepared=None):
    value = state.reference_input
    return (
        TaskTestEnvelope(1, number, TaskTestAttemptStarted(value), f"start-{number}"),
        TaskTestEnvelope(
            1,
            number,
            prepared or preparation(value, number=number),
            f"prepare-{number}",
        ),
    )


def managed():
    implementation, state = legacy.setup_stage()
    started, prepared = pair(state)
    return implementation, reserve_task_test_execution(
        state, started, prepared, implementation
    )


def retryable():
    implementation, state = managed()
    for event in (
        TaskTestProblem(None, None, "synthetic preflight failure"),
        legacy.receipt(None),
        TaskTestAttemptFinished(),
    ):
        state = legacy.advance(state, implementation, event)
    return implementation, state


def report(sequence=1, code=0):
    return TaskTestCommandReported(
        sequence, code, {"exitCode": code, "stdout": "", "stderr": ""}
    )


def test_task_test_execution_pair_is_immutable_and_does_not_grant_a_result():
    implementation, state = legacy.setup_stage()
    started, prepared = pair(state)
    updated = reserve_task_test_execution(state, started, prepared, implementation)
    assert state.attempts == ()
    assert updated.phase == TaskTestPhase.RUNNING
    assert updated.current_attempt.events == (started.event, prepared.event)
    assert updated.current_attempt.commands == ()
    assert prepared_for_test(updated.current_attempt) is prepared.event
    assert prepared.event.scratch.home == "/synthetic/test-1/scratch/home"
    assert prepared.event.scratch.tmp == "/synthetic/test-1/scratch/tmp"
    assert prepared.event.scratch.cache == "/synthetic/test-1/scratch/cache"
    assert (
        prepared.event.checkout.commit
        == state.reference_input.candidate.delivery.candidate
    )
    assert prepared.event.checkout.commands == state.reference_input.commands
    with pytest.raises(FrozenInstanceError):
        prepared.event.scratch.path = "changed"


def test_task_test_execution_prepared_duplicates_and_conflicts():
    implementation, state = managed()
    original = prepared_for_test(state.current_attempt)
    duplicate, disposition = legacy.observe(state, implementation, original)
    assert duplicate is state and disposition == Disposition.HISTORY_ONLY
    with pytest.raises(ValueError, match="different preparation"):
        legacy.observe(
            state,
            implementation,
            replace(
                original, context=replace(original.context, manifest_sha256="c" * 64)
            ),
        )


def test_task_test_execution_rejects_legacy_upgrade_and_managed_downgrade():
    implementation, state = legacy.started()
    with pytest.raises(ValueError, match="atomic"):
        legacy.observe(state, implementation, preparation(state.reference_input))
    implementation, state = legacy.retryable()
    with pytest.raises(ValueError, match="legacy"):
        reserve_task_test_execution(state, *pair(state, number=2), implementation)
    implementation, state = retryable()
    with pytest.raises(ValueError, match="atomic"):
        legacy.observe(
            state,
            implementation,
            TaskTestAttemptStarted(state.reference_input),
            attempt=2,
        )


@pytest.mark.parametrize(
    "kind",
    [
        "number",
        "input",
        "observed",
        "policy",
        "root",
        "git",
        "context",
        "token",
        "scratch",
        "home",
        "tmp",
        "cache",
        "control",
    ],
)
def test_task_test_execution_retry_rejects_changed_or_reused_preparation(kind):
    implementation, state = retryable()
    previous = prepared_for_test(state.current_attempt)
    prepared = preparation(state.reference_input, number=2)
    if kind == "input":
        prepared = replace(
            prepared,
            checkout=replace(
                prepared.checkout,
                input=replace(state.reference_input, declaration_source="different"),
            ),
        )
    elif kind == "observed":
        prepared = replace(
            prepared, checkout=replace(prepared.checkout, observed_test_attempt=None)
        )
    elif kind == "policy":
        prepared = replace(prepared, policy=replace(prepared.policy, timeout=30.0))
    elif kind == "root":
        prepared = replace(
            prepared, checkout=replace(prepared.checkout, root=previous.checkout.root)
        )
    elif kind == "git":
        prepared = replace(
            prepared,
            checkout=replace(
                prepared.checkout, git_identity=previous.checkout.git_identity
            ),
        )
    elif kind == "control":
        controls = prepared.checkout.controls
        prepared = replace(
            prepared,
            checkout=replace(
                prepared.checkout,
                controls=(
                    replace(
                        controls[0], identity=previous.checkout.controls[0].identity
                    ),
                    *controls[1:],
                ),
            ),
        )
    elif kind == "context":
        prepared = replace(
            prepared,
            context=replace(prepared.context, identity=previous.context.identity),
        )
    elif kind == "token":
        prepared = replace(
            prepared,
            context=replace(prepared.context, token=previous.checkout.owner_token),
        )
    elif kind == "scratch":
        prepared = replace(
            prepared, scratch=replace(prepared.scratch, path=previous.scratch.path)
        )
    elif kind in ("home", "tmp", "cache"):
        field = kind + "_identity"
        prepared = replace(
            prepared,
            scratch=replace(
                prepared.scratch, **{field: getattr(previous.scratch, field)}
            ),
        )
    started, envelope = pair(state, number=2, prepared=prepared)
    if kind == "number":
        envelope = replace(envelope, test_attempt=3)
    with pytest.raises(ValueError):
        reserve_task_test_execution(state, started, envelope, implementation)
    assert len(state.attempts) == 1
    assert state.current_attempt.finished


def test_task_test_execution_retry_checks_all_older_resources():
    implementation, state = retryable()
    old = prepared_for_test(state.current_attempt)
    state = reserve_task_test_execution(state, *pair(state, number=2), implementation)
    for event in (
        TaskTestProblem(None, None, "again"),
        legacy.receipt(None),
        TaskTestAttemptFinished(),
    ):
        state = legacy.advance(state, implementation, event)
    prepared = preparation(state.reference_input, number=3)
    prepared = replace(
        prepared,
        scratch=replace(prepared.scratch, cache_identity=old.scratch.cache_identity),
    )
    with pytest.raises(ValueError, match="fresh"):
        reserve_task_test_execution(
            state, *pair(state, number=3, prepared=prepared), implementation
        )


def test_task_test_report_is_frozen_and_separate_from_exit():
    implementation, state = managed()
    state = legacy.advance(state, implementation, TaskTestCommandStarted(1))
    raw = {"exitCode": 200, "stdout": "raw", "stderr": ""}
    event = TaskTestCommandReported(1, 200, raw)
    raw["exitCode"] = 0
    state = legacy.advance(state, implementation, event)
    assert event.response["exitCode"] == 200
    assert state.current_command.exit_code is None
    assert state.phase == TaskTestPhase.RUNNING
    updated, disposition = legacy.observe(state, implementation, event)
    assert updated is state and disposition == Disposition.HISTORY_ONLY
    with pytest.raises(ValueError, match="another final"):
        legacy.observe(state, implementation, report(code=0))
    state = legacy.advance(state, implementation, TaskTestCommandExited(1, 200))
    state = legacy.advance(state, implementation, TaskTestAttemptFinished())
    assert state.phase == TaskTestPhase.FAILED
    assert state.current_command.exit_code == 200


@pytest.mark.parametrize("code", [-1, 124, 999])
def test_task_test_report_ambiguous_code_never_fills_exit(code):
    implementation, state = managed()
    state = legacy.advance(state, implementation, TaskTestCommandStarted(1))
    state = legacy.advance(state, implementation, report(code=code))
    state = legacy.advance(state, implementation, TaskTestAttemptFinished())
    assert state.phase == TaskTestPhase.AWAITING_EVIDENCE
    assert state.current_command.exit_code is None


@pytest.mark.parametrize(
    "response",
    [
        {"exitCode": True, "stdout": "", "stderr": ""},
        {"exitCode": 1, "stdout": "", "stderr": ""},
        {"result": {"exitCode": 0}},
        {"exitCode": 0, "stdout": ""},
    ],
)
def test_task_test_report_rejects_mismatched_result(response):
    implementation, state = managed()
    state = legacy.advance(state, implementation, TaskTestCommandStarted(1))
    with pytest.raises(ValueError):
        legacy.observe(state, implementation, TaskTestCommandReported(1, 0, response))


def test_task_test_report_late_sequence_is_history_and_cancel_sticks():
    implementation, state = managed()
    for event in (
        TaskTestCommandStarted(1),
        report(),
        TaskTestCommandExited(1, 0),
        legacy.receipt(1),
        TaskTestCommandStarted(2),
    ):
        state = legacy.advance(state, implementation, event)
    same, disposition = legacy.observe(state, implementation, report(code=124))
    assert same is state and disposition == Disposition.HISTORY_ONLY
    with pytest.raises(ValueError):
        legacy.observe(state, implementation, report(sequence=3))
    for event in (
        TaskTestCancelRequested(),
        report(sequence=2),
        TaskTestCommandExited(2, 0),
        TaskTestAttemptFinished(),
    ):
        state = legacy.advance(state, implementation, event)
    assert state.phase == TaskTestPhase.CANCELLING
    state = legacy.advance(state, implementation, legacy.receipt(2))
    assert state.phase == TaskTestPhase.CANCELLED
