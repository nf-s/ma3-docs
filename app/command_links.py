"""Build lexical relationships between source extracts and current references.

This is a source index, not a grandMA3 parser or syntax validator. Quoted values,
placeholder identifiers and Lua source are kept distinct from command tokens.
Only canonical names and explicitly documented shortcuts are executable aliases;
the manual's broader search synonyms are not command spellings.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any


REFERENCE_CATEGORIES = {"command", "option", "lua"}
WORD = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
SHORTCUT = re.compile(r"(?:/?[A-Za-z][A-Za-z0-9]*[+-]?|[<>]{1,3})\Z")
QUOTES = {'"': '"', "'": "'", "“": "”", "‘": "’"}
PROSE = re.compile(r"\b(?:is|are|was|were|should|would|could|means|allows|refers)\b", re.I)


@dataclass(frozen=True)
class Token:
    value: str
    kind: str
    start: int
    end: int


def tokens(text: str, lua: bool = False) -> list[Token]:
    result = []
    position = 0
    while position < len(text):
        start = position
        char = text[position]
        if char.isspace():
            position += 1
            continue
        if lua and text.startswith("--", position):
            opening = re.match(r"--\[(=*)\[", text[position:])
            if opening:
                end = text.find("]" + opening.group(1) + "]", position + opening.end())
                position = len(text) if end < 0 else end + len(opening.group(1)) + 2
            else:
                end = text.find("\n", position)
                position = len(text) if end < 0 else end + 1
            continue
        opening = re.match(r"\[(=*)\[", text[position:]) if lua else None
        if opening:
            closing = "]" + opening.group(1) + "]"
            end = text.find(closing, position + opening.end())
            if end < 0:
                result.append(Token(text[position:], "incomplete-string", start, len(text)))
                break
            result.append(Token(text[position + opening.end():end], "string", start, end + len(closing)))
            position = end + len(closing)
            continue
        if char in QUOTES:
            closing = QUOTES[char]
            position += 1
            value = []
            complete = False
            while position < len(text):
                char = text[position]
                if char == "\\" and position + 1 < len(text):
                    escaped = text[position + 1]
                    value.append({"n": "\n", "r": "\r", "t": "\t"}.get(escaped, escaped))
                    position += 2
                elif char == closing:
                    position += 1
                    complete = True
                    break
                else:
                    value.append(char)
                    position += 1
            result.append(Token("".join(value), "string" if complete else "incomplete-string", start, position))
            continue
        if text.startswith("#[", position):
            result.append(Token("#[Object]", "symbol", start, position + 2))
            position += 2
            continue
        if char == "$":
            position += 1
            variable = WORD.match(text, position)
            if variable:
                position = variable.end()
            result.append(Token("$", "symbol", start, position))
            continue
        option = WORD.match(text, position + 1) if char == "/" else None
        word = WORD.match(text, position)
        if option or word:
            match = option or word
            if match is None:
                raise ValueError("Expected a matched command token")
            position = match.end()
            if word and text[start:position].casefold() == "go" and position < len(text) and text[position] in "+-":
                position += 1
            result.append(Token(text[start:position], "word", start, position))
            continue
        number = re.match(r"\d+(?:\.\d+)*", text[position:])
        if number:
            position += number.end()
            result.append(Token(text[start:position], "number", start, position))
            continue
        if text.startswith(("<<<", ">>>"), position):
            position += 3
        else:
            position += 1
        result.append(Token(text[start:position], "symbol", start, position))
    return result


class CommandReferences:
    def __init__(self, documents: list[dict[str, Any]], commands: list[dict[str, Any]]) -> None:
        keyword_names = {record["document_id"]: record["command"] for record in commands if record["type"] == "keyword" and record["authority"] == "manual"}
        self.references: dict[str, dict[str, str]] = {}
        self.terms: dict[str, set[str]] = defaultdict(set)
        self.term_origins: dict[tuple[str, str], str] = {}
        self.lua_names: dict[str, set[str]] = defaultdict(set)
        self.lua_scopes: dict[str, str] = {}
        for document in documents:
            live_lua = document["category"] == "lua" and document.get("source_kind") == "live-introspection"
            if (document["authority"] != "manual" and not live_lua) or document["category"] not in REFERENCE_CATEGORIES:
                continue
            name = keyword_names.get(document["id"], document.get("command"))
            if not isinstance(name, str) or not name:
                continue
            identifier = document["id"]
            self.references[identifier] = {"document_id": identifier, "command": name, "command_fold": name.casefold(), "category": document["category"]}
            self.terms[name.casefold()].add(identifier)
            self.term_origins[(name.casefold(), identifier)] = "canonical"
            if document["category"] == "lua":
                self.lua_names[name].add(identifier)
                self.lua_scopes[identifier] = document.get("parent_id", "")
                continue
            for line in document["text"].splitlines():
                shortcut = re.search(r"\btype (?:the )?shortcuts?\s+(.+)", line, re.I)
                if not shortcut:
                    continue
                for value in re.split(r"\s+or\s+(?:type\s+)?", shortcut.group(1), flags=re.I):
                    alias = value.strip().rstrip(".")
                    if SHORTCUT.fullmatch(alias):
                        # An option spelling must include its slash. Do not fix
                        # apparent omissions in the source shortcut description.
                        if document["category"] == "option" and not alias.startswith("/"):
                            continue
                        self.terms[alias.casefold()].add(identifier)
                        self.term_origins.setdefault((alias.casefold(), identifier), "documented-shortcut")

    def ma_target(self, token: str) -> str | None:
        candidates = {identifier for identifier in self.terms.get(token.casefold(), ()) if self.references[identifier]["category"] != "lua"}
        canonical = {identifier for identifier in candidates if self.term_origins[(token.casefold(), identifier)] == "canonical"}
        candidates = canonical or candidates
        return next(iter(candidates)) if len(candidates) == 1 else None

    def lua_target(self, token: str, member: bool) -> str | None:
        candidates = self.lua_names.get(token, set())
        if len(candidates) > 1:
            scope = "manual/lua_object" if member else "manual/lua_objectfree"
            candidates = {identifier for identifier in candidates if self.lua_scopes[identifier] == scope}
        return next(iter(candidates)) if len(candidates) == 1 else None

    def ma_references(self, text: str, record: dict[str, Any]) -> dict[str, set[str]]:
        # A few exact source boxes include the current command-line destination.
        # Ignore that prompt for indexing while preserving the original extract.
        text = re.sub(r"^[^@\n]+@[^>\n]+>\s*", "", text)
        lexical = tokens(text)
        unquoted = [token for token in lexical if token.kind not in {"string", "incomplete-string"}]
        if not unquoted:
            return {}
        first = unquoted[0]
        leading_template = text.lstrip().startswith(("[", "(["))
        optional_keyword = first.value == "(" and len(unquoted) > 1 and self.ma_target(unquoted[1].value)
        historical_head = record["authority"] == "release-note" and record.get("command", "").casefold() == first.value.casefold()
        marked_command = record.get("source_style") == "commandLineInput" or (record.get("language") == "ma3" and isinstance(record.get("raw_text"), str))
        if not self.ma_target(first.value) and first.kind != "number" and not leading_template and not optional_keyword and not historical_head and not marked_command:
            return {}
        if PROSE.search(" ".join(token.value for token in unquoted)):
            return {}
        found: dict[str, set[str]] = defaultdict(set)
        for token in unquoted:
            identifier = self.ma_target(token.value)
            if identifier:
                found[identifier].add(token.value)
        return found

    def relationships(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        found: dict[str, set[str]] = defaultdict(set)
        own = record["document_id"] if record["document_id"] in self.references else None
        if own:
            found[own].add(self.references[own]["command"])
        if record["type"] != "keyword":
            # Some bundled release-note syntax spans have no language marker.
            # A leading documented Lua function call identifies those explicitly.
            leading_call = re.match(r"\s*([A-Za-z_][A-Za-z_0-9]*)\s*\(", record["text"])
            is_lua = record.get("language") == "lua" or bool(leading_call and leading_call.group(1) in self.lua_names)
            if is_lua:
                lexical = tokens(record["text"], lua=True)
                for index, token in enumerate(lexical[:-1]):
                    if token.kind != "word" or lexical[index + 1].value != "(":
                        continue
                    if index and lexical[index - 1].value == "function":
                        continue
                    identifier = self.lua_target(token.value, bool(index and lexical[index - 1].value in {".", ":"}))
                    if identifier:
                        found[identifier].add(token.value)
                    # Only complete literal arguments to documented MA command
                    # functions qualify. Concatenations and variables stay Lua.
                    if token.value in {"Cmd", "CmdIndirect", "CmdIndirectWait"} and index + 3 < len(lexical):
                        argument, following = lexical[index + 2:index + 4]
                        if argument.kind == "string" and following.value in {")", ","}:
                            for target, values in self.ma_references(argument.value, record).items():
                                found[target].update(values)
            elif record.get("language") in {None, "ma3", "text"}:
                for target, values in self.ma_references(record["text"], record).items():
                    found[target].update(values)
        return [{"command_id": record["id"], "keyword_document_id": identifier, "matched_tokens": sorted(values, key=str.casefold), "relation": "own-document" if identifier == own else "source-token"} for identifier, values in sorted(found.items())]
