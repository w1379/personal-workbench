"""Immutable delivery batches and recoverable publication of the flat basket.

The publish lock coordinates writers, not Explorer. Directory replacement on
Windows has a brief name transition; a journal makes interruption recoverable.
An existing current directory is never recursively removed before publication.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time
import uuid
import zipfile
from datetime import datetime, timezone


class FileLock:
    """OS-owned cross-process lock, released automatically on process death."""

    def __init__(self, path, timeout=30):
        self.path, self.timeout, self.handle = Path(path), timeout, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink() or _reparse(self.path):
            raise ValueError("Lock path must be an ordinary file")
        self.handle = self.path.open("a+b")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            self.handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self.handle.close()
                    self.handle = None
                    raise TimeoutError("Another operation holds the filesystem lock")
                time.sleep(0.05)

    def __exit__(self, *exc):
        if self.handle is not None:
            try:
                self.handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            finally:
                self.handle.close()
                self.handle = None


def _root(root=None):
    return Path(root if root is not None else Path(__file__).resolve().parents[2]).resolve()


def _reparse(path):
    try:
        return bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except (AttributeError, FileNotFoundError):
        return False


def _inside(root, path):
    """Reject escapes and junctions, including a symlink at the final path."""
    root, path = Path(root).resolve(), Path(path).absolute()
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise ValueError("Managed path escapes the target root") from None
    cursor = root
    for part in relative.parts:
        if part in (".", ".."):
            raise ValueError("Relative traversal is not allowed")
        cursor /= part
        if cursor.is_symlink() or _reparse(cursor):
            raise ValueError("Managed path cannot traverse a symlink or junction")
    if not path.resolve().is_relative_to(root):
        raise ValueError("Managed path resolves outside the target root")
    return path


def workspace_lock(root=None, timeout=30):
    root = _root(root)
    return FileLock(_inside(root, root / "cache/locks/workspaces.lock"), timeout)


def _hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def _name(value, fallback="资料"):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value)).strip().rstrip(". ")
    value = value[:110].rstrip(". ") or fallback
    if value.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{x}" for x in range(1, 10)], *[f"LPT{x}" for x in range(1, 10)]}:
        value = "_" + value
    return value


def _write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _copy_checked(source, destination):
    """Copy bytes and detect source changes, without exposing content in logs."""
    source, destination = Path(source), Path(destination)
    before = source.stat()
    if not source.is_file():
        raise ValueError("Delivery accepts regular source files only")
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with source.open("rb") as src, destination.open("xb") as dst:
        for block in iter(lambda: src.read(1024 * 1024), b""):
            dst.write(block)
            digest.update(block)
        dst.flush()
        os.fsync(dst.fileno())
    after = source.stat()
    sha = digest.hexdigest()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or _hash(source) != sha:
        raise RuntimeError("Source changed during copy; batch was not published")
    shutil.copystat(source, destination)
    return {"sha256": sha, "byte_size": after.st_size}


def _fingerprint(directory):
    result = {}
    for path in sorted(Path(directory).rglob("*")):
        if path.is_symlink() or _reparse(path):
            raise ValueError("Basket contains an unsupported filesystem link")
        if path.is_file():
            result[path.relative_to(directory).as_posix()] = _hash(path)
    return result


def _safe_discard(root, path, allowed_parent, prefix):
    path = _inside(root, path)
    if path.parent != allowed_parent or not path.name.startswith(prefix):
        raise ValueError("Refusing to remove a non-staging directory")
    if path.exists():
        # Verify all descendants before recursive deletion, including junctions.
        for child in path.rglob("*"):
            _inside(root, child)
        shutil.rmtree(path)


def _retire_previous(root, previous):
    """Discard an exact disposable copy; preserve unknown or edited contents."""
    previous = _inside(root, previous)
    if not previous.exists():
        return None
    if next(previous.iterdir(), None) is None:
        _safe_discard(root, previous, root / "exports", ".publish-rollback-")
        return None
    try:
        metadata = json.loads((previous / "01_本批资料清单.json").read_text(encoding="utf-8"))
        old_id = metadata["bundle_id"]
        if not re.fullmatch(r"[A-Za-z0-9._+-]+", old_id):
            raise ValueError("Invalid bundle identity")
        original = _history_payload(root, old_id)
        if original.is_dir() and _fingerprint(original) == _fingerprint(previous):
            _safe_discard(root, previous, root / "exports", ".publish-rollback-")
            return None
    except (OSError, ValueError, KeyError, TypeError):
        pass
    recovered = _inside(root, root / "exports/history" / ("recovered-current-" + _stamp() + "-" + uuid.uuid4().hex[:8]))
    os.replace(previous, recovered)
    return str(recovered)


def _recover_locked(root):
    exports = root / "exports"
    journal = _inside(root, exports / ".publish-journal.json")
    if not journal.exists():
        return []
    data = json.loads(journal.read_text(encoding="utf-8"))
    stage = _inside(root, exports / data["stage"])
    previous = _inside(root, exports / data["previous"])
    if stage.parent != exports or not stage.name.startswith(".publish-stage-"):
        raise ValueError("Invalid publication recovery stage")
    if previous.parent != exports or not previous.name.startswith(".publish-rollback-"):
        raise ValueError("Invalid publication recovery backup")
    current = _inside(root, exports / "current")
    preserved = []
    if not current.exists() and previous.exists():
        os.replace(previous, current)
    elif current.exists() and previous.exists():
        # A completed replacement or unusual crash still keeps its old batch.
        recovered = exports / "history" / ("recovered-current-" + _stamp() + "-" + uuid.uuid4().hex[:8])
        os.replace(previous, _inside(root, recovered))
        preserved.append(str(recovered))
    _safe_discard(root, stage, exports, ".publish-stage-")
    journal.unlink()
    return preserved


def recover_publication(root=None, *, timeout=30):
    root = _root(root)
    exports = _inside(root, root / "exports")
    exports.mkdir(parents=True, exist_ok=True)
    _inside(root, exports / "history").mkdir(exist_ok=True)
    with FileLock(_inside(root, exports / ".publish.lock"), timeout):
        preserved = _recover_locked(root)
    return {"recovered": True, "preserved_previous": preserved}


def _publish(root, history, *, timeout=30, fault=None):
    exports = root / "exports"
    transaction_id = uuid.uuid4().hex
    stage = _inside(root, exports / (".publish-stage-" + transaction_id))
    previous = _inside(root, exports / (".publish-rollback-" + transaction_id))
    current = _inside(root, exports / "current")
    journal = _inside(root, exports / ".publish-journal.json")
    shutil.copytree(history, stage)
    if _fingerprint(stage) != _fingerprint(history):
        raise RuntimeError("Staged basket differs from its immutable history batch")
    with FileLock(_inside(root, exports / ".publish.lock"), timeout):
        preserved = _recover_locked(root)
        _inside(root, current)
        _write_json(journal, {"stage": stage.name, "previous": previous.name, "bundle_id": history.name})
        try:
            if current.exists():
                if not current.is_dir():
                    raise ValueError("Current basket is not a directory")
                os.replace(current, previous)
            if fault:
                fault("after_old_rename")
            os.replace(stage, current)
            if fault:
                fault("after_publish")
            journal.unlink()
        except BaseException:
            # If old current was locked, it was never moved. If publication had
            # not completed, recovery restores it. Hard process death is dealt
            # with by recover_publication or the next publisher.
            with contextlib.suppress(Exception):
                preserved.extend(_recover_locked(root))
            raise
    # Hashing/deleting a verified disposable old copy is outside the short lock.
    try:
        retired = _retire_previous(root, previous)
        if retired:
            preserved.append(retired)
    except (OSError, ValueError):
        preserved.append(str(previous))
    return preserved


def build(root, paths, title, **kwargs):
    """Create and publish a batch. `paths` contains paths or path/name mappings."""
    root = _root(root)
    exports = _inside(root, root / "exports")
    history_root = _inside(root, exports / "history")
    history_root.mkdir(parents=True, exist_ok=True)
    title = str(title).strip() or "资料交付"
    bundle_id = _stamp() + "-" + uuid.uuid4().hex[:12]
    temporary = _inside(root, history_root / (".building-" + bundle_id))
    history = _inside(root, history_root / bundle_id)
    temporary.mkdir()
    records = []
    try:
        for ordinal, value in enumerate(paths, start=2):
            if ordinal >= 99:
                ordinal += 1  # 99 remains the recognisable convenience-ZIP slot.
            if isinstance(value, dict):
                source = Path(value.get("path", value.get("source", "")))
                label = value.get("name", source.name)
            else:
                source = Path(value)
                label = source.name
            if not source.is_absolute():
                source = root / source
            source = source.resolve(strict=True)
            filename = f"{ordinal:02d}_" + _name(label)
            info = _copy_checked(source, temporary / filename)
            records.append({"filename": filename, "source_path": str(source), **info})
        if not records:
            raise ValueError("A basket needs at least one source file")
        metadata = {"format": "personal-system-basket-v1", "bundle_id": bundle_id, "title": title,
                    "created_at": datetime.now(timezone.utc).isoformat(), "files": records}
        _write_json(temporary / "01_本批资料清单.json", metadata)
        (temporary / "00_请先读_本批资料说明.md").write_text(
            f"# {title}\n\n本目录是本次交付篮子，可以直接全选取走。\n\n"
            "编号文件是原件副本；清单保留来源和SHA-256。便利ZIP包含本批全部文件（不包含ZIP自身）。\n\n"
            f"批次：`{bundle_id}`。完整历史批次可恢复，源文件没有移动或改写。\n", encoding="utf-8")
        zip_name = "99_" + _name(title) + ".zip"
        with zipfile.ZipFile(temporary / zip_name, "x", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            for path in sorted(temporary.iterdir()):
                if path.name != zip_name:
                    archive.write(path, path.name)
        validate_bundle(temporary)
        os.replace(temporary, history)
    except BaseException:
        # Only our incomplete private stage is disposable; history is untouched.
        with contextlib.suppress(Exception):
            _safe_discard(root, temporary, history_root, ".building-")
        raise
    fault = kwargs.get("_fault")
    if fault:
        fault("after_history")
    preserved = _publish(root, history, timeout=kwargs.get("timeout", 30), fault=fault)
    return {"bundle_id": bundle_id, "title": title, "current": str(exports / "current"),
            "history": str(history), "zip": str(exports / "current" / zip_name),
            "file_count": len(records), "preserved_previous": preserved}


def validate_bundle(directory):
    directory = Path(directory)
    entries = list(directory.iterdir())
    if any(not p.is_file() or p.is_symlink() or _reparse(p) for p in entries):
        raise ValueError("A managed basket must contain only flat ordinary files")
    metadata = json.loads((directory / "01_本批资料清单.json").read_text(encoding="utf-8"))
    legacy = metadata.get("schema_version") == "1.0" and isinstance(metadata.get("items"), list)
    if metadata.get("format") != "personal-system-basket-v1" and not legacy:
        raise ValueError("Unrecognized basket manifest")
    records = metadata["items"] if legacy else metadata["files"]
    for record in records:
        name = record["destination"] if legacy else record["filename"]
        if Path(name).name != name or name in (".", ".."):
            raise ValueError("Invalid basket file name")
        path = directory / name
        if path.stat().st_size != record["byte_size"] or _hash(path) != record["sha256"]:
            raise ValueError("Basket file does not match its recorded hash")
    archive_name = (metadata.get("convenience_archive") or {}).get("filename") if legacy else None
    zip_files = [p for p in entries if p.name == archive_name] if archive_name else [p for p in entries if p.name.startswith("99_") and p.suffix.lower() == ".zip"]
    if legacy and not zip_files and not archive_name:
        # The earliest retained legacy batches predate the convenience ZIP.
        # Restoring those bytes faithfully must not silently manufacture one.
        return {"bundle_id": metadata["bundle_id"], "file_count": len(records), "valid": True, "legacy": True, "zip": "not present in historical batch"}
    if len(zip_files) != 1:
        raise ValueError("Expected exactly one themed convenience ZIP")
    zipped = zip_files[0]
    expected = {p.name for p in entries if p != zipped}
    with zipfile.ZipFile(zipped) as archive:
        if len(archive.namelist()) != len(expected) or set(archive.namelist()) != expected or archive.testzip():
            raise ValueError("Convenience ZIP is incomplete or corrupt")
        for name in expected:
            with archive.open(name) as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != _hash(directory / name):
                raise ValueError("ZIP member differs from the flat basket")
    return {"bundle_id": metadata["bundle_id"], "file_count": len(records), "valid": True, "legacy": legacy}


def _history_payload(root, bundle_id):
    candidates = [root / "exports/history" / bundle_id,
                  root / "workspaces/imported/exports/history" / bundle_id]
    for candidate in candidates:
        candidate = _inside(root, candidate)
        if candidate.is_dir():
            return _inside(root, candidate / "files") if (candidate / "files").is_dir() else candidate
    raise FileNotFoundError("Historical batch is not available in this system")


def list_bundles(root=None):
    root = _root(root)
    result = []
    seen = set()
    for parent, legacy in [(root / "exports/history", False),
                           (root / "workspaces/imported/exports/history", True)]:
        parent = _inside(root, parent)
        if not parent.exists():
            continue
        for candidate in sorted(parent.iterdir()):
            _inside(root, candidate)
            if not candidate.is_dir() or candidate.name.startswith((".", "recovered-current-")) or candidate.name in seen:
                continue
            payload = _history_payload(root, candidate.name)
            manifest_path = payload / "01_本批资料清单.json"
            if manifest_path.is_file():
                metadata = json.loads(manifest_path.read_text(encoding="utf-8"))
                result.append({"bundle_id": candidate.name, "title": metadata.get("title", ""), "legacy": legacy, "path": str(payload)})
                seen.add(candidate.name)
    return result


def restore_bundle(root, bundle_id, **kwargs):
    root = _root(root)
    if not re.fullmatch(r"[A-Za-z0-9._+-]+", str(bundle_id)) or str(bundle_id).startswith("."):
        raise ValueError("Invalid historical bundle ID")
    history = _history_payload(root, str(bundle_id))
    validate_bundle(history)
    preserved = _publish(root, history, timeout=kwargs.get("timeout", 30), fault=kwargs.get("_fault"))
    return {"bundle_id": bundle_id, "current": str(root / "exports/current"), "preserved_previous": preserved}
