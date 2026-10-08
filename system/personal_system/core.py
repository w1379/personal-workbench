"""Small transactional storage primitives. No network, services or LLM calls."""
from __future__ import annotations
import hashlib
import html.parser
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import sqlite3
import time
from datetime import datetime, timezone
import uuid


class ConflictError(RuntimeError):
    """Re-read the current item and merge; never blindly retry stale values."""


def root_path(root=None):
    return Path(root).resolve() if root is not None else Path(__file__).resolve().parents[2]


def db_path(root=None):
    return root_path(root) / 'data/library.sqlite3'


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def uid(prefix):
    return f'{prefix}:{uuid.uuid4().hex}'


def json_text(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=str)


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def connect(root=None, readonly=False):
    path = db_path(root)
    if readonly:
        con = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=15)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    con.execute('PRAGMA busy_timeout=15000')
    if readonly:
        con.execute('PRAGMA query_only=ON')
        def readonly_authorizer(action, arg1, arg2, database, trigger):
            if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_PRAGMA and arg2 is not None and str(arg1).lower() in {'query_only','writable_schema','journal_mode'}:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        con.set_authorizer(readonly_authorizer)
    else:
        mode = con.execute('PRAGMA journal_mode=WAL').fetchone()[0]
        if mode.lower() != 'wal':
            con.close()
            raise RuntimeError('WAL unavailable')
        con.execute('PRAGMA synchronous=FULL')
        con.execute('PRAGMA wal_autocheckpoint=1000')
    return con


def transaction(con, callback, attempts=6):
    if con.in_transaction:
        raise RuntimeError('Nested write transaction is not supported')
    for attempt in range(attempts):
        try:
            con.execute('BEGIN IMMEDIATE')
            result = callback(con)
            con.commit()
            return result
        except sqlite3.OperationalError as exc:
            con.rollback()
            code = getattr(exc, 'sqlite_errorcode', 0) & 255
            if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or attempt == attempts - 1:
                raise
            time.sleep(min(.05 * 2 ** attempt, 1))
        except BaseException:
            con.rollback()
            raise


def initialize(root=None):
    base = root_path(root)
    for directory in ('data/originals', 'workspaces', 'derived', 'exports/history', 'exports/current', 'cache', 'backups'):
        (base / directory).mkdir(parents=True, exist_ok=True)
    schema = Path(__file__).resolve().parents[1] / 'schema'
    con = connect(base)
    try:
        # Import baseline definitions only on an empty database; never seed old JSON into current state.
        if not con.execute("SELECT 1 FROM sqlite_master WHERE name='documents'").fetchone():
            for name in ('legacy.sql', 'legacy_advisor.sql', 'legacy_infrastructure.sql'):
                con.executescript((schema / name).read_text(encoding='utf-8'))
        con.executescript((schema / 'v2.sql').read_text(encoding='utf-8'))
        con.execute('INSERT OR IGNORE INTO v2_migrations VALUES(?,?,?)', ('v2-001', now(), json_text({'version': 2})))
        con.commit()
    finally:
        con.close()
    return {'root': str(base), 'database': str(db_path(base)), 'schema': 'v2-001', 'sqlite': sqlite3.sqlite_version}


