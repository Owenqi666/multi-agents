"""Derive the local workbench inbox from current workflow records."""

import json
from collections.abc import Callable, Iterator
from functools import cache
from typing import Any
from urllib.parse import quote

from multi_agent.cli.integration_cli import _view as integration_view
from multi_agent.cli.integration_verification import verification_view
from multi_agent.cli.project_stage import _prerequisite_run, stage_view
from multi_agent.cli.runner import confirmation_seats
from multi_agent.cli.snapshot import _common_directory
from multi_agent.cli.task_candidates import inspect_task_candidate
from multi_agent.cli.task_cli import _ERRORS
from multi_agent.cli.task_review_cli import view_task_review
from multi_agent.cli.task_test_cli import _existing_lease, view_task_test
from multi_agent.core.integration import IntegrationConflict
from multi_agent.core.state import AttemptStatus, RoundState, RoundStatus, round_status
from multi_agent.store.integration_store import IntegrationStore
from multi_agent.store.project_store import ProjectStore
from multi_agent.store.task_review_store import TaskReviewStore

from .activity import read_activity
from .gate import ReadGate
from .jobs import EnvironmentJobs
from .workspace import Workspace

type Item = dict[str, str]
type StageProof = Callable[[str], bool]

_ROUTES = {
    "consultation": "consultations",
    "goal": "goals",
    "project-plan": "plans",
    "stage": "stages",
    "plan": "task-plans",
    "task": "tasks",
    "integration": "integrations",
    "review": "reviews",
}


def _item(kind: str, identifier: str, label: str, reason: str, detail: str) -> Item:
    return {
        "kind": kind,
        "id": identifier,
        "label": label,
        "reason": reason,
        "detail": detail,
        "href": f"/{_ROUTES[kind]}/{quote(identifier, safe='')}",
    }


def _goals(
    store: ProjectStore, common: str, live: bool, stage_passed: StageProof
) -> Iterator[Item]:
    rows = store._connection.execute(
        """SELECT c.consultation_id FROM project_consultations c
           WHERE c.git_common_dir = ? AND NOT EXISTS (
             SELECT 1 FROM project_goal_consultations g
             WHERE g.consultation_id = c.consultation_id)
           AND NOT EXISTS (SELECT 1 FROM project_consultations newer
              WHERE newer.git_common_dir = c.git_common_dir
                AND newer.project_id IS c.project_id AND newer.baseline_commit = c.baseline_commit
                AND newer.original_goal = c.original_goal AND newer.rowid > c.rowid)""",
        (common,),
    )
    for (identifier,) in rows:
        record = store.show_consultation(identifier)
        if record["status"] == "succeeded":
            yield _item(
                "consultation",
                identifier,
                record["original_goal"],
                "confirm_goal",
                "Review the advisor draft and confirm the goal.",
            )
        elif record["status"] == "failed":
            # The original-goal fallback has no adoption record. Match the later
            # direct confirmation in the same project (or the first goal of a new one).
            confirmed = store._connection.execute(
                """SELECT 1 FROM project_goals g JOIN projects p USING(project_id)
                   WHERE p.git_common_dir = ? AND g.repository = ? AND g.baseline_commit = ?
                     AND g.original_goal = ? AND g.confirmed_goal = g.original_goal
                     AND g.confirmed_at >= ? AND NOT EXISTS (
                       SELECT 1 FROM project_goal_consultations a WHERE a.goal_id = g.goal_id)
                     AND (g.project_id = ? OR (? IS NULL AND g.rowid = (
                       SELECT MIN(first.rowid) FROM project_goals first
                       WHERE first.project_id = g.project_id))) LIMIT 1""",
                (
                    common,
                    record["repository"],
                    record["baseline_commit"],
                    record["original_goal"],
                    record["finished_at"],
                    record["project_id"],
                    record["project_id"],
                ),
            ).fetchone()
            if confirmed:
                continue
            item = _item(
                "consultation",
                identifier,
                record["original_goal"],
                "consultation_failed",
                "Start a new consultation or confirm the original goal.",
            )
            item["href"] = f"/new?consultation={quote(identifier, safe='')}"
            yield item

    @cache
    def delivered(plan_id: str, stage_id: str) -> bool:
        run = _prerequisite_run(store, plan_id, stage_id)
        return bool(run and stage_passed(run.stage_run_id))

    rows = store._connection.execute(
        """SELECT g.goal_id, g.project_id, g.confirmed_goal, p.error, p.plan_id,
                  p.document
           FROM project_goals g JOIN projects r USING(project_id)
           LEFT JOIN project_plans p ON p.rowid = (
             SELECT max(rowid) FROM project_plans WHERE goal_id = g.goal_id)
           WHERE r.git_common_dir = ? AND (p.plan_id IS NULL OR p.finished_at IS NOT NULL)""",
        (common,),
    )
    for identifier, project, goal, error, plan_id, document in rows:
        if document:
            claimed = {
                row[0]
                for row in store._connection.execute(
                    "SELECT stage_id FROM project_stage_runs WHERE plan_id = ?",
                    (plan_id,),
                )
            }
            for stage in json.loads(document)["stages"]:
                stage_id = stage["stage_id"]
                if stage_id in claimed or not all(
                    live and delivered(plan_id, dep) for dep in stage["depends_on"]
                ):
                    continue
                item = _item(
                    "project-plan",
                    plan_id,
                    stage["title"],
                    "choose_stage",
                    stage["goal"],
                )
                item["href"] += f"?stage={quote(stage_id, safe='')}"
                yield item
            continue
        item = _item(
            "goal",
            identifier,
            goal,
            "retry_project_plan" if error else "plan_goal",
            "Planning failed; inspect the result and choose a planner."
            if error
            else "Choose a planner for this confirmed goal.",
        )
        item["href"] = f"/projects/{quote(project, safe='')}"
        yield item


