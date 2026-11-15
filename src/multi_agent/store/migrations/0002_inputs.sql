CREATE TABLE snapshots (
    snapshot_id TEXT PRIMARY KEY NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE round_inputs (
    round_id TEXT PRIMARY KEY NOT NULL REFERENCES rounds(round_id),
    snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
    metadata TEXT NOT NULL,
    parent_round_id TEXT UNIQUE REFERENCES round_inputs(round_id),
    CHECK (parent_round_id IS NULL OR parent_round_id <> round_id)
);

CREATE TABLE executions (
    round_id TEXT NOT NULL,
    seat_key TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    metadata TEXT NOT NULL,
    PRIMARY KEY (round_id, seat_key, attempt),
    FOREIGN KEY (round_id, seat_key, attempt)
        REFERENCES attempts(round_id, seat_key, number)
);
