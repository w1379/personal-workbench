"""Shared discovery index; precise school/advisor queries retain their algorithms.

The index is a projection, never an editable source of facts. Scope constraints
are applied to evidence rows before matching, grouping, ranking or pagination.
All query terms must occur in one object's same version, possibly in different
fragments. This prevents accidental combinations of superseded facts.
"""
from __future__ import annotations

import importlib.util
import hashlib
import json
import re
import sqlite3
import sys
import threading
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import core

OFFICIAL_AUTHORITIES = ("official", "enterprise_official", "official_notice")
INDEX_VERSION = 1
_LEGACY_LOAD_LOCK = threading.RLock()


def _has(c, name):
    return c.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() is not None


def _json(value, default=None):
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return default


def _values(value):
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


def term_variants(term):
    """Retain the small, explicit variant rules already used by school search."""
    variants = [term]
    for old, new in [("II类", "Ⅱ类"), ("Ⅱ类", "II类"), ("一篇", "1 篇"),
                     ("一篇", " 1 篇"), ("一篇", "1篇"), ("一次", "1 次"),
                     ("一次", " 1 次"), ("一次", "1次"), ("参加", "参与"), ("参与", "参加")]:
        for value in list(variants):
            if old in value and value.replace(old, new) not in variants:
                variants.append(value.replace(old, new))
    if "日" in term:
        variants.append(term.replace("日", "号"))
    elif "号" in term:
        variants.append(term.replace("号", "日"))
    return list(dict.fromkeys(variants))


def _topic_groups(c, values):
    if not values:
        return []
    available = {}
    parents = defaultdict(list)
    if _has(c, "topics"):
        for row in c.execute("SELECT topic_id,name,parent_topic_id FROM topics"):
            available[row["topic_id"].casefold()] = row["topic_id"]
            available[row["name"].casefold()] = row["topic_id"]
            parents[row["parent_topic_id"]].append(row["topic_id"])
    if _has(c, "topic_aliases"):
        for row in c.execute("SELECT topic_id,alias FROM topic_aliases"):
            available[row["alias"].casefold()] = row["topic_id"]
    # Native entries may use lightweight tags before a source catalogue exists.
    for row in c.execute("SELECT DISTINCT j.value FROM search_entries e,json_each(e.topics_json) j"):
        available.setdefault(str(row[0]).casefold(), str(row[0]))
    groups = []
    for value in values:
        resolved = available.get(str(value).casefold())
        if resolved is None:
            raise ValueError(f"Unknown topic: {value}")
        descendants, pending = set(), [resolved]
        while pending:
            current = pending.pop()
            if current not in descendants:
                descendants.add(current)
                pending.extend(parents[current])
        groups.append(sorted(descendants))
    return groups


def _scope(c, *, domain, kind, authority, official_only, topic, topics,
           topic_mode, history, include_archived, evidence_kind, author_role):
    clauses, args = [], []
    for column, values in [("e.domain", domain), ("i.kind", kind), ("e.authority", authority),
                           ("e.evidence_kind", evidence_kind), ("e.author_role", author_role)]:
        values = _values(values)
        if values:
            clauses.append(f"{column} IN ({','.join('?' for _ in values)})")
            args.extend(values)
    if official_only:
        clauses.append("e.authority IN (?,?,?)")
        args.extend(OFFICIAL_AUTHORITIES)
        # An official document's container must not launder assistant inference.
        clauses.append("COALESCE(e.author_role,'') != 'assistant'")
        clauses.append("e.evidence_kind NOT IN ('assistant_analysis','derived_analysis','derived_summary')")
    if history == "current":
        clauses.append("e.is_current=1")
    elif history == "historical":
        clauses.append("(e.is_current=0 OR i.archived_at IS NOT NULL)")
    elif history != "all":
        raise ValueError("history must be all, current or historical")
    if not include_archived:
        clauses.append("i.archived_at IS NULL")
    groups = _topic_groups(c, _values(topic) + _values(topics))
    if topic_mode not in ("any", "all"):
        raise ValueError("topic_mode must be any or all")
    if groups:
        groups = groups if topic_mode == "all" else [sorted(set().union(*map(set, groups)))]
        for group in groups:
            clauses.append("EXISTS (SELECT 1 FROM json_each(e.topics_json) jt WHERE jt.value IN ("
                           + ",".join("?" for _ in group) + "))")
            args.extend(group)
    return clauses or ["1"], args