def _replaced_stages(store: ProjectStore, common: str) -> set[str]:
    """A claimed stage replaces the same stage in its plan's revision ancestors."""
    rows = store._connection.execute(
        """WITH RECURSIVE ancestors(plan_id, stage_id) AS (
             SELECT v.source_plan_id, s.stage_id FROM project_stage_runs s
             JOIN project_plan_revisions v USING(plan_id)
             JOIN project_plans p USING(plan_id) JOIN project_goals g USING(goal_id)
             JOIN projects repo USING(project_id) WHERE repo.git_common_dir = ?
             UNION
             SELECT v.source_plan_id, a.stage_id FROM ancestors a
             JOIN project_plan_revisions v ON v.plan_id = a.plan_id
           )
           SELECT s.stage_run_id FROM project_stage_runs s JOIN ancestors USING(plan_id, stage_id)""",
        (common,),
    )
    return {row[0] for row in rows}


def _tasks(
    store: ProjectStore,
    common: str,
    live: bool,
    direct: dict[str, tuple[str, str, int, int | None]],
    hidden_tasks: set[str],
) -> Iterator[Item]:
    replaced = _replaced_stages(store, common)
    # Refreezing a delegated card moves its actionable work to the new revision.
    rows = store._connection.execute(
        """SELECT p.run_id, p.phase, p.card_payload, p.bound_task_run_id,
                  json_extract(p.input_payload, '$.requirements') AS requirements,
                  s.stage_run_id, s.stage_id, s.dispatch_error, s.plan_id, s.mode,
                  (SELECT json_extract(def.value, '$.title')
                   FROM project_plans pp, json_each(pp.document, '$.stages') def
                   WHERE pp.plan_id = s.plan_id AND json_extract(def.value, '$.stage_id') = s.stage_id
                  ) AS stage_title,
                  l.stage_run_id AS leaf_stage, l.card_id, t.phase AS task_phase,
                  t.current_attempt, test.current_test_attempt
           FROM planning_runs p
           LEFT JOIN project_stage_runs s ON s.planning_run_id = p.run_id
           LEFT JOIN project_stage_leaves l ON l.planning_run_id = p.run_id
           LEFT JOIN task_runs t ON t.run_id = p.bound_task_run_id
           LEFT JOIN task_test_stages test ON test.task_run_id = t.run_id
                                      AND test.implementation_attempt = t.current_attempt
           WHERE json_extract(p.input_payload, '$.frozen_rules.repository') = ?
             AND NOT EXISTS (SELECT 1 FROM project_stage_leaves newer
                WHERE newer.stage_run_id = l.stage_run_id AND newer.card_id = l.card_id
                  AND newer.revision > l.revision)
             AND (s.dispatch_error IS NULL OR NOT EXISTS (
               SELECT 1 FROM project_plan_revisions v WHERE v.source_plan_id = s.plan_id))""",
        (common,),
    )
    for row in rows:
        card = json.loads(row["card_payload"]) if row["card_payload"] else {}
        stage = row["stage_run_id"] or row["leaf_stage"]
        task = row["bound_task_run_id"]
        if stage in replaced:
            if task:
                hidden_tasks.add(task)
            continue
        kind = "task" if task else "stage" if stage else "plan"
        identifier = task or stage or row["run_id"]
        label = (
            card.get("goal")
            or row["stage_title"]
            or row["requirements"]
            or row["card_id"]
            or row["stage_id"]
            or card.get("id")
            or str(row["run_id"])
        )
        if (
            live
            and task
            and row["mode"] == "direct"
            and row["task_phase"] == "awaiting_delivery"
        ):
            direct[task] = (
                stage,
                label,
                row["current_attempt"],
                row["current_test_attempt"],
            )
        if row["dispatch_error"]:
            # A dispatch refusal is frozen on this stage. Its plan owns revision.
            item = _item(
                "stage", stage, label, "dispatch_failed", row["dispatch_error"]
            )
            item["href"] = f"/plans/{quote(row['plan_id'], safe='')}"
            yield item
        elif task:
            if row["task_phase"] in {"failed", "cancelled", "planning_required"}:
                yield _item(
                    kind,
                    identifier,
                    label,
                    "implementation_attention",
                    f"Implementation is {row['task_phase']}. Inspect the attempt and available next action.",
                )
            elif row["current_test_attempt"]:
                test = store.load_task_test(task, row["current_attempt"])
                if test.phase in {"failed", "cancelled"}:
                    yield _item(
                        kind,
                        identifier,
                        label,
                        "task_test_failed",
                        f"The current test attempt is {test.phase}. Inspect its evidence.",
                    )
        elif (live and stage and row["phase"] == "pending") or row["phase"] in {
            "failed",
            "cancelled",
            "revision_required",
            "awaiting_review",
            "reconciliation_required",
        }:
            questions = [
                (index, q["question"])
                for index, q in enumerate(card.get("open_questions", ()))
                if q["blocks_dispatch"] and not q.get("resolution")
            ]
            if questions and not stage:
                # Answering on the planning page creates a new frozen input with
                # user-declared decisions. The original card stays immutable.
                answers = {
                    answer[0]
                    for answer in store._connection.execute(
                        """SELECT json_extract(d.value, '$.question')
                           FROM planning_runs p JOIN planning_runs n ON n.rowid > p.rowid,
                                json_each(n.input_payload, '$.decisions') d
                           WHERE p.run_id = ?
                             AND json_remove(n.input_payload, '$.decisions', '$.draft_request', '$.review_request')
                               = json_remove(p.input_payload, '$.decisions', '$.draft_request', '$.review_request')
                             AND json_extract(d.value, '$.source') = 'user_declared'""",
                        (identifier,),
                    )
                }
                for index, question in questions:
                    if question not in answers:
                        item = _item(
                            kind, identifier, label, "planning_question", question
                        )
                        item["href"] = (
                            f"/task-plans/new?from={quote(identifier, safe='')}&question={index}"
                        )
                        yield item
                continue
            item = _item(
                kind,
                identifier,
                label,
                "planning_attention",
                f"Planning is {row['phase']}. Inspect the current attempt and recovery options.",
            )
            if row["leaf_stage"]:
                item["href"] += f"?card={quote(row['card_id'], safe='')}"
            yield item


