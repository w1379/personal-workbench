#!/usr/bin/env python3
"""Portable structured submissions over existing SSH. Python 3.10+, stdlib only.

This file runs unchanged on a sending computer and on the receiving file server.
It never opens the personal database. Local database ingestion lives separately.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone

DEFAULT_REMOTE_ROOT = '/srv/personal-workbench-inbox'
SCHEMA = 'pis-submission-v1'
KINDS = ('note', 'project_update', 'project_location', 'correction')
EVIDENCE = ('user_confirmed', 'observed', 'source_document', 'inferred')


def now():
    return datetime.now(timezone.utc).isoformat()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{32}', value):
        raise ValueError('submission_id must be a lowercase UUID hex (32 characters)')
    return value


def plain(path):
    path = Path(path)
    if path.is_symlink() or (getattr(path.lstat(), 'st_file_attributes', 0) & 0x400):
        raise ValueError(f'Symlinks/junctions are not package inputs: {path}')
    if not path.is_file():
        raise ValueError(f'Expected a regular file: {path}')
    return path


def required_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{label} must be nonempty text')


def validate_metadata(m, packaged=False):
    if not isinstance(m, dict) or m.get('schema') != SCHEMA:
        raise ValueError(f'Expected schema {SCHEMA}')
    identifier(m.get('submission_id'))
    for key in ('title', 'summary', 'created_at'):
        required_text(m.get(key), key)
    if datetime.fromisoformat(m['created_at']).tzinfo is None:
        raise ValueError('created_at must include a timezone')
    if m.get('kind') not in KINDS:
        raise ValueError(f'kind must be one of {KINDS}')
    source = m.get('source')
    if not isinstance(source, dict):
        raise ValueError('source must be an object')
    for key in ('device', 'reference'):
        required_text(source.get(key), 'source.' + key)
    for key in ('path', 'occurred_at'):
        if key in source and not isinstance(source[key], str):
            raise ValueError('source.' + key + ' must be text')
    if source.get('occurred_at'):
        datetime.fromisoformat(source['occurred_at'])
    for key in ('project_hint', 'domain'):
        if key in m and not isinstance(m[key], str):
            raise ValueError(key + ' must be text')
    if not isinstance(m.get('facts'), list):
        raise ValueError('facts must be a list, possibly empty')
    for fact in m['facts']:
        if not isinstance(fact, dict) or fact.get('evidence') not in EVIDENCE:
            raise ValueError(f'Each fact needs text, source and evidence in {EVIDENCE}')
        required_text(fact.get('text'), 'fact.text')
        required_text(fact.get('source'), 'fact.source')
    for key in ('decisions', 'open_questions'):
        if not isinstance(m.get(key), list) or any(not isinstance(x, str) for x in m[key]):
            raise ValueError(key + ' must be a list of text')
    attachments = m.get('attachments')
    if not isinstance(attachments, list) or len(attachments) > 999:
        raise ValueError('attachments must be a list with at most 999 files')
    names = set()
    for a in attachments:
        if not isinstance(a, dict):
            raise ValueError('Each attachment must be an object')
        required_text(a.get('path'), 'attachment.path')
        required_text(a.get('title'), 'attachment.title')
        if packaged:
            required_text(a.get('original_name'), 'attachment.original_name')
            if not re.fullmatch(r'files/[0-9]{4}-[A-Za-z0-9_.-]+', a['path']):
                raise ValueError('Invalid package member path')
            if a['path'] in names:
                raise ValueError('Duplicate attachment path')
            names.add(a['path'])
            if not re.fullmatch(r'[0-9a-f]{64}', a.get('sha256', '')):
                raise ValueError('Attachment hash missing')
            if type(a.get('size')) is not int or a['size'] < 0:
                raise ValueError('Attachment size missing')
    return m


def draft():
    return {'schema': SCHEMA, 'submission_id': uuid.uuid4().hex, 'created_at': now(),
            'kind': 'note', 'title': '', 'summary': '', 'domain': 'general',
            'project_hint': '', 'source': {'device': socket.gethostname(), 'path': '',
            'occurred_at': '', 'reference': ''}, 'facts': [], 'decisions': [],
            'open_questions': [], 'attachments': []}


def publish_file(temp, target):
    """Publish without replacing an existing identity; same bytes are a retry."""
    temp, target = Path(temp), Path(target)
    try:
        os.link(temp, target)
    except FileExistsError:
        plain(target)
        if digest(temp) != digest(target):
            raise ValueError(f'Identity already exists with different bytes: {target.name}')
    if os.name == 'posix':
        fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    # A killed process can leave a hidden temporary file, never a partial ready file.


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.partial')
    try:
        with temp.open('xb') as f:
            f.write(encoded(value)); f.flush(); os.fsync(f.fileno())
        publish_file(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def inspect_package(path):
    plain(path)
    with zipfile.ZipFile(path) as z:
        members = z.infolist()
        names = [x.filename for x in members]
        if len(names) != len(set(names)) or len(names) > 1000:
            raise ValueError('Duplicate or excessive ZIP members')
        for entry in members:
            if entry.compress_type != zipfile.ZIP_STORED or entry.flag_bits & 1:
                raise ValueError('Only unencrypted ZIP_STORED packages are accepted')
            if stat.S_ISLNK(entry.external_attr >> 16) or entry.is_dir():
                raise ValueError('Directories/symlinks are not allowed in packages')
        if z.getinfo('manifest.json').file_size > 1024 * 1024:
            raise ValueError('Manifest exceeds 1 MiB')
        m = validate_metadata(json.loads(z.read('manifest.json')), packaged=True)
        if set(names) != {'manifest.json', *(a['path'] for a in m['attachments'])}:
            raise ValueError('Package has missing or unlisted members')
        for a in m['attachments']:
            if z.getinfo(a['path']).file_size != a['size']:
                raise ValueError('Attachment size mismatch: ' + a['path'])
            h = hashlib.sha256()
            with z.open(a['path']) as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b''):
                    h.update(chunk)
            if h.hexdigest() != a['sha256']:
                raise ValueError('Attachment hash mismatch: ' + a['path'])
    return {'manifest': m, 'sha256': digest(path), 'size': Path(path).stat().st_size}


def pack(draft_path, output):
    draft_path, output = Path(draft_path), Path(output)
    m = validate_metadata(json.loads(draft_path.read_text(encoding='utf-8-sig')))
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name('.' + output.name + '.' + uuid.uuid4().hex + '.partial')
    try:
        with zipfile.ZipFile(temp, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as z:
            for i, a in enumerate(m['attachments']):
                source = Path(a['path'])
                if not source.is_absolute():
                    source = draft_path.parent / source
                plain(source)
                suffix = re.sub(r'[^A-Za-z0-9_.-]', '_', source.name)[-120:] or 'file'
                name = f'files/{i:04d}-{suffix}'
                before = digest(source)
                info = zipfile.ZipInfo(name)
                info.external_attr = 0o100660 << 16
                with source.open('rb') as src, z.open(info, 'w', force_zip64=True) as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
                if digest(source) != before:
                    raise ValueError('Attachment changed while packing: ' + str(source))
                a.update(path=name, original_name=source.name, sha256=before,
                         size=source.stat().st_size)
            z.writestr(zipfile.ZipInfo('manifest.json'), encoded(m))
        info = inspect_package(temp)
        with temp.open('r+b') as f:
            os.fsync(f.fileno())
        publish_file(temp, output)
        return {'package': str(output.resolve()), 'submission_id': m['submission_id'],
                'sha256': info['sha256'], 'status': 'packed'}
    finally:
        temp.unlink(missing_ok=True)


def server_dirs(root):
    root = Path(root)
    for name in ('staging', 'ready', 'receipts'):
        folder = root / name
        if folder.is_symlink():
            raise ValueError('Server directory must not be a symlink')
        folder.mkdir(parents=True, exist_ok=True)
    return root


def receive(root, stream):
    root = server_dirs(root)
    temp = root / 'staging' / (uuid.uuid4().hex + '.partial')
    try:
        with temp.open('xb') as f:
            shutil.copyfileobj(stream, f, 1024 * 1024)
            f.flush(); os.fsync(f.fileno())
        info = inspect_package(temp)
        sid = info['manifest']['submission_id']
        target = root / 'ready' / (sid + '.zip')
        publish_file(temp, target)
        return {'submission_id': sid, 'sha256': info['sha256'], 'status': 'submitted',
                'receipt': receipt(root, sid)}
    finally:
        temp.unlink(missing_ok=True)


def receipt(root, sid):
    path = Path(root) / 'receipts' / (identifier(sid) + '.json')
    return json.loads(plain(path).read_text(encoding='utf-8')) if path.exists() else None


def server_list(root, include_imported=False):
    rows = []
    for path in sorted((Path(root) / 'ready').glob('*.zip')):
        try:
            sid = identifier(path.stem)
            ack = receipt(root, sid)
            if ack and not include_imported:
                continue
            info = inspect_package(path)
            m = info['manifest']
            if m['submission_id'] != sid:
                raise ValueError('Filename and manifest identity differ')
            rows.append({'submission_id': sid, 'title': m['title'], 'kind': m['kind'],
                         'summary': m['summary'],
                         'attachments': [{'title': a['title'], 'name': a['original_name'], 'size': a['size']} for a in m['attachments']],
                         'device': m['source']['device'], 'sha256': info['sha256'],
                         'size': info['size'], 'status': 'imported' if ack else 'submitted'})
        except Exception as exc:
            rows.append({'file': path.name, 'status': 'invalid', 'error': str(exc)})
    return rows


def acknowledge(root, ack):
    sid = identifier(ack.get('submission_id'))
    if ack.get('status') != 'imported' or not ack.get('note_id'):
        raise ValueError('Only completed local imports may be acknowledged')
    if digest(plain(Path(root) / 'ready' / (sid + '.zip'))) != ack.get('sha256'):
        raise ValueError('Receipt does not match submitted package')
    atomic_json(Path(root) / 'receipts' / (sid + '.json'), ack)
    return {'submission_id': sid, 'status': 'acknowledged'}


def remote(host, remote_root, action, *args, input_file=None, output_file=None, data=None):
    if not host or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]*', host):
        raise ValueError('Use an SSH config alias or user@hostname')
    if not remote_root.startswith('/') or '\n' in remote_root:
        raise ValueError('remote-root must be an absolute POSIX path')
    script = remote_root.rstrip('/') + '/tools/inbox.py'
    command = shlex.join(['python3', script, 'server', '--root', remote_root, action, *args])
    call = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
            '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3', host, command]
    if input_file is None and data is None:
        call.insert(1, '-n')
    # Captured pipes can remain open in nested Windows SSH sessions after the
    # command has finished. File handles let us wait for the process itself.
    with tempfile.TemporaryFile() as response, tempfile.TemporaryFile() as errors:
        result = subprocess.run(call, stdin=input_file or (subprocess.DEVNULL if data is None else None), input=data,
                                stdout=output_file or response, stderr=errors, timeout=900)
        if result.returncode:
            errors.seek(0)
            raise RuntimeError(errors.read().decode('utf-8', errors='replace').strip() or 'SSH failed')
        if output_file is None:
            response.seek(0)
            return json.load(response)
        return None


def submit(package, host=None, remote_root=DEFAULT_REMOTE_ROOT):
    inspect_package(package)
    with Path(package).open('rb') as f:
        return remote(host, remote_root, 'receive', input_file=f)


def fetch(destination, host=None, remote_root=DEFAULT_REMOTE_ROOT, submission_ids=None):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    rows = remote(host, remote_root, 'list')
    if submission_ids is not None:
        selected = {identifier(sid) for sid in submission_ids}
        missing = selected - {row.get('submission_id') for row in rows}
        if missing:
            raise ValueError('Selected submissions are not pending: ' + ', '.join(sorted(missing)))
        rows = [row for row in rows if row.get('submission_id') in selected]
    results = []
    for row in rows:
        if row['status'] == 'invalid':
            results.append(row); continue
        sid = identifier(row['submission_id'])
        target = destination / (sid + '.zip')
        temp = destination / ('.' + sid + '.' + uuid.uuid4().hex + '.partial')
        try:
            if not target.exists():
                with temp.open('xb') as f:
                    remote(host, remote_root, 'download', sid, output_file=f)
                    f.flush(); os.fsync(f.fileno())
                info = inspect_package(temp)
                if info['sha256'] != row['sha256'] or info['manifest']['submission_id'] != sid:
                    raise ValueError('Downloaded package identity/hash mismatch')
                publish_file(temp, target)
            elif inspect_package(target)['sha256'] != row['sha256']:
                raise ValueError('Existing local package differs from server')
            results.append(dict(row, local_path=str(target.resolve()), status='fetched'))
        except Exception as exc:
            results.append(dict(row, status='failed', error=str(exc)))
        finally:
            temp.unlink(missing_ok=True)
    return {'ok': all(x['status'] == 'fetched' for x in results), 'packages': results}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    s = sub.add_parser('draft'); s.add_argument('output', type=Path)
    s = sub.add_parser('pack'); s.add_argument('draft', type=Path); s.add_argument('output', type=Path)
    s = sub.add_parser('inspect'); s.add_argument('package', type=Path)
    for name in ('submit', 'status'):
        s = sub.add_parser(name)
        s.add_argument('package' if name == 'submit' else 'submission_id')
        s.add_argument('--host', required=True); s.add_argument('--remote-root', default=DEFAULT_REMOTE_ROOT)
    s = sub.add_parser('server'); s.add_argument('--root', type=Path, default=Path(DEFAULT_REMOTE_ROOT))
    commands = s.add_subparsers(dest='action', required=True)
    for name in ('init', 'receive', 'ack'):
        commands.add_parser(name)
    t = commands.add_parser('list'); t.add_argument('--all', action='store_true')
    for name in ('download', 'receipt'):
        t = commands.add_parser(name); t.add_argument('submission_id')
    a = p.parse_args()
    try:
        if a.command == 'draft':
            atomic_json(a.output, draft()); result = {'draft': str(a.output.resolve())}
        elif a.command == 'pack': result = pack(a.draft, a.output)
        elif a.command == 'inspect': result = inspect_package(a.package)
        elif a.command == 'submit': result = submit(a.package, a.host, a.remote_root)
        elif a.command == 'status': result = remote(a.host, a.remote_root, 'receipt', identifier(a.submission_id))
        elif a.action == 'init': result = {'root': str(server_dirs(a.root)), 'status': 'ready'}
        elif a.action == 'receive': result = receive(a.root, sys.stdin.buffer)
        elif a.action == 'list': result = server_list(a.root, a.all)
        elif a.action == 'receipt': result = receipt(a.root, a.submission_id)
        elif a.action == 'ack':
            raw = sys.stdin.buffer.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024: raise ValueError('Receipt too large')
            result = acknowledge(a.root, json.loads(raw))
        elif a.action == 'download':
            with plain(a.root / 'ready' / (identifier(a.submission_id) + '.zip')).open('rb') as f:
                shutil.copyfileobj(f, sys.stdout.buffer, 1024 * 1024)
            return 0
        sys.stdout.buffer.write(encoded(result))
        return 1 if isinstance(result, dict) and result.get('ok') is False else 0
    except Exception as exc:
        sys.stderr.buffer.write(encoded({'error': type(exc).__name__, 'detail': str(exc)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
