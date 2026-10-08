from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from db import connect_database, database_settings, run_write_transaction


PROJECT_ROOT = Path(__file__).resolve().parents[2]
KB_ROOT = PROJECT_ROOT / "system/legacy_kb"
DEFAULT_DB = PROJECT_ROOT / "data/library.sqlite3"
DEFAULT_SCHEMA = KB_ROOT / "schema" / "schema.sql"
DEFAULT_SOURCES = KB_ROOT / "config" / "sources.json"
DEFAULT_ENDPOINTS = KB_ROOT / "config" / "endpoints.json"
DEFAULT_BENCHMARKS = KB_ROOT / "config" / "benchmarks.json"
DEFAULT_TOPICS = KB_ROOT / "config" / "topics.json"
DEFAULT_CHAT_TOPICS = KB_ROOT / "config" / "chat_topics.json"
DEFAULT_CHAT_DIR = PROJECT_ROOT / "Original-Discussions"
DEFAULT_BACKUPS = KB_ROOT / "database" / "backups"

MESSAGE_HEADING_RE = re.compile(r"^##\s+(\d+)\.\s+(你|ChatGPT)\s*$", re.MULTILINE)
MESSAGE_ID_RE = re.compile(r"<!--\s*chatgpt-message-id:\s*([^\s]+)\s*-->")


@dataclass(frozen=True)
class ChatFragment:
    fragment_id: str
    sequence_no: int
    heading: str
    author_role: str
    content: str
    line_start: int
    line_end: int
    message_id: str | None


