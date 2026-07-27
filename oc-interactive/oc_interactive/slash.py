"""Parse slash commands from -t text."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class SlashKind(Enum):
    NEW_SESSION = auto()
    SET_SYSTEM_PROMPT = auto()
    SET_VOICE_DESIGN = auto()
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
    ("voice design", SlashKind.SET_VOICE_DESIGN),
    ("voice-design", SlashKind.SET_VOICE_DESIGN),
    ("history", SlashKind.DUMP_HISTORY),
    ("status", SlashKind.STATUS),
    ("clear", SlashKind.NEW_SESSION),
    ("help", SlashKind.HELP),
    ("dump", SlashKind.DUMP_HISTORY),
    ("new", SlashKind.NEW_SESSION),
]

# Verbs whose value may span multiple lines (same-line text + following lines).
_MULTILINE_VALUE_KINDS = (SlashKind.SET_SYSTEM_PROMPT, SlashKind.SET_VOICE_DESIGN)


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
            if kind in _MULTILINE_VALUE_KINDS:
                value = tail.strip()
            else:
                value = ""
            return SlashCommand(kind=kind, value=value, raw_verb=verb)

        prefix = verb + " "
        if lower_remainder.startswith(prefix):
            same_line = remainder[len(prefix) :].strip()
            if kind in _MULTILINE_VALUE_KINDS:
                parts = [same_line, tail.strip()]
                value = "\n".join(p for p in parts if p).strip()
            else:
                value = same_line
            return SlashCommand(kind=kind, value=value, raw_verb=verb)

    first_word = remainder.split(None, 1)[0].lower() if remainder else ""
    return SlashCommand(kind=SlashKind.UNKNOWN, raw_verb=first_word or remainder)


# Base commands supported everywhere (one-shot CLI and chat UI, via the daemon).
_HELP_COMMANDS: list[str] = [
    "new",
    "clear",
    "clean all",
    "system prompt",
    "voice design",
    "help",
    "status",
    "dump",
    "dump all",
    "history",
]


def _join_commands(commands: list[str]) -> str:
    if not commands:
        return ""
    if len(commands) == 1:
        return commands[0]
    return ", ".join(commands[:-1]) + ", and " + commands[-1]


def confirmation_text(
    cmd: SlashCommand,
    *,
    message_count: int = 0,
    extra_commands: list[str] | None = None,
) -> str:
    if cmd.kind == SlashKind.NEW_SESSION:
        return "New session started."
    if cmd.kind == SlashKind.SET_SYSTEM_PROMPT:
        return (
            "System prompt cleared."
            if not cmd.value
            else "System prompt updated."
        )
    if cmd.kind == SlashKind.SET_VOICE_DESIGN:
        # daemon.py builds the actual spoken confirmation (it cites the
        # current description, or names the resulting speaker/model); this
        # is a generic fallback only.
        if not cmd.value:
            return "Current voice design requested."
        if cmd.value.strip().lower() in ("reset", "default"):
            return "Voice design reset."
        return "Voice design updated."
    if cmd.kind == SlashKind.HELP:
        commands = _HELP_COMMANDS + list(extra_commands or [])
        return f"Commands: {_join_commands(commands)}."
    if cmd.kind == SlashKind.STATUS:
        return f"Session active. {message_count} messages."
    return ""
