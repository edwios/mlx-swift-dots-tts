"""Session state persisted in session.json."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from oc_interactive.paths import ensure_state_dir, session_path, sessions_dir


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_user_id() -> str:
    return f"oc-interactive:{uuid.uuid4()}"


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


def load_session(path: Path | None = None) -> Session:
    p = path or session_path()
    if not p.exists():
        return Session()
    with p.open(encoding="utf-8") as f:
        data = json.load(f)
    return Session.from_dict(data)


def save_session(session: Session, path: Path | None = None) -> None:
    ensure_state_dir()
    p = path or session_path()
    tmp = p.with_suffix(".json.tmp")
    payload = json.dumps(session.to_dict(), indent=2, ensure_ascii=False) + "\n"
    with tmp.open("w", encoding="utf-8") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def new_session(*, keep_system_prompt: bool = True) -> Session:
    current = load_session()
    return Session(
        user_id=_new_user_id(),
        system_prompt=current.system_prompt if keep_system_prompt else None,
        last_agent=current.last_agent,
        last_refaudio=current.last_refaudio,
        last_reftext=current.last_reftext,
        last_tts_model=current.last_tts_model,
        last_speaker=current.last_speaker,
        last_instruct=current.last_instruct,
        last_voice_mode=current.last_voice_mode,
        last_voice_design=current.last_voice_design,
        last_language=current.last_language,
        last_openclaw_config=current.last_openclaw_config,
        messages=[],
    )


def archive_current_session(session: Session | None = None) -> Path | None:
    """Write the current session under sessions/ if it has messages.

    Returns the archive path, or None when there was nothing to archive.
    """
    current = session or load_session()
    if not current.messages:
        return None

    ensure_state_dir()
    archive_root = sessions_dir()
    archive_root.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_id = current.user_id.replace(":", "_").replace("/", "_")
    dest = archive_root / f"{stamp}_{safe_id}.json"
    payload = json.dumps(current.to_dict(), indent=2, ensure_ascii=False) + "\n"
    tmp = dest.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, dest)
    return dest


def archive_and_new_session(*, keep_system_prompt: bool = True) -> tuple[Session, Path | None]:
    """Archive the current session (if it has messages) and start a new one."""
    current = load_session()
    archived = archive_current_session(current)
    session = new_session(keep_system_prompt=keep_system_prompt)
    save_session(session)
    return session, archived


def clear_cached_settings(session: Session | None = None) -> Session:
    """Drop cached voice/agent/config paths; keep conversation and system prompt."""
    s = session or load_session()
    s.last_agent = None
    s.last_refaudio = None
    s.last_reftext = None
    s.last_tts_model = None
    s.last_speaker = None
    s.last_instruct = None
    s.last_voice_mode = None
    s.last_voice_design = None
    s.last_language = None
    s.last_openclaw_config = None
    save_session(s)
    return s


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
