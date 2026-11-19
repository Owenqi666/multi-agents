CREATE TABLE projects (
    project_id TEXT PRIMARY KEY,
    git_common_dir TEXT NOT NULL
);

CREATE TABLE project_goals (
    goal_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    repository TEXT NOT NULL,
    baseline_commit TEXT NOT NULL,
    original_goal TEXT NOT NULL,
    confirmed_goal TEXT NOT NULL,
    confirmed_at TEXT NOT NULL
);
CREATE INDEX project_goals_project ON project_goals(project_id, confirmed_at, goal_id);

CREATE TABLE project_plans (
    plan_id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL REFERENCES project_goals(goal_id),
    planner_request TEXT NOT NULL,
    source_snapshot_id TEXT NOT NULL,
    prompt_template_version TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    evidence TEXT,
    document TEXT,
    error TEXT,
    CHECK (
        (finished_at IS NULL AND evidence IS NULL AND document IS NULL AND error IS NULL)
        OR (finished_at IS NOT NULL AND evidence IS NOT NULL
            AND ((document IS NULL) <> (error IS NULL)))
    )
);
CREATE INDEX project_plans_goal ON project_plans(goal_id, started_at, plan_id);
