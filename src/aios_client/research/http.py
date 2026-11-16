"""Bounded HTTP transport for operator-approved research endpoints."""

import asyncio

import httpx

SEARXNG_FORBIDDEN_HINT = (
    "SearXNG rejected the JSON search request (HTTP 403). Enable json in "
    "search.formats in settings.yml, or check limiter and reverse-proxy access rules."
)


def searxng_request(endpoint, query):
    return endpoint.rstrip("/") + "/search", {"q": query, "format": "json"}


async def fetch(url, *, transport=None, timeout, max_bytes, accept, form=None, guard=None):
    body = bytearray()
    try:
        async with asyncio.timeout(timeout):
            async with httpx.AsyncClient(
                transport=transport, trust_env=False, follow_redirects=False, timeout=timeout
            ) as client:
                async with client.stream(
                    "POST" if form is not None else "GET", url, data=form,
                    headers={"Accept": accept, "Accept-Encoding": "identity"},
                ) as response:
                    if guard is not None:
                        guard()
                    if response.status_code != 200:
                        return None, None, f"http_status_{response.status_code}"
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        return None, None, "encoded_response"
                    chunks = (
                        response.aiter_bytes() if response.is_stream_consumed else response.aiter_raw()
                    )
                    async for chunk in chunks:
                        if guard is not None:
                            guard()
                        if len(body) + len(chunk) > max_bytes:
                            return None, None, "response_too_large"
                        body.extend(chunk)
                    return bytes(body), response.headers.get("content-type", "").lower(), None
    except (httpx.HTTPError, httpx.InvalidURL, OSError, TimeoutError) as exc:
        # Exception messages can contain request URLs.
        return None, None, type(exc).__name__
