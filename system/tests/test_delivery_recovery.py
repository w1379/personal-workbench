from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile

from personal_system import core, delivery, recovery


class DeliveryRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pis-delivery-test-")
        self.base = Path(self.temporary.name)
        self.root = self.base / "system-root"
        core.initialize(self.root)
        self.source = self.root / "workspaces/example.txt"
        self.source.write_text("a retained original\n", encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def run_python(self, code, *args, wait=True):
        process = subprocess.Popen([sys.executable, "-c", code, *map(str, args)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if not wait:
            return process
        stdout, stderr = process.communicate(timeout=60)
        self.assertEqual(process.returncode, 0, stderr)
        return stdout

    def fixture_version(self, suffix="one", *, missing=False, mismatch=False):
        original = self.root / f"data/originals/{suffix}/v1/evidence.txt"
        original.parent.mkdir(parents=True, exist_ok=True)
        if not missing:
            original.write_text("original bytes " + suffix, encoding="utf-8")
        actual = delivery._hash(original) if not missing else None
        expected = "f" * 64 if missing or mismatch else actual
        con = core.connect(self.root)
        try:
            con.execute("INSERT OR IGNORE INTO sources(source_id,name,source_kind,authority,access_mode,created_at,updated_at) VALUES('fixture','Fixture','file','user','local','2026','2026')")
            con.execute("INSERT INTO documents(document_id,source_id,document_kind,title,first_seen_at,last_seen_at,evidence_kind) VALUES(?,?,?,?,?,?,?)",
                        (suffix, "fixture", "file", "Test evidence", "2026", "2026", "user_statement"))
            con.execute("INSERT INTO versions(version_id,document_id,content_sha256,raw_path,media_type,captured_at,byte_size) VALUES(?,?,?,?,?,?,?)",
                        ("v:" + suffix, suffix, expected, original.relative_to(self.root).as_posix(), "text/plain", "2026", original.stat().st_size if not missing else 0))
            if missing or mismatch:
                con.execute("INSERT INTO document_processing VALUES(?,?,?,?,?)", ("v:" + suffix,
                    "legacy_raw_missing" if missing else "legacy_raw_hash_mismatch", json.dumps({"actual_sha256": actual, "expected_sha256": expected}), "migration", "2026"))
            con.commit()
        finally:
            con.close()
        return original

    def test_flat_zip_source_untouched_and_restore(self):
        before = self.source.read_bytes()
        first = delivery.build(self.root, [self.source], "第一批资料")
        self.assertEqual(self.source.read_bytes(), before)
        self.assertTrue(delivery.validate_bundle(self.root / "exports/current")["valid"])
        self.assertTrue(all(path.is_file() for path in (self.root / "exports/current").iterdir()))
        self.source.write_text("updated working copy", encoding="utf-8")
        second = delivery.build(self.root, [self.source], "第二批资料")
        self.assertNotEqual(first["bundle_id"], second["bundle_id"])
        delivery.restore_bundle(self.root, first["bundle_id"])
        self.assertEqual((self.root / "exports/current/02_example.txt").read_bytes(), before)
        self.assertTrue(delivery.validate_bundle(Path(second["history"]))["valid"])

    def test_current_manual_additions_are_preserved(self):
        delivery.build(self.root, [self.source], "First")
        (self.root / "exports/current/user-added.txt").write_text("keep this", encoding="utf-8")
        result = delivery.build(self.root, [self.source], "Second")
        self.assertEqual(len(result["preserved_previous"]), 1)
        self.assertTrue((Path(result["preserved_previous"][0]) / "user-added.txt").is_file())

    def test_fault_restores_previous_current(self):
        first = delivery.build(self.root, [self.source], "Before interruption")
        def fail(phase):
            if phase == "after_old_rename":
                raise RuntimeError("injected interruption")
        with self.assertRaises(RuntimeError):
            delivery.build(self.root, [self.source], "Incomplete publication", _fault=fail)
        self.assertEqual(delivery.validate_bundle(self.root / "exports/current")["bundle_id"], first["bundle_id"])
        self.assertFalse((self.root / "exports/.publish-journal.json").exists())

    def test_process_death_releases_lock_and_recovers(self):
        first = delivery.build(self.root, [self.source], "Before process death")
        code = """import os,sys
from personal_system.delivery import build
def fail(phase):
    if phase == 'after_old_rename': os._exit(91)
build(sys.argv[1], [sys.argv[2]], 'Interrupted', _fault=fail)
"""
        child = subprocess.run([sys.executable, "-c", code, str(self.root), str(self.source)], capture_output=True, text=True, timeout=30)
        self.assertEqual(child.returncode, 91, child.stderr)
        self.assertTrue((self.root / "exports/.publish-journal.json").exists())
        delivery.recover_publication(self.root)
        self.assertEqual(delivery.validate_bundle(self.root / "exports/current")["bundle_id"], first["bundle_id"])

    def test_concurrent_publishers_keep_every_batch(self):
        code = """import sys
from personal_system.delivery import build
for n in range(3): build(sys.argv[1], [sys.argv[2]], sys.argv[3]+str(n))
"""
        children = [self.run_python(code, self.root, self.source, f"Worker{worker}", wait=False) for worker in range(2)]
        for child in children:
            stdout, stderr = child.communicate(timeout=60)
            self.assertEqual(child.returncode, 0, stderr)
        history = [p for p in (self.root / "exports/history").iterdir() if p.name[0].isdigit()]
        self.assertEqual(len(history), 6)
        for batch in history:
            self.assertTrue(delivery.validate_bundle(batch)["valid"])
        self.assertTrue(delivery.validate_bundle(self.root / "exports/current")["valid"])

    @unittest.skipUnless(os.name == "nt", "Windows file sharing semantics")
    def test_windows_locked_current_leaves_previous_usable(self):
        first = delivery.build(self.root, [self.source], "Locked directory")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p]
        kernel.CreateFileW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.CreateFileW(str(self.root / "exports/current"), 0x80000000, 1, None, 3, 0x02000000, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        try:
            with self.assertRaises(OSError):
                delivery.build(self.root, [self.source], "Must not overwrite")
            self.assertEqual(delivery.validate_bundle(self.root / "exports/current")["bundle_id"], first["bundle_id"])
        finally:
            kernel.CloseHandle(handle)
        delivery.recover_publication(self.root)

    def test_no_path_traversal_or_history_overwrite(self):
        with self.assertRaises(ValueError):
            delivery.restore_bundle(self.root, "../../outside")
        with self.assertRaises(ValueError):
            delivery._inside(self.root, self.root / "exports/../../outside")

    def test_legacy_bundle_restores_original_bytes_without_old_root(self):
        bundle_id = "20260924T185040+0800-legacy-test"
        files = self.root / "workspaces/imported/exports/history" / bundle_id / "files"
        files.mkdir(parents=True)
        (files / "02_original.txt").write_text("retained historical bytes", encoding="utf-8")
        metadata = {"schema_version": "1.0", "bundle_id": bundle_id, "title": "Legacy batch",
                    "convenience_archive": {"filename": "99_legacy.zip"}, "items": [
                        {"destination": "02_original.txt", "sha256": delivery._hash(files / "02_original.txt"),
                         "byte_size": (files / "02_original.txt").stat().st_size}]}
        delivery._write_json(files / "01_本批资料清单.json", metadata)
        (files / "00_请先读_本批资料说明.md").write_text("Historical instructions", encoding="utf-8")
        with zipfile.ZipFile(files / "99_legacy.zip", "x") as archive:
            for path in files.iterdir():
                if path.suffix != ".zip":
                    archive.write(path, path.name)
        expected = delivery._fingerprint(files)
        result = delivery.restore_bundle(self.root, bundle_id)
        self.assertEqual(delivery._fingerprint(self.root / "exports/current"), expected)
        self.assertEqual(delivery.list_bundles(self.root)[0]["bundle_id"], bundle_id)
        delivery.build(self.root, [self.source], "A new batch")
        self.assertEqual(delivery._fingerprint(files), expected)

    def test_full_backup_and_restore_at_new_location(self):
        self.fixture_version()
        note = core.add_note(self.root, "A searchable remembered decision")
        core.register(self.root, "project", "external example", path=str(self.base / "outside-project"))
        (self.root / "system").mkdir(exist_ok=True)
        (self.root / "system/config.json").write_text('{"mode":"test"}', encoding="utf-8")
        (self.root / "system/runtime").mkdir()
        (self.root / "system/runtime/rebuildable.bin").write_bytes(b"runtime")
        delivery.build(self.root, [self.source], "Saved delivery")
        result = recovery.backup(self.root)
        destination = self.base / "restored-new-location"
        restored = recovery.restore_backup(result["path"], destination)
        self.assertTrue(restored["ok"], restored)
        self.assertEqual((destination / "workspaces/example.txt").read_bytes(), self.source.read_bytes())
        self.assertTrue((destination / "system/config.json").is_file())
        self.assertFalse((destination / "system/runtime/rebuildable.bin").exists())
        self.assertEqual(restored["external_reference_count"], 1)
        self.assertTrue(delivery.validate_bundle(destination / "exports/current")["valid"])
        con = core.connect(destination, readonly=True)
        try:
            self.assertEqual(con.execute("SELECT count(*) FROM notes").fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT count(*) FROM search_fts WHERE search_fts MATCH 'remembered'").fetchone()[0], 1)
        finally:
            con.close()

    def test_backup_rejects_uncoordinated_workspace_change(self):
        def change(phase):
            if phase == "after_database":
                self.source.write_text("changed during backup", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            recovery.backup(self.root, _fault=change)
        self.assertFalse(any((p / recovery.MANIFEST).exists() for p in (self.root / "backups").iterdir()))

    def test_online_database_snapshot_is_consistent_with_later_writes(self):
        core.add_note(self.root, "Before snapshot")
        def write_later(phase):
            if phase == "after_database":
                core.add_note(self.root, "After snapshot")
        result = recovery.backup(self.root, _fault=write_later)
        destination = self.base / "snapshot-restore"
        recovery.restore_backup(result["path"], destination)
        con = core.connect(destination, readonly=True)
        try:
            self.assertEqual(con.execute("SELECT count(*) FROM notes").fetchone()[0], 1)
        finally:
            con.close()

    def test_restore_refuses_existing_content_and_corrupt_payload(self):
        result = recovery.backup(self.root)
        destination = self.base / "occupied"
        destination.mkdir()
        keep = destination / "keep.txt"
        keep.write_text("do not modify", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            recovery.restore_backup(result["path"], destination)
        self.assertEqual(keep.read_text(), "do not modify")
        (Path(result["path"]) / "payload/workspaces/example.txt").write_text("tampered", encoding="utf-8")
        with self.assertRaises(ValueError):
            recovery.restore_backup(result["path"], self.base / "must-not-publish")
        self.assertFalse((self.base / "must-not-publish").exists())

    def test_baseline_defects_are_distinguished_from_new_damage(self):
        path = self.fixture_version("mismatch", mismatch=True)
        self.fixture_version("missing", missing=True)
        result = recovery.check(self.root)
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(result["baseline_issues"]), 2)
        path.write_text("newly corrupted bytes", encoding="utf-8")
        result = recovery.check(self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["baseline_issues"]), 1)
        self.assertEqual(result["errors"][0]["issue"], "raw_hash_mismatch")

    def test_backup_known_baseline_defects_survive_restore_honestly(self):
        self.fixture_version("mismatch", mismatch=True)
        self.fixture_version("missing", missing=True)
        result = recovery.backup(self.root)
        restored = recovery.restore_backup(result["path"], self.base / "restored-with-baseline")
        self.assertTrue(restored["ok"], restored)
        self.assertEqual(restored["baseline_issue_count"], 2)


if __name__ == "__main__":
    unittest.main()
