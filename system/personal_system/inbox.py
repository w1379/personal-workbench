"""Explicit, resumable ingestion of structured submissions into the sole PIS root."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import uuid
import zipfile

import inbox as wire
from . import core, documents
from .delivery import FileLock

LEDGER_SQL = '''CREATE TABLE IF NOT EXISTS inbox_imports (
 submission_id TEXT PRIMARY KEY, package_sha256 TEXT NOT NULL,
 manifest_json TEXT NOT NULL, plan_json TEXT NOT NULL,
 status TEXT NOT NULL, receipt_json TEXT, error TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
)'''


def packages(root):
    return core.root_path(root) / 'data/inbox/packages'


def package_path(root, sid):
    return packages(root) / (wire.identifier(sid) + '.zip')


def local_list(root):
    results = []
    c = core.connect(root, readonly=True)
    try:
        ledger = {r['submission_id']: dict(r) for r in c.execute('SELECT * FROM inbox_imports')} if c.execute(
            "SELECT 1 FROM sqlite_master WHERE name='inbox_imports'").fetchone() else {}
    finally:
        c.close()
    for path in sorted(packages(root).glob('*.zip')):
        try:
            info = wire.inspect_package(path)
            m = info['manifest']
            row = ledger.get(m['submission_id'], {})
            results.append({'submission_id': m['submission_id'], 'title': m['title'],
                            'kind': m['kind'], 'project_hint': m.get('project_hint', ''),
                            'status': row.get('status', 'pending'), 'error': row.get('error')})
        except Exception as exc:
            results.append({'file': path.name, 'status': 'invalid', 'error': str(exc)})
    return {'packages': results}


def stable_id(sid, role):
    return 'document:' + uuid.uuid5(uuid.NAMESPACE_URL, 'pis-inbox:' + sid + ':' + role).hex


def render(m):
    source = m['source']
    lines = ['# ' + m['title'], '', m['summary'], '', '## 来源',
             '- 投递编号：' + m['submission_id'], '- 类型：' + m['kind'],
             '- 来源设备：' + source['device'], '- 来源依据：' + source['reference'],
             '- 原位置（来源设备上的位置，不是本机路径）：' + source.get('path', ''),
             '- 依据时间：' + source.get('occurred_at', ''), '- 打包时间：' + m['created_at'],
             '- 关联项目线索：' + m.get('project_hint', ''), '', '## 事实与判断']
    for fact in m['facts']:
        lines.append(f"- [{fact['evidence']}] {fact['text']}（依据：{fact['source']}）")
    for key, label in (('decisions', '已确认决定'), ('open_questions', '待确认问题')):
        lines += ['', '## ' + label, *('- ' + x for x in m[key])]
    lines += ['', '## 随附原件', *('- ' + a['title'] + '：' + a['original_name'] for a in m['attachments'])]
    if m['kind'] == 'correction':
        lines += ['', '本条保存更正依据；不表示已覆盖既有个人字段或项目当前状态。']
    return '\n'.join(lines) + '\n'


def import_package(root, sid, *, links=None, reviewed=False):
    root, sid = core.root_path(root), wire.identifier(sid)
    package = package_path(root, sid)
    info = wire.inspect_package(package)
    m, checksum = info['manifest'], info['sha256']
    if m['submission_id'] != sid:
        raise ValueError('Package filename and submission identity differ')
    plan = {'links': sorted(set(links or [])), 'reviewed': bool(reviewed)}
    if m['kind'] == 'correction' and not reviewed:
        return {'ok': False, 'submission_id': sid, 'status': 'needs_review',
                'action': 'Read the correction and relevant current records. Use --reviewed to retain its evidence; apply any field changes separately with revision checks.'}
    with FileLock(root / 'data/inbox/import.lock'):
        c = core.connect(root)
        try:
            def begin(con):
                con.execute(LEDGER_SQL)
                old = con.execute('SELECT * FROM inbox_imports WHERE submission_id=?', (sid,)).fetchone()
                if old:
                    if old['package_sha256'] != checksum:
                        raise ValueError('Same submission identity has different contents')
                    if old['status'] == 'imported':
                        if links is not None and plan['links'] != json.loads(old['plan_json'])['links']:
                            raise ValueError('Already imported with different links; use pis relate for new links')
                        return dict(old)
                    if links is None:
                        plan['links'] = json.loads(old['plan_json'])['links']
                    if json.loads(old['plan_json']) != plan:
                        raise ValueError('Resume with the original import plan')
                for target in plan['links']:
                    core._item(con, target)
                if old:
                    con.execute("UPDATE inbox_imports SET status='importing',error=NULL,updated_at=? WHERE submission_id=?", (core.now(), sid))
                else:
                    con.execute('INSERT INTO inbox_imports VALUES(?,?,?,?,?,?,?,?,?)',
                                (sid, checksum, core.json_text(m), core.json_text(plan), 'importing', None, None, core.now(), core.now()))
                return None
            previous = core.transaction(c, begin)
        finally:
            c.close()
        if previous:
            return json.loads(previous['receipt_json'])
        try:
            # A complete immutable package is saved before creating its searchable note.
            archive = documents.save(root, package, document_id=stable_id(sid, 'package'),
                                     title=m['title'] + ' · 投递原始包', parse=False,
                                     evidence_kind='structured_submission', domain=m.get('domain') or 'general')
            attachments = []
            cache = root / 'cache'
            cache.mkdir(exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='inbox-', dir=cache) as directory:
                with zipfile.ZipFile(package) as z:
                    for a in m['attachments']:
                        # Names are validated; never extract arbitrary ZIP paths.
                        path = Path(directory) / a['path'].split('/')[1]
                        with z.open(a['path']) as src, path.open('wb') as dst:
                            shutil.copyfileobj(src, dst, 1024 * 1024)
                        saved = documents.save(root, path, document_id=stable_id(sid, a['path']),
                                               title=a['title'], authority='user', evidence_kind='user_provided',
                                               domain=m.get('domain') or 'general')
                        attachments.append({'item_id': saved['item_id'], 'version_id': saved['version_id'],
                                            'title': a['title'], 'sha256': a['sha256'],
                                            'processing_state': saved['status']})
            source = 'inbox:' + sid + ' | ' + m['source']['device'] + ' | ' + m['source']['reference']
            note = core.add_note(root, render(m), title=m['title'], source_ref=source,
                                 occurred_at=m['source'].get('occurred_at') or None,
                                 domain=m.get('domain') or 'general', evidence_kind='structured_submission',
                                 links=[archive['item_id'], *(x['item_id'] for x in attachments), *plan['links']],
                                 operation_id='inbox:' + sid)
            ack = {'submission_id': sid, 'sha256': checksum, 'status': 'imported',
                   'note_id': note['item_id'], 'package_document_id': archive['item_id'],
                   'attachments': attachments, 'linked_items': plan['links'],
                   'imported_at': core.now(), 'consumer': 'personal-information-system-v2',
                   'correction_applied': False if m['kind'] == 'correction' else None}
            c = core.connect(root)
            try:
                core.transaction(c, lambda con: con.execute(
                    "UPDATE inbox_imports SET status='imported',receipt_json=?,error=NULL,updated_at=? WHERE submission_id=?",
                    (core.json_text(ack), core.now(), sid)))
            finally:
                c.close()
            return ack
        except Exception as exc:
            c = core.connect(root)
            try:
                core.transaction(c, lambda con: con.execute(
                    "UPDATE inbox_imports SET status='failed',error=?,updated_at=? WHERE submission_id=?",
                    (str(exc), core.now(), sid)))
            finally:
                c.close()
            raise


def send_receipt(root, sid, host=None, remote_root=wire.DEFAULT_REMOTE_ROOT):
    c = core.connect(root, readonly=True)
    try:
        row = c.execute('SELECT status,receipt_json FROM inbox_imports WHERE submission_id=?',
                        (wire.identifier(sid),)).fetchone()
        if not row or row['status'] != 'imported':
            raise ValueError('No completed local import for this submission')
        ack = json.loads(row['receipt_json'])
    finally:
        c.close()
    return wire.remote(host, remote_root, 'ack', data=wire.encoded(ack))


def run(root, args):
    action = args.inbox_action
    if action == 'pending':
        return {'packages': wire.remote(args.host, args.remote_root, 'list')}
    if action == 'fetch':
        return wire.fetch(packages(root), args.host, args.remote_root, submission_ids=args.submission_ids)
    if action == 'list':
        return local_list(root)
    if action == 'inspect':
        return wire.inspect_package(package_path(root, args.submission_id))
    if action == 'ack':
        return send_receipt(root, args.submission_id, args.host, args.remote_root)
    if not args.approved:
        return {'ok': False, 'status': 'needs_approval',
                'action': 'Show the user the candidate title, source, summary and attachments. Import only IDs explicitly approved by the user; then use --approved.'}
    if args.ack and not args.host:
        raise ValueError('--host is required when importing with --ack')
    result = import_package(root, args.submission_id, links=args.link, reviewed=args.reviewed)
    if args.ack and result.get('status') == 'imported':
        try:
            return dict(result, server_receipt=send_receipt(root, args.submission_id, args.host, args.remote_root))
        except Exception as exc:
            return dict(result, ok=False, server_receipt_error=str(exc),
                        action='Local import completed. Retry inbox ack; do not create a new submission.')
    return result


def add_parser(sub):
    parser = sub.add_parser('inbox', help='Fetch, inspect, import and acknowledge SSH submissions')
    commands = parser.add_subparsers(dest='inbox_action', required=True)
    for action in ('pending', 'fetch', 'list', 'inspect', 'import', 'ack'):
        p = commands.add_parser(action)
        if action in ('inspect', 'import', 'ack'):
            p.add_argument('submission_id')
        if action == 'fetch':
            p.add_argument('submission_ids', nargs='+', help='Only download submissions explicitly selected by the user')
        if action in ('pending', 'fetch', 'import', 'ack'):
            p.add_argument('--host', required=action in ('pending', 'fetch', 'ack'))
            p.add_argument('--remote-root', default=wire.DEFAULT_REMOTE_ROOT)
        if action == 'import':
            p.add_argument('--approved', action='store_true', help='The user explicitly approved importing this submission; never infer approval from upload or a request to list pending items')
            p.add_argument('--link', action='append')
            p.add_argument('--reviewed', action='store_true', help='Correction evidence has been reviewed; does not update existing facts')
            p.add_argument('--ack', action='store_true', help='Send server receipt after successful local import')
