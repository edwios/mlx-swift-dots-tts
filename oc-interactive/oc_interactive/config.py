"""OpenClaw gateway configuration."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_ENV_REF = re.compile(r"^\$([A-Za-z_][A-Za-z0-9_]*)$")


@dataclass(frozen=True)
class OpenClawConfig:
    base_url: str
    token: str
    default_agent: str
    agents: tuple[str, ...]
    dots_tts_binary: Path | None
    raw: dict[str, Any]

    def resolve_agent(self, name: str | None) -> str:
        agent = (name or self.default_agent).strip().lower()
        if agent.startswith("openclaw/"):
            agent = agent[len("openclaw/") :]
        if agent not in self.agents:
            allowed = ", ".join(self.agents)
            raise ValueError(
                f'agent "{name}" not allowed; permitted: {allowed}'
            )
        return agent

    def openclaw_model(self, agent: str) -> str:
        return f"openclaw/{agent}"


def _resolve_token(value: str) -> str:
    value = value.strip()
    m = _ENV_REF.match(value)
    if m:
        env_name = m.group(1)
        resolved = os.environ.get(env_name)
        if not resolved:
            raise ValueError(
                f"environment variable {env_name} is not set "
                f"(required by openclawToken {value!r})"
            )
        return resolved
    return value


def _expand_path(value: str | None, *, base: Path) -> Path | None:
    if not value:
        return None
    p = Path(value).expanduser()
    if not p.is_absolute():
        p = (base / p).resolve()
    else:
        p = p.resolve()
    return p


def load_config(path: Path) -> OpenClawConfig:
    if not path.exists():
        raise FileNotFoundError(f"openclaw config not found: {path}")

    with path.open(encoding="utf-8") as f:
        raw: dict[str, Any] = json.load(f)

    base_url = str(raw.get("openclawBaseURL", "")).rstrip("/")
    if not base_url:
        raise ValueError("openclawBaseURL is required in config")

    token_raw = raw.get("openclawToken")
    if not token_raw:
        raise ValueError("openclawToken is required in config")
    token = _resolve_token(str(token_raw))

    agents_list = raw.get("agents") or ["main"]
    agents = tuple(str(a).lower() for a in agents_list)
    if not agents:
        raise ValueError("agents list must not be empty")

    default_agent = str(raw.get("defaultAgent", agents[0])).lower()
    if default_agent not in agents:
        raise ValueError(
            f'defaultAgent "{default_agent}" is not in agents list'
        )

    dots_bin = _expand_path(raw.get("dotsTtsBinary"), base=path.parent)

    return OpenClawConfig(
        base_url=base_url,
        token=token,
        default_agent=default_agent,
        agents=agents,
        dots_tts_binary=dots_bin,
        raw=raw,
    )
