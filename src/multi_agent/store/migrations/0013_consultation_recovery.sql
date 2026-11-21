ALTER TABLE project_consultations ADD COLUMN owner_device INTEGER;
ALTER TABLE project_consultations ADD COLUMN owner_inode INTEGER
    CHECK ((owner_device IS NULL) = (owner_inode IS NULL));
ALTER TABLE project_consultations ADD COLUMN interrupted_at TEXT
    CHECK (interrupted_at IS NULL OR owner_inode IS NOT NULL);

CREATE TABLE project_consultation_late_results (
    sequence INTEGER PRIMARY KEY,
    consultation_id TEXT NOT NULL,
    number INTEGER NOT NULL,
    finished_at TEXT NOT NULL,
    advice TEXT,
    error TEXT,
    evidence TEXT NOT NULL,
    FOREIGN KEY (consultation_id, number) REFERENCES project_consultation_seats(consultation_id, number),
    CHECK ((advice IS NULL) <> (error IS NULL))
);
CREATE INDEX project_consultation_late_results_order ON project_consultation_late_results(consultation_id, sequence);
