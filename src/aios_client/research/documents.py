"""Bounded, optional-format document extraction with stable section locations."""

from __future__ import annotations

import hashlib
import io
import logging
import threading
import zipfile
from html.parser import HTMLParser

MAX_DOCUMENT = 6 * 1024 * 1024
MAX_PAGES = 80
MAX_EXTRACTED_CHARS = 250_000
_PDF_PARSE_LOCK = threading.Lock()


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


def extract_document(data: bytes, kind: str, *, max_bytes: int = MAX_DOCUMENT):
    """Return (sections, limitations), never execute embedded content."""
    if max_bytes > 24 * 1024 * 1024 or max_bytes < 1024 * 1024:
        return [], ["document_limit_invalid"]
    if len(data) > max_bytes:
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
        # pypdf may print full font dictionaries and malformed-object diagnostics
        # for every page. These warnings are not evidence that extraction failed.
        # Keep extraction outcomes explicit while limiting console noise.
        with _PDF_PARSE_LOCK:
            logger = logging.getLogger("pypdf")
            old_level = logger.level
            logger.setLevel(logging.ERROR)
            try:
                reader = PdfReader(io.BytesIO(data), strict=False)
                sections = []
                remaining = MAX_EXTRACTED_CHARS
                limitations = ["page_limit"] if len(reader.pages) > MAX_PAGES else []
                for i, page in enumerate(reader.pages[:MAX_PAGES], 1):
                    if remaining <= 0:
                        limitations.append("text_limit")
                        break
                    text = (page.extract_text() or "")[:min(8000, remaining)]
                    if text.strip():
                        sections.append((f"page:{i}", text))
                        remaining -= len(text)
                if not sections:
                    limitations.append("no_extractable_text")
                return sections, limitations
            except Exception:
                return [], ["pdf_extraction_failed"]
            finally:
                logger.setLevel(old_level)
    if kind == ".epub":
        try:
            result = []
            remaining = MAX_EXTRACTED_CHARS
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                if len(archive.infolist()) > 2000:
                    return [], ["epub_entry_limit"]
                for item in archive.infolist():
                    if len(result) >= MAX_PAGES or remaining <= 0:
                        break
                    if (item.filename.lower().endswith((".xhtml", ".html", ".htm"))
                        and not item.is_dir() and item.file_size <= 256_000):
                        content = archive.read(item)
                        if len(content) > 256_000:
                            continue
                        extracted = html_text(content.decode("utf-8", "replace"))
                        if extracted:
                            excerpt = extracted[:min(8000, remaining)]
                            result.append((item.filename[:240], excerpt))
                            remaining -= len(excerpt)
                limits = []
                if len(result) >= MAX_PAGES:
                    limits.append("section_limit")
                if remaining <= 0:
                    limits.append("text_limit")
                return result, limits
        except (OSError, ValueError, zipfile.BadZipFile, RuntimeError):
            return [], ["epub_extraction_failed"]
    return [], ["unsupported_format"]


def digest(data: bytes):
    return hashlib.sha256(data).hexdigest()
