"""OpenClaw chat completions HTTP client."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass
class OpenClawError(Exception):
    reason: str

    def __str__(self) -> str:
        return self.reason


def chat_completion(
    *,
    base_url: str,
    token: str,
    model: str,
    user_id: str,
    messages: list[dict[str, str]],
    timeout: float = 300.0,
) -> str:
    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    body = {
        "model": model,
        "user": user_id,
        "messages": messages,
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = _http_error_detail(e)
        if e.code in (401, 403):
            raise OpenClawError("unauthorized") from e
        raise OpenClawError(f"HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise OpenClawError(f"connection failed: {e.reason}") from e

    return _parse_reply(payload)


def _http_error_detail(e: urllib.error.HTTPError) -> str:
    try:
        raw = e.read().decode("utf-8", errors="replace")
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and "error" in parsed:
            err = parsed["error"]
            if isinstance(err, dict) and "message" in err:
                return str(err["message"])
            return str(err)
        return raw[:200] if raw else e.reason
    except Exception:
        return str(e.reason)


def _parse_reply(payload: dict[str, Any]) -> str:
    if "error" in payload:
        err = payload["error"]
        if isinstance(err, dict):
            msg = err.get("message") or err.get("type") or str(err)
        else:
            msg = str(err)
        raise OpenClawError(msg)

    choices = payload.get("choices")
    if not choices or not isinstance(choices, list):
        raise OpenClawError("empty reply")

    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise OpenClawError("empty reply")

    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()

    tool_calls = message.get("tool_calls")
    if tool_calls:
        raise OpenClawError("reply contained tool calls only")

    raise OpenClawError("empty reply")
