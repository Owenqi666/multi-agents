"""Immutable coordinates for integration tests, reviews, and acceptance."""

from dataclasses import dataclass

from .integration import IntegrationCandidate
from .integration_testing import IntegrationTestResult
from .task_execution import task_execution_hash
from .task_test_execution import TaskTestPolicy
from .tasks import FrozenProjectRules


@dataclass(frozen=True, slots=True)
class IntegrationTestRequest:
    candidate: IntegrationCandidate
    rules: FrozenProjectRules
    policy: TaskTestPolicy
    directory: str


@dataclass(frozen=True, slots=True)
class IntegrationTestAttempt:
    number: int
    request: IntegrationTestRequest
    result: IntegrationTestResult | None = None

    @property
    def evidence_hash(self) -> str:
        return task_execution_hash(self)


@dataclass(frozen=True, slots=True)
class IntegrationReviewLink:
    round_id: str
    test_attempt: int
    snapshot_id: str
    directory: str
    directory_identity: tuple[int, int]


@dataclass(frozen=True, slots=True)
class IntegrationAcceptance:
    acceptance_id: str
    integration_id: str
    target_ref: str
    target_baseline: str
    candidate: IntegrationCandidate
    test_attempt: int
    test_evidence_hash: str
    review_round_id: str
    snapshot_id: str
    review_checkpoint_hash: str
    accepted_at: str
