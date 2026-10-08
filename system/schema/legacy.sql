PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT INTO schema_metadata(key, value)
VALUES ('schema_version', '0.7')
ON CONFLICT(key) DO UPDATE SET value = excluded.value;

CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    base_url TEXT,
    authority TEXT NOT NULL,
    access_mode TEXT NOT NULL,
    scope TEXT,
    priority INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(source_id),
    external_id TEXT,
    document_kind TEXT NOT NULL,
    title TEXT NOT NULL,
    canonical_url TEXT,
    source_path TEXT,
    published_at TEXT,
    source_updated_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    evidence_kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'current',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(source_id, external_id)
);

CREATE INDEX IF NOT EXISTS idx_documents_source
ON documents(source_id);

CREATE INDEX IF NOT EXISTS idx_documents_published
ON documents(published_at);

CREATE TABLE IF NOT EXISTS versions (
    version_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    content_sha256 TEXT NOT NULL,
    raw_path TEXT NOT NULL,
    media_type TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(document_id, content_sha256)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_versions_one_current
ON versions(document_id)
WHERE is_current = 1;

CREATE TABLE IF NOT EXISTS fragments (
    fragment_row_id TEXT PRIMARY KEY,
    fragment_id TEXT NOT NULL,
    version_id TEXT NOT NULL REFERENCES versions(version_id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    sequence_no INTEGER NOT NULL,
    fragment_kind TEXT NOT NULL,
    stable_anchor TEXT,
    heading TEXT,
    author_role TEXT,
    evidence_kind TEXT NOT NULL,
    content TEXT NOT NULL,
    source_line_start INTEGER,
    source_line_end INTEGER,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(version_id, fragment_id),
    UNIQUE(version_id, sequence_no)
);

CREATE INDEX IF NOT EXISTS idx_fragments_document
ON fragments(document_id, sequence_no);

CREATE INDEX IF NOT EXISTS idx_fragments_stable_id
ON fragments(fragment_id);

CREATE TABLE IF NOT EXISTS relations (
    relation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    to_document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL,
    confidence REAL,
    notes TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(from_document_id, to_document_id, relation_type)
);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id TEXT PRIMARY KEY,
    source_id TEXT REFERENCES sources(source_id),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    documents_seen INTEGER NOT NULL DEFAULT 0,
    versions_added INTEGER NOT NULL DEFAULT 0,
    fragments_added INTEGER NOT NULL DEFAULT 0,
    error_text TEXT
);

CREATE TABLE IF NOT EXISTS source_endpoints (
    endpoint_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(source_id),
    name TEXT NOT NULL,
    endpoint_kind TEXT NOT NULL,
    url TEXT NOT NULL,
    access_mode TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    expected_markers_json TEXT NOT NULL DEFAULT '[]',
    last_checked_at TEXT,
    last_success_at TEXT,
    last_status TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_id, url)
);

CREATE INDEX IF NOT EXISTS idx_source_endpoints_source
ON source_endpoints(source_id, priority DESC);

-- Topics classify documents independently from publishers and time-bound
-- events.  A document can belong to multiple topics, and parent topics are
-- optional navigation groups rather than exclusive folders.

