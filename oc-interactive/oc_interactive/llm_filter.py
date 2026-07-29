"""Local LLM text-filter client.

Talks to any OpenAI-compatible ``/v1/chat/completions`` endpoint (e.g. LM
Studio: https://lmstudio.ai/docs/developer/rest) to transform text before
it is handed to TTS. Kept independent from ``openclaw.py`` (the agent
chat client) since the filter LLM is a separate local server with no auth
token and no conversation/user-id state.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass
class FilterError(Exception):
    reason: str

    def __str__(self) -> str:
        return self.reason


def filter_text(
    text: str,
    *,
    base_url: str,
    model: str,
    system_prompt: str | None,
    timeout: float = 30.0,
) -> str:
    """Route ``text`` through a local chat model and return its reply.

    Used to rewrite/redact/restyle text right before TTS synthesis. Raises
    ``FilterError`` on any failure (connection, timeout, malformed reply);
    callers should fall back to the original ``text`` rather than let a
    local-LLM hiccup silently swallow speech.
    """
    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": text})

    body = {"model": model, "messages": messages, "context_length": 8000}
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = _http_error_detail(e)
        raise FilterError(f"HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise FilterError(f"connection failed: {e.reason}") from e
    except TimeoutError as e:
        raise FilterError("timed out") from e
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise FilterError(f"malformed response: {e}") from e

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
        raise FilterError(msg)

    choices = payload.get("choices")
    if not choices or not isinstance(choices, list):
        raise FilterError("empty reply")

    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise FilterError("empty reply")

    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()

    raise FilterError("empty reply")
