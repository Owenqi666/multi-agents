ALTER TABLE integration_review_links
    ADD COLUMN auto_rework INTEGER NOT NULL DEFAULT 0 CHECK (auto_rework IN (0, 1));

CREATE TABLE integration_reworks (
    rework_id TEXT PRIMARY KEY NOT NULL,
    lineage_id TEXT NOT NULL UNIQUE REFERENCES integrations(integration_id),
    source_id TEXT NOT NULL REFERENCES integrations(integration_id),
    review_round_id TEXT NOT NULL REFERENCES integration_review_links(round_id),
    child_id TEXT UNIQUE REFERENCES integrations(integration_id),
    request_payload TEXT NOT NULL,
    result_payload TEXT,
    process_stopped INTEGER CHECK (process_stopped IN (0, 1)),
    late_results TEXT NOT NULL DEFAULT '[]',
    CHECK (child_id IS NULL OR (result_payload IS NOT NULL AND process_stopped IS 1)),
    CHECK (result_payload IS NOT NULL OR process_stopped IS NULL)
);
