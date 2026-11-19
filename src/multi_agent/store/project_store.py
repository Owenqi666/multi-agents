"""Project plans share the workbench database and retain their original goal."""

import json
from dataclasses import asdict
from typing import Any

from multi_agent.core.planning import PlanningRequest, planning_json
from multi_agent.core.project_plan import ConfirmedGoal, ProjectPlan, render_markdown
from multi_agent.core.transitions import project_plan_status

from .database import ReviewStore


class ProjectStore(ReviewStore):
    def confirm_goal(self, goal: ConfirmedGoal, *, new_project: bool) -> None:
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            if new_project:
                self._connection.execute(
                    "INSERT INTO projects VALUES (?, ?)",
                    (goal.project_id, goal.git_common_dir),
                )
            elif self.project_repository(goal.project_id) != goal.git_common_dir:
                raise ValueError("the project belongs to a different Git repository")
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

    def show_plan(self, plan_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT * FROM project_plans WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown project plan {plan_id}")
        document: ProjectPlan | None = (
            json.loads(row["document"]) if row["document"] is not None else None
        )
        return {
            "kind": "project_plan",
            "plan_id": plan_id,
            "goal": asdict(self.load_goal(row["goal_id"])),
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

    def show_project(self, project_id: str) -> dict[str, Any]:
        with self._connection:
            self._connection.execute("BEGIN")
            git_common_dir = self.project_repository(project_id)
            goals = [
                asdict(self.load_goal(row["goal_id"]))
                for row in self._connection.execute(
                    """SELECT goal_id FROM project_goals WHERE project_id = ?
                       ORDER BY confirmed_at, goal_id""",
                    (project_id,),
                )
            ]
            plans = self._connection.execute(
                """SELECT p.plan_id, g.goal_id, p.started_at, p.finished_at,
                          p.document IS NOT NULL AS has_document, p.error IS NOT NULL AS has_error
                   FROM project_goals AS g JOIN project_plans AS p USING (goal_id)
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
                    }
                    for row in plans
                ],
            }
