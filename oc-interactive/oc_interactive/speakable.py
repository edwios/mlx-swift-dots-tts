"""Prepare text for dots-tts synthesis."""

from __future__ import annotations

EN_TAG = "[EN]"


def tag_for_tts(text: str, *, language: str = "EN") -> str:
    """Prefix [CODE] when not already present (dots-tts English = EN)."""
    text = text.strip()
    code = language.upper() if language else "EN"
    tag = f"[{code}]"
    if text.startswith("["):
        return text
    return tag + text


def agent_error_line(reason: str) -> str:
    return f"Something wrong with the agent, {reason}"


def ensure_utf8(text: str) -> str:
    encoded = text.encode("utf-8")
    decoded = encoded.decode("utf-8")
    if decoded != text:
        raise ValueError("invalid UTF-8 reply")
    return text
