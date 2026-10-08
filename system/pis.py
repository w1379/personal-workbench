#!/usr/bin/env python3
"""One short-lived CLI for the local personal information system."""
from pathlib import Path
import argparse
import json
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from personal_system import core


def payload(value):
    return json.loads(Path(value[1:]).read_text(encoding='utf-8-sig')) if value.startswith('@') else json.loads(value)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, default=core.root_path())
    sub = p.add_subparsers(dest='command', required=True)
    from personal_system import inbox
    inbox.add_parser(sub)
    sub.add_parser('init')
    sub.add_parser('status')
    s = sub.add_parser('todos', help='List personal todos explicitly added by the user'); s.add_argument('--all', action='store_true', help='Include completed, cancelled and archived todos')
    s = sub.add_parser('note'); s.add_argument('text', nargs='?'); s.add_argument('--file', type=Path); s.add_argument('--title'); s.add_argument('--source', default=''); s.add_argument('--occurred-at'); s.add_argument('--domain', default='general'); s.add_argument('--link', action='append'); s.add_argument('--operation-id')
    s = sub.add_parser('note-update'); s.add_argument('item_id'); s.add_argument('--file', type=Path, required=True); s.add_argument('--revision', type=int, required=True); s.add_argument('--source', default=''); s.add_argument('--reason', default='')
    s = sub.add_parser('get'); s.add_argument('item_id')
    s = sub.add_parser('history'); s.add_argument('item_id'); s.add_argument('--limit', type=int, default=30)
    s = sub.add_parser('list'); s.add_argument('--kind'); s.add_argument('--domain'); s.add_argument('--active-only', action='store_true'); s.add_argument('--limit', type=int, default=50); s.add_argument('--offset', type=int, default=0)
    s = sub.add_parser('register'); s.add_argument('title'); s.add_argument('--kind', default='project'); s.add_argument('--description', default=''); s.add_argument('--path'); s.add_argument('--alias', action='append'); s.add_argument('--domain', default='general')
    s = sub.add_parser('workspace'); s.add_argument('title'); s.add_argument('--description', default='')
    s = sub.add_parser('state'); s.add_argument('item_id'); s.add_argument('--json', required=True); s.add_argument('--revision', type=int, required=True); s.add_argument('--source', default=''); s.add_argument('--effective-at'); s.add_argument('--reason', default='')
    s = sub.add_parser('profile-get'); s.add_argument('field_key')
    s = sub.add_parser('profile-set'); s.add_argument('field_key'); s.add_argument('--json', required=True); s.add_argument('--revision', type=int); s.add_argument('--status', default='unverified'); s.add_argument('--verified-at'); s.add_argument('--source', default='')
    s = sub.add_parser('relate'); s.add_argument('from_item'); s.add_argument('to_item'); s.add_argument('--type', default='related_to'); s.add_argument('--source', default='')
    s = sub.add_parser('archive'); s.add_argument('item_id'); s.add_argument('--revision', type=int, required=True); s.add_argument('--restore', action='store_true'); s.add_argument('--reason', default='')
    s = sub.add_parser('relocate'); s.add_argument('item_id'); s.add_argument('path'); s.add_argument('--revision', type=int, required=True)
    s = sub.add_parser('save'); s.add_argument('file', type=Path); s.add_argument('--title'); s.add_argument('--document-id'); s.add_argument('--revision', type=int); s.add_argument('--source-id'); s.add_argument('--url'); s.add_argument('--authority', default='user'); s.add_argument('--evidence-kind', default='user_provided'); s.add_argument('--domain', default='general'); s.add_argument('--topic', action='append'); s.add_argument('--published-at'); s.add_argument('--no-parse', action='store_true')
    s = sub.add_parser('parse'); s.add_argument('version_id')
    s = sub.add_parser('search'); s.add_argument('query'); s.add_argument('--domain'); s.add_argument('--kind'); s.add_argument('--official-only', action='store_true'); s.add_argument('--authority'); s.add_argument('--topic', action='append'); s.add_argument('--topic-mode', choices=['any','all'], default='any'); s.add_argument('--history', choices=['all','current','historical'], default='all'); s.add_argument('--active-only', action='store_true'); s.add_argument('--evidence-kind'); s.add_argument('--limit', type=int, default=20); s.add_argument('--offset', type=int, default=0)
    s = sub.add_parser('resolve'); s.add_argument('query'); s.add_argument('--limit', type=int, default=20)
    s = sub.add_parser('school-search'); s.add_argument('query'); s.add_argument('--topic', action='append'); s.add_argument('--topic-mode', choices=['any','all'], default='any'); s.add_argument('--include-unofficial', action='store_true'); s.add_argument('--limit', type=int, default=20)
    s = sub.add_parser('advisor-search'); s.add_argument('query'); s.add_argument('--line'); s.add_argument('--limit', type=int, default=20)
    sub.add_parser('reindex')
    s = sub.add_parser('sql'); s.add_argument('query'); s.add_argument('--params', default='[]')
    s = sub.add_parser('record-update'); s.add_argument('table'); s.add_argument('record_id'); s.add_argument('--json', required=True); s.add_argument('--revision', type=int, required=True); s.add_argument('--source', default=''); s.add_argument('--reason', default='')
    s = sub.add_parser('bundle'); s.add_argument('--title', required=True); s.add_argument('files', nargs='+', type=Path)
    s = sub.add_parser('bundle-restore'); s.add_argument('bundle_id')
    sub.add_parser('bundle-recover')
    sub.add_parser('bundle-list')
    s = sub.add_parser('check'); s.add_argument('--full', action='store_true')
    s = sub.add_parser('backup'); s.add_argument('--destination', type=Path)
    s = sub.add_parser('restore'); s.add_argument('backup_path', type=Path); s.add_argument('destination', type=Path)
    return p


