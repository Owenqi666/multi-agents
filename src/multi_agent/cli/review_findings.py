"""Resolve a user-selected finding against its original, frozen source."""

from typing import Any

from multi_agent.core.reports import check_report
from multi_agent.core.state import SeatConfig
from multi_agent.store import ReviewStore
from multi_agent.store.review_annotations import FindingAnchor

from .review_inputs import load_review_input


def resolve_finding(
    store: ReviewStore, anchor: FindingAnchor
) -> tuple[dict[str, Any], SeatConfig]:
    metadata = store.load_review(anchor.round_id)
    if not isinstance(metadata, dict) or metadata.get("kind") != "review":
        raise ValueError("finding annotations require an original review round")
    finding, seat = anchor.find(store.load_round(anchor.round_id))
    snapshot = load_review_input(store, anchor.snapshot_id)
    report = check_report({"findings": [finding]}, snapshot)
    return report["findings"][0], seat
