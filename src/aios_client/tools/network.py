"""Bounded read-only HTTP probes to operator-configured exact origins."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Annotated, Literal
from urllib.parse import unquote
from uuid import uuid4

import httpx
from pydantic import Field


def canonical_origin(value: str, *, origin_only=False):
    if (
        not isinstance(value, str)
        or any(c.isspace() or ord(c) < 32 for c in value)
        or "\\" in value
    ):
        raise ValueError(
            "Use an absolute HTTP(S) URL without whitespace or credentials"
        )
    try:
        url = httpx.URL(value)
    except (httpx.InvalidURL, ValueError):
        raise ValueError("Invalid HTTP URL") from None
    if (
        url.scheme not in ("http", "https")
        or not url.host
        or url.userinfo
        or url.fragment
        or (origin_only and (url.path != "/" or url.query))
    ):
        raise ValueError(
            "Use an HTTP(S) origin without credentials, path, query or fragment"
            if origin_only
            else "Use an absolute HTTP(S) URL without credentials or fragment"
        )
    host = f"[{url.host}]" if ":" in url.host else url.host
    port = f":{url.port}" if url.port is not None else ""
    return f"{url.scheme}://{host}{port}"


class HTTPTools:
    def __init__(self, workspace, origins=(), transport=None):
        self.workspace = workspace
        self.origins = frozenset(
            canonical_origin(origin, origin_only=True) for origin in origins
        )
        self.transport = transport

    def http_request(
        self,
        url: Annotated[str, Field(min_length=1, max_length=2000)],
        method: Literal["GET", "HEAD", "OPTIONS"] = "GET",
        timeout_seconds: Annotated[float, Field(gt=0, le=30)] = 10,
        max_bytes: Annotated[int, Field(ge=1, le=131072)] = 65536,
        max_chars: Annotated[int, Field(ge=1, le=12000)] = 6000,
    ):
        origin = canonical_origin(url)
        if origin not in self.origins:
            raise ValueError(
                "HTTP origin is not enabled in this agent profile's http_origins"
            )
        if any(secret in unquote(url) for secret in self.workspace.secrets):
            raise ValueError("Do not put configured credentials in HTTP probe URLs")
        self.workspace.guard()
        timeout = min(timeout_seconds, self.workspace.command_timeout)
        body, headers, status_code, truncated, error = asyncio.run(
            self._probe(url, method, timeout, max_bytes)
        )
        if error is not None:
            return {
                "ok": False,
                "error": {"code": error, "message": "HTTP probe failed"},
                "origin": origin,
                "method": method,
                "status_code": status_code,
            }
        media_type = headers.get("content-type", "").split(";", 1)[0].lower()
        is_text = (
            media_type.startswith("text/")
            or "json" in media_type
            or "xml" in media_type
            or not media_type
        )
        # Unexpected compressed bodies remain bounded raw bytes, not an
        # unbounded decompression operation. Request identity encoding below.
        encoded = headers.get("content-encoding", "identity").lower() != "identity"
        is_text = is_text and not encoded
        text = (
            self.workspace.redact(bytes(body).decode("utf-8", "replace"))
            if is_text
            else ""
        )
        if truncated and is_text:
            # A byte cap can cut a known credential before its full value reaches
            # redact(). Suppress a possible trailing credential prefix as well.
            for secret in self.workspace.secrets:
                for length in range(min(len(secret) - 1, len(text)), 0, -1):
                    if text.endswith(secret[:length]):
                        text = text[:-length] + "[REDACTED_PARTIAL]"
                        break
        artifact_id = None
        if is_text:
            artifact_id = str(uuid4()) + ".http.txt"
            (self.workspace.artifacts / artifact_id).write_text(text, encoding="utf-8")
        return {
            "origin": origin,
            "url": url,
            "method": method,
            "status_code": status_code,
            "http_success": 200 <= status_code < 300,
            "headers": headers,
            "body": text[:max_chars],
            "body_is_text": is_text,
            "body_encoded": encoded,
            "captured_bytes": len(body),
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "body_truncated": truncated,
            "excerpt_truncated": len(text) > max_chars,
            "artifact_id": artifact_id,
            "redirect_followed": False,
        }

    async def _probe(self, url, method, timeout, max_bytes):
        body, headers, status_code, truncated = bytearray(), {}, None, False
        try:
            # The aggregate async deadline also covers trickled response headers,
            # which a socket inactivity timeout alone cannot bound.
            async with asyncio.timeout(timeout):
                async with httpx.AsyncClient(
                    transport=self.transport,
                    trust_env=False,
                    follow_redirects=False,
                    timeout=httpx.Timeout(timeout, read=min(timeout, 1)),
                ) as client:
                    async with client.stream(
                        method,
                        url,
                        headers={
                            "Accept": "application/json, text/plain, */*",
                            "Accept-Encoding": "identity",
                        },
                    ) as response:
                        status_code = response.status_code
                        headers = {
                            key: value
                            for key, value in response.headers.items()
                            if key
                            in {
                                "content-type",
                                "content-length",
                                "content-encoding",
                                "etag",
                                "last-modified",
                                "location",
                                "retry-after",
                            }
                        }
                        async for chunk in response.aiter_raw():
                            self.workspace.guard()
                            remaining = max_bytes - len(body)
                            body.extend(chunk[:remaining])
                            if len(chunk) > remaining:
                                truncated = True
                                break
        except (httpx.HTTPError, TimeoutError) as exc:
            # Exception text can embed URLs; keep only a stable error class.
            return body, headers, status_code, truncated, type(exc).__name__
        self.workspace.guard()
        return body, headers, status_code, truncated, None


def register(registry, http):
    registry.add(
        "http_request",
        http.http_request,
        "Probe operator-enabled HTTP origins using GET/HEAD/OPTIONS; no redirects or credentials. HTTP status is separate from request failure",
        "workspace.http",
        effect="execute",
    )
