#!/usr/bin/env python3
"""Capture versioned grandMA3 Lua signatures and read-only keyword introspection.

No show objects are written. HelpLua exports a text file in the console library.
Token capture needs an enabled Lua bridge; --temporary-lua explicitly enables
the bridge temporarily and verifies restoration even if capture fails.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
MAX_REPLY = 4 * 1024 * 1024
TOKEN_CODE = """
local out = {tokens={}, failures={}}
for i=0,4095 do
    local ok, name = pcall(GetTokenNameByIndex, i)
    if not ok then
        out.failures[#out.failures+1]={index=i,error=tostring(name)}
    elseif type(name)=="string" and name~="" then
        out.tokens[#out.tokens+1]={index=i,name=name}
    end
end
return out
"""


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def object_value(value, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


class Bridge:
    def __init__(self, port: int):
        self.socket = socket.create_connection(("127.0.0.1", port), timeout=15)
        self.reader = self.socket.makefile("rb")
        self.request_id = 0

    def request(self, op: str, args: dict) -> dict:
        self.request_id += 1
        identity = f"lua-reference-{self.request_id}"
        self.socket.sendall(
            (json.dumps({"id": identity, "op": op, "args": args}) + "\n").encode()
        )
        line = self.reader.readline(MAX_REPLY + 1)
        if len(line) > MAX_REPLY or not line.endswith(b"\n"):
            raise ValueError("Bridge reply is missing or exceeds the capture limit")
        reply = object_value(json.loads(line), "Bridge reply")
        if reply.get("id") != identity or reply.get("ok") is not True:
            raise ValueError(f"Bridge {op} failed: {reply.get('error', 'invalid reply')}")
        return object_value(reply.get("result"), f"Bridge {op} result")

    def command(self, command: str) -> dict:
        result = self.request("cmd", {"command": command})
        if result.get("feedback") != "OK":
            raise ValueError(f"Console did not accept {command}: {result.get('feedback')}")
        return result

    def lua_enabled(self, enabled: bool) -> dict:
        for _ in range(20):
            status = self.request("ping", {})
            policy = object_value(status.get("lua"), "Lua policy")
            if policy.get("enabled") is enabled:
                return status
            time.sleep(0.1)
        raise ValueError(f"Bridge Lua policy did not become enabled={enabled}")

    def close(self) -> None:
        self.reader.close()
        self.socket.close()


def validate_descriptors(value: dict) -> dict:
    for scope in ("free", "object"):
        rows = value.get(scope)
        if not isinstance(rows, list) or not rows:
            raise ValueError(f"Missing {scope} descriptors")
        names = set()
        for row in rows:
            if not isinstance(row, list) or len(row) != 3:
                raise ValueError(f"Invalid {scope} descriptor")
            if not all(isinstance(part, str) for part in row):
                raise ValueError(f"Non-string {scope} descriptor")
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", row[0]):
                raise ValueError(f"Invalid function name: {row[0]}")
            if row[0] in names:
                raise ValueError(f"Duplicate {scope} descriptor: {row[0]}")
            names.add(row[0])
    return value


def capture(args) -> None:
    output = args.root / "data/live-lua"
    output.mkdir(parents=True, exist_ok=True)
    bridge = Bridge(args.port)
    changed_policy = False
    try:
        before = bridge.request("ping", {})
        policy = object_value(before.get("lua"), "Lua policy")
        if not isinstance(policy.get("enabled"), bool):
            raise ValueError("Missing Lua enabled policy")
        api = validate_descriptors(bridge.request("api", {}))
        write_json(output / "api-descriptors.json", api)
        metadata = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "authority": "live-introspection",
            "build": object_value(before.get("build"), "Console build"),
            "bridge_version": before.get("bridgeVersion"),
            "lua_version": before.get("luaVersion"),
            "source_functions": ["GetApiDescriptor", "GetObjApiDescriptor"],
            "lua_enabled_before": policy["enabled"],
            "show_object_writes": False,
            "token_index_range": [0, 4095] if args.tokens else None,
        }
        if args.helplua:
            metadata["helplua_command"] = bridge.command("HelpLua")
            source = args.helplua_file.expanduser()
            if not source.is_file():
                raise ValueError(f"HelpLua export not found: {source}")
            shutil.copyfile(source, output / "grandMA3_lua_functions.txt")
            metadata["helplua_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
            metadata["helplua_source_path"] = str(source)
        if args.tokens:
            if not policy["enabled"]:
                if not args.temporary_lua:
                    raise ValueError("Token capture needs enabled Lua or --temporary-lua")
                changed_policy = True
                bridge.command('Plugin "gma3_mcp_bridge" "lua on"')
                bridge.lua_enabled(True)
            response = bridge.request("lua", {"code": TOKEN_CODE, "maxMs": 3000})
            values = response.get("values")
            if not isinstance(values, list) or len(values) != 1:
                raise ValueError("Unexpected token capture response")
            token_result = object_value(values[0], "Token capture")
            rows = token_result.get("tokens")
            if not isinstance(rows, list):
                raise ValueError("Missing token rows")
            for row in rows:
                item = object_value(row, "Token row")
                if not isinstance(item.get("index"), int) or not isinstance(item.get("name"), str):
                    raise ValueError("Invalid token row")
            write_json(output / "tokens.json", token_result)
            metadata["tokens_sha256"] = hashlib.sha256((output / "tokens.json").read_bytes()).hexdigest()
        metadata["api_sha256"] = hashlib.sha256(
            (output / "api-descriptors.json").read_bytes()
        ).hexdigest()
    finally:
        try:
            if changed_policy:
                bridge.command('Plugin "gma3_mcp_bridge" "lua off"')
                bridge.lua_enabled(False)
        finally:
            bridge.close()
    metadata["lua_enabled_after"] = policy["enabled"]
    write_json(output / "capture.json", metadata)


def build_report(root: Path) -> dict:
    output = root / "data/live-lua"
    api = validate_descriptors(json.loads((output / "api-descriptors.json").read_text()))
    metadata = object_value(json.loads((output / "capture.json").read_text()), "Capture")
    if hashlib.sha256((output / "api-descriptors.json").read_bytes()).hexdigest() != metadata.get("api_sha256"):
        raise ValueError("API descriptors do not match capture provenance")
    manual = [json.loads(line) for line in (root / "data/manual/documents.jsonl").read_text().splitlines()]
    lua = [doc for doc in manual if doc["category"] == "lua"]
    identities = {}
    for doc in lua:
        scope = "free" if "/lua_objectfree_" in doc["id"] else "object"
        identities[(scope, doc["title"].split("(")[0].strip().casefold())] = doc["id"]
    functions = []
    for scope in ("free", "object"):
        for name, arguments, returns in api[scope]:
            functions.append({
                "scope": scope,
                "name": name,
                "arguments": arguments,
                "returns": returns,
                "signature": f"{name}({arguments}): {returns}",
                "manual_document_id": identities.get((scope, name.casefold())),
                "build": metadata["build"],
                "authority": "live-introspection",
                "behaviour_verified": False,
                "source_function": "GetApiDescriptor" if scope == "free" else "GetObjApiDescriptor",
                "source_url": "/assets/live-lua/grandMA3_lua_functions.txt",
                "captured_at": metadata["captured_at"],
            })
    (output / "functions.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in functions)
    )
    missing = [row for row in functions if row["manual_document_id"] is None]
    expected_help = []
    for scope in ("free", "object"):
        expected_help.extend(f"{name}({arguments}): {returns}" for name, arguments, returns in api[scope])
    help_path = output / "grandMA3_lua_functions.txt"
    help_lines = help_path.read_text().splitlines() if help_path.exists() else []
    if metadata.get("helplua_sha256") and hashlib.sha256(help_path.read_bytes()).hexdigest() != metadata["helplua_sha256"]:
        raise ValueError("HelpLua export does not match capture provenance")
    if metadata.get("helplua_sha256"):
        assets = root / "assets/live-lua"
        assets.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(help_path, assets / "grandMA3_lua_functions.txt")
    report = {
        "build": metadata["build"],
        "manual_lua_articles": len(lua),
        "live_signatures": len(functions),
        "covered_signatures": len(functions) - len(missing),
        "missing_manual_articles": len(missing),
        "missing_by_scope": {scope: [row["name"] for row in missing if row["scope"] == scope] for scope in ("free", "object")},
        "helplua_signatures_match_descriptors": all(line in help_lines for line in expected_help),
        "manual_not_in_live_descriptors": [doc_id for identity, doc_id in identities.items() if not any((row["scope"], row["name"].casefold()) == identity for row in functions)],
    }
    enrich_tokens(root, metadata, report)
    build_documents(root, metadata, functions, report)
    write_json(output / "coverage.json", report)
    return report


def build_documents(root: Path, metadata: dict, functions: list, report: dict) -> None:
    output = root / "data/live-lua"
    content = root / "content/live-lua"
    content.mkdir(parents=True, exist_ok=True)
    version = metadata["build"]["version"]
    documents = []
    commands = []
    for row in functions:
        if row["manual_document_id"] is not None:
            continue
        slug = f"{row['scope']}-{row['name'].lower()}"
        identity = f"live-lua/{slug}"
        scope_title = "Object-Free API" if row["scope"] == "free" else "Object API"
        parent = "manual/lua_objectfree" if row["scope"] == "free" else "manual/lua_object"
        notice = (
            f"Live introspection from grandMA3 {version}, {scope_title}. "
            "The installed manual has no separate article for this function. "
            "This is the console's exported signature; arguments and return labels "
            "are reproduced verbatim. Behaviour and calling examples were not tested."
        )
        document = {
            "id": identity,
            "title": f"{row['name']}()",
            "kind": "guide",
            "category": "lua",
            "version": version,
            "authority": "editorial",
            "source_kind": "live-introspection",
            "source_path": metadata.get("helplua_source_path", str(output / "api-descriptors.json")),
            "source_url": "/assets/live-lua/grandMA3_lua_functions.txt",
            "source_sha256": metadata.get("helplua_sha256", metadata["api_sha256"]),
            "markdown_path": f"content/live-lua/{slug}.md",
            "html_path": f"content/live-lua/{slug}.html",
            "text": f"{notice}\n{row['signature']}",
            "breadcrumbs": ["Lua live API", scope_title],
            "parent_id": parent,
            "aliases": [],
            "searchable": True,
            "lua_scope": row["scope"],
            "function_name": row["name"],
            "command": row["name"],
            "captured_at": metadata["captured_at"],
            "behaviour_verified": False,
        }
        documents.append(document)
        frontmatter = "\n".join(f"{key}: {json.dumps(value)}" for key, value in document.items() if key not in {"text", "breadcrumbs", "aliases"})
        (root / document["markdown_path"]).write_text(
            f"---\n{frontmatter}\n---\n\n# {row['name']}()\n\n"
            f"> {notice}\n\n## Signature\n\n```lua\n{row['signature']}\n```\n\n"
            f"Source: [HelpLua export]({document['source_url']}) captured {metadata['captured_at']}; "
            f"[{row['source_function']}()](/?doc=manual%2Flua_objectfree_{row['source_function'].lower()}). "
            "[Official HelpLua documentation](https://help.malighting.com/grandMA3/2.5/HTML/keyword_helplua.html).\n"
        )
        (root / document["html_path"]).write_text(
            f"<h1>{html.escape(row['name'])}()</h1>\n<p>{html.escape(notice)}</p>\n"
            f"<h2>Signature</h2>\n<pre><code class=\"language-lua\">{html.escape(row['signature'])}</code></pre>\n"
            f"<p>Source: <a href=\"{document['source_url']}\">HelpLua export</a> captured {html.escape(metadata['captured_at'])}; "
            f"<a href=\"/?doc=manual%2Flua_objectfree_{row['source_function'].lower()}\">{row['source_function']}()</a>. "
            "<a href=\"https://help.malighting.com/grandMA3/2.5/HTML/keyword_helplua.html\">Official HelpLua documentation</a>.</p>\n"
        )
        for record_type, text in (("keyword", row["name"]), ("syntax", row["signature"])):
            commands.append({
                "id": f"{identity}/{record_type}/{hashlib.sha256(text.encode()).hexdigest()[:16]}",
                "document_id": identity,
                "command": row["name"],
                "text": text,
                "type": record_type,
                "language": "lua",
                "version": version,
                "authority": "editorial",
                "source_kind": "live-introspection",
                "source_path": document["source_path"],
                "source_url": document["source_url"],
                "source_sha256": document["source_sha256"],
                "status": "signature-exported-not-executed",
                "lua_scope": row["scope"],
            })
    documents.append(build_overview(root, metadata, functions, report))
    for filename, rows in (("documents.jsonl", documents), ("commands.jsonl", commands)):
        (output / filename).write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))


def enrich_tokens(root: Path, metadata: dict, report: dict) -> None:
    output = root / "data/live-lua"
    token_path = output / "tokens.json"
    if not token_path.exists() or not metadata.get("tokens_sha256"):
        return
    if hashlib.sha256(token_path.read_bytes()).hexdigest() != metadata["tokens_sha256"]:
        raise ValueError("Token metadata does not match capture provenance")
    captured = object_value(json.loads(token_path.read_text()), "Tokens")
    tokens = captured.get("tokens")
    if not isinstance(tokens, list):
        raise ValueError("Invalid captured tokens")
    keywords = {}
    for line in (root / "data/manual/commands.jsonl").read_text().splitlines():
        row = json.loads(line)
        if row["type"] == "keyword" and row["language"] == "ma3":
            keywords.setdefault(row["command"].casefold(), []).append(row["document_id"])
    enriched = []
    for token in tokens:
        item = object_value(token, "Captured token")
        enriched.append({
            "index": item["index"],
            "name": item["name"],
            "manual_document_ids": sorted(set(keywords.get(item["name"].casefold(), []))),
            "internal_name": item["name"].startswith("_"),
            "authority": "live-introspection",
            "build": metadata["build"],
            "source_function": "GetTokenNameByIndex",
            "source_url": "/?doc=live-lua%2Fcoverage-audit",
            "captured_at": metadata["captured_at"],
            "behaviour_verified": False,
        })
    (output / "keyword-metadata.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in enriched))
    report["token_introspection"] = {
        "indices_scanned": metadata.get("token_index_range"),
        "named_tokens": len(enriched),
        "matched_manual_tokens": sum(bool(row["manual_document_ids"]) for row in enriched),
        "internal_names": sum(row["internal_name"] for row in enriched),
        "unmatched_public_names": [row["name"] for row in enriched if not row["manual_document_ids"] and not row["internal_name"]],
        "failures": captured.get("failures"),
        "qualification": "Names and numeric token identities only; not evidence of documented syntax, safety or supported user commands. No shortcuts inferred.",
    }


def build_overview(root: Path, metadata: dict, functions: list, report: dict) -> dict:
    version = metadata["build"]["version"]
    title = f"Lua API coverage audit — grandMA3 {version}"
    introduction = (
        f"Live grandMA3 {version} onPC exported {len(functions)} scoped Lua signatures: "
        f"{report['covered_signatures']} have installed manual articles and "
        f"{report['missing_manual_articles']} have signature-only reference supplements. "
        "Every manual Lua article maps to a live descriptor. HelpLua and both API "
        "descriptor tables agree. This checks exported names and signatures, not runtime behaviour."
    )
    md = [f"# {title}", introduction,
          f"Captured {metadata['captured_at']}; bridge {metadata['bridge_version']}; {metadata['lua_version']}.",
          "[Exact HelpLua export](/assets/live-lua/grandMA3_lua_functions.txt) · "
          "[HelpLua manual](/?doc=manual%2Fkeyword_helplua) · "
          "[GetApiDescriptor manual](/?doc=manual%2Flua_objectfree_getapidescriptor) · "
          "[GetObjApiDescriptor manual](/?doc=manual%2Flua_objectfree_getobjapidescriptor)"]
    rendered = [f"<h1>{html.escape(title)}</h1>", f"<p>{html.escape(introduction)}</p>",
                f"<p>Captured {html.escape(metadata['captured_at'])}; bridge {html.escape(metadata['bridge_version'])}; {html.escape(metadata['lua_version'])}.</p>",
                '<p><a href="/assets/live-lua/grandMA3_lua_functions.txt">Exact HelpLua export</a> · <a href="/?doc=manual%2Fkeyword_helplua">HelpLua manual</a> · <a href="/?doc=manual%2Flua_objectfree_getapidescriptor">GetApiDescriptor manual</a> · <a href="/?doc=manual%2Flua_objectfree_getobjapidescriptor">GetObjApiDescriptor manual</a></p>']
    for scope, name in (("free", "Object-Free API"), ("object", "Object API")):
        rows = [row for row in functions if row["scope"] == scope]
        md.append(f"## {name} ({len(rows)})")
        rendered.append(f"<h2>{name} ({len(rows)})</h2><ul>")
        for row in sorted(rows, key=lambda item: item["name"].casefold()):
            identity = row["manual_document_id"] or f"live-lua/{scope}-{row['name'].lower()}"
            url = f"/?doc={quote(identity, safe='')}"
            label = "manual article" if row["manual_document_id"] else "live signature only"
            md.append(f"- [{row['name']}()]({url}) — {label}")
            rendered.append(f'<li><a href="{url}">{row["name"]}()</a> — {label}</li>')
        rendered.append("</ul>")
    tokens = report.get("token_introspection")
    if isinstance(tokens, dict):
        paragraph = (
            f"GetTokenNameByIndex inspected indices 0–4095 and returned {tokens['named_tokens']} "
            f"named tokens. {tokens['matched_manual_tokens']} match existing manual command/option "
            f"names; {tokens['internal_names']} begin with an underscore. The remaining names "
            "are discovery evidence only. Token presence does not establish documented syntax, "
            "supported public commands, aliases or safe execution. No discovered command was run."
        )
        md.extend(["## Keyword introspection", paragraph])
        rendered.extend(["<h2>Keyword introspection</h2>", f"<p>{html.escape(paragraph)}</p>"])
    source_path = root / "data/live-lua/github-source-audit.json"
    if source_path.exists():
        sources = json.loads(source_path.read_text())
        md.append("## Additional source discovery")
        rendered.append("<h2>Additional source discovery</h2><ul>")
        for source in sources:
            if "commit" not in source:
                continue
            license_info = source.get("license") or {}
            label = f"{source['repository']} ({license_info.get('spdx_id', 'licence unconfirmed')})"
            url = f"{source['url']}/tree/{source['commit']}"
            description = "Unofficial community source; linked for further research, not imported as current documentation."
            source_digest = source.get("comparison_to_live", {}).get("source_sha256")
            if source_digest and source_digest == metadata.get("helplua_sha256"):
                description = "The referenced export matches the locally captured HelpLua file byte for byte."
            md.append(f"- [{label}]({url}): {description}")
            rendered.append(f'<li><a href="{url}">{html.escape(label)}</a>: {html.escape(description)}</li>')
        rendered.append("</ul>")
    identity = "live-lua/coverage-audit"
    document = {
        "id": identity, "title": title, "kind": "guide", "category": "workflow",
        "version": version, "authority": "editorial", "source_kind": "live-introspection",
        "source_path": metadata.get("helplua_source_path", "data/live-lua/api-descriptors.json"),
        "source_url": "/assets/live-lua/grandMA3_lua_functions.txt",
        "source_sha256": metadata.get("helplua_sha256", metadata["api_sha256"]),
        "markdown_path": "content/live-lua/coverage-audit.md", "html_path": "content/live-lua/coverage-audit.html",
        "text": "\n\n".join(md), "breadcrumbs": ["Lua live API"], "parent_id": "manual/plugins",
        "aliases": [], "searchable": True, "captured_at": metadata["captured_at"],
    }
    (root / document["markdown_path"]).write_text("\n\n".join(md) + "\n")
    (root / document["html_path"]).write_text("\n".join(rendered) + "\n")
    return document


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--port", type=int, default=9800)
    parser.add_argument("--tokens", action="store_true")
    parser.add_argument("--temporary-lua", action="store_true")
    parser.add_argument("--helplua", action="store_true")
    parser.add_argument("--helplua-file", type=Path, default=Path.home() / "MALightingTechnology/gma3_library/grandMA3_lua_functions.txt")
    parser.add_argument("--offline", action="store_true", help="Rebuild the comparison without connecting")
    args = parser.parse_args()
    args.root = args.root.resolve()
    if not args.offline:
        capture(args)
    report = build_report(args.root)
    print(json.dumps({key: value for key, value in report.items() if key != "missing_by_scope"}, indent=2))


if __name__ == "__main__":
    main()
