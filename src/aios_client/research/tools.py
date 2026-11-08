"""Bounded source-aware search. Search result URLs are evidence, not fetch authority."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Annotated

import httpx
from pydantic import Field


class ResearchTools:
    def __init__(self, workspace, sources, transport=None):
        self.workspace = workspace
        self.sources = {source.id: source for source in sources}
        self.transport = transport
        self.evidence = {}

    def research_sources(self):
        return {"sources": [
            {"id": s.id, "adapter": s.adapter, "priority": s.priority,
             "topics": s.topics, "capabilities": ["search", "read_excerpt"]}
            for s in self.sources.values()
        ]}

    def _store(self, source, kind, title, excerpt, *, url=None, location=None):
        excerpt = self.workspace.redact(excerpt[:1500])
        title = self.workspace.redact(title[:200])
        identity = json.dumps([source.id, kind, url, location, excerpt],
                              ensure_ascii=True, sort_keys=True)
        evidence_id = hashlib.sha256(identity.encode()).hexdigest()[:24]
        item = {"evidence_id": evidence_id, "source_id": source.id,
                "kind": kind, "title": title, "excerpt": excerpt,
                "content_scope": "search_excerpt_only"}
        if url:
            item["url"] = url
        if location:
            item["location"] = location
        self.evidence[evidence_id] = item
        return item

    def _searxng(self, source, query, limit):
        # Only the configured endpoint can be contacted; never follow result URLs,
        # environment proxies, or redirects. The operator explicitly grants LAN
        # endpoints; research output cannot nominate a new HTTP destination.
        url = source.endpoint.rstrip("/") + "/search"
        data = bytearray()
        try:
            with httpx.Client(transport=self.transport, trust_env=False,
                              follow_redirects=False, timeout=10) as client:
                with client.stream("GET", url, params={"q": query, "format": "json"},
                                   headers={"Accept": "application/json",
                                            "Accept-Encoding": "identity"}) as response:
                    if response.status_code != 200:
                        return {"error": "http_status", "status_code": response.status_code}
                    if "json" not in response.headers.get("content-type", "").lower():
                        return {"error": "not_json"}
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        return {"error": "encoded_response"}
                    # Buffered mock responses may have consumed the stream already.
                    chunks = (response.content,) if response.is_stream_consumed else response.iter_raw()
                    for chunk in chunks:
                        self.workspace.guard()
                        if len(data) + len(chunk) > 131072:
                            return {"error": "response_too_large"}
                        data.extend(chunk)
        except (httpx.HTTPError, TimeoutError) as exc:
            return {"error": type(exc).__name__}
        try:
            payload = json.loads(data)
        except (ValueError, UnicodeError):
            return {"error": "invalid_json"}
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            return {"error": "invalid_results"}
        items = []
        for raw in payload["results"][:max(20, limit * 4)]:
            if not isinstance(raw, dict):
                continue
            title, excerpt, url = raw.get("title"), raw.get("content"), raw.get("url")
            if not isinstance(title, str) or not isinstance(excerpt, str) or not isinstance(url, str):
                continue
            # Links are citations only, never followed by this adapter.
            try:
                p = httpx.URL(url)
                if p.scheme not in ("http", "https") or not p.host or p.userinfo:
                    continue
            except (ValueError, httpx.InvalidURL):
                continue
            items.append(self._store(source, "web_search_result", title, excerpt, url=url[:1500]))
            if len(items) >= limit:
                break
        return {"results": items}

    def _local_text(self, source, query, limit):
        root = Path(source.root)
        if root.is_symlink() or not root.is_dir():
            return {"error": "local_root_unavailable"}
        root = root.resolve(strict=True)
        needles = query.casefold().split()
        if not needles:
            return {"results": []}
        results, visited = [], 0
        allowed = {".txt", ".md", ".rst", ".csv", ".json"}
        for base, dirs, files in os.walk(root, followlinks=False):
            self.workspace.guard()
            dirs[:] = sorted(d for d in dirs if not (Path(base) / d).is_symlink())
            for name in sorted(files):
                path = Path(base) / name
                if path.suffix.lower() not in allowed or path.is_symlink():
                    continue
                visited += 1
                if visited > 1000:
                    return {"results": results, "scan_truncated": True}
                try:
                    if not path.resolve().is_relative_to(root) or path.stat().st_size > 1048576:
                        continue
                    with path.open("rb") as handle:
                        data = handle.read(1048577)
                    if len(data) > 1048576:
                        continue
                    content = data.decode("utf-8")
                except (OSError, UnicodeError):
                    continue
                lower = content.casefold()
                if all(needle in lower for needle in needles):
                    start = min(lower.find(needle) for needle in needles)
                    excerpt = content[max(0, start - 200):start + 900]
                    rel = path.relative_to(root).as_posix()
                    results.append(self._store(source, "local_text", name, excerpt,
                                               location=rel))
                    if len(results) >= limit:
                        return {"results": results}
        return {"results": results}

    def research_search(
        self,
        query: Annotated[str, Field(min_length=2, max_length=400)],
        source_id: Annotated[str | None, Field(max_length=64)] = None,
        limit: Annotated[int, Field(ge=1, le=10)] = 5,
    ):
        self.workspace.guard()
        if not self.sources:
            return {"status": "unavailable", "results": [], "reason": "no_authorized_sources"}
        if source_id is not None and source_id not in self.sources:
            raise ValueError("Research source is not authorized")
        options = ([self.sources[source_id]] if source_id else
                   sorted(self.sources.values(), key=lambda s: (s.priority, s.id)))
        attempted, errors = [], {}
        for source in options[:2] if source_id is None else options:
            self.workspace.guard()
            attempted.append(source.id)
            result = (self._searxng(source, query, limit) if source.adapter == "searxng"
                      else self._local_text(source, query, limit))
            if result.get("results"):
                return {"status": "ok", "source_id": source.id,
                        "attempted_sources": attempted, **result}
            if result.get("error"):
                errors[source.id] = result["error"]
        return {"status": "no_results", "results": [],
                "attempted_sources": attempted, "errors": errors}

    def research_read(
        self,
        evidence_id: Annotated[str, Field(pattern=r"^[a-f0-9]{24}$")],
    ):
        self.workspace.guard()
        if evidence_id not in self.evidence:
            raise ValueError("Unknown research evidence ID for this run")
        return {"evidence": self.evidence[evidence_id].copy(),
                "full_document_retrieved": False,
                "note": "This is a bounded discovery excerpt, not verified document content."}


def register(registry, tools):
    if not tools.sources:
        return
    registry.add("research_sources", tools.research_sources,
                 "List only authorized research sources and capabilities",
                 "research.read", effect="read")
    registry.add("research_search", tools.research_search,
                 "Search approved sources. Result URLs are citations, NOT permission to fetch URLs.",
                 "research.read", effect="read")
    registry.add("research_read", tools.research_read,
                 "Revisit a retained search excerpt by evidence ID; not a full-page reader",
                 "research.read", effect="read")