def new_item(con, kind, title, description='', domain='general', authority='user', evidence_kind='user_statement', item_id=None, metadata=None):
    item_id = item_id or uid(kind)
    stamp = now()
    con.execute('INSERT INTO items(item_id,kind,title,description,domain,authority,evidence_kind,created_at,updated_at,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (item_id, kind, title, description, domain, authority, evidence_kind, stamp, stamp, json_text(metadata or {})))
    return item_id


def index_entry(con, item_id, text, *, entry_key='main', heading='', source_ref='', version_id='', is_current=1, authority=None, evidence_kind=None, author_role='', topics=None):
    item = con.execute('SELECT * FROM items WHERE item_id=?', (item_id,)).fetchone()
    if item is None:
        raise KeyError(item_id)
    con.execute('''INSERT INTO search_entries(item_id,entry_key,heading,text,domain,authority,evidence_kind,author_role,source_ref,version_id,is_current,topics_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(item_id,entry_key) DO UPDATE SET
        heading=excluded.heading,text=excluded.text,domain=excluded.domain,authority=excluded.authority,
        evidence_kind=excluded.evidence_kind,author_role=excluded.author_role,source_ref=excluded.source_ref,
        version_id=excluded.version_id,is_current=excluded.is_current,topics_json=excluded.topics_json,index_version=1''',
        (item_id, entry_key, heading or item['title'], str(text), item['domain'], authority or item['authority'], evidence_kind or item['evidence_kind'], author_role, source_ref or '', version_id or '', int(is_current), json_text(topics or [])))


def _item(con, item_id):
    row = con.execute('SELECT * FROM items WHERE item_id=?', (item_id,)).fetchone()
    if row is None:
        raise KeyError(f'Unknown item: {item_id}')
    return dict(row)


def _bump(con, item_id, expected_revision):
    if expected_revision is None:
        raise ValueError('Existing item updates require expected_revision from a fresh read')
    row = con.execute('UPDATE items SET revision=revision+1,updated_at=? WHERE item_id=? AND revision=? RETURNING revision',
                      (now(), item_id, int(expected_revision))).fetchone()
    if row is None:
        actual = con.execute('SELECT revision FROM items WHERE item_id=?', (item_id,)).fetchone()
        raise ConflictError(f'{item_id}: expected revision {expected_revision}; current {actual[0] if actual else "missing"}')
    return row[0]


def _change(con, item_id, revision, before, after, *, effective_at=None, source_ref='', reason='', operation_id=None):
    change_id = uid('change')
    con.execute('INSERT INTO changes VALUES(?,?,?,?,?,?,?,?,?,?)',
        (change_id, item_id, revision, json_text(before), json_text(after), now(), effective_at, source_ref, reason, operation_id))
    if before is not None:
        index_entry(con, item_id, json_text(before), entry_key='change:' + change_id,
                    source_ref='changes:' + change_id + '#before', version_id='revision:' + str(revision - 1), is_current=0)


def get(root, item_id):
    con = connect(root, readonly=True)
    try:
        item = _item(con, item_id)
        for table, key in (('notes', 'note'), ('current_state', 'current_state'), ('profile_fields', 'profile')):
            row = con.execute(f'SELECT * FROM {table} WHERE item_id=?', (item_id,)).fetchone()
            if row:
                item[key] = dict(row)
        item['aliases'] = [dict(r) for r in con.execute('SELECT * FROM aliases WHERE item_id=?', (item_id,))]
        item['locations'] = [dict(r) for r in con.execute('SELECT * FROM locations WHERE item_id=? ORDER BY is_current DESC', (item_id,))]
        for loc in item['locations']:
            path = Path(loc['path']) if loc['is_external'] else root_path(root) / loc['path']
            loc['resolved_path'], loc['exists'] = str(path), path.exists()
        item['links'] = [dict(r) for r in con.execute('SELECT * FROM links WHERE from_item=? OR to_item=?', (item_id, item_id))]
        if con.execute('SELECT 1 FROM documents WHERE document_id=?', (item_id,)).fetchone():
            item['document'] = dict(con.execute('SELECT * FROM documents WHERE document_id=?', (item_id,)).fetchone())
            item['versions'] = [dict(r) for r in con.execute('SELECT v.*,p.state AS processing_state,p.error AS processing_error FROM versions v LEFT JOIN document_processing p USING(version_id) WHERE document_id=? ORDER BY is_current DESC,captured_at DESC', (item_id,))]
        return item
    finally:
        con.close()


def add_note(root, body, title=None, *, source_ref='', occurred_at=None, domain='general', evidence_kind='user_statement', links=None, operation_id=None):
    if not body.strip():
        raise ValueError('Empty note')
    con = connect(root)
    try:
        def write(c):
            if operation_id:
                old = c.execute('SELECT item_id FROM write_receipts WHERE operation_id=?', (operation_id,)).fetchone()
                if old:
                    return old[0]
            item_id = new_item(c, 'note', title or body.strip().splitlines()[0][:80], domain=domain, evidence_kind=evidence_kind, metadata={'auto_title': not bool(title)})
            c.execute('INSERT INTO notes VALUES(?,?,?,?)', (item_id, body, occurred_at, source_ref))
            index_entry(c, item_id, body, source_ref=source_ref)
            for parent in links or []:
                c.execute('INSERT INTO links VALUES(?,?,?,?)', (item_id, parent, 'related_to', source_ref))
            if operation_id:
                c.execute('INSERT INTO write_receipts VALUES(?,?,?)', (operation_id, item_id, now()))
            return item_id
        item_id = transaction(con, write)
    finally:
        con.close()
    return {'item_id': item_id, 'revision': 1, 'status': 'full_text_searchable'}


def update_note(root, item_id, body, expected_revision, *, source_ref='', reason=''):
    con = connect(root)
    try:
        def write(c):
            if not body.strip():
                raise ValueError('Empty note')
            previous = c.execute('SELECT body FROM notes WHERE item_id=?', (item_id,)).fetchone()
            if previous is None:
                raise KeyError(item_id)
            revision = _bump(c, item_id, expected_revision)
            item = _item(c, item_id)
            if json.loads(item['metadata_json']).get('auto_title'):
                c.execute('UPDATE items SET title=? WHERE item_id=?', (body.strip().splitlines()[0][:80], item_id))
            c.execute('UPDATE notes SET body=?,source_ref=? WHERE item_id=?', (body, source_ref, item_id))
            _change(c, item_id, revision, {'body': previous[0]}, {'body': body}, source_ref=source_ref, reason=reason)
            index_entry(c, item_id, body, source_ref=source_ref)
            return {'item_id': item_id, 'revision': revision, 'status': 'full_text_searchable'}
        return transaction(con, write)
    finally:
        con.close()


def register(root, kind, title, *, description='', path=None, aliases=None, domain='general', item_id=None, metadata=None):
    base = root_path(root)
    if kind == 'workspace' and path is None:
        from .delivery import workspace_lock
        with workspace_lock(base):
            path = base / 'workspaces' / (uuid.uuid4().hex[:12] + '-' + re.sub(r'[<>:"/\\|?*\s]+', '-', title)[:60])
            Path(path).mkdir(parents=True)
    con = connect(base)
    try:
        def write(c):
            ident = new_item(c, kind, title, description, domain=domain, item_id=item_id, metadata=metadata)
            for alias in aliases or []:
                c.execute('INSERT OR IGNORE INTO aliases VALUES(?,?,?)', (ident, alias, ''))
            if path:
                absolute = Path(path).resolve() if Path(path).is_absolute() else (base / path).resolve()
                external = not absolute.is_relative_to(base)
                stored = str(absolute) if external else absolute.relative_to(base).as_posix()
                c.execute('INSERT INTO locations VALUES(?,?,?,?,?,?,?,?)', (uid('location'), ident, stored, 'primary', int(external), 1, now() if absolute.exists() else None, '{}'))
            index_entry(c, ident, '\n'.join([title, description, *(aliases or [])]))
            return ident
        ident = transaction(con, write)
    finally:
        con.close()
    return get(base, ident)


def list_todos(root, *, include_closed=False):
    """Read the explicit personal list, never inferred notification deadlines."""
    con = connect(root, readonly=True)
    try:
        rows = con.execute('''SELECT i.item_id,i.title,i.description,i.revision,i.archived_at,
            s.state_json,s.source_ref,s.effective_at FROM items i
            LEFT JOIN current_state s USING(item_id)
            WHERE i.kind='task' AND i.domain='personal_todo' ORDER BY i.created_at,i.item_id''')
        results = []
        for row in rows:
            item = dict(row)
            state = json.loads(item.pop('state_json') or '{}')
            # Incomplete registration stays visible so it can be finished later.
            item['state'] = state
            item['status'] = state.get('status') or 'needs_review'
            if not include_closed and (item['archived_at'] or item['status'] in ('completed', 'cancelled')):
                continue
            results.append(item)
        results.sort(key=lambda item: (not bool(item['state'].get('due_on')),
                                       item['state'].get('due_on') or ''))
        return {'scope': 'personal_todo', 'include_closed': include_closed,
                'total': len(results), 'items': results}
    finally:
        con.close()


def set_state(root, item_id, state, expected_revision, *, source_ref='', effective_at=None, reason=''):
    con = connect(root)
    try:
        def write(c):
            item = _item(c, item_id)
            if item['kind'] in ('document', 'advisor', 'admission', 'admission_target', 'admission_route', 'job', 'career_position', 'career_application', 'infrastructure_observation', 'event', 'profile_field', 'profile'):
                raise ValueError('This object has a dedicated authoritative table; update that table through its module')
            old = c.execute('SELECT state_json FROM current_state WHERE item_id=?', (item_id,)).fetchone()
            revision = _bump(c, item_id, expected_revision)
            c.execute('INSERT INTO current_state VALUES(?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET state_json=excluded.state_json,effective_at=excluded.effective_at,source_ref=excluded.source_ref', (item_id, json_text(state), effective_at, source_ref))
            _change(c, item_id, revision, json.loads(old[0]) if old else None, state, source_ref=source_ref, effective_at=effective_at, reason=reason)
            index_entry(c, item_id, json_text(state), entry_key='state', source_ref=source_ref)
            return {'item_id': item_id, 'revision': revision}
        return transaction(con, write)
    finally:
        con.close()


def set_profile(root, field_key, value, expected_revision=None, *, status='unverified', verified_at=None, source_ref='', metadata=None):
    con = connect(root)
    try:
        def write(c):
            old = c.execute('SELECT * FROM profile_fields WHERE field_key=?', (field_key,)).fetchone()
            if old:
                item_id = old['item_id']
                revision = _bump(c, item_id, expected_revision)
                _change(c, item_id, revision, dict(old), {'value': value, 'status': status}, source_ref=source_ref)
                meta = json.loads(old['metadata_json']); meta.update(metadata or {})
            else:
                if expected_revision is not None:
                    raise ConflictError('Profile field disappeared')
                item_id = new_item(c, 'profile_field', field_key, domain='personal', item_id='profile:' + field_key)
                revision, meta = 1, metadata or {}
            c.execute('''INSERT INTO profile_fields VALUES(?,?,?,?,?,?,?) ON CONFLICT(field_key) DO UPDATE SET
                value_json=excluded.value_json,status=excluded.status,verified_at=excluded.verified_at,source_ref=excluded.source_ref,metadata_json=excluded.metadata_json''',
                (field_key, item_id, json_text(value), status, verified_at, source_ref, json_text(meta)))
            index_entry(c, item_id, field_key + '\n' + json_text(value), source_ref=source_ref)
            return {'item_id': item_id, 'revision': revision, 'field_key': field_key}
        return transaction(con, write)
    finally:
        con.close()


def relate(root, from_item, to_item, relation_type='related_to', source_ref=''):
    con = connect(root)
    try:
        return transaction(con, lambda c: (c.execute('INSERT INTO links VALUES(?,?,?,?) ON CONFLICT(from_item,to_item,relation_type) DO UPDATE SET source_ref=excluded.source_ref', (from_item, to_item, relation_type, source_ref)), {'from_item': from_item, 'to_item': to_item})[1])
    finally:
        con.close()


def archive(root, item_id, expected_revision, archived=True, reason=''):
    con = connect(root)
    try:
        def write(c):
            old = _item(c, item_id)
            revision = _bump(c, item_id, expected_revision)
            value = now() if archived else None
            c.execute('UPDATE items SET archived_at=? WHERE item_id=?', (value, item_id))
            _change(c, item_id, revision, {'archived_at': old['archived_at']}, {'archived_at': value}, reason=reason)
            return {'item_id': item_id, 'revision': revision, 'archived_at': value}
        return transaction(con, write)
    finally:
        con.close()


def relocate(root, item_id, path, expected_revision):
    base, con = root_path(root), connect(root)
    absolute = Path(path).resolve() if Path(path).is_absolute() else (base / path).resolve()
    if not absolute.exists():
        con.close()
        raise FileNotFoundError(absolute)
    try:
        def write(c):
            revision = _bump(c, item_id, expected_revision)
            old = [dict(r) for r in c.execute('SELECT * FROM locations WHERE item_id=? AND is_current=1', (item_id,))]
            external = not absolute.is_relative_to(base)
            stored = str(absolute) if external else absolute.relative_to(base).as_posix()
            c.execute('UPDATE locations SET is_current=0 WHERE item_id=?', (item_id,))
            c.execute('INSERT INTO locations VALUES(?,?,?,?,?,?,?,?)', (uid('location'), item_id, stored, 'primary', int(external), 1, now(), '{}'))
            _change(c, item_id, revision, old, {'path': stored}, reason='Registered new location; files were not moved by this operation')
            return {'item_id': item_id, 'revision': revision, 'path': stored}
        return transaction(con, write)
    finally:
        con.close()


BUSINESS_KEYS = {
    'research_advisors': 'advisor_id', 'research_routes': 'route_id',
    'admission_targets': 'target_id', 'career_positions': 'position_id',
    'career_applications': 'application_id', 'infrastructure_observations': 'observation_id',
}


def update_record(root, table, record_id, fields, expected_revision, *, source_ref='', reason=''):
    """Update a retained structured object with the same conflict/history contract."""
    if table not in BUSINESS_KEYS or not isinstance(fields, dict) or not fields:
        raise ValueError('Unsupported business table or empty field update')
    pk = BUSINESS_KEYS[table]
    item_id = 'route:' + record_id if table == 'research_routes' else record_id
    con = connect(root)
    try:
        columns = {r['name'] for r in con.execute(f'PRAGMA table_info({table})')}
        foreign = {r['from'] for r in con.execute(f'PRAGMA foreign_key_list({table})')}
        prohibited = {pk, *foreign}
        if not set(fields) <= columns or set(fields) & prohibited:
            raise ValueError('Unknown field or identity/foreign-key change; use explicit relationship maintenance')
        values = {key: json_text(value) if isinstance(value, (dict,list)) else value for key,value in fields.items()}
        def write(c):
            before = c.execute(f'SELECT * FROM {table} WHERE {pk}=?', (record_id,)).fetchone()
            if before is None:
                raise KeyError(record_id)
            revision = _bump(c, item_id, expected_revision)
            if 'updated_at' in columns:
                values['updated_at'] = now()
            assignments = ','.join('"' + key + '"=?' for key in values)
            c.execute(f'UPDATE {table} SET {assignments} WHERE {pk}=?', [*values.values(), record_id])
            after = dict(c.execute(f'SELECT * FROM {table} WHERE {pk}=?', (record_id,)).fetchone())
            _change(c, item_id, revision, dict(before), after, source_ref=source_ref, reason=reason)
            from .search import reindex_item
            reindex_item(c, item_id)
            return {'item_id': item_id, 'revision': revision, 'updated_fields': list(values)}
        return transaction(con, write)
    finally:
        con.close()
