"""Read only the observations and persisted seats belonging to a known web job."""

import fcntl
import json
import os
import re
import stat
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from multi_agent.cli.errors import CLIRefusal
from multi_agent.cli.project_consult import _consultation_lock
from multi_agent.core.events import thaw_json
from multi_agent.core.review_input import ExecutedReviewInput, IntegrationReviewInput

from .jobs import EnvironmentJobs
from .resources import (
    _owned,
    _read_snapshot,
    _ResourceStore,
    _scope,
    _snapshot_repository,
)
from .workspace import Workspace

_MAX_BYTES = 4 * 1024 * 1024
_RETRIES = {"review.retry", "task.retry_review", "integration.retry_review"}


def _journal(
    path: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool | None, bool]:
    recent: deque[dict[str, Any]] = deque(maxlen=50)
    actors: dict[tuple[str, str, str, int], dict[str, Any]] = {}
    if not path.exists():
        return [], [], None, False
    if path.resolve() != path:
        raise ValueError("invalid activity journal path")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise ValueError("invalid private activity journal")
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            owned = True
        else:
            owned = False
            fcntl.flock(fd, fcntl.LOCK_UN)
        offset = max(0, info.st_size - _MAX_BYTES)
        if offset:
            stream.seek(offset)
            stream.readline()
        position = stream.tell()
        count = 0
        for line in stream:
            position += len(line)
            if not line.endswith(b"\n"):
                break
            item = json.loads(line)
            count += 1
            item["sequence"] = position
            resource = item["resource"]
            key = (resource["kind"], resource["id"], item["seat"], item["attempt"])
            actor = actors.setdefault(
                key,
                {
                    "resource": resource,
                    "seat": item["seat"],
                    "provider": item["provider"],
                    "attempt": item["attempt"],
                    "state": "running",
                },
            )
            actor.update(observed_at=item["at"], last_event=item["event"])
            if item["state"] is not None:
                actor["state"] = item["state"]
            recent.append(item)
    return (
        list(recent),
        list(actors.values()),
        owned,
        bool(offset or count > len(recent)),
    )


