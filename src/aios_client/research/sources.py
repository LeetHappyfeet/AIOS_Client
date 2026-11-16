"""Operator-owned source registry; agent grants never define arbitrary endpoints."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..config import base_url


SOURCE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class ResearchSource(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    adapter: Literal["searxng", "local_text", "local_documents", "calibre_opds"]
    enabled: bool = True
    priority: int = Field(default=100, ge=0, le=1000)
    topics: list[str] = Field(default_factory=list, max_length=20)
    endpoint: str | None = None
    root: str | None = None
    read_origins: list[str] = Field(default_factory=list, max_length=20)
    indexed: bool = False

    @model_validator(mode="after")
    def validate_source(self):
        if not SOURCE_ID.fullmatch(self.id):
            raise ValueError("Invalid research source ID")
        if any(not isinstance(t, str) or not SOURCE_ID.fullmatch(t) for t in self.topics):
            raise ValueError("Invalid research topic")
        from ..tools.network import canonical_origin
        for origin in self.read_origins:
            canonical_origin(origin, origin_only=True)
        if self.indexed and self.adapter != "local_documents":
            raise ValueError("Only local_documents supports indexing")
        if self.adapter != "searxng" and self.read_origins:
            raise ValueError("Only SearXNG sources may grant result-page reading")
        if self.adapter in ("searxng", "calibre_opds"):
            if self.root is not None or not self.endpoint:
                raise ValueError("Network source requires endpoint and forbids root")
            normalized = base_url(self.endpoint)
            if len(normalized) > 400 or "\\" in normalized:
                raise ValueError("Invalid research source endpoint")
            self.endpoint = normalized
        else:
            if self.endpoint is not None or not self.root:
                raise ValueError("Local source requires root and forbids endpoint")
            path = Path(self.root).expanduser()
            if not path.is_absolute() or ".." in path.parts or len(str(path)) > 1000:
                raise ValueError("Local research root must be an absolute path")
            self.root = str(path)
        if self.adapter == "calibre_opds" and self.endpoint and not self.endpoint.rstrip("/").endswith("/opds"):
            raise ValueError("Calibre OPDS endpoint must end in /opds")
        return self


class SourceFile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1]
    sources: list[ResearchSource] = Field(max_length=100)


def load_sources(path: Path, grants=()):
    """Never auto-discover search engines or grant all sources by default."""
    grants = tuple(grants)
    if not grants:
        return ()
    for grant in grants:
        if not isinstance(grant, str) or not SOURCE_ID.fullmatch(grant):
            raise ValueError("Invalid research source grant")
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("Research source registry missing or is a symlink")
    if path.stat().st_size > 65536:
        raise ValueError("Research source registry exceeds size limit")
    config = SourceFile.model_validate(json.loads(path.read_text(encoding="utf-8")))
    ids = [source.id for source in config.sources]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate research source IDs")
    by_id = {source.id: source for source in config.sources}
    if any(grant not in by_id or not by_id[grant].enabled for grant in grants):
        raise ValueError("Agent grants an unknown or disabled research source")
    return tuple(sorted((by_id[g] for g in set(grants)), key=lambda s: (s.priority, s.id)))
