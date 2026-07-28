"""Session state, persisted per-agent under agents/<agent>/session.json.

Each OpenClaw agent (main, news, eileen, ...) gets its own session file, its
own conversation history, its own cached voice/system-prompt/filter/config
settings, and its own archive directory. A small separate pointer file
(``active_agent.json``) records which agent's session is "current" so a
launch with no ``--agent`` restores the right one, and so a mid-chat
``/agent <name>`` switch is remembered the next time any oc-interactive
entry point starts.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from oc_interactive.paths import (
    active_agent_path,
    agent_session_path,
    agent_sessions_archive_dir,
    ensure_state_dir,
    normalize_agent_name,
    session_path,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_user_id() -> str:
    return f"oc-interactive:{uuid.uuid4()}"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


@dataclass
class Session:
    user_id: str = field(default_factory=_new_user_id)
    system_prompt: str | None = None
    last_agent: str | None = None
    last_refaudio: str | None = None
    last_reftext: str | None = None
    last_tts_model: str | None = None
    last_speaker: str | None = None
    last_instruct: str | None = None
    last_voice_mode: str | None = None
    last_voice_design: str | None = None
    last_language: str | None = None
    last_openclaw_config: str | None = None
    # None = never explicitly toggled this session -> defer to the config's
    # filterEnabled/filterPrompt default (see config.py, daemon._effective_filter_*).
    # An explicit /filter on|off or /filter <description> always overrides it.
    filter_enabled: bool | None = None
    filter_prompt: str | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Session:
        return cls(
            user_id=data.get("userId") or _new_user_id(),
            system_prompt=data.get("systemPrompt") or None,
            last_agent=data.get("lastAgent"),
            last_refaudio=data.get("lastRefaudio"),
            last_reftext=data.get("lastReftext"),
            last_tts_model=data.get("lastTtsModel"),
            last_speaker=data.get("lastSpeaker"),
            last_instruct=data.get("lastInstruct"),
            last_voice_mode=data.get("lastVoiceMode"),
            last_voice_design=data.get("lastVoiceDesign"),
            last_language=data.get("lastLanguage"),
            last_openclaw_config=data.get("lastOpenclawConfig"),
            # Preserve None (unset -> use config default) vs. an explicit
            # True/False previously written by /filter on|off.
            filter_enabled=(
                data.get("filterEnabled")
                if isinstance(data.get("filterEnabled"), bool)
                else None
            ),
            filter_prompt=data.get("filterPrompt") or None,
            messages=list(data.get("messages") or []),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "userId": self.user_id,
            "messages": self.messages,
        }
        if self.system_prompt:
            out["systemPrompt"] = self.system_prompt
        if self.last_agent:
            out["lastAgent"] = self.last_agent
        if self.last_refaudio:
            out["lastRefaudio"] = self.last_refaudio
        if self.last_reftext:
            out["lastReftext"] = self.last_reftext
        if self.last_tts_model:
            out["lastTtsModel"] = self.last_tts_model
        if self.last_speaker:
            out["lastSpeaker"] = self.last_speaker
        if self.last_instruct:
            out["lastInstruct"] = self.last_instruct
        if self.last_voice_mode:
            out["lastVoiceMode"] = self.last_voice_mode
        if self.last_voice_design:
            out["lastVoiceDesign"] = self.last_voice_design
        if self.last_language:
            out["lastLanguage"] = self.last_language
        if self.last_openclaw_config:
            out["lastOpenclawConfig"] = self.last_openclaw_config
        if self.filter_enabled is not None:
            # Written explicitly (True or False) only once /filter on|off has
            # been used this session; omitted while still None so config's
            # filterEnabled default keeps applying.
            out["filterEnabled"] = self.filter_enabled
        if self.filter_prompt:
            out["filterPrompt"] = self.filter_prompt
        return out

    def dump_document(self, agent: str | None = None) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "sessionId": self.user_id,
            "agent": agent or self.last_agent or "main",
            "messageCount": len(self.messages),
            "messages": self.messages,
        }
        if self.system_prompt:
            doc["systemPrompt"] = self.system_prompt
        else:
            doc["systemPrompt"] = None
        return doc


def load_session(agent: str, path: Path | None = None) -> Session:
    """Load the session belonging to ``agent``.

    Runs the one-time legacy migration first (see
    ``_migrate_legacy_session``) so an old flat ``session.json`` from
    before sessions were split per-agent isn't silently orphaned.
    """
    _migrate_legacy_session()
    p = path or agent_session_path(agent)
    if not p.exists():
        return Session(last_agent=normalize_agent_name(agent) or None)
    with p.open(encoding="utf-8") as f:
        data = json.load(f)
    return Session.from_dict(data)


def save_session(session: Session, agent: str, path: Path | None = None) -> None:
    ensure_state_dir()
    p = path or agent_session_path(agent)
    _atomic_write_json(p, session.to_dict())


def new_session(agent: str, *, keep_system_prompt: bool = True) -> Session:
    current = load_session(agent)
    return Session(
        user_id=_new_user_id(),
        system_prompt=current.system_prompt if keep_system_prompt else None,
        last_agent=normalize_agent_name(agent) or agent,
        last_refaudio=current.last_refaudio,
        last_reftext=current.last_reftext,
        last_tts_model=current.last_tts_model,
        last_speaker=current.last_speaker,
        last_instruct=current.last_instruct,
        last_voice_mode=current.last_voice_mode,
        last_voice_design=current.last_voice_design,
        last_language=current.last_language,
        last_openclaw_config=current.last_openclaw_config,
        filter_enabled=current.filter_enabled,
        filter_prompt=current.filter_prompt,
        messages=[],
    )


def archive_current_session(agent: str, session: Session | None = None) -> Path | None:
    """Write ``agent``'s current session under its archive/ dir if it has
    messages.

    Returns the archive path, or None when there was nothing to archive.
    """
    current = session or load_session(agent)
    if not current.messages:
        return None

    ensure_state_dir()
    archive_root = agent_sessions_archive_dir(agent)
    archive_root.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_id = current.user_id.replace(":", "_").replace("/", "_")
    dest = archive_root / f"{stamp}_{safe_id}.json"
    _atomic_write_json(dest, current.to_dict())
    return dest


def archive_and_new_session(
    agent: str, *, keep_system_prompt: bool = True
) -> tuple[Session, Path | None]:
    """Archive ``agent``'s current session (if it has messages) and start a
    new one for that same agent. Cached voice/system-prompt/filter/config
    settings carry over; only the conversation resets. Other agents'
    sessions are untouched."""
    current = load_session(agent)
    archived = archive_current_session(agent, current)
    session = new_session(agent, keep_system_prompt=keep_system_prompt)
    save_session(session, agent)
    return session, archived


def reset_session(agent: str) -> tuple[Session, Path | None]:
    """Archive ``agent``'s current session (if it has messages) and replace
    it with a completely blank one -- no cached voice/system-prompt/filter/
    config carried over. This is the harder reset used by ``--init``,
    versus ``archive_and_new_session`` (``/new``) which keeps those caches.
    """
    current = load_session(agent)
    archived = archive_current_session(agent, current)
    norm = normalize_agent_name(agent) or agent
    session = Session(last_agent=norm)
    save_session(session, norm)
    return session, archived


def reset_all_agents(agents: list[str]) -> dict[str, Path | None]:
    """Reset every agent in ``agents`` (see ``reset_session``) and clear the
    active-agent pointer, so the next launch without --agent falls back to
    the config's defaultAgent instead of a stale pointer. Returns a map of
    normalized agent name -> archive path (None if nothing to archive)."""
    archived: dict[str, Path | None] = {}
    seen: set[str] = set()
    for agent in agents:
        norm = normalize_agent_name(agent)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        _, path = reset_session(norm)
        archived[norm] = path
    clear_active_agent()
    return archived


def load_active_agent() -> str | None:
    """Return the agent name the last turn (anywhere) was talking to, or
    None if there's no cached pointer yet."""
    _migrate_legacy_session()
    p = active_agent_path()
    if not p.exists():
        return None
    try:
        with p.open(encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    agent = data.get("agent")
    return agent if isinstance(agent, str) and agent else None


def save_active_agent(agent: str) -> None:
    ensure_state_dir()
    norm = normalize_agent_name(agent) or agent
    _atomic_write_json(active_agent_path(), {"agent": norm})


def clear_active_agent() -> None:
    p = active_agent_path()
    if p.exists():
        p.unlink()


def append_user_message(session: Session, content: str) -> None:
    session.messages.append(
        {"role": "user", "content": content, "timestamp": _utc_now()}
    )


def append_assistant_message(
    session: Session, content: str, *, agent: str
) -> None:
    session.messages.append(
        {
            "role": "assistant",
            "content": content,
            "agent": agent,
            "timestamp": _utc_now(),
        }
    )


def build_api_messages(session: Session, new_user_text: str) -> list[dict[str, str]]:
    """Build OpenAI-style messages for the chat completion request."""
    out: list[dict[str, str]] = []
    if session.system_prompt:
        out.append({"role": "system", "content": session.system_prompt})
    for msg in session.messages:
        role = msg.get("role")
        content = msg.get("content")
        if role in ("user", "assistant") and isinstance(content, str):
            out.append({"role": role, "content": content})
    out.append({"role": "user", "content": new_user_text})
    return out


def _migrate_legacy_session() -> None:
    """One-time migration of the pre-per-agent flat ``session.json`` into
    the new ``agents/<agent>/session.json`` layout.

    No-ops immediately once the legacy file has been renamed away (first
    call after upgrade), so this is cheap to call defensively from every
    session/active-agent read.
    """
    legacy = session_path()
    if not legacy.exists():
        return
    try:
        with legacy.open(encoding="utf-8") as f:
            data = json.load(f)
        legacy_session = Session.from_dict(data)
    except (OSError, json.JSONDecodeError):
        legacy.rename(legacy.with_name(legacy.name + ".migrate-failed"))
        return

    agent = normalize_agent_name(legacy_session.last_agent) or "main"
    dest = agent_session_path(agent)
    if not dest.exists():
        save_session(legacy_session, agent)
        save_active_agent(agent)
    legacy.rename(legacy.with_name(legacy.name + ".migrated"))
