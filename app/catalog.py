"""Validated corpus indexing and version-aware reference retrieval."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from app.command_links import CommandReferences

KINDS = {"manual", "release_note", "guide"}
AUTHORITIES = {"manual", "release-note", "editorial"}
DOCUMENT_FIELDS = ("id", "title", "kind", "category", "version", "authority", "text")


class ReferenceLimitError(ValueError):
    """The complete reference vocabulary exceeds the response bound."""


def fold(value: str) -> str:
    return " ".join(value.casefold().split())


def read_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_no}: expected a JSON object")
        records.append(value)
    return records


def validate_document(record: dict[str, Any]) -> None:
    for field in DOCUMENT_FIELDS:
        if not isinstance(record.get(field), str):
            raise ValueError(f"Document {record.get('id')!r}: {field} must be a string")
    if record["kind"] not in KINDS or record["authority"] not in AUTHORITIES:
        raise ValueError(f"Document {record['id']}: invalid kind or authority")
    if record["kind"] == "release_note" and record["authority"] == "manual":
        raise ValueError(f"Historical document {record['id']} cannot have manual authority")
    if not record["id"] or len(record["id"]) > 500:
        raise ValueError("Document IDs must be 1–500 characters")
    aliases = record.get("aliases", [])
    if not isinstance(aliases, list) or any(not isinstance(item, str) for item in aliases):
        raise ValueError(f"Document {record['id']}: aliases must be a string array")
    if "searchable" in record and not isinstance(record["searchable"], bool):
        raise ValueError(f"Document {record['id']}: searchable must be boolean")


def validate_command(record: dict[str, Any], document_ids: set[str]) -> None:
    for field in ("id", "document_id", "command", "text", "type", "version", "authority"):
        if not isinstance(record.get(field), str):
            raise ValueError(f"Command {record.get('id')!r}: {field} must be a string")
    if record["document_id"] not in document_ids:
        raise ValueError(f"Command {record['id']}: unknown document {record['document_id']}")
    if record["type"] not in {"keyword", "syntax", "example"}:
        raise ValueError(f"Command {record['id']}: invalid type")
    if record["authority"] not in AUTHORITIES:
        raise ValueError(f"Command {record['id']}: invalid authority")


def version_memberships(record: dict[str, Any]) -> set[tuple[str, str]]:
    versions = {(record["version"], "document")}
    publication = record.get("publication_version")
    if isinstance(publication, str) and publication:
        versions.add((publication, "edition"))
    for field, role in (("versions", "release"), ("publication_versions", "edition")):
        values = record.get(field, [])
        if isinstance(values, list):
            versions.update((value, role) for value in values if isinstance(value, str) and value)
    occurrences = record.get("occurrences", [])
    if isinstance(occurrences, list):
        for occurrence in occurrences:
            if not isinstance(occurrence, dict):
                continue
            for field, role in (("version", "release"), ("publication_version", "edition")):
                value = occurrence.get(field)
                if isinstance(value, str) and value:
                    versions.add((value, role))
    return versions


def build_index(root: Path, destination: Path | None = None, *, allow_empty: bool = False) -> dict[str, Any]:
    started = time.perf_counter()
    destination = destination or root / ".cache" / "catalog.sqlite"
    destination.parent.mkdir(parents=True, exist_ok=True)
    documents = []
    commands = []
    manifests = []
    for section in ("manual", "releases", "guides", "live-lua"):
        for filename, collection in (("documents.jsonl", documents), ("commands.jsonl", commands)):
            path = root / "data" / section / filename
            collection.extend(read_records(path))
            if path.exists():
                manifests.append({"path": str(path.relative_to(root)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    if not documents and not allow_empty:
        raise ValueError("No corpus documents found. Run the ingestion scripts first.")
    token_metadata = read_records(root / "data/live-lua/keyword-metadata.jsonl")
    function_metadata = read_records(root / "data/live-lua/functions.jsonl")
    live_functions = {}
    for function in function_metadata:
        if not isinstance(function.get("name"), str) or not isinstance(function.get("signature"), str) or function.get("scope") not in {"free", "object"}:
            raise ValueError("Invalid live Lua function metadata")
        identifier = function.get("manual_document_id")
        if identifier is not None:
            if not isinstance(identifier, str):
                raise ValueError("Invalid live Lua document identity")
            live_functions[identifier] = function
    tokens_by_document: dict[str, list[dict[str, Any]]] = {}
    for token in token_metadata:
        if not isinstance(token.get("index"), int) or not isinstance(token.get("name"), str) or not isinstance(token.get("manual_document_ids"), list):
            raise ValueError("Invalid live keyword metadata")
        for identifier in token["manual_document_ids"]:
            if not isinstance(identifier, str):
                raise ValueError("Invalid live keyword document identity")
            tokens_by_document.setdefault(identifier, []).append(token)
    for document in documents:
        if document["id"] in tokens_by_document:
            document["live_keyword_tokens"] = tokens_by_document[document["id"]]
        if document["id"] in live_functions:
            document["live_lua_signature"] = live_functions[document["id"]]
    for filename in ("keyword-metadata.jsonl", "functions.jsonl"):
        path = root / "data/live-lua" / filename
        if path.exists():
            manifests.append({"path": str(path.relative_to(root)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    document_ids: set[str] = set()
    for record in documents:
        validate_document(record)
        if record["id"] in document_ids:
            raise ValueError(f"Duplicate document ID: {record['id']}")
        document_ids.add(record["id"])
    command_ids: set[str] = set()
    for record in commands:
        validate_command(record, document_ids)
        if record["id"] in command_ids:
            raise ValueError(f"Duplicate command ID: {record['id']}")
        command_ids.add(record["id"])
    keyword_index = CommandReferences(documents, commands)
    revisions_path = root / "data" / "releases" / "possible-revisions.jsonl"
    revisions = read_records(revisions_path)
    for revision in revisions:
        if revision.get("left_id") not in document_ids or revision.get("right_id") not in document_ids or not isinstance(revision.get("similarity"), (float, int)):
            raise ValueError("Invalid possible-revision source record")
    temporary = destination.with_suffix(".sqlite.new")
    temporary.unlink(missing_ok=True)
    connection = sqlite3.connect(temporary)
    try:
        connection.executescript("""
            PRAGMA journal_mode=DELETE;
            CREATE TABLE documents (
                id TEXT UNIQUE NOT NULL, title TEXT NOT NULL, title_fold TEXT NOT NULL,
                kind TEXT NOT NULL, category TEXT NOT NULL, version TEXT NOT NULL,
                authority TEXT NOT NULL, searchable INTEGER NOT NULL,
                text TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE INDEX document_filters ON documents(kind, category, version, searchable);
            CREATE TABLE terms (term TEXT NOT NULL, document_id TEXT NOT NULL, UNIQUE(term, document_id));
            CREATE INDEX exact_terms ON terms(term);
            CREATE TABLE document_versions (document_id TEXT NOT NULL, version TEXT NOT NULL, role TEXT NOT NULL, UNIQUE(document_id,version,role));
            CREATE INDEX version_lookup ON document_versions(version,document_id);
            CREATE VIRTUAL TABLE documents_fts USING fts5(title, aliases, body, content='', tokenize='porter unicode61 remove_diacritics 2');
            CREATE TABLE commands (
                id TEXT PRIMARY KEY, document_id TEXT NOT NULL, command TEXT NOT NULL,
                command_fold TEXT NOT NULL, text TEXT NOT NULL, type TEXT NOT NULL,
                authority TEXT NOT NULL, version TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE INDEX command_lookup ON commands(command_fold, authority, type);
            CREATE INDEX command_document ON commands(document_id);
            CREATE TABLE command_keywords (document_id TEXT PRIMARY KEY, command TEXT NOT NULL, command_fold TEXT NOT NULL, category TEXT NOT NULL);
            CREATE INDEX keyword_name ON command_keywords(command_fold);
            CREATE TABLE command_keyword_terms (term TEXT NOT NULL, keyword_document_id TEXT NOT NULL, origin TEXT NOT NULL, PRIMARY KEY(term,keyword_document_id));
            CREATE TABLE command_references (command_id TEXT NOT NULL, keyword_document_id TEXT NOT NULL, matched_tokens TEXT NOT NULL, relation TEXT NOT NULL, PRIMARY KEY(command_id,keyword_document_id));
            CREATE INDEX reference_keyword ON command_references(keyword_document_id,command_id);
            CREATE TABLE revisions (left_id TEXT NOT NULL, right_id TEXT NOT NULL, similarity REAL NOT NULL, UNIQUE(left_id,right_id));
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        for record in documents:
            payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            cursor = connection.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?)", (
                record["id"], record["title"], fold(record["title"]), record["kind"], record["category"], record["version"],
                record["authority"], int(record.get("searchable", True)), record["text"], payload,
            ))
            aliases = record.get("aliases", [])
            connection.execute("INSERT INTO documents_fts(rowid,title,aliases,body) VALUES (?,?,?,?)", (
                cursor.lastrowid, record["title"], " ".join(aliases), record["text"],
            ))
            for term in [record["title"], *aliases]:
                connection.execute("INSERT OR IGNORE INTO terms VALUES (?,?)", (fold(term), record["id"]))
            for version, role in sorted(version_memberships(record)):
                connection.execute("INSERT INTO document_versions VALUES (?,?,?)", (record["id"], version, role))
        for reference in keyword_index.references.values():
            connection.execute("INSERT INTO command_keywords VALUES (?,?,?,?)", (reference["document_id"], reference["command"], reference["command_fold"], reference["category"]))
        for (term, identifier), origin in sorted(keyword_index.term_origins.items()):
            connection.execute("INSERT INTO command_keyword_terms VALUES (?,?,?)", (term, identifier, origin))
        relationship_count = 0
        related_records = 0
        for record in commands:
            connection.execute("INSERT INTO commands VALUES (?,?,?,?,?,?,?,?,?)", (
                record["id"], record["document_id"], record["command"], fold(record["command"]), record["text"],
                record["type"], record["authority"], record["version"], json.dumps(record, ensure_ascii=False),
            ))
            if record["command"] and record["type"] == "keyword":
                connection.execute("INSERT OR IGNORE INTO terms VALUES (?,?)", (fold(record["command"]), record["document_id"]))
            relationships = keyword_index.relationships(record)
            relationship_count += len(relationships)
            related_records += bool(relationships)
            for relationship in relationships:
                connection.execute("INSERT INTO command_references VALUES (?,?,?,?)", (record["id"], relationship["keyword_document_id"], json.dumps(relationship["matched_tokens"], ensure_ascii=False), relationship["relation"]))
        for revision in revisions:
            connection.execute("INSERT INTO revisions VALUES (?,?,?)", (revision["left_id"], revision["right_id"], revision["similarity"]))
        summary = {
            "documents": len(documents), "commands": len(commands), "sources": manifests,
            "build_seconds": round(time.perf_counter() - started, 3), "schema_version": 2,
            "command_relationships": relationship_count, "commands_with_relationships": related_records,
            "command_reference_policy": "Current canonical keywords and documented shortcuts in source extracts; quoted values excluded; Lua calls and literal Cmd arguments handled separately. Lexical evidence, not syntax validation or exhaustive prose extraction.",
        }
        connection.execute("INSERT INTO metadata VALUES ('build',?)", (json.dumps(summary),))
        connection.execute("INSERT INTO documents_fts(documents_fts) VALUES ('optimize')")
        connection.commit()
    finally:
        connection.close()
    temporary.replace(destination)
    return summary


