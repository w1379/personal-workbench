"""Meaningful retrieval regressions on disposable databases; no personal data."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from personal_system import core
from personal_system.search import _evidence_authority, rebuild_index, reindex_item, resolve, search


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        core.initialize(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def item(self, ident, text, *, title=None, kind="document", domain="school", authority="official", **kwargs):
        con = core.connect(self.root)
        try:
            def write(c):
                if not c.execute("SELECT 1 FROM items WHERE item_id=?", (ident,)).fetchone():
                    core.new_item(c, kind, title or ident, item_id=ident, domain=domain,
                                  authority=authority, evidence_kind="official_document")
                core.index_entry(c, ident, text, authority=authority, **kwargs)
            core.transaction(con, write)
        finally:
            con.close()

    def ids(self, query, **filters):
        return [r["item_id"] for r in search(self.root, query, **filters)["results"]]

    def test_two_character_terms_and_literal_fts_punctuation(self):
        self.item("doc", "校内免听政策包括人工智能课程，表达式 alpha-beta。")
        self.assertEqual(self.ids("免听"), ["doc"])
        self.assertEqual(self.ids("alpha-beta"), ["doc"])
        self.assertEqual(self.ids('" OR "'), [])

    def test_scopes_before_limit_and_no_assistant_authority_laundering(self):
        for n in range(25):
            self.item(f"job:{n:02}", "推免课程混合匹配", domain="career")
        self.item("policy", "推免课程申请办法", topics=["policy"])
        self.item("inference", "推免课程虚构结论", author_role="assistant", evidence_kind="assistant_analysis")
        result = search(self.root, "推免课程", domain="school", official_only=True, limit=1)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["results"][0]["item_id"], "policy")
        self.assertEqual(self.ids("推免课程", topic="policy"), ["policy"])
        self.assertEqual(_evidence_authority("official", "user_provided_official_copy"), "official")
        self.assertEqual(_evidence_authority("user", "user_provided_official_copy"), "user")
        self.assertEqual(_evidence_authority("official", "assistant_analysis"), "derived")

    def test_cross_fragment_match_same_version_only(self):
        self.item("combined", "firstterm", entry_key="a", version_id="v1")
        self.item("combined", "secondterm", entry_key="b", version_id="v1")
        self.item("wrong", "firstterm", entry_key="a", version_id="old", is_current=0)
        self.item("wrong", "secondterm", entry_key="b", version_id="new")
        result = search(self.root, "firstterm secondterm")
        self.assertEqual([r["item_id"] for r in result["results"]], ["combined"])
        self.assertEqual(result["results"][0]["match_reason"], "same-version cross-fragment")
        self.assertEqual({e["entry_key"] for e in result["results"][0]["matches"]}, {"a", "b"})

    def test_object_pagination_not_fragment_pagination(self):
        for n in range(30):
            self.item("large", "needle", entry_key=str(n), version_id="v1")
        for n in range(6):
            self.item(f"other:{n}", "needle")
        first = search(self.root, "needle", limit=3)
        second = search(self.root, "needle", limit=3, offset=first["next_offset"])
        last = search(self.root, "needle", limit=3, offset=second["next_offset"])
        all_ids = [r["item_id"] for result in (first, second, last) for r in result["results"]]
        self.assertEqual(len(all_ids), 7)
        self.assertEqual(len(set(all_ids)), 7)
        self.assertFalse(last["has_more"])
        self.assertIsNone(last["next_offset"])

    def test_current_history_archive_are_independent(self):
        self.item("versioned", "oldword", version_id="v1", entry_key="old", is_current=0)
        self.item("versioned", "newword", version_id="v2", entry_key="new")
        self.assertEqual(self.ids("oldword"), ["versioned"])
        self.assertEqual(self.ids("oldword", history="current"), [])
        self.assertEqual(self.ids("newword", history="historical"), [])
        c = core.connect(self.root)
        try:
            core.transaction(c, lambda db: db.execute("UPDATE items SET archived_at=? WHERE item_id='versioned'", (core.now(),)))
        finally:
            c.close()
        self.assertEqual(self.ids("newword", history="historical"), ["versioned"])
        self.assertEqual(self.ids("newword", include_archived=False), [])

    def test_ambiguous_aliases_and_actual_path_resolution(self):
        folder = self.root / "workspaces" / "simulation"
        folder.mkdir(parents=True)
        for ident in ("proj:A", "proj:B"):
            core.register(self.root, "project", ident, path=folder, aliases=["退磁项目"], item_id=ident)
        result = resolve(self.root, "退磁项目")
        self.assertTrue(result["ambiguous"])
        self.assertEqual(result["total"], 2)
        self.assertTrue(result["results"][0]["locations"][0]["exists"])
        self.assertEqual(resolve(self.root, "proj:A")["results"][0]["item_id"], "proj:A")
        self.assertEqual(len(self.ids("退磁项目")), 2)
        self.assertEqual(resolve(self.root, "workspaces/simulation")["total"], 2)

    def test_rebuild_native_index_and_subsequent_updates(self):
        note = core.add_note(self.root, "recordold", title="Diary")
        core.set_profile(self.root, "test_field", "profileold")
        obj = core.register(self.root, "machine", "Test machine")
        core.set_state(self.root, obj["item_id"], {"purpose": "statevalue"}, 1)
        c = core.connect(self.root)
        try:
            core.transaction(c, lambda db: db.execute("DELETE FROM search_entries"))
        finally:
            c.close()
        rebuild_index(self.root)
        for query in ("recordold", "profileold", "statevalue"):
            self.assertEqual(len(self.ids(query)), 1)
        core.update_note(self.root, note["item_id"], "recordnew", 1)
        core.set_profile(self.root, "test_field", "profilenew", 1)
        self.assertEqual(self.ids("profileold", history="current"), [])
        self.assertEqual(self.ids("recordold", history="current"), [])
        rebuild_index(self.root)
        self.assertEqual(self.ids("profileold", history="historical"), ["profile:test_field"])
        self.assertEqual(self.ids("recordold", history="historical"), [note["item_id"]])
        before = search(self.root, "new")["total"]
        rebuild_index(self.root)
        self.assertEqual(search(self.root, "new")["total"], before)

    def test_topic_descendants_aliases_and_all(self):
        c = core.connect(self.root)
        try:
            def write(db):
                for ident, name, parent in [("parent", "规则", None), ("child", "免听规则", "parent")]:
                    db.execute("INSERT INTO topics(topic_id,name,parent_topic_id,created_at,updated_at) VALUES(?,?,?,?,?)",
                               (ident, name, parent, core.now(), core.now()))
                db.execute("INSERT INTO topic_aliases(topic_id,alias,created_at) VALUES('parent','校规',?)", (core.now(),))
            core.transaction(c, write)
        finally:
            c.close()
        self.item("both", "needletopic", topics=["child", "cross"])
        self.item("single", "needletopic", topics=["child"])
        self.assertEqual(len(self.ids("needletopic", topic="校规")), 2)
        self.assertEqual(self.ids("needletopic", topics=["校规", "cross"], topic_mode="all"), ["both"])
        with self.assertRaises(ValueError):
            search(self.root, "needletopic", topic="does-not-exist")

    def test_rebuild_legacy_documents_preserves_versions_and_evidence(self):
        c = core.connect(self.root)
        try:
            def write(db):
                now = core.now()
                db.execute("""INSERT INTO sources(source_id,name,source_kind,authority,access_mode,created_at,updated_at)
                    VALUES('source','Test source','web','official','public',?,?)""", (now, now))
                db.execute("""INSERT INTO documents(document_id,source_id,document_kind,title,first_seen_at,last_seen_at,evidence_kind)
                    VALUES('legacy-doc','source','web_notice','School notice',?,?,'official_document')""", (now, now))
                for n, current in [(1, 0), (2, 1)]:
                    db.execute("""INSERT INTO versions(version_id,document_id,content_sha256,raw_path,media_type,captured_at,byte_size,is_current)
                        VALUES(?, 'legacy-doc', ?, ?, 'text/plain', ?, 10, ?)""",
                        (f"v{n}", f"hash{n}", f"data/originals/legacy-doc/v{n}/notice.txt", now, current))
                for ident, version, text, role, evidence in [("f1", "v1", "oldpolicyword", None, "official_document"),
                    ("f2", "v2", "currentpolicyword", None, "official_document"),
                    ("f3", "v2", "madeupword", "assistant", "assistant_analysis")]:
                    db.execute("""INSERT INTO fragments(fragment_row_id,fragment_id,version_id,document_id,sequence_no,fragment_kind,author_role,evidence_kind,content)
                        VALUES(?,?,?,'legacy-doc',?,'paragraph',?,?,?)""", (ident,ident,version,int(ident[-1]),role,evidence,text))
                # Migration metadata must remain untouched by index maintenance.
                core.new_item(db, "document", "School notice", domain="school", item_id="legacy-doc", metadata={"migration": "kept"})
            core.transaction(c, write)
        finally:
            c.close()
        summary = rebuild_index(self.root, batch_size=1)
        self.assertEqual(summary["state"], "complete")
        self.assertEqual(self.ids("oldpolicyword", official_only=True), ["legacy-doc"])
        self.assertEqual(self.ids("oldpolicyword", history="current"), [])
        self.assertEqual(self.ids("madeupword", official_only=True), [])
        hit = search(self.root, "currentpolicyword", official_only=True)["results"][0]["matches"][0]
        self.assertTrue(hit["source_ref"].startswith("data/originals/"))
        self.assertEqual(hit["version_id"], "v2")
        self.assertEqual(json.loads(core.get(self.root, "legacy-doc")["metadata_json"]), {"migration": "kept"})

    def test_route_identity_collision_and_incremental_dependent_reindex(self):
        self.seed_route_pair()
        rebuild_index(self.root)
        self.assertEqual(set(self.ids("oldroutekeyword")), {"same-id", "route:same-id"})
        self.assertEqual(core.get(self.root, "same-id")["kind"], "advisor")
        self.assertEqual(core.get(self.root, "route:same-id")["kind"], "admission_route")
        c = core.connect(self.root)
        try:
            def update(db):
                db.execute("UPDATE research_routes SET program_evidence_scope='newroutekeyword' WHERE route_id='same-id'")
                return reindex_item(db, "route:same-id")
            affected = core.transaction(c, update)
            self.assertEqual(set(affected), {"same-id", "route:same-id"})
            with self.assertRaises(Exception):
                reindex_item(c, "route:same-id")
        finally:
            c.close()
        self.assertEqual(self.ids("oldroutekeyword", history="current"), [])
        self.assertEqual(set(self.ids("newroutekeyword")), {"same-id", "route:same-id"})

    def seed_route_pair(self):
        c = core.connect(self.root)
        try:
            def seed(db):
                db.execute("""INSERT INTO research_advisors VALUES(
                    'same-id','Test advisor','Test University','College','[]','research','assessment',
                    'supervision','normal','[]','[]',?)""", (core.now(),))
                db.execute("""INSERT INTO research_routes VALUES(
                    'same-id',NULL,2027,'Test University','College','0702','Program','academic',
                    'ordinary','oldroutekeyword','unknown','unknown','','','','[]','[]',?)""", (core.now(),))
                db.execute("INSERT INTO research_advisor_routes VALUES('same-id','same-id','verified','example')")
            core.transaction(c, seed)
        finally:
            c.close()

    def test_core_business_update_and_index_are_one_atomic_transaction(self):
        self.seed_route_pair()
        rebuild_index(self.root)
        def partial_then_fail(c, ident):
            reindex_item(c, ident)
            raise RuntimeError("synthetic index failure after projection changes")
        with mock.patch("personal_system.search.reindex_item", side_effect=partial_then_fail):
            with self.assertRaises(RuntimeError):
                core.update_record(self.root, "research_routes", "same-id", {"program_evidence_scope": "failedword"}, 1)
        self.assertEqual(core.get(self.root, "route:same-id")["revision"], 1)
        self.assertEqual(self.ids("failedword"), [])
        self.assertEqual(set(self.ids("oldroutekeyword")), {"same-id", "route:same-id"})
        result = core.update_record(self.root, "research_routes", "same-id", {"program_evidence_scope": "newroutekeyword"}, 1)
        self.assertEqual(result["revision"], 2)
        self.assertEqual(self.ids("oldroutekeyword", history="current"), [])
        self.assertEqual(set(self.ids("newroutekeyword")), {"same-id", "route:same-id"})
        self.assertEqual(self.ids("oldroutekeyword", history="historical"), ["route:same-id"])
        with self.assertRaises(core.ConflictError):
            core.update_record(self.root, "research_routes", "same-id", {"program_evidence_scope": "staleoverwrite"}, 1)
        self.assertEqual(self.ids("staleoverwrite"), [])
        c = core.connect(self.root, readonly=True)
        try:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM changes").fetchone()[0], 1)
            self.assertIsNone(c.execute("SELECT 1 FROM current_state WHERE item_id='route:same-id'").fetchone())
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
