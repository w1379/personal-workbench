from pathlib import Path
import tempfile
import unittest

from personal_system import core


class TodoTests(unittest.TestCase):
    def test_personal_scope_lifecycle_and_undated_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            core.initialize(root)
            core.register(root, 'task', '旧任务', domain='school')
            core.register(root, 'note', '不是待办', domain='personal_todo')
            self.assertEqual(core.list_todos(root)['total'], 0)

            pending = core.register(root, 'task', '尚未补齐', domain='personal_todo')
            later = core.register(root, 'task', '较晚事项', domain='personal_todo')
            early = core.register(root, 'task', '较早事项', domain='personal_todo')
            for item, day in ((later, '2026-11-20'), (early, '2026-10-01')):
                core.set_state(root, item['item_id'], {'status': 'open', 'due_on': day},
                               item['revision'], source_ref='测试用户明确要求')
            listed = core.list_todos(root)['items']
            self.assertEqual([r['item_id'] for r in listed],
                             [early['item_id'], later['item_id'], pending['item_id']])
            self.assertEqual(listed[-1]['status'], 'needs_review')
            self.assertEqual(listed[0]['source_ref'], '测试用户明确要求')

            core.set_state(root, early['item_id'], {'status': 'completed', 'due_on': '2026-10-01'}, 2)
            core.set_state(root, later['item_id'], {'status': 'cancelled', 'due_on': '2026-11-20'}, 2)
            core.archive(root, pending['item_id'], 1)
            self.assertEqual(core.list_todos(root)['total'], 0)
            self.assertEqual(core.list_todos(root, include_closed=True)['total'], 3)
            with self.assertRaises(core.ConflictError):
                core.set_state(root, early['item_id'], {'status': 'open'}, 2)
            core.set_state(root, early['item_id'], {'status': 'open', 'due_on': None}, 3)
            self.assertEqual(core.list_todos(root)['items'][0]['state']['due_on'], None)


if __name__ == '__main__':
    unittest.main()
