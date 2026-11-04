"""Strict HTML boundary for vendor documents and extracted PDF fragments."""

from html import escape
from html.parser import HTMLParser
from urllib.parse import urlsplit

ALLOWED_TAGS = {"a", "abbr", "aside", "b", "blockquote", "br", "caption", "code", "col", "colgroup", "dd", "del", "details", "div", "dl", "dt", "em", "figcaption", "figure", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "kbd", "li", "main", "mark", "nav", "ol", "p", "pre", "s", "samp", "section", "small", "span", "strong", "sub", "summary", "sup", "table", "tbody", "td", "th", "thead", "tr", "u", "ul", "var"}
VOID_TAGS = {"br", "col", "hr", "img"}
BLOCKED_TAGS = {"script", "style", "iframe", "object", "embed", "form", "textarea", "button", "input", "svg", "math", "template"}


def safe_url(value: str, image: bool = False) -> bool:
    if any(ord(char) < 32 for char in value) or "\\" in value:
        return False
    parsed = urlsplit(value.strip())
    if image:
        return not parsed.scheme and not parsed.netloc and parsed.path.startswith("/assets/")
    return parsed.scheme in {"http", "https", "mailto"} or (not parsed.scheme and not parsed.netloc and (value.startswith(("/", "#"))))


class Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.blocked: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in BLOCKED_TAGS:
            if tag not in {"input", "embed"}:
                self.blocked.append(tag)
            return
        if self.blocked or tag not in ALLOWED_TAGS:
            return
        clean = []
        for key, value in attrs:
            if value is None:
                continue
            if key in {"id", "title", "alt", "colspan", "rowspan", "scope", "open"}:
                clean.append((key, value))
            elif key == "class":
                # Class names carry presentation only; no CSS or event handlers are admitted.
                clean.append((key, value))
            elif key in {"width", "height", "data-source-page"} and value.isdecimal() and 0 < int(value) <= 9999:
                clean.append((key, value))
            elif tag == "img" and key in {"data-source-max-width", "data-source-max-height"} and value.isdecimal() and 0 < int(value) <= 9999:
                clean.append((key, value))
            elif ((tag == "ol" and key == "start") or (tag == "li" and key == "value")) and value.isdecimal() and 0 < int(value) <= 9999:
                clean.append((key, value))
            elif tag == "a" and key == "href" and safe_url(value):
                clean.append((key, value))
            elif tag == "img" and key == "src" and safe_url(value, image=True):
                clean.append((key, value))
        if tag == "a":
            clean.append(("rel", "noopener noreferrer"))
        if tag == "img":
            clean.extend([("loading", "lazy"), ("decoding", "async")])
        attributes = "".join(f' {key}="{escape(value, quote=True)}"' for key, value in clean)
        self.parts.append(f"<{tag}{attributes}>")

    def handle_endtag(self, tag: str) -> None:
        if self.blocked:
            if tag == self.blocked[-1]:
                self.blocked.pop()
            return
        if tag in ALLOWED_TAGS and tag not in VOID_TAGS:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self.blocked:
            self.parts.append(escape(data))


def sanitize_html(value: str) -> str:
    parser = Sanitizer()
    parser.feed(value)
    parser.close()
    return "".join(parser.parts)
