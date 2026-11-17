"""Freeze an explicit test suite against an actually inspected current candidate.

This boundary is read-only. Its result records this preparation's source and
history checkpoint; it does not reserve a test attempt or grant execution rights.
"""

from multi_agent.core.task_execution import task_execution_hash, task_execution_json
from multi_agent.core.task_test_input import TaskTestInput
from multi_agent.store.planning_store import PlanningStore

from .snapshot import _string
from .task_candidates import _capture, inspect_task_candidate
from .task_inputs import _name
from .workspaces import ExecutorLease

_MAX_DECLARATION_BYTES = 1_048_576


def _declaration(
    task_run_id: str,
    attempt: int,
    required_commands: tuple[str, ...],
    declaration_source: str,
) -> None:
    _string(task_run_id, "task run id", nonempty=True)
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise ValueError("implementation attempt must be a positive integer")
    if not isinstance(required_commands, tuple) or not required_commands:
        raise ValueError("required commands must be an explicit nonempty tuple")
    for name in required_commands:
        _name(name)
    if len(set(required_commands)) != len(required_commands):
        raise ValueError("required command names must be unique")
    _string(declaration_source, "declaration source", nonempty=True)
    encoded = task_execution_json(
        {
            "required_commands": required_commands,
            "declaration_source": declaration_source,
        }
    ).encode("utf-8")
    if len(encoded) > _MAX_DECLARATION_BYTES:
        raise ValueError("test declaration exceeds the UTF-8 byte limit")


def prepare_task_test_input(
    store: PlanningStore,
    task_run_id: str,
    attempt: int,
    lease: ExecutorLease,
    *,
    required_commands: tuple[str, ...],
    declaration_source: str,
) -> TaskTestInput:
    """Resolve approved names in declared order, then recheck the actual candidate."""
    _declaration(task_run_id, attempt, required_commands, declaration_source)
    guard = _capture(store, task_run_id, attempt, lease, bound=True)
    registry = {
        command.name: command for command in guard.value.frozen_rules.rules.commands
    }
    unknown = tuple(name for name in required_commands if name not in registry)
    if unknown:
        raise ValueError(
            f"test command names are not in the frozen registry: {unknown!r}"
        )
    commands = tuple(registry[name] for name in required_commands)
    candidate = inspect_task_candidate(store, task_run_id, attempt, lease)
    if candidate != guard.state.current_candidate:
        raise ValueError("test candidate changed from the original task history")
    guard.check()
    # Only captured values are used after this final source/identity check.
    return TaskTestInput(
        task_run_id=task_run_id,
        implementation_attempt=attempt,
        task_id=guard.value.card.id,
        task_hash=guard.value.card.task_hash,
        rules_hash=guard.value.frozen_rules.rules_hash,
        task_input_hash=task_execution_hash(guard.value),
        candidate=candidate,
        commands=commands,
        declaration_source=declaration_source,
        task_history_length=len(guard.history),
        task_history_hash=task_execution_hash(guard.history),
    )