def _snippet(text, variants, width=260):
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    positions = [compact.casefold().find(v.casefold()) for v in variants]
    positions = [p for p in positions if p >= 0]
    start = max(0, (min(positions) if positions else 0) - 60)
    return ("…" if start else "") + compact[start:start + width] + ("…" if start + width < len(compact) else "")


def _coverage(c):
    result = {"indexed_entries": c.execute("SELECT COUNT(*) FROM search_entries").fetchone()[0],
              "index_version": INDEX_VERSION, "scope": "saved and indexed local content"}
    if _has(c, "schema_metadata"):
        row = c.execute("SELECT value FROM schema_metadata WHERE key='v2_search_rebuild'").fetchone()
        if row:
            result["rebuild"] = _json(row[0], {})
    if _has(c, "document_processing"):
        result["document_processing"] = {r[0]: r[1] for r in c.execute(
            "SELECT state,COUNT(*) FROM document_processing GROUP BY state")}
    return result


def search(root, query, *, limit=20, offset=0, domain=None, kind=None, authority=None,
           official_only=False, topic=None, topics=None, topic_mode="any", history="all",
           include_archived=True, evidence_kind=None, author_role=None):
    """Return object-level discovery results with per-evidence provenance.

    No candidate LIMIT occurs before scope filtering and version-aware object
    aggregation. LIMIT/OFFSET paginate *objects*. No hidden lexical rewriting
    other than the existing explicit spelling variants is performed.
    """
    query = str(query).strip()
    if not query:
        raise ValueError("Search query must not be empty")
    if not isinstance(limit, int) or not 1 <= limit <= 1000 or not isinstance(offset, int) or offset < 0:
        raise ValueError("limit must be 1..1000 and offset must be non-negative")
    terms = re.split(r"\s+", query)
    variants = [term_variants(term) for term in terms]
    c = core.connect(root, readonly=True)
    try:
        # One SQLite read snapshot keeps candidates and pagination internally
        # consistent if another task commits during this query.
        c.execute("BEGIN")
        clauses, scope_args = _scope(c, domain=domain, kind=kind, authority=authority,
            official_only=official_only, topic=topic, topics=topics, topic_mode=topic_mode,
            history=history, include_archived=include_archived,
            evidence_kind=evidence_kind, author_role=author_role)
        candidates = {}
        for number, group in enumerate(variants):
            matches, parameters = [], []
            long = [v for v in group if len(v) >= 3]
            if long:
                fts_query = " OR ".join('"' + v.replace('"', '""') + '"' for v in long)
                matches.append("e.entry_id IN (SELECT rowid FROM search_fts WHERE search_fts MATCH ?)")
                parameters.append(fts_query)
            for value in group:
                if len(value) < 3:
                    matches.append("(instr(lower(e.heading),lower(?))>0 OR instr(lower(e.text),lower(?))>0)")
                    parameters.extend([value, value])
                matches.append("(instr(lower(i.title),lower(?))>0 OR i.item_id=? OR EXISTS "
                               "(SELECT 1 FROM aliases a WHERE a.item_id=i.item_id AND instr(lower(a.alias),lower(?))>0))")
                parameters.extend([value, value, value])
            sql = """SELECT e.*,i.kind,i.title,i.revision,i.archived_at,i.updated_at,
                            p.state AS processing_state
                     FROM search_entries e JOIN items i USING(item_id)
                     LEFT JOIN document_processing p ON p.version_id=e.version_id WHERE """
            sql += " AND ".join(clauses) + " AND (" + " OR ".join(matches) + ")"
            for row in c.execute(sql, [*scope_args, *parameters]):
                data = dict(row)
                key = (data["item_id"], data["version_id"] or "")
                candidate = candidates.setdefault(key, {"terms": set(), "entries": {}, "item": data})
                candidate["terms"].add(number)
                candidate["entries"].setdefault(data["entry_id"], {"data": data, "terms": set()})["terms"].add(number)
        per_item = {}
        flat_variants = [v for group in variants for v in group]
        for (item_id, version), candidate in candidates.items():
            if len(candidate["terms"]) != len(terms):
                continue
            data = candidate["item"]
            entries = list(candidate["entries"].values())
            entries.sort(key=lambda e: (-len(e["terms"]), -int(e["data"]["is_current"]), e["data"]["entry_id"]))
            is_current = any(e["data"]["is_current"] for e in entries)
            exact = query.casefold() in (item_id.casefold(), data["title"].casefold())
            score = (int(exact), int(is_current), int(data["archived_at"] is None),
                     max(len(e["terms"]) for e in entries),
                     int(any(e["data"]["authority"] in OFFICIAL_AUTHORITIES for e in entries)),
                     int(query.casefold() in data["title"].casefold()))
            previous = per_item.get(item_id)
            if previous is None or score > previous["_score"]:
                # Keep enough snippets to show evidence for every term, without
                # flooding results with all fragments of a long PDF.
                selected, covered = [], set()
                for entry in entries:
                    if len(selected) < 3 or not entry["terms"].issubset(covered):
                        selected.append(entry)
                        covered.update(entry["terms"])
                evidence = []
                for entry in selected:
                    e = entry["data"]
                    evidence.append({k: e[k] for k in ("entry_id", "entry_key", "heading", "source_ref",
                        "version_id", "is_current", "authority", "evidence_kind", "author_role", "processing_state")})
                    evidence[-1].update(snippet=_snippet(e["text"], flat_variants),
                                        topics=_json(e["topics_json"], []),
                                        matched_terms=[terms[n] for n in sorted(entry["terms"])])
                per_item[item_id] = {k: data[k] for k in ("item_id", "kind", "title", "domain", "revision", "archived_at")}
                per_item[item_id].update(matches=evidence, is_current=bool(is_current),
                    match_reason="exact identity/title" if exact else ("same-version cross-fragment" if
                        max(len(e["terms"]) for e in entries) < len(terms) else "text or alias"), _score=score)
        ordered = sorted(per_item.values(), key=lambda row: row["item_id"])
        ordered.sort(key=lambda row: row["_score"], reverse=True)
        page = ordered[offset:offset + limit]
        for row in page:
            row.pop("_score", None)
        total = len(ordered)
        return {"query": query, "results": page, "total": total, "limit": limit, "offset": offset,
                "has_more": offset + limit < total,
                "next_offset": offset + limit if offset + limit < total else None,
                "coverage": _coverage(c)}
    finally:
        c.close()


