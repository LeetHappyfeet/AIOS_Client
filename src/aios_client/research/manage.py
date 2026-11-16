"""Operator-managed research source configuration and bounded connectivity tests.

The JSON file remains the canonical source of truth for UI and headless installs.
No scanning or discovery of network endpoints is performed.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from xml.etree import ElementTree

import httpx
from .sources import ResearchSource, SourceFile


_lock = threading.RLock()
MAX_REGISTRY_BYTES = 65536
MAX_TEST_BYTES = 65536


def _read(path: Path) -> SourceFile:
    if path.is_symlink():
        raise ValueError("Research source registry cannot be a symlink")
    if not path.exists():
        return SourceFile(version=1, sources=[])
    if not path.is_file() or path.stat().st_size > MAX_REGISTRY_BYTES:
        raise ValueError("Research source registry must be a small regular JSON file")
    value = SourceFile.model_validate_json(path.read_bytes())
    ids = [entry.id for entry in value.sources]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate research source IDs")
    return value


def registry_snapshot(path: Path) -> dict:
    with _lock:
        registry = _read(Path(path))
        return {
            "version": registry.version,
            "sources": [item.model_dump(mode="json") for item in registry.sources],
        }


def _validated_file(payload: dict) -> SourceFile:
    registry = SourceFile.model_validate(payload)
    ids = [entry.id for entry in registry.sources]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate research source IDs")
    return registry


def write_registry(path: Path, data: dict) -> dict:
    """Atomic replacement; permissions 0600; never follow a registry symlink."""
    path = Path(path)
    registry = _validated_file(data)
    body = (json.dumps(registry.model_dump(mode="json"), indent=2) + "\n").encode()
    if len(body) > MAX_REGISTRY_BYTES:
        raise ValueError("Research source registry exceeds size limit")
    with _lock:
        if path.is_symlink():
            raise ValueError("Research source registry cannot be a symlink")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=".research-sources-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            if path.is_symlink():
                raise ValueError("Research source registry cannot be a symlink")
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
    return registry_snapshot(path)


def update_source(path: Path, *, source: dict | None = None,
                  delete_id: str | None = None,
                  previous_id: str | None = None,
                  granted_ids=()) -> dict:
    """One-source edit preserving other sources and rejecting removal of grants."""
    with _lock:
        registry = registry_snapshot(path)
        by_id = {item["id"]: item for item in registry["sources"]}
        if source is not None:
            parsed = ResearchSource.model_validate(source).model_dump(mode="json")
            if previous_id and previous_id != parsed["id"]:
                if parsed["id"] in by_id:
                    raise ValueError("Another research source already uses that ID")
                if previous_id in granted_ids:
                    raise ValueError("Source is still granted to an agent; update grants first")
                if previous_id not in by_id:
                    raise ValueError("Original research source was not found")
                by_id.pop(previous_id)
            if not parsed["enabled"] and parsed["id"] in granted_ids:
                raise ValueError("Source is still granted to an agent; remove grants before disabling")
            by_id[parsed["id"]] = parsed
        elif delete_id:
            if delete_id in granted_ids:
                raise ValueError("Source is still granted to an agent; update grants first")
            if delete_id not in by_id:
                raise ValueError("Research source was not found")
            by_id.pop(delete_id)
        else:
            raise ValueError("Choose a source to save or delete")
        registry["sources"] = list(by_id.values())
        return write_registry(path, registry)


def test_source(raw: dict, *, transport=None) -> dict:
    """Bounded, read-only provider probe. Never fetches results from search hits."""
    source = ResearchSource.model_validate(raw)
    if not source.enabled:
        return {"ok": False, "source_id": source.id, "reason": "source_disabled"}
    if source.adapter not in {"searxng", "calibre_opds"}:
        raise ValueError("Network test is supported for SearXNG and Calibre OPDS only")
    if source.adapter == "searxng":
        url = source.endpoint.rstrip("/") + "/search"
        url = str(httpx.URL(url).copy_merge_params({"q": "aios-connection-test", "format": "json"}))
        accept = "application/json"
    else:
        url = source.endpoint
        accept = "application/atom+xml, application/xml"
    try:
        with httpx.Client(transport=transport, follow_redirects=False,
                          trust_env=False, timeout=httpx.Timeout(6.0, read=3.0)) as client:
            with client.stream("GET", url, headers={
                "Accept": accept, "Accept-Encoding": "identity",
            }) as response:
                status = response.status_code
                if status != 200:
                    return {"ok": False, "source_id": source.id, "status_code": status,
                            "reason": "http_status"}
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    return {"ok": False, "source_id": source.id, "reason": "encoded_response"}
                content = bytearray()
                for chunk in response.iter_raw():
                    if len(content) + len(chunk) > MAX_TEST_BYTES:
                        return {"ok": False, "source_id": source.id, "reason": "response_too_large"}
                    content.extend(chunk)
                media = response.headers.get("content-type", "").lower()
        if source.adapter == "searxng":
            if "json" not in media:
                return {"ok": False, "source_id": source.id, "reason": "not_json"}
            parsed = json.loads(content)
            valid = isinstance(parsed, dict) and isinstance(parsed.get("results"), list)
        else:
            if "xml" not in media and "atom" not in media:
                return {"ok": False, "source_id": source.id, "reason": "not_opds"}
            root = ElementTree.fromstring(content)
            valid = root.tag in ("{http://www.w3.org/2005/Atom}feed",
                                 "{http://www.w3.org/2005/Atom}entry")
        return {"ok": bool(valid), "source_id": source.id,
                "status_code": 200, "reason": "connected" if valid else "invalid_response"}
    except (httpx.HTTPError, OSError, TimeoutError):
        return {"ok": False, "source_id": source.id, "reason": "connection_failed"}
    except (ElementTree.ParseError, ValueError, UnicodeError):
        # Never reflect response text or target host credentials in errors.
        return {"ok": False, "source_id": source.id, "reason": "invalid_response"}
