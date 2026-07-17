"""Prepare text for TTS synthesis."""

from __future__ import annotations


def agent_error_line(reason: str) -> str:
    return f"Something wrong with the agent, {reason}"


def ensure_utf8(text: str) -> str:
    encoded = text.encode("utf-8")
    decoded = encoded.decode("utf-8")
    if decoded != text:
        raise ValueError("invalid UTF-8 reply")
    return text