def _review_status(state: RoundState, live: bool = True) -> RoundStatus:
    status = round_status(state)
    if (
        live
        and status == RoundStatus.IN_PROGRESS
        and all(
            not seat.attempts
            or seat.attempts[-1].status
            in {AttemptStatus.SUCCEEDED, AttemptStatus.FAILED}
            for seat in state.seats
        )
    ):
        # Unstarted seats can be retried, but also occur during an active launch.
        return RoundStatus.INCOMPLETE
    return status


def _standalone_reviews(
    store: ProjectStore, common: str, live: bool, in_progress: dict[str, str]
) -> Iterator[Item]:
    rows = store._connection.execute(
        """SELECT r.round_id, json_extract(s.payload, '$.requirements'), r.parent_round_id FROM round_inputs r
           JOIN snapshots s USING(snapshot_id)
           WHERE COALESCE(json_extract(s.payload, '$.repository'),
                          json_extract(s.payload, '$.bound_repository')) = ?
             AND json_extract(s.payload, '$.kind') IS NULL""",
        (common,),
    )
    for identifier, label, parent in rows:
        state = store.load_round(identifier)
        status = _review_status(state, live)
        if status == RoundStatus.IN_PROGRESS:
            in_progress[identifier] = label or identifier
        elif status == RoundStatus.INCOMPLETE:
            yield _item(
                "review",
                identifier,
                label or identifier,
                "review_incomplete",
                "Inspect the failed or unstarted review seats and retry on this snapshot.",
            )
        elif (
            status == RoundStatus.READY_FOR_ASSESSMENT
            and parent is None
            and len(state.seats) == 2
            and store.confirmation_for(identifier) is None
            and confirmation_seats(state)
        ):
            yield _item(
                "review",
                identifier,
                label or identifier,
                "review_decision",
                "Open the review to cross-confirm its findings.",
            )


