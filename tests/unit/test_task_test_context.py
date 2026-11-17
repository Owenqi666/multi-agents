"""Fresh test capability and fixed scratch over a genuinely sealed candidate."""

from copy import copy, deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
import test_task_test_runner as feature_helpers

from multi_agent.cli.task_test_context import (
    inspect_task_test_context,
    prepare_task_test_launch,
)
from multi_agent.core.task_execution import task_execution_hash
from multi_agent.core.task_test_events import TaskTestCommandStarted, TaskTestEnvelope

feature = feature_helpers.feature


def launch(feature, **changes):
    arguments = {
        "repository": feature.repo,
        "directory": feature.directory,
        "required_commands": ("build", "test"),
        "declaration_source": "The caller declares the complete suite.",
        "policy": feature_helpers.policy(feature),
    }
    arguments.update(changes)
    return prepare_task_test_launch(feature.database, "implementation", 1, **arguments)


def reserve_command(handle, sequence=1):
    history = handle.store.load_task_test_history("implementation", 1)
    handle.store.reserve_task_test_command(
        "implementation",
        TaskTestEnvelope(
            1,
            handle.test_attempt,
            TaskTestCommandStarted(sequence),
            "synthetic-reservation",
        ),
        expected_test_attempt=handle.test_attempt,
        expected_command_sequence=None,
        expected_history_length=len(history),
        expected_history_hash=task_execution_hash(history),
    )


def test_task_test_launch_requires_fresh_committed_pair_and_shared_claim(feature):
    with launch(feature) as handle:
        clones = (handle, copy(handle), deepcopy(handle), replace(handle))
        with pytest.raises(ValueError):
            handle.claim(1)
        stored = handle.store.load_task_test("implementation", 1)
        assert stored.current_attempt.events[1] == handle.prepared
        assert (
            inspect_task_test_context(
                handle.store, "implementation", 1, handle.test_attempt, handle.lease
            )
            == handle.prepared
        )
        reserve_command(handle)
        assert clones[1].claim(1) == handle.prepared
        for duplicate in clones:
            with pytest.raises(ValueError):
                duplicate.claim(1)
    for duplicate in clones:
        with pytest.raises(ValueError):
            duplicate.check()
        with pytest.raises(ValueError):
            duplicate.claim(1)


def test_task_test_scratch_is_bound_shared_and_separate_from_host_auth(feature):
    with launch(feature) as handle:
        scratch = handle.prepared.scratch
        assert handle.scratch == scratch
        for name in ("home", "tmp", "cache"):
            path = Path(scratch.path) / name
            info = path.stat()
            assert (info.st_dev, info.st_ino) == getattr(scratch, f"{name}_identity")
            assert info.st_mode & 0o777 == 0o700
            (path / "tool-created-file").write_bytes(b"tool-owned mutable bytes")
        handle.check()
        assert Path(scratch.path).parent == feature.directory
        assert Path(scratch.home) != feature.host_home
        assert feature.auth.parent not in Path(scratch.home).parents
        assert not (Path(scratch.home) / "auth.json").exists()
        assert (
            inspect_task_test_context(
                handle.store, "implementation", 1, 1, handle.lease
            )
            == handle.prepared
        )


def test_task_test_scratch_replaced_identity_is_rejected(feature):
    with launch(feature) as handle:
        original = Path(handle.prepared.scratch.path) / "cache"
        original.rename(original.with_name("cache-retained"))
        original.mkdir(mode=0o700)
        with pytest.raises(ValueError):
            handle.check()


def test_task_test_unknown_owner_blocks_second_launch(feature):
    with launch(feature) as handle:
        first = handle.prepared
    with (
        pytest.raises(ValueError),
        launch(feature, directory=feature.base / "second-test-root"),
    ):
        pytest.fail("unsettled test owner admitted a second launch")
    assert not (feature.base / "second-test-root").exists()
    assert Path(first.context.path).exists()
    assert Path(first.checkout.root).exists()