def open_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def excerpt(text: str, query: str, length: int = 240) -> str:
    clean = " ".join(text.split())
    words = re.findall(r"[\w]+", query, flags=re.UNICODE)
    positions = [clean.casefold().find(word.casefold()) for word in words]
    found = [position for position in positions if position >= 0]
    start = max(0, min(found) - 65) if found else 0
    return ("…" if start else "") + clean[start:start + length] + ("…" if start + length < len(clean) else "")


def brief(row: sqlite3.Row, query: str = "", edition: str = "") -> dict[str, Any]:
    value = json.loads(row["payload"])
    result = {field: value.get(field) for field in ("id", "title", "kind", "category", "version", "authority", "breadcrumbs", "source_url", "source_page", "parent_id", "section", "status")}
    result["excerpt"] = excerpt(value.get("text", ""), query)
    occurrences = value.get("occurrences", [])
    result["occurrence_count"] = len(occurrences) if isinstance(occurrences, list) else 0
    if edition:
        result["matched_edition"] = edition
        result["matched_occurrences"] = [
            {field: occurrence.get(field) for field in ("version", "publication_version", "document_id", "source_url", "source_page", "section")}
            for occurrence in occurrences if isinstance(occurrence, dict) and occurrence.get("publication_version") == edition
        ] if isinstance(occurrences, list) else []
    return result


