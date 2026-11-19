CREATE TABLE project_consultations (
    consultation_id TEXT PRIMARY KEY,
    repository TEXT NOT NULL,
    git_common_dir TEXT NOT NULL,
    baseline_commit TEXT NOT NULL,
    original_goal TEXT NOT NULL,
    project_id TEXT REFERENCES projects(project_id),
    source_snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
    started_at TEXT NOT NULL
);

CREATE TABLE project_consultation_seats (
    consultation_id TEXT NOT NULL REFERENCES project_consultations(consultation_id),
    number INTEGER NOT NULL CHECK (number IN (1, 2)),
    request TEXT NOT NULL,
    prompt_template_version TEXT,
    started_at TEXT,
    finished_at TEXT,
    advice TEXT,
    error TEXT,
    evidence TEXT,
    PRIMARY KEY (consultation_id, number),
    CHECK ((prompt_template_version IS NULL) = (started_at IS NULL)),
    CHECK (
        (finished_at IS NULL AND advice IS NULL AND error IS NULL AND evidence IS NULL)
        OR (finished_at IS NOT NULL AND started_at IS NOT NULL AND evidence IS NOT NULL
            AND ((advice IS NULL) <> (error IS NULL)))
    )
);

CREATE TABLE project_goal_consultations (
    goal_id TEXT PRIMARY KEY REFERENCES project_goals(goal_id),
    consultation_id TEXT NOT NULL REFERENCES project_consultations(consultation_id),
    adoption TEXT NOT NULL CHECK (adoption IN ('accepted', 'edited'))
);
CREATE INDEX project_goal_consultations_consultation ON project_goal_consultations(consultation_id);

CREATE TABLE project_plan_revisions (
    plan_id TEXT PRIMARY KEY REFERENCES project_plans(plan_id),
    source_plan_id TEXT NOT NULL REFERENCES project_plans(plan_id),
    CHECK (plan_id <> source_plan_id)
);
