import concurrent.futures
import argparse
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import inbox as wire
from personal_system import core, documents, inbox, recovery, search


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='pis-inbox-')
        self.base = Path(self.tmp.name)
        self.root = self.base / 'pis'
        core.initialize(self.root)
        self.server = self.base / 'server'
        wire.server_dirs(self.server)
        self.file = self.base / '实验结果.txt'
        self.file.write_text('实验输出：磁矩收敛。', encoding='utf-8')
        self.m = wire.draft()
        self.m.update(title='跨设备实验记录', summary='实验结果与参数已核验。',
                      project_hint='磁矩实验', source={'device': 'desktop-test',
                      'path': 'E:/实验', 'reference': '测试现场记录', 'occurred_at': '2026-10-02'},
                      facts=[{'text': '磁矩收敛', 'evidence': 'observed', 'source': '实验结果.txt'}],
                      attachments=[{'path': str(self.file), 'title': '实验结果原件'}])
        self.sid = self.m['submission_id']
        self.draft = self.base / 'draft.json'
        self.package = self.base / 'package.zip'
        self.make_package()

    def tearDown(self):
        self.tmp.cleanup()

    def make_package(self, output=None):
        self.draft.write_bytes(wire.encoded(self.m))
        return wire.pack(self.draft, output or self.package)

    def local_package(self):
        target = inbox.package_path(self.root, self.sid)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.package, target)
        return target

    def receive(self):
        with self.package.open('rb') as f:
            return wire.receive(self.server, f)

    def counts(self):
        c = core.connect(self.root, readonly=True)
        try:
            return {t: c.execute('SELECT count(*) FROM ' + t).fetchone()[0]
                    for t in ('items', 'notes', 'documents', 'versions', 'links')}
        finally:
            c.close()

    def test_deterministic_pack_preserves_unicode_metadata_and_hashes(self):
        other = self.base / 'again.zip'
        self.make_package(other)
        self.assertEqual(wire.digest(self.package), wire.digest(other))
        info = wire.inspect_package(self.package)
        a = info['manifest']['attachments'][0]
        self.assertEqual(a['original_name'], self.file.name)
        self.assertEqual(a['sha256'], wire.digest(self.file))

    def test_receive_retries_and_conflicting_identity(self):
        self.receive(); self.receive()
        self.assertEqual(len(wire.server_list(self.server)), 1)
        self.m['summary'] = '不同内容'
        self.package = self.base / 'changed.zip'
        self.make_package()
        with self.assertRaises(ValueError): self.receive()
        self.assertEqual(len(list((self.server / 'staging').iterdir())), 0)

    def test_interrupted_transfer_is_not_published(self):
        class Broken(io.BytesIO):
            def read(self, *args):
                if self.tell(): raise OSError('connection lost')
                return super().read(20)
        with self.assertRaises(OSError):
            wire.receive(self.server, Broken(self.package.read_bytes()))
        self.assertEqual(wire.server_list(self.server), [])

    def test_rejects_extra_traversal_member_and_corrupt_file(self):
        evil = self.base / 'evil.zip'
        shutil.copyfile(self.package, evil)
        with zipfile.ZipFile(evil, 'a') as z:
            z.writestr('../escape.txt', 'bad')
        with self.assertRaises(ValueError): wire.inspect_package(evil)
        bad = self.base / 'bad.zip'
        with zipfile.ZipFile(self.package) as src, zipfile.ZipFile(bad, 'w') as dst:
            for entry in src.infolist():
                dst.writestr(entry, src.read(entry) if entry.filename == 'manifest.json' else b'bad bytes')
        with self.assertRaises(ValueError): wire.inspect_package(bad)

    def test_import_is_searchable_linked_and_idempotent(self):
        self.local_package()
        project = core.register(self.root, 'project', '磁矩实验')
        ack = inbox.import_package(self.root, self.sid, links=[project['item_id']])
        counts = self.counts()
        again = inbox.import_package(self.root, self.sid)
        self.assertEqual(ack, again)
        self.assertEqual(counts, self.counts())
        self.assertEqual(counts['notes'], 1)
        self.assertEqual(counts['documents'], 2)
        self.assertGreater(search.search(self.root, '磁矩')['total'], 0)
        note = core.get(self.root, ack['note_id'])
        self.assertIn('desktop-test', note['note']['body'])
        self.assertEqual(len(note['links']), 3)
        self.assertEqual(ack['attachments'][0]['processing_state'], 'full_text_searchable')

    def test_failed_import_resumes_after_some_documents_were_saved(self):
        self.local_package()
        real_save = documents.save
        calls = []
        def fail_attachment(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2: raise OSError('disk interruption')
            return real_save(*args, **kwargs)
        with patch.object(documents, 'save', side_effect=fail_attachment):
            with self.assertRaises(OSError): inbox.import_package(self.root, self.sid)
        self.assertEqual(inbox.local_list(self.root)['packages'][0]['status'], 'failed')
        self.assertEqual(self.counts()['notes'], 0)
        ack = inbox.import_package(self.root, self.sid)
        self.assertEqual(ack['status'], 'imported')
        self.assertEqual(self.counts()['documents'], 2)

    def test_concurrent_imports_return_same_receipt(self):
        self.local_package()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: inbox.import_package(self.root, self.sid), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.counts()['notes'], 1)

    def test_correction_needs_review_and_never_overwrites_current_fact(self):
        self.m['kind'] = 'correction'
        self.package = self.base / 'correction.zip'; self.make_package(); self.local_package()
        profile = core.set_profile(self.root, 'test.value', 'original', status='user_confirmed')
        before = self.counts()
        result = inbox.import_package(self.root, self.sid)
        self.assertEqual(result['status'], 'needs_review')
        self.assertEqual(before, self.counts())
        ack = inbox.import_package(self.root, self.sid, reviewed=True)
        self.assertFalse(ack['correction_applied'])
        self.assertEqual(json.loads(core.get(self.root, profile['item_id'])['profile']['value_json']), 'original')

    def test_receipt_hides_imported_packet_and_is_repeatable(self):
        self.receive(); self.local_package()
        ack = inbox.import_package(self.root, self.sid)
        wire.acknowledge(self.server, ack); wire.acknowledge(self.server, ack)
        self.assertEqual(wire.server_list(self.server), [])
        self.assertEqual(wire.server_list(self.server, True)[0]['status'], 'imported')
        self.assertEqual(wire.receipt(self.server, self.sid), ack)
        changed = dict(ack, sha256='0' * 64)
        with self.assertRaises(ValueError): wire.acknowledge(self.server, changed)

    def test_invalid_link_fails_before_originals_or_notes_are_created(self):
        self.local_package()
        before = self.counts()
        with self.assertRaises(KeyError):
            inbox.import_package(self.root, self.sid, links=['project:missing'])
        self.assertEqual(before, self.counts())

    def test_download_failure_and_retry(self):
        self.receive()
        failed = [True]
        def fake_remote(host, root, action, *args, **kw):
            if action == 'list': return wire.server_list(self.server)
            if action == 'download':
                if failed[0]:
                    kw['output_file'].write(b'half transfer')
                    raise OSError('interrupted')
                kw['output_file'].write(self.package.read_bytes())
        with patch.object(wire, 'remote', side_effect=fake_remote):
            self.assertFalse(wire.fetch(inbox.packages(self.root))['ok'])
            self.assertFalse(inbox.package_path(self.root, self.sid).exists())
            failed[0] = False
            self.assertTrue(wire.fetch(inbox.packages(self.root))['ok'])
            self.assertTrue(wire.fetch(inbox.packages(self.root))['ok'])

    def test_backup_keeps_submission_and_import_ledger(self):
        self.local_package()
        inbox.import_package(self.root, self.sid)
        snapshot = recovery.backup(self.root)
        backup_dir = Path(snapshot.get('path') or snapshot.get('backup_path') or '')
        # Inspect the one completed backup independently of response field naming.
        backup_dir = next(p for p in (self.root / 'backups').iterdir() if not p.name.startswith('.'))
        self.assertTrue((backup_dir / 'payload/data/inbox/packages' / (self.sid + '.zip')).is_file())
        self.assertTrue(recovery.check(backup_dir / 'payload')['ok'])

    def test_selected_fetch_does_not_download_unselected_package(self):
        self.receive()
        other_id = 'f' * 32
        rows = wire.server_list(self.server) + [{'submission_id': other_id, 'status': 'submitted'}]
        downloaded = []
        def fake_remote(host, root, action, *args, **kw):
            if action == 'list': return rows
            downloaded.append(args[0])
            kw['output_file'].write(self.package.read_bytes())
        with patch.object(wire, 'remote', side_effect=fake_remote):
            result = wire.fetch(inbox.packages(self.root), submission_ids=[self.sid])
        self.assertTrue(result['ok'])
        self.assertEqual(downloaded, [self.sid])
        self.assertFalse(inbox.package_path(self.root, other_id).exists())

    def test_cli_requires_selection_and_explicit_import_approval(self):
        parser = argparse.ArgumentParser()
        inbox.add_parser(parser.add_subparsers(dest='command'))
        self.local_package()
        args = parser.parse_args(['inbox', 'import', self.sid])
        before = self.counts()
        self.assertEqual(inbox.run(self.root, args)['status'], 'needs_approval')
        self.assertEqual(before, self.counts())
        args = parser.parse_args(['inbox', 'import', self.sid, '--approved'])
        self.assertEqual(inbox.run(self.root, args)['status'], 'imported')
        with self.assertRaises(SystemExit), patch('sys.stderr', new_callable=io.StringIO):
            parser.parse_args(['inbox', 'fetch'])


if __name__ == '__main__':
    unittest.main()