def resolve(root, query, *, limit=20):
    """Resolve IDs, names, aliases and registered paths; do not guess uniqueness."""
    if not str(query).strip():
        raise ValueError("Resolve query must not be empty")
    root = core.root_path(root)
    query = str(query).strip()
    c = core.connect(root, readonly=True)
    try:
        rows = c.execute("""SELECT DISTINCT i.* FROM items i
            LEFT JOIN aliases a USING(item_id) LEFT JOIN locations l USING(item_id)
            WHERE i.item_id=? OR instr(lower(i.title),lower(?))>0
            OR instr(lower(a.alias),lower(?))>0 OR instr(lower(l.path),lower(?))>0""",
            (query, query, query, query)).fetchall()
        results = []
        for row in rows:
            data = dict(row)
            data.pop("metadata_json", None)
            aliases = [dict(r) for r in c.execute("SELECT alias,context FROM aliases WHERE item_id=?", (row["item_id"],))]
            paths = []
            for r in c.execute("SELECT * FROM locations WHERE item_id=? ORDER BY is_current DESC,path", (row["item_id"],)):
                location = dict(r)
                location.pop("metadata_json", None)
                raw = location["path"]
                if "://" in raw:
                    location.update(resolved_path=raw, exists=None)
                else:
                    path = Path(raw) if location["is_external"] else root / raw
                    resolved = path.resolve()
                    # Internal locations must stay under the new root. A stale
                    # malicious path never gains permission merely by indexing.
                    valid = bool(location["is_external"] or resolved.is_relative_to(root.resolve()))
                    location.update(resolved_path=str(resolved), exists=valid and resolved.exists(), valid=valid)
                paths.append(location)
            score = 3 if query == row["item_id"] else 2 if query.casefold() == row["title"].casefold() else 1 if any(
                a["alias"].casefold() == query.casefold() for a in aliases) else 0
            data.update(aliases=aliases, locations=paths, _score=score)
            results.append(data)
        results.sort(key=lambda r: (-r["_score"], r["item_id"]))
        page = results[:limit]
        for row in page:
            row.pop("_score", None)
        return {"query": query, "results": page, "total": len(results), "ambiguous": len(results) > 1,
                "has_more": len(results) > limit}
    finally:
        c.close()


def _text(row, keys):
    return "\n".join(f"{key}: {row[key]}" for key in keys if key in row.keys() and row[key] not in (None, "", "[]", "{}"))


