CREATE TABLE task_test_stages (
    task_run_id TEXT NOT NULL,
    implementation_attempt INTEGER NOT NULL CHECK (implementation_attempt >= 1),
    reference_input_payload TEXT NOT NULL,
    current_test_attempt INTEGER NOT NULL CHECK (current_test_attempt >= 1),
    PRIMARY KEY (task_run_id, implementation_attempt),
    FOREIGN KEY (task_run_id, implementation_attempt)
        REFERENCES task_attempts(run_id, number),
    FOREIGN KEY (task_run_id, implementation_attempt, current_test_attempt)
        REFERENCES task_test_attempts(task_run_id, implementation_attempt, number)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE task_test_attempts (
    task_run_id TEXT NOT NULL,
    implementation_attempt INTEGER NOT NULL CHECK (implementation_attempt >= 1),
    number INTEGER NOT NULL CHECK (number >= 1),
    payload TEXT NOT NULL,
    PRIMARY KEY (task_run_id, implementation_attempt, number),
    FOREIGN KEY (task_run_id, implementation_attempt)
        REFERENCES task_test_stages(task_run_id, implementation_attempt)
);

CREATE TABLE task_test_history (
    task_run_id TEXT NOT NULL,
    implementation_attempt INTEGER NOT NULL CHECK (implementation_attempt >= 1),
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    envelope TEXT NOT NULL,
    disposition TEXT NOT NULL CHECK (disposition IN ('applied', 'history_only')),
    PRIMARY KEY (task_run_id, implementation_attempt, sequence),
    FOREIGN KEY (task_run_id, implementation_attempt)
        REFERENCES task_test_stages(task_run_id, implementation_attempt)
);