def _current_review_status(
    store: TaskReviewStore,
    review: dict[str, Any],
    label: str,
    in_progress: dict[str, str],
) -> RoundStatus:
    child = review["confirmation_round_id"]
    if child or review["status"] == RoundStatus.IN_PROGRESS:
        identifier = child or review["round_id"]
        status = _review_status(store.load_round(identifier))
        if status == RoundStatus.IN_PROGRESS:
            in_progress[identifier] = label
        return status
    return RoundStatus(review["status"])


def _task_reviews(
    workspace: Workspace,
    store: ProjectStore,
    common: str,
    hidden_tasks: set[str],
    in_progress: dict[str, str],
    current_rounds: dict[str, bool],
) -> Iterator[Item]:
    rows = store._connection.execute(
        """SELECT r.round_id, t.run_id,
                  COALESCE(json_extract(t.input_payload, '$.card.goal'), t.task_id)
           FROM round_inputs r JOIN snapshots s USING(snapshot_id)
           JOIN task_runs t
             ON t.run_id = json_extract(s.payload, '$.executed_suite.prepared.checkout.input.task_run_id')
            AND t.current_attempt = json_extract(s.payload, '$.executed_suite.prepared.checkout.input.implementation_attempt')
           JOIN task_test_stages test ON test.task_run_id = t.run_id
             AND test.implementation_attempt = t.current_attempt
             AND test.current_test_attempt = json_extract(s.payload, '$.executed_suite.test_attempt')
           WHERE json_extract(s.payload, '$.kind') = 'executed_task_review'
             AND json_extract(s.payload, '$.source.repository') = ?
             AND r.parent_round_id IS NULL ORDER BY r.rowid DESC""",
        (common,),
    )
    for identifier, task, label in rows:
        if task in hidden_tasks or task in current_rounds:
            continue
        view = view_task_review(workspace.database, identifier)
        current_rounds[task] = view["current_eligible"]
        if not view["source_current"]:
            continue
        status = _current_review_status(store, view, label, in_progress)
        decision = view["assessment"]["status"]
        if status == RoundStatus.INCOMPLETE:
            reason, detail = (
                "review_incomplete",
                "Inspect the incomplete review and retry the missing seats.",
            )
        elif status == RoundStatus.READY_FOR_ASSESSMENT and decision in {
            "needs_rework",
            "user_decision",
        }:
            reason, detail = (
                "review_decision",
                "Inspect the current task review and choose confirmation or rework.",
            )
        else:
            continue
        item = _item("review", identifier, label, reason, detail)
        item["href"] = f"/tasks/{quote(task, safe='')}"
        yield item


