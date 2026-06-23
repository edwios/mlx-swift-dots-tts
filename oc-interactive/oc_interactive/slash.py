"""Parse slash commands from -t text."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class SlashKind(Enum):
    NEW_SESSION = auto()
    SET_SYSTEM_PROMPT = auto()
    HELP = auto()
    STATUS = auto()
    DUMP_HISTORY = auto()
    UNKNOWN = auto()


@dataclass(frozen=True)
class SlashCommand:
    kind: SlashKind
    value: str = ""
    raw_verb: str = ""


# Longest verbs first for matching.
_VERBS: list[tuple[str, SlashKind]] = [
    ("dump all", SlashKind.DUMP_HISTORY),
    ("clean all", SlashKind.NEW_SESSION),
    ("system prompt", SlashKind.SET_SYSTEM_PROMPT),
    ("history", SlashKind.DUMP_HISTORY),
    ("status", SlashKind.STATUS),
    ("clear", SlashKind.NEW_SESSION),
    ("help", SlashKind.HELP),
    ("dump", SlashKind.DUMP_HISTORY),
    ("new", SlashKind.NEW_SESSION),
]


def is_slash_command(text: str) -> bool:
    return text.lstrip().startswith("/")


def is_dump_command(cmd: SlashCommand) -> bool:
    return cmd.kind == SlashKind.DUMP_HISTORY


def parse_slash_command(text: str) -> SlashCommand | None:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None

    if "\n" in stripped:
        head, tail = stripped.split("\n", 1)
    else:
        head, tail = stripped, ""

    remainder = head[1:].strip()
    lower_remainder = remainder.lower()

    for verb, kind in _VERBS:
        if lower_remainder == verb:
            if kind == SlashKind.SET_SYSTEM_PROMPT:
                value = tail.strip()
            else:
                value = ""
            return SlashCommand(kind=kind, value=value, raw_verb=verb)

        prefix = verb + " "
        if lower_remainder.startswith(prefix):
            same_line = remainder[len(prefix) :].strip()
            if kind == SlashKind.SET_SYSTEM_PROMPT:
                parts = [same_line, tail.strip()]
                value = "\n".join(p for p in parts if p).strip()
            else:
                value = same_line
            return SlashCommand(kind=kind, value=value, raw_verb=verb)

    first_word = remainder.split(None, 1)[0].lower() if remainder else ""
    return SlashCommand(kind=SlashKind.UNKNOWN, raw_verb=first_word or remainder)


def confirmation_text(cmd: SlashCommand, *, message_count: int = 0) -> str:
    if cmd.kind == SlashKind.NEW_SESSION:
        return "New session started."
    if cmd.kind == SlashKind.SET_SYSTEM_PROMPT:
        return (
            "System prompt cleared."
            if not cmd.value
            else "System prompt updated."
        )
    if cmd.kind == SlashKind.HELP:
        return (
            "Commands: new, clear, clean all, system prompt, help, status, "
            "dump, dump all, and history."
        )
    if cmd.kind == SlashKind.STATUS:
        return f"Session active. {message_count} messages."
    return ""
