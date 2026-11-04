#!/usr/bin/env python3
"""Convert the installed MA3 manual, its TOC and indexes without network access.

Requires the existing lxml build-time package. The generated corpus has no lxml
runtime dependency. No JavaScript from the manual is evaluated.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import html as html_escape
import json
import re
import shutil
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from lxml import etree, html


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = Path.home() / "MALightingTechnology/gma3_2.5.1/shared/language"
DROP_TAGS = {
    "script", "style", "iframe", "object", "embed", "form", "input", "button",
    "textarea", "select", "link", "meta", "svg", "noscript",
}
ALLOWED_TAGS = {
    "div", "section", "article", "main", "span", "p", "br", "hr", "h1", "h2",
    "h3", "h4", "h5", "h6", "a", "img", "figure", "figcaption", "ul", "ol",
    "li", "table", "thead", "tbody", "tfoot", "tr", "td", "th", "strong",
    "b", "em", "i", "u", "s", "del", "code", "pre", "blockquote", "dl",
    "dt", "dd", "sup", "sub", "kbd", "details", "summary",
}
ALLOWED_ATTRIBUTES = {
    "id", "class", "title", "alt", "href", "src", "colspan", "rowspan",
    "scope", "start", "value", "width", "height",
}
ASSET_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".pdf"}
EMPTY_BLOCK_TAGS = {
    "p", "div", "section", "article", "main", "table", "pre", "code",
    "blockquote", "figure", "figcaption", "ul", "ol", "dl",
    "h1", "h2", "h3", "h4", "h5", "h6",
}
TEXT_CHROME_CLASSES = {"syntax", "inlineCode", "commandLineInput", "commandLineResponse", "softkey", "hardkey"}
SOURCE_CODE_CLASSES = {"syntax", "inlineCode", "commandLineInput"}
JSON_DECODER = json.JSONDecoder()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def compact_text(element: etree._Element) -> str:
    """HTML flow text, with adjacent inline spans kept adjacent."""
    return re.sub(r"\s+", " ", "".join(element.itertext())).strip()


def code_text(element: etree._Element) -> str:
    return "".join(element.itertext()).replace("\xa0", " ").strip("\r\n")


def search_text(element: etree._Element) -> str:
    """Keep block boundaries so adjacent paragraphs do not merge search tokens."""
    pieces = []

    def visit(node):
        if node.text:
            pieces.append(node.text)
        for child in node:
            if child.tag in {"p", "div", "li", "tr", "td", "th", "br", "h1", "h2", "h3", "h4", "h5", "pre"}:
                pieces.append("\n")
            visit(child)
            if child.tag in {"p", "div", "li", "tr", "td", "th", "br", "h1", "h2", "h3", "h4", "h5", "pre"}:
                pieces.append("\n")
            if child.tail:
                pieces.append(child.tail)

    visit(element)
    lines = [re.sub(r"[\t \r\f\v\xa0]+", " ", line).strip() for line in "".join(pieces).splitlines()]
    return "\n".join(line for line in lines if line)


def has_class(element: etree._Element, value: str) -> bool:
    return value in element.get("class", "").split()


def is_source_code(element: etree._Element) -> bool:
    # One legacy source class includes a trailing comma. Keep the same explicit
    # source markers for both renderers without inferring code from prose.
    return bool({value.rstrip(",") for value in element.get("class", "").split()} & SOURCE_CODE_CLASSES)


def visible_text(element: etree._Element) -> str:
    return re.sub(r"[\s\u200b\ufeff]+", "", "".join(element.itertext()))


def has_visible_content(element: etree._Element) -> bool:
    if visible_text(element):
        return True
    # Images and separators are meaningful without text. Keep empty table cells
    # and list items as structural examples; never collapse their containing grid.
    for child in element.iter():
        if child.tag in {"img", "svg", "iframe", "video", "audio", "canvas", "hr"}:
            return True
        if child is not element and child.tag in {"td", "th", "li", "dt", "dd"}:
            return True
    return False


def is_visual_block(element: etree._Element) -> bool:
    return element.tag in EMPTY_BLOCK_TAGS or bool(set(element.get("class", "").split()) & TEXT_CHROME_CLASSES)


def image_dimensions(element: etree._Element) -> dict[str, str]:
    """Retain bounded pixel sizing without carrying vendor CSS into the site."""
    dimensions = {}
    for name in ("width", "height"):
        value = element.get(name, "")
        if re.fullmatch(r"[0-9]{1,4}", value) and 0 < int(value) <= 9999:
            dimensions[name] = str(int(value))
    for declaration in element.get("style", "").split(";"):
        property_name, separator, value = declaration.partition(":")
        property_name = property_name.strip().lower()
        if not separator or property_name not in {"width", "height", "max-width", "max-height"}:
            continue
        value = value.strip().lower()
        if value == "auto" and property_name in {"width", "height"}:
            dimensions.pop(property_name, None)
            continue
        match = re.fullmatch(r"([0-9]{1,4}(?:\.[0-9]+)?|\.[0-9]+)px", value)
        if not match:
            continue
        pixels = float(match.group(1))
        if not 0 < pixels <= 9999:
            continue
        name = f"data-source-{property_name}" if property_name.startswith("max-") else property_name
        dimensions[name] = str(max(1, round(pixels)))
    return dimensions


def json_array_after(source: str, marker: str) -> list:
    position = source.find(marker)
    if position < 0:
        raise ValueError(f"Missing expected JSON marker: {marker}")
    result, _ = JSON_DECODER.raw_decode(source[position:])
    if not isinstance(result, list):
        raise ValueError("Expected an array in official navigation")
    return result


def parse_toc(path: Path) -> list:
    return json_array_after(path.read_text(encoding="utf-8-sig"), '[{"id":')


def parse_alphabetical_index(path: Path) -> list:
    source = path.read_text(encoding="utf-8-sig")
    marker = '"treeNodes":'
    start = source.find(marker)
    if start < 0:
        raise ValueError("Official alphabetical index has no treeNodes field")
    encoded, _ = JSON_DECODER.raw_decode(source[start + len(marker):])
    if not isinstance(encoded, str):
        raise ValueError("Official alphabetical index treeNodes is not a string")
    nodes = json.loads(encoded)
    if not isinstance(nodes, list):
        raise ValueError("Official alphabetical index is not an array")
    return nodes


def walk_nodes(nodes: list):
    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError("Malformed official navigation node")
        yield node
        children = node.get("c", [])
        if not isinstance(children, list):
            raise ValueError("Malformed official navigation children")
        yield from walk_nodes(children)


def fence(value: str, language: str = "") -> str:
    longest = max((len(m.group()) for m in re.finditer(r"`+", value)), default=0)
    delimiter = "`" * max(3, longest + 1)
    return f"\n\n{delimiter}{language}\n{value.strip()}\n{delimiter}\n\n"


def inline_code(value: str) -> str:
    if "\n" in value:
        value = re.sub(r"\s+", " ", value)
    longest = max((len(m.group()) for m in re.finditer(r"`+", value)), default=0)
    delimiter = "`" * (longest + 1)
    padding = " " if value.startswith("`") or value.endswith("`") else ""
    return f"{delimiter}{padding}{value}{padding}{delimiter}"


def escaped_text(value: str) -> str:
    value = re.sub(r"\s+", " ", value.replace("\xa0", " "))
    return re.sub(r"([\\`*_\[\]<>])", r"\\\1", value)


def inline_spacing(original: str, rendered: str) -> str:
    """Keep spaces carried inside an inline tag outside Markdown delimiters."""
    before = " " if original[:1].isspace() else ""
    after = " " if original[-1:].isspace() else ""
    return before + rendered + after


def input_command(table: etree._Element) -> tuple[str, str]:
    cells = table.xpath('.//*[contains(concat(" ", normalize-space(@class), " "), " maInputBoxTextCell ")]')
    raw = compact_text(cells[0]) if cells else compact_text(table)
    # The prompt is retained separately. Only the known prompt format is removed.
    command = re.sub(r"^.*?\[[^\]]*\]\s*>\s*", "", raw, count=1)
    return command, raw


class ManualConverter:
    def __init__(self, source: Path, root: Path, version: str):
        self.source = source.resolve()
        self.root = root.resolve()
        self.version = version
        self.files = sorted((self.source / "HTML").rglob("*.html"))
        self.stems = {path.stem for path in self.files}
        if len(self.stems) != len(self.files):
            raise ValueError("Duplicate source HTML basenames need explicit document IDs")
        self.parents: dict[str, str | None] = {}
        self.crumbs: dict[str, list[str]] = {}
        self.releases: set[str] = set()
        self.release_versions: dict[str, str] = {}
        self.index_aliases: dict[str, set[str]] = defaultdict(set)
        self.asset_manifest: dict[str, dict] = {}
        self.missing_links: set[tuple[str, str]] = set()
        self.missing_assets: set[tuple[str, str]] = set()
        self.external_media: set[tuple[str, str]] = set()
        self.original_anchors: dict[str, set[str]] = {}
        self.outgoing_links: dict[str, list[dict]] = defaultdict(list)
        self.index_coverage: list[dict] = []
        self.warnings: list[dict] = []
        self.navigation: list[dict] = []
        self.documents: list[dict] = []
        self.commands: list[dict] = []
        self.command_names: dict[str, str] = {}
        self.console_controls_removed = 0
        self.quality_audit: dict[str, dict] = {}

    def prepare_navigation(self):
        toc_path = self.source / "_webHelpScripts/Master/toc_nav.js"
        nodes = parse_toc(toc_path)

        def convert(nodes: list, parent: str | None, crumbs: list[str], release: bool, release_version: str | None):
            result = []
            for node in nodes:
                if not isinstance(node, dict):
                    raise ValueError("Invalid navigation node")
                title = str(node.get("t", "")).strip()
                values = node.get("vals", {})
                if not isinstance(values, dict):
                    raise ValueError("Invalid navigation metadata")
                stem = str(values.get("e", ""))
                is_release = release or stem == "release_notes"
                this_release_version = release_version
                version_match = re.search(r"(?:Version|Release Notes)\s+(\d+(?:\.\d+)+)", title, re.I)
                if is_release and version_match:
                    this_release_version = version_match.group(1)
                if stem:
                    self.parents[stem] = f"manual/{parent}" if parent else None
                    self.crumbs[stem] = crumbs
                    if is_release:
                        self.releases.add(stem)
                        if this_release_version:
                            self.release_versions[stem] = this_release_version
                children = node.get("c", [])
                if not isinstance(children, list):
                    raise ValueError("Invalid navigation children")
                converted = {
                    "id": f"manual/{stem}" if stem else None,
                    "title": title,
                    "children": convert(children, stem or parent, crumbs + [title], is_release, this_release_version),
                }
                result.append(converted)
            return result

        self.navigation = convert(nodes, None, [], False, None)
        # These supplementary release splash pages sit outside the official TOC.
        self.releases.update({"key_releasenotes", "splash_key_releasenotes"} & self.stems)
        toc_stems = {str(n.get("vals", {}).get("e", "")) for n in walk_nodes(nodes)} - {""}
        self.index_coverage.append({
            "source": str(toc_path), "type": "official_toc",
            "targets": len(toc_stems), "present": len(toc_stems & self.stems),
            "missing": sorted(toc_stems - self.stems),
        })
        index_path = self.source / "_webHelpScripts/Index/index_data.js"
        index_nodes = parse_alphabetical_index(index_path)
        index_targets = set()
        for node in walk_nodes(index_nodes):
            values = node.get("vals", {})
            if not isinstance(values, dict):
                raise ValueError("Invalid alphabetical index metadata")
            for stem in str(values.get("i", "")).split("|"):
                if stem:
                    index_targets.add(stem)
                    self.index_aliases[stem].add(str(node.get("t", "")))
        self.index_coverage.append({
            "source": str(index_path), "type": "official_alphabetical_index",
            "targets": len(index_targets), "present": len(index_targets & self.stems),
            "missing": sorted(index_targets - self.stems),
        })
        for path in self.files:
            if not path.stem.startswith(("keyword_", "ok_")):
                continue
            tree = html.parse(str(path))
            title = str(tree.xpath('string(//*[@id="pnlTitle"])')).strip()
            name = re.sub(r"\s+(?:Option )?Keywords?$", "", title)
            name = re.sub(r"\s+\([^)]*\)$", "", name).strip()
            if name:
                self.command_names[name.casefold()] = name

    def asset(self, value: str, source_file: Path, markdown: bool) -> str:
        parsed = urlparse(value)
        if parsed.scheme or parsed.netloc:
            if parsed.scheme in {"https", "http"}:
                self.external_media.add((source_file.name, value))
                return value
            return ""
        asset_path = (source_file.parent / unquote(parsed.path)).resolve()
        if not asset_path.is_relative_to(self.source) or not asset_path.is_file():
            self.missing_assets.add((source_file.name, value))
            return ""
        if asset_path.suffix.lower() not in ASSET_EXTENSIONS:
            self.warnings.append({"source": source_file.name, "unsupported_asset": value})
            return ""
        relative = asset_path.relative_to(self.source)
        target = self.root / "assets/manual" / relative
        key = target.relative_to(self.root).as_posix()
        if key not in self.asset_manifest:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(asset_path, target)
            self.asset_manifest[key] = {
                "source_path": str(asset_path), "path": key,
                "sha256": sha256_bytes(asset_path.read_bytes()), "bytes": asset_path.stat().st_size,
            }
        return f"../../{quote(key)}" if markdown else f"/{quote(key)}"

    def link(self, value: str, source_file: Path, markdown: bool) -> str:
        if value.startswith("www."):
            return "https://" + value.replace(" ", "")
        if value.startswith(("/Page/", "/articles/")):
            return "https://help.malighting.com" + value
        parsed = urlparse(value)
        if parsed.scheme or parsed.netloc:
            if parsed.scheme in {"https", "http", "mailto"}:
                return value
            return ""
        if not parsed.path:
            return f"#{parsed.fragment}" if parsed.fragment else ""
        path = (source_file.parent / unquote(parsed.path)).resolve()
        if path.suffix.lower() in {".html", ".htm"}:
            stem = path.stem
            if path.is_relative_to(self.source / "HTML") and path.is_file() and stem in self.stems:
                self.outgoing_links[source_file.stem].append({"target": stem, "fragment": unquote(parsed.fragment)})
                fragment = f"#{parsed.fragment}" if parsed.fragment else ""
                return f"{quote(stem)}.md{fragment}" if markdown else f"/?doc={quote('manual/' + stem, safe='')}{fragment}"
            self.missing_links.add((source_file.name, value))
            if path.is_relative_to(self.source):
                return f"https://help.malighting.com/grandMA3/{self.version}/{path.relative_to(self.source).as_posix()}"
            return ""
        return self.asset(value, source_file, markdown)

    def cleaned_article(self, tree: etree._ElementTree, source_file: Path) -> etree._Element:
        containers = tree.xpath('//*[@id="pnlTopicContentContainer"]')
        if not containers:
            containers = tree.xpath("//body")
        if not containers:
            raise ValueError(f"No topic or body content in {source_file}")
        article = copy.deepcopy(containers[0])
        article.tag = "div"
        originally_empty = {element for element in article.iter() if isinstance(element.tag, str) and not has_visible_content(element)}
        for link in article.xpath('.//a[starts-with(@href, "#ma_cmd?")]'):
            parent = link.getparent()
            if parent is not None and has_class(parent, "ContextHelp"):
                parent.drop_tree()
            else:
                link.drop_tree()
            self.console_controls_removed += 1
        # This includes all sibling offline-subtopics, several of which contain
        # substantive examples rather than only navigation.
        for table in article.xpath('.//table[.//*[contains(concat(" ", normalize-space(@class), " "), " CHBreadcrumb ")]]'):
            if table.getparent() is not None:
                table.drop_tree()
        for element in list(article.iter()):
            if not isinstance(element.tag, str):
                if element.getparent() is not None:
                    element.drop_tree()
                continue
            tag = element.tag.lower()
            if tag in DROP_TAGS or has_class(element, "CHHeadingLink") or has_class(element, "CHCodeSample_copyCode"):
                if tag == "iframe" and element.get("src"):
                    self.external_media.add((source_file.name, element.get("src", "")))
                    # Source videos occur inside paragraphs and headings. An
                    # inline replacement avoids invalid nested block elements.
                    replacement = html.Element("span")
                    replacement.text = "External video: "
                    link = etree.SubElement(replacement, "a", href=element.get("src"))
                    link.text = element.get("title") or "Watch source video"
                    element.addprevious(replacement)
                if element.getparent() is not None:
                    element.drop_tree()
            elif tag not in ALLOWED_TAGS:
                if element.getparent() is not None:
                    element.drop_tag()
        for element in article.iter():
            if not isinstance(element.tag, str):
                continue
            dimensions = image_dimensions(element) if element.tag == "img" else {}
            for attribute in list(element.attrib):
                if attribute.lower() not in ALLOWED_ATTRIBUTES or (element.tag == "img" and attribute in {"width", "height"}):
                    del element.attrib[attribute]
            element.attrib.update(dimensions)
            if element.tag == "a":
                href = element.get("href", "")
                if href and urlparse(href).scheme.lower() not in {"", "http", "https", "mailto"}:
                    del element.attrib["href"]
        self.prune_empty_blocks(article, source_file, originally_empty)
        return article

    def prune_empty_blocks(self, article: etree._Element, source_file: Path, originally_empty: set):
        text_before = visible_text(article)
        images_before = [(node.get("src"), node.get("alt")) for node in article.xpath(".//img")]
        anchors_before = Counter(article.xpath(".//@id"))
        links_before = Counter(article.xpath(".//a/@href"))
        removed = []
        for element in reversed(list(article.iter())):
            if element is article or element.getparent() is None or not isinstance(element.tag, str):
                continue
            if not is_visual_block(element) or has_visible_content(element):
                continue
            markers = [node for node in element.iter() if node.get("id") or node.tag == "a" and "href" in node.attrib]
            removed.append({
                "tag": element.tag, "class": element.get("class", ""),
                "origin": "empty-in-source" if element in originally_empty else "empty-after-chrome-removal",
            })
            for marker in markers:
                replacement = html.Element("a" if marker.tag == "a" and "href" in marker.attrib else "span")
                for attribute in ("id", "href", "title"):
                    if attribute in marker.attrib:
                        replacement.set(attribute, marker.get(attribute))
                element.addprevious(replacement)
            if element.tag not in EMPTY_BLOCK_TAGS or element.tag == "code" or any(parent.tag == "pre" for parent in element.iterancestors()):
                # Inline whitespace can separate words or indent code even when
                # its styled wrapper has no visible characters of its own.
                element.tail = "".join(element.itertext()) + (element.tail or "")
            element.drop_tree()
        empty_topic = not has_visible_content(article)
        if empty_topic:
            # Some published topics genuinely have no body. Preserve their
            # anchor without inventing text or rendering an empty article box.
            article.tag = "span"
            article.attrib.pop("class", None)
        images_after = [(node.get("src"), node.get("alt")) for node in article.xpath(".//img")]
        if visible_text(article) != text_before or images_after != images_before or Counter(article.xpath(".//@id")) != anchors_before or Counter(article.xpath(".//a/@href")) != links_before:
            raise ValueError(f"Empty-block cleanup changed source content or anchors: {source_file.name}")
        self.quality_audit[source_file.name] = {
            "source": str(source_file), "document_id": f"manual/{source_file.stem}",
            "source_topic_body_empty": empty_topic,
            "removed_empty_blocks": removed,
            "remaining_empty_visual_blocks": sum(
                is_visual_block(node) and not has_visible_content(node)
                for node in article.iterdescendants() if isinstance(node.tag, str)
            ),
            "structural_empty_cells_retained": sum(not has_visible_content(node) for node in article.xpath(".//td|.//th")),
            "text_images_and_anchors_preserved": True,
        }

    def rewritten_html(self, article: etree._Element, source_file: Path, markdown: bool = False) -> str:
        result = copy.deepcopy(article)
        for element in list(result.iter()):
            if not isinstance(element.tag, str):
                continue
            if is_source_code(element) and element.tag not in {"code", "pre"} and not element.xpath("ancestor::pre | ancestor::code"):
                if element.tag in {"p", "li", "div"}:
                    # Keep paragraph, list and legacy line boundaries, along
                    # with nested source anchors and links inside the code.
                    code = html.Element("code")
                    code.text = element.text
                    element.text = None
                    for child in list(element):
                        code.append(child)
                    element.append(code)
                else:
                    element.tag = "code"
            if element.tag == "a" and element.get("href"):
                value = self.link(element.get("href", ""), source_file, markdown)
                if value:
                    element.set("href", value)
                else:
                    element.attrib.pop("href", None)
            if element.tag == "img" and element.get("src"):
                value = self.asset(element.get("src", ""), source_file, markdown)
                if value:
                    element.set("src", value)
                    element.set("loading", "lazy")
                else:
                    element.tag = "span"
                    element.text = f"[Unavailable source image: {element.get('alt', '')}]"
                    element.attrib.clear()
        return html.tostring(result, encoding="unicode", method="html")

    def markdown(self, article: etree._Element, source_file: Path) -> str:
        def contents(element: etree._Element) -> str:
            value = escaped_text(element.text or "")
            for child in element:
                value += render(child)
                value += escaped_text(child.tail or "")
            return value

        def table(element: etree._Element) -> str:
            classes = element.get("class", "")
            if "maInputBox" in classes:
                command, _ = input_command(element)
                return fence(command, "ma3")
            if "CHCodeSample" in classes:
                pres = element.xpath('.//pre[not(ancestor::pre)]')
                lang_nodes = element.xpath('.//*[contains(@class, "CHCodeSample_langName")]')
                language = compact_text(lang_nodes[0]).lower() if lang_nodes else ""
                return fence(code_text(pres[0]) if pres else compact_text(element), language)
            if "maFeedbackBox" in classes:
                return fence(compact_text(element), "text")
            if re.search(r"ma(?:Hint|Important|Warning|Restriction|Limitation)Box", classes):
                pieces = []
                for cell in element.xpath("./tbody/tr/td | ./tr/td"):
                    if "ImageCell" not in cell.get("class", ""):
                        value = contents(cell).strip()
                        if value:
                            pieces.append(value)
                value = "\n\n".join(pieces)
                return "\n\n" + "\n".join("> " + line if line else ">" for line in value.splitlines()) + "\n\n"
            # Merged cells cannot be represented faithfully by pipe tables.
            if element.xpath('.//*[@rowspan and @rowspan!="1"] | .//*[@colspan and @colspan!="1"] | .//table'):
                return "\n\n" + self.rewritten_html(element, source_file, markdown=True) + "\n\n"
            rows = element.xpath("./tr | ./thead/tr | ./tbody/tr | ./tfoot/tr")
            values = []
            for row in rows:
                values.append([
                    re.sub(r"\s*\n\s*", "<br>", contents(cell).strip()).replace("|", "\\|")
                    for cell in row if cell.tag in {"td", "th"}
                ])
            values = [row for row in values if row]
            if not values:
                return ""
            width = max(len(row) for row in values)
            values = [row + [""] * (width - len(row)) for row in values]
            has_heading = bool(rows and rows[0].xpath("./th"))
            if not has_heading:
                values.insert(0, [""] * width)
            lines = ["| " + " | ".join(values[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
            lines.extend("| " + " | ".join(row) + " |" for row in values[1:])
            return "\n\n" + "\n".join(lines) + "\n\n"

        def render(element: etree._Element) -> str:
            value = render_body(element)
            anchor = element.get("id")
            wrappers = {"pnlTopicContentContainer", "offline-topic", "offline-content", "offline-subtopics"}
            if anchor and anchor not in wrappers and element.tag != "a" and not re.fullmatch(r"h[1-6]", str(element.tag)):
                return f'<a id="{html_escape.escape(anchor, quote=True)}"></a>\n' + value
            return value

        def render_body(element: etree._Element) -> str:
            tag = element.tag
            if not isinstance(tag, str):
                return ""
            if tag == "table":
                return table(element)
            if tag == "pre":
                return fence(code_text(element), "")
            if tag == "code" or is_source_code(element):
                value = compact_text(element)
                if not value:
                    return ""
                rendered = inline_spacing("".join(element.itertext()).replace("\xa0", " "), inline_code(value))
                if tag in {"div", "p", "li"}:
                    return "\n\n" + rendered.strip() + "\n\n"
                return rendered
            if tag in {"strong", "b", "em", "i"}:
                original = contents(element)
                value = original.strip()
                delimiter = "**" if tag in {"strong", "b"} else "*"
                return inline_spacing(original, delimiter + value + delimiter) if value else original
            if tag == "a":
                original = contents(element)
                value = original.strip()
                href = self.link(element.get("href", ""), source_file, True)
                anchor = element.get("id")
                prefix = f'<a id="{html_escape.escape(anchor, quote=True)}"></a>' if anchor else ""
                return prefix + inline_spacing(original, f"[{value}]({href.replace(' ', '%20')})" if value and href else value)
            if tag == "img":
                source = self.asset(element.get("src", ""), source_file, True)
                alt = escaped_text(element.get("alt", "")).strip()
                dimension_names = ("width", "height", "data-source-max-width", "data-source-max-height")
                if source and any(element.get(name) for name in dimension_names):
                    image = html.Element("img", src=source, alt=element.get("alt", ""))
                    for name in dimension_names:
                        if element.get(name):
                            image.set(name, element.get(name))
                    return html.tostring(image, encoding="unicode", method="html")
                return f"![{alt}]({source})" if source else f"[Unavailable source image: {alt}]"
            if re.fullmatch(r"h[1-6]", tag):
                value = contents(element).strip()
                anchor = element.get("id")
                prefix = f'<a id="{html_escape.escape(anchor, quote=True)}"></a>\n\n' if anchor else ""
                if not value:
                    return prefix
                return "\n\n" + prefix + "#" * max(2, int(tag[1])) + " " + value + "\n\n"
            if tag in {"ul", "ol"}:
                items = []
                number = int(element.get("start", "1")) if element.get("start", "1").isdigit() else 1
                for child in element:
                    if child.tag != "li":
                        continue
                    value = render_body(child).strip() if is_source_code(child) else contents(child).strip()
                    if child.get("id"):
                        value = f'<a id="{html_escape.escape(child.get("id"), quote=True)}"></a>' + value
                    marker = "- " if tag == "ul" else f"{number}. "
                    if value:
                        lines = value.splitlines()
                        items.append(marker + lines[0] + "\n" + "\n".join(" " * len(marker) + line if line else "" for line in lines[1:]))
                    number += 1
                return "\n\n" + "\n".join(items) + "\n\n" if items else ""
            if tag == "br":
                return "  \n"
            if tag == "hr":
                return "\n\n---\n\n"
            if tag in {"sup", "sub", "u", "kbd"}:
                return f"<{tag}>{contents(element)}</{tag}>"
            if tag in {"p", "div", "section", "article", "main", "figure", "figcaption", "blockquote", "dl", "dt", "dd"}:
                value = contents(element).strip()
                return "\n\n" + value + "\n\n" if value else ""
            return contents(element)

        value = render(article)
        # Only collapse structural blank lines; never alter fenced literal code.
        chunks = re.split(r"(^`{3,}[^\n]*\n[\s\S]*?^`{3,}\s*$)", value, flags=re.M)
        for index in range(0, len(chunks), 2):
            chunks[index] = re.sub(r"\n[ \t]+\n", "\n\n", chunks[index])
            chunks[index] = re.sub(r"\n{3,}", "\n\n", chunks[index])
        return "".join(chunks).strip() + "\n"

    def category(self, stem: str) -> str:
        if stem in self.releases:
            return "release_note"
        if stem.startswith("keyword_"):
            return "command"
        if stem.startswith("ok_"):
            return "option"
        if stem.startswith(("lua_object_", "lua_objectfree_")):
            return "lua"
        if stem in {"help", "csk_general_keywords", "option_keywords", "lua_object", "lua_objectfree", "command_syntax_keywords", "csk_internalkeywords"}:
            return "index"
        return "topic"

    def extract_commands(self, article: etree._Element, metadata: dict, name: str):
        records = []
        category = metadata["category"]

        def add(text: str, kind: str, language: str = "ma3", raw_text: str | None = None, status: str = "documented-not-executed", source_style: str | None = None):
            if not text.strip():
                return
            digest = sha256_bytes((kind + "\0" + text).encode())[:16]
            command_name = name if category in {"command", "option", "lua"} else ""
            if not command_name and language == "ma3":
                first_token = text.strip().split()[0]
                command_name = self.command_names.get(first_token.casefold(), "")
            record = {
                "id": f"{metadata['id']}/{kind}/{digest}",
                "document_id": metadata["id"], "command": command_name,
                "text": text, "type": kind, "language": language,
                "version": metadata["version"], "page_version": metadata["page_version"],
                "authority": metadata["authority"],
                "source_path": metadata["source_path"], "source_url": metadata["source_url"],
                "status": status,
            }
            if raw_text is not None:
                record["raw_text"] = raw_text
            if source_style:
                record["source_style"] = source_style
            records.append(record)

        if category in {"command", "option", "lua"}:
            add(name, "keyword", "lua" if category == "lua" else "ma3")
        if category == "lua" and "(" in metadata["title"]:
            add(metadata["title"], "syntax", "lua")
        for element in article.xpath('.//*[contains(concat(" ", normalize-space(@class), " "), " syntax ")]'):
            value = compact_text(element)
            add(value, "syntax", "lua" if category == "lua" else "ma3")
        for table in article.xpath('.//table[contains(concat(" ", normalize-space(@class), " "), " maInputBox ")]'):
            command, raw = input_command(table)
            add(command, "example", raw_text=raw)
        for table in article.xpath('.//table[contains(@class, "CHCodeSample_container")]'):
            pres = table.xpath('.//pre[not(ancestor::pre)]')
            lang = table.xpath('.//*[contains(@class, "CHCodeSample_langName")]')
            if pres:
                add(code_text(pres[0]), "example", compact_text(lang[0]).lower() if lang else "text")
        for pre in article.xpath('.//pre[not(ancestor::pre) and not(ancestor::table[contains(@class, "CHCodeSample_container")])]'):
            add(code_text(pre), "example", "lua" if category == "lua" else "text", status="source-code-block-unverified")
        for element in article.xpath('.//*[contains(concat(" ", normalize-space(@class), " "), " commandLineInput ")]'):
            if element.xpath('ancestor::pre | ancestor::*[contains(concat(" ", normalize-space(@class), " "), " maInputBox ")]'):
                continue
            command, raw = input_command(element)
            add(command, "example", raw_text=raw, source_style="commandLineInput")
        # Earlier release pages encode entire Lua examples as adjacent divs,
        # one source line per div. Preserve that block rather than indexing
        # comments, table fields and closing `end` tokens as separate examples.
        consumed = set()
        inline_nodes = [node for node in article.iter() if isinstance(node.tag, str) and any(value.rstrip(",") == "inlineCode" for value in node.get("class", "").split())]
        for element in inline_nodes:
            if element in consumed or element.xpath('ancestor::pre | ancestor::*[contains(concat(" ", normalize-space(@class), " "), " inlineCode ")]'):
                continue
            group = [element]
            if element.tag == "div":
                following = element.getnext()
                while following is not None and following.tag == "div" and has_class(following, "inlineCode"):
                    group.append(following)
                    following = following.getnext()
            consumed.update(group)
            value = "\n".join(compact_text(node) for node in group)
            # The class is also used for XML samples. Only explicitly styled
            # function calls or Lua program openings qualify for this index.
            lua_opening = re.match(r"^(?:(?:return\s+)?(?:local\s+)?function\b|local\s+[A-Za-z_]|[A-Za-z_][A-Za-z_0-9]*(?:[.:][A-Za-z_][A-Za-z_0-9]*)*\s*\()", value)
            if not lua_opening:
                continue
            kind = "syntax" if len(group) == 1 and re.search(r"\b(?:light_userdata|boolean|string|number|integer):", value) else "example"
            add(value, kind, "lua", raw_text="\n".join(code_text(node) for node in group), status="source-inline-code-unverified", source_style="inlineCode")
        seen = set()
        for record in records:
            if record["id"] not in seen:
                self.commands.append(record)
                seen.add(record["id"])
        return [r for r in records if r["type"] == "syntax"]

    def convert_page(self, source_file: Path):
        source_bytes = source_file.read_bytes()
        tree = html.parse(str(source_file))
        titles = tree.xpath('//*[@id="pnlTitle"]')
        title = compact_text(titles[0]) if titles else str(tree.xpath("string(//title)"))
        title = title.strip()
        stem = source_file.stem
        self.original_anchors[stem] = set(tree.xpath("//@id"))
        original_containers = tree.xpath('//*[@id="pnlTopicContentContainer"]') or tree.xpath("//body")
        original_content = original_containers[0]
        version_tables = original_content.xpath('.//table[.//*[contains(concat(" ", normalize-space(@class), " "), " CHBreadcrumb ")]]')
        header_text = compact_text(version_tables[0]) if version_tables else ""
        match = re.search(r"\bVersion\s+(\d+(?:\.\d+)+)", header_text)
        page_version = match.group(1) if match else None
        article = self.cleaned_article(tree, source_file)
        headings = Counter(compact_text(node) for node in article.xpath(".//h1|.//h2|.//h3|.//h4|.//h5|.//h6") if compact_text(node))
        self.quality_audit[source_file.name].update({
            "source_headings_equal_document_title": [
                {"tag": node.tag, "id": node.get("id"), "text": compact_text(node)}
                for node in article.xpath(".//h1") if compact_text(node) == title
            ],
            "repeated_source_heading_labels_retained": {label: count for label, count in headings.items() if count > 1},
        })
        category = self.category(stem)
        is_release = stem in self.releases
        version = self.release_versions.get(stem, self.version) if is_release else self.version
        if is_release:
            match = re.search(r"(?:Version|Release Notes)\s+(\d+(?:\.\d+)+)", title, re.I)
            if match:
                version = match.group(1)
            elif stem in {"key_releasenotes", "splash_key_releasenotes"}:
                match = re.search(r"\bversion\s+(\d+(?:\.\d+)+)", compact_text(article), re.I)
                if match:
                    version = match.group(1)
        aliases = set(self.index_aliases.get(stem, set()))
        for item in article.xpath(".//li"):
            if re.match(r"Type (?:the )?shortcut", compact_text(item), re.I):
                for strong in item.xpath(".//strong | .//b"):
                    aliases.add(compact_text(strong))
        aliases.discard(title)
        aliases.discard("")
        name = re.sub(r"\s+(?:Option )?Keywords?$", "", title)
        if category == "lua":
            name = name.split("(", 1)[0].strip()
        elif category in {"command", "option"}:
            name = re.sub(r"\s+\([^)]*\)$", "", name).strip()
        metadata = {
            "id": f"manual/{stem}", "title": title,
            "kind": "release_note" if is_release else "manual", "category": category,
            "version": version, "publication_version": self.version, "page_version": page_version,
            "authority": "release-note" if is_release else "manual",
            "source_path": str(source_file),
            "source_url": f"https://help.malighting.com/grandMA3/{self.version}/{quote(source_file.relative_to(self.source).as_posix())}",
            "source_sha256": sha256_bytes(source_bytes),
            "markdown_path": f"content/manual/{stem}.md", "html_path": f"content/manual/{stem}.html",
            "text": search_text(article),
            "breadcrumbs": self.crumbs.get(stem, []), "parent_id": self.parents.get(stem),
            "aliases": sorted(aliases, key=str.casefold), "searchable": True,
        }
        syntax = self.extract_commands(article, metadata, name)
        if category in {"command", "option", "lua"}:
            metadata["command"] = name
            metadata["syntax"] = [entry["text"] for entry in syntax]
        notice = (
            f"Release-note history for {version}; supplementary evidence only. The current {self.version} manual takes precedence."
            if is_release else f"Authoritative baseline: installed grandMA3 {self.version} manual."
        )
        provenance = {
            key: metadata[key] for key in ("id", "title", "kind", "category", "version", "publication_version", "page_version", "authority", "source_path", "source_url", "source_sha256")
        }
        frontmatter = "---\n" + "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in provenance.items()) + "\n---\n\n"
        markdown = frontmatter + "# " + escaped_text(title) + "\n\n> " + notice + "\n\n"
        markdown += f"Source: [MA Lighting manual]({metadata['source_url']})"
        if page_version:
            markdown += f" · Page label: {page_version} · Publication: {self.version}"
        markdown += "\n\n" + self.markdown(article, source_file)
        (self.root / metadata["markdown_path"]).write_text(markdown, encoding="utf-8")
        fragment = '<aside class="source-note"><p>' + html_escape.escape(notice) + '</p><p>Source: <a href="' + html_escape.escape(metadata["source_url"], quote=True) + '">MA Lighting manual</a>'
        if page_version:
            fragment += f" · Page label: {page_version} · Publication: {self.version}"
        fragment += "</p></aside>\n" + self.rewritten_html(article, source_file)
        (self.root / metadata["html_path"]).write_text(fragment, encoding="utf-8")
        self.documents.append(metadata)
        links = article.xpath(".//a/@href")
        local_targets = {Path(unquote(urlparse(link).path)).stem for link in links if urlparse(link).path.endswith(".html") and not urlparse(link).scheme}
        if category == "index" or stem in {"lua_objectfree", "lua_object", "release_notes"} or (len(local_targets) >= 10 and not article.xpath('.//*[contains(@class,"maInputBox")]')):
            self.index_coverage.append({
                "source": str(source_file), "type": "topic_index",
                "targets": len(local_targets), "present": len(local_targets & self.stems),
                "missing": sorted(local_targets - self.stems),
            })

    def run(self) -> dict:
        if not self.files:
            raise ValueError(f"No HTML manual found at {self.source}")
        for directory in ["content/manual", "data/manual", "assets/manual"]:
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        self.prepare_navigation()
        for path in self.files:
            self.convert_page(path)
        data_path = self.root / "data/manual"
        self.write_jsonl(data_path / "documents.jsonl", self.documents)
        self.write_jsonl(data_path / "commands.jsonl", self.commands)
        (data_path / "navigation.json").write_text(json.dumps(self.navigation, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        (data_path / "index-coverage.json").write_text(json.dumps(self.index_coverage, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        (data_path / "assets.json").write_text(json.dumps(list(self.asset_manifest.values()), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        removed_blocks = [block for page in self.quality_audit.values() for block in page["removed_empty_blocks"]]
        quality_summary = {
            "pages_checked": len(self.quality_audit),
            "removed_empty_blocks": len(removed_blocks),
            "removed_by_tag": dict(sorted(Counter(block["tag"] for block in removed_blocks).items())),
            "removed_by_origin": dict(sorted(Counter(block["origin"] for block in removed_blocks).items())),
            "remaining_empty_visual_blocks": sum(page["remaining_empty_visual_blocks"] for page in self.quality_audit.values()),
            "structural_empty_cells_retained": sum(page["structural_empty_cells_retained"] for page in self.quality_audit.values()),
            "source_headings_equal_document_title": sum(len(page["source_headings_equal_document_title"]) for page in self.quality_audit.values()),
            "source_empty_topic_bodies": [page["document_id"] for page in self.quality_audit.values() if page["source_topic_body_empty"]],
            "text_images_and_anchors_preserved": all(page["text_images_and_anchors_preserved"] for page in self.quality_audit.values()),
        }
        (data_path / "quality-audit.json").write_text(json.dumps({
            **quality_summary,
            "policy": "Remove whitespace-only visual containers; preserve anchors and link targets, meaningful text, images, code, structural table cells and repeated source headings. Source title duplication is a presentation concern.",
            "pages": list(self.quality_audit.values()),
        }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        missing_fragments = sorted({
            (stem, link["target"], link["fragment"])
            for stem, links in self.outgoing_links.items() for link in links
            if link["fragment"] and link["fragment"] not in self.original_anchors.get(link["target"], set())
        })
        report = {
            "source": str(self.source), "publication_version": self.version,
            "input_html_files": len(self.files), "output_documents": len(self.documents),
            "categories": dict(sorted(Counter(d["category"] for d in self.documents).items())),
            "kinds": dict(sorted(Counter(d["kind"] for d in self.documents).items())),
            "command_records": len(self.commands),
            "command_record_types": dict(sorted(Counter(c["type"] for c in self.commands).items())),
            "legacy_source_extracts": dict(sorted(Counter(c["source_style"] for c in self.commands if c.get("source_style")).items())),
            "assets": len(self.asset_manifest), "asset_bytes": sum(a["bytes"] for a in self.asset_manifest.values()),
            "index_sources_checked": len(self.index_coverage),
            "index_missing_targets": [item for item in self.index_coverage if item["missing"]],
            "missing_links": [{"source": s, "href": h} for s, h in sorted(self.missing_links)],
            "missing_assets": [{"source": s, "src": h} for s, h in sorted(self.missing_assets)],
            "source_missing_fragments": [{"source": s, "target": t, "fragment": f} for s, t, f in missing_fragments],
            "external_media": [{"source": s, "url": u} for s, u in sorted(self.external_media)],
            "console_paste_controls_removed": self.console_controls_removed,
            "empty_block_quality_audit": quality_summary,
            "warnings": self.warnings,
            "limitations": [
                "Page version labels identify the source topic revision, not the installed publication version.",
                "Command examples are transcribed source material, not executed or runtime-verified.",
                "Merged-cell tables are retained as sanitized HTML inside Markdown to preserve their structure.",
                "External videos are linked; no remote media was downloaded.",
            ],
            "source_manifest": [{"path": d["source_path"], "sha256": d["source_sha256"], "document_id": d["id"]} for d in self.documents],
            "navigation_source_manifest": [{"path": str(path), "sha256": sha256_bytes(path.read_bytes())} for path in [self.source / "_webHelpScripts/Master/toc_nav.js", self.source / "_webHelpScripts/Index/index_data.js"]],
        }
        (data_path / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        self.write_catalog()
        return report

    @staticmethod
    def write_jsonl(path: Path, rows: list[dict]):
        with path.open("w", encoding="utf-8") as output:
            for row in rows:
                output.write(json.dumps(row, ensure_ascii=False) + "\n")

    def write_catalog(self):
        lines = ["# Command and Lua reference index", "", f"Generated from the local {self.version} manual.", ""]
        for category, heading in [("command", "Command keywords"), ("option", "Option keywords"), ("lua", "Lua functions")]:
            lines.extend([f"## {heading}", "", "| Reference | Aliases / search terms |", "| --- | --- |"])
            for document in sorted((d for d in self.documents if d["category"] == category), key=lambda d: d["title"].casefold()):
                target = Path(document["markdown_path"]).name
                title = document["title"].replace("|", "\\|")
                aliases = ", ".join(document["aliases"]).replace("|", "\\|")
                lines.append(f"| [{title}]({target}) | {aliases} |")
            lines.append("")
        (self.root / "content/manual/COMMANDS.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--version", default="2.5")
    args = parser.parse_args()
    report = ManualConverter(args.source, args.root, args.version).run()
    print(json.dumps({key: report[key] for key in ["input_html_files", "output_documents", "categories", "command_records", "assets", "index_sources_checked", "index_missing_targets", "missing_assets"]}, indent=2))


if __name__ == "__main__":
    main()
