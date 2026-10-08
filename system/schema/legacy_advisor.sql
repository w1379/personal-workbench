-- Additive, rebuildable research map; application state stays in admission_targets.
CREATE TABLE IF NOT EXISTS research_advisors (
 advisor_id TEXT PRIMARY KEY, name TEXT NOT NULL, institution TEXT NOT NULL,
 college TEXT NOT NULL, interest_lines_json TEXT NOT NULL,
 research_fact TEXT NOT NULL, training_assessment TEXT NOT NULL,
 supervision_fact TEXT NOT NULL, action_tier TEXT NOT NULL,
 gaps_json TEXT NOT NULL, source_urls_json TEXT NOT NULL, checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_routes (
 route_id TEXT PRIMARY KEY, admission_target_id TEXT REFERENCES admission_targets(target_id),
 entry_year INTEGER NOT NULL, institution TEXT NOT NULL, college TEXT NOT NULL,
 program_code TEXT, program_name TEXT NOT NULL, degree_type TEXT NOT NULL,
 channel TEXT NOT NULL, program_evidence_scope TEXT NOT NULL,
 window_status TEXT NOT NULL, deadline_raw TEXT NOT NULL,
 application_url TEXT, selection_rule TEXT NOT NULL, eligibility_assessment TEXT NOT NULL,
 gaps_json TEXT NOT NULL, source_urls_json TEXT NOT NULL, checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_advisor_routes (
 advisor_id TEXT NOT NULL REFERENCES research_advisors(advisor_id),
 route_id TEXT NOT NULL REFERENCES research_routes(route_id),
 mapping_status TEXT NOT NULL, mapping_basis TEXT NOT NULL,
 PRIMARY KEY(advisor_id, route_id)
);
CREATE TABLE IF NOT EXISTS research_evidence (
 url TEXT PRIMARY KEY, capture_kind TEXT NOT NULL,
 raw_path TEXT NOT NULL, sha256 TEXT NOT NULL, captured_at TEXT NOT NULL,
 coverage_note TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS research_advisors_institution ON research_advisors(institution);
CREATE INDEX IF NOT EXISTS research_routes_target ON research_routes(admission_target_id);
