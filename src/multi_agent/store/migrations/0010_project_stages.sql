CREATE TABLE project_stage_runs (
    stage_run_id TEXT PRIMARY KEY NOT NULL,
    plan_id TEXT NOT NULL REFERENCES project_plans(plan_id),
    stage_id TEXT NOT NULL,
    planning_run_id TEXT UNIQUE NOT NULL REFERENCES planning_runs(run_id),
    mode TEXT NOT NULL CHECK (mode = 'direct'),
    execution_baseline TEXT NOT NULL,
    dependencies TEXT NOT NULL,
    created_at TEXT NOT NULL,
    dispatch_error TEXT,
    UNIQUE (plan_id, stage_id)
);
