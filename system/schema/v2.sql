CREATE TABLE IF NOT EXISTS items (
 item_id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
 description TEXT NOT NULL DEFAULT '', domain TEXT NOT NULL DEFAULT 'general',
 authority TEXT NOT NULL DEFAULT 'user', evidence_kind TEXT NOT NULL DEFAULT 'user_statement',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
 archived_at TEXT, metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS aliases (
 item_id TEXT NOT NULL REFERENCES items(item_id), alias TEXT NOT NULL COLLATE NOCASE,
 context TEXT NOT NULL DEFAULT '', PRIMARY KEY(item_id,alias,context)
);
CREATE INDEX IF NOT EXISTS aliases_name ON aliases(alias);
CREATE TABLE IF NOT EXISTS links (
 from_item TEXT NOT NULL REFERENCES items(item_id), to_item TEXT NOT NULL REFERENCES items(item_id),
 relation_type TEXT NOT NULL, source_ref TEXT NOT NULL DEFAULT '', PRIMARY KEY(from_item,to_item,relation_type)
);
CREATE TABLE IF NOT EXISTS locations (
 location_id TEXT PRIMARY KEY, item_id TEXT NOT NULL REFERENCES items(item_id), path TEXT NOT NULL,
 role TEXT NOT NULL DEFAULT 'primary', is_external INTEGER NOT NULL DEFAULT 0,
 is_current INTEGER NOT NULL DEFAULT 1, last_verified_at TEXT, metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS locations_item ON locations(item_id,is_current);
CREATE TABLE IF NOT EXISTS notes (
 item_id TEXT PRIMARY KEY REFERENCES items(item_id), body TEXT NOT NULL, occurred_at TEXT, source_ref TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS current_state (
 item_id TEXT PRIMARY KEY REFERENCES items(item_id), state_json TEXT NOT NULL,
 effective_at TEXT, source_ref TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS changes (
 change_id TEXT PRIMARY KEY, item_id TEXT NOT NULL REFERENCES items(item_id), revision INTEGER NOT NULL,
 before_json TEXT NOT NULL, after_json TEXT NOT NULL, recorded_at TEXT NOT NULL, effective_at TEXT,
 source_ref TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '', operation_id TEXT
);
CREATE INDEX IF NOT EXISTS changes_item ON changes(item_id,revision);
CREATE TABLE IF NOT EXISTS profile_fields (
 field_key TEXT PRIMARY KEY, item_id TEXT NOT NULL UNIQUE REFERENCES items(item_id), value_json TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'unverified', verified_at TEXT, source_ref TEXT NOT NULL DEFAULT '', metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS search_entries (
 entry_id INTEGER PRIMARY KEY, item_id TEXT NOT NULL REFERENCES items(item_id), entry_key TEXT NOT NULL,
 heading TEXT NOT NULL DEFAULT '', text TEXT NOT NULL, domain TEXT NOT NULL,
 authority TEXT NOT NULL, evidence_kind TEXT NOT NULL, author_role TEXT NOT NULL DEFAULT '',
 source_ref TEXT NOT NULL DEFAULT '', version_id TEXT NOT NULL DEFAULT '', is_current INTEGER NOT NULL DEFAULT 1,
 topics_json TEXT NOT NULL DEFAULT '[]', index_version INTEGER NOT NULL DEFAULT 1, UNIQUE(item_id,entry_key)
);
CREATE INDEX IF NOT EXISTS search_entries_scope ON search_entries(domain,authority,is_current,item_id);
CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(heading,text,content='search_entries',content_rowid='entry_id',tokenize='trigram');
CREATE TRIGGER IF NOT EXISTS search_insert AFTER INSERT ON search_entries BEGIN
 INSERT INTO search_fts(rowid,heading,text) VALUES(new.entry_id,new.heading,new.text);
END;
CREATE TRIGGER IF NOT EXISTS search_delete AFTER DELETE ON search_entries BEGIN
 INSERT INTO search_fts(search_fts,rowid,heading,text) VALUES('delete',old.entry_id,old.heading,old.text);
END;
CREATE TRIGGER IF NOT EXISTS search_update AFTER UPDATE ON search_entries BEGIN
 INSERT INTO search_fts(search_fts,rowid,heading,text) VALUES('delete',old.entry_id,old.heading,old.text);
 INSERT INTO search_fts(rowid,heading,text) VALUES(new.entry_id,new.heading,new.text);
END;
CREATE TABLE IF NOT EXISTS v2_migrations (migration_id TEXT PRIMARY KEY,applied_at TEXT NOT NULL,metadata_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS migration_files (source_path TEXT PRIMARY KEY,target_path TEXT,sha256 TEXT,byte_size INTEGER,status TEXT,notes TEXT);
CREATE TABLE IF NOT EXISTS document_processing (version_id TEXT PRIMARY KEY REFERENCES versions(version_id),state TEXT NOT NULL,error TEXT,extractor TEXT,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS write_receipts (operation_id TEXT PRIMARY KEY,item_id TEXT NOT NULL REFERENCES items(item_id),created_at TEXT NOT NULL);