def _integration_reason(
    store: IntegrationStore,
    view: dict[str, Any],
    label: str,
    in_progress: dict[str, str],
) -> tuple[str, str] | None:
    if view["superseded_by"] or view["acceptance"]:
        return None
    if not view["target_is_current"]:
        return (
            "rebuild_integration",
            "The target moved. Rebuild the candidate on its current commit.",
        )
    if view["verification"] != "verified":
        return "inspect_candidate", "The candidate could not be verified."
    if view["current_eligible"]:
        return (
            "accept_integration",
            "Tests and review qualify this candidate for acceptance.",
        )
    test = view["test"]
    if not test:
        return "test_integration", "Run all registered tests for this candidate."
    if test["status"] in {"failed", "cancelled"}:
        return (
            "integration_test_failed",
            "Inspect the current test failure before retrying.",
        )
    if test["status"] != "succeeded":
        return None
    if view["test_evidence_verification"] != "verified":
        return "inspect_tests", "The recorded test evidence could not be verified."
    current = next((r for r in reversed(view["reviews"]) if r["source_current"]), None)
    if not current:
        return "review_integration", "Choose reviewers for this tested candidate."
    status = _current_review_status(store, current, label, in_progress)
    if status == RoundStatus.IN_PROGRESS:
        return None
    if status == RoundStatus.INCOMPLETE:
        return (
            "review_incomplete",
            "Inspect the incomplete review and its retry options.",
        )
    return (
        "review_decision",
        "Inspect the current review findings and confirmation or rework options.",
    )


def _integrations(
    workspace: Workspace, common: str, live: bool, in_progress: dict[str, str]
) -> Iterator[Item]:
    with IntegrationStore.open_readonly(workspace.database) as store:
        rows = store._connection.execute(
            """SELECT i.integration_id FROM integrations i WHERE i.repository = ?
               AND (? OR i.conflict_paths IS NOT NULL)
               AND NOT EXISTS (SELECT 1 FROM integration_acceptances a
                               WHERE a.integration_id = i.integration_id)
               AND NOT EXISTS (SELECT 1 FROM integration_rebases b WHERE b.source_id = i.integration_id)
               AND NOT EXISTS (SELECT 1 FROM integration_reworks w WHERE w.source_id = i.integration_id
                               AND w.child_id IS NOT NULL)""",
            (common, live),
        )
        for (identifier,) in rows:
            record = store.load_integration(identifier)
            label = " · ".join(
                dict.fromkeys(member.task_input.card.goal for member in record.members)
            )
            reason: tuple[str, str] | None
            if isinstance(record.result, IntegrationConflict):
                reason = (
                    "integration_conflict",
                    "The integration has conflicting changes.",
                )
            else:
                view = integration_view(record, "show")
                view.update(
                    verification_view(
                        store, record, target_current=view["target_is_current"]
                    )
                )
                reason = _integration_reason(store, view, label, in_progress)
            if reason:
                yield _item("integration", identifier, label, *reason)


def _direct_handoffs(
    workspace: Workspace,
    store: ProjectStore,
    common: str,
    direct: dict[str, tuple[str, str, int, int | None]],
    current_rounds: dict[str, bool],
    stage_passed: StageProof,
) -> Iterator[Item]:
    if not direct:
        return
    members = {
        tuple(row)
        for row in store._connection.execute(
            """SELECT json_extract(m.value, '$.task_run_id'), json_extract(m.value, '$.attempt'),
                  json_extract(m.value, '$.delivery.candidate')
           FROM integrations i, json_each(i.members_payload) m WHERE i.repository = ?""",
            (common,),
        )
    }
    for task, (stage, label, attempt, test_attempt) in direct.items():
        state = store.load_task(task)
        candidate = state.current_candidate
        if not state.candidate_ready or candidate is None:
            continue
        if (task, attempt, candidate.delivery.candidate) in members:
            continue
        if task in current_rounds:
            if current_rounds[task] and stage_passed(stage):
                yield _item(
                    "stage",
                    stage,
                    label,
                    "integrate_stage",
                    "Create an integration for this reviewed stage candidate.",
                )
        elif test_attempt is None:
            try:
                with _existing_lease(store, task, attempt) as lease:
                    inspect_task_candidate(store, task, attempt, lease)
            except _ERRORS:
                continue
            yield _item(
                "task",
                task,
                label,
                "test_task",
                "Run the task tests on this verified candidate.",
            )
        else:
            test = view_task_test(
                workspace.database, task, attempt, test_attempt=test_attempt
            )
            if (
                test["phase"] == "succeeded"
                and test["verification"] == "verified"
                and test["details"]["finished"]
                and not test["details"]["cancel_requested"]
                and test["candidate"] == candidate.delivery.candidate
            ):
                yield _item(
                    "task",
                    task,
                    label,
                    "review_task",
                    "Choose reviewers for this tested task candidate.",
                )


