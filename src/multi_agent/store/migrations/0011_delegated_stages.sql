CREATE TABLE project_planning_sources (
    planning_run_id TEXT PRIMARY KEY NOT NULL REFERENCES planning_runs(run_id),
    kind TEXT NOT NULL CHECK (kind IN ('split', 'projected')),
    projection TEXT,
    CHECK ((kind = 'projected') = (projection IS NOT NULL))
);

ALTER TABLE project_stage_runs RENAME TO project_stage_runs_v10;
CREATE TABLE project_stage_runs (
    stage_run_id TEXT PRIMARY KEY NOT NULL,
    plan_id TEXT NOT NULL REFERENCES project_plans(plan_id),
    stage_id TEXT NOT NULL,
    planning_run_id TEXT UNIQUE NOT NULL REFERENCES planning_runs(run_id),
    mode TEXT NOT NULL CHECK (mode IN ('direct', 'delegate')),
    execution_baseline TEXT NOT NULL,
    dependencies TEXT NOT NULL,
    created_at TEXT NOT NULL,
    dispatch_error TEXT,
    UNIQUE (plan_id, stage_id)
);
INSERT INTO project_stage_runs SELECT * FROM project_stage_runs_v10;
DROP TABLE project_stage_runs_v10;

CREATE TABLE project_stage_leaves (
    stage_run_id TEXT NOT NULL REFERENCES project_stage_runs(stage_run_id),
    card_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    planning_run_id TEXT NOT NULL UNIQUE REFERENCES planning_runs(run_id),
    PRIMARY KEY (stage_run_id, card_id, revision)
);
