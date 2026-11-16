CREATE TABLE planning_runs (
    run_id TEXT PRIMARY KEY NOT NULL,
    input_hash TEXT NOT NULL,
    input_payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    phase TEXT NOT NULL CHECK (phase IN (
        'pending', 'drafting', 'reviewing', 'awaiting_validation',
        'awaiting_review', 'revision_required', 'reconciliation_required',
        'failed', 'cancelling', 'cancelled', 'ready', 'bound'
    )),
    stage TEXT CHECK (stage IN ('draft', 'review')),
    card_payload TEXT,
    review_artifact_payload TEXT,
    validation_error TEXT,
    bound_task_run_id TEXT REFERENCES task_runs(run_id)
);

CREATE TABLE planning_attempts (
    run_id TEXT NOT NULL REFERENCES planning_runs(run_id),
    stage TEXT NOT NULL CHECK (stage IN ('draft', 'review')),
    number INTEGER NOT NULL CHECK (number >= 1),
    payload TEXT NOT NULL,
    PRIMARY KEY (run_id, stage, number)
);

CREATE TABLE planning_history (
    run_id TEXT NOT NULL REFERENCES planning_runs(run_id),
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    envelope TEXT NOT NULL,
    disposition TEXT NOT NULL CHECK (disposition IN ('applied', 'history_only')),
    PRIMARY KEY (run_id, sequence)
);

CREATE TABLE task_planning_provenance (
    task_run_id TEXT PRIMARY KEY NOT NULL REFERENCES task_runs(run_id),
    planning_run_id TEXT UNIQUE NOT NULL REFERENCES planning_runs(run_id),
    plan_hash TEXT NOT NULL,
    payload TEXT NOT NULL
);
