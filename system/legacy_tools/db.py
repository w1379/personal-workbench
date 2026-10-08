#!/usr/bin/env python3
"""Shared SQLite connection and short-transaction helpers.

The knowledge base is intentionally one SQLite database.  Concurrent Codex
tasks may prepare work in parallel; SQLite serializes only their short commit
sections.  Read-only commands use URI read-only connections and never run
schema/configuration seeders.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Callable, TypeVar


BUSY_TIMEOUT_MS = 15_000
WRITE_ATTEMPTS = 6
_Result = TypeVar("_Result")


def connect_database(
    db_path: Path,
    *,
    readonly: bool = False,
    busy_timeout_ms: int = BUSY_TIMEOUT_MS,
) -> sqlite3.Connection:
    """Open a configured database connection.

    WAL is persistent and is enabled by every write-capable opener so new and
    restored databases automatically receive the concurrency policy.  A
    read-only opener fails if the database does not exist and is additionally
    protected with ``query_only``.
    """

    if not readonly:
        raise RuntimeError("Legacy tools are read-only in V2; use the shared V2 write API")
    resolved = db_path.resolve()
    timeout_seconds = max(busy_timeout_ms, 1) / 1000
    if readonly:
        if not resolved.is_file():
            raise FileNotFoundError(f"数据库不存在：{resolved}")
        connection = sqlite3.connect(
            f"{resolved.as_uri()}?mode=ro",
            uri=True,
            timeout=timeout_seconds,
        )
    else:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(resolved, timeout=timeout_seconds)

    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    connection.execute("PRAGMA foreign_keys = ON")
    if readonly:
        connection.execute("PRAGMA query_only = ON")
    else:
        journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(journal_mode).casefold() != "wal":
            connection.close()
            raise sqlite3.OperationalError(
                f"无法启用 SQLite WAL，当前 journal_mode={journal_mode}"
            )
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA wal_autocheckpoint = 1000")
    return connection


def is_busy_error(error: sqlite3.OperationalError) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    if code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
        return True
    message = str(error).casefold()
    return "database is locked" in message or "database table is locked" in message


def run_write_transaction(
    connection: sqlite3.Connection,
    operation: Callable[[sqlite3.Connection], _Result],
    *,
    attempts: int = WRITE_ATTEMPTS,
) -> _Result:
    """Run one short, retryable ``BEGIN IMMEDIATE`` transaction.

    The callback must contain database work only.  Network access, PDF parsing,
    OCR, hashing large batches, and archive writes belong before this helper.
    Callbacks must also be safe to retry; maintained importers use stable IDs
    and UPSERTs for that reason.
    """

    if attempts < 1:
        raise ValueError("attempts 必须至少为 1")
    if connection.in_transaction:
        raise sqlite3.ProgrammingError("短写事务开始前已有未提交事务")

    for attempt in range(attempts):
        try:
            connection.execute("BEGIN IMMEDIATE")
            result = operation(connection)
            connection.commit()
            return result
        except sqlite3.OperationalError as error:
            connection.rollback()
            if not is_busy_error(error) or attempt + 1 >= attempts:
                raise
            time.sleep(min(0.05 * (2**attempt), 1.0))
        except Exception:
            connection.rollback()
            raise
    raise AssertionError("unreachable")


def database_settings(connection: sqlite3.Connection) -> dict[str, int | str]:
    """Return the concurrency settings used by diagnostics and smoke tests."""

    return {
        "journal_mode": str(connection.execute("PRAGMA journal_mode").fetchone()[0]),
        "busy_timeout_ms": int(connection.execute("PRAGMA busy_timeout").fetchone()[0]),
        "query_only": int(connection.execute("PRAGMA query_only").fetchone()[0]),
        "foreign_keys": int(connection.execute("PRAGMA foreign_keys").fetchone()[0]),
    }
