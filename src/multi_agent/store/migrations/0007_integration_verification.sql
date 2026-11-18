CREATE TABLE integration_rebases (
    integration_id TEXT PRIMARY KEY NOT NULL REFERENCES integrations(integration_id),
    source_id TEXT NOT NULL REFERENCES integrations(integration_id),
    CHECK (integration_id <> source_id)
);

CREATE TABLE integration_test_attempts (
    integration_id TEXT NOT NULL REFERENCES integrations(integration_id),
    attempt INTEGER NOT NULL CHECK (attempt > 0),
    request_payload TEXT NOT NULL,
    result_payload TEXT,
    process_stopped INTEGER CHECK (process_stopped IN (0, 1)),
    late_results TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (integration_id, attempt),
    CHECK (result_payload IS NOT NULL OR process_stopped IS NULL)
);

CREATE TABLE integration_review_links (
    round_id TEXT PRIMARY KEY NOT NULL REFERENCES round_inputs(round_id),
    integration_id TEXT NOT NULL REFERENCES integrations(integration_id),
    test_attempt INTEGER NOT NULL,
    directory TEXT NOT NULL UNIQUE,
    directory_identity TEXT NOT NULL,
    FOREIGN KEY (integration_id, test_attempt)
        REFERENCES integration_test_attempts(integration_id, attempt)
);

CREATE TABLE integration_acceptances (
    integration_id TEXT PRIMARY KEY NOT NULL REFERENCES integrations(integration_id),
    acceptance_id TEXT NOT NULL UNIQUE,
    review_round_id TEXT NOT NULL REFERENCES integration_review_links(round_id),
    payload TEXT NOT NULL
);
