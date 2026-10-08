"""End-to-end fictional workflows for an independent, empty installation."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import inbox as wire

SYSTEM = Path(__file__).resolve().parents[1]


class FreshInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='workbench-')
        self.base = Path(self.temp.name)
        self.root = self.base / '空工作台 with spaces'
        self.foreign = self.base / 'unrelated directory'
        self.foreign.mkdir()
        self.cli('init')

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, *args, root=None, script=None, expected=0):
        command = [sys.executable, '-X', 'utf8', str(script or SYSTEM / 'pis.py'),
                   '--root', str(root or self.root), *map(str, args)]
        result = subprocess.run(command, cwd=self.foreign, capture_output=True,
                                text=True, encoding='utf-8', timeout=60)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(result.stdout if expected == 0 else result.stderr)

    def test_empty_root_and_optional_queries_work_from_foreign_directory(self):
        self.assertTrue(all(v == 0 for v in self.cli('status')['counts'].values()))
        self.assertEqual(self.cli('todos')['total'], 0)
        self.assertEqual(self.cli('school-search', 'research'), [])
        self.assertEqual(self.cli('advisor-search', 'physics'), [])
        self.assertTrue(self.cli('check')['ok'])
        self.assertFalse((self.foreign / 'data').exists())

    def test_records_document_delivery_and_standalone_restored_code(self):
        note = self.cli('note', '选择二维模型作为研究起点', '--source', 'fictional test')
        self.assertEqual(self.cli('search', '模型')['results'][0]['item_id'], note['item_id'])
        value = self.base / 'value.json'
        value.write_text(json.dumps('demo@example.org'), encoding='utf-8')
        self.cli('profile-set', 'contact.email', '--json', '@' + str(value),
                 '--status', 'user_confirmed', '--source', 'fictional test')
        self.assertEqual(json.loads(self.cli('profile-get', 'contact.email')['profile']['value_json']),
                         'demo@example.org')
        conflict = self.cli('profile-set', 'contact.email', '--json', '@' + str(value),
                            '--revision', '99', expected=3)
        self.assertEqual(conflict['error'], 'revision_conflict')
        task = self.cli('workspace', 'Research draft')
        workspace = self.root / task['locations'][0]['path']
        source = workspace / 'notice.txt'
        shutil.copyfile(SYSTEM.parent / 'examples/fictional-notice.txt', source)
        saved = self.cli('save', source, '--title', 'Fictional notice', '--domain', 'school')
        self.assertEqual(saved['status'], 'full_text_searchable')
        self.assertEqual(self.cli('search', '科研 工作坊')['results'][0]['item_id'], saved['item_id'])
        self.cli('relate', saved['item_id'], task['item_id'])
        batch = self.cli('bundle', '--title', 'Fictional delivery', source)
        self.assertTrue(list(Path(batch['current']).glob('*.zip')))
        self.assertTrue(self.cli('check')['ok'])
        # A real full backup carries its own code, but never an installed runtime.
        shutil.copytree(SYSTEM, self.root / 'system',
                        ignore=shutil.ignore_patterns('runtime', '__pycache__'))
        backup = self.cli('backup')
        restored = self.base / 'restored'
        result = self.cli('restore', backup['path'], restored)
        self.assertTrue(result['ok'])
        restored_cli = restored / 'system/pis.py'
        self.assertEqual(self.cli('search', '科研 工作坊', root=restored, script=restored_cli)['total'], 1)
        self.assertEqual(self.cli('get', note['item_id'], root=restored, script=restored_cli)['note']['body'],
                         '选择二维模型作为研究起点')
        self.assertEqual(self.cli('school-search', 'research', root=restored, script=restored_cli), [])
        self.assertFalse((restored / 'system/runtime').exists())
        self.assertFalse((self.foreign / 'data').exists())

    def test_remote_host_is_explicit_and_missing_ack_host_cannot_import(self):
        with patch.object(wire.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'SSH config'):
                wire.remote(None, wire.DEFAULT_REMOTE_ROOT, 'list')
            run.assert_not_called()
        result = self.cli('inbox', 'import', 'a' * 32, '--approved', '--ack', expected=1)
        self.assertIn('--host is required', result['detail'])
        self.assertTrue(all(v == 0 for v in self.cli('status')['counts'].values()))


if __name__ == '__main__':
    unittest.main()
