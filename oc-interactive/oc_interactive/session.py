"""Session state persisted in session.json."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from oc_interactive.paths import ensure_state_dir, session_path


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
    last_dots_tts: str | None = None
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
            last_dots_tts=data.get("lastDotsTts"),
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
        if self.last_dots_tts:
            out["lastDotsTts"] = self.last_dots_tts
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
        last_dots_tts=current.last_dots_tts,
        last_openclaw_config=current.last_openclaw_config,
        messages=[],
    )


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
