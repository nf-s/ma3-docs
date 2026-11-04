# Local reference browser and retrieval API

The viewer searches a local SQLite FTS5 index. Start it from the repository root with Python 3.10 or later.

```sh
python3 scripts/serve.py --port 8765
```

Open [the local viewer](http://127.0.0.1:8765). First launch creates an index. A fresh checkout shows import instructions and links to the publisher. Once you have imported the manual or release notes, refresh the viewer to search them. Keyboard shortcuts are **⌘ K**, **Ctrl K** and **/**. See [Getting started](../README.md) for source locations, dependencies and import commands.

The server defaults to loopback. `--host`, `--port`, `--root` and `--database` are configurable. Opening an existing database keeps the current index until you explicitly rebuild it. An ordinary rebuild with missing source documents refuses to replace the previous index.

## Authority and ranking

The default search scope is the current manual. Searching all sources ranks manual material before original editorial guides, then release-note history. Exact keyword/function/option matches rank above physical-key topics with similar names. A dedicated exact-term index preserves meaningful punctuation such as `Go+`, `$` and `/NoConfirmation`; full-text search uses FTS5 with English stemming and prefix matching.

The browser makes source class and version visible on results and article pages. Related release-note links are explicitly labelled as text matches, not verification that historical behaviour still applies. Repeated release entries expose their complete occurrence list. No historical entry is promoted to manual authority, and the index build rejects a `release_note` document claiming `manual` authority.

Release-version filtering includes each entry's original/document version and every recorded occurrence's release and publication-edition versions. Results retain their original `version`; `matched_version` and `version_match_roles` explain why a cumulative edition matched. The UI says “Included in edition …” when a later PDF edition contains an older entry. Page revision labels inside the current manual do not make those pages historical; the publication version identifies the source edition.

The separate `edition` search filter requires an exact publication-edition occurrence. Results retain their canonical version and add `matched_edition` and `matched_occurrences`, containing only the matching PDF's provenance. Use this filter when presenting the known limitations of a specific publication; `version` alone may also match an entry's original release version.

The index consumes:

- `data/manual/documents.jsonl` and `commands.jsonl`
- `data/releases/documents.jsonl` and `commands.jsonl`
- `data/guides/documents.jsonl` and optional `commands.jsonl`

An atomic file replacement publishes each successful build. A failed validation leaves the previous index intact. The service opens the current index for each request, so rebuilding does not require restarting the service. Changes to Python server code do require a restart; frontend changes require refreshing the browser.

## Read-only API

All API responses are JSON except the Markdown format. Errors have an `error` string and a non-success HTTP status. Pagination is bounded and stable for unchanged corpus contents. No cross-origin permissions are enabled.

| Endpoint | Parameters | Result |
| --- | --- | --- |
| `GET /api/health` | none | Ready status, source policy and corpus statistics |
| `GET /api/stats` | none | Counts by source/category/version, command counts and input fingerprints |
| `GET /api/search` | `q`, `kind=manual\|release_note\|guide\|all`, `category`, `version`, `edition`, `limit=1..100`, `offset` | Ranked documents, exact-match flag, source/version, matching edition provenance, excerpt and elapsed milliseconds |
| `GET /api/commands` | `q`, `authority=manual\|release-note\|editorial\|all`, `type=keyword\|syntax\|example\|all`, `document_id`, `keyword`, `keyword_document_id`, `reference_kind=commands\|lua\|all`, `sort=source\|text`, `limit=1..200`, `offset` | Exact source extracts, source article links and matched reference identities; default scope is manual keywords |
| `GET /api/document` | `id`, `format=json\|markdown` | Metadata, sanitized article fragment, first 200 collated extracts with `commands_page`, canonical `command_reference_id`, `command_lookup` and related historical context; or the complete Markdown document |
| `GET /api/references` | none | Canonical manual and captured live Lua command/option/function names, documented shortcuts and Lua scopes; complete map, at most 2,000 references |
| `GET /api/navigation` | none | Official manual TOC, release source editions and editorial guides |
| `GET /api/revisions` | `document_id`, `limit=1..100`, `offset` | Similar wording retained as separate canonical entries; pairs requiring review, not established conflicts |

Examples:

```sh
curl --get 'http://127.0.0.1:8765/api/search' --data-urlencode 'q=Go+'
curl --get 'http://127.0.0.1:8765/api/commands' --data-urlencode 'q=/NoConfirmation' --data-urlencode 'type=all'
curl --get 'http://127.0.0.1:8765/api/search' --data-urlencode 'q=macro handles' --data-urlencode 'kind=release_note'
curl --get 'http://127.0.0.1:8765/api/search' --data-urlencode 'kind=release_note' --data-urlencode 'category=limitations' --data-urlencode 'edition=2.5.1.1'
curl --get 'http://127.0.0.1:8765/api/document' --data-urlencode 'id=manual/keyword_store' --data-urlencode 'format=markdown'
```

`document_id` restricts extracts to the article they came from. `keyword` resolves one canonical reference name or an explicitly documented shortcut and collates matching extracts across source articles. Canonical names take precedence over shortcuts; MA command/option names take precedence over colliding Lua names. Ambiguous names return no collated results. Use `keyword_document_id` for an exact canonical reference identity, especially same-named Lua functions in different scopes. It takes precedence over `keyword`. Use `authority=all&type=all` to include historical extracts and all record kinds; the default remains current manual keyword records.

Identical non-keyword commands are grouped by exact text and effective language before pagination. Generic plain text is treated as MA command text; explicit Lua remains separate. `total`, `offset` and `limit` count groups; `record_total` counts the original source records. Each result includes complete enriched `source_records` and `source_record_count`, and the browser retains every linked source above one command block, collapsing multiple provenance records into an accessible source popover. Case and whitespace remain significant.

Command results add `source_document_id`, `source_document_title`, `source_document_url` and `matched_keywords`. Each matched keyword includes its canonical `keyword_document_id`, source-matched tokens and relation (`own-document` or `source-token`). Source records keep their original document and provenance; collation does not reassign ownership or upgrade historical authority. Current command/option/Lua document responses return `commands` as the first page, plus `commands_page` with the full `total`, `offset` and `limit`. The browser loads later pages using the canonical ID, so articles such as Store are not truncated at 200. Other document responses retain source-owned extracts.

```sh
curl --get 'http://127.0.0.1:8765/api/commands' \
  --data-urlencode 'keyword_document_id=manual/keyword_extract' \
  --data-urlencode 'authority=all' --data-urlencode 'type=all'
curl --get 'http://127.0.0.1:8765/api/commands' \
  --data-urlencode 'keyword_document_id=manual/keyword_store' \
  --data-urlencode 'authority=all' --data-urlencode 'type=all' \
  --data-urlencode 'limit=200' --data-urlencode 'offset=200'
```

Command extracts preserve source wording; they are not validated execution plans. Use their source document and version before generating commands for a console. A query with no results is not evidence that an operation is unsupported.

## Local data and access

All endpoints are read-only. The server bounds query sizes, parameterizes SQL and sanitizes imported HTML. It serves static files only from the viewer, generated content, assets and source PDF directories. Paths that escape those directories are refused.

Keep imported documentation on your machine. The local viewer requires no account. The optional `MA3_DOCS_USERNAME` and `MA3_DOCS_PASSWORD` settings enable HTTP Basic authentication. Binding beyond loopback requires a password of at least 16 characters. [Source and copyright notice](../NOTICE.md)

## Known limits

- Search is lexical and exact-term based; it does not generate answers or establish command validity.
- The manual's original hierarchy and local assets are preserved. Source defects remain recorded in ingestion reports.
- Related release links are a convenience based on text overlap, not a complete semantic conflict analysis.
- Document responses contain the first 200 extracts. Use `commands_page.total` and canonical `command_reference_id` to retrieve further reference pages from the commands endpoint.
- Command collation covers marked source examples, syntax and documented spellings. It does not infer unmarked prose, undocumented historical aliases or dynamically constructed Lua commands.
- The PDF source preserves figures and page layout that text conversion cannot reproduce exactly.

## Clickable command mentions

The browser loads `/api/references` once and turns recognized command mentions into canonical reference links in articles, inline syntax, code examples and search excerpts. Existing source links and markup are preserved. Command links use a subtle blue-gray color with an underline on hover or keyboard focus. Code matching preserves meaningful punctuation, excludes quoted object names and keeps Lua scope distinct from MA commands. The API uses existing read-only tables; no corpus rewrite or new search index is required.

`reference_kind` defaults to `all` for API compatibility. `lua` selects explicit Lua language extracts or records owned by Lua reference documents; `commands` selects the complement. This filter runs before grouping. `sort=text` uses case-insensitive alphabetical text, exact text and stable source tie-breaks before grouped pagination; `source` retains the previous ordering. The Commands view defaults to current manual authority; Lua defaults to all sources so the explicitly labelled live supplements are included.

Optional local live evidence is ingested from `data/live-lua/` when supplied. Manual Lua articles keep canonical identities and receive `live_lua_signature`; matching command/option articles receive `live_keyword_tokens`. These metadata fields retain build, capture time and source links. Missing manual functions receive separate editorial reference identities, never manual authority.
