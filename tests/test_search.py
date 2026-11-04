"""Behavioural checks for retrieval authority, literal syntax and HTTP boundaries."""

import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from functools import partial
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

from app.auth import AuthConfig
from app.catalog import ReferenceLimitError, build_index, list_commands, list_references, open_database, search
from app.command_links import CommandReferences
from app.sanitize import sanitize_html
from app.server import Handler


class ReferenceFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "data" / "manual").mkdir(parents=True)
        (self.root / "content" / "manual").mkdir(parents=True)
        (self.root / "web").mkdir()
        (self.root / "web" / "index.html").write_text("<!doctype html><title>Reference</title>")
        (self.root / "content" / "manual" / "go.html").write_text('<h2>Syntax</h2><p>Go+ Sequence 1</p><script>alert(1)</script>')
        (self.root / "content" / "manual" / "go.md").write_text("# Go+\n\n`Go+ Sequence 1`\n")
        documents = [
            self.document("manual/go", "Go+ Keyword", "manual", "command", "Go+ executes the next cue in a sequence.", ["Go+", ">>>"]),
            self.document("manual/go-key", "Go+ Key", "manual", "topic", "Use this key to go to the next cue.", ["Go+"]),
            self.document("manual/store", "Store Keyword", "manual", "command", "Store cues, presets and macros.", ["Store", "S"]),
            self.document("release-entry/go", "Go+", "release_note", "bug-fixes", "Go+ Go+ Go+ previous behaviour and historic changes.", []),
            self.document("guide/go", "Go+ practical guide", "guide", "guide", "Go+ sequence actions explained.", []),
        ]
        documents[0]["html_path"] = "content/manual/go.html"
        documents[0]["markdown_path"] = "content/manual/go.md"
        documents[3]["occurrences"] = [{"version": "1.9", "publication_version": "1.9", "source_page": 7}, {"version": "1.9", "publication_version": "2.0", "source_page": 9}]
        self.documents = documents
        self.write_documents()
        commands = [{"id": "go/keyword", "document_id": "manual/go", "command": "Go+", "text": "Go+", "type": "keyword", "authority": "manual", "version": "2.5"}, {"id": "go/example", "document_id": "manual/go", "command": "Go+", "text": "Go+ Sequence 1", "type": "example", "authority": "manual", "version": "2.5"}, {"id": "store/keyword", "document_id": "manual/store", "command": "Store", "text": "Store", "type": "keyword", "authority": "manual", "version": "2.5"}]
        self.commands = commands
        (self.root / "data" / "manual" / "commands.jsonl").write_text("\n".join(json.dumps(item) for item in commands))
        self.database = self.root / ".cache" / "catalog.sqlite"
        build_index(self.root)
        self.connection = open_database(self.database)

    def tearDown(self):
        self.connection.close()
        self.directory.cleanup()

    def document(self, identifier, title, kind, category, text, aliases):
        return {"id": identifier, "title": title, "kind": kind, "category": category, "version": "1.9" if kind == "release_note" else "2.5", "authority": {"manual": "manual", "release_note": "release-note", "guide": "editorial"}[kind], "text": text, "aliases": aliases, "searchable": True}

    def write_documents(self):
        (self.root / "data" / "manual" / "documents.jsonl").write_text("\n".join(json.dumps(item) for item in self.documents))


