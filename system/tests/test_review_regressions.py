"""Real failure cases found by the independent implementation review."""
from pathlib import Path
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from personal_system import core, documents, recovery, search


class ReviewRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pis-reviewed-regression-")
        self.root = Path(self.temporary.name) / "root"
        core.initialize(self.root)
        self.source = self.root / "workspaces/fixture.txt"

    def tearDown(self):
        self.temporary.cleanup()

    def save(self, body, **kwargs):
        self.source.write_text(body, encoding="utf-8")
        return documents.save(self.root, self.source, **kwargs)

    def test_topic_survives_new_version_before_body_is_parsed(self):
        first = self.save("initial body", title="Topic document", topics=["topic-fixture"], parse=False)
        self.assertEqual(search.search(self.root, "Topic", topic=["topic-fixture"], history="current")["total"], 1)
        second = self.save("updated body", document_id=first["item_id"], expected_revision=1, parse=False)
        self.assertNotEqual(first["version_id"], second["version_id"])
        self.assertEqual(search.search(self.root, "Topic", topic=["topic-fixture"], history="current")["total"], 1)

    def test_return_to_prior_content_updates_current_version_location_and_index(self):
        first = self.save("firsttoken unique content", title="Versioned evidence")
        second = self.save("secondtoken unique content", document_id=first["item_id"], expected_revision=1)
        repeated = self.save("firsttoken unique content", document_id=first["item_id"], expected_revision=2)
        self.assertEqual(repeated["version_id"], first["version_id"])
        con = core.connect(self.root, readonly=True)
        try:
            versions = list(con.execute("SELECT version_id,raw_path FROM versions WHERE document_id=? AND is_current=1", (first["item_id"],)))
            self.assertEqual(len(versions), 1)
            self.assertEqual(versions[0]["version_id"], first["version_id"])
            locations = list(con.execute("SELECT path FROM locations WHERE item_id=? AND is_current=1 AND role='original'", (first["item_id"],)))
            self.assertEqual(len(locations), 1)
            self.assertEqual(locations[0]["path"], versions[0]["raw_path"])
            self.assertEqual(con.execute("SELECT revision FROM items WHERE item_id=?", (first["item_id"],)).fetchone()[0], 3)
        finally:
            con.close()
        self.assertEqual(search.search(self.root, "firsttoken", history="current")["total"], 1)
        self.assertEqual(search.search(self.root, "secondtoken", history="current")["total"], 0)
        self.assertEqual(search.search(self.root, "secondtoken", history="historical")["total"], 1)

    def test_return_to_prior_content_requires_current_revision(self):
        first = self.save("original version")
        second = self.save("updated version", document_id=first["item_id"], expected_revision=1)
        with self.assertRaises(core.ConflictError):
            self.save("original version", document_id=first["item_id"], expected_revision=1)
        con = core.connect(self.root, readonly=True)
        try:
            self.assertEqual(con.execute("SELECT version_id FROM versions WHERE document_id=? AND is_current=1", (first["item_id"],)).fetchone()[0], second["version_id"])
        finally:
            con.close()

    def test_failed_parse_does_not_destroy_migration_damage_baseline(self):
        record = self.save("preserved imperfect legacy bytes", parse=False)
        con = core.connect(self.root)
        try:
            con.execute("UPDATE versions SET content_sha256=? WHERE version_id=?", ("f" * 64, record["version_id"]))
            con.execute("UPDATE document_processing SET state=?,error=? WHERE version_id=?", (
                "legacy_raw_hash_mismatch", json.dumps({"actual_sha256": record["sha256"], "expected_sha256": "f" * 64}), record["version_id"]))
            con.commit()
        finally:
            con.close()
        before = recovery.check(self.root)
        documents.parse_version(self.root, record["version_id"])
        after = recovery.check(self.root)
        self.assertTrue(before["ok"])
        self.assertTrue(after["ok"], after)
        self.assertEqual(after["baseline_issues"], before["baseline_issues"])

    def test_readonly_connection_cannot_attach_create_or_disable_query_only(self):
        target = self.root / "unwanted-output.sqlite3"
        con = core.connect(self.root, readonly=True)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                con.execute("ATTACH DATABASE ? AS attached", (str(target),))
            self.assertFalse(target.exists())
            with self.assertRaises(sqlite3.DatabaseError):
                con.execute("PRAGMA query_only=OFF")
            self.assertEqual(con.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.DatabaseError):
                con.execute("DELETE FROM items")
        finally:
            con.close()

    def test_gb18030_without_bom_is_not_misread_as_utf16(self):
        self.source.write_bytes("中文记录".encode("gb18030"))
        chunks, warnings = documents.extract(self.source)
        self.assertEqual("".join(chunk[1] for chunk in chunks), "中文记录")
        self.assertFalse(warnings)

    def test_utf16_bom_remains_supported(self):
        self.source.write_bytes("中文记录".encode("utf-16"))
        chunks, warnings = documents.extract(self.source)
        self.assertEqual("".join(chunk[1] for chunk in chunks), "中文记录")

    def test_automatic_note_title_does_not_keep_old_fact_current(self):
        note = core.add_note(self.root, "oldfacttoken initial statement")
        core.update_note(self.root, note["item_id"], "newfacttoken corrected statement", 1)
        self.assertEqual(search.search(self.root, "oldfacttoken", history="current")["total"], 0)
        self.assertEqual(search.search(self.root, "oldfacttoken", history="historical")["total"], 1)
        self.assertEqual(search.search(self.root, "newfacttoken", history="current")["total"], 1)

    def test_manual_note_title_is_retained(self):
        note = core.add_note(self.root, "initial statement", title="A deliberate title")
        core.update_note(self.root, note["item_id"], "corrected statement", 1)
        self.assertEqual(core.get(self.root, note["item_id"])["title"], "A deliberate title")

    def test_profile_update_keeps_old_value_only_in_history_and_rejects_stale_revision(self):
        field = core.set_profile(self.root, "fixture_field", "oldprofiletoken", status="confirmed", verified_at="2026-09-24", source_ref="fixture:one")
        updated = core.set_profile(self.root, "fixture_field", "newprofiletoken", field["revision"], status="confirmed", verified_at="2026-09-24", source_ref="fixture:two")
        self.assertEqual(search.search(self.root, "oldprofiletoken", history="current")["total"], 0)
        self.assertEqual(search.search(self.root, "oldprofiletoken", history="historical")["total"], 1)
        self.assertEqual(search.search(self.root, "newprofiletoken", history="current")["total"], 1)
        with self.assertRaises(core.ConflictError):
            core.set_profile(self.root, "fixture_field", "stale update", field["revision"])
        self.assertEqual(core.get(self.root, field["item_id"])["revision"], updated["revision"])

    def test_state_update_keeps_old_value_only_in_history(self):
        item = core.register(self.root, "machine", "Example machine")
        first = core.set_state(self.root, item["item_id"], {"purpose": "oldstatetoken"}, 1)
        core.set_state(self.root, item["item_id"], {"purpose": "newstatetoken"}, first["revision"])
        self.assertEqual(search.search(self.root, "oldstatetoken", history="current")["total"], 0)
        self.assertEqual(search.search(self.root, "oldstatetoken", history="historical")["total"], 1)

    def test_reparse_preserves_deadline_evidence_reference(self):
        record = self.save("Deadline evidence")
        con = core.connect(self.root)
        try:
            fragment = con.execute("SELECT fragment_row_id FROM fragments WHERE version_id=?", (record["version_id"],)).fetchone()[0]
            con.execute("INSERT INTO deadlines(deadline_id,document_id,fragment_row_id,deadline_kind,raw_text,precision,action,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        ("fixture-deadline", record["item_id"], fragment, "application", "fixture evidence", "day", "example", "2026", "2026"))
            con.commit()
        finally:
            con.close()
        documents.parse_version(self.root, record["version_id"])
        con = core.connect(self.root, readonly=True)
        try:
            self.assertEqual(con.execute("SELECT fragment_row_id FROM deadlines WHERE deadline_id='fixture-deadline'").fetchone()[0], fragment)
        finally:
            con.close()

    def test_parser_failure_retains_previously_extracted_evidence(self):
        record = self.save("retainedextracttoken useful evidence")
        with patch.object(documents, "extract", side_effect=RuntimeError("injected parser failure")):
            documents.parse_version(self.root, record["version_id"])
        self.assertEqual(search.search(self.root, "retainedextracttoken", history="current")["total"], 1)

    def test_reparse_refuses_to_remove_referenced_legacy_anchor(self):
        record = self.save("Referenced legacy evidence")
        con = core.connect(self.root)
        try:
            original = dict(con.execute("SELECT * FROM fragments WHERE version_id=?", (record["version_id"],)).fetchone())
            con.execute("DELETE FROM fragments WHERE fragment_row_id=?", (original["fragment_row_id"],))
            original["fragment_row_id"] = "legacy-stable-evidence-anchor"
            columns = list(original)
            con.execute("INSERT INTO fragments(" + ",".join(columns) + ") VALUES(" + ",".join("?" for _ in columns) + ")", list(original.values()))
            con.execute("INSERT INTO deadlines(deadline_id,document_id,fragment_row_id,deadline_kind,raw_text,precision,action,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        ("legacy-deadline", record["item_id"], original["fragment_row_id"], "application", "legacy evidence", "day", "example", "2026", "2026"))
            con.commit()
        finally:
            con.close()
        outcome = documents.parse_version(self.root, record["version_id"])
        self.assertIn("Preserved existing extracted evidence", outcome["detail"])
        con = core.connect(self.root, readonly=True)
        try:
            self.assertEqual(con.execute("SELECT fragment_row_id FROM deadlines WHERE deadline_id='legacy-deadline'").fetchone()[0], "legacy-stable-evidence-anchor")
            self.assertEqual(con.execute("SELECT content FROM fragments WHERE fragment_row_id='legacy-stable-evidence-anchor'").fetchone()[0], original["content"])
        finally:
            con.close()

    def test_cli_failed_check_has_failure_exit_status(self):
        record = self.save("original integrity check fixture", parse=False)
        (self.root / record["path"]).write_text("changed after receipt", encoding="utf-8")
        cli = Path(core.__file__).resolve().parents[1] / "pis.py"
        result = subprocess.run([sys.executable, str(cli), "--root", str(self.root), "check"], capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertFalse(json.loads(result.stdout)["ok"])
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
