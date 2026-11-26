"""Finding discussions and rework evidence, separate from the assessed verdict."""

from dataclasses import asdict
from typing import Any

from multi_agent.cli.integration_autorework import (
    check_confirmation_decisions,
    rework_directories,
)
from multi_agent.cli.integration_cli import _view as integration_view
from multi_agent.cli.integration_review import _path_owners, select_rework_member
from multi_agent.cli.integration_rework_runtime import prepare_integration_rework_actor
from multi_agent.cli.integration_verification import _current_review, verification_view
from multi_agent.cli.runner import view_round
from multi_agent.cli.task_context import _store_database
from multi_agent.cli.task_test_cli import _existing_lease
from multi_agent.core.integration import IntegrationMember, IntegrationRecord
from multi_agent.core.review_input import review_source
from multi_agent.store.review_annotations import FindingAnchor, finding_activity

from .resources import _owned, _read_snapshot, _ResourceStore, _scope
from .workspace import Workspace


def findings(workspace: Workspace, round_id: str) -> dict[str, Any]:
    database, common = _scope(workspace, "review", round_id)
    with _ResourceStore.open_readonly(database) as store:
        _owned(store, common, "review", round_id, archive=workspace.archive is not None)
        review = view_round(store, round_id)
        if review["metadata"]["kind"] != "review":
            raise ValueError("use the original review round for finding discussions")
        snapshot_id = review["snapshot_id"]
        source = review_source(_read_snapshot(store, snapshot_id))
        activity = finding_activity(store._connection, round_id)
    assessment = review["assessment"]
    attempts = {seat["key"]: seat["current_attempt"] for seat in review["seats"]}
    groups = []
    for group in assessment["groups"]:
        members = []
        for member in group["members"]:
            seat = member["seat"]
            anchor = FindingAnchor(
                round_id, snapshot_id, seat, attempts[seat], member["finding"]["id"]
            )
            members.append(
                {
                    **member,
                    "anchor": asdict(anchor),
                    "activity": activity.get(anchor, []),
                }
            )
        groups.append({**group, "members": members})
    return {
        "round_id": round_id,
        "snapshot_id": snapshot_id,
        "status": assessment["status"],
        "reasons": assessment["reasons"],
        "source": {
            "kind": "pasted_diff" if source.repository is None else "repository",
            "baseline": source.baseline,
            "candidate": source.candidate,
            "correction_baseline": source.candidate,
        },
        "groups": groups,
    }


def record_decision(
    workspace: Workspace, round_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    if workspace.archive is not None:
        raise ValueError("the selected archive is read-only")
    if not isinstance(body, dict) or set(body) != {"anchor", "decision", "reason"}:
        raise ValueError("a decision requires anchor, decision and reason")
    anchor = FindingAnchor.from_dict(body["anchor"])
    if anchor.round_id != round_id:
        raise ValueError("finding anchor belongs to a different review round")
    database, common = _scope(workspace, "review", round_id)
    with _ResourceStore(database) as store:
        _owned(store, common, "review", round_id)
        return store.record_finding_decision(anchor, body["decision"], body["reason"])


def _owner(member: IntegrationMember) -> dict[str, str]:
    return {"task_run_id": member.task_run_id, "task_id": member.task_input.card.id}


def _blockers(
    record: IntegrationRecord, review: dict[str, Any]
) -> list[dict[str, Any]]:
    result = []
    for group in (review["assessment"] or {}).get("groups", ()):
        for member in group["members"]:
            finding = member["finding"]
            if finding["severity"] in ("P0", "P1"):
                result.append(
                    {
                        "seat": member["seat"],
                        "finding_id": finding["id"],
                        "path": finding["path"],
                        "reference_status": finding["reference_status"],
                        "owners": [
                            _owner(owner)
                            for owner in _path_owners(record, finding["path"])
                        ],
                    }
                )
    return result


def rework_eligibility(workspace: Workspace, integration_id: str) -> dict[str, Any]:
    database, common = _scope(workspace, "integration", integration_id)
    result: dict[str, Any] = {
        "integration_id": integration_id,
        "round_id": None,
        "lock_busy": False,
        "eligible": False,
        "reason": None,
        "owner": None,
        "findings": [],
    }
    with _ResourceStore.open_readonly(database) as store:
        _owned(
            store,
            common,
            "integration",
            integration_id,
            archive=workspace.archive is not None,
        )
        record = store.load_integration(integration_id)
        if workspace.archive is not None:
            return result | {"reason": "The selected archive is read-only."}
        view = integration_view(record, "show")
        if not store.has_integration_rework():
            return result | {"reason": "This database predates integration rework."}
        verification = verification_view(
            store, record, target_current=view["target_is_current"]
        )
        review = verification["reviews"][-1] if verification["reviews"] else None
        if review:
            result.update(
                round_id=review["round_id"], findings=_blockers(record, review)
            )
        if verification["acceptance"]:
            return result | {
                "reason": "The integration already has an acceptance receipt."
            }
        if verification["rework"]:
            return result | {
                "reason": "This integration lineage has already reserved its rework."
            }
        if not view["target_is_current"]:
            return result | {
                "reason": "The target branch has moved; rebuild the integration."
            }
        if view["verification"] != "verified":
            return result | {"reason": "The integration candidate is not verified."}
        try:
            check_confirmation_decisions(store, record)
            if (
                not review
                or not review["source_current"]
                or review["verification"] != "verified"
            ):
                detail = (review["source_errors"] if review else []) or verification[
                    "test_source_errors"
                ]
                return result | {
                    "reason": "; ".join(detail)
                    or "A current, verified review of the tested candidate is required."
                }
            attempt = store.current_integration_test(integration_id)
            if attempt is None:
                return result | {"reason": "The candidate has no current test."}
            member, _ = select_rework_member(
                record,
                review,
                target_protected=attempt.request.rules.rules.protected_paths,
            )
            prepare_integration_rework_actor(store, member)
            link = _current_review(store, record, review["round_id"])
            with _existing_lease(store, member.task_run_id, member.attempt) as lease:
                rework_directories(link, _store_database(store), lease)
        except (ValueError, OSError, KeyError) as error:
            return result | {"reason": str(error)}
        return result | {"eligible": True, "owner": _owner(member)}