def _ensure(c, item_id, kind, title, *, domain, authority="derived", evidence_kind="derived_summary", description=""):
    if not c.execute("SELECT 1 FROM items WHERE item_id=?", (item_id,)).fetchone():
        core.new_item(c, kind, title, description=description, domain=domain, authority=authority,
                      evidence_kind=evidence_kind, item_id=item_id)


def _evidence_authority(authority, evidence_kind, role=""):
    if role == "assistant" or evidence_kind in ("assistant_analysis", "derived_analysis", "derived_summary", "derived_artifact"):
        return "derived"
    if role == "user" or (evidence_kind.startswith("user_") and evidence_kind != "user_provided_official_copy"):
        return "user"
    return authority or "unknown"


def _legacy_projections(c, item_id=None):
    """Yield one object's derived rows at a time. No raw file content is read."""
    def selection(sql, key, lookup=None):
        return c.execute(sql + (f" WHERE {key}=?" if item_id is not None else ""),
                         (item_id if lookup is None else lookup,) if item_id is not None else ()).fetchall()
    if _has(c, "documents"):
        for d in selection("SELECT d.*,s.authority FROM documents d JOIN sources s USING(source_id)", "d.document_id"):
            topic_ids = [r[0] for r in c.execute("SELECT topic_id FROM document_topics WHERE document_id=?", (d["document_id"],))]
            specs = []
            for v in c.execute("SELECT * FROM versions WHERE document_id=?", (d["document_id"],)).fetchall():
                version = v["version_id"]
                authority = _evidence_authority(d["authority"], d["evidence_kind"])
                specs.append(dict(text=d["title"], entry_key=f"legacy:version:{version}", heading=d["title"],
                    source_ref=v["raw_path"], version_id=version, is_current=v["is_current"],
                    authority=authority, evidence_kind=d["evidence_kind"], topics=topic_ids))
                for f in c.execute("SELECT * FROM fragments WHERE version_id=? ORDER BY sequence_no", (version,)):
                    source_ref = v["raw_path"]
                    if f["source_line_start"]:
                        source_ref += f"#L{f['source_line_start']}"
                    specs.append(dict(text=f["content"], entry_key=f"legacy:fragment:{f['fragment_row_id']}",
                        heading=f["heading"] or d["title"], source_ref=source_ref, version_id=version,
                        is_current=v["is_current"], authority=_evidence_authority(d["authority"], f["evidence_kind"], f["author_role"] or ""),
                        evidence_kind=f["evidence_kind"], author_role=f["author_role"] or "", topics=topic_ids))
            if not specs:
                specs = [dict(text=d["title"], entry_key="legacy:metadata", source_ref=d["source_path"] or d["canonical_url"] or "",
                              evidence_kind=d["evidence_kind"], authority=_evidence_authority(d["authority"], d["evidence_kind"]), topics=topic_ids)]
            yield d["document_id"], "document", d["title"], "school" if d["authority"] == "official" else "general", specs
    if _has(c, "research_advisors"):
        for row in selection("SELECT * FROM research_advisors", "advisor_id"):
            specs = [dict(text=_text(row, row.keys()), entry_key="legacy:advisor", heading=row["name"],
                          authority="derived", evidence_kind="derived_analysis",
                          source_ref=f"sqlite:research_advisors:{row['advisor_id']}")]
            for route in c.execute("""SELECT r.*,l.mapping_status,l.mapping_basis FROM research_routes r
                JOIN research_advisor_routes l USING(route_id) WHERE l.advisor_id=?""", (row["advisor_id"],)):
                specs.append(dict(text=_text(route, route.keys()), entry_key=f"legacy:route:{route['route_id']}",
                                  authority="derived", evidence_kind="derived_analysis", source_ref=f"sqlite:research_routes:{route['route_id']}"))
            yield row["advisor_id"], "advisor", f"{row['institution']} {row['name']}", "research", specs
    if _has(c, "career_positions"):
        for p in selection("SELECT p.*,cp.authority FROM career_positions p JOIN career_platforms cp USING(platform_id)", "p.position_id"):
            specs = []
            for s in c.execute("SELECT * FROM career_position_snapshots WHERE position_id=?", (p["position_id"],)).fetchall():
                specs.append(dict(text=_text(p, ["organization", "title", "location_text"]) + "\n" +
                    _text(s, ["published_or_updated_raw", "application_start_raw", "application_deadline_raw", "application_status_raw",
                              "salary_raw", "education_requirement_raw", "major_requirement_raw", "graduation_requirement_raw",
                              "job_description_text", "qualification_text"]),
                    entry_key=f"legacy:snapshot:{s['snapshot_id']}", version_id=s["snapshot_id"], is_current=s["is_current"],
                    source_ref=s["evidence_path"], authority=p["authority"], evidence_kind="source_record"))
            for a in c.execute("SELECT * FROM career_assessments WHERE position_id=?", (p["position_id"],)):
                specs.append(dict(text=_text(a, ["assessment_kind", "outcome", "rationale", "profile_context"]),
                    entry_key=f"legacy:assessment:{a['assessment_id']}", version_id=a["snapshot_id"] or "",
                    is_current=int(a["status"] == "current"), source_ref=f"sqlite:career_assessments:{a['assessment_id']}",
                    authority="derived", evidence_kind=a["evidence_kind"], author_role="assistant"))
            yield p["position_id"], "job", f"{p['organization']} {p['title']}", "career", specs
    generic = [("admission_targets", "target_id", "admission_target", "admissions", ["institution", "college", "program_name"], "derived"),
               ("research_routes", "route_id", "admission_route", "admissions", ["institution", "program_name"], "derived"),
               ("career_applications", "application_id", "career_application", "career", ["organization"], "user"),
               ("infrastructure_observations", "observation_id", "infrastructure_observation", "infrastructure", ["name", "observed_at"], "user"),
               ("events", "event_id", "event", "school", ["title"], "derived")]
    for table, key, kind, domain, title_keys, authority in generic:
        if not _has(c, table):
            continue
        if table == "research_routes" and item_id is not None and not item_id.startswith("route:"):
            continue
        lookup = item_id.removeprefix("route:") if table == "research_routes" and item_id is not None else item_id
        for row in selection(f"SELECT * FROM {table}", key, lookup):
            title = " ".join(str(row[k]) for k in title_keys if row[k])
            public_id = "route:" + row[key] if table == "research_routes" else row[key]
            yield public_id, kind, title, domain, [dict(text=_text(row, row.keys()), entry_key=f"legacy:{table}",
                source_ref=f"sqlite:{table}:{row[key]}", authority=authority,
                evidence_kind="user_statement" if authority == "user" else "derived_summary")]


