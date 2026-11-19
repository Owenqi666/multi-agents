"""Project plans share the workbench database and retain their original goal."""

import json
import sqlite3
from dataclasses import asdict
from typing import Any, cast

from multi_agent.core.planning import (
    PlanningInput,
    PlanningPhase,
    PlanningRequest,
    ProjectedDraft,
    planning_json,
)
from multi_agent.core.project_plan import (
    ConfirmedGoal,
    ProjectPlan,
    StageRun,
    render_markdown,
)
from multi_agent.core.state import AttemptStatus
from multi_agent.core.tasks import TaskCard
from multi_agent.core.transitions import (
    ProjectConsultationSeatStatus,
    project_consultation_seat_status,
    project_consultation_status,
    project_plan_status,
    require_project_consultation_seat,
)

from .task_review_store import TaskReviewStore


class ProjectStore(TaskReviewStore):
    def start_consultation(
        self,
        consultation_id: str,
        repository: str,
        git_common_dir: str,
        baseline_commit: str,
        original_goal: str,
        project_id: str | None,
        source_snapshot_id: str,
        advisors: tuple[PlanningRequest, ...],
        started_at: str,
    ) -> None:
        if len(advisors) not in (1, 2):
            raise ValueError("a consultation requires one or two advisors")
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            if (
                project_id is not None
                and self.project_repository(project_id) != git_common_dir
            ):
                raise ValueError("the project belongs to a different Git repository")
            self._connection.execute(
                "INSERT INTO project_consultations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    consultation_id,
                    repository,
                    git_common_dir,
                    baseline_commit,
                    original_goal,
                    project_id,
                    source_snapshot_id,
                    started_at,
                ),
            )
            self._connection.executemany(
                """INSERT INTO project_consultation_seats
                   (consultation_id, number, request) VALUES (?, ?, ?)""",
                (
                    (consultation_id, number, planning_json(request))
                    for number, request in enumerate(advisors, 1)
                ),
            )

    def _consultation_row(self, consultation_id: str) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM project_consultations WHERE consultation_id = ?",
            (consultation_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown consultation {consultation_id}")
        return cast(sqlite3.Row, row)

    def _consultation_seats(self, consultation_id: str) -> tuple[sqlite3.Row, ...]:
        return tuple(
            self._connection.execute(
                "SELECT * FROM project_consultation_seats WHERE consultation_id = ? ORDER BY number",
                (consultation_id,),
            )
        )

    @staticmethod
    def _consultation_seat_statuses(
        seats: tuple[sqlite3.Row, ...],
    ) -> tuple[ProjectConsultationSeatStatus, ...]:
        return tuple(
            project_consultation_seat_status(
                started=seat["started_at"] is not None,
                finished=seat["finished_at"] is not None,
                advice=seat["advice"] is not None,
                error=seat["error"] is not None,
            )
            for seat in seats
        )

    def reserve_consultation_seat(
        self,
        consultation_id: str,
        number: int,
        prompt_template_version: str,
        started_at: str,
    ) -> None:
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            self._consultation_row(consultation_id)
            seats = self._consultation_seats(consultation_id)
            require_project_consultation_seat(
                self._consultation_seat_statuses(seats), number, settling=False
            )
            self._connection.execute(
                """UPDATE project_consultation_seats SET prompt_template_version = ?,
                   started_at = ? WHERE consultation_id = ? AND number = ?""",
                (prompt_template_version, started_at, consultation_id, number),
            )

    def finish_consultation_seat(
        self,
        consultation_id: str,
        number: int,
        finished_at: str,
        *,
        advice: dict[str, Any] | None,
        error: dict[str, Any] | None,
        evidence: dict[str, Any],
    ) -> None:
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            self._consultation_row(consultation_id)
            seats = self._consultation_seats(consultation_id)
            require_project_consultation_seat(
                self._consultation_seat_statuses(seats), number, settling=True
            )
            if (advice is None) == (error is None):
                raise ValueError("consultation settlement requires advice or an error")
            self._connection.execute(
                """UPDATE project_consultation_seats SET finished_at = ?, advice = ?,
                   error = ?, evidence = ? WHERE consultation_id = ? AND number = ?""",
                (
                    finished_at,
                    planning_json(advice) if advice is not None else None,
                    planning_json(error) if error is not None else None,
                    planning_json(evidence),
                    consultation_id,
                    number,
                ),
            )

    def show_consultation(self, consultation_id: str) -> dict[str, Any]:
        with self._connection:
            self._connection.execute("BEGIN")
            consultation = self._consultation_row(consultation_id)
            seats = self._consultation_seats(consultation_id)
            statuses = self._consultation_seat_statuses(seats)
            status = project_consultation_status(statuses)
            return {
                "kind": "goal_consultation",
                **dict(consultation),
                "status": status,
                "finished_at": next(
                    (
                        item["finished_at"]
                        for item in reversed(seats)
                        if item["finished_at"]
                    ),
                    None,
                )
                if status != AttemptStatus.RUNNING
                else None,
                "draft": json.loads(seats[-1]["advice"])
                if status == AttemptStatus.SUCCEEDED
                else None,
                "seats": [
                    {
                        **dict(seat),
                        "source_snapshot_id": consultation["source_snapshot_id"],
                        "status": seat_status,
                        **{
                            field: json.loads(seat[field])
                            if seat[field] is not None
                            else None
                            for field in ("request", "advice", "error", "evidence")
                        },
                    }
                    for seat, seat_status in zip(seats, statuses, strict=True)
                ],
            }

    def _adopt_consultation(
        self, goal: ConfirmedGoal, consultation_id: str, adoption: str | None
    ) -> str:
        row = self._consultation_row(consultation_id)
        owner = row["project_id"]
        if owner is None:
            prior = self._connection.execute(
                """SELECT g.project_id FROM project_goal_consultations AS c
                   JOIN project_goals AS g USING (goal_id)
                   WHERE c.consultation_id = ? LIMIT 1""",
                (consultation_id,),
            ).fetchone()
            owner = prior["project_id"] if prior is not None else None
        if owner is not None and owner != goal.project_id:
            raise ValueError("consultation belongs to a different project")
        if (
            row["repository"],
            row["git_common_dir"],
            row["baseline_commit"],
            row["original_goal"],
        ) != (
            goal.repository,
            goal.git_common_dir,
            goal.baseline_commit,
            goal.original_goal,
        ):
            raise ValueError(
                "consultation differs from the goal repository, baseline or original text"
            )
        seats = self._consultation_seats(consultation_id)
        if (
            project_consultation_status(self._consultation_seat_statuses(seats))
            != AttemptStatus.SUCCEEDED
        ):
            raise ValueError("confirmation requires a successful consultation")
        draft = json.loads(seats[-1]["advice"])["draft_goal"]
        decision = "accepted" if goal.confirmed_goal == draft else "edited"
        if adoption is not None and adoption != decision:
            raise ValueError(
                f"the confirmed text requires the {decision} adoption decision"
            )
        return decision

    def confirm_goal(
        self,
        goal: ConfirmedGoal,
        *,
        new_project: bool,
        consultation_id: str | None = None,
        adoption: str | None = None,
    ) -> None:
        if consultation_id is None and adoption is not None:
            raise ValueError("an adoption decision requires a consultation")
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            if new_project:
                self._connection.execute(
                    "INSERT INTO projects VALUES (?, ?)",
                    (goal.project_id, goal.git_common_dir),
                )
            elif self.project_repository(goal.project_id) != goal.git_common_dir:
                raise ValueError("the project belongs to a different Git repository")
            decision = (
                self._adopt_consultation(goal, consultation_id, adoption)
                if consultation_id is not None
                else None
            )
            self._connection.execute(
                "INSERT INTO project_goals VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    goal.goal_id,
                    goal.project_id,
                    goal.repository,
                    goal.baseline_commit,
                    goal.original_goal,
                    goal.confirmed_goal,
                    goal.confirmed_at,
                ),
            )
            if consultation_id is not None:
                self._connection.execute(
                    "INSERT INTO project_goal_consultations VALUES (?, ?, ?)",
                    (goal.goal_id, consultation_id, decision),
                )

    def project_repository(self, project_id: str) -> str:
        row = self._connection.execute(
            "SELECT git_common_dir FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown project {project_id}")
        return str(row["git_common_dir"])

    def load_goal(self, goal_id: str) -> ConfirmedGoal:
        row = self._connection.execute(
            """SELECT g.*, p.git_common_dir FROM project_goals AS g
               JOIN projects AS p USING (project_id) WHERE g.goal_id = ?""",
            (goal_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown confirmed goal {goal_id}")
        return ConfirmedGoal(**dict(row))

    def _goal_view(self, goal_id: str) -> dict[str, Any]:
        result = asdict(self.load_goal(goal_id))
        consultation = self._connection.execute(
            """SELECT consultation_id, adoption, source_snapshot_id
               FROM project_goal_consultations JOIN project_consultations USING (consultation_id)
               WHERE goal_id = ?""",
            (goal_id,),
        ).fetchone()
        if consultation is not None:
            result["consultation"] = dict(consultation)
        return result

    def show_goal(self, goal_id: str) -> dict[str, Any]:
        return {"kind": "confirmed_goal", **self._goal_view(goal_id)}

    def start_plan(
        self,
        plan_id: str,
        goal_id: str,
        request: PlanningRequest,
        source_snapshot_id: str,
        prompt_template_version: str,
        started_at: str,
    ) -> None:
        self._connection.execute(
            """INSERT INTO project_plans
               (plan_id, goal_id, planner_request, source_snapshot_id,
                prompt_template_version, started_at) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                plan_id,
                goal_id,
                planning_json(request),
                source_snapshot_id,
                prompt_template_version,
                started_at,
            ),
        )

    def finish_plan(
        self,
        plan_id: str,
        finished_at: str,
        *,
        document: ProjectPlan | None,
        error: dict[str, Any] | None,
        evidence: dict[str, Any],
    ) -> None:
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT finished_at FROM project_plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown project plan {plan_id}")
            project_plan_status(
                finished=row["finished_at"] is not None,
                document=document is not None,
                error=error is not None,
                settling=True,
            )
            self._connection.execute(
                """UPDATE project_plans SET finished_at = ?, evidence = ?,
                   document = ?, error = ? WHERE plan_id = ?""",
                (
                    finished_at,
                    planning_json(evidence),
                    planning_json(document) if document is not None else None,
                    planning_json(error) if error is not None else None,
                    plan_id,
                ),
            )

    def _plan_row(self, plan_id: str) -> sqlite3.Row:
        row = self._connection.execute(
            """SELECT p.*, r.source_plan_id FROM project_plans AS p
               LEFT JOIN project_plan_revisions AS r USING (plan_id)
               WHERE p.plan_id = ?""",
            (plan_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown project plan {plan_id}")
        return cast(sqlite3.Row, row)

    def revise_plan(
        self, new_id: str, source_id: str, document: ProjectPlan, created_at: str
    ) -> None:
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            source = self._plan_row(source_id)
            if source["document"] is None:
                raise ValueError("plan revision requires a successful source plan")
            self._connection.execute(
                """INSERT INTO project_plans
                   (plan_id, goal_id, planner_request, source_snapshot_id,
                    prompt_template_version, started_at, finished_at, evidence, document)
                   VALUES (?, ?, 'null', ?, '', ?, ?, 'null', ?)""",
                (
                    new_id,
                    source["goal_id"],
                    source["source_snapshot_id"],
                    created_at,
                    created_at,
                    planning_json(document),
                ),
            )
            self._connection.execute(
                "INSERT INTO project_plan_revisions VALUES (?, ?)", (new_id, source_id)
            )

    def show_plan(self, plan_id: str) -> dict[str, Any]:
        row = self._plan_row(plan_id)
        document: ProjectPlan | None = (
            json.loads(row["document"]) if row["document"] is not None else None
        )
        result = {
            "kind": "project_plan",
            "plan_id": plan_id,
            "goal": self._goal_view(row["goal_id"]),
            "status": project_plan_status(
                finished=row["finished_at"] is not None,
                document=document is not None,
                error=row["error"] is not None,
            ),
            "planner_request": json.loads(row["planner_request"]),
            "source_snapshot_id": row["source_snapshot_id"],
            "prompt_template_version": row["prompt_template_version"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "evidence": json.loads(row["evidence"]) if row["evidence"] else None,
            "error": json.loads(row["error"]) if row["error"] else None,
            "document": document,
            "markdown": render_markdown(document) if document is not None else None,
        }
        if row["source_plan_id"] is not None:
            result.update(
                origin="pm",
                source_plan_id=row["source_plan_id"],
                prompt_template_version=None,
            )
        return result

    def show_project(self, project_id: str) -> dict[str, Any]:
        with self._connection:
            self._connection.execute("BEGIN")
            git_common_dir = self.project_repository(project_id)
            goals = [
                self._goal_view(row["goal_id"])
                for row in self._connection.execute(
                    """SELECT goal_id FROM project_goals WHERE project_id = ?
                       ORDER BY confirmed_at, goal_id""",
                    (project_id,),
                )
            ]
            plans = self._connection.execute(
                """SELECT p.plan_id, g.goal_id, p.started_at, p.finished_at,
                          r.source_plan_id,
                          p.document IS NOT NULL AS has_document, p.error IS NOT NULL AS has_error
                   FROM project_goals AS g JOIN project_plans AS p USING (goal_id)
                   LEFT JOIN project_plan_revisions AS r USING (plan_id)
                   WHERE g.project_id = ? ORDER BY p.started_at, p.plan_id""",
                (project_id,),
            )
            return {
                "kind": "project",
                "project_id": project_id,
                "git_common_dir": git_common_dir,
                "goals": goals,
                "plans": [
                    {
                        "plan_id": row["plan_id"],
                        "goal_id": row["goal_id"],
                        "started_at": row["started_at"],
                        "finished_at": row["finished_at"],
                        "status": project_plan_status(
                            finished=row["finished_at"] is not None,
                            document=bool(row["has_document"]),
                            error=bool(row["has_error"]),
                        ),
                        **(
                            {"origin": "pm", "source_plan_id": row["source_plan_id"]}
                            if row["source_plan_id"] is not None
                            else {}
                        ),
                    }
                    for row in plans
                ],
            }

    def reserve_stage(self, stage: StageRun, planning: PlanningInput) -> None:
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            self._insert_planning(stage.planning_run_id, planning, stage.created_at)
            if stage.mode == "delegate":
                self._connection.execute(
                    "INSERT INTO project_planning_sources VALUES (?, 'split', NULL)",
                    (stage.planning_run_id,),
                )
            self._connection.execute(
                """INSERT INTO project_stage_runs (
                       stage_run_id, plan_id, stage_id, planning_run_id, mode,
                       execution_baseline, dependencies, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    stage.stage_run_id,
                    stage.plan_id,
                    stage.stage_id,
                    stage.planning_run_id,
                    stage.mode,
                    stage.execution_baseline,
                    planning_json(stage.dependencies),
                    stage.created_at,
                ),
            )

    def load_stage(self, stage_run_id: str) -> StageRun:
        row = self._connection.execute(
            "SELECT * FROM project_stage_runs WHERE stage_run_id = ?", (stage_run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown project stage run {stage_run_id}")
        values = dict(row)
        values["dependencies"] = tuple(json.loads(values["dependencies"]))
        return StageRun(**values)

    def find_stage(self, plan_id: str, stage_id: str) -> StageRun | None:
        row = self._connection.execute(
            "SELECT stage_run_id FROM project_stage_runs WHERE plan_id = ? AND stage_id = ?",
            (plan_id, stage_id),
        ).fetchone()
        return None if row is None else self.load_stage(row["stage_run_id"])

    def stage_for_planning(self, planning_run_id: str) -> StageRun | None:
        row = self._connection.execute(
            "SELECT stage_run_id FROM project_stage_runs WHERE planning_run_id = ?",
            (planning_run_id,),
        ).fetchone()
        return None if row is None else self.load_stage(row["stage_run_id"])

    def list_stages(self, plan_id: str) -> list[StageRun]:
        self.show_plan(plan_id)
        return [
            self.load_stage(row["stage_run_id"])
            for row in self._connection.execute(
                "SELECT stage_run_id FROM project_stage_runs WHERE plan_id = ? ORDER BY created_at, stage_run_id",
                (plan_id,),
            )
        ]

    def block_stage_dispatch(self, stage_run_id: str, reason: str) -> None:
        self._connection.execute(
            "UPDATE project_stage_runs SET dispatch_error = ? WHERE stage_run_id = ? AND dispatch_error IS NULL",
            (reason, stage_run_id),
        )

    def reserve_leaf(
        self,
        planning_run_id: str,
        position: ProjectedDraft,
        card: TaskCard,
        value: PlanningInput,
        created_at: str,
        *,
        replaces_run_id: str | None = None,
    ) -> None:
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            head = self._connection.execute(
                """SELECT revision, planning_run_id FROM project_stage_leaves
                   WHERE stage_run_id = ? AND card_id = ? ORDER BY revision DESC LIMIT 1""",
                (position.stage_run_id, card.id),
            ).fetchone()
            if (
                head["planning_run_id"] if head is not None else None
            ) != replaces_run_id:
                raise ValueError("the current delegated leaf changed")
            if replaces_run_id is not None:
                old = self._read_planning(replaces_run_id)
                if old.bound_task_run_id is not None or any(
                    not item.stopped for item in old.review_attempts
                ):
                    raise ValueError("only a stopped, unbound leaf can be frozen again")
            split = self._read_planning(position.split_run_id)
            attempt = split.current("draft")
            if (
                not split.split
                or split.phase != PlanningPhase.READY
                or attempt is None
                or attempt.number != position.split_attempt
                or not attempt.usable
                or card.task_hash != position.card_hash
            ):
                raise ValueError("the delegated split or projected card changed")
            self.require_current_projection_dependencies(position)
            self._insert_planning(planning_run_id, value, created_at)
            self._connection.execute(
                "INSERT INTO project_planning_sources VALUES (?, 'projected', ?)",
                (planning_run_id, planning_json(position)),
            )
            self._connection.execute(
                "UPDATE planning_runs SET card_payload = ?, phase = ? WHERE run_id = ?",
                (planning_json(card), PlanningPhase.AWAITING_REVIEW, planning_run_id),
            )
            self._connection.execute(
                "INSERT INTO project_stage_leaves VALUES (?, ?, ?, ?)",
                (
                    position.stage_run_id,
                    card.id,
                    head["revision"] + 1 if head else 1,
                    planning_run_id,
                ),
            )

    def leaf_runs(self, stage_run_id: str) -> dict[str, str]:
        return {
            row["card_id"]: row["planning_run_id"]
            for row in self._connection.execute(
                "SELECT card_id, planning_run_id FROM project_stage_leaves WHERE stage_run_id = ? ORDER BY revision",
                (stage_run_id,),
            )
        }

    def leaf_history(self, stage_run_id: str, card_id: str) -> tuple[str, ...]:
        return tuple(
            row["planning_run_id"]
            for row in self._connection.execute(
                """SELECT planning_run_id FROM project_stage_leaves
               WHERE stage_run_id = ? AND card_id = ? ORDER BY revision""",
                (stage_run_id, card_id),
            )
        )
