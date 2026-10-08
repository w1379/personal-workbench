import concurrent.futures
from pathlib import Path
import sqlite3
import tempfile
import unittest
from personal_system import core, documents, search


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name); core.initialize(self.root)
    def tearDown(self): self.tmp.cleanup()

    def test_note_receipt_and_revision_conflict(self):
        one=core.add_note(self.root,'今天记录永磁体思路',operation_id='receipt-one')
        again=core.add_note(self.root,'今天记录永磁体思路',operation_id='receipt-one')
        self.assertEqual(one['item_id'],again['item_id'])
        other=core.add_note(self.root,'今天记录永磁体思路')
        self.assertNotEqual(one['item_id'],other['item_id'])
        core.update_note(self.root,one['item_id'],'已采用有限元研究',1)
        with self.assertRaises(core.ConflictError): core.update_note(self.root,one['item_id'],'过期覆盖',1)
        self.assertEqual(core.get(self.root,one['item_id'])['note']['body'],'已采用有限元研究')
        hits=search.search(self.root,'永磁体',history='historical')
        self.assertIn(one['item_id'],[x['item_id'] for x in hits['results']])

    def test_profile_keeps_types_and_conflicts(self):
        result=core.set_profile(self.root,'test.code','0012300',status='user_confirmed',metadata={'unit':'text'})
        core.set_profile(self.root,'test.code','0099900',1)
        with self.assertRaises(core.ConflictError): core.set_profile(self.root,'test.code','broken',1)
        field=core.get(self.root,result['item_id'])['profile']
        self.assertEqual(field['value_json'],'"0099900"')
        self.assertIn('unit',field['metadata_json'])

    def test_readonly_cannot_write(self):
        con=core.connect(self.root,readonly=True)
        try:
            with self.assertRaises(sqlite3.OperationalError): con.execute("INSERT INTO items(item_id) VALUES('bad')")
        finally: con.close()

    def test_school_html_body_excludes_navigation(self):
        file=self.root/'notice.html'
        file.write_text('<html><title>公告</title><div>导航哨兵</div><div class="v_news_content">这是学校发布的正文，需要保存为可检索的公告内容。</div></html>',encoding='utf-8')
        chunks,warnings=documents.extract(file)
        self.assertIn('学校发布的正文',chunks[0][1])
        self.assertNotIn('导航哨兵',chunks[0][1])
        self.assertFalse(warnings)

    def test_embedded_html_body_remains_incomplete(self):
        for name,markup in [('pdf','<div pdfsrc="/notice.pdf"></div>'),('image','<img src="/notice.png">'),('empty','')]:
            with self.subTest(name=name):
                file=self.root/(name+'.html')
                file.write_text('<html><title>待提取公告</title><div class="wp_articlecontent">'+markup+'</div></html>',encoding='utf-8')
                result=documents.save(self.root,file)
                self.assertEqual(result['status'],'partial')

    def test_documents_versions_and_both_indexes(self):
        file=self.root/'input.md'; file.write_text('研究生报名资料\n磁矩计算方法',encoding='utf-8')
        one=documents.save(self.root,file,authority='official',domain='school',topics=['graduate'])
        self.assertEqual(one['status'],'full_text_searchable')
        original=self.root/one['path']
        file.write_text('研究生报名资料\n新版材料截止',encoding='utf-8')
        two=documents.save(self.root,file,document_id=one['item_id'],expected_revision=1,authority='official',domain='school')
        self.assertNotEqual(one['version_id'],two['version_id'])
        self.assertIn('磁矩',original.read_text(encoding='utf-8'))
        self.assertEqual(search.search(self.root,'磁矩',history='current')['total'],0)
        self.assertGreater(search.search(self.root,'磁矩',history='historical')['total'],0)
        c=core.connect(self.root,readonly=True)
        try:
            self.assertEqual(c.execute('SELECT count(*) FROM fragments').fetchone()[0],c.execute('SELECT count(*) FROM fragments_fts').fetchone()[0])
        finally: c.close()

    def test_unknown_binary_is_received_not_indexed_as_full(self):
        file=self.root/'simulation.mph'; file.write_bytes(b'opaque-model\x00')
        result=documents.save(self.root,file)
        self.assertEqual(result['status'],'pending')
        self.assertTrue((self.root/result['path']).is_file())
        self.assertGreater(search.search(self.root,'simulation')['total'],0)

    def test_concurrent_state_merges(self):
        item=core.register(self.root,'project','concurrency project')
        ident=item['item_id']; core.set_state(self.root,ident,{'counter':0},1)
        import json
        def worker(_):
            for i in range(15):
                for retry in range(100):
                    before=core.get(self.root,ident)
                    counter=json.loads(before['current_state']['state_json'])['counter']
                    try:
                        core.set_state(self.root,ident,{'counter':counter+1},before['revision'])
                        break
                    except core.ConflictError: pass
                else: self.fail('could not converge')
            return True
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            self.assertTrue(all(pool.map(worker,range(4))))
        after=core.get(self.root,ident)
        self.assertEqual(json.loads(after['current_state']['state_json'])['counter'],60)
        self.assertEqual(after['revision'],62)

    def test_state_cannot_create_parallel_authority_for_business_records(self):
        for kind in ('career_application', 'infrastructure_observation', 'event'):
            with self.subTest(kind=kind):
                item=core.register(self.root,kind,'Authoritative business record')
                with self.assertRaises(ValueError):
                    core.set_state(self.root,item['item_id'],{'status':'shadow copy'},1)
                self.assertEqual(core.get(self.root,item['item_id'])['revision'],1)


if __name__=='__main__': unittest.main()