class ReferenceTests(ReferenceFixture):
    def test_source_free_first_launch_and_missing_source_rebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build_index(root, allow_empty=True)
            with closing(open_database(root / ".cache/catalog.sqlite")) as connection:
                self.assertEqual(search(connection, "sample")["total"], 0)
                self.assertEqual(list_references(connection)["references"], [])

        (self.root / "data/manual/documents.jsonl").unlink()
        before = self.database.read_bytes()
        with self.assertRaisesRegex(ValueError, "No corpus documents"):
            build_index(self.root)
        self.assertEqual(self.database.read_bytes(), before)

    def test_live_lua_additions_preserve_editorial_authority_and_keyword_provenance(self):
        section = self.root / "data/live-lua"
        section.mkdir()
        live = self.document("live-lua/free-livefunction", "LiveFunction()", "guide", "lua", "Live signature only", [])
        live.update(source_kind="live-introspection", command="LiveFunction", parent_id="manual/lua_objectfree")
        (section / "documents.jsonl").write_text(json.dumps(live))
        (section / "commands.jsonl").write_text(json.dumps({"id": "live/keyword", "document_id": live["id"], "command": "LiveFunction", "text": "LiveFunction", "type": "keyword", "authority": "editorial", "version": "2.5.1.1", "language": "lua"}))
        token = {"index": 7, "name": "Store", "manual_document_ids": ["manual/store"], "behaviour_verified": False}
        (section / "keyword-metadata.jsonl").write_text(json.dumps(token))
        build_index(self.root)
        with open_database(self.database) as connection:
            refs = list_references(connection)["references"]
            self.assertTrue(any(item["document_id"] == live["id"] for item in refs))
            value = list_commands(connection, authority="all", reference_kind="lua")["results"][0]
            self.assertEqual(value["authority"], "editorial")
            self.assertEqual(list_commands(connection, reference_kind="lua")["total"], 0)
            stored = json.loads(connection.execute("SELECT payload FROM documents WHERE id='manual/store'").fetchone()[0])
            self.assertEqual(stored["live_keyword_tokens"], [token])
            self.assertEqual(stored["authority"], "manual")

    def test_reference_vocabulary_is_bounded_without_silently_truncating(self):
        with closing(sqlite3.connect(self.database)) as writable:
            for index in range(2000):
                identifier = f"manual/reference-{index:04d}"
                writable.execute("INSERT INTO documents SELECT ?,title,title_fold,kind,category,version,authority,searchable,text,payload FROM documents WHERE id='manual/store'", (identifier,))
                writable.execute("INSERT INTO command_keywords VALUES (?,'Store','store','command')", (identifier,))
            writable.commit()
        with self.assertRaises(ReferenceLimitError):
            list_references(self.connection)

    def test_exact_keyword_beats_key_and_historical_text(self):
        result = search(self.connection, "Go+", kind="all")
        self.assertEqual(result["results"][0]["id"], "manual/go")
        authorities = [item["authority"] for item in result["results"]]
        self.assertLess(authorities.index("editorial"), authorities.index("release-note"))

    def test_punctuation_only_alias_is_not_lost_to_tokenization(self):
        result = search(self.connection, ">>>")
        self.assertEqual([item["id"] for item in result["results"]], ["manual/go"])

    def test_default_scope_does_not_include_history(self):
        result = search(self.connection, "Go+")
        self.assertTrue(all(item["authority"] == "manual" for item in result["results"]))

    def test_case_insensitive_command_lookup_keeps_punctuation(self):
        result = list_commands(self.connection, "go+", record_type="all")
        self.assertEqual(result["total"], 2)
        self.assertIn("Go+ Sequence 1", [item["text"] for item in result["results"]])
        self.assertEqual(list_commands(self.connection, "%", record_type="all")["total"], 0)

    def test_exact_command_alias_ranks_its_canonical_keyword_first(self):
        result = list_commands(self.connection, "S")
        self.assertEqual(result["results"][0]["command"], "Store")

    def test_fts_metacharacters_do_not_become_query_operators(self):
        self.assertEqual(search(self.connection, '" OR * : ( )')["total"], 0)
        self.assertEqual(search(self.connection, 'Go+"')["results"][0]["authority"], "manual")

    def test_historical_filter_preserves_all_occurrence_provenance(self):
        result = search(self.connection, "Go+", kind="release_note", version="1.9")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["results"][0]["occurrence_count"], 2)

    def test_cumulative_edition_filter_preserves_original_release_version(self):
        result = search(self.connection, "Go+", kind="release_note", version="2.0")
        self.assertEqual(result["total"], 1)
        entry = result["results"][0]
        self.assertEqual(entry["version"], "1.9")
        self.assertEqual(entry["matched_version"], "2.0")
        self.assertEqual(entry["version_match_roles"], ["edition"])

    def test_known_limitations_require_the_selected_pdf_edition(self):
        limitation = self.document("release-entry/limitation", "Preset limitation", "release_note", "limitations", "Presets have a documented limitation.", [])
        limitation["source_url"] = "/release-notes-pdf/later.pdf#page=8"
        limitation["occurrences"] = [
            {"version": "1.9", "publication_version": "2.0", "source_page": 4, "source_url": "/release-notes-pdf/selected.pdf#page=4", "document_id": "release/selected", "section": "Known Limitations"},
            {"version": "1.9", "publication_version": "2.1", "source_page": 8, "source_url": "/release-notes-pdf/later.pdf#page=8", "document_id": "release/later"},
        ]
        later = self.document("release-entry/later", "Later preset limitation", "release_note", "limitations", "Presets have another limitation.", [])
        later["version"] = "2.0"
        later["occurrences"] = [{"version": "2.0", "publication_version": "2.1", "source_page": 9}]
        fixed = self.document("release-entry/fixed", "Fixed preset bug", "release_note", "bug-fixes", "Presets were fixed.", [])
        fixed["occurrences"] = [{"version": "1.9", "publication_version": "2.0", "source_page": 3}]
        self.documents.extend([limitation, later, fixed])
        self.write_documents()
        build_index(self.root)
        with open_database(self.database) as rebuilt:
            self.assertEqual(search(rebuilt, "", kind="release_note", category="limitations", version="2.0")["total"], 2)
            for query in ("", "presets"):
                result = search(rebuilt, query, kind="release_note", category="limitations", edition="2.0")
                self.assertEqual([item["id"] for item in result["results"]], [limitation["id"]])
                item = result["results"][0]
                self.assertEqual(item["version"], "1.9")
                self.assertEqual(item["matched_edition"], "2.0")
                self.assertEqual(item["matched_occurrences"], [limitation["occurrences"][0]])
            self.assertEqual(search(rebuilt, "", kind="release_note", category="limitations", edition="9.9")["total"], 0)

    def test_invalid_authority_fails_before_replacing_good_index(self):
        before = self.database.read_bytes()
        self.documents[3]["authority"] = "manual"
        self.write_documents()
        with self.assertRaises(ValueError):
            build_index(self.root)
        self.assertEqual(self.database.read_bytes(), before)

    def test_rebuild_is_deterministic_for_search_results(self):
        before = [item["id"] for item in search(self.connection, "Go+", kind="all")["results"]]
        build_index(self.root)
        with open_database(self.database) as rebuilt:
            after = [item["id"] for item in search(rebuilt, "Go+", kind="all")["results"]]
        self.assertEqual(before, after)

    def test_sanitizer_drops_active_content_and_dangerous_urls(self):
        result = sanitize_html('<p onclick="evil()">Keep <b>this</b></p><script>bad()</script><a href="javascript:evil()">bad link</a><img src="https://remote.invalid/a"><img src="/assets/a.png" onerror="bad()"><input><p>Still here</p>')
        self.assertIn("Keep <b>this</b>", result)
        self.assertIn("Still here", result)
        self.assertIn('/assets/a.png', result)
        for forbidden in ("onclick", "javascript:", "bad()", "onerror", "remote.invalid", "<script"):
            self.assertNotIn(forbidden, result)

    def test_semantic_source_structure_and_safe_icon_dimensions_survive(self):
        result = sanitize_html('<aside class="source-note">Source</aside><nav class="rn-toc"><a href="#features">Features</a></nav><img src="/assets/icon.png" width="18" height="18" data-source-page="3" style="position:fixed">')
        self.assertIn('<aside class="source-note">', result)
        self.assertIn('<nav class="rn-toc">', result)
        self.assertIn('width="18" height="18" data-source-page="3"', result)
        self.assertNotIn("position:fixed", result)

    def test_source_image_caps_are_bounded_and_only_apply_to_images(self):
        result = sanitize_html('<img src="/assets/icon.png" data-source-max-width="15" data-source-max-height="24"><img src="/assets/other.png" data-source-max-width="10000" data-source-max-height="-1"><p data-source-max-width="15">Text</p>')
        self.assertIn('data-source-max-width="15" data-source-max-height="24"', result)
        self.assertEqual(result.count("data-source-max-width="), 1)
        self.assertEqual(result.count("data-source-max-height="), 1)

    def test_numbered_source_steps_preserve_only_positive_bounded_counters(self):
        result = sanitize_html('<ol start="3"><li value="4">Step</li></ol><ol start="-2"><li value="10000">Bad</li></ol><p start="2" value="2">Not a list</p>')
        self.assertIn('<ol start="3"><li value="4">', result)
        self.assertEqual(result.count("start="), 1)
        self.assertEqual(result.count("value="), 1)


