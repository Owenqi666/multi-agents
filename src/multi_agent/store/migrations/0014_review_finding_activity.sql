CREATE TABLE review_finding_activity (
    id INTEGER PRIMARY KEY,
    round_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
    seat TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    finding_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('decision', 'answer', 'linked_plan')),
    payload TEXT NOT NULL,
    planning_run_id TEXT UNIQUE REFERENCES planning_runs(run_id),
    CHECK ((kind = 'linked_plan') = (planning_run_id IS NOT NULL)),
    FOREIGN KEY (round_id, seat, attempt) REFERENCES attempts(round_id, seat_key, number)
);
CREATE INDEX review_finding_activity_round ON review_finding_activity(round_id, id);