@dataclass(frozen=True)
class ChatDocument:
    document_id: str
    conversation_id: str
    title: str
    source_path: str
    published_at: str | None
    source_updated_at: str | None
    metadata: dict
    raw_text: str
    raw_bytes: bytes
    fragments: list[ChatFragment]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def project_relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def connect(db_path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    return connect_database(db_path, readonly=readonly)


def load_topic_config() -> list[dict[str, Any]]:
    topics = json.loads(DEFAULT_TOPICS.read_text(encoding="utf-8"))
    if not isinstance(topics, list):
        raise ValueError("topics.json 顶层必须是数组")
    topic_ids = {topic["topic_id"] for topic in topics}
    if len(topic_ids) != len(topics):
        raise ValueError("topics.json 存在重复 topic_id")

    lookup_owners: dict[str, str] = {}
    for topic in topics:
        topic_id = topic["topic_id"]
        parent = topic.get("parent_topic_id")
        if parent and parent not in topic_ids:
            raise ValueError(f"主题 {topic_id} 的父主题不存在：{parent}")
        for value in [topic_id, topic["name"], *topic.get("aliases", [])]:
            normalized = value.strip().casefold()
            owner = lookup_owners.get(normalized)
            if owner and owner != topic_id:
                raise ValueError(
                    f"主题名称或别名 {value!r} 同时指向 {owner} 和 {topic_id}"
                )
            lookup_owners[normalized] = topic_id
    return topics


def seed_topics(connection: sqlite3.Connection, now: str) -> None:
    topics = load_topic_config()
    for topic in topics:
        connection.execute(
            """
            INSERT INTO topics(
                topic_id, name, parent_topic_id, description, status, priority,
                metadata_json, created_at, updated_at
            ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(topic_id) DO UPDATE SET
                name = excluded.name,
                description = excluded.description,
                status = excluded.status,
                priority = excluded.priority,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                topic["topic_id"],
                topic["name"],
                topic.get("description"),
                topic.get("status", "active"),
                int(topic.get("priority", 0)),
                json.dumps(
                    {"managed_by": "config", **topic.get("metadata", {})},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                now,
                now,
            ),
        )
    for topic in topics:
        connection.execute(
            "UPDATE topics SET parent_topic_id = ?, updated_at = ? WHERE topic_id = ?",
            (topic.get("parent_topic_id"), now, topic["topic_id"]),
        )

    connection.execute("DELETE FROM topic_aliases WHERE managed_by = 'config'")
    connection.execute("DELETE FROM topic_sources WHERE managed_by = 'config'")
    for topic in topics:
        topic_id = topic["topic_id"]
        for alias in topic.get("aliases", []):
            connection.execute(
                """
                INSERT INTO topic_aliases(
                    topic_id, alias, alias_kind, managed_by, created_at
                ) VALUES (?, ?, 'synonym', 'config', ?)
                """,
                (topic_id, alias.strip(), now),
            )
        for source in topic.get("sources", []):
            source_id = source["source_id"]
            source_row = connection.execute(
                "SELECT 1 FROM sources WHERE source_id = ?", (source_id,)
            ).fetchone()
            if not source_row:
                raise ValueError(f"主题 {topic_id} 引用了未登记来源：{source_id}")
            endpoint_id = source.get("endpoint_id")
            if endpoint_id:
                endpoint = connection.execute(
                    "SELECT source_id FROM source_endpoints WHERE endpoint_id = ?",
                    (endpoint_id,),
                ).fetchone()
                if not endpoint:
                    raise ValueError(f"主题 {topic_id} 引用了未登记入口：{endpoint_id}")
                if endpoint["source_id"] != source_id:
                    raise ValueError(
                        f"主题 {topic_id} 的入口 {endpoint_id} 不属于来源 {source_id}"
                    )
            topic_source_id = (
                f"topic-source:{topic_id}:{source_id}:{endpoint_id or 'all'}"
            )
            connection.execute(
                """
                INSERT INTO topic_sources(
                    topic_source_id, topic_id, source_id, endpoint_id,
                    coverage_role, priority, required, managed_by, notes,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'config', ?, ?, ?)
                """,
                (
                    topic_source_id,
                    topic_id,
                    source_id,
                    endpoint_id,
                    source.get("coverage_role", "relevant"),
                    int(source.get("priority", 0)),
                    int(bool(source.get("required", False))),
                    source.get("notes"),
                    now,
                    now,
                ),
            )


def topic_descendants(
    connection: sqlite3.Connection, topic_id: str
) -> list[str]:
    rows = connection.execute(
        "SELECT topic_id, parent_topic_id FROM topics WHERE status = 'active'"
    ).fetchall()
    children: dict[str, list[str]] = {}
    for row in rows:
        if row["parent_topic_id"]:
            children.setdefault(row["parent_topic_id"], []).append(row["topic_id"])
    result: list[str] = []
    pending = [topic_id]
    seen: set[str] = set()
    while pending:
        current = pending.pop(0)
        if current in seen:
            continue
        seen.add(current)
        result.append(current)
        pending.extend(sorted(children.get(current, [])))
    return result


def resolve_topic_id(connection: sqlite3.Connection, value: str) -> str:
    candidate = value.strip()
    row = connection.execute(
        """
        SELECT topic_id FROM topics
        WHERE topic_id = ? COLLATE NOCASE OR name = ? COLLATE NOCASE
        ORDER BY CASE WHEN topic_id = ? COLLATE NOCASE THEN 0 ELSE 1 END
        LIMIT 1
        """,
        (candidate, candidate, candidate),
    ).fetchone()
    if not row:
        row = connection.execute(
            "SELECT topic_id FROM topic_aliases WHERE alias = ? COLLATE NOCASE",
            (candidate,),
        ).fetchone()
    if not row:
        raise ValueError(f"未知主题：{value}；可用 kb.py topics 查看主题")
    return row["topic_id"]


def resolve_topic_groups(
    connection: sqlite3.Connection, values: Sequence[str] | None
) -> tuple[list[list[str]], list[str]]:
    groups: list[list[str]] = []
    roots: list[str] = []
    for value in values or []:
        topic_id = resolve_topic_id(connection, value)
        if topic_id in roots:
            continue
        roots.append(topic_id)
        groups.append(topic_descendants(connection, topic_id))
    return groups, roots


def document_scope_conditions(
    topic_groups: Sequence[Sequence[str]],
    topic_mode: str,
    official_only: bool,
    *,
    document_alias: str = "d",
    source_alias: str = "s",
) -> tuple[list[str], list[str]]:
    conditions: list[str] = []
    parameters: list[str] = []
    if official_only:
        conditions.append(f"{source_alias}.authority = 'official'")
    if topic_groups:
        groups = topic_groups if topic_mode == "all" else [
            sorted({topic_id for group in topic_groups for topic_id in group})
        ]
        for group in groups:
            placeholders = ", ".join("?" for _ in group)
            conditions.append(
                "EXISTS (SELECT 1 FROM document_topics dt "
                f"WHERE dt.document_id = {document_alias}.document_id "
                f"AND dt.topic_id IN ({placeholders}))"
            )
            parameters.extend(group)
    return conditions, parameters


def document_topic_names(
    connection: sqlite3.Connection, document_ids: Iterable[str]
) -> dict[str, list[str]]:
    ids = list(dict.fromkeys(document_ids))
    if not ids:
        return {}
    placeholders = ", ".join("?" for _ in ids)
    rows = connection.execute(
        f"""
        SELECT dt.document_id, t.name
        FROM document_topics dt JOIN topics t USING(topic_id)
        WHERE dt.document_id IN ({placeholders})
        ORDER BY t.priority DESC, t.topic_id
        """,
        ids,
    ).fetchall()
    result: dict[str, list[str]] = {}
    for row in rows:
        result.setdefault(row["document_id"], []).append(row["name"])
    return result


def set_document_topics(
    connection: sqlite3.Connection,
    document_id: str,
    topic_ids: Iterable[str],
    *,
    assignment_method: str,
    inherited_from_document_id: str | None = None,
    notes: str | None = None,
) -> None:
    normalized = list(dict.fromkeys(topic_ids))
    missing = [
        topic_id
        for topic_id in normalized
        if not connection.execute(
            "SELECT 1 FROM topics WHERE topic_id = ?", (topic_id,)
        ).fetchone()
    ]
    if missing:
        raise ValueError(
            f"文档 {document_id} 引用了未知主题：{', '.join(missing)}"
        )
    connection.execute(
        """
        DELETE FROM document_topics
        WHERE document_id = ? AND assignment_method IN ('configured', 'inherited')
        """,
        (document_id,),
    )
    now = utc_now()
    for topic_id in normalized:
        connection.execute(
            """
            INSERT INTO document_topics(
                document_id, topic_id, topic_role, assignment_method,
                confidence, inherited_from_document_id, notes, created_at, updated_at
            ) VALUES (?, ?, 'related', ?, 1.0, ?, ?, ?, ?)
            ON CONFLICT(document_id, topic_id) DO UPDATE SET
                topic_role = excluded.topic_role,
                assignment_method = excluded.assignment_method,
                confidence = excluded.confidence,
                inherited_from_document_id = excluded.inherited_from_document_id,
                notes = excluded.notes,
                updated_at = excluded.updated_at
            """,
            (
                document_id,
                topic_id,
                assignment_method,
                inherited_from_document_id,
                notes,
                now,
                now,
            ),
        )


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(DEFAULT_SCHEMA.read_text(encoding="utf-8"))
    fragment_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(fragments)").fetchall()
    }
    if "evidence_kind" not in fragment_columns:
        connection.execute(
            "ALTER TABLE fragments ADD COLUMN evidence_kind TEXT NOT NULL "
            "DEFAULT 'assistant_analysis'"
        )
        connection.execute(
            "UPDATE fragments SET evidence_kind = 'user_statement' "
            "WHERE author_role = 'user'"
        )
    now = utc_now()
    sources = json.loads(DEFAULT_SOURCES.read_text(encoding="utf-8"))
    for source in sources:
        connection.execute(
            """
            INSERT INTO sources(
                source_id, name, source_kind, base_url, authority, access_mode,
                scope, priority, enabled, notes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                name = excluded.name,
                source_kind = excluded.source_kind,
                base_url = excluded.base_url,
                authority = excluded.authority,
                access_mode = excluded.access_mode,
                scope = excluded.scope,
                priority = excluded.priority,
                enabled = excluded.enabled,
                notes = excluded.notes,
                updated_at = excluded.updated_at
            """,
            (
                source["source_id"],
                source["name"],
                source["source_kind"],
                source.get("base_url"),
                source["authority"],
                source["access_mode"],
                source.get("scope"),
                int(source.get("priority", 0)),
                int(bool(source.get("enabled", True))),
                source.get("notes"),
                now,
                now,
            ),
        )
    endpoints = json.loads(DEFAULT_ENDPOINTS.read_text(encoding="utf-8"))
    for endpoint in endpoints:
        connection.execute(
            """
            INSERT INTO source_endpoints(
                endpoint_id, source_id, name, endpoint_kind, url, access_mode,
                priority, enabled, expected_markers_json, notes, created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(endpoint_id) DO UPDATE SET
                source_id = excluded.source_id,
                name = excluded.name,
                endpoint_kind = excluded.endpoint_kind,
                url = excluded.url,
                access_mode = excluded.access_mode,
                priority = excluded.priority,
                enabled = excluded.enabled,
                expected_markers_json = excluded.expected_markers_json,
                notes = excluded.notes,
                updated_at = excluded.updated_at
            """,
            (
                endpoint["endpoint_id"],
                endpoint["source_id"],
                endpoint["name"],
                endpoint["endpoint_kind"],
                endpoint["url"],
                endpoint["access_mode"],
                int(endpoint.get("priority", 0)),
                int(bool(endpoint.get("enabled", True))),
                json.dumps(
                    endpoint.get("expected_markers", []),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                endpoint.get("notes"),
                now,
                now,
            ),
        )
    seed_topics(connection, now)
    connection.commit()


def first_group(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text, re.MULTILINE)
    return match.group(1).strip() if match else None


def clean_message_body(block: str) -> str:
    body = re.sub(r"<!--.*?-->\s*", "", block, flags=re.DOTALL)
    body = re.sub(r"^>\s*时间：.*$", "", body, flags=re.MULTILINE)
    body = re.sub(r"^::chatgpt-content-reference\{.*?\}\s*$", "", body, flags=re.MULTILINE)
    body = body.strip()
    body = re.sub(r"\n?---\s*$", "", body).strip()
    return body


def parse_chat(path: Path) -> ChatDocument:
    raw_bytes = path.read_bytes()
    raw_text = raw_bytes.decode("utf-8-sig")
    title = first_group(r"^#\s+(.+)$", raw_text) or path.stem
    exported_title = title
    if title.strip().casefold() in {"new chat", "新聊天"}:
        filename_title = re.sub(r"^ChatGPT[_ -]*", "", path.stem, flags=re.IGNORECASE).strip()
        if filename_title:
            title = filename_title
    conversation_id = first_group(r"^>\s*对话 ID：\s*(.+)$", raw_text)
    if not conversation_id:
        raise ValueError(f"缺少对话 ID：{path}")

    published_at = first_group(r"^>\s*对话创建时间：\s*(.+?)（", raw_text)
    source_updated_at = first_group(r"^>\s*对话最后更新时间：\s*(.+?)（", raw_text)
    if not source_updated_at:
        source_updated_at = first_group(r"^>\s*最后更新：\s*(.+)$", raw_text)
    export_date = first_group(r"^>\s*导出日期：\s*(.+)$", raw_text)
    declared_count = first_group(r"^>\s*共\s*(\d+)\s*条消息", raw_text)
    model_note = first_group(r"^>\s*模型说明：\s*(.+)$", raw_text)
    attachment_line = first_group(r"^>\s*返回附件文件名：\s*(.+)$", raw_text)
    attachment_names = re.findall(r"`([^`]+)`", attachment_line or "")

    matches = list(MESSAGE_HEADING_RE.finditer(raw_text))
    fragments: list[ChatFragment] = []
    for index, match in enumerate(matches):
        block_start = match.end()
        block_end = matches[index + 1].start() if index + 1 < len(matches) else len(raw_text)
        block = raw_text[block_start:block_end]
        message_match = MESSAGE_ID_RE.search(block)
        message_id = message_match.group(1) if message_match else None
        role_label = match.group(2)
        role = "user" if role_label == "你" else "assistant"
        sequence_no = int(match.group(1))
        if message_id:
            fragment_id = f"msg:{message_id}"
        else:
            fallback = hashlib.sha256(
                f"{conversation_id}:{sequence_no}:{role}".encode("utf-8")
            ).hexdigest()[:24]
            fragment_id = f"chat-fragment:{fallback}"
        line_start = raw_text.count("\n", 0, match.start()) + 1
        line_end = raw_text.count("\n", 0, block_end) + (0 if block_end == len(raw_text) else 0)
        content = clean_message_body(block)
        fragments.append(
            ChatFragment(
                fragment_id=fragment_id,
                sequence_no=sequence_no,
                heading=f"消息 {sequence_no} · {role_label}",
                author_role=role,
                content=content,
                line_start=line_start,
                line_end=max(line_start, line_end),
                message_id=message_id,
            )
        )

    if declared_count and int(declared_count) != len(fragments):
        raise ValueError(
            f"声明 {declared_count} 条消息，但解析到 {len(fragments)} 条：{path.name}"
        )
    if not fragments:
        raise ValueError(f"没有解析到消息：{path}")

    metadata = {
        "exported_title": exported_title,
        "export_date": export_date,
        "declared_message_count": int(declared_count) if declared_count else None,
        "parsed_message_count": len(fragments),
        "model_note": model_note,
        "attachment_names": attachment_names,
        "attachments_archived": False if attachment_names else None,
        "contains_unresolved_reference_markers": bool(
            re.search(r"(?:cite|filecite)|::chatgpt-content-reference", raw_text)
        ),
    }
    return ChatDocument(
        document_id=f"chatgpt:{conversation_id}",
        conversation_id=conversation_id,
        title=title,
        source_path=project_relative(path),
        published_at=published_at,
        source_updated_at=source_updated_at,
        metadata=metadata,
        raw_text=raw_text,
        raw_bytes=raw_bytes,
        fragments=fragments,
    )


def ingest_chat_document(
    connection: sqlite3.Connection,
    document: ChatDocument,
    captured_at: str,
    topic_ids: Sequence[str] = (),
) -> tuple[bool, int]:
    digest = hashlib.sha256(document.raw_bytes).hexdigest()
    version_id = f"{document.document_id}:sha256:{digest[:20]}"
    now = utc_now()
    metadata_json = json.dumps(document.metadata, ensure_ascii=False, sort_keys=True)

    connection.execute(
        """
        INSERT INTO documents(
            document_id, source_id, external_id, document_kind, title,
            canonical_url, source_path, published_at, source_updated_at,
            first_seen_at, last_seen_at, evidence_kind, status, metadata_json
        ) VALUES (?, 'chatgpt-desktop', ?, 'conversation', ?, NULL, ?, ?, ?, ?, ?,
                  'conversation_mixed', 'current', ?)
        ON CONFLICT(document_id) DO UPDATE SET
            title = excluded.title,
            source_path = excluded.source_path,
            published_at = excluded.published_at,
            source_updated_at = excluded.source_updated_at,
            last_seen_at = excluded.last_seen_at,
            metadata_json = excluded.metadata_json
        """,
        (
            document.document_id,
            document.conversation_id,
            document.title,
            document.source_path,
            document.published_at,
            document.source_updated_at,
            now,
            now,
            metadata_json,
        ),
    )
    set_document_topics(
        connection,
        document.document_id,
        topic_ids,
        assignment_method="configured",
        notes="由 config/chat_topics.json 维护",
    )

    existing = connection.execute(
        "SELECT version_id FROM versions WHERE document_id = ? AND content_sha256 = ?",
        (document.document_id, digest),
    ).fetchone()
    if existing:
        connection.execute(
            "UPDATE versions SET is_current = 0 WHERE document_id = ?",
            (document.document_id,),
        )
        connection.execute(
            "UPDATE versions SET is_current = 1 WHERE version_id = ?",
            (existing["version_id"],),
        )
        return False, 0

    connection.execute(
        "UPDATE versions SET is_current = 0 WHERE document_id = ?",
        (document.document_id,),
    )
    connection.execute(
        """
        INSERT INTO versions(
            version_id, document_id, content_sha256, raw_path, media_type,
            captured_at, byte_size, is_current, metadata_json
        ) VALUES (?, ?, ?, ?, 'text/markdown', ?, ?, 1, ?)
        """,
        (
            version_id,
            document.document_id,
            digest,
            document.source_path,
            captured_at,
            len(document.raw_bytes),
            metadata_json,
        ),
    )

    for fragment in document.fragments:
        fragment_row_id = f"{version_id}:{fragment.fragment_id}"
        fragment_metadata = json.dumps(
            {"message_id": fragment.message_id}, ensure_ascii=False, sort_keys=True
        )
        connection.execute(
            """
            INSERT INTO fragments(
                fragment_row_id, fragment_id, version_id, document_id,
                sequence_no, fragment_kind, stable_anchor, heading, author_role,
                evidence_kind, content, source_line_start, source_line_end, metadata_json
            ) VALUES (?, ?, ?, ?, ?, 'chat_message', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fragment_row_id,
                fragment.fragment_id,
                version_id,
                document.document_id,
                fragment.sequence_no,
                fragment.fragment_id,
                fragment.heading,
                fragment.author_role,
                "user_statement"
                if fragment.author_role == "user"
                else "assistant_analysis",
                fragment.content,
                fragment.line_start,
                fragment.line_end,
                fragment_metadata,
            ),
        )
        connection.execute(
            """
            INSERT INTO fragments_fts(
                fragment_row_id, fragment_id, version_id, document_id,
                title, heading, content
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fragment_row_id,
                fragment.fragment_id,
                version_id,
                document.document_id,
                document.title,
                fragment.heading,
                fragment.content,
            ),
        )
    return True, len(document.fragments)


def command_init(args: argparse.Namespace) -> int:
    with connect(args.db) as connection:
        initialize(connection)
    print(f"已初始化：{project_relative(args.db)}")
    return 0


def command_ingest_chats(args: argparse.Namespace) -> int:
    chat_files = sorted(args.chat_dir.glob("ChatGPT_*.md"))
    if not chat_files:
        print(f"没有找到聊天文件：{args.chat_dir}", file=sys.stderr)
        return 1

    run_id = f"ingest:{uuid.uuid4()}"
    chat_topics = json.loads(DEFAULT_CHAT_TOPICS.read_text(encoding="utf-8"))
    if not isinstance(chat_topics, dict):
        raise ValueError("chat_topics.json 顶层必须是对象")
    started_at = utc_now()
    documents_seen = 0
    versions_added = 0
    fragments_added = 0
    with connect(args.db) as connection:
        initialize(connection)
        run_write_transaction(
            connection,
            lambda active: active.execute(
                "INSERT INTO ingestion_runs(run_id, source_id, started_at, status) "
                "VALUES (?, 'chatgpt-desktop', ?, 'running')",
                (run_id, started_at),
            ),
        )
        try:
            for path in chat_files:
                document = parse_chat(path)
                added, fragment_count = run_write_transaction(
                    connection,
                    lambda active: ingest_chat_document(
                        active,
                        document,
                        captured_at=started_at,
                        topic_ids=chat_topics.get(document.document_id, []),
                    ),
                )
                documents_seen += 1
                versions_added += int(added)
                fragments_added += fragment_count
                state = "新增版本" if added else "内容未变"
                print(f"{state}: {path.name} ({len(document.fragments)} 条消息)")
            run_write_transaction(
                connection,
                lambda active: active.execute(
                    """
                    UPDATE ingestion_runs
                    SET finished_at = ?, status = 'success', documents_seen = ?,
                        versions_added = ?, fragments_added = ?
                    WHERE run_id = ?
                    """,
                    (
                        utc_now(),
                        documents_seen,
                        versions_added,
                        fragments_added,
                        run_id,
                    ),
                ),
            )
        except Exception as exc:
            run_write_transaction(
                connection,
                lambda active: active.execute(
                    """
                    UPDATE ingestion_runs
                    SET finished_at = ?, status = 'failed', documents_seen = ?,
                        versions_added = ?, fragments_added = ?, error_text = ?
                    WHERE run_id = ?
                    """,
                    (
                        utc_now(),
                        documents_seen,
                        versions_added,
                        fragments_added,
                        str(exc),
                        run_id,
                    ),
                ),
            )
            raise

    print(
        f"完成：扫描 {documents_seen} 份；新增 {versions_added} 个版本、"
        f"{fragments_added} 个片段。"
    )
    return 0


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def term_variants(term: str) -> list[str]:
    variants = [term]
    replacements = [
        ("II类", "Ⅱ类"),
        ("Ⅱ类", "II类"),
        ("一篇", "1 篇"),
        ("一篇", " 1 篇"),
        ("一篇", "1篇"),
        ("一次", "1 次"),
        ("一次", " 1 次"),
        ("一次", "1次"),
        ("参加", "参与"),
        ("参与", "参加"),
    ]
    for old, new in replacements:
        for value in list(variants):
            if old in value:
                candidate = value.replace(old, new)
                if candidate not in variants:
                    variants.append(candidate)
    if "日" in term:
        candidate = term.replace("日", "号")
        if candidate not in variants:
            variants.append(candidate)
    elif "号" in term:
        candidate = term.replace("号", "日")
        if candidate not in variants:
            variants.append(candidate)
    return variants


def fts_term_clause(variants: list[str]) -> str:
    quoted = [f'"{value.replace(chr(34), chr(34) * 2)}"' for value in variants]
    return quoted[0] if len(quoted) == 1 else "(" + " OR ".join(quoted) + ")"


def make_snippet(content: str, terms: Iterable[str], width: int = 220) -> str:
    compact = re.sub(r"\s+", " ", content).strip()
    positions = [compact.casefold().find(term.casefold()) for term in terms]
    positions = [position for position in positions if position >= 0]
    start = max(0, (min(positions) if positions else 0) - 60)
    end = min(len(compact), start + width)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(compact) else ""
    return prefix + compact[start:end] + suffix


def document_wide_substring_rows(
    connection: sqlite3.Connection,
    variant_groups: list[list[str]],
    limit: int,
    *,
    topic_groups: Sequence[Sequence[str]] = (),
    topic_mode: str = "any",
    official_only: bool = False,
) -> list[sqlite3.Row]:
    """Find documents when query terms are split across adjacent fragments/pages."""
    if not variant_groups:
        return []
    having_conditions: list[str] = []
    having_parameters: list[str] = []
    for group in variant_groups:
        variant_conditions: list[str] = []
        for variant in group:
            pattern = f"%{escape_like(variant)}%"
            variant_conditions.append(
                "(d.title LIKE ? ESCAPE '\\' OR f.heading LIKE ? ESCAPE '\\' "
                "OR f.content LIKE ? ESCAPE '\\')"
            )
            having_parameters.extend([pattern, pattern, pattern])
        having_conditions.append(
            "MAX(CASE WHEN " + " OR ".join(variant_conditions) + " THEN 1 ELSE 0 END) = 1"
        )
    scope_conditions, scope_parameters = document_scope_conditions(
        topic_groups, topic_mode, official_only
    )
    where_clause = (
        "WHERE " + " AND ".join(scope_conditions) if scope_conditions else ""
    )
    parameters: list[str | int] = [
        *scope_parameters,
        *having_parameters,
        limit,
    ]
    return connection.execute(
        f"""
        SELECT
            d.document_id, d.title, d.source_path,
            '__document_wide__' AS fragment_id,
            '文档级命中（关键词分布于多个片段）' AS heading,
            NULL AS author_role, d.evidence_kind,
            s.authority AS source_authority, s.priority AS source_priority,
            GROUP_CONCAT(f.content, '\n') AS content,
            NULL AS source_line_start, NULL AS source_line_end,
            0 AS simple_rank
        FROM documents d
        JOIN sources s ON s.source_id = d.source_id
        JOIN versions v ON v.document_id = d.document_id AND v.is_current = 1
        JOIN fragments f ON f.version_id = v.version_id
        {where_clause}
        GROUP BY d.document_id
        HAVING {' AND '.join(having_conditions)}
        ORDER BY CASE WHEN s.authority = 'official' THEN 0 ELSE 1 END,
                 s.priority DESC, d.source_updated_at DESC, d.document_id
        LIMIT ?
        """,
        parameters,
    ).fetchall()


def search_result_score(
    row: sqlite3.Row, scope_rank: int, native_rank: int
) -> tuple[int, int]:
    """Prefer direct intent matches, then official evidence over assistant paraphrases."""
    keys = set(row.keys())
    simple_rank = row["simple_rank"] if "simple_rank" in keys else 0
    if scope_rank == 0 and simple_rank >= 40:
        band = 0  # Exact full-query title or heading hit.
    elif scope_rank == 0 and row["source_authority"] == "official":
        band = 1  # Direct official fragment hit.
    elif scope_rank == 0 and row["evidence_kind"] == "user_statement":
        band = 2  # Direct user statement remains discoverable as personal context.
    elif scope_rank == 1 and row["source_authority"] == "official":
        band = 3  # Official terms split across pages/fragments.
    elif scope_rank == 0:
        band = 4  # Assistant analysis or another direct secondary hit.
    elif row["evidence_kind"] == "user_statement":
        band = 5
    else:
        band = 6
    return band, native_rank


def merge_search_rows(
    fragment_rows: list[sqlite3.Row],
    document_rows: list[sqlite3.Row],
    limit: int,
) -> list[sqlite3.Row]:
    """Merge fragment and document-wide hits, returning one best hit per document."""
    ranked: dict[str, tuple[tuple[int, int, int], sqlite3.Row]] = {}
    for scope_rank, candidates in enumerate((fragment_rows, document_rows)):
        for native_rank, row in enumerate(candidates):
            band, source_rank = search_result_score(row, scope_rank, native_rank)
            score = (band, scope_rank, source_rank)
            current = ranked.get(row["document_id"])
            if current is None or score < current[0]:
                ranked[row["document_id"]] = (score, row)
    return [row for _, row in sorted(ranked.values(), key=lambda item: item[0])[:limit]]


def command_search(args: argparse.Namespace) -> int:
    total_started = time.perf_counter()
    query = args.query.strip()
    if not query:
        print("搜索词不能为空。", file=sys.stderr)
        return 2
    terms = [term for term in re.split(r"\s+", query) if term]
    variant_groups = [term_variants(term) for term in terms]
    rows: list[sqlite3.Row] = []
    fts_rows: list[sqlite3.Row] = []
    document_rows: list[sqlite3.Row] = []
    header_mode = ""
    use_fts = all(len(term) >= 3 for term in terms)
    database_started = time.perf_counter()
    with connect(args.db, readonly=True) as connection:
        topic_groups, topic_roots = resolve_topic_groups(connection, args.topic)
        scope_conditions, scope_parameters = document_scope_conditions(
            topic_groups, args.topic_mode, args.official_only
        )

        if use_fts:
            fts_query = " AND ".join(
                fts_term_clause(group) for group in variant_groups
            )
            fts_conditions = ["fragments_fts MATCH ?", *scope_conditions]
            fts_sql = f"""
                SELECT
                    d.document_id, d.title, d.source_path,
                    f.fragment_id, f.heading, f.author_role, f.evidence_kind,
                    s.authority AS source_authority, s.priority AS source_priority,
                    f.content, f.source_line_start, f.source_line_end,
                    bm25(fragments_fts) AS fts_rank
                FROM fragments_fts
                JOIN fragments f ON f.fragment_row_id = fragments_fts.fragment_row_id
                JOIN versions v ON v.version_id = f.version_id AND v.is_current = 1
                JOIN documents d ON d.document_id = f.document_id
                JOIN sources s ON s.source_id = d.source_id
                WHERE {' AND '.join(fts_conditions)}
                ORDER BY fts_rank, d.source_updated_at DESC, f.sequence_no ASC
                LIMIT ?
            """
            fts_rows = connection.execute(
                fts_sql, [fts_query, *scope_parameters, args.limit]
            ).fetchall()
            document_rows = document_wide_substring_rows(
                connection,
                variant_groups,
                args.limit,
                topic_groups=topic_groups,
                topic_mode=args.topic_mode,
                official_only=args.official_only,
            )
            if fts_rows:
                rows = merge_search_rows(fts_rows, document_rows, args.limit)
                header_mode = (
                    "FTS5 trigram + 文档级精确子串"
                    if document_rows
                    else "FTS5 trigram"
                )

        if not fts_rows:
            query_conditions: list[str] = []
            query_parameters: list[str] = []
            for group in variant_groups:
                variant_conditions: list[str] = []
                for variant in group:
                    pattern = f"%{escape_like(variant)}%"
                    variant_conditions.append(
                        "(d.title LIKE ? ESCAPE '\\' OR f.heading LIKE ? ESCAPE '\\' "
                        "OR f.content LIKE ? ESCAPE '\\')"
                    )
                    query_parameters.extend([pattern, pattern, pattern])
                query_conditions.append("(" + " OR ".join(variant_conditions) + ")")

            full_pattern = f"%{escape_like(query)}%"
            rank_expression = (
                "(CASE WHEN d.title LIKE ? ESCAPE '\\' THEN 100 ELSE 0 END + "
                " CASE WHEN f.heading LIKE ? ESCAPE '\\' THEN 40 ELSE 0 END + "
                " CASE WHEN f.content LIKE ? ESCAPE '\\' THEN 20 ELSE 0 END)"
            )
            sql = f"""
                SELECT
                    d.document_id, d.title, d.source_path,
                    f.fragment_id, f.heading, f.author_role, f.evidence_kind,
                    s.authority AS source_authority, s.priority AS source_priority,
                    f.content, f.source_line_start, f.source_line_end,
                    {rank_expression} AS simple_rank
                FROM fragments f
                JOIN versions v ON v.version_id = f.version_id AND v.is_current = 1
                JOIN documents d ON d.document_id = f.document_id
                JOIN sources s ON s.source_id = d.source_id
                WHERE {' AND '.join([*query_conditions, *scope_conditions])}
                ORDER BY simple_rank DESC, d.source_updated_at DESC, f.sequence_no ASC
                LIMIT ?
            """
            sql_parameters: list[str | int] = [
                full_pattern,
                full_pattern,
                full_pattern,
                *query_parameters,
                *scope_parameters,
                args.limit,
            ]
            fragment_rows = connection.execute(sql, sql_parameters).fetchall()
            document_rows = document_wide_substring_rows(
                connection,
                variant_groups,
                args.limit,
                topic_groups=topic_groups,
                topic_mode=args.topic_mode,
                official_only=args.official_only,
            )
            rows = merge_search_rows(fragment_rows, document_rows, args.limit)
            if rows:
                document_wide_count = sum(
                    row["fragment_id"] == "__document_wide__" for row in rows
                )
                if document_wide_count == len(rows):
                    header_mode = "文档级精确子串；关键词可分布于同一原件的不同页或片段"
                elif document_wide_count:
                    header_mode = "片段 + 文档级精确子串；关键词需同时出现"
                else:
                    header_mode = "精确子串；关键词需同时出现，用于短中文词及 FTS 回退"

        topic_names = document_topic_names(
            connection, (row["document_id"] for row in rows)
        )
        root_name_rows = connection.execute(
            "SELECT topic_id, name FROM topics"
        ).fetchall()
        root_names = {row["topic_id"]: row["name"] for row in root_name_rows}
    database_seconds = time.perf_counter() - database_started

    scope_parts: list[str] = []
    if topic_roots:
        selected = "、".join(root_names[topic_id] for topic_id in topic_roots)
        mode_label = "全部主题" if args.topic_mode == "all" else "任一主题"
        scope_parts.append(f"{mode_label}={selected}")
    if args.official_only:
        scope_parts.append("仅官方来源")
    scope_label = f"；范围：{'；'.join(scope_parts)}" if scope_parts else ""

    if rows:
        print(f"命中 {len(rows)} 条（{header_mode}{scope_label}）：\n")
    else:
        print(f"没有命中：{query}{scope_label}")
        if args.timings:
            print(
                f"timings: database={database_seconds:.4f}s "
                f"total={time.perf_counter() - total_started:.4f}s"
            )
        return 1

    for index, row in enumerate(rows, start=1):
        line_text = ""
        if row["source_line_start"]:
            line_text = f":{row['source_line_start']}"
        names = "、".join(topic_names.get(row["document_id"], [])) or "[未分类]"
        print(f"[{index}] {row['title']} · {row['heading']}")
        print(f"    document_id: {row['document_id']}")
        print(f"    fragment_id: {row['fragment_id']}")
        print(f"    topics: {names}")
        print(f"    role/evidence: {row['author_role']} / {row['evidence_kind']}")
        print(f"    source: {row['source_path']}{line_text}")
        print(f"    {make_snippet(row['content'], terms)}\n")
    if args.timings:
        print(
            f"timings: database={database_seconds:.4f}s "
            f"total={time.perf_counter() - total_started:.4f}s"
        )
    return 0


def command_show(args: argparse.Namespace) -> int:
    identifier = args.identifier
    with connect(args.db, readonly=True) as connection:
        document = connection.execute(
            "SELECT * FROM documents WHERE document_id = ?",
            (identifier,),
        ).fetchone()
        if document:
            fragments = connection.execute(
                """
                SELECT f.* FROM fragments f
                JOIN versions v ON v.version_id = f.version_id
                WHERE f.document_id = ? AND v.is_current = 1
                ORDER BY f.sequence_no
                """,
                (identifier,),
            ).fetchall()
            print(f"title: {document['title']}")
            print(f"document_id: {document['document_id']}")
            print(f"source: {document['source_path']}")
            print(f"evidence: {document['evidence_kind']}")
            topics = document_topic_names(connection, [identifier]).get(identifier, [])
            print(f"topics: {'、'.join(topics) or '[未分类]'}")
            print(f"published: {document['published_at']}")
            print(f"updated: {document['source_updated_at']}")
            print(f"fragments: {len(fragments)}")
            for fragment in fragments:
                print(
                    f"\n--- {fragment['heading']} | {fragment['fragment_id']} | "
                    f"lines {fragment['source_line_start']}-{fragment['source_line_end']} ---\n"
                )
                print(fragment["content"])
            return 0

        fragments = connection.execute(
            """
            SELECT f.*, d.title, d.source_path
            FROM fragments f
            JOIN versions v ON v.version_id = f.version_id AND v.is_current = 1
            JOIN documents d ON d.document_id = f.document_id
            WHERE f.fragment_id = ? OR f.fragment_row_id = ?
            ORDER BY d.source_updated_at DESC
            """,
            (identifier, identifier),
        ).fetchall()
        if not fragments:
            print(f"没有找到 ID：{identifier}", file=sys.stderr)
            return 1
        if len(fragments) > 1:
            print(f"找到 {len(fragments)} 个当前片段，显示全部。")
        topic_names = document_topic_names(
            connection, (fragment["document_id"] for fragment in fragments)
        )
        for fragment in fragments:
            print(f"title: {fragment['title']}")
            print(f"document_id: {fragment['document_id']}")
            print(f"fragment_id: {fragment['fragment_id']}")
            print(f"source: {fragment['source_path']}:{fragment['source_line_start']}")
            print(
                "topics: "
                + ("、".join(topic_names.get(fragment["document_id"], [])) or "[未分类]")
            )
            print(f"role/evidence: {fragment['author_role']} / {fragment['evidence_kind']}")
            print(f"\n{fragment['content']}\n")
        return 0


def command_stats(args: argparse.Namespace) -> int:
    with connect(args.db, readonly=True) as connection:
        counts = {
            "sources": connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
            "documents": connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
            "versions": connection.execute("SELECT COUNT(*) FROM versions").fetchone()[0],
            "current_versions": connection.execute(
                "SELECT COUNT(*) FROM versions WHERE is_current = 1"
            ).fetchone()[0],
            "fragments": connection.execute("SELECT COUNT(*) FROM fragments").fetchone()[0],
            "current_fragments": connection.execute(
                "SELECT COUNT(*) FROM fragments f JOIN versions v ON v.version_id = f.version_id "
                "WHERE v.is_current = 1"
            ).fetchone()[0],
            "fts_rows": connection.execute("SELECT COUNT(*) FROM fragments_fts").fetchone()[0],
            "endpoints": connection.execute("SELECT COUNT(*) FROM source_endpoints").fetchone()[0],
            "collection_runs": connection.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0],
            "fetch_attempts": connection.execute("SELECT COUNT(*) FROM fetch_attempts").fetchone()[0],
            "discovered_items": connection.execute(
                "SELECT COUNT(*) FROM discovered_items"
            ).fetchone()[0],
            "topics": connection.execute("SELECT COUNT(*) FROM topics").fetchone()[0],
            "topic_aliases": connection.execute(
                "SELECT COUNT(*) FROM topic_aliases"
            ).fetchone()[0],
            "topic_sources": connection.execute(
                "SELECT COUNT(*) FROM topic_sources"
            ).fetchone()[0],
            "document_topic_assignments": connection.execute(
                "SELECT COUNT(*) FROM document_topics"
            ).fetchone()[0],
            "classified_documents": connection.execute(
                "SELECT COUNT(DISTINCT document_id) FROM document_topics"
            ).fetchone()[0],
            "unclassified_official_documents": connection.execute(
                """
                SELECT COUNT(*) FROM documents d JOIN sources s USING(source_id)
                WHERE s.authority = 'official'
                  AND NOT EXISTS (
                    SELECT 1 FROM document_topics dt
                    WHERE dt.document_id = d.document_id
                  )
                """
            ).fetchone()[0],
            "events": connection.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            "deadlines": connection.execute("SELECT COUNT(*) FROM deadlines").fetchone()[0],
        }
        kinds = connection.execute(
            "SELECT evidence_kind, COUNT(*) AS count FROM documents "
            "GROUP BY evidence_kind ORDER BY count DESC"
        ).fetchall()
    print(f"database: {project_relative(args.db)}")
    for key, value in counts.items():
        print(f"{key}: {value}")
    if kinds:
        print("evidence:")
        for row in kinds:
            print(f"  {row['evidence_kind']}: {row['count']}")
    return 0


def command_db_info(args: argparse.Namespace) -> int:
    with connect(args.db, readonly=True) as connection:
        settings = {
            "database": project_relative(args.db),
            "sqlite_version": sqlite3.sqlite_version,
            "schema_version": connection.execute(
                "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
            ).fetchone()[0],
            **database_settings(connection),
            "page_count": int(connection.execute("PRAGMA page_count").fetchone()[0]),
            "page_size": int(connection.execute("PRAGMA page_size").fetchone()[0]),
        }
    print(json.dumps(settings, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def command_backup(args: argparse.Namespace) -> int:
    output = args.output
    if output is None:
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        output = DEFAULT_BACKUPS / f"library-backup-{stamp}.sqlite3"
    output = output.resolve()
    if output.exists():
        raise ValueError(f"备份目标已存在，不覆盖：{output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".incomplete")
    if temporary.exists():
        raise ValueError(f"临时备份目标已存在：{temporary}")

    source = connect(args.db, readonly=True)
    target = sqlite3.connect(temporary)
    try:
        source.backup(target)
        integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise sqlite3.DatabaseError(f"备份完整性检查失败：{integrity}")
    finally:
        target.close()
        source.close()
    temporary.replace(output)
    digest = hashlib.sha256(output.read_bytes()).hexdigest().upper()
    print(f"backup: {project_relative(output)}")
    print(f"sha256: {digest}")
    return 0


def command_coverage(args: argparse.Namespace) -> int:
    with connect(args.db, readonly=True) as connection:
        if args.topic:
            topic_groups, roots = resolve_topic_groups(connection, [args.topic])
            selected_topics = topic_groups[0]
            placeholders = ", ".join("?" for _ in selected_topics)
            rows = connection.execute(
                f"""
                WITH expected AS (
                    SELECT source_id, MAX(required) AS required,
                           MAX(priority) AS topic_priority,
                           GROUP_CONCAT(DISTINCT coverage_role) AS coverage_roles
                    FROM topic_sources
                    WHERE topic_id IN ({placeholders})
                    GROUP BY source_id
                ),
                document_counts AS (
                    SELECT d.source_id, COUNT(DISTINCT d.document_id) AS document_count
                    FROM documents d JOIN document_topics dt USING(document_id)
                    WHERE dt.topic_id IN ({placeholders})
                    GROUP BY d.source_id
                ),
                endpoint_counts AS (
                    SELECT source_id,
                           COUNT(*) AS endpoint_count,
                           SUM(CASE WHEN last_checked_at IS NOT NULL THEN 1 ELSE 0 END)
                               AS checked_count,
                           SUM(CASE WHEN last_status = 'success' THEN 1 ELSE 0 END)
                               AS success_count,
                           MAX(last_checked_at) AS last_checked_at,
                           MAX(last_success_at) AS last_success_at
                    FROM source_endpoints
                    GROUP BY source_id
                )
                SELECT s.source_id, s.name, s.source_kind,
                       COALESCE(d.document_count, 0) AS document_count,
                       COALESCE(e.endpoint_count, 0) AS endpoint_count,
                       COALESCE(e.checked_count, 0) AS checked_count,
                       COALESCE(e.success_count, 0) AS success_count,
                       e.last_checked_at, e.last_success_at,
                       x.required, x.topic_priority, x.coverage_roles
                FROM sources s
                LEFT JOIN expected x USING(source_id)
                LEFT JOIN document_counts d USING(source_id)
                LEFT JOIN endpoint_counts e USING(source_id)
                WHERE s.enabled = 1
                  AND (x.source_id IS NOT NULL OR COALESCE(d.document_count, 0) > 0)
                ORDER BY COALESCE(x.required, 0) DESC,
                         COALESCE(x.topic_priority, 0) DESC,
                         s.priority DESC, s.source_id
                """,
                [*selected_topics, *selected_topics],
            ).fetchall()
            root = connection.execute(
                "SELECT name FROM topics WHERE topic_id = ?", (roots[0],)
            ).fetchone()
            print(
                f"主题覆盖：{root['name']} [{roots[0]}]"
                + ("（含子主题）" if len(selected_topics) > 1 else "")
            )
        else:
            rows = connection.execute(
                """
                WITH document_counts AS (
                    SELECT source_id, COUNT(*) AS document_count
                    FROM documents
                    GROUP BY source_id
                ),
                endpoint_counts AS (
                    SELECT source_id,
                           COUNT(*) AS endpoint_count,
                           SUM(CASE WHEN last_checked_at IS NOT NULL THEN 1 ELSE 0 END)
                               AS checked_count,
                           SUM(CASE WHEN last_status = 'success' THEN 1 ELSE 0 END)
                               AS success_count,
                           MAX(last_checked_at) AS last_checked_at,
                           MAX(last_success_at) AS last_success_at
                    FROM source_endpoints
                    GROUP BY source_id
                )
                SELECT s.source_id, s.name, s.source_kind,
                       COALESCE(d.document_count, 0) AS document_count,
                       COALESCE(e.endpoint_count, 0) AS endpoint_count,
                       COALESCE(e.checked_count, 0) AS checked_count,
                       COALESCE(e.success_count, 0) AS success_count,
                       e.last_checked_at, e.last_success_at,
                       NULL AS required, NULL AS topic_priority,
                       NULL AS coverage_roles
                FROM sources s
                LEFT JOIN document_counts d USING(source_id)
                LEFT JOIN endpoint_counts e USING(source_id)
                WHERE s.enabled = 1
                ORDER BY s.priority DESC, s.source_id
                """
            ).fetchall()

    for row in rows:
        if row["source_kind"] == "conversation_archive":
            state = "本地档案"
        elif row["document_count"]:
            state = "已有馆藏"
        elif row["endpoint_count"]:
            state = "有入口，尚无本主题馆藏" if args.topic else "有入口，尚无馆藏"
        else:
            state = "仅登记"
        print(f"{row['name']} [{state}]")
        print(
            f"  source_id: {row['source_id']} | documents: {row['document_count']} | "
            f"endpoints: {row['endpoint_count']}"
        )
        if args.topic:
            expectation = (
                "必查" if row["required"] else "补充"
            ) if row["coverage_roles"] else "馆藏中实际命中"
            print(
                f"  topic expectation: {expectation} | roles: "
                f"{row['coverage_roles'] or '[未登记]'}"
            )
        if row["endpoint_count"]:
            check_label = (
                "入口历史健康（非本主题核查）" if args.topic else "已记录检查"
            )
            print(
                f"  {check_label}: {row['checked_count']}/{row['endpoint_count']} | "
                f"success: {row['success_count']}/{row['endpoint_count']} | "
                f"last checked: {row['last_checked_at'] or '未记录'} | "
                f"last success: {row['last_success_at'] or '未记录'}"
            )
    if args.topic:
        print(
            "说明：主题文档数只统计已完成主题标注的馆藏；来源被列为必查或补充，"
            "不表示该来源已经完成本主题检查。"
        )
    else:
        print(
            "说明：这里只报告已经写入数据库的采集审计；没有采集记录不等于来源没有内容，"
            "临时访问失败也必须与正式的‘未发现’分开记录。"
        )
    return 0


def command_topics(args: argparse.Namespace) -> int:
    with connect(args.db, readonly=True) as connection:
        if args.topic:
            topic_id = resolve_topic_id(connection, args.topic)
            topic = connection.execute(
                "SELECT * FROM topics WHERE topic_id = ?", (topic_id,)
            ).fetchone()
            aliases = connection.execute(
                "SELECT alias FROM topic_aliases WHERE topic_id = ? ORDER BY alias",
                (topic_id,),
            ).fetchall()
            children = connection.execute(
                """
                SELECT topic_id, name FROM topics
                WHERE parent_topic_id = ? AND status = 'active'
                ORDER BY priority DESC, topic_id
                """,
                (topic_id,),
            ).fetchall()
            documents = connection.execute(
                """
                SELECT d.document_id, d.title, s.authority
                FROM document_topics dt
                JOIN documents d USING(document_id)
                JOIN sources s USING(source_id)
                WHERE dt.topic_id = ?
                ORDER BY CASE WHEN s.authority = 'official' THEN 0 ELSE 1 END,
                         d.published_at DESC, d.document_id
                """,
                (topic_id,),
            ).fetchall()
            sources = connection.execute(
                """
                SELECT ts.*, s.name AS source_name
                FROM topic_sources ts JOIN sources s USING(source_id)
                WHERE ts.topic_id = ?
                ORDER BY ts.required DESC, ts.priority DESC, ts.source_id
                """,
                (topic_id,),
            ).fetchall()
            print(f"{topic['name']} [{topic_id}]")
            print(f"parent: {topic['parent_topic_id'] or '[无]'}")
            print(f"description: {topic['description'] or '[无]'}")
            print(
                "aliases: "
                + ("、".join(row["alias"] for row in aliases) or "[无]")
            )
            print(
                "children: "
                + (
                    "、".join(
                        f"{row['name']} [{row['topic_id']}]" for row in children
                    )
                    or "[无]"
                )
            )
            print(f"direct documents: {len(documents)}")
            for row in documents[: args.limit]:
                print(
                    f"  {row['document_id']} | {row['authority']} | {row['title']}"
                )
            if len(documents) > args.limit:
                print(f"  …另有 {len(documents) - args.limit} 份")
            print("expected sources:")
            if not sources:
                print("  [未登记]")
            for row in sources:
                requirement = "必查" if row["required"] else "补充"
                print(
                    f"  {requirement} | {row['coverage_role']} | "
                    f"{row['source_name']} [{row['source_id']}]"
                )
            return 0

        rows = connection.execute(
            """
            SELECT t.topic_id, t.name, t.parent_topic_id, t.priority,
                   COUNT(DISTINCT dt.document_id) AS document_count,
                   COUNT(DISTINCT CASE WHEN s.authority = 'official'
                                       THEN dt.document_id END) AS official_count
            FROM topics t
            LEFT JOIN document_topics dt USING(topic_id)
            LEFT JOIN documents d USING(document_id)
            LEFT JOIN sources s USING(source_id)
            WHERE t.status = 'active'
            GROUP BY t.topic_id
            ORDER BY CASE WHEN t.parent_topic_id IS NULL THEN 0 ELSE 1 END,
                     t.priority DESC, t.topic_id
            """
        ).fetchall()
        aliases = connection.execute(
            "SELECT topic_id, alias FROM topic_aliases ORDER BY topic_id, alias"
        ).fetchall()
        alias_map: dict[str, list[str]] = {}
        for row in aliases:
            alias_map.setdefault(row["topic_id"], []).append(row["alias"])
        unclassified = connection.execute(
            """
            SELECT COUNT(*) FROM documents d JOIN sources s USING(source_id)
            WHERE s.authority = 'official'
              AND NOT EXISTS (
                SELECT 1 FROM document_topics dt WHERE dt.document_id = d.document_id
              )
            """
        ).fetchone()[0]

    print(f"主题 {len(rows)} 个；未分类官方文档 {unclassified} 份：")
    for row in rows:
        parent = f" | parent={row['parent_topic_id']}" if row["parent_topic_id"] else ""
        alias_text = "、".join(alias_map.get(row["topic_id"], []))
        print(
            f"- {row['name']} [{row['topic_id']}] | direct_documents={row['document_count']} "
            f"(official={row['official_count']}){parent}"
        )
        if alias_text:
            print(f"  aliases: {alias_text}")
    return 0


def command_events(args: argparse.Namespace) -> int:
    with connect(args.db, readonly=True) as connection:
        if args.event_id:
            events = connection.execute(
                "SELECT * FROM events WHERE event_id = ?", (args.event_id,)
            ).fetchall()
        else:
            events = connection.execute(
                "SELECT * FROM events ORDER BY priority DESC, starts_at DESC"
            ).fetchall()
        if not events:
            print("没有找到事件。", file=sys.stderr)
            return 1
        for event in events:
            print(f"{event['title']} [{event['status']}]")
            print(f"event_id: {event['event_id']}")
            print(f"period: {event['starts_at']} -> {event['ends_at']}")
            documents = connection.execute(
                """
                SELECT d.document_id, d.title, ed.document_role
                FROM event_documents ed JOIN documents d USING(document_id)
                WHERE ed.event_id = ? ORDER BY ed.sequence_no, d.document_id
                """,
                (event["event_id"],),
            ).fetchall()
            for document in documents:
                print(
                    f"source: {document['document_role']} | "
                    f"{document['document_id']} | {document['title']}"
                )
            deadlines = connection.execute(
                """
                SELECT * FROM deadlines WHERE event_id = ?
                ORDER BY CASE WHEN deadline_at IS NULL THEN 1 ELSE 0 END, deadline_at
                """,
                (event["event_id"],),
            ).fetchall()
            for deadline in deadlines:
                print(
                    f"deadline: {deadline['deadline_at'] or deadline['raw_text']} | "
                    f"{deadline['action']} | {deadline['status']}"
                )
            print()
    return 0


def command_deadlines(args: argparse.Namespace) -> int:
    with connect(args.db, readonly=True) as connection:
        rows = connection.execute(
            """
            SELECT dl.*, e.title AS event_title
            FROM deadlines dl LEFT JOIN events e USING(event_id)
            WHERE (? = 1 OR dl.status = 'open')
            ORDER BY CASE WHEN dl.deadline_at IS NULL THEN 1 ELSE 0 END,
                     dl.deadline_at, dl.deadline_id
            """,
            (int(args.all),),
        ).fetchall()
    if not rows:
        print("没有截止节点。")
        return 0
    for row in rows:
        print(f"{row['deadline_at'] or '[时间待核]'} | {row['event_title']}")
        print(f"  {row['action']}")
        print(f"  applies_to: {row['applies_to']} | status: {row['status']}")
        print(f"  evidence: {row['document_id']} / {row['fragment_row_id'] or '未定位片段'}")
    return 0


def benchmark_search_rows(
    connection: sqlite3.Connection,
    query: str,
    limit: int,
    *,
    topic_groups: Sequence[Sequence[str]] = (),
    topic_mode: str = "any",
    official_only: bool = False,
) -> list[sqlite3.Row]:
    terms = [term for term in re.split(r"\s+", query.strip()) if term]
    variant_groups = [term_variants(term) for term in terms]
    scope_conditions, scope_parameters = document_scope_conditions(
        topic_groups, topic_mode, official_only
    )
    if terms and all(len(term) >= 3 for term in terms):
        fts_query = " AND ".join(fts_term_clause(group) for group in variant_groups)
        fts_conditions = ["fragments_fts MATCH ?", *scope_conditions]
        rows = connection.execute(
            f"""
            SELECT d.document_id, d.title, f.fragment_id, f.evidence_kind,
                   s.authority AS source_authority, s.priority AS source_priority,
                   bm25(fragments_fts) AS rank_value
            FROM fragments_fts
            JOIN fragments f ON f.fragment_row_id = fragments_fts.fragment_row_id
            JOIN versions v ON v.version_id = f.version_id AND v.is_current = 1
            JOIN documents d ON d.document_id = f.document_id
            JOIN sources s ON s.source_id = d.source_id
            WHERE {' AND '.join(fts_conditions)}
            ORDER BY rank_value, d.source_updated_at DESC, f.sequence_no
            LIMIT ?
            """,
            [fts_query, *scope_parameters, limit],
        ).fetchall()
        if rows:
            document_rows = document_wide_substring_rows(
                connection,
                variant_groups,
                limit,
                topic_groups=topic_groups,
                topic_mode=topic_mode,
                official_only=official_only,
            )
            return merge_search_rows(rows, document_rows, limit)

    query_conditions: list[str] = []
    query_parameters: list[str] = []
    for group in variant_groups:
        variant_conditions: list[str] = []
        for variant in group:
            pattern = f"%{escape_like(variant)}%"
            variant_conditions.append(
                "(d.title LIKE ? ESCAPE '\\' OR f.heading LIKE ? ESCAPE '\\' "
                "OR f.content LIKE ? ESCAPE '\\')"
            )
            query_parameters.extend([pattern, pattern, pattern])
        query_conditions.append("(" + " OR ".join(variant_conditions) + ")")
    if not query_conditions:
        return []
    rows = connection.execute(
        f"""
        SELECT d.document_id, d.title, f.fragment_id, f.evidence_kind,
               s.authority AS source_authority, s.priority AS source_priority,
               0 AS rank_value
        FROM fragments f
        JOIN versions v ON v.version_id = f.version_id AND v.is_current = 1
        JOIN documents d ON d.document_id = f.document_id
        JOIN sources s ON s.source_id = d.source_id
        WHERE {' AND '.join([*query_conditions, *scope_conditions])}
        ORDER BY d.source_updated_at DESC, f.sequence_no
        LIMIT ?
        """,
        [*query_parameters, *scope_parameters, limit],
    ).fetchall()
    document_rows = document_wide_substring_rows(
        connection,
        variant_groups,
        limit,
        topic_groups=topic_groups,
        topic_mode=topic_mode,
        official_only=official_only,
    )
    return merge_search_rows(rows, document_rows, limit)


def command_benchmark(args: argparse.Namespace) -> int:
    benchmarks = json.loads(args.config.read_text(encoding="utf-8"))
    failures = 0
    with connect(args.db, readonly=True) as connection:
        for benchmark in benchmarks:
            max_rank = int(benchmark.get("max_rank", 10))
            topic_groups, roots = resolve_topic_groups(
                connection, benchmark.get("topics", [])
            )
            rows = benchmark_search_rows(
                connection,
                benchmark["query"],
                max_rank,
                topic_groups=topic_groups,
                topic_mode=benchmark.get("topic_mode", "any"),
                official_only=bool(benchmark.get("official_only", False)),
            )
            expected = set(benchmark["expected_any"])
            found_rank: int | None = None
            found_document: str | None = None
            for rank, row in enumerate(rows, start=1):
                if row["document_id"] in expected:
                    found_rank = rank
                    found_document = row["document_id"]
                    break
            if found_rank is None:
                failures += 1
                candidates = ", ".join(dict.fromkeys(row["document_id"] for row in rows))
                print(
                    f"FAIL {benchmark['benchmark_id']}: {benchmark['query']} | "
                    f"top={candidates or '[无结果]'}"
                )
            else:
                print(
                    f"PASS {benchmark['benchmark_id']}: rank {found_rank} | "
                    f"{found_document}"
                    + (f" | topics={','.join(roots)}" if roots else "")
                )
    print(f"基准完成：{len(benchmarks) - failures}/{len(benchmarks)} 通过。")
    return 1 if failures else 0


def command_check(args: argparse.Namespace) -> int:
    failures: list[str] = []
    with connect(args.db, readonly=True) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            failures.append(f"integrity_check: {integrity}")
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            failures.append(f"foreign_key_check: {len(foreign_keys)} 个问题")
        duplicate_current = connection.execute(
            """
            SELECT document_id, COUNT(*) AS count
            FROM versions WHERE is_current = 1
            GROUP BY document_id HAVING COUNT(*) != 1
            """
        ).fetchall()
        document_count = connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        if document_count and duplicate_current:
            failures.append(f"current_version: {len(duplicate_current)} 个文档异常")
        missing_current = connection.execute(
            """
            SELECT COUNT(*) FROM documents d
            LEFT JOIN versions v ON v.document_id = d.document_id AND v.is_current = 1
            WHERE v.version_id IS NULL
            """
        ).fetchone()[0]
        if missing_current:
            failures.append(f"missing_current_version: {missing_current}")
        fragment_rows = connection.execute("SELECT COUNT(*) FROM fragments").fetchone()[0]
        fts_rows = connection.execute("SELECT COUNT(*) FROM fragments_fts").fetchone()[0]
        if fragment_rows != fts_rows:
            failures.append(f"fts_rows: fragments={fragment_rows}, fts={fts_rows}")
        unclassified_official = connection.execute(
            """
            SELECT d.document_id FROM documents d JOIN sources s USING(source_id)
            WHERE s.authority = 'official'
              AND NOT EXISTS (
                SELECT 1 FROM document_topics dt WHERE dt.document_id = d.document_id
              )
            ORDER BY d.document_id
            """
        ).fetchall()
        if unclassified_official:
            sample = ", ".join(row["document_id"] for row in unclassified_official[:5])
            failures.append(
                f"unclassified_official_documents: {len(unclassified_official)}"
                f"（示例：{sample}）"
            )
        mismatched_topic_endpoints = connection.execute(
            """
            SELECT ts.topic_source_id
            FROM topic_sources ts JOIN source_endpoints se USING(endpoint_id)
            WHERE ts.endpoint_id IS NOT NULL AND ts.source_id != se.source_id
            """
        ).fetchall()
        if mismatched_topic_endpoints:
            failures.append(
                f"topic_endpoint_source_mismatch: {len(mismatched_topic_endpoints)}"
            )
        for row in connection.execute(
            "SELECT item_id, topic_hints_json, metadata_json, first_seen_at, last_seen_at "
            "FROM discovered_items"
        ):
            try:
                topic_hints = json.loads(row["topic_hints_json"])
                metadata = json.loads(row["metadata_json"])
                if not isinstance(topic_hints, list) or not isinstance(metadata, dict):
                    raise ValueError("JSON 类型错误")
                if row["last_seen_at"] < row["first_seen_at"]:
                    raise ValueError("last_seen_at 早于 first_seen_at")
            except (ValueError, json.JSONDecodeError) as exc:
                failures.append(f"discovered_item_invalid: {row['item_id']} ({exc})")
        current_raw_files = connection.execute(
            "SELECT raw_path, content_sha256 FROM versions WHERE is_current = 1"
        ).fetchall()
        for raw_file in current_raw_files:
            raw_path = Path(raw_file["raw_path"])
            if not raw_path.is_absolute():
                raw_path = PROJECT_ROOT / raw_path
            if not raw_path.is_file():
                failures.append(f"missing_raw_file: {raw_file['raw_path']}")
                continue
            actual_hash = hashlib.sha256(raw_path.read_bytes()).hexdigest()
            if actual_hash != raw_file["content_sha256"]:
                failures.append(f"raw_hash_mismatch: {raw_file['raw_path']}")

    if failures:
        print("检查失败：")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print(
        "检查通过：SQLite 完整性、外键、当前版本、主题分类、发现层、"
        "原件哈希和 FTS 行数均正常。"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Retained local evidence query tools")
    parser.add_argument(
        "--db", type=Path, default=DEFAULT_DB, help="SQLite 数据库路径"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="初始化数据库和信息源")
    init_parser.set_defaults(func=command_init)

    ingest_parser = subparsers.add_parser("ingest-chats", help="导入原始 ChatGPT 讨论")
    ingest_parser.add_argument(
        "--chat-dir", type=Path, default=DEFAULT_CHAT_DIR, help="聊天 Markdown 目录"
    )
    ingest_parser.set_defaults(func=command_ingest_chats)

    search_parser = subparsers.add_parser("search", help="按关键词搜索当前版本片段")
    search_parser.add_argument("query", help="关键词；空格分隔时要求全部出现")
    search_parser.add_argument("--limit", type=int, default=20, help="最多返回多少条")
    search_parser.add_argument(
        "--topic",
        action="append",
        help="按主题 ID、名称或别名过滤；可重复指定",
    )
    search_parser.add_argument(
        "--topic-mode",
        choices=("any", "all"),
        default="any",
        help="多个主题要求任一匹配或全部匹配；默认 any",
    )
    search_parser.add_argument(
        "--official-only", action="store_true", help="只返回官方来源"
    )
    search_parser.add_argument(
        "--timings", action="store_true", help="输出数据库与命令总耗时"
    )
    search_parser.set_defaults(func=command_search)

    show_parser = subparsers.add_parser("show", help="查看文档或当前片段")
    show_parser.add_argument("identifier", help="document_id、fragment_id 或 fragment_row_id")
    show_parser.set_defaults(func=command_show)

    stats_parser = subparsers.add_parser("stats", help="显示数据库统计")
    stats_parser.set_defaults(func=command_stats)

    db_info_parser = subparsers.add_parser(
        "db-info", help="只读显示 WAL、锁等待和连接保护状态"
    )
    db_info_parser.set_defaults(func=command_db_info)

    backup_parser = subparsers.add_parser(
        "backup", help="使用 SQLite 在线备份接口生成 WAL 安全快照"
    )
    backup_parser.add_argument("--output", type=Path)
    backup_parser.set_defaults(func=command_backup)

    coverage_parser = subparsers.add_parser(
        "coverage", help="按来源显示馆藏、入口和已记录检查状态"
    )
    coverage_parser.add_argument(
        "--topic", help="只显示指定主题（含子主题）的相关来源与馆藏"
    )
    coverage_parser.set_defaults(func=command_coverage)

    topics_parser = subparsers.add_parser(
        "topics", help="列出主题，或查看单个主题的文档和预期来源"
    )
    topics_parser.add_argument(
        "topic", nargs="?", help="可选的主题 ID、名称或别名"
    )
    topics_parser.add_argument(
        "--limit", type=int, default=20, help="详情模式最多显示多少份文档"
    )
    topics_parser.set_defaults(func=command_topics)

    events_parser = subparsers.add_parser("events", help="列出事件、证据和截止节点")
    events_parser.add_argument("event_id", nargs="?", help="可选的事件 ID")
    events_parser.set_defaults(func=command_events)

    deadlines_parser = subparsers.add_parser("deadlines", help="按时间列出截止节点")
    deadlines_parser.add_argument("--all", action="store_true", help="包含已关闭节点")
    deadlines_parser.set_defaults(func=command_deadlines)

    benchmark_parser = subparsers.add_parser(
        "benchmark", help="运行真实问题前10条检索基准"
    )
    benchmark_parser.add_argument(
        "--config", type=Path, default=DEFAULT_BENCHMARKS, help="基准配置 JSON"
    )
    benchmark_parser.set_defaults(func=command_benchmark)

    check_parser = subparsers.add_parser("check", help="执行数据库一致性检查")
    check_parser.set_defaults(func=command_check)
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args()
    args.db = args.db.resolve()
    if getattr(args, "chat_dir", None):
        args.chat_dir = args.chat_dir.resolve()
    if getattr(args, "config", None):
        args.config = args.config.resolve()
    try:
        return args.func(args)
    except (OSError, ValueError, sqlite3.Error, json.JSONDecodeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