def reindex_item(connection, item_id):
    """Refresh one legacy object's projections inside the caller's transaction.

    Route changes also refresh the linked advisors whose discovery text includes
    those routes. This function does not commit, bump revisions, alter metadata
    or create a second current-state record.
    """
    if not connection.in_transaction:
        raise sqlite3.ProgrammingError("reindex_item requires an active write transaction")
    affected = [item_id]
    if item_id.startswith("route:") and _has(connection, "research_advisor_routes"):
        affected.extend(r[0] for r in connection.execute(
            "SELECT advisor_id FROM research_advisor_routes WHERE route_id=?", (item_id.removeprefix("route:"),)))
    refreshed = []
    for ident in dict.fromkeys(affected):
        for owner, kind, title, domain, specs in _legacy_projections(connection, ident):
            _ensure(connection, owner, kind, title, domain=domain)
            # Names belong to their dedicated table; the public card is a
            # projection. Migration metadata, archive status and revision stay.
            connection.execute("UPDATE items SET title=? WHERE item_id=?", (title, owner))
            connection.execute("DELETE FROM search_entries WHERE item_id=? AND entry_key LIKE 'legacy:%'", (owner,))
            for spec in specs:
                core.index_entry(connection, owner, **spec)
            refreshed.append(owner)
    return refreshed


