"""Read-only HTTP service for a local MA3 documentation workspace."""

from __future__ import annotations

import json
import mimetypes
import re
import sqlite3
from functools import partial
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from app.auth import AuthConfig, CHALLENGE
from app.catalog import AUTHORITIES, KINDS, ReferenceLimitError, brief, build_index, list_commands, list_references, open_database, revision_candidates, search, statistics
from app.sanitize import sanitize_html


class RequestError(ValueError):
    """An invalid client request, returned as JSON with HTTP 400."""


def project_file(root: Path, value: str, allowed: tuple[str, ...]) -> Path:
    candidate = (root / value).resolve()
    if not any(candidate.is_relative_to((root / directory).resolve()) for directory in allowed):
        raise RequestError("File is outside the permitted documentation directories")
    if not candidate.is_file():
        raise FileNotFoundError(value)
    return candidate


class Handler(BaseHTTPRequestHandler):
    server_version = "MA3Reference/1.0"

    def __init__(self, *args: Any, root: Path, database: Path, auth: AuthConfig | None = None, **kwargs: Any) -> None:
        self.root = root
        self.database = database
        self.auth = auth or AuthConfig.from_environment("127.0.0.1", {})
        super().__init__(*args, **kwargs)

    def log_message(self, format_string: str, *args: Any) -> None:
        # Keep normal search requests quiet; malformed requests are still reported.
        if args and str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
            super().log_message(format_string, *args)

    def send_bytes(self, content: bytes, content_type: str, status: int = 200, cache: bool = False, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header("Cache-Control", "private, max-age=3600" if cache else "no-store")
        if self.auth.enabled:
            self.send_header("Vary", "Authorization")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def send_json(self, value: Any, status: int = 200) -> None:
        self.send_bytes(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(), "application/json; charset=utf-8", status)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        try:
            parsed = urlsplit(self.path)
            if parsed.path == "/healthz":
                with open_database(self.database) as connection:
                    ready = connection.execute("SELECT 1 FROM metadata WHERE key='build'").fetchone() is not None
                self.send_json({"status": "ready" if ready else "unavailable"}, 200 if ready else 503)
                return
            authorization = self.headers.get_all("Authorization", [])
            if self.auth.enabled and (len(authorization) != 1 or not self.auth.is_authorized(authorization[0])):
                self.send_bytes(b'{"error":"Authentication required"}', "application/json; charset=utf-8", 401, headers={"WWW-Authenticate": CHALLENGE})
                return
            path = unquote(parsed.path)
            if "\x00" in path or "\\" in path:
                raise RequestError("Invalid path")
            if path.startswith("/api/"):
                if not self.database.exists():
                    self.send_json({"error": "The search index is not built yet", "hint": "Run python3 scripts/build_index.py"}, 503)
                    return
                self.api(path, parse_qs(parsed.query, keep_blank_values=True))
                return
            if path == "/":
                file = self.root / "web" / "index.html"
            elif path.startswith("/web/"):
                file = project_file(self.root, path.lstrip("/"), ("web",))
            elif path.startswith("/assets/"):
                file = project_file(self.root, path.lstrip("/"), ("assets",))
            elif path.startswith("/content/"):
                file = project_file(self.root, path.lstrip("/"), ("content",))
            elif path.startswith("/release-notes-pdf/"):
                file = project_file(self.root, path.lstrip("/"), ("release-notes-pdf",))
            elif path == "/favicon.ico":
                self.send_bytes(b"", "image/x-icon", 204)
                return
            else:
                raise FileNotFoundError(path)
            content_type = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
            if file.suffix == ".md":
                content_type = "text/markdown; charset=utf-8"
            # Generated HTML fragments are only exposed via the sanitizer API.
            if file.is_relative_to((self.root / "content").resolve()) and file.suffix.lower() in {".html", ".htm"}:
                raise RequestError("Read article HTML through /api/document")
            self.send_bytes(file.read_bytes(), content_type, cache=path.startswith("/assets/"))
        except RequestError as error:
            self.send_json({"error": str(error)}, 400)
        except FileNotFoundError:
            self.send_json({"error": "Document or file not found"}, 404)
        except ReferenceLimitError as error:
            self.send_json({"error": str(error)}, 503)
        except (sqlite3.Error, ValueError, OSError):
            self.send_json({"error": "The documentation index could not be read. Rebuild it and retry."}, 500)

    def parameter(self, values: dict[str, list[str]], name: str, default: str = "", maximum: int = 500) -> str:
        entries = values.get(name, [default])
        if len(entries) != 1 or len(entries[0]) > maximum:
            raise RequestError(f"Invalid {name} parameter")
        return entries[0].strip()

    def integer(self, values: dict[str, list[str]], name: str, default: int, minimum: int, maximum: int) -> int:
        value = self.parameter(values, name, str(default), 10)
        if not value.isdecimal() or not minimum <= int(value) <= maximum:
            raise RequestError(f"{name} must be between {minimum} and {maximum}")
        return int(value)

    def api(self, path: str, values: dict[str, list[str]]) -> None:
        with open_database(self.database) as connection:
            if path in {"/api/health", "/api/stats"}:
                result = statistics(connection)
                result["status"] = "ready"
                result["policy"] = "The imported manual is the reference baseline. Release notes retain their source versions."
                self.send_json(result)
            elif path == "/api/search":
                kind = self.parameter(values, "kind", "manual")
                if kind not in KINDS | {"all"}:
                    raise RequestError("kind must be manual, release_note, guide or all")
                self.send_json(search(connection, self.parameter(values, "q"), kind, self.parameter(values, "category"), self.parameter(values, "version"), self.integer(values, "limit", 30, 1, 100), self.integer(values, "offset", 0, 0, 100000), edition=self.parameter(values, "edition")))
            elif path == "/api/references":
                self.send_json(list_references(connection))
            elif path == "/api/commands":
                authority = self.parameter(values, "authority", "manual")
                record_type = self.parameter(values, "type", "keyword")
                reference_kind = self.parameter(values, "reference_kind", "all")
                sort = self.parameter(values, "sort", "source")
                if authority not in AUTHORITIES | {"all"} or record_type not in {"keyword", "syntax", "example", "all"}:
                    raise RequestError("Invalid command authority or type")
                if reference_kind not in {"all", "commands", "lua"} or sort not in {"source", "text"}:
                    raise RequestError("Invalid command reference kind or sort")
                self.send_json(list_commands(connection, self.parameter(values, "q"), authority, record_type, self.parameter(values, "document_id"), self.integer(values, "limit", 40, 1, 200), self.integer(values, "offset", 0, 0, 100000), keyword=self.parameter(values, "keyword", maximum=200), keyword_document_id=self.parameter(values, "keyword_document_id"), reference_kind=reference_kind, sort=sort))
            elif path == "/api/document":
                document_id = self.parameter(values, "id")
                row = connection.execute("SELECT payload FROM documents WHERE id=?", (document_id,)).fetchone()
                if row is None:
                    raise FileNotFoundError(document_id)
                document = json.loads(row["payload"])
                requested_format = self.parameter(values, "format", "json")
                if requested_format not in {"json", "markdown"}:
                    raise RequestError("format must be json or markdown")
                if requested_format == "markdown":
                    file = project_file(self.root, document.get("markdown_path", ""), ("content",))
                    self.send_bytes(file.read_bytes(), "text/markdown; charset=utf-8")
                    return
                html_path = document.get("html_path", "")
                if html_path:
                    fragment = project_file(self.root, html_path, ("content",)).read_text(encoding="utf-8")
                else:
                    fragment = "<p>" + escape(document.get("text", "")).replace("\n\n", "</p><p>") + "</p>"
                document["html"] = sanitize_html(fragment)
                reference = connection.execute("SELECT command FROM command_keywords WHERE document_id=?", (document_id,)).fetchone()
                document["command_lookup"] = reference["command"] if reference else ""
                document["command_reference_id"] = document_id if reference else ""
                document["commands_page"] = list_commands(connection, authority="all", record_type="all", document_id="" if reference else document_id, keyword=document["command_lookup"], keyword_document_id=document["command_reference_id"], limit=200)
                document["commands"] = document["commands_page"]["results"]
                document["related"] = self.related(connection, document)
                document["revision_candidates"] = revision_candidates(connection, document_id, limit=20)["results"]
                self.send_json(document)
            elif path == "/api/revisions":
                self.send_json(revision_candidates(connection, self.parameter(values, "document_id"), self.integer(values, "limit", 30, 1, 100), self.integer(values, "offset", 0, 0, 100000)))
            elif path == "/api/navigation":
                file = self.root / "data" / "manual" / "navigation.json"
                navigation = json.loads(file.read_text(encoding="utf-8")) if file.exists() else []
                releases = [brief(row) for row in connection.execute("SELECT * FROM documents WHERE category='release-document' ORDER BY version DESC,title")]
                guides = [brief(row) for row in connection.execute("SELECT * FROM documents WHERE kind='guide' AND category NOT IN ('policy','lua') ORDER BY title")]
                self.send_json({"manual": navigation, "releases": releases, "guides": guides})
            else:
                raise FileNotFoundError(path)

    def related(self, connection: sqlite3.Connection, document: dict[str, Any]) -> list[dict[str, Any]]:
        if document.get("authority") != "manual":
            return []
        title = document.get("title", "")
        title = re.sub(r"\s+(Keyword|Function)$", "", title, flags=re.IGNORECASE)
        if len(title) < 4 or title.casefold() in {"help", "introduction", "overview", "examples", "syntax"}:
            return []
        related = search(connection, title, kind="release_note", limit=5)["results"]
        for item in related:
            item["relation"] = "Text match; historical context, not verified current behaviour"
        return related


def serve(root: Path, database: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    auth = AuthConfig.from_environment(host)
    if not database.exists():
        build_index(root, database, allow_empty=True)
    handler = partial(Handler, root=root, database=database, auth=auth)
    server = ThreadingHTTPServer((host, port), handler)
    print(f"MA3 Reference ready at http://{host}:{server.server_address[1]}", flush=True)
    print("Read-only local service. Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
