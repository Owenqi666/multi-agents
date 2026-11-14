CREATE TABLE rounds (
    round_id TEXT PRIMARY KEY NOT NULL,
    snapshot_id TEXT NOT NULL,
    prompt_template_version TEXT NOT NULL
);

CREATE TABLE seats (
    round_id TEXT NOT NULL REFERENCES rounds(round_id),
    seat_key TEXT NOT NULL,
    position INTEGER NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    effort TEXT NOT NULL,
    PRIMARY KEY (round_id, seat_key),
    UNIQUE (round_id, position)
);

CREATE TABLE attempts (
    round_id TEXT NOT NULL,
    seat_key TEXT NOT NULL,
    number INTEGER NOT NULL CHECK (number >= 1),
    status TEXT NOT NULL CHECK (status IN ('running', 'cancelling', 'succeeded', 'failed')),
    failure_category TEXT CHECK (failure_category IN (
        'auth_expired', 'rate_limited', 'model_unavailable', 'timeout',
        'cancelled', 'out_of_scope_patch', 'tests_failed', 'integration_conflict'
    )),
    detail TEXT,
    retry_after TEXT,
    session_id TEXT,
    reported_model TEXT,
    reported_model_source TEXT,
    reported_effort TEXT,
    reported_effort_source TEXT,
    result TEXT NOT NULL,
    exit_code INTEGER,
    PRIMARY KEY (round_id, seat_key, number),
    FOREIGN KEY (round_id, seat_key) REFERENCES seats(round_id, seat_key)
);

CREATE TABLE history (
    round_id TEXT NOT NULL REFERENCES rounds(round_id),
    sequence INTEGER NOT NULL,
    envelope TEXT NOT NULL,
    disposition TEXT NOT NULL CHECK (disposition IN ('applied', 'history_only')),
    PRIMARY KEY (round_id, sequence)
);
