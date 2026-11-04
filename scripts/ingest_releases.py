#!/usr/bin/env python3
"""Extract local MA3 release PDFs with conservative, provenance-preserving deduplication.

Requires Poppler, pdfplumber and pypdf with image support.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
import hashlib
import html
import json
import os
from pathlib import Path
import re
import statistics
import subprocess
import unicodedata
from urllib.parse import quote
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
NS = {"x": "http://www.w3.org/1999/xhtml"}
VERSION = r"[12]\.\d+(?:\.\d+){0,2}"
PROMPT = re.compile(r"(?:User\s*name|Username)\s*(?:\[[^\]]*\])?\s*>\s*(.*)", re.I)
CATEGORY_NAMES = {
    "features": "features",
    "other enhancements": "enhancements",
    "changes": "changes",
    "what's changed": "changes",
    "bug fixes": "bug-fixes",
    "fixed bugs": "bug-fixes",
    "deprecated": "deprecated",
    "known limitations": "limitations",
    "appendix": "appendix",
    "let's get started": "getting-started",
    "get started": "getting-started",
}
NOTICE = (
    "Historical release notes. Check the manual for your running software version. "
    "Examples are reproduced from the local source PDF."
)


@dataclass
class Line:
    text: str
    page: int
    top: float
    bottom: float
    left: float
    right: float
    height: float
    runs: list = field(default_factory=list)
    images: list = field(default_factory=list)
    font_size: float = 0
    heading_level: int = 0
    anchor: str = ""
    bullet: bool = False
    toc_entry: bool = False
    structure_scanned: bool = False


@dataclass
class Block:
    text: str
    page: int
    style: str
    category: str
    section: str
    version: str
    rich_html: str = ""
    rich_markdown: str = ""
    heading_level: int = 3
    anchor: str = ""
    top: float = 0
    bottom: float = 0
    images: list = field(default_factory=list)
    indent: float = 0
    bullet: bool = False


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def clean_text(value: str) -> str:
    return unicodedata.normalize("NFC", value).replace("\u200b", "").replace("\ufeff", "").replace("\xa0", " ").strip()


def canonical_text(value: str) -> str:
    """Only layout whitespace is normalized; case, numbers and punctuation matter."""
    return re.sub(r"\s+", " ", clean_text(value)).strip()


def version_key(value: str) -> tuple[int, ...]:
    return tuple(int(piece) for piece in value.split("."))


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def run(command: list[str]) -> str:
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout


def source_url(path: Path, page: int | None = None) -> str:
    base = "/release-notes-pdf/" + quote(path.name)
    return base + (f"#page={page}" if page else "")


def markdown_source_url(path: Path, directory: Path, page: int | None = None) -> str:
    base = quote(Path(os.path.relpath(path, directory)).as_posix(), safe="/")
    return base + (f"#page={page}" if page else "")


def category_of(text: str) -> str | None:
    stripped = re.sub(r"^\d+\.\s*", "", text).strip().lower().replace("’", "'")
    return CATEGORY_NAMES.get(stripped)


def release_heading(text: str) -> str | None:
    match = re.fullmatch(
        rf"(?:\d+\.\s*)?(?:grandMA3\s+)?(?:(?:Bug\s*Fix|Release)\s+)?(?:Version|Release Notes)\s+({VERSION})",
        text,
        re.I,
    )
    return match.group(1) if match else None


def infer_versions(pages: list[list[Line]], metadata_title: str) -> dict:
    opening = "\n".join(line.text for page in pages[:3] for line in page)
    family_match = re.search(rf"Release Notes\s+(?:Bug\s*Fix Version\s+)?({VERSION})", opening + "\n" + metadata_title, re.I)
    if not family_match:
        raise ValueError("Cannot identify release family from the PDF content")
    family = ".".join(family_match.group(1).split(".")[:2])
    candidates = []
    for line in [line for page in pages[:4] for line in page]:
        version = release_heading(line.text)
        if version and version.startswith(family + "."):
            candidates.append(version)
    for match in re.finditer(rf"(?:Release[_ ]Notes[_ -]?v?|Version\s+)({VERSION})", metadata_title, re.I):
        if match.group(1).startswith(family + "."):
            candidates.append(match.group(1))
    # First pages may state the base software version in prose, without a heading.
    for match in re.finditer(rf"(?:latest release of grandMA3 (?:software )?(?:version )?|grandMA3 software version )({VERSION})", opening, re.I):
        if match.group(1).startswith(family + "."):
            candidates.append(match.group(1))
    publication_version = max(candidates, key=version_key) if candidates else family
    dates = re.findall(r"\b(?:20\d{2}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}/\d{1,2}/20\d{2})\b", opening)
    return {
        "family": family,
        "publication_version": publication_version,
        "publication_date_text": dates[0] if dates else None,
        "version_evidence": "PDF release headings and PDF title metadata; never filename",
        "date_caution": "Document publication/revision date; not necessarily the original software release date",
    }


def parse_pdf(path: Path, cache: Path) -> tuple[list[list[Line]], dict]:
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    cache.mkdir(parents=True, exist_ok=True)
    xml_path = cache / f"{sha}.xml"
    if not xml_path.exists():
        xml_path.write_text(run(["pdftotext", "-bbox-layout", str(path), "-"]), encoding="utf-8")
    tree = ET.fromstring(xml_path.read_text(encoding="utf-8"))
    title_node = tree.find("x:head/x:title", NS)
    title = title_node.text if title_node is not None and title_node.text else ""
    pages = []
    for page_number, page in enumerate(tree.findall(".//x:page", NS), 1):
        lines = []
        for element in page.findall(".//x:line", NS):
            words = element.findall("x:word", NS)
            text = clean_text(" ".join(word.text or "" for word in words))
            if not text:
                continue
            top, bottom = float(element.attrib["yMin"]), float(element.attrib["yMax"])
            heights = [float(word.attrib["yMax"]) - float(word.attrib["yMin"]) for word in words]
            lines.append(Line(text, page_number, top, bottom, float(element.attrib["xMin"]), float(element.attrib["xMax"]), statistics.median(heights)))
        # Poppler can emit title number and title text as separate blocks at one y.
        lines.sort(key=lambda item: (round(item.top / 3), item.left))
        merged = []
        for line in lines:
            if merged and abs(merged[-1].top - line.top) < 3 and line.left >= merged[-1].right - 1:
                before = merged[-1]
                before.text += " " + line.text
                before.right = line.right
                before.bottom = max(before.bottom, line.bottom)
                before.height = max(before.height, line.height)
            else:
                merged.append(line)
        pages.append(merged)
    return pages, {"sha256": sha, "pdf_title": title, "pages": len(pages), **infer_versions(pages, title)}


def is_furniture(line: Line) -> bool:
    text = line.text
    if text.startswith(("©", "Phone +", "Phone+")):
        return True
    if re.fullmatch(r"(?:Page\s*)?\d+\s*(?:of|von)\s*\d+", text, re.I):
        return True
    if line.top < 115:
        if text == "English" or text.startswith("grandMA3 User Manual"):
            return True
        if re.fullmatch(rf"Release Notes {VERSION}", text):
            return True
        if re.match(rf"Version {VERSION}\s*[|–-]", text):
            return True
        if re.fullmatch(r"\d{1,4}[-/]\d{1,2}[-/]\d{1,4}", text):
            return True
        if re.match(r"\d{1,4}[-/]\d{1,2}[-/]\d{1,4}\s+Release Notes", text):
            return True
    return False


def color_hex(value) -> str:
    """PDF RGB, grayscale and CMYK device colors; always a safe CSS token."""
    values = list(value) if isinstance(value, (tuple, list)) else [value or 0]
    if len(values) == 1:
        values *= 3
    elif len(values) == 4:
        c, m, y, k = values
        values = [(1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k)]
    return "".join(f"{max(0, min(255, round(float(v) * 255))):02x}" for v in values[:3])


def destination_position(reader, destination) -> tuple[int, float] | None:
    if isinstance(destination, str):
        destination = reader.named_destinations.get(destination)
    if destination is None:
        return None
    if isinstance(destination, (tuple, list)):
        target, top = destination[0], destination[3] if len(destination) > 3 else None
    else:
        target, top = destination.get("/Page"), destination.get("/Top")
    if isinstance(target, int):
        number = target
    else:
        number = next((i for i, page in enumerate(reader.pages) if page.indirect_reference == target), None)
    if number is None or not 0 <= number < len(reader.pages):
        return None
    height = float(reader.pages[number].mediabox.height)
    return number + 1, height - float(top) if isinstance(top, (int, float)) else 0


def source_structure(reader, pages: list[list[Line]], toc_pages: list[int]) -> dict:
    """Resolve real PDF TOC links, bookmarks and named anchors before typography."""
    raw_outlines = []

    def walk(items, depth=0):
        for item in items:
            if isinstance(item, list):
                walk(item, depth + 1)
            else:
                position = destination_position(reader, item)
                if position:
                    raw_outlines.append({"title": clean_text(item.title), "page": position[0], "top": position[1], "depth": depth})

    walk(reader.outline)
    toc = []
    for number in sorted(set(toc_pages) | {1}):
        pdf_page = reader.pages[number - 1]
        height = float(pdf_page.mediabox.height)
        links = []
        for ref in pdf_page.get("/Annots", []):
            annotation = ref.get_object()
            if annotation.get("/Subtype") != "/Link":
                continue
            destination = annotation.get("/Dest")
            if destination is None:
                action = annotation.get("/A", {})
                destination = action.get_object().get("/D") if hasattr(action, "get_object") else action.get("/D")
            target = destination_position(reader, destination)
            rect = annotation.get("/Rect")
            if target and rect:
                links.append((float(rect[0]), height - float(rect[3]), float(rect[2]), height - float(rect[1]), target))
        for line in pages[number - 1]:
            if is_furniture(line) or "Table of Contents" in line.text:
                continue
            hit = next((link for link in links if abs((link[1] + link[3]) / 2 - (line.top + line.bottom) / 2) < 8 and link[0] < line.right and link[2] > line.left), None)
            if not hit:
                continue
            title = re.sub(r"\s*\.{2,}\s*\d+\s*$", "", line.text).strip()
            title = re.sub(r"^[·•§o]\s+", "", title)
            if len(title) > 110:
                continue
            toc.append({"title": title, "page": hit[4][0], "top": hit[4][1], "indent": round(hit[0], 1), "source_toc_page": number})
            line.toc_entry = True
    # The old PDFs encode the full feature hierarchy through TOC indentation.
    indents = sorted({item["indent"] for item in toc})
    for item in toc:
        item["level"] = min(5, 2 + indents.index(item["indent"]))
    # In newer notes all numbered chapters are visually subordinate to the
    # introduction in the generated bookmark tree, but have the same printed
    # heading size. Numbered chapters belong at the same section level.
    if any(re.match(r"\d+\. ", item["title"]) for item in toc):
        for item in toc:
            item["level"] = 2
    targets = list(toc)
    for item in raw_outlines:
        if not any(row["page"] == item["page"] and abs(row["top"] - item["top"]) < 4 for row in targets):
            targets.append({**item, "level": 2})
    # Named destinations include feature headings absent from the printed TOC.
    for destination in reader.named_destinations.values():
        position = destination_position(reader, destination)
        if position and not any(row["page"] == position[0] and abs(row["top"] - position[1]) < 4 for row in targets):
            targets.append({"title": "", "page": position[0], "top": position[1], "level": 3})
    for item in targets:
        candidates = [line for line in pages[item["page"] - 1] if not is_furniture(line) and not line.toc_entry]
        if not candidates:
            continue
        compact = re.sub(r"\s", "", item["title"]).lower()
        matches = [line for line in candidates if compact and re.sub(r"\s", "", line.text).lower() == compact]
        if not matches and compact:
            # Some 2.1 TOC links all point at the article's introduction. Repair
            # only by an exact printed heading match elsewhere in that PDF.
            matches = [line for page in pages for line in page if not is_furniture(line) and not line.toc_entry
                       and re.sub(r"\s", "", line.text).lower() == compact and line.height >= 11]
            if matches:
                matches.sort(key=lambda line: (-line.height, line.page))
                matches = [matches[0]]
        line = min(matches or candidates, key=lambda row: abs(row.top - item["top"]))
        if not matches and abs(line.top - item["top"]) > 30:
            continue
        if not item["title"] and line.height < 11:
            continue
        item["title"] = line.text
        item["page"] = line.page
        line.heading_level = item["level"]
        line.anchor = f"section-{slugify(line.text)}-{line.page}-{round(line.top)}"
        item["anchor"] = line.anchor
    for lines in pages:
        for line in lines:
            line.structure_scanned = True
            if line.text == "Table of Contents":
                line.toc_entry = True
    return {"toc": [item for item in toc if "anchor" in item], "outline": raw_outlines, "destinations": len(reader.named_destinations)}


def image_markup(image: dict, command: bool = False) -> str:
    klass = "rn-command-icon" if command else "rn-icon"
    return (f'<img class="{klass}" src="/{image["path"]}" alt="{html.escape(image["alt"], quote=True)}" '
            f'width="{max(1, round(image["width"]))}" height="{max(1, round(image["height"]))}">')


def render_runs(line: Line, start: int = 0, end: int | None = None, markdown: bool = False, include_images: bool = True) -> str:
    end = len(line.text) if end is None else end
    images = [image for image in line.images if start <= image.get("offset", 0) < end
              or (end == len(line.text) and image.get("offset", 0) == end)] if include_images else []
    if images:
        output, cursor = [], start
        for image in sorted(images, key=lambda item: item.get("offset", 0)):
            offset = image.get("offset", 0)
            output.append(render_runs(line, cursor, offset, markdown, False))
            output.append(image_markup(image, bool(PROMPT.search(line.text))))
            cursor = offset
        output.append(render_runs(line, cursor, end, markdown, False))
        return "".join(output)
    output = []
    for run in line.runs or [{"start": 0, "end": len(line.text), "style": {}}]:
        left, right = max(start, run["start"]), min(end, run["end"])
        if left >= right:
            continue
        text = line.text[left:right]
        style = run["style"]
        safe = html.escape(text)
        if style.get("bold"):
            safe = "<strong>" + safe + "</strong>"
        if style.get("italic"):
            safe = "<em>" + safe + "</em>"
        if style.get("underline"):
            safe = "<u>" + safe + "</u>"
        classes = []
        if style.get("background"):
            shade = style["background"]
            classes += ["rn-key", "rn-key-dark" if int(shade[:2], 16) < 110 else "rn-key-light", "rn-bg-" + shade, "rn-fg-" + style.get("color", "ffffff")]
        elif style.get("color") not in {None, "000000", "ffffff"}:
            classes.append("rn-fg-" + style["color"])
        if classes:
            safe = '<span class="' + " ".join(classes) + '">' + safe + "</span>"
        output.append(safe)
    # HTML inline spans are valid Markdown and keep source color/underline where
    # Markdown has no lossless equivalent. Plain text remains separate for search.
    return "".join(output)


def enrich_pdf(path: Path, pages: list[list[Line]], meta: dict, stats: dict, root: Path) -> dict:
    """Cache compact source font/color/geometry and decoded image placements."""
    import pdfplumber
    from pypdf import PdfReader

    cache = root / ".cache/releases" / (meta["sha256"] + ".rich-v6.json")
    previous_cache = cache.with_name(meta["sha256"] + ".rich-v5.json")
    cached = cache if cache.exists() else previous_cache
    if cached.exists():
        result = json.loads(cached.read_text(encoding="utf-8"))
        for lines, saved in zip(pages, result["lines"]):
            if len(lines) != len(saved):
                raise ValueError("Rich PDF cache and positioned text disagree")
            for line, props in zip(lines, saved):
                for key, value in props.items():
                    setattr(line, key, value)
        if cached != cache:
            for lines in pages:
                for line in lines:
                    line.heading_level, line.anchor, line.toc_entry, line.structure_scanned = 0, "", False, False
            result["structure"] = source_structure(PdfReader(path), pages, stats["toc_pages"])
            for lines, saved in zip(pages, result["lines"]):
                for line, props in zip(lines, saved):
                    for key in ("heading_level", "anchor", "toc_entry", "structure_scanned"):
                        props[key] = getattr(line, key)
            cache.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        return result
    reader = PdfReader(path)
    structure = source_structure(reader, pages, stats["toc_pages"])
    result = {"structure": structure, "figures": [], "palette": [], "style_characters": Counter(), "images": [], "alignment": [], "lines": []}
    palette = set()
    assets = root / "assets/releases/images"
    assets.mkdir(parents=True, exist_ok=True)
    with pdfplumber.open(path) as pdf:
        for number, (page, lines) in enumerate(zip(pdf.pages, pages), 1):
            body = [line for line in lines if not is_furniture(line)]
            minimum = min((line.top for line in body), default=0) - 12
            maximum = max((line.bottom for line in body), default=page.height) + 8
            bins = defaultdict(list)
            for char in page.chars:
                bins[round((char["top"] + char["bottom"]) / 2 / 5)].append(char)
            decoded = {}
            for source_image in reader.pages[number - 1].images:
                if source_image.indirect_reference is not None:
                    decoded[source_image.indirect_reference.idnum] = source_image
            for placement in page.images:
                # Repeated logo/header images are furniture; body icons are not.
                if placement["bottom"] < minimum or placement["top"] > maximum or (placement["x0"] > 450 and placement["top"] < 120):
                    continue
                source_image = decoded.get(placement["stream"].objid)
                if source_image is None:
                    raise ValueError(f"Unresolved body image in {path.name}, page {number}")
                extension = Path(source_image.name).suffix.lower()
                image_hash = hashlib.sha256(source_image.data).hexdigest()
                target = assets / (image_hash[:24] + extension)
                if not target.exists():
                    target.write_bytes(source_image.data)
                width, height = placement["x1"] - placement["x0"], placement["bottom"] - placement["top"]
                info = {"path": target.relative_to(root).as_posix(), "page": number, "left": placement["x0"], "top": placement["top"], "bottom": placement["bottom"], "width": width, "height": height, "sha256": image_hash, "alt": "Source release-note icon" if width <= 36 and height <= 36 else "Source release-note illustration"}
                result["images"].append(info)
                if width <= 36 and height <= 36 and body:
                    closest = min(body, key=lambda line: abs((line.top + line.bottom) / 2 - (info["top"] + info["bottom"]) / 2) + max(0, line.left - placement["x1"] - 30))
                    if abs((closest.top + closest.bottom) / 2 - (info["top"] + info["bottom"]) / 2) < max(22, height):
                        closest.images.append(info)
                    else:
                        result["figures"].append(info)
                else:
                    result["figures"].append(info)
            for line in lines:
                middle = (line.top + line.bottom) / 2
                vector_bullet = any(shape.get("fill") and color_hex(shape.get("non_stroking_color")) == "000000"
                                    and 1 <= shape["width"] <= 5 and 1 <= shape["height"] <= 5
                                    and line.left - 20 <= shape["x0"] < line.left - 2
                                    and line.top - 2 <= (shape["top"] + shape["bottom"]) / 2 <= line.bottom + 2 for shape in page.curves)
                glyph_bullet = any(c["text"] in {"·", "•", "§", "o"} and line.left - 28 <= c["x0"] < line.left - 2
                                   and line.top - 2 <= (c["top"] + c["bottom"]) / 2 <= line.bottom + 2 for c in page.chars)
                line.bullet = vector_bullet or glyph_bullet
                candidates = [c for slot in range(round(middle / 5) - 2, round(middle / 5) + 3) for c in bins[slot]
                              if line.top - 2 <= (c["top"] + c["bottom"]) / 2 <= line.bottom + 2 and line.left - 2 <= (c["x0"] + c["x1"]) / 2 <= line.right + 2]
                candidates.sort(key=lambda c: c["x0"])
                raw, styles, raw_x = [], [], []
                rectangles = [r for r in page.rects if r.get("fill") and r["top"] <= middle + 4 and r["bottom"] >= middle - 4 and r["bottom"] - r["top"] > 3]
                underlines = [r for r in page.lines if r.get("stroke") and color_hex(r.get("stroking_color")) == "000000" and r["bottom"] - r["top"] < 1 and abs(r["top"] - line.bottom) < 3]
                for char in candidates:
                    cx, cy = (char["x0"] + char["x1"]) / 2, (char["top"] + char["bottom"]) / 2
                    background = next((r for r in rectangles if r["x0"] <= cx <= r["x1"] and r["top"] <= cy <= r["bottom"] and r["x1"] - r["x0"] < 180 and color_hex(r.get("non_stroking_color")) != "ffffff"), None)
                    color = color_hex(char.get("non_stroking_color"))
                    font = char["fontname"].lower()
                    style = {"bold": "bold" in font, "italic": "italic" in font or "oblique" in font, "underline": any(r["x0"] - 1 <= cx <= r["x1"] + 1 for r in underlines), "color": color}
                    if background:
                        style["background"] = color_hex(background.get("non_stroking_color"))
                        palette.add(("bg", style["background"]))
                        palette.add(("fg", color))
                    elif color not in {"000000", "ffffff"}:
                        palette.add(("fg", color))
                    for letter in unicodedata.normalize("NFKC", char["text"]):
                        if not letter.isspace() and letter not in {"\u200b", "\ufeff"}:
                            raw.append(letter)
                            styles.append(style)
                            raw_x.append(char["x0"])
                target = [(i, expanded) for i, letter in enumerate(line.text) for expanded in unicodedata.normalize("NFKC", letter) if not expanded.isspace()]
                target_text = "".join(letter for _, letter in target)
                mapped = [{} for _ in line.text]
                mapped_x = {}
                aligned = 0
                for opcode, a, b, c, d in SequenceMatcher(None, "".join(raw), target_text, autojunk=False).get_opcodes():
                    if opcode == "equal":
                        for offset in range(b - a):
                            mapped[target[c + offset][0]] = styles[a + offset]
                            mapped_x[target[c + offset][0]] = raw_x[a + offset]
                            aligned += 1
                if target_text and aligned / len(target_text) < 0.95 and not is_furniture(line):
                    result["alignment"].append({"page": number, "text": line.text, "matched": aligned, "characters": len(target_text)})
                for i, letter in enumerate(line.text):
                    if letter.isspace() and i and i + 1 < len(mapped) and mapped[i - 1] == mapped[i + 1]:
                        mapped[i] = mapped[i - 1]
                runs = []
                for i, style in enumerate(mapped):
                    if runs and runs[-1]["style"] == style:
                        runs[-1]["end"] = i + 1
                    else:
                        runs.append({"start": i, "end": i + 1, "style": style})
                    if not is_furniture(line):
                        for kind in ("bold", "italic", "underline", "background"):
                            if style.get(kind):
                                result["style_characters"][kind] += 1
                line.runs = runs
                for image in line.images:
                    image["offset"] = next((i for i, x in sorted(mapped_x.items()) if x >= image["left"] + image["width"] / 2), len(line.text))
                line.font_size = statistics.median(c["size"] for c in candidates) if candidates else 0
            result["lines"].append([{key: getattr(line, key) for key in ("runs", "images", "font_size", "heading_level", "anchor", "bullet", "toc_entry", "structure_scanned")} for line in lines])
            page.close()
    result["palette"] = sorted(palette)
    result["style_characters"] = dict(result["style_characters"])
    cache.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result


def keyword_names(manual_path: Path) -> set[str]:
    names = set()
    for path in manual_path.glob("keyword_*.html"):
        match = re.search(r"<title>([^<]+)</title>", path.read_text(encoding="utf-8-sig"))
        if match:
            title = html.unescape(match.group(1)).removesuffix(" Keyword").strip()
            names.add(title.lower())
    return names


def command_kind(text: str, previous: str, keywords: set[str]) -> str | None:
    if PROMPT.search(text):
        return "example"
    first = text.split(" ", 1)[0].lower()
    if first not in keywords:
        return None
    # A keyword at the beginning of an explanatory sentence is not an example.
    # Prompt-prefixed examples above bypass this conservative prose filter.
    if text[:1].islower() or re.match(r"^\S+\s+(?:the|you|into|in|and|that|cloning)\b", text):
        return None
    if '"' not in text and "[" not in text and re.search(r"\b(?:knocks|would|will|should|allows|provides)\b", text):
        return None
    if re.search(r"\bsyntax\b.*:\s*$", previous, re.I):
        return "syntax"
    # Inline syntax can wrap onto a keyword such as Camera. Keep it with its
    # introductory list item, and do not promote prose listing alternatives.
    if re.search(r"\bsyntax\b[^:]*:\s*\S", previous, re.I) or re.search(r"\],\s+[A-Za-z]", text):
        return None
    if len(text.split("[", 1)[0].split()) <= 3 and re.search(r"\[[^\]]*_(?:Name|Number|Value)", text):
        return "syntax"
    if re.search(r"(?:type|enter|execute|command)\s*:$", previous, re.I):
        return "example"
    return None


def style_fraction(line: Line, key: str, values: set) -> float:
    """Measure source character styles without counting unstyled whitespace."""
    characters = sum(not char.isspace() for char in line.text)
    matched = sum(sum(not char.isspace() for char in line.text[run["start"]:run["end"]])
                  for run in line.runs if run["style"].get(key) in values)
    return matched / characters if characters else 0


def command_continues(block: Block, previous: Line | None, line: Line) -> bool:
    if previous is None or line.page != previous.page or PROMPT.search(line.text):
        return False
    if line.bullet or line.heading_level or line.text.endswith(":") or re.match(r"^[·•§o]\s+", line.text):
        return False
    # A PDF wrap retains the command's left edge, font size and tight leading.
    # Balanced brackets/quotes do not mean a command ends: /Path and Property
    # arguments, for example, can begin on the next printed line.
    gap = line.top - previous.bottom
    if abs(line.left - previous.left) > 6 or not -2 <= gap <= max(line.height, previous.height) * 0.8:
        return False
    if line.font_size and previous.font_size and abs(line.font_size - previous.font_size) > 1:
        return False
    if line.runs and previous.runs:
        if block.style == "example":
            panel_colors = {"ffffff", "ffff00"}
            return style_fraction(previous, "color", panel_colors) > 0.9 and style_fraction(line, "color", panel_colors) > 0.9
        # Separate consecutive alternatives such as Locate Page / Locate Reset.
        if line.text.split(" ", 1)[0] == block.text.split(" ", 1)[0]:
            return False
        return style_fraction(previous, "bold", {True}) > 0.9 and style_fraction(line, "bold", {True}) > 0.9
    # Before the source styles are loaded, only join unfinished delimiters.
    return block.text.count("[") > block.text.count("]") or len(re.findall(r'(?<!\\)"', block.text)) % 2 == 1


def make_blocks(pages: list[list[Line]], meta: dict, keywords: set[str]) -> tuple[list[Block], dict]:
    blocks = []
    family = meta["family"]
    current_version = family
    patch_section = False
    category = "other"
    section = "Release Notes " + family
    stats = {"removed_header_footer_lines": 0, "toc_pages": [], "command_candidates": 0, "command_continuation_lines": 0}
    for lines in pages:
        page = lines[0].page if lines else len(stats["toc_pages"]) + 1
        clean = []
        for line in lines:
            if is_furniture(line) or line.text in {"o", "·", "§", "•"}:
                stats["removed_header_footer_lines"] += 1
            else:
                clean.append(line)
        if not clean:
            continue
        if any(line.structure_scanned for line in clean):
            # Some sources put the introduction and TOC on the same page. Only
            # confirmed TOC rows are removed; never discard the page's body text.
            if any(line.toc_entry for line in clean):
                stats["toc_pages"].append(page)
            clean = [line for line in clean if not line.toc_entry]
            if not clean:
                continue
        else:
            toc = any("Table of Contents" in line.text for line in clean)
            if page == 1 and sum(len(line.text) > 110 for line in clean) < 3 and len(clean) > 6:
                toc = True
            if toc:
                stats["toc_pages"].append(page)
                blocks.append(Block("\n".join(line.text for line in clean), page, "toc", "contents", "Table of Contents", family))
                continue
        sizes = [line.height for line in clean if len(line.text) > 50]
        body_height = statistics.median(sizes) if sizes else statistics.median(line.height for line in clean)
        buffer = []
        previous_line = None
        previous_text = ""

        def flush() -> None:
            if buffer:
                blocks.append(Block(" ".join(buffer), page, "paragraph", category, section, current_version))
                buffer.clear()

        pending_command = None
        for line in clean:
            text = line.text
            if pending_command is not None:
                if command_continues(pending_command, previous_line, line):
                    pending_command.text += " " + text
                    stats["command_continuation_lines"] += 1
                    previous_line, previous_text = line, pending_command.text
                    continue
                pending_command = None
            new_category = category_of(text)
            new_release = release_heading(text)
            heading = line.heading_level or (len(text) < 85 and len(text.split()) <= 9 and line.height > body_height * 1.35 and not PROMPT.search(text) and text not in {"Hint:", "Important:", "Warning:"})
            if new_category or new_release or heading:
                flush()
                if new_release:
                    current_version = new_release
                    patch_section = bool(re.search(r"Bug\s*Fix", text, re.I))
                    section = text
                elif new_category:
                    category = new_category
                    section = re.sub(r"^\d+\.\s*", "", text)
                    if new_category == "features" and patch_section:
                        current_version = family
                        patch_section = False
                else:
                    section = text
                blocks.append(Block(text, page, "heading", category, section, current_version))
                previous_line, previous_text = line, text
                continue
            kind = command_kind(text, previous_text, keywords)
            if not kind and blocks and blocks[-1].style == "syntax" and previous_text == blocks[-1].text:
                if text.split(" ", 1)[0] == blocks[-1].text.split(" ", 1)[0] and "[" in text:
                    kind = "syntax"
            if kind:
                flush()
                prompt = PROMPT.search(text)
                command = prompt.group(1).strip() if prompt else text
                if command:
                    pending_command = Block(command, page, kind, category, section, current_version)
                    blocks.append(pending_command)
                    stats["command_candidates"] += 1
                previous_line, previous_text = line, text
                continue
            bullet = line.bullet or re.match(r"^[·•§]\s+|^\d+[.)]\s+|^[o]\s+", text)
            gap = line.top - previous_line.bottom if previous_line else 100
            if text == "Description":
                flush()
                blocks.append(Block(text, page, "label", category, section, current_version))
                previous_line, previous_text = line, text
                continue
            table_row = category == "bug-fixes" and gap > 0.5 and previous_text.endswith((".", ":", "!"))
            if buffer and (bullet or gap > body_height * 0.8 or table_row):
                flush()
            buffer.append(text)
            previous_line, previous_text = line, text
        flush()
    prompts = [(line.page, PROMPT.search(line.text)) for page in pages for line in page if PROMPT.search(line.text)]
    stats["prompt_lines_detected"] = len(prompts)
    stats["prompt_lines_not_extracted"] = [
        {"page": page, "text": match.group(1)} for page, match in prompts
        if match is not None and not any(block.page == page and block.style == "example" and block.text.startswith(match.group(1).strip()) for block in blocks)
    ]
    return blocks, stats


def decorate_blocks(blocks: list[Block], pages: list[list[Line]], rich: dict) -> list[Block]:
    """Attach typography without changing exact searchable/canonical text."""
    if sum(len(block.text) for block in blocks if block.page == 1) < 150:
        blocks = [block for block in blocks if block.page != 1]
    blocks = [block for block in blocks if not (block.page == 1 and block.style == "heading" and re.fullmatch(r"Release Notes [12]\.\d+", block.text))]
    page_text = {}
    positions = {}
    for number, lines in enumerate(pages, 1):
        # Poppler occasionally emits only a bullet beside a pictogram as its own
        # text line. Keep the icon with the following same-row description when
        # that bullet glyph is converted to a semantic list marker.
        for line in lines:
            if line.text in {"o", "·", "§", "•"} and line.images:
                candidates = [candidate for candidate in lines if not is_furniture(candidate) and candidate.text not in {"o", "·", "§", "•"}]
                for image in line.images:
                    target = min(candidates, key=lambda candidate: abs((candidate.top + candidate.bottom) / 2 - (image["top"] + image["bottom"]) / 2))
                    target.images.append({**image, "offset": 0})
                line.images.clear()
        text, ranges = "", []
        for line in lines:
            if is_furniture(line) or line.text in {"o", "·", "§", "•"}:
                continue
            if text:
                text += " "
            ranges.append((len(text), len(text) + len(line.text), line))
            text += line.text
        page_text[number], positions[number] = text, ranges
    offsets = defaultdict(int)
    unmatched = []
    current_major_level = 2
    within_release = False
    for block in blocks:
        if block.style == "toc":
            continue
        haystack = page_text[block.page]
        start = haystack.find(block.text, offsets[block.page])
        if start < 0:
            start = haystack.find(block.text)
        if start < 0:
            unmatched.append({"page": block.page, "text": block.text[:100]})
            continue
        end = start + len(block.text)
        offsets[block.page] = end
        lines = [(a, b, line) for a, b, line in positions[block.page] if b > start and a < end]
        if not lines:
            continue
        first = lines[0][2]
        block.top, block.bottom, block.indent = first.top, lines[-1][2].bottom, first.left
        block.bullet = first.bullet
        marker = next((line for line in pages[block.page - 1] if line.text in {"o", "·", "§", "•"}
                       and line.left < first.left and abs((line.top + line.bottom) / 2 - (first.top + first.bottom) / 2) < 5), None)
        if marker is not None:
            block.indent = marker.left
        fragments = []
        for a, b, line in lines:
            left, right = max(0, start - a), min(len(line.text), end - a)
            if block.style == "example" and PROMPT.search(line.text):
                # Retain the visible source prompt separately from executable
                # command text, which remains unchanged in the command index.
                prompt = PROMPT.search(line.text)
                prefix = render_runs(line, 0, prompt.start(1))
                body = render_runs(line, max(left, prompt.start(1)), right)
                fragments.append('<span class="rn-prompt">' + prefix + '</span>' + body)
            else:
                fragments.append(render_runs(line, left, right))
            block.images.extend(line.images)
        block.rich_html = " ".join(fragments)
        block.rich_markdown = block.rich_html
        if block.style == "heading":
            if first.heading_level:
                block.heading_level = first.heading_level
            elif release_heading(block.text):
                block.heading_level = 2
            elif category_of(block.text):
                block.heading_level = 3 if within_release and not re.match(r"\d+\. ", block.text) else 2
            else:
                block.heading_level = min(5, current_major_level + (2 if first.font_size and first.font_size < 12 else 1))
            if category_of(block.text) or release_heading(block.text):
                current_major_level = block.heading_level
            if release_heading(block.text):
                within_release = True
            elif first.heading_level == 2:
                within_release = False
            block.anchor = first.anchor or f"section-{slugify(block.text)}-{block.page}-{round(block.top)}"
    for image in rich["figures"]:
        preceding = [block for block in blocks if (block.page, block.top) <= (image["page"], image["top"]) and block.style != "toc"]
        context = preceding[-1] if preceding else Block("", image["page"], "paragraph", "other", "Release notes", "")
        blocks.append(Block("", image["page"], "figure", context.category, context.section, context.version, top=image["top"], bottom=image["bottom"], images=[image]))
    rich["unmatched_blocks"] = unmatched
    return sorted(blocks, key=lambda block: (block.page, block.top))


def block_list_type(block: Block) -> str | None:
    if block.style not in {"paragraph", "list"}:
        return None
    if re.match(r"^[·•§o]\s+", block.text):
        return "ul"
    if re.match(r"^\d+[.)]\s+", block.text):
        return "ol"
    if block.bullet:
        return "ul"
    if block.category == "bug-fixes":
        return "ul"
    return None


def block_content(block: Block) -> str:
    value = block.rich_html or html.escape(block.text)
    if block_list_type(block):
        # List markers are typographical source glyphs, not command characters.
        value = re.sub(r"^((?:<[^>]+>)*)[·•§o]\s+", r"\1", value)
        value = re.sub(r"^((?:<[^>]+>)*)\d+[.)]\s+", r"\1", value)
    return value


def block_markdown(block: Block) -> str:
    if block.style == "heading":
        return f'<a id="{block.anchor}"></a>\n\n' + "#" * block.heading_level + " " + block.text
    if block.style in {"syntax", "example"}:
        if block.rich_html:
            return block_html(block)
        return "```text\n" + block.text + "\n```"
    if block.style in {"toc", "label"}:
        return ""
    if block.style == "figure":
        return block_html(block)
    if block_list_type(block):
        marker = re.match(r"^\d+[.)]", block.text)
        prefix = marker.group().rstrip(")") + ". " if marker else "- "
        prefix = prefix.replace("..", ".")
        return prefix + block_content(block)
    return block.rich_markdown or block.text


def block_html(block: Block) -> str:
    safe = block_content(block)
    if block.style == "heading":
        return f'<h{block.heading_level} id="{block.anchor}">' + safe + f'</h{block.heading_level}>'
    if block.style in {"syntax", "example"}:
        return '<pre class="command-example"><code>' + safe + "</code></pre>"
    if block.style in {"toc", "label"}:
        return ""
    if block.style == "figure":
        image = block.images[0]
        return (f'<figure class="rn-figure"><img src="/{image["path"]}" alt="{image["alt"]}" '
                f'width="{round(image["width"] * 1.333)}" height="{round(image["height"] * 1.333)}"></figure>')
    return "<p>" + safe + "</p>"


def contents_markup(blocks: list[Block]) -> str:
    headings = [block for block in blocks if block.style == "heading" and block.anchor]
    if len(headings) < 2:
        return ""
    result = ['<nav class="rn-toc"><details><summary>Contents</summary>']
    levels = []
    for block in headings:
        while levels and block.heading_level < levels[-1]:
            result.append("</li></ul>")
            levels.pop()
        if not levels or block.heading_level > levels[-1]:
            result.append("<ul>")
            levels.append(block.heading_level)
        else:
            result.append("</li>")
        result.append(f'<li><a href="#{block.anchor}">{html.escape(block.text)}</a>')
    while levels:
        result.append("</li></ul>")
        levels.pop()
    return "".join(result) + "</details></nav>"


def write_document(root: Path, stem: str, title: str, metadata: dict, blocks: list[Block], pdf: Path, notice: str = NOTICE) -> tuple[str, str]:
    relative = Path("content/releases") / stem
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = "---\n" + "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in metadata.items()) + "\n---\n\n"
    markdown = [frontmatter + "# " + title, "> " + notice, f"[Original PDF]({markdown_source_url(pdf, path.parent)})"]
    markup = ["<h1>" + html.escape(title) + "</h1>", '<aside class="provenance-warning">' + html.escape(notice) + "</aside>", '<p><a href="' + source_url(pdf) + '">Original PDF</a></p>', contents_markup(blocks)]
    headings = [block for block in blocks if block.style == "heading" and block.anchor]
    if len(headings) > 1:
        markdown.append("## Contents\n\n" + "\n".join("  " * (block.heading_level - 2) + f"- [{block.text}](#{block.anchor})" for block in headings))
    last_page = None
    lists = []

    def close_lists() -> None:
        while lists:
            kind, _ = lists.pop()
            markup.append(f"</li></{kind}>")

    for block in blocks:
        if block.style in {"toc", "label"}:
            continue
        if block.page != last_page:
            close_lists()
            markdown.append(f'<!-- Source PDF page {block.page}; see document provenance. -->')
            markup.append(f'<span id="page-{block.page}" data-source-page="{block.page}"></span>')
            last_page = block.page
        next_list = block_list_type(block)
        if next_list:
            while lists and block.indent < lists[-1][1] - 6:
                kind, _ = lists.pop()
                markup.append(f"</li></{kind}>")
            if lists and abs(block.indent - lists[-1][1]) <= 6 and next_list != lists[-1][0]:
                kind, _ = lists.pop()
                markup.append(f"</li></{kind}>")
            if not lists or block.indent > lists[-1][1] + 6:
                ordinal = re.match(r"^(\d+)[.)]\s+", block.text)
                start_attr = f' start="{int(ordinal.group(1))}"' if next_list == "ol" and ordinal else ""
                markup.append(f'<{next_list} class="rn-list"{start_attr}>')
                lists.append((next_list, block.indent))
            else:
                markup.append("</li>")
            ordinal = re.match(r"^(\d+)[.)]\s+", block.text)
            value_attr = f' value="{int(ordinal.group(1))}"' if next_list == "ol" and ordinal else ""
            markup.append(f"<li{value_attr}>" + block_content(block))
            markdown.append("  " * (len(lists) - 1) + block_markdown(block))
        else:
            close_lists()
            markdown.append(block_markdown(block))
            markup.append(block_html(block))
    close_lists()
    md_text = "\n\n".join(markdown) + "\n"
    md_text = re.sub(r'(src=")/assets/', lambda match: match.group(1) + Path(os.path.relpath(root / "assets", path.parent)).as_posix() + "/", md_text)
    path.with_suffix(".md").write_text(md_text, encoding="utf-8")
    path.with_suffix(".html").write_text("\n".join(markup) + "\n", encoding="utf-8")
    return str(relative.with_suffix(".md")), str(relative.with_suffix(".html"))


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records), encoding="utf-8")


def occurrence(doc: dict, block: Block) -> dict:
    return {
        "document_id": doc["id"], "source_path": doc["source_path"],
        "source_url": source_url(Path(doc["source_path"]), block.page),
        "source_page": block.page, "version": block.version,
        "publication_version": doc["version"], "section": block.section,
        "category": block.category, "source_sha256": doc["source_sha256"],
    }


def related_topics(text: str, keywords: set[str]) -> list[dict]:
    # Lexical links are explicitly not semantic equivalence or conflict resolution.
    matches = []
    for word in set(re.findall(r"\b[A-Za-z][A-Za-z0-9_+]*\b", text)):
        if word.lower() in keywords and len(word) > 3:
            matches.append({"term": word, "basis": "keyword mention; relevance requires review"})
    return sorted(matches, key=lambda item: item["term"].lower())[:30]


def near_duplicate_candidates(entries: list[dict]) -> list[dict]:
    """Flag possible revisions without merging or claiming they conflict."""
    buckets = defaultdict(list)
    for entry in entries:
        tokens = re.findall(r"\w+", entry["text"].lower())
        if len(tokens) >= 12:
            buckets[tuple(tokens[:5])].append(entry)
    candidates = []
    seen = set()
    for bucket in buckets.values():
        for pos, left in enumerate(bucket):
            for right in bucket[pos + 1:]:
                pair = tuple(sorted((left["id"], right["id"])))
                if pair in seen:
                    continue
                seen.add(pair)
                a, b = canonical_text(left["text"]), canonical_text(right["text"])
                if min(len(a), len(b)) / max(len(a), len(b)) < 0.75:
                    continue
                ratio = SequenceMatcher(None, a, b, autojunk=False).ratio()
                if ratio >= 0.90:
                    candidates.append({"left_id": left["id"], "right_id": right["id"], "similarity": round(ratio, 4), "status": "possible_revision_not_merged", "review_required": True})
    return sorted(candidates, key=lambda row: (-row["similarity"], row["left_id"], row["right_id"]))


def ingest(root: Path, source: Path, manual: Path) -> dict:
    output = root / "data/releases"
    output.mkdir(parents=True, exist_ok=True)
    keywords = keyword_names(manual)
    documents = []
    source_docs = []
    canon = {}
    command_canon = {}
    manifests = []
    palette = set()
    all_occurrences = 0
    source_pdfs = sorted(source.glob("*.pdf"), key=lambda p: p.name.lower())
    if not source_pdfs:
        raise ValueError(f"No PDFs found in {source}")
    for pdf in source_pdfs:
        pages, meta = parse_pdf(pdf, root / ".cache/releases")
        blocks, stats = make_blocks(pages, meta, keywords)
        rich = enrich_pdf(pdf, pages, meta, stats, root)
        blocks, stats = make_blocks(pages, meta, keywords)
        blocks = decorate_blocks(blocks, pages, rich)
        palette.update(tuple(item) for item in rich["palette"])
        slug = slugify(pdf.stem)
        doc_id = "release/" + slug
        title = f"Release notes {meta['publication_version']}"
        metadata = {"id": doc_id, "authority": "release-note", "version": meta["publication_version"], "source": pdf.name, "source_sha256": meta["sha256"], "publication_date_text": meta["publication_date_text"]}
        md_path, html_path = write_document(root, slug, title, metadata, blocks, pdf)
        doc = {
            "id": doc_id, "title": title, "kind": "release_note", "category": "release-document",
            "version": meta["publication_version"], "authority": "release-note", "source_path": str(pdf.resolve()),
            "source_url": source_url(pdf), "source_sha256": meta["sha256"], "markdown_path": md_path,
            "html_path": html_path, "text": "\n\n".join(block.text for block in blocks),
            "breadcrumbs": ["Release Notes", meta["family"]], "parent_id": "release/index", "aliases": [],
            "searchable": False, "family": meta["family"], "publication_date_text": meta["publication_date_text"],
            "pages": meta["pages"], "status": "source_document_not_current_guidance",
            "extraction": "Poppler positioned text, source PDF font/color/vector styles, decoded image placements, resolved contents/bookmark destinations and heading typography",
            "toc": [{"title": block.text, "level": block.heading_level, "anchor": block.anchor, "source_page": block.page} for block in blocks if block.style == "heading"],
            "source_structure": rich["structure"],
        }
        source_docs.append(doc)
        documents.append(doc)
        entry_count = 0
        for block in blocks:
            if block.style in {"heading", "toc", "label"} or (len(block.text) < 30 and block.style not in {"syntax", "example"}):
                continue
            normalized = canonical_text(block.text)
            key = digest(block.style + "\n" + normalized)
            if key not in canon:
                canon[key] = {"block": block, "text": block.text, "occurrences": [], "id": "release-entry/" + key[:20], "representative_version": meta["publication_version"]}
            elif version_key(meta["publication_version"]) >= version_key(canon[key]["representative_version"]):
                canon[key]["block"] = block
                canon[key]["representative_version"] = meta["publication_version"]
            canon[key]["occurrences"].append(occurrence(doc, block))
            all_occurrences += 1
            entry_count += 1
            if block.style in {"syntax", "example"}:
                command_key = digest(block.style + "\n" + block.text)
                if command_key not in command_canon:
                    command_canon[command_key] = {
                        "id": "release-command/" + command_key[:20], "document_id": canon[key]["id"],
                        "command": block.text.split(" ", 1)[0], "text": block.text, "type": block.style,
                        "version": block.version, "authority": "release-note", "source_path": str(pdf.resolve()),
                        "source_url": source_url(pdf, block.page), "source_page": block.page,
                        "status": "historical_pdf_extraction_unverified", "provenance": [],
                    }
                command_canon[command_key]["provenance"].append(occurrence(doc, block))
        manifests.append({"file": pdf.name, "source_path": str(pdf.resolve()), **meta, **stats, "entries": entry_count,
                          "empty_pages": [i + 1 for i, page in enumerate(pages) if not page],
                          "characters": len(doc["text"]), "document_id": doc_id,
                          "source_structure": rich["structure"], "image_placements": len(rich["images"]),
                          "images": rich["images"], "style_characters": rich["style_characters"],
                          "style_alignment_warnings": rich["alignment"], "unmatched_style_blocks": rich["unmatched_blocks"]})
        print(f"{pdf.name}: {meta['pages']} pages, release {meta['publication_version']}, {entry_count} entries", flush=True)

    entries = []
    for key, entry in sorted(canon.items()):
        occurrences = sorted(entry["occurrences"], key=lambda item: (version_key(item["publication_version"]), item["document_id"], item["source_page"]))
        representative = occurrences[-1]
        original = entry["block"]
        block = replace(original, page=representative["source_page"], category=representative["category"], section=representative["section"], version=representative["version"])
        name = block.section + ": " + re.sub(r"^[·•§]\s+", "", entry["text"]).split(". ", 1)[0]
        title = name[:137] + ("…" if len(name) > 137 else "")
        versions = sorted({item["version"] for item in occurrences}, key=version_key)
        publication_versions = sorted({item["publication_version"] for item in occurrences}, key=version_key)
        md_path, html_path = write_document(root, "entries/" + key[:20], title,
                                           {"id": entry["id"], "authority": "release-note", "versions": versions},
                                           [block], Path(representative["source_path"]))
        occurrence_md = [f'\n<details><summary>Source occurrences ({len(occurrences)})</summary>\n']
        occurrence_html = [f'<details class="rn-provenance"><summary>Source occurrences ({len(occurrences)})</summary><ul>']
        for item in occurrences:
            label = f"{Path(item['source_path']).name}, page {item['source_page']} — section version {item['version']}"
            occurrence_md.append(f"- [{label}]({markdown_source_url(Path(item['source_path']), (root / md_path).parent, item['source_page'])})")
            occurrence_html.append('<li><a href="' + item["source_url"] + '">' + html.escape(label) + "</a></li>")
        occurrence_md.append("\n</details>")
        occurrence_html.append("</ul></details>")
        with (root / md_path).open("a", encoding="utf-8") as handle:
            handle.write("\n".join(occurrence_md) + "\n")
        with (root / html_path).open("a", encoding="utf-8") as handle:
            handle.write("\n".join(occurrence_html) + "\n")
        record = {
            "id": entry["id"], "title": title, "kind": "release_note", "category": block.category,
            "version": representative["version"], "versions": versions, "publication_versions": publication_versions,
            "authority": "release-note", "source_path": representative["source_path"], "source_url": representative["source_url"],
            "source_sha256": representative["source_sha256"], "source_page": representative["source_page"],
            "markdown_path": md_path, "html_path": html_path, "text": entry["text"],
            "breadcrumbs": ["Release Notes", block.category, block.section], "parent_id": representative["document_id"],
            "aliases": [], "searchable": True, "occurrences": occurrences, "occurrence_count": len(occurrences),
            "status": "release_note_supplement_requires_current_manual_check",
            "deduplication": "identical full extracted paragraph/example after whitespace normalization; all versions retained",
            "related_keywords": related_topics(entry["text"], keywords),
        }
        entries.append(record)
    documents.extend(entries)
    near = near_duplicate_candidates(entries)
    index_title = "Release Notes: source archive and deduplicated history"
    overview = [Block(index_title, 1, "heading", "index", "Release Notes", "2.5")]
    overview.extend(Block(f"{doc['title']} — {doc['pages']} pages — {Path(doc['source_path']).name}", 1, "paragraph", "index", "Release Notes", doc["version"]) for doc in sorted(source_docs, key=lambda item: version_key(item["version"]), reverse=True))
    index_md = "content/releases/index.md"
    index_html = "content/releases/index.html"
    counts = Counter(entry["category"] for entry in entries)
    summary = f"{len(source_docs)} PDF sources; {sum(item['pages'] for item in manifests)} pages; {all_occurrences} extracted entry occurrences; {len(entries)} unique entries; {all_occurrences - len(entries)} repeated occurrences consolidated. {len(near)} possible revisions retained separately for review."
    md_lines = ["# " + index_title, "> " + NOTICE, summary, "## Categories"]
    html_lines = ["<h1>" + index_title + "</h1>", '<aside class="provenance-warning">' + NOTICE + "</aside>", "<p>" + summary + "</p>", "<h2>Categories</h2><ul>"]
    for name, count in sorted(counts.items()):
        md_lines.append(f"- {name}: {count} canonical entries")
        html_lines.append(f"<li>{name}: {count} canonical entries</li>")
    html_lines.append("</ul><h2>Full source documents</h2><ul>")
    md_lines.append("## Full source documents")
    for doc in sorted(source_docs, key=lambda item: (version_key(item["version"]), item["publication_date_text"] or ""), reverse=True):
        label = doc["title"] + " — " + Path(doc["source_path"]).name
        md_lines.append(f"- [{label}]({Path(doc['markdown_path']).name})")
        html_lines.append('<li><a href="/?doc=' + quote(doc["id"], safe="") + '">' + html.escape(label) + "</a></li>")
    html_lines.append("</ul>")
    (root / index_md).write_text("\n\n".join(md_lines) + "\n", encoding="utf-8")
    (root / index_html).write_text("\n".join(html_lines) + "\n", encoding="utf-8")
    documents.append({"id": "release/index", "title": index_title, "kind": "release_note", "category": "index", "version": "2.5", "authority": "editorial", "source_path": str(source.resolve()), "source_url": "", "source_sha256": "", "markdown_path": index_md, "html_path": index_html, "text": summary, "breadcrumbs": ["Release Notes"], "parent_id": None, "aliases": [], "searchable": False})
    commands = [command_canon[key] for key in sorted(command_canon)]
    for command in commands:
        command["versions"] = sorted({item["version"] for item in command["provenance"]}, key=version_key)
    write_jsonl(output / "documents.jsonl", documents)
    write_jsonl(output / "commands.jsonl", commands)
    write_jsonl(output / "possible-revisions.jsonl", near)
    css = ["/* Numeric colors extracted from MA source PDF character spans and rectangles. */"]
    for kind, color in sorted(palette):
        css.append(f'.rn-{kind}-{color} {{ {"background-color" if kind == "bg" else "color"}: #{color}; }}')
    (root / "assets/releases/source-styles.css").write_text("\n".join(css) + "\n", encoding="utf-8")
    navigation = {"id": "release/index", "title": "Release Notes", "children": [{"id": doc["id"], "title": doc["title"] + " · " + (doc["publication_date_text"] or "undated"), "children": []} for doc in sorted(source_docs, key=lambda item: version_key(item["version"]), reverse=True)]}
    (output / "navigation.json").write_text(json.dumps(navigation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = {
        "source_files": len(source_docs), "source_pages": sum(item["pages"] for item in manifests),
        "document_records": len(documents), "canonical_entries": len(entries), "entry_occurrences": all_occurrences,
        "duplicate_occurrences_consolidated": all_occurrences - len(entries), "unique_command_examples": len(commands),
        "command_occurrences": sum(len(command["provenance"]) for command in commands), "possible_revision_pairs": len(near),
        "command_continuation_lines": sum(item["command_continuation_lines"] for item in manifests),
        "prompt_lines_detected": sum(item["prompt_lines_detected"] for item in manifests),
        "prompt_lines_not_extracted": sum(len(item["prompt_lines_not_extracted"]) for item in manifests),
        "image_placements": sum(item["image_placements"] for item in manifests),
        "unique_images": len({image["sha256"] for item in manifests for image in item["images"]}),
        "source_style_characters": dict(sum((Counter(item["style_characters"]) for item in manifests), Counter())),
        "style_alignment_warnings": sum(len(item["style_alignment_warnings"]) for item in manifests),
        "unmatched_style_blocks": sum(len(item["unmatched_style_blocks"]) for item in manifests),
        "categories": dict(sorted(counts.items())), "sources": manifests, "missing_links": [],
        "warnings": ["Release notes never supersede the current manual automatically.", "Whitespace-normalized exact deduplication does not merge semantic near-duplicates; possible revisions are separately flagged.", "PDF reading order and some paragraph/heading boundaries remain heuristic; actual source TOC/bookmark destinations take precedence when present. Source typography and body image placements are retained; recurring page furniture is omitted.", "Extracted command examples are unexecuted historical evidence; review their PDF and current manual before use.", "PDF publication/revision dates do not necessarily identify original software release dates.", "Lexical keyword links are inferred relevance, not verified feature equivalence or conflict adjudication."],
        "coverage": {"all_input_pdfs_accounted_for": len(source_docs) == len(source_pdfs), "all_pages_have_extracted_text": not any(item["empty_pages"] for item in manifests), "source_pdfs_unchanged": all(hashlib.sha256(Path(item["source_path"]).read_bytes()).hexdigest() == item["sha256"] for item in manifests), "all_retained_image_placements_rendered": all((root / doc["html_path"]).read_text(encoding="utf-8").count("<img ") == manifest["image_placements"] for doc, manifest in zip(source_docs, manifests))},
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # Only remove previously generated, content-addressed entry fragments. Source
    # PDFs, user notes and non-generated files are outside this cleanup boundary.
    expected = {root / doc[field] for doc in documents for field in ("markdown_path", "html_path")}
    for path in (root / "content/releases/entries").iterdir():
        if re.fullmatch(r"[0-9a-f]{20}\.(?:md|html)", path.name) and path not in expected:
            path.unlink()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--manual", type=Path, default=Path.home() / "MALightingTechnology/gma3_2.5.1/shared/language/HTML")
    args = parser.parse_args()
    root = args.root.resolve()
    report = ingest(root, (args.source or root / "release-notes-pdf").resolve(), args.manual)
    print(json.dumps({key: value for key, value in report.items() if key not in {"sources", "warnings"}}, indent=2))


if __name__ == "__main__":
    main()
