CREATE TABLE task_runs (
    run_id TEXT PRIMARY KEY NOT NULL,
    task_id TEXT NOT NULL,
    task_hash TEXT NOT NULL,
    rules_hash TEXT NOT NULL,
    baseline_commit TEXT NOT NULL,
    input_payload TEXT NOT NULL,
    phase TEXT NOT NULL CHECK (phase IN (
        'ready', 'implementing', 'cancelling', 'awaiting_delivery',
        'failed', 'cancelled', 'planning_required'
    )),
    current_attempt INTEGER CHECK (current_attempt IS NULL OR current_attempt >= 1),
    scope_block TEXT,
    FOREIGN KEY (run_id, current_attempt) REFERENCES task_attempts(run_id, number)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE task_attempts (
    run_id TEXT NOT NULL REFERENCES task_runs(run_id),
    number INTEGER NOT NULL CHECK (number >= 1),
    payload TEXT NOT NULL,
    PRIMARY KEY (run_id, number)
);

CREATE TABLE task_history (
    run_id TEXT NOT NULL REFERENCES task_runs(run_id),
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    envelope TEXT NOT NULL,
    disposition TEXT NOT NULL CHECK (disposition IN ('applied', 'history_only')),
    PRIMARY KEY (run_id, sequence)
);
