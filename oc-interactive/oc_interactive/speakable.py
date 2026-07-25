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


def select_tts_text(text: str) -> str:
    """Pick what actually gets sent to the TTS engine.

    If the first line is enclosed in square brackets (e.g. a stage
    direction or tag like ``[laughs]``), only that line is spoken, with
    the brackets stripped. Otherwise the full text is spoken unchanged.
    """
    first_line = text.split("\n", 1)[0].strip()
    if len(first_line) >= 2 and first_line.startswith("[") and first_line.endswith("]"):
        return first_line[1:-1].strip()
    return text