def run(a):
    r, cmd = a.root, a.command
    if cmd == 'inbox':
        from personal_system import inbox
        return inbox.run(r, a)
    if cmd == 'init': return core.initialize(r)
    if cmd == 'todos': return core.list_todos(r, include_closed=a.all)
    if cmd == 'note': return core.add_note(r, a.file.read_text(encoding='utf-8-sig') if a.file else a.text or '', title=a.title, source_ref=a.source, occurred_at=a.occurred_at, domain=a.domain, links=a.link, operation_id=a.operation_id)
    if cmd == 'note-update': return core.update_note(r,a.item_id,a.file.read_text(encoding='utf-8-sig'),a.revision,source_ref=a.source,reason=a.reason)
    if cmd == 'get': return core.get(r, a.item_id)
    if cmd == 'register': return core.register(r,a.kind,a.title,description=a.description,path=a.path,aliases=a.alias,domain=a.domain)
    if cmd == 'workspace': return core.register(r,'workspace',a.title,description=a.description)
    if cmd == 'state': return core.set_state(r,a.item_id,payload(a.json),a.revision,source_ref=a.source,effective_at=a.effective_at,reason=a.reason)
    if cmd == 'profile-set': return core.set_profile(r,a.field_key,payload(a.json),a.revision,status=a.status,verified_at=a.verified_at,source_ref=a.source)
    if cmd == 'relate': return core.relate(r,a.from_item,a.to_item,a.type,a.source)
    if cmd == 'archive': return core.archive(r,a.item_id,a.revision,not a.restore,a.reason)
    if cmd == 'relocate': return core.relocate(r,a.item_id,a.path,a.revision)
    if cmd == 'save':
        from personal_system.documents import save
        return save(r,a.file,title=a.title,document_id=a.document_id,source_id=a.source_id,source_url=a.url,authority=a.authority,evidence_kind=a.evidence_kind,domain=a.domain,topics=a.topic,published_at=a.published_at,expected_revision=a.revision,parse=not a.no_parse)
    if cmd == 'parse':
        from personal_system.documents import parse_version
        return parse_version(r,a.version_id)
    if cmd in ('search','resolve','reindex','school-search','advisor-search'):
        from personal_system import search
        if cmd=='search': return search.search(r,a.query,domain=a.domain,kind=a.kind,authority=a.authority,official_only=a.official_only,topic=a.topic,topic_mode=a.topic_mode,history=a.history,include_archived=not a.active_only,evidence_kind=a.evidence_kind,limit=a.limit,offset=a.offset)
        if cmd=='resolve': return search.resolve(r,a.query,limit=a.limit)
        if cmd=='reindex': return search.rebuild_index(r)
        if cmd=='school-search': return search.school_search(r,a.query,topic=a.topic,topic_mode=a.topic_mode,official_only=not a.include_unofficial,limit=a.limit)
        return search.advisor_search(r,a.query,line=a.line,limit=a.limit)
    if cmd=='record-update': return core.update_record(r,a.table,a.record_id,payload(a.json),a.revision,source_ref=a.source,reason=a.reason)
    if cmd in ('bundle','bundle-restore','bundle-recover','bundle-list'):
        from personal_system import delivery
        if cmd=='bundle': return delivery.build(r,a.files,a.title)
        if cmd=='bundle-restore': return delivery.restore_bundle(r,a.bundle_id)
        if cmd=='bundle-list': return delivery.list_bundles(r)
        return delivery.recover_publication(r)
    if cmd in ('check','backup','restore'):
        from personal_system import recovery
        if cmd=='check': return recovery.check(r,full=a.full)
        if cmd=='backup': return recovery.backup(r,**({'destination':a.destination} if a.destination else {}))
        return recovery.restore_backup(a.backup_path,a.destination)
    con = core.connect(r,readonly=True)
    try:
        if cmd=='sql': return [dict(row) for row in con.execute(a.query,payload(a.params)).fetchall()]
        if cmd=='profile-get':
            row=con.execute('SELECT * FROM profile_fields WHERE field_key=?',(a.field_key,)).fetchone()
            return core.get(r,row['item_id']) if row else {'found':False,'field_key':a.field_key}
        if cmd=='history': return [dict(row) for row in con.execute('SELECT * FROM changes WHERE item_id=? ORDER BY revision DESC LIMIT ?',(a.item_id,a.limit))]
        if cmd=='list':
            conditions,params=[],[]
            for key in ('kind','domain'):
                if getattr(a,key): conditions.append(key+'=?');params.append(getattr(a,key))
            if a.active_only: conditions.append('archived_at IS NULL')
            where=' WHERE '+' AND '.join(conditions) if conditions else ''
            return [dict(row) for row in con.execute('SELECT * FROM items'+where+' ORDER BY updated_at DESC,item_id LIMIT ? OFFSET ?',[*params,a.limit,a.offset])]
        if cmd=='status':
            return {'root':str(core.root_path(r)),'sqlite':sqlite3.sqlite_version,'schema':[dict(row) for row in con.execute('SELECT * FROM v2_migrations')],
                    'counts':{name:con.execute('SELECT count(*) FROM '+name).fetchone()[0] for name in ('items','notes','documents','versions','fragments','search_entries','profile_fields')},
                    'journal_mode':con.execute('PRAGMA journal_mode').fetchone()[0]}
        raise ValueError(cmd)
    finally:
        con.close()


def main():
    if hasattr(sys.stdout,'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
    try:
        result=run(parser().parse_args())
        print(json.dumps(result,ensure_ascii=False,indent=2,default=str))
        return 1 if isinstance(result,dict) and result.get('ok') is False else 0
    except core.ConflictError as exc:
        print(json.dumps({'error':'revision_conflict','detail':str(exc),'action':'Read current state, merge, then retry with its revision.'},ensure_ascii=False),file=sys.stderr)
        return 3
    except Exception as exc:
        print(json.dumps({'error':type(exc).__name__,'detail':str(exc)},ensure_ascii=False),file=sys.stderr)
        return 1


if __name__=='__main__': raise SystemExit(main())
