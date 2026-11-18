"""Frozen authorization and incremental delivery for a single integration repair."""

from dataclasses import dataclass

from .events import JSONValue
from .integration import IntegrationMember


@dataclass(frozen=True, slots=True)
class IntegrationRepairDelta:
    base: str
    commit: str
    tree: str
    paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IntegrationReworkRequest:
    rework_id: str
    integration_id: str
    review_round_id: str
    snapshot_id: str
    review_checkpoint_hash: str
    test_attempt: int
    test_evidence_hash: str
    member: IntegrationMember
    directory: str
    requested_actor: JSONValue
    findings: tuple[JSONValue, ...]
