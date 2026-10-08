-- Infrastructure observations stay separate from school/career search.
CREATE TABLE IF NOT EXISTS infrastructure_observations (
    observation_id TEXT PRIMARY KEY,
    droplet_id INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    name TEXT NOT NULL,
    summary TEXT NOT NULL,
    data_json TEXT NOT NULL,
    evidence_path TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL
);
