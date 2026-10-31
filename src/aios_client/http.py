from __future__ import annotations

import json

import httpx


class APIError(RuntimeError):
    def __init__(self, status, method, path, detail=''):
        self.status = status
        super().__init__(f"API returned HTTP {status} for {method} {path}" + (f": {detail}" if detail else ''))


class APIUnavailable(RuntimeError):
    pass


class API:
    def __init__(self, url: str, key: str = "", timeout: float = 30, transport=None):
        self.key = key
        self.http = httpx.Client(
            base_url=url.rstrip("/") + "/",
            timeout=timeout,
            headers={"Authorization": f"Bearer {key}"} if key else {},
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        )

    def close(self):
        self.http.close()

    def request(self, method, path, **kwargs):
        try:
            # Relative path preserves configured API base paths.
            r = self.http.request(method, path.lstrip("/"), **kwargs)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as exc:
            # Do not log arbitrary response bodies (providers can echo credentials/prompts).
            detail = ''
            # Only setup conflicts from AIOS have a bounded, redacted detail exposed.
            # Provider responses and other routes retain their generic error behavior.
            if not isinstance(self, Model) and exc.response.status_code == 409 and path.lstrip('/') in {
                'workspace-client/bind', 'workspace-client/registrations'}:
                try:
                    value = exc.response.json().get('detail')
                    if isinstance(value, str):
                        if self.key:
                            value = value.replace(self.key, '[REDACTED]')
                        detail = ' '.join(value.split())[:600]
                except (ValueError, AttributeError):
                    pass
            raise APIError(exc.response.status_code, method, path, detail) from None
        except (httpx.HTTPError, json.JSONDecodeError):
            raise APIUnavailable(
                f"API connection or JSON response failed for {method} {path}"
            ) from None


class Model(API):
    def __init__(self, *args, max_tokens=4096, **kwargs):
        if type(max_tokens) is not int or not 128 <= max_tokens <= 32768:
            raise ValueError("Endpoint max_tokens must be an integer from 128 to 32768")
        super().__init__(*args, **kwargs)
        self.max_tokens = max_tokens

    def models(self):
        return [x["id"] for x in self.request("GET", "models").get("data", [])]

    def completion(self, model, messages, tools, timeout=None):
        d = self.request(
            "POST",
            "chat/completions",
            **({"timeout": timeout} if timeout is not None else {}),
            json={
                "model": model,
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto",
                "stream": False,
                "max_tokens": self.max_tokens,
            },
        )
        try:
            message = d["choices"][0]["message"]
            if not isinstance(message, dict) or message.get("role") != "assistant":
                raise ValueError()
            choice = d["choices"][0]
            usage = d.get("usage")
            usage = usage if isinstance(usage, dict) else {}
            # Keep diagnostics out of messages sent back to the provider. Never
            # retain headers, credentials or arbitrary provider response fields.
            return {**message, "_diagnostics": {
                "finish_reason": choice.get("finish_reason")[:80]
                if isinstance(choice.get("finish_reason"), str) else None,
                "max_tokens": self.max_tokens,
                "usage": {k: usage[k] for k in (
                    "prompt_tokens", "completion_tokens", "total_tokens"
                ) if type(usage.get(k)) is int},
            }}
        except (KeyError, IndexError, TypeError, ValueError):
            raise RuntimeError("Model returned an invalid assistant message") from None

    def test_tools(self, model):
        tool = {
            "type": "function",
            "function": {
                "name": "connection_test",
                "description": "Return test value",
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "string", "enum": ["ok"]}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            },
        }
        m = self.completion(
            model,
            [{"role": "user", "content": "Call connection_test with value ok."}],
            [tool],
        )
        try:
            assert m.get("_diagnostics", {}).get("finish_reason") not in {"length", "content_filter"}
            calls = m["tool_calls"]
            assert len(calls) == 1 and calls[0]["function"]["name"] == "connection_test"
            assert json.loads(calls[0]["function"]["arguments"]) == {"value": "ok"}
        except (KeyError, AssertionError, ValueError, TypeError):
            raise ValueError(
                "Generation responded, but the tool-call compatibility test failed"
            ) from None
        return {"ok": True, "message": "Generation and tool-call test passed"}
