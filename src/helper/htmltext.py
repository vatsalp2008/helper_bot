"""Job description HTML to plain text that keeps the structure keywords.py relies on (headings and list items)."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

_BLOCKS = {"p", "div", "section", "article", "header", "footer", "ul", "ol", "table", "tr", "blockquote",
           "h1", "h2", "h3", "h4", "h5", "h6"}


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag == "br":
            self.parts.append("\n")
        elif tag in _BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCKS or tag == "li":
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    """Also accepts HTML that was escaped once more (&lt;p&gt;), as the Greenhouse API returns it."""
    if "&lt;" in markup and "<" not in markup:
        markup = html.unescape(markup)
    parser = _TextParser()
    parser.feed(markup)
    parser.close()
    text = "".join(parser.parts).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    out = re.sub(r"(?m)^- *\n(?=\S)", "- ", out)
    return re.sub(r"(?m)^(- .*)\n\n(?=- )", r"\1\n", re.sub(r"(?m)^(- .*)\n\n(?=- )", r"\1\n", out))
