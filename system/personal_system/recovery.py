"""Explicit full backups, safe new-directory restore, and read-only checks.

No scheduler and no external-project copying. Backups coordinate cooperative
workspace writes, then verify the entire managed file inventory before/after;
an uncoordinated edit causes failure rather than a false complete snapshot.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone

from .delivery import (FileLock, _root, _inside, _hash, _reparse, _copy_checked,
                       _write_json, _stamp, workspace_lock)


FORMAT = "personal-system-backup-v1"
MANIFEST = "backup-manifest.json"
_EXCLUDE_ROOTS = {".git", "cache", "backups"}
_EXCLUDE_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache"}


def _tables(connection):
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _read_db(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _excluded(relative):
    parts = relative.parts
    if not parts:
        return None
    if parts[0] in _EXCLUDE_ROOTS:
        return "repository history, cache, or backup output; not authoritative live data"
    if len(parts) >= 2 and parts[:2] == ("system", "runtime"):
        return "rebuildable official Python/SQLite runtime; install instructions are in system/docs"
    if any(part in _EXCLUDE_PARTS for part in parts):
        return "rebuildable Python or test cache"
    if relative.as_posix() in {"data/library.sqlite3", "data/library.sqlite3-wal", "data/library.sqlite3-shm"}:
        return "database is captured by SQLite online backup instead of filesystem copy"
    if parts[0] == "exports" and len(parts) > 1 and (parts[1].startswith(".publish-") or parts[1] == ".publish.lock"):
        return "transient publication lock/journal; completed history and current are retained"
    if parts[0] == "exports" and len(parts) > 2 and parts[1] == "history" and parts[2].startswith(".building-"):
        return "unfinished private delivery batch"
    return None


def _inventory(root):
    files, directories, excluded = {}, [], []

    def visit(directory):
        for path in sorted(directory.iterdir()):
            relative = path.relative_to(root)
            reason = _excluded(relative)
            if reason:
                excluded.append({"path": relative.as_posix(), "reason": reason})
                continue
            if path.is_symlink() or _reparse(path):
                excluded.append({"path": relative.as_posix(), "reason": "filesystem link recorded only; external content is not copied"})
                continue
            if path.is_dir():
                directories.append(relative.as_posix())
                visit(path)
            elif path.is_file():
                before = path.stat()
                sha = _hash(path)
                after = path.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise RuntimeError("A managed file changed during backup inventory")
                files[relative.as_posix()] = {"sha256": sha, "byte_size": after.st_size}
            else:
                raise ValueError("Unsupported managed filesystem object")

    visit(root)
    return files, directories, excluded


def _plain_destination(path):
    path = Path(path).absolute()
    if path == path.anchor or len(path.parts) < 2:
        raise ValueError("A filesystem root cannot be a restore destination")
    for ancestor in [path, *path.parents]:
        if ancestor.is_symlink() or _reparse(ancestor):
            raise ValueError("Backup/restore destination cannot traverse a filesystem link")
    if path.exists() and (not path.is_dir() or next(path.iterdir(), None) is not None):
        raise FileExistsError("Destination must be missing or an empty ordinary directory")
    return path.resolve()


def _publish_new_directory(stage, destination):
    # Recheck just before publication. rmdir cannot delete a concurrently added
    # file; it fails rather than silently overwriting anyone's work.
    _plain_destination(destination)
    if destination.exists():
        destination.rmdir()
    os.replace(stage, destination)


def _external_references(connection):
    if "locations" not in _tables(connection):
        return []
    return [{"location_id": row["location_id"], "item_id": row["item_id"], "path": row["path"],
             "backup_scope": "registration_only; external content not copied"}
            for row in connection.execute("SELECT location_id,item_id,path FROM locations WHERE is_external=1")]


def backup(root=None, **kwargs):
    """Create a complete local backup directory, or fail without publishing it.

    destination may select a new/empty directory. Excluded external projects are
    recorded, never traversed. _fault is reserved for failure-injection tests.
    """
    root = _root(root)
    database = _inside(root, root / "data/library.sqlite3")
    if not database.is_file():
        raise FileNotFoundError("The main database does not exist")
    backup_id = _stamp() + "-" + uuid.uuid4().hex[:12]
    default = root / "backups" / backup_id
    destination = _plain_destination(kwargs.get("destination", default))
    # A backup may be under the excluded backups directory, or outside root. It
    # cannot be inserted into data/workspaces/system and recursively back itself.
    if destination.is_relative_to(root) and not destination.is_relative_to(root / "backups"):
        raise ValueError("Internal backup destination must be below backups/")
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = destination.parent / (".backup-incomplete-" + backup_id)
    stage.mkdir()
    payload = stage / "payload"
    payload.mkdir()
    fault = kwargs.get("_fault")
    lock_timeout = kwargs.get("timeout", 30)
    try:
        with FileLock(_inside(root, root / "cache/locks/backup.lock"), lock_timeout), workspace_lock(root, lock_timeout):
            before, directories, excluded = _inventory(root)
            if fault:
                fault("after_inventory")
            target_db = payload / "data/library.sqlite3"
            target_db.parent.mkdir(parents=True, exist_ok=True)
            source_connection = _read_db(database)
            target_connection = sqlite3.connect(target_db)
            target_connection.row_factory = sqlite3.Row
            try:
                source_connection.backup(target_connection, pages=512, sleep=0.05)
                target_connection.commit()
                target_connection.execute("PRAGMA journal_mode=DELETE")
                external = _external_references(target_connection)
                integrity = target_connection.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    raise RuntimeError("Database snapshot did not pass integrity checking")
            finally:
                target_connection.close()
                source_connection.close()
            if fault:
                fault("after_database")
            for directory in directories:
                _inside(payload, payload / directory).mkdir(parents=True, exist_ok=True)
            for relative, expected in before.items():
                copied = _copy_checked(_inside(root, root / relative), _inside(payload, payload / relative))
                if copied != expected:
                    raise RuntimeError("A managed file changed since the database snapshot inventory")
            if fault:
                fault("after_copy")
            after, after_directories, _ = _inventory(root)
            if after != before or set(after_directories) != set(directories):
                raise RuntimeError("Managed files changed during backup; incomplete snapshot retained for diagnosis")
            manifest_files = {**before, "data/library.sqlite3": {"sha256": _hash(target_db), "byte_size": target_db.stat().st_size}}
            validation = check(payload, include_locations=True)
            if not validation["ok"]:
                raise RuntimeError("Backup snapshot has new integrity errors; it was not published")
            manifest = {
                "format": FORMAT, "backup_id": backup_id, "created_at": datetime.now(timezone.utc).isoformat(),
                "source_root": str(root), "files": manifest_files, "directories": directories,
                "exclusions": excluded, "external_references": external,
                "consistency": "online SQLite snapshot; cooperative workspace lock; before/copy/after content inventory agreement",
                "known_baseline_issues": validation["baseline_issues"],
                "warning": "A backup on the same local disk is not offsite disaster recovery.",
            }
            _write_json(stage / MANIFEST, manifest)
            (stage / "README.md").write_text(
                "# 个人信息系统完整备份\n\n用 system/pis.cmd restore <本备份目录> <新的空目录> 恢复。payload 保存数据库、受管原件、工作区、代码配置及交付。\n\n"
                "清单逐文件记录哈希并列出排除范围；外部项目仅保存登记，不复制其内容。"
                "SQLite 运行时可重建，按恢复后 system/docs 的官方安装说明配置。"
                "本机备份不等于异地灾备。\n", encoding="utf-8")
        if fault:
            fault("before_publish")
        _publish_new_directory(stage, destination)
    except BaseException:
        # Never present an incomplete backup as a completed one. Keep it clearly
        # named for diagnosis instead of deleting potential unique evidence.
        raise
    return {"backup_id": backup_id, "path": str(destination), "file_count": len(manifest_files),
            "byte_size": sum(entry["byte_size"] for entry in manifest_files.values()),
            "external_reference_count": len(external), "baseline_issue_count": len(validation["baseline_issues"]),
            "complete": True}


def _validated_manifest(backup_path):
    backup_path = Path(backup_path).resolve(strict=True)
    manifest = json.loads((backup_path / MANIFEST).read_text(encoding="utf-8"))
    if manifest.get("format") != FORMAT:
        raise ValueError("Unrecognized or incomplete backup")
    payload = _inside(backup_path, backup_path / "payload")
    expected = manifest["files"]
    for relative, record in expected.items():
        path = _inside(payload, payload / relative)
        if not path.is_file() or path.stat().st_size != record["byte_size"] or _hash(path) != record["sha256"]:
            raise ValueError("Backup payload has a missing or altered file")
    actual = set()
    for path in payload.rglob("*"):
        _inside(payload, path)
        if path.is_file():
            actual.add(path.relative_to(payload).as_posix())
    if actual != set(expected):
        raise ValueError("Backup payload contains unrecorded files")
    for relative in manifest.get("directories", []):
        _inside(payload, payload / relative)
    if "data/library.sqlite3" not in expected:
        raise ValueError("Backup has no database snapshot")
    return backup_path, payload, manifest


def restore_backup(backup_path, destination, **kwargs):
    """Restore a verified backup to a new or empty directory; never overwrite."""
    backup_path, payload, manifest = _validated_manifest(backup_path)
    destination = _plain_destination(destination)
    if destination.is_relative_to(backup_path) or backup_path.is_relative_to(destination):
        raise ValueError("Restore destination must be separate from the backup")
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = destination.parent / (".restore-incomplete-" + uuid.uuid4().hex)
    stage.mkdir()
    for relative in manifest.get("directories", []):
        _inside(stage, stage / relative).mkdir(parents=True, exist_ok=True)
    for relative, expected in manifest["files"].items():
        copied = _copy_checked(_inside(payload, payload / relative), _inside(stage, stage / relative))
        if copied != expected:
            raise ValueError("Backup changed during restore")
    validation = check(stage, include_locations=False)
    if not validation["ok"]:
        raise RuntimeError("Restored database or original files failed validation")
    if kwargs.get("_fault"):
        kwargs["_fault"]("before_publish")
    _publish_new_directory(stage, destination)
    _write_json(destination / "RESTORE_RECORD.json", {
        "backup_id": manifest["backup_id"], "restored_at": datetime.now(timezone.utc).isoformat(),
        "source_root": manifest["source_root"], "destination": str(destination),
        "external_content_restored": False, "runtime_reinstallation_may_be_required": True,
    })
    final = check(destination, include_locations=True)
    return {"destination": str(destination), "backup_id": manifest["backup_id"],
            "file_count": len(manifest["files"]), "ok": final["ok"],
            "baseline_issue_count": len(final["baseline_issues"]),
            "external_reference_count": len(manifest.get("external_references", [])), "check": final}


def _local_reference(root, value):
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    return _inside(root, path)


def check(root=None, **kwargs):
    """Read-only structural/hash check, separating documented old deficiencies.

    Existing baseline damage is acknowledged only if its actual stored bytes
    still match the migration audit; later corruption is a new error.
    """
    root = _root(root)
    database = _inside(root, root / "data/library.sqlite3")
    result = {"ok": True, "errors": [], "baseline_issues": [], "warnings": [], "counts": {}}
    if not database.is_file():
        result["ok"] = False
        result["errors"].append({"issue": "database_missing"})
        return result
    with contextlib.closing(_read_db(database)) as connection:
        integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        if integrity != ["ok"]:
            result["errors"].append({"issue": "database_integrity", "count": len(integrity)})
        foreign_keys = list(connection.execute("PRAGMA foreign_key_check"))
        if foreign_keys:
            result["errors"].append({"issue": "foreign_key_errors", "count": len(foreign_keys)})
        tables = _tables(connection)
        baseline = {}
        if "document_processing" in tables:
            for row in connection.execute("SELECT version_id,state,error FROM document_processing WHERE state IN ('legacy_raw_missing','legacy_raw_hash_mismatch')"):
                try:
                    details = json.loads(row["error"] or "{}")
                except (ValueError, TypeError):
                    details = {}
                baseline[row["version_id"]] = {"state": row["state"], "details": details}
        if "versions" in tables:
            rows = list(connection.execute("SELECT version_id,raw_path,content_sha256 FROM versions"))
            result["counts"]["versions_checked"] = len(rows)
            for row in rows:
                entry = {"version_id": row["version_id"]}
                old = baseline.get(row["version_id"], {})
                try:
                    original = _local_reference(root, row["raw_path"])
                    present = original.is_file()
                    actual = _hash(original) if present else None
                except (ValueError, OSError, TypeError):
                    result["errors"].append({**entry, "issue": "unsafe_or_unreadable_original_reference"})
                    continue
                if not present:
                    issue = {**entry, "issue": "raw_missing"}
                    target = "baseline_issues" if old.get("state") == "legacy_raw_missing" else "errors"
                    result[target].append(issue)
                elif actual != row["content_sha256"]:
                    issue = {**entry, "issue": "raw_hash_mismatch"}
                    known_hash = old.get("details", {}).get("actual_sha256")
                    target = "baseline_issues" if old.get("state") == "legacy_raw_hash_mismatch" and known_hash == actual else "errors"
                    result[target].append(issue)
        if "search_entries" in tables:
            entries = connection.execute("SELECT count(*) FROM search_entries").fetchone()[0]
            result["counts"]["search_entries"] = entries
            if "search_fts_docsize" in tables:
                indexed = connection.execute("SELECT count(*) FROM search_fts_docsize").fetchone()[0]
                if entries != indexed:
                    result["errors"].append({"issue": "fts_entry_count_mismatch", "entries": entries, "indexed": indexed})
        if "locations" in tables and kwargs.get("include_locations", True):
            missing = 0
            external = 0
            for row in connection.execute("SELECT location_id,path,is_external FROM locations WHERE is_current=1"):
                if row["is_external"]:
                    external += 1
                    continue
                try:
                    path = _local_reference(root, row["path"])
                    if not path.exists():
                        missing += 1
                except (ValueError, TypeError, OSError):
                    result["errors"].append({"issue": "unsafe_internal_location", "location_id": row["location_id"]})
            result["counts"]["external_locations_not_opened"] = external
            if missing:
                # Some legacy registrations deliberately refer to known missing
                # artifacts; the version-level audit above remains authoritative.
                result["warnings"].append({"issue": "missing_registered_locations", "count": missing})
        if "items" in tables:
            result["counts"]["items"] = connection.execute("SELECT count(*) FROM items").fetchone()[0]
    result["ok"] = not result["errors"]
    return result