CREATE TABLE IF NOT EXISTS topics (
    topic_id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    parent_topic_id TEXT REFERENCES topics(topic_id) ON DELETE SET NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    priority INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_topics_parent
ON topics(parent_topic_id, priority DESC, topic_id);

CREATE TABLE IF NOT EXISTS topic_aliases (
    topic_id TEXT NOT NULL REFERENCES topics(topic_id) ON DELETE CASCADE,
    alias TEXT NOT NULL COLLATE NOCASE,
    alias_kind TEXT NOT NULL DEFAULT 'synonym',
    managed_by TEXT NOT NULL DEFAULT 'config',
    created_at TEXT NOT NULL,
    PRIMARY KEY(topic_id, alias)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_topic_aliases_alias
ON topic_aliases(alias COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS document_topics (
    document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    topic_id TEXT NOT NULL REFERENCES topics(topic_id) ON DELETE CASCADE,
    topic_role TEXT NOT NULL DEFAULT 'related',
    assignment_method TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0 CHECK (confidence >= 0.0 AND confidence <= 1.0),
    inherited_from_document_id TEXT REFERENCES documents(document_id) ON DELETE SET NULL,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(document_id, topic_id)
);

CREATE INDEX IF NOT EXISTS idx_document_topics_topic
ON document_topics(topic_id, document_id);

CREATE INDEX IF NOT EXISTS idx_document_topics_document
ON document_topics(document_id, topic_id);

CREATE TABLE IF NOT EXISTS topic_sources (
    topic_source_id TEXT PRIMARY KEY,
    topic_id TEXT NOT NULL REFERENCES topics(topic_id) ON DELETE CASCADE,
    source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
    endpoint_id TEXT REFERENCES source_endpoints(endpoint_id) ON DELETE CASCADE,
    coverage_role TEXT NOT NULL DEFAULT 'relevant',
    priority INTEGER NOT NULL DEFAULT 0,
    required INTEGER NOT NULL DEFAULT 0 CHECK (required IN (0, 1)),
    managed_by TEXT NOT NULL DEFAULT 'config',
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_topic_sources_topic
ON topic_sources(topic_id, priority DESC, source_id, endpoint_id);

CREATE INDEX IF NOT EXISTS idx_topic_sources_source
ON topic_sources(source_id, topic_id);

CREATE TABLE IF NOT EXISTS collection_runs (
    run_id TEXT PRIMARY KEY,
    purpose TEXT NOT NULL,
    mode TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    scope_json TEXT NOT NULL DEFAULT '{}',
    summary_json TEXT NOT NULL DEFAULT '{}',
    error_text TEXT
);

CREATE TABLE IF NOT EXISTS fetch_attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES collection_runs(run_id) ON DELETE CASCADE,
    endpoint_id TEXT REFERENCES source_endpoints(endpoint_id),
    document_id TEXT REFERENCES documents(document_id),
    requested_url TEXT NOT NULL,
    final_url TEXT,
    access_path TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    http_status INTEGER,
    content_type TEXT,
    byte_size INTEGER,
    content_sha256 TEXT,
    error_text TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_fetch_attempts_run
ON fetch_attempts(run_id, attempt_id);

CREATE INDEX IF NOT EXISTS idx_fetch_attempts_endpoint
ON fetch_attempts(endpoint_id, started_at DESC);

-- A cheap, domain-neutral inbox for list items that have been observed but do
-- not yet justify full archival, parsing, or a specialized business module.
CREATE TABLE IF NOT EXISTS discovered_items (
    item_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(source_id),
    endpoint_id TEXT REFERENCES source_endpoints(endpoint_id),
    external_id TEXT,
    title TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    published_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    item_status TEXT NOT NULL DEFAULT 'active',
    review_status TEXT NOT NULL DEFAULT 'unreviewed',
    relevance TEXT NOT NULL DEFAULT 'unknown',
    topic_hints_json TEXT NOT NULL DEFAULT '[]',
    promoted_document_id TEXT REFERENCES documents(document_id) ON DELETE SET NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_id, canonical_url)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_discovered_items_external
ON discovered_items(source_id, external_id)
WHERE external_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_discovered_items_queue
ON discovered_items(review_status, relevance, published_at DESC, last_seen_at DESC);

CREATE INDEX IF NOT EXISTS idx_discovered_items_source
ON discovered_items(source_id, endpoint_id, last_seen_at DESC);

CREATE INDEX IF NOT EXISTS idx_discovered_items_promoted
ON discovered_items(promoted_document_id)
WHERE promoted_document_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    event_kind TEXT NOT NULL,
    academic_year TEXT,
    status TEXT NOT NULL DEFAULT 'current',
    priority INTEGER NOT NULL DEFAULT 0,
    starts_at TEXT,
    ends_at TEXT,
    evidence_kind TEXT NOT NULL DEFAULT 'derived_summary',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_documents (
    event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    document_role TEXT NOT NULL,
    sequence_no INTEGER,
    notes TEXT,
    PRIMARY KEY(event_id, document_id, document_role)
);

CREATE INDEX IF NOT EXISTS idx_event_documents_document
ON event_documents(document_id, event_id);

CREATE TABLE IF NOT EXISTS deadlines (
    deadline_id TEXT PRIMARY KEY,
    event_id TEXT REFERENCES events(event_id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    fragment_row_id TEXT REFERENCES fragments(fragment_row_id) ON DELETE SET NULL,
    deadline_kind TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    deadline_at TEXT,
    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    precision TEXT NOT NULL,
    applies_to TEXT,
    action TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    confidence REAL NOT NULL DEFAULT 1.0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_deadlines_event
ON deadlines(event_id, deadline_at);

CREATE INDEX IF NOT EXISTS idx_deadlines_status
ON deadlines(status, deadline_at);

CREATE VIRTUAL TABLE IF NOT EXISTS fragments_fts USING fts5(
    fragment_row_id UNINDEXED,
    fragment_id UNINDEXED,
    version_id UNINDEXED,
    document_id UNINDEXED,
    title,
    heading,
    content,
    tokenize = 'trigram'
);

-- Career research is deliberately stored in dedicated structured tables.
-- It does not enter documents/fragments/fragments_fts by default, so large
-- recruitment archives cannot dilute school-policy and push-recommendation
-- retrieval.  Raw evidence remains immutable on disk and is referenced here.

CREATE TABLE IF NOT EXISTS career_platforms (
    platform_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    platform_kind TEXT NOT NULL,
    base_url TEXT,
    authority TEXT NOT NULL,
    access_mode TEXT NOT NULL,
    scope TEXT,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS career_positions (
    position_id TEXT PRIMARY KEY,
    platform_id TEXT NOT NULL REFERENCES career_platforms(platform_id),
    external_job_id TEXT,
    legacy_detail_id TEXT UNIQUE,
    organization TEXT NOT NULL,
    title TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    location_text TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    lifecycle_status TEXT NOT NULL DEFAULT 'observed',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(platform_id, canonical_url)
);

CREATE INDEX IF NOT EXISTS idx_career_positions_platform
ON career_positions(platform_id, organization, title);

CREATE TABLE IF NOT EXISTS career_position_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    position_id TEXT NOT NULL REFERENCES career_positions(position_id) ON DELETE CASCADE,
    content_sha256 TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    published_or_updated_raw TEXT,
    application_start_raw TEXT,
    application_deadline_raw TEXT,
    application_status_raw TEXT,
    employment_type_raw TEXT,
    recruit_count_raw TEXT,
    salary_raw TEXT,
    education_requirement_raw TEXT,
    major_requirement_raw TEXT,
    graduation_requirement_raw TEXT,
    job_description_text TEXT,
    qualification_text TEXT,
    evidence_path TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL,
    is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(position_id, content_sha256)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_career_snapshots_one_current
ON career_position_snapshots(position_id)
WHERE is_current = 1;

CREATE INDEX IF NOT EXISTS idx_career_snapshots_position
ON career_position_snapshots(position_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS career_discoveries (
    discovery_id TEXT PRIMARY KEY,
    platform_id TEXT NOT NULL REFERENCES career_platforms(platform_id),
    position_id TEXT REFERENCES career_positions(position_id) ON DELETE SET NULL,
    observed_at TEXT NOT NULL,
    query_text TEXT,
    batch_label TEXT,
    organization_raw TEXT,
    title_raw TEXT,
    location_raw TEXT,
    salary_raw TEXT,
    education_raw TEXT,
    major_raw TEXT,
    cohort_raw TEXT,
    recruitment_type_raw TEXT,
    organization_type_raw TEXT,
    platform_source_raw TEXT,
    listing_url TEXT,
    detail_url TEXT,
    evidence_path TEXT NOT NULL,
    visible_summary TEXT,
    notes TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_career_discoveries_platform_query
ON career_discoveries(platform_id, query_text, observed_at);

CREATE INDEX IF NOT EXISTS idx_career_discoveries_position
ON career_discoveries(position_id);

CREATE TABLE IF NOT EXISTS career_assessments (
    assessment_id TEXT PRIMARY KEY,
    position_id TEXT NOT NULL REFERENCES career_positions(position_id) ON DELETE CASCADE,
    snapshot_id TEXT REFERENCES career_position_snapshots(snapshot_id) ON DELETE SET NULL,
    assessment_kind TEXT NOT NULL,
    outcome TEXT NOT NULL,
    rationale TEXT,
    profile_context TEXT,
    assessed_at TEXT NOT NULL,
    evidence_kind TEXT NOT NULL DEFAULT 'derived_analysis',
    status TEXT NOT NULL DEFAULT 'current',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_career_assessments_one_current
ON career_assessments(position_id, assessment_kind)
WHERE status = 'current';

CREATE INDEX IF NOT EXISTS idx_career_assessments_outcome
ON career_assessments(assessment_kind, outcome);

CREATE TABLE IF NOT EXISTS career_import_runs (
    run_id TEXT PRIMARY KEY,
    source_root TEXT NOT NULL,
    manifest_path TEXT,
    manifest_sha256 TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    discoveries_seen INTEGER NOT NULL DEFAULT 0,
    positions_seen INTEGER NOT NULL DEFAULT 0,
    snapshots_seen INTEGER NOT NULL DEFAULT 0,
    assessments_seen INTEGER NOT NULL DEFAULT 0,
    summary_json TEXT NOT NULL DEFAULT '{}',
    error_text TEXT
);

-- The external push-admission pool is intentionally short-lived and small.
-- It reuses the general evidence store for archived official pages, but keeps
-- opportunity state, negative checks, and user-specific judgments out of the
-- school policy/event tables and out of the career module.

CREATE TABLE IF NOT EXISTS admission_campaigns (
    campaign_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    academic_year TEXT NOT NULL,
    purpose TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'archived')),
    starts_at TEXT,
    expires_at TEXT,
    scope_json TEXT NOT NULL DEFAULT '{}',
    strategy_json TEXT NOT NULL DEFAULT '{}',
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admission_context_links (
    context_link_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL
        REFERENCES admission_campaigns(campaign_id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    fragment_id TEXT NOT NULL,
    context_role TEXT NOT NULL,
    expected_author_role TEXT,
    priority INTEGER NOT NULL DEFAULT 0,
    summary TEXT NOT NULL,
    managed_by TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(campaign_id, document_id, fragment_id, context_role)
);

CREATE INDEX IF NOT EXISTS idx_admission_context_campaign
ON admission_context_links(campaign_id, priority DESC, fragment_id);

CREATE TABLE IF NOT EXISTS admission_targets (
    target_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL
        REFERENCES admission_campaigns(campaign_id) ON DELETE CASCADE,
    institution TEXT NOT NULL,
    college TEXT,
    program_code TEXT,
    program_name TEXT,
    direction_name TEXT,
    batch_name TEXT,
    discovery_channel TEXT NOT NULL DEFAULT 'manual',
    lead_url TEXT,
    official_url TEXT,
    application_url TEXT,
    announcement_title TEXT,
    published_at TEXT,
    deadline_raw TEXT,
    deadline_at TEXT,
    assessment_time_raw TEXT,
    assessment_mode_raw TEXT,
    window_status TEXT NOT NULL DEFAULT 'unknown'
        CHECK (window_status IN ('unknown', 'not_started', 'open', 'closed')),
    verification_status TEXT NOT NULL DEFAULT 'unverified'
        CHECK (verification_status IN (
            'unverified', 'official_verified', 'not_found',
            'access_failed', 'inconclusive'
        )),
    eligibility_status TEXT NOT NULL DEFAULT 'unassessed'
        CHECK (eligibility_status IN (
            'unassessed', 'eligible', 'unclear', 'ineligible'
        )),
    interest_status TEXT NOT NULL DEFAULT 'unassessed'
        CHECK (interest_status IN (
            'unassessed', 'would_consider', 'maybe', 'would_not'
        )),
    application_status TEXT NOT NULL DEFAULT 'not_started'
        CHECK (application_status IN (
            'not_started', 'preparing', 'submitted', 'screening',
            'interview', 'offered', 'rejected', 'withdrawn', 'not_applying'
        )),
    expected_push_qualification_raw TEXT,
    undergraduate_major_raw TEXT,
    ranking_requirement_raw TEXT,
    english_requirement_raw TEXT,
    other_requirement_raw TEXT,
    eligibility_rationale TEXT,
    interest_notes TEXT,
    last_checked_at TEXT,
    next_check_at TEXT,
    priority INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_admission_targets_identity
ON admission_targets(
    campaign_id,
    institution,
    COALESCE(college, ''),
    COALESCE(program_code, ''),
    COALESCE(program_name, ''),
    COALESCE(direction_name, ''),
    COALESCE(batch_name, '')
);

CREATE INDEX IF NOT EXISTS idx_admission_targets_queue
ON admission_targets(
    campaign_id, window_status, verification_status, deadline_at, priority DESC
);

CREATE TABLE IF NOT EXISTS admission_checks (
    check_id TEXT PRIMARY KEY,
    target_id TEXT NOT NULL
        REFERENCES admission_targets(target_id) ON DELETE CASCADE,
    checked_at TEXT NOT NULL,
    result TEXT NOT NULL CHECK (result IN (
        'found_open', 'found_closed', 'not_found',
        'access_failed', 'inconclusive'
    )),
    query_text TEXT,
    checked_paths_json TEXT NOT NULL DEFAULT '[]',
    stop_reason TEXT,
    notes TEXT,
    collection_run_id TEXT REFERENCES collection_runs(run_id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_admission_checks_target
ON admission_checks(target_id, checked_at DESC);

CREATE TABLE IF NOT EXISTS admission_evidence_links (
    evidence_link_id TEXT PRIMARY KEY,
    target_id TEXT NOT NULL
        REFERENCES admission_targets(target_id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    fragment_id TEXT,
    evidence_role TEXT NOT NULL,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_admission_evidence_identity
ON admission_evidence_links(
    target_id, document_id, COALESCE(fragment_id, ''), evidence_role
);

DROP VIEW IF EXISTS admission_current_pool;
CREATE VIEW admission_current_pool AS
SELECT
    t.target_id,
    t.campaign_id,
    c.name AS campaign_name,
    t.institution,
    t.college,
    t.program_code,
    t.program_name,
    t.direction_name,
    t.batch_name,
    t.discovery_channel,
    t.window_status,
    t.verification_status,
    t.eligibility_status,
    t.interest_status,
    t.application_status,
    t.deadline_raw,
    t.deadline_at,
    t.official_url,
    t.application_url,
    t.last_checked_at,
    t.next_check_at,
    t.priority,
    t.updated_at
FROM admission_targets t
JOIN admission_campaigns c USING(campaign_id)
WHERE c.status = 'active';
