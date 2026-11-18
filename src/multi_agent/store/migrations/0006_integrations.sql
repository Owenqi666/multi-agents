CREATE TABLE integrations (
    integration_id TEXT PRIMARY KEY NOT NULL,
    repository TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    target_baseline TEXT NOT NULL,
    members_payload TEXT NOT NULL,
    applied_count INTEGER NOT NULL CHECK (applied_count >= 0),
    candidate_ref TEXT UNIQUE,
    candidate_commit TEXT,
    candidate_tree TEXT,
    conflict_paths TEXT,
    CHECK (
        (candidate_ref IS NOT NULL AND candidate_commit IS NOT NULL
            AND candidate_tree IS NOT NULL AND conflict_paths IS NULL)
        OR
        (candidate_ref IS NULL AND candidate_commit IS NULL
            AND candidate_tree IS NULL AND conflict_paths IS NOT NULL)
    )
);
