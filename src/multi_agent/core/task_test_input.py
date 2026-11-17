"""Immutable caller-declared test inputs, not a test execution or reservation."""

from dataclasses import dataclass

from .task_events import TaskCandidateFrozen
from .task_execution import task_execution_hash
from .tasks import NamedCommand


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskTestInput:
    version: int = 1
    task_run_id: str
    implementation_attempt: int
    task_id: str
    task_hash: str
    rules_hash: str
    task_input_hash: str
    candidate: TaskCandidateFrozen
    commands: tuple[NamedCommand, ...]
    declaration_source: str
    source_kind: str = "caller_declared"
    task_history_length: int
    task_history_hash: str

    @property
    def input_hash(self) -> str:
        """Include every stored field, without encoding derived properties."""
        return task_execution_hash(self)

    @property
    def candidate_hash(self) -> str:
        """Bind the complete candidate event, including its execution anchor."""
        return task_execution_hash(self.candidate)
