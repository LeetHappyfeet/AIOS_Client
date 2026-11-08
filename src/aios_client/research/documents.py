"""Bounded, optional-format document extraction with stable section locations."""

from __future__ import annotations

import hashlib
import io
import zipfile
from html.parser import HTMLParser

MAX_DOCUMENT = 6 * 1024 * 1024
MAX_PAGES = 80
MAX_EXTRACTED_CHARS = 250_000


class TextOnly(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "nav", "svg", "noscript"}:
            self.skip += 1
        elif tag in {"p", "div", "section", "h1", "h2", "h3", "li", "br"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "nav", "svg", "noscript"}:
            self.skip = max(0, self.skip - 1)
        elif tag in {"p", "div", "section", "li"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip and len(self.parts) < 20000:
            self.parts.append(data)

    def text(self):
        return " ".join(" ".join(self.parts)[:MAX_EXTRACTED_CHARS].split())


def html_text(data: str):
    parser = TextOnly()
    parser.feed(data[:MAX_EXTRACTED_CHARS])
    return parser.text()


def extract_document(data: bytes, kind: str):
    """Return (sections, limitations), never execute embedded content."""
    if len(data) > MAX_DOCUMENT:
        return [], ["document_too_large"]
    if kind in {".txt", ".md", ".rst", ".json", ".csv"}:
        try:
            return [("text", data.decode("utf-8")[:MAX_EXTRACTED_CHARS])], []
        except UnicodeError:
            return [], ["not_utf8"]
    if kind in {".html", ".htm"}:
        return [("html", html_text(data.decode("utf-8", "replace")))], []
    if kind == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            return [], ["pypdf_not_installed"]
        try:
            reader = PdfReader(io.BytesIO(data), strict=False)
            sections = []
            for i, page in enumerate(reader.pages[:MAX_PAGES], 1):
                text = (page.extract_text() or "")[:8000]
                if text.strip():
                    sections.append((f"page:{i}", text))
            limitations = ["page_limit"] if len(reader.pages) > MAX_PAGES else []
            if not sections:
                limitations.append("no_extractable_text")
            return sections, limitations
        except Exception:
            return [], ["pdf_extraction_failed"]
    if kind == ".epub":
        try:
            result = []
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                if len(archive.infolist()) > 2000:
                    return [], ["epub_entry_limit"]
                for item in archive.infolist():
                    if len(result) >= MAX_PAGES:
                        break
                    if (item.filename.lower().endswith((".xhtml", ".html", ".htm"))
                        and not item.is_dir() and item.file_size <= 256_000):
                        content = archive.read(item)
                        if len(content) > 256_000:
                            continue
                        extracted = html_text(content.decode("utf-8", "replace"))
                        if extracted:
                            result.append((item.filename[:240], extracted[:8000]))
                return result, (["section_limit"] if len(result) >= MAX_PAGES else [])
        except (OSError, ValueError, zipfile.BadZipFile, RuntimeError):
            return [], ["epub_extraction_failed"]
    return [], ["unsupported_format"]


def digest(data: bytes):
    return hashlib.sha256(data).hexdigest()
