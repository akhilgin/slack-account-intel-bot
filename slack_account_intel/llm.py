"""Minimal Anthropic Messages API client on the standard library."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"


class LLMError(Exception):
    """Any failure talking to the model. Callers fall back to non-LLM behavior."""


class AnthropicClient:
    def __init__(self, api_key: str, model: str, timeout: float = 30.0):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def build_request(self, system: str, user: str, max_tokens: int) -> urllib.request.Request:
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        return urllib.request.Request(
            API_URL,
            data=json.dumps(body).encode(),
            method="POST",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": API_VERSION,
                "content-type": "application/json",
            },
        )

    def complete(self, system: str, user: str, max_tokens: int = 600) -> str:
        req = self.build_request(system, user, max_tokens)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
            raise LLMError(f"request failed: {e}") from e
        parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        text = "".join(parts).strip()
        if not text:
            raise LLMError("empty response")
        return text


def parse_json_object(raw: str) -> dict:
    """Pull the first {...} object out of a model reply, tolerating code fences."""
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in model reply")
    obj = json.loads(raw[start : end + 1])
    if not isinstance(obj, dict):
        raise ValueError("model reply is not a JSON object")
    return obj
