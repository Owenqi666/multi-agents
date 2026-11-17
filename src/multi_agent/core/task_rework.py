"""Immutable, boundary-validated provenance for one scoped task rework."""

from dataclasses import dataclass

from .events import JSONValue, freeze_json
from .task_execution import task_execution_hash


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskReworkSource:
    task_run_id: str
    implementation_attempt: int
    task_input_hash: str
    candidate_commit: str
    candidate_hash: str
    task_history_length: int
    task_history_hash: str
    test_attempt: int
    test_history_length: int
    test_history_hash: str
    review_round_id: str
    review_snapshot_id: str
    review_checkpoint_hash: str
    assessment: JSONValue
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "assessment", freeze_json(self.assessment))

    @property
    def source_hash(self) -> str:
        return task_execution_hash(self)