def rebuild_index(root, *, batch_size=100):
    """Recreate search projections in bounded write batches; sources stay intact.

    During a maintenance rebuild queries expose the incomplete rebuild marker.
    Original row values, migration metadata, locations and revisions are not
    overwritten. Repeating this operation is idempotent.
    """
    if not 1 <= batch_size <= 1000:
        raise ValueError("batch_size must be 1..1000")
    c = core.connect(root)
    count = 0
    started = core.now()
    def marker(state, **values):
        payload = {"state": state, "started_at": started, "updated_at": core.now(), **values}
        core.transaction(c, lambda db: db.execute("INSERT INTO schema_metadata(key,value) VALUES('v2_search_rebuild',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (core.json_text(payload),)))
    try:
        marker("in_progress")
        # Native content is re-read within each transaction. Concurrent ordinary
        # updates therefore cannot be replaced with a cached older projection.
        ids = [r[0] for r in c.execute("SELECT item_id FROM items").fetchall()]
        for item_id in ids:
            def native(db):
                item = db.execute("SELECT * FROM items WHERE item_id=?", (item_id,)).fetchone()
                for table, column, key in [("notes", "body", "main"), ("current_state", "state_json", "state"),
                                           ("profile_fields", "value_json", "main")]:
                    row = db.execute(f"SELECT * FROM {table} WHERE item_id=?", (item_id,)).fetchone()
                    if row:
                        value = row[column]
                        if table == "profile_fields":
                            value = row["field_key"] + "\n" + value
                        core.index_entry(db, item_id, value, entry_key=key, heading=item["title"], source_ref=row["source_ref"] or "")
                # Identity/navigation descriptions are searchable even when an
                # object has no attached document yet. Do not index private JSON
                # metadata indiscriminately as a second editable record.
                if item["kind"] in ("project", "workspace", "machine", "task", "object", "career_evidence"):
                    aliases = [r[0] for r in db.execute("SELECT alias FROM aliases WHERE item_id=?", (item_id,))]
                    core.index_entry(db, item_id, "\n".join([item["title"], item["description"], *aliases]), heading=item["title"])
                for change in db.execute("SELECT * FROM changes WHERE item_id=?", (item_id,)).fetchall():
                    if _json(change["before_json"]) is not None:
                        core.index_entry(db, item_id, change["before_json"], entry_key="change:" + change["change_id"],
                            version_id="revision:" + str(change["revision"] - 1), is_current=0,
                            heading=item["title"], source_ref="changes:" + change["change_id"] + "#before")
            core.transaction(c, native)
        for item_id, kind, title, domain, specs in _legacy_projections(c):
            def prepare(db):
                _ensure(db, item_id, kind, title, domain=domain)
                db.execute("DELETE FROM search_entries WHERE item_id=? AND entry_key LIKE 'legacy:%'", (item_id,))
            core.transaction(c, prepare)
            for start in range(0, len(specs), batch_size):
                batch = specs[start:start + batch_size]
                def write(db):
                    for spec in batch:
                        core.index_entry(db, item_id, **spec)
                core.transaction(c, write)
                count += len(batch)
        marker("complete", legacy_entries=count)
        return {"state": "complete", "legacy_entries": count, **_coverage(c)}
    except Exception:
        marker("failed", legacy_entries=count)
        raise
    finally:
        c.close()


def _legacy_module(root, filename):
    # --root selects data, while query implementations belong to this program.
    path = Path(__file__).resolve().parents[1] / "legacy_tools" / filename
    if not path.is_file():
        raise FileNotFoundError(f"Retained query tool missing: {path}")
    return _load_legacy(str(path.resolve()))


@lru_cache(maxsize=16)
def _load_legacy(path_text):
    path = Path(path_text)
    # Retained modules import sibling db.py. Loading is local and does not run
    # their CLI/main or their import/write commands. Isolate the sibling name
    # from another Python application's unrelated module named "db".
    with _LEGACY_LOAD_LOCK:
        previous = list(sys.path)
        previous_db = sys.modules.get("db")
        try:
            sys.path.insert(0, str(path.parent))
            db_spec = importlib.util.spec_from_file_location("db", path.parent / "db.py")
            db_module = importlib.util.module_from_spec(db_spec)
            sys.modules["db"] = db_module
            db_spec.loader.exec_module(db_module)
            name = "_pis_legacy_" + hashlib.sha256(str(path).encode()).hexdigest()[:16]
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            return module
        finally:
            sys.path[:] = previous
            if previous_db is None:
                sys.modules.pop("db", None)
            else:
                sys.modules["db"] = previous_db


def school_search(root, query, *, limit=20, topic=None, topic_mode="any", official_only=True):
    """Use the retained school evidence-ranking algorithm on the V2 database."""
    module = _legacy_module(root, "kb.py")
    c = core.connect(root, readonly=True)
    try:
        groups, _ = module.resolve_topic_groups(c, _values(topic))
        return [dict(row) for row in module.benchmark_search_rows(c, query, limit,
            topic_groups=groups, topic_mode=topic_mode, official_only=official_only)]
    finally:
        c.close()


def advisor_search(root, query, *, line=None, limit=20):
    """Preserve reviewed route matching and evidence-field ranking."""
    module = _legacy_module(root, "advisor_map.py")
    c = core.connect(root, readonly=True)
    try:
        return module.search(c, query, line)[:limit]
    finally:
        c.close()
