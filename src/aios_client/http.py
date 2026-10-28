from __future__ import annotations

import json

import httpx


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
            raise RuntimeError(
                f"API returned HTTP {exc.response.status_code} for {method} {path}"
            ) from None
        except (httpx.HTTPError, json.JSONDecodeError):
            raise RuntimeError(
                f"API connection or JSON response failed for {method} {path}"
            ) from None


class Model(API):
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
                "max_tokens": 2048,
            },
        )
        try:
            message = d["choices"][0]["message"]
            if message.get("role") != "assistant":
                raise ValueError()
            return message
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
            calls = m["tool_calls"]
            assert len(calls) == 1 and calls[0]["function"]["name"] == "connection_test"
            assert json.loads(calls[0]["function"]["arguments"]) == {"value": "ok"}
        except (KeyError, AssertionError, ValueError, TypeError):
            raise ValueError(
                "Generation responded, but the tool-call compatibility test failed"
            ) from None
        return {"ok": True, "message": "Generation and tool-call test passed"}