class HttpBoundaryTests(ReferenceFixture):
    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, root=self.root, database=self.database))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        super().tearDown()

    def request(self, path):
        client = HTTPConnection("127.0.0.1", self.server.server_address[1])
        client.request("GET", path)
        response = client.getresponse()
        result = response.status, response.getheader("Content-Type"), response.read()
        client.close()
        return result

    def test_document_endpoint_returns_sanitized_html_and_exact_extracts(self):
        status, _, raw = self.request("/api/document?id=manual%2Fgo")
        self.assertEqual(status, 200)
        document = json.loads(raw)
        self.assertNotIn("<script", document["html"])
        self.assertEqual(len(document["commands"]), 2)
        self.assertEqual(document["command_lookup"], "Go+")
        self.assertEqual(document["commands_page"]["results"], document["commands"])
        self.assertEqual(document["commands_page"]["total"], 2)

    def test_reference_endpoint_returns_only_the_compact_current_vocabulary(self):
        status, content_type, raw = self.request("/api/references")
        self.assertEqual(status, 200)
        self.assertIn("application/json", content_type)
        self.assertEqual(json.loads(raw), {"references": [
            {"document_id": "manual/go", "command": "Go+", "category": "command", "terms": [{"term": "Go+", "origin": "canonical"}]},
            {"document_id": "manual/store", "command": "Store", "category": "command", "terms": [{"term": "Store", "origin": "canonical"}]},
        ]})

    def test_reference_endpoint_requires_configured_authentication(self):
        config = AuthConfig.from_environment("127.0.0.1", {"MA3_DOCS_PASSWORD": "reference test password"})
        self.server.RequestHandlerClass = partial(Handler, root=self.root, database=self.database, auth=config)
        status, _, raw = self.request("/api/references")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(raw), {"error": "Authentication required"})

    def test_keyword_endpoint_preserves_punctuation_and_source_links(self):
        status, _, raw = self.request("/api/commands?keyword=Go%2B&authority=all&type=all&limit=1&offset=1")
        self.assertEqual(status, 200)
        page = json.loads(raw)
        self.assertEqual((page["total"], page["offset"], page["limit"]), (2, 1, 1))
        self.assertEqual(page["results"][0]["source_document_title"], "Go+ Keyword")
        self.assertEqual(page["results"][0]["source_document_id"], "manual/go")
        self.assertEqual(page["results"][0]["source_document_url"], "/?doc=manual%2Fgo")

    def test_raw_html_and_directory_traversal_are_refused(self):
        for path in ("/web/../ARCHITECTURE.md", "/assets/%2e%2e/%2e%2e/secret.txt", "/assets/../content/manual/go.html", "/assets/%2e%2e/content/manual/go.html", "/content/manual/go.html"):
            self.assertEqual(self.request(path)[0], 400)

    def test_query_limits_and_invalid_filters_return_json_errors(self):
        for path in ("/api/search?limit=0", "/api/search?offset=-1", "/api/search?kind=everything", "/api/search?edition=2.0&edition=1.9", "/api/commands?type=unsafe", "/api/commands?keyword=Go&keyword=Store", "/api/commands?keyword=" + "a" * 201, "/api/document?id=manual%2Fgo&format=html"):
            status, content_type, raw = self.request(path)
            self.assertEqual(status, 400)
            self.assertIn("application/json", content_type)
            self.assertIn("error", json.loads(raw))

    def test_search_accepts_an_exact_edition_filter(self):
        status, _, raw = self.request("/api/search?kind=release_note&edition=2.0")
        self.assertEqual(status, 200)
        item = json.loads(raw)["results"][0]
        self.assertEqual(item["matched_edition"], "2.0")
        self.assertEqual([entry["source_page"] for entry in item["matched_occurrences"]], [9])

    def test_markdown_is_downloadable_but_source_files_are_not_exposed(self):
        self.assertEqual(self.request("/api/document?id=manual%2Fgo&format=markdown")[0], 200)
        self.assertEqual(self.request("/app/server.py")[0], 404)