def apply_version_match(connection: sqlite3.Connection, item: dict[str, Any], version: str) -> dict[str, Any]:
    if version:
        item["matched_version"] = version
        item["version_match_roles"] = [row[0] for row in connection.execute("SELECT role FROM document_versions WHERE document_id=? AND version=? ORDER BY role", (item["id"], version))]
    return item


def search(connection: sqlite3.Connection, query: str, kind: str = "manual", category: str = "", version: str = "", limit: int = 30, offset: int = 0, edition: str = "") -> dict[str, Any]:
    started = time.perf_counter()
    clauses = ["searchable=1"]
    parameters: list[Any] = []
    if kind != "all":
        clauses.append("kind=?")
        parameters.append(kind)
    if category:
        clauses.append("category=?")
        parameters.append(category)
    if version:
        clauses.append("EXISTS (SELECT 1 FROM document_versions WHERE document_id=documents.id AND document_versions.version=?)")
        parameters.append(version)
    if edition:
        clauses.append("EXISTS (SELECT 1 FROM document_versions WHERE document_id=documents.id AND document_versions.version=? AND role='edition')")
        parameters.append(edition)
    query = query.strip()
    if not query:
        where = " AND ".join(clauses)
        total = connection.execute(f"SELECT count(*) FROM documents WHERE {where}", parameters).fetchone()[0]
        rows = connection.execute(f"SELECT * FROM documents WHERE {where} ORDER BY CASE authority WHEN 'manual' THEN 0 WHEN 'editorial' THEN 1 ELSE 2 END, title_fold, id LIMIT ? OFFSET ?", [*parameters, limit, offset]).fetchall()
        return {"query": query, "results": [apply_version_match(connection, brief(row, edition=edition), version) for row in rows], "total": total, "offset": offset, "limit": limit, "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)}
    exact = fold(query)
    words = re.findall(r"[\w]+", query, flags=re.UNICODE)[:20]
    fts_query = " AND ".join('"' + word.replace('"', '""') + '"*' for word in words)
    scores: dict[int, float] = {}
    if fts_query:
        for row in connection.execute("SELECT rowid, bm25(documents_fts, 12, 8, 1) AS rank FROM documents_fts WHERE documents_fts MATCH ?", (fts_query,)):
            scores[row["rowid"]] = row["rank"]
    exact_ids = {row[0] for row in connection.execute("SELECT document_id FROM terms WHERE term=?", (exact,))}
    matching_ids = list(scores)
    alternatives = []
    if matching_ids:
        # Avoid SQLite parameter limits with a JSON table; json_each is bundled in modern SQLite.
        alternatives.append("rowid IN (SELECT value FROM json_each(?))")
        parameters.append(json.dumps(matching_ids))
    if exact_ids:
        alternatives.append("id IN (SELECT value FROM json_each(?))")
        parameters.append(json.dumps(sorted(exact_ids)))
    if not alternatives:
        return {"query": query, "results": [], "total": 0, "offset": offset, "limit": limit, "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)}
    clauses.append("(" + " OR ".join(alternatives) + ")")
    rows = connection.execute("SELECT rowid AS _rowid, * FROM documents WHERE " + " AND ".join(clauses), parameters).fetchall()
    rows = sorted(rows, key=lambda row: (
        {"manual": 0, "editorial": 1, "release-note": 2}.get(row["authority"], 3),
        0 if row["id"] in exact_ids else 1,
        0 if row["id"] in exact_ids and row["category"] in {"command", "option", "lua"} else 1,
        0 if row["title_fold"].startswith(exact) else 1,
        scores.get(row["_rowid"], 0), row["title_fold"], row["id"],
    ))
    results = []
    for row in rows[offset:offset + limit]:
        item = apply_version_match(connection, brief(row, query, edition), version)
        item["exact_match"] = row["id"] in exact_ids
        results.append(item)
    return {"query": query, "results": results, "total": len(rows), "offset": offset, "limit": limit, "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)}


def list_references(connection: sqlite3.Connection) -> dict[str, Any]:
    rows = connection.execute("SELECT k.document_id,k.command,k.category,json_extract(d.payload,'$.parent_id') AS scope FROM command_keywords k JOIN documents d ON d.id=k.document_id ORDER BY k.document_id LIMIT 2001").fetchall()
    if len(rows) > 2000:
        raise ReferenceLimitError("The command reference vocabulary exceeds its response limit")
    references = {}
    for row in rows:
        item = {key: row[key] for key in ("document_id", "command", "category")}
        item["terms"] = [{"term": row["command"], "origin": "canonical"}]
        if row["category"] == "lua" and isinstance(row["scope"], str) and row["scope"]:
            item["scope"] = row["scope"]
        references[row["document_id"]] = item
    identifiers = json.dumps(list(references))
    for row in connection.execute("SELECT keyword_document_id,term,origin FROM command_keyword_terms WHERE origin='documented-shortcut' AND keyword_document_id IN (SELECT value FROM json_each(?)) ORDER BY keyword_document_id,term", (identifiers,)):
        references[row["keyword_document_id"]]["terms"].append({"term": row["term"], "origin": row["origin"]})
    return {"references": list(references.values())}


def list_commands(connection: sqlite3.Connection, query: str = "", authority: str = "manual", record_type: str = "keyword", document_id: str = "", limit: int = 40, offset: int = 0, keyword: str = "", keyword_document_id: str = "", reference_kind: str = "all", sort: str = "source") -> dict[str, Any]:
    clauses = []
    parameters: list[Any] = []
    keyword_matches = []
    if reference_kind not in {"all", "commands", "lua"} or sort not in {"source", "text"}:
        raise ValueError("Invalid command reference kind or sort")
    if reference_kind != "all":
        lua = "(coalesce(json_extract(payload,'$.language'),'')='lua' OR document_id IN (SELECT id FROM documents WHERE category='lua'))"
        clauses.append(lua if reference_kind == "lua" else f"NOT {lua}")
    if keyword or keyword_document_id:
        if keyword_document_id:
            keyword_matches = [dict(row) for row in connection.execute("SELECT *, 'document-id' AS origin FROM command_keywords WHERE document_id=?", (keyword_document_id,))]
        else:
            keyword_matches = [dict(row) for row in connection.execute("SELECT k.*,t.origin FROM command_keyword_terms t JOIN command_keywords k ON k.document_id=t.keyword_document_id WHERE t.term=? ORDER BY k.document_id", (fold(keyword),))]
            canonical = [item for item in keyword_matches if item["origin"] == "canonical"]
            keyword_matches = canonical or keyword_matches
            ma_keywords = [item for item in keyword_matches if item["category"] != "lua"]
            keyword_matches = ma_keywords or keyword_matches
        if len(keyword_matches) != 1:
            keyword_matches = []
        clauses.append("id IN (SELECT command_id FROM command_references WHERE keyword_document_id IN (SELECT value FROM json_each(?)))")
        parameters.append(json.dumps([item["document_id"] for item in keyword_matches]))
    for column, value in (("authority", authority), ("type", record_type), ("document_id", document_id)):
        if value and value != "all":
            clauses.append(f"{column}=?")
            parameters.append(value)
    if query:
        clauses.append("(command_fold LIKE ? ESCAPE '\\' OR lower(text) LIKE ? ESCAPE '\\' OR document_id IN (SELECT document_id FROM terms WHERE term=?))")
        escaped = fold(query).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        parameters.extend(["%" + escaped + "%", "%" + escaped + "%", fold(query)])
    where = " AND ".join(clauses) or "1"
    group_key = "json_array(CASE WHEN type='keyword' THEN id ELSE '' END,text,coalesce(nullif(json_extract(payload,'$.language'),'text'),'ma3'))"
    counts = connection.execute(f"SELECT count(DISTINCT {group_key}),count(*) FROM commands WHERE {where}", parameters).fetchone()
    total, record_total = counts
    own_ids = json.dumps([item["document_id"] for item in keyword_matches])
    order = "CASE WHEN document_id IN (SELECT value FROM json_each(?)) THEN 0 ELSE 1 END, CASE authority WHEN 'manual' THEN 0 ELSE 1 END, (SELECT title_fold FROM documents WHERE documents.id=commands.document_id), document_id, CASE type WHEN 'keyword' THEN 0 WHEN 'syntax' THEN 1 ELSE 2 END, id" if keyword or keyword_document_id else "CASE WHEN command_fold=? THEN 0 WHEN document_id IN (SELECT document_id FROM terms WHERE term=?) THEN 1 ELSE 2 END, CASE authority WHEN 'manual' THEN 0 ELSE 1 END, command_fold, type, id"
    order_parameters = [own_ids] if keyword or keyword_document_id else [fold(query), fold(query)]
    if sort == "text":
        order = "lower(text),text," + order
    rows = connection.execute(f"""
        WITH filtered AS (
            SELECT payload,{group_key} AS group_key,row_number() OVER (ORDER BY {order}) AS position
            FROM commands WHERE {where}
        ), selected_groups AS (
            SELECT group_key,min(position) AS group_position FROM filtered
            GROUP BY group_key ORDER BY group_position LIMIT ? OFFSET ?
        )
        SELECT f.payload,g.group_position FROM filtered f JOIN selected_groups g ON g.group_key=f.group_key
        ORDER BY g.group_position,f.position
    """, [*order_parameters, *parameters, limit, offset]).fetchall()
    results = [json.loads(row["payload"]) for row in rows]
    source_ids = json.dumps(sorted({item["document_id"] for item in results}))
    sources = {row["id"]: row["title"] for row in connection.execute("SELECT id,title FROM documents WHERE id IN (SELECT value FROM json_each(?))", (source_ids,))}
    links: dict[str, list[dict[str, Any]]] = {}
    result_ids = json.dumps([item["id"] for item in results])
    for row in connection.execute("SELECT r.*,k.command,k.category FROM command_references r JOIN command_keywords k ON k.document_id=r.keyword_document_id WHERE r.command_id IN (SELECT value FROM json_each(?)) ORDER BY k.command_fold,k.document_id", (result_ids,)):
        links.setdefault(row["command_id"], []).append({"keyword_document_id": row["keyword_document_id"], "command": row["command"], "category": row["category"], "matched_tokens": json.loads(row["matched_tokens"]), "relation": row["relation"]})
    for item in results:
        item["source_document_id"] = item["document_id"]
        item["source_document_title"] = sources[item["document_id"]]
        item["source_document_url"] = "/?doc=" + quote(item["document_id"], safe="")
        item["matched_keywords"] = links.get(item["id"], [])
    groups: dict[int, list[dict[str, Any]]] = {}
    for row, item in zip(rows, results):
        groups.setdefault(row["group_position"], []).append(item)
    grouped_results = [{**members[0], "source_records": members, "source_record_count": len(members)} for members in groups.values()]
    return {"results": grouped_results, "total": total, "record_total": record_total, "limit": limit, "offset": offset, "query": query, "keyword": keyword, "keyword_document_id": keyword_document_id, "keyword_matches": keyword_matches, "reference_kind": reference_kind, "sort": sort}


def statistics(connection: sqlite3.Connection) -> dict[str, Any]:
    build = json.loads(connection.execute("SELECT value FROM metadata WHERE key='build'").fetchone()[0])
    for label, column in (("kinds", "kind"), ("categories", "category"), ("versions", "version"), ("authorities", "authority")):
        build[label] = [dict(row) for row in connection.execute(f"SELECT {column} AS name,count(*) AS count FROM documents GROUP BY {column} ORDER BY count DESC")]
    build["versions"] = [dict(row) for row in connection.execute("SELECT version AS name,count(DISTINCT document_id) AS count FROM document_versions GROUP BY version ORDER BY version DESC")]
    build["keyword_count"] = connection.execute("SELECT count(*) FROM commands WHERE type='keyword' AND authority='manual'").fetchone()[0]
    build["example_count"] = connection.execute("SELECT count(*) FROM commands WHERE type='example'").fetchone()[0]
    build["manual_count"] = connection.execute("SELECT count(*) FROM documents WHERE authority='manual'").fetchone()[0]
    build["release_entry_count"] = connection.execute("SELECT count(*) FROM documents WHERE id LIKE 'release-entry/%'").fetchone()[0]
    build["pdf_count"] = connection.execute("SELECT count(*) FROM documents WHERE category='release-document'").fetchone()[0]
    build["revision_candidate_count"] = connection.execute("SELECT count(*) FROM revisions").fetchone()[0]
    build["has_lua_coverage"] = connection.execute("SELECT 1 FROM documents WHERE id='live-lua/coverage-audit'").fetchone() is not None
    return build


def revision_candidates(connection: sqlite3.Connection, document_id: str = "", limit: int = 30, offset: int = 0) -> dict[str, Any]:
    where = "WHERE left_id=? OR right_id=?" if document_id else ""
    params = [document_id, document_id] if document_id else []
    total = connection.execute(f"SELECT count(*) FROM revisions {where}", params).fetchone()[0]
    results = []
    for row in connection.execute(f"SELECT * FROM revisions {where} ORDER BY similarity DESC,left_id,right_id LIMIT ? OFFSET ?", [*params, limit, offset]):
        pair = {"similarity": row["similarity"], "status": "possible_wording_revision_not_an_established_conflict"}
        for side in ("left", "right"):
            source = connection.execute("SELECT * FROM documents WHERE id=?", (row[side + "_id"],)).fetchone()
            pair[side] = brief(source)
        results.append(pair)
    return {"results": results, "total": total, "limit": limit, "offset": offset}