def _recoverable_reviews(
    workspace: Workspace,
    jobs: EnvironmentJobs,
    in_progress: dict[str, str],
) -> Iterator[Item]:
    if not in_progress:
        return
    # A stopped job may predate the recent-jobs list. Inspect only interrupted
    # controller jobs, and let activity evaluate the journal, seats and versions.
    with jobs.lock:
        interrupted = [
            job["job_id"]
            for job in reversed(jobs.jobs.values())
            if job["state"] == "interrupted"
            and job["action"]
            in {
                "review.run",
                "review.retry",
                "review.confirm",
                "task.review",
                "task.retry_review",
                "task.confirm",
                "task.rework",
                "integration.review",
                "integration.retry_review",
                "integration.confirm",
                "integration.rework",
            }
        ]
    for job_id in interrupted:
        for action in read_activity(workspace, jobs, job_id)["recovery"]:
            resource = action["resource"]
            if (
                resource["kind"] == "review"
                and resource["id"] in in_progress
                and action["args"].get("recover") is True
            ):
                round_id = resource["id"]
                item = _item(
                    "review",
                    round_id,
                    in_progress.pop(round_id),
                    "review_incomplete",
                    "Recover the interrupted review seats.",
                )
                item["href"] = f"/jobs/{quote(job_id, safe='')}"
                yield item
        if not in_progress:
            break


def read_attention(
    workspace: Workspace, gate: ReadGate, jobs: EnvironmentJobs | None = None
) -> dict[str, Any]:
    if workspace.database.is_symlink():
        raise ValueError("workspace database must be a regular file")
    if workspace.archive is not None:
        return {"items": [], "complete": True}
    with gate.read() as allowed:
        # The first CLI writer creates the file before committing the initial schema.
        if not workspace.database.exists() or (
            not allowed and workspace.database.stat().st_size == 0
        ):
            return {"items": [], "complete": allowed}
        common = _common_directory(workspace.repository)
        with ProjectStore.open_readonly(workspace.database) as store:

            @cache
            def stage_passed(identifier: str) -> bool:
                stage = store.load_stage(identifier)
                return bool(
                    stage_view(store, workspace.database, stage)["review_satisfied"]
                )

            current_rounds: dict[str, bool] = {}
            direct: dict[str, tuple[str, str, int, int | None]] = {}
            hidden_tasks: set[str] = set()
            in_progress: dict[str, str] = {}
            items = [
                *_goals(store, common, allowed, stage_passed),
                *_tasks(store, common, allowed, direct, hidden_tasks),
                *_standalone_reviews(store, common, allowed, in_progress),
                *_integrations(workspace, common, allowed, in_progress),
            ]
            if allowed:
                items.extend(
                    _task_reviews(
                        workspace,
                        store,
                        common,
                        hidden_tasks,
                        in_progress,
                        current_rounds,
                    )
                )
                items.extend(
                    _direct_handoffs(
                        workspace, store, common, direct, current_rounds, stage_passed
                    )
                )
                if jobs is not None:
                    items.extend(_recoverable_reviews(workspace, jobs, in_progress))
    items.sort(key=lambda item: (item["reason"], item["label"], item["id"]))
    return {"items": items, "complete": allowed}