class CommandRelationshipTests(ReferenceFixture):
    def setUp(self):
        super().setUp()
        for name, category, shortcut in [("Extract", "command", "Ext"), ("Preset", "command", "Pr"), ("Fixture", "command", "F or Fi"), ("Selection", "command", "Selecti"), ("Go-", "command", ""), ("/Single", "option", "/Si"), ("Cmd", "lua", ""), ("Printf", "lua", ""), ("If", "command", "I")]:
            identifier = "manual/" + name.lower().replace("/", "option-")
            document = self.document(identifier, name, "manual", category, "Type the shortcut " + shortcut if shortcut else name, [name, "semantic search phrase"])
            document["command"] = name
            self.documents.append(document)
        self.documents[0]["text"] += "\nType the shortcut Go"
        self.documents.append(self.document("manual/presets", "Presets overview", "manual", "topic", "Preset overview", []))
        self.documents.append(self.document("release-entry/extract", "Historical Extract change", "release_note", "features", "Old behaviour", []))
        self.lexicon = CommandReferences(self.documents, self.commands)

    def extract(self, text, *, identifier="topic/example", document_id="manual/presets", language="ma3", authority="manual", record_type="example"):
        return {"id": identifier, "document_id": document_id, "command": "", "text": text, "type": record_type, "authority": authority, "version": "1.9" if authority == "release-note" else "2.5", "language": language}

    def names(self, text, **kwargs):
        return {self.lexicon.references[row["keyword_document_id"]]["command"] for row in self.lexicon.relationships(self.extract(text, **kwargs))}

    def rebuild(self, commands):
        self.write_documents()
        (self.root / "data/manual/commands.jsonl").write_text("\n".join(json.dumps(item) for item in [*self.commands, *commands]))
        build_index(self.root)

    def test_reference_vocabulary_preserves_names_shortcuts_punctuation_and_lua_scopes(self):
        for identifier, parent in [("manual/lua_object_export", "manual/lua_object"), ("manual/lua_objectfree_export", "manual/lua_objectfree")]:
            document = self.document(identifier, "Export", "manual", "lua", "Export", ["old export spelling"])
            document.update(command="Export", parent_id=parent)
            self.documents.append(document)
        historical = next(item for item in self.documents if item["id"] == "release-entry/extract")
        historical.update(command="RetiredKeyword", aliases=["OldExtract"])
        self.rebuild([])
        with open_database(self.database) as connection:
            result = list_references(connection)
        references = result["references"]
        self.assertEqual([item["document_id"] for item in references], sorted(self.lexicon.references | {"manual/lua_object_export": {}, "manual/lua_objectfree_export": {}}))
        by_id = {item["document_id"]: item for item in references}
        self.assertEqual(by_id["manual/extract"]["terms"], [{"term": "Extract", "origin": "canonical"}, {"term": "ext", "origin": "documented-shortcut"}])
        self.assertEqual(by_id["manual/fixture"]["terms"], [{"term": "Fixture", "origin": "canonical"}, {"term": "f", "origin": "documented-shortcut"}, {"term": "fi", "origin": "documented-shortcut"}])
        self.assertEqual(by_id["manual/go"]["terms"], [{"term": "Go+", "origin": "canonical"}, {"term": "go", "origin": "documented-shortcut"}])
        self.assertEqual(by_id["manual/go-"]["command"], "Go-")
        self.assertEqual(by_id["manual/option-single"]["terms"], [{"term": "/Single", "origin": "canonical"}, {"term": "/si", "origin": "documented-shortcut"}])
        self.assertEqual(by_id["manual/option-single"]["category"], "option")
        self.assertEqual(by_id["manual/lua_object_export"]["scope"], "manual/lua_object")
        self.assertEqual(by_id["manual/lua_objectfree_export"]["scope"], "manual/lua_objectfree")
        self.assertNotIn("scope", by_id["manual/extract"])
        self.assertNotIn("scope", by_id["manual/cmd"])
        terms = {item["term"] for reference in references for item in reference["terms"]}
        for excluded in ("semantic search phrase", "old export spelling", ">>>", "S", "OldExtract", "RetiredKeyword"):
            self.assertNotIn(excluded, terms)
        for reference in references:
            self.assertLessEqual(set(reference), {"document_id", "command", "category", "terms", "scope"})

    def test_extract_overview_and_history_are_linked_with_provenance(self):
        own = self.extract("Extract Preset 4.2", identifier="extract/own", document_id="manual/extract")
        overview = self.extract("Extract Selection /Single", identifier="extract/overview")
        historical = self.extract("Extract Fixture 1", identifier="extract/history", document_id="release-entry/extract", authority="release-note", language=None)
        historical.update({"source_page": 9, "source_url": "/release-notes-pdf/old.pdf#page=9", "provenance": [{"source_page": 9, "version": "1.9"}, {"source_page": 11, "version": "2.0"}]})
        self.rebuild([own, overview, historical])
        with open_database(self.database) as connection:
            result = list_commands(connection, keyword="Extract", authority="all", record_type="all")
            self.assertEqual([item["id"] for item in result["results"]], [own["id"], overview["id"], historical["id"]])
            self.assertEqual(result["total"], 3)
            self.assertEqual(list_commands(connection, keyword="Ext", authority="all", record_type="all")["results"], result["results"])
            self.assertEqual(result["results"][1]["source_document_title"], "Presets overview")
            for key, value in historical.items():
                self.assertEqual(result["results"][2][key], value)
            self.assertEqual(list_commands(connection, keyword="/Single", record_type="all")["results"][0]["id"], overview["id"])
            self.assertEqual(list_commands(connection, keyword="Extract", authority="release-note", record_type="all")["total"], 1)

    def test_literals_substrings_prose_and_search_synonyms_do_not_match(self):
        self.assertEqual(self.names('Store Preset "Extract Selection /Single"'), {"Store", "Preset"})
        self.assertEqual(self.names('Store Preset "escaped \\" Extract"'), {"Store", "Preset"})
        self.assertEqual(self.names('Store Preset “Extract Selection”'), {"Store", "Preset"})
        self.assertEqual(self.names("Extracted Preset 1"), set())
        self.assertEqual(self.names("This prose mentions Extract and Preset."), set())
        self.assertEqual(self.names("Extract is a keyword for Preset data."), set())
        self.assertEqual(self.names("semantic search phrase Preset 1"), set())
        self.assertNotIn("Preset", self.names("Store $Preset"))
        self.assertNotIn("Fixture", self.names("Extract [Fixture_Number]"))

    def test_shortcuts_options_and_go_punctuation_are_exact(self):
        self.assertEqual(self.names("Ext Pr 4.2 /Si"), {"Extract", "Preset", "/Single"})
        self.assertEqual(self.names("Go+ Preset 1; Go- Preset 2"), {"Go+", "Go-", "Preset"})
        self.assertEqual(self.names("Go Preset 1"), {"Go+", "Preset"})
        self.assertNotIn("Go+", self.names("Go- Preset 1"))
        self.assertNotIn("/Single", self.names("Extract Selection /SingleSomething"))
        self.assertEqual(self.names('Extract Preset [FeatureGroup_Number or "FeatureGroup_Name"].[Preset_Number]'), {"Extract", "Preset"})

    def test_numeric_selection_optional_keywords_and_source_prompts_are_retained(self):
        self.assertEqual(self.names("301.1 Thru Fixture 302.1 Fixture 303.2"), {"Fixture"})
        self.assertEqual(self.names('(Extract) Preset ["Preset_Name" or Preset_Number]'), {"Extract", "Preset"})
        self.assertEqual(self.names("Admin@ShowData>Extract Selection"), {"Extract", "Selection"})
        self.assertEqual(self.names("Admin@ShowData> Extract Selection"), {"Extract", "Selection"})
        historical = self.extract("RetiredKeyword Fixture 2", authority="release-note")
        historical["source_style"] = "commandLineInput"
        links = self.lexicon.relationships(historical)
        self.assertEqual({row["keyword_document_id"] for row in links}, {"manual/fixture"})

    def test_lua_only_links_real_calls_and_literal_command_arguments(self):
        source = '''-- Cmd("Extract Preset 1")
local value = "Extract Selection"
if value then Printf("Extract Preset 2") end
Cmd('Extract Preset 4.2 /Single')
Cmd("Extract " .. value)
'''
        self.assertEqual(self.names(source, language="lua"), {"Printf", "Cmd", "Extract", "Preset", "/Single"})
        self.assertEqual(self.names('Printf("Extract Preset") -- Cmd("Extract Selection")', language="lua"), {"Printf"})
        self.assertEqual(self.names('--[[ Cmd("Extract Preset") ]]\nCmd([=[Extract Selection]=])', language="lua"), {"Cmd", "Extract", "Selection"})
        self.assertEqual(self.names('Cmd("Extract " .. value)', language="lua"), {"Cmd"})
        self.assertEqual(self.names('Cmd("Extract Preset 1")', language="ma3"), {"Cmd", "Extract", "Preset"})
        self.assertEqual(self.names('function Cmd(value) return value end', language="lua"), set())

    def test_pagination_reaches_every_record_beyond_two_hundred_without_duplicates(self):
        commands = [self.extract("Extract Preset " + str(index), identifier=f"extract/{index:03d}") for index in range(215)]
        self.rebuild(commands)
        with open_database(self.database) as connection:
            first = list_commands(connection, keyword="Extract", record_type="all", limit=200)
            second = list_commands(connection, keyword="Extract", record_type="all", limit=200, offset=200)
            self.assertEqual((first["total"], second["total"]), (215, 215))
            ids = [item["id"] for item in first["results"] + second["results"]]
            self.assertEqual(ids, [item["id"] for item in commands])
            self.assertEqual(len(ids), len(set(ids)))
            self.assertEqual(list_commands(connection, keyword="Extract", record_type="all", offset=215)["results"], [])

    def test_exact_duplicate_extracts_group_before_pagination_with_every_source(self):
        first = self.extract('Extract Preset "A  B"', identifier="extract/own", document_id="manual/extract")
        duplicate = self.extract(first["text"], identifier="extract/overview", record_type="syntax")
        historical = self.extract(first["text"], identifier="extract/history", document_id="release-entry/extract", authority="release-note", language=None)
        historical.update(source_page=9, source_url="/release-notes-pdf/old.pdf#page=9", provenance=[
            {"version": "1.9", "publication_version": "1.9", "source_page": 9, "source_url": "/release-notes-pdf/old.pdf#page=9"},
            {"version": "1.9", "publication_version": "2.0", "source_page": 11, "source_url": "/release-notes-pdf/later.pdf#page=11"},
        ])
        others = [self.extract("Extract Preset " + str(index), identifier=f"extract/{index:03d}") for index in range(205)]
        self.rebuild([first, *others, duplicate, historical])
        with open_database(self.database) as connection:
            page = list_commands(connection, keyword="Extract", authority="all", record_type="all", limit=1)
            self.assertEqual((page["total"], page["record_total"]), (206, 208))
            grouped = page["results"][0]
            self.assertEqual(grouped["text"], first["text"])
            self.assertEqual(grouped["source_record_count"], 3)
            self.assertEqual([item["id"] for item in grouped["source_records"]], [first["id"], duplicate["id"], historical["id"]])
            self.assertEqual(grouped["source_records"][-1]["provenance"], historical["provenance"])
            self.assertEqual(grouped["source_records"][-1]["authority"], "release-note")
            self.assertEqual(grouped["source_records"][1]["source_document_title"], "Presets overview")
            later = list_commands(connection, keyword="Extract", authority="all", record_type="all", limit=200, offset=1)
            last = list_commands(connection, keyword="Extract", authority="all", record_type="all", limit=200, offset=201)
            texts = [item["text"] for item in page["results"] + later["results"] + last["results"]]
            self.assertEqual(len(texts), len(set(texts)))
            self.assertEqual(len(texts), page["total"])
            manual = list_commands(connection, keyword="Extract", record_type="all", limit=1)["results"][0]
            self.assertEqual([item["id"] for item in manual["source_records"]], [first["id"], duplicate["id"]])
            owned = list_commands(connection, keyword="Extract", document_id="manual/extract", record_type="all")["results"][0]
            self.assertEqual([item["id"] for item in owned["source_records"]], [first["id"]])
            syntax = list_commands(connection, keyword="Extract", record_type="syntax")["results"][0]
            self.assertEqual([item["id"] for item in syntax["source_records"]], [duplicate["id"]])

    def test_plain_text_language_groups_with_ma3_without_changing_sources(self):
        command = self.extract("Extract Preset 1", identifier="extract/ma3")
        plain = self.extract(command["text"], identifier="extract/plain", language="text")
        plain["status"] = "unclassified source language"
        self.rebuild([command, plain])
        with open_database(self.database) as connection:
            result = list_commands(connection, keyword="Extract", record_type="all")
            self.assertEqual((result["total"], result["record_total"]), (1, 2))
            sources = result["results"][0]["source_records"]
            self.assertEqual([item["language"] for item in sources], ["ma3", "text"])
            self.assertEqual(sources[1]["status"], plain["status"])

    def test_lua_scope_filters_before_grouping_and_text_sort_is_stable(self):
        records = [
            self.extract("Store Cue 2", identifier="z"),
            self.extract("Store Cue 1", identifier="a"),
            self.extract("Store Cue 1", identifier="lua/source", document_id="manual/cmd", language="text"),
            self.extract("Printf('hello')", identifier="lua/inline", language="lua"),
            self.extract("Store Cue 1", identifier="duplicate"),
        ]
        self.rebuild(records)
        with open_database(self.database) as connection:
            ma = list_commands(connection, query="Store Cue", record_type="all", reference_kind="commands", sort="text", limit=1)
            lua = list_commands(connection, record_type="all", reference_kind="lua", sort="text")
            self.assertEqual((ma["total"], ma["record_total"]), (2, 3))
            self.assertEqual(ma["results"][0]["text"], "Store Cue 1")
            self.assertEqual({item["id"] for item in ma["results"][0]["source_records"]}, {"a", "duplicate"})
            self.assertEqual(list_commands(connection, query="Store Cue", record_type="all", reference_kind="commands", sort="text", limit=1, offset=1)["results"][0]["text"], "Store Cue 2")
            self.assertEqual({item["id"] for item in lua["results"]}, {"lua/source", "lua/inline"})
            for options in ({"reference_kind": "bad"}, {"sort": "bad"}):
                with self.assertRaises(ValueError):
                    list_commands(connection, **options)

    def test_grouping_preserves_case_whitespace_language_and_keyword_identity(self):
        values = [('Extract Preset "A  B"', "ma3"), ('Extract Preset "A B"', "ma3"), ('Extract Preset "a  B"', "ma3"), ('Extract Preset "A  B"', "lua"), ('Extract Preset "A  B"\n', "ma3")]
        records = [self.extract(text, identifier=f"separate/{index}", language=language) for index, (text, language) in enumerate(values)]
        keyword = self.extract("Extract", identifier="extract/keyword", document_id="manual/extract", record_type="keyword")
        keyword["command"] = "Extract"
        example = self.extract("Extract", identifier="extract/example")
        self.rebuild([*records, keyword, example])
        with open_database(self.database) as connection:
            result = list_commands(connection, authority="all", record_type="all", query="Extract")
            self.assertEqual(result["total"], len(records) + 2)
            self.assertEqual({item["id"] for item in result["results"]}, {item["id"] for item in [*records, keyword, example]})
            self.assertTrue(all(item["source_record_count"] == 1 for item in result["results"]))

    def test_shared_ma_lua_and_object_names_have_separate_reference_pages(self):
        for identifier, category, parent in [("manual/keyword_export", "command", ""), ("manual/lua_object_export", "lua", "manual/lua_object"), ("manual/lua_objectfree_export", "lua", "manual/lua_objectfree")]:
            document = self.document(identifier, "Export", "manual", category, "Export", [])
            document.update(command="Export", parent_id=parent)
            self.documents.append(document)
        ma = self.extract('Export Preset 1 "test.xml"', identifier="ma/export")
        obj = self.extract('handle:Export("test.xml")', identifier="lua/object", language="lua")
        free = self.extract('Export("test.xml", data)', identifier="lua/free", language="lua")
        self.rebuild([ma, obj, free])
        with open_database(self.database) as connection:
            self.assertEqual([item["id"] for item in list_commands(connection, keyword="Export", record_type="all")["results"]], [ma["id"]])
            for identifier, record in [("manual/lua_object_export", obj), ("manual/lua_objectfree_export", free)]:
                result = list_commands(connection, keyword="Export", keyword_document_id=identifier, record_type="all")
                self.assertEqual([item["id"] for item in result["results"]], [record["id"]])
            self.assertEqual(list_commands(connection, keyword_document_id="manual/unknown", record_type="all")["total"], 0)


if __name__ == "__main__":
    unittest.main()