def _receipt_rounds(action: str, result: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Get the executed rounds and seat versions from this job's own CLI receipt."""
    views: list[dict[str, Any] | None] = []
    if action in {
        "review.run",
        "review.retry",
        "review.confirm",
        "task.review",
        "task.retry_review",
        "task.confirm",
    }:
        views.append(result)
    elif action in {
        "integration.review",
        "integration.confirm",
        "integration.retry_review",
        "integration.rework",
    }:
        # A confirmation receipt also includes its parent for the assessment.
        field = (
            "confirmation"
            if action == "integration.confirm" or "confirmation" in result
            else "review"
        )
        views.append(result.get(field))
        views.append(result.get("rereview", {}).get("review"))
    return {
        view["round_id"]: {
            seat["key"]: seat["current_attempt"] or 0 for seat in view.get("seats", ())
        }
        for view in views
        if view and isinstance(view.get("round_id"), str)
    }


def _review(
    workspace: Workspace,
    identifier: str,
    actors: list[dict[str, Any]],
    *,
    job: dict[str, Any],
    controller_released: bool,
    allow_actions: bool,
    receipt_attempts: dict[str, int] | None,
) -> dict[str, Any]:
    # While the job is active, its private journal already binds this round to
    # the workspace. Poll only the round, never deserialize its frozen Git input.
    resource = {"kind": "review", "id": identifier}
    round_actors = [actor for actor in actors if actor["resource"] == resource]
    args = job.get("args") or {}
    retry_args = (
        args if job["action"] in _RETRIES and args.get("round_id") == identifier else {}
    )
    interrupted = job["state"] == "interrupted"
    if not round_actors and receipt_attempts is None:
        allow_actions = allow_actions and interrupted and bool(retry_args)
    verify_scope = allow_actions or workspace.archive is not None or not round_actors
    database, common = (
        _scope(workspace, "review", identifier)
        if verify_scope
        else (workspace.database, None)
    )
    frozen = None
    with _ResourceStore.open_readonly(database) as store:
        current = store.load_round(identifier)
        executions = (
            cast(dict[str, Any], thaw_json(store.load_execution(identifier)))
            if allow_actions
            else {}
        )
        if common is not None:
            frozen = _read_snapshot(store, current.snapshot_id)
            if _snapshot_repository(frozen) != common:
                raise KeyError(identifier)
            if allow_actions and isinstance(frozen, IntegrationReviewInput):
                links = store.integration_reviews(frozen.integration_id)
                latest = links[-1] if links else None
                test = store.current_integration_test(frozen.integration_id)
                allow_actions = bool(
                    latest
                    and test
                    and latest.test_attempt == test.number
                    and (
                        identifier == latest.round_id
                        or identifier == store.confirmation_for(latest.round_id)
                    )
                )
    retry: dict[str, Any] | None = None
    action_args: dict[str, Any] = {"round_id": identifier}
    if isinstance(frozen, ExecutedReviewInput):
        action = "task.retry_review"
    elif isinstance(frozen, IntegrationReviewInput):
        action = "integration.retry_review"
        action_args["integration_id"] = frozen.integration_id
    else:
        action = "review.retry"
    if retry_args:
        allow_actions = (
            allow_actions
            and job["action"] == action
            and all(retry_args.get(key) == value for key, value in action_args.items())
        )
    seats = []
    for seat in current.seats:
        key = seat.config.key
        attempt = seat.attempts[-1] if seat.attempts else None
        number = attempt.number if attempt else 0
        status = attempt.status.value if attempt else "pending"
        recorded = {
            actor["attempt"]: actor for actor in round_actors if actor["seat"] == key
        }
        for historical in seat.attempts:
            if historical.number in recorded:
                recorded[historical.number]["state"] = historical.status.value
        evidence = executions.get(key, {}).get(str(number), {})
        if recorded:
            matches_attempt = number in recorded
        elif receipt_attempts is not None:
            matches_attempt = receipt_attempts.get(key) == number
        elif round_actors:
            matches_attempt = number == 0
        else:
            # A seat name alone still matches later retries on the same round.
            expected = retry_args.get("expected_attempts", {})
            matches_attempt = expected.get(key) == number
        retryable = (
            matches_attempt
            and ("seat_keys" not in retry_args or key in retry_args["seat_keys"])
            and "completion_unconfirmed" not in evidence
            and (
                status in {"pending", "failed"}
                or (
                    status == "running"
                    and interrupted
                    and controller_released
                    and number in recorded
                )
            )
        )
        seats.append(
            {
                "seat": seat.config.key,
                "provider": seat.config.provider,
                "attempt": number or None,
                "state": status,
                "retryable": allow_actions and retryable,
            }
        )
    missing = [seat for seat in seats if seat["retryable"]]
    if missing:
        action_args["seat_keys"] = [seat["seat"] for seat in missing]
        action_args["expected_attempts"] = {
            seat["seat"]: seat["attempt"] or 0 for seat in missing
        }
        if any(seat["state"] == "running" for seat in missing):
            action_args["recover"] = True
        retry = {"action": action, "args": action_args}
    return {
        "round_id": identifier,
        "snapshot_id": current.snapshot_id,
        "seats": seats,
        "retry": retry,
    }


def read_activity(
    workspace: Workspace, jobs: EnvironmentJobs, identifier: str
) -> dict[str, Any]:
    job = jobs.get(identifier)
    if job is None or not re.fullmatch(r"[a-f0-9]{32}", identifier):
        raise KeyError(identifier)
    events, actors, owned, truncated = _journal(
        jobs.job_root / identifier / "activity.jsonl"
    )
    state = job["state"]
    can_act = (
        workspace.archive is None
        and state in {"finished", "interrupted"}
        and owned is not True
        and jobs.read_gate.active_job_id is None
    )
    resources: dict[tuple[str, str], dict[str, str]] = {}
    for actor in actors:
        resource = actor["resource"]
        resources[resource["kind"], resource["id"]] = resource
    for resource in job.get("resources", ()):
        resources.setdefault(
            (resource["kind"], resource["id"]),
            {"kind": resource["kind"], "id": resource["id"]},
        )
    args = job.get("args") or {}
    # These typed IDs were authorized when the CLI job was submitted. They also
    # give older interrupted jobs an inspection target before a receipt exists.
    for name, kind in (
        ("snapshot_id", "snapshot"),
        ("stage_run_id", "stage"),
        ("planning_run_id", "plan"),
        ("task_run_id", "task"),
        ("integration_id", "integration"),
        ("plan_id", "project-plan"),
        ("round_id", "review"),
    ):
        if name in args:
            resources[kind, args[name]] = {"kind": kind, "id": args[name]}
    if (
        job["action"] == "project.stage.start"
        and {"plan_id", "stage_id"} <= args.keys()
        and workspace.archive is None
        and workspace.database.is_file()
    ):
        with _ResourceStore.open_readonly(workspace.database) as store:
            stage = store.find_stage(args["plan_id"], args["stage_id"])
        if stage is not None:
            resources["stage", stage.stage_run_id] = {
                "kind": "stage",
                "id": stage.stage_run_id,
            }
    review_ids = dict.fromkeys(
        actor["resource"]["id"]
        for actor in actors
        if actor["resource"]["kind"] == "review"
        and not actor["seat"].startswith("followup:")
    )
    receipts = _receipt_rounds(job["action"], job.get("result") or {})
    review_ids.update(dict.fromkeys(receipts))
    retry_target = args.get("round_id") if job["action"] in _RETRIES else None
    if retry_target:
        review_ids[retry_target] = None
    reviews = []
    recovery = []
    for round_id in review_ids:
        try:
            review = _review(
                workspace,
                round_id,
                actors,
                job=job,
                controller_released=owned is False,
                allow_actions=can_act,
                receipt_attempts=receipts.get(round_id),
            )
        except KeyError:
            continue
        reviews.append(review)
        if review["retry"]:
            recovery.append(
                {"resource": {"kind": "review", "id": round_id}, **review["retry"]}
            )
    consultation_id = (
        (job.get("recovery_consultation_id") or f"consultation-{identifier}")
        if job["action"] == "project.consult"
        else None
    )
    if consultation_id:
        try:
            database, common = _scope(workspace, "consultation", consultation_id)
            with _ResourceStore.open_readonly(database) as store:
                _owned(
                    store,
                    common,
                    "consultation",
                    consultation_id,
                    archive=workspace.archive is not None,
                )
                consultation = store.show_consultation(consultation_id)
                owner = store.consultation_owner(consultation_id) if can_act else None
            resource = {"kind": "consultation", "id": consultation_id}
            resources["consultation", consultation_id] = resource
            for seat in consultation["seats"]:
                name = f"advisor-{seat['number']}"
                advisor = next(
                    (
                        item
                        for item in actors
                        if item["resource"] == resource and item["seat"] == name
                    ),
                    None,
                )
                if advisor is None:
                    advisor = {
                        "resource": resource,
                        "seat": name,
                        "provider": seat["request"]["provider"],
                        "attempt": 1,
                        "observed_at": seat["finished_at"] or seat["started_at"],
                        "last_event": None,
                    }
                    actors.append(advisor)
                advisor["state"] = seat["status"]
            if (
                state == "interrupted"
                and can_act
                and owner
                and consultation["status"] == "running"
            ):
                try:
                    with _consultation_lock(
                        workspace.data_dir, consultation_id, expected=owner
                    ):
                        recovery.append(
                            {
                                "resource": resource,
                                "action": "project.consult.reconcile",
                                "args": {"consultation_id": consultation_id},
                            }
                        )
                except CLIRefusal:
                    pass
        except KeyError:
            pass
    for actor in actors:
        actor["last_observed_state"] = actor["state"]
        if state in {"interrupted", "finished"} and actor["state"] in {
            "running",
            "cancelling",
        }:
            actor["state"] = "unconfirmed"
    return {
        "job_id": identifier,
        "state": state,
        "observed_at": datetime.now(UTC).isoformat(),
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "controller_holds_journal": owned,
        "history_truncated": truncated,
        "events": events,
        "actors": actors,
        "reviews": reviews,
        "resources": list(resources.values()),
        "recovery": recovery,
    }
