"""OpenClaw gateway configuration."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from oc_interactive.filter_defaults import (
    DEFAULT_FILTER_BASE_URL,
    DEFAULT_FILTER_MODEL,
)
from oc_interactive.tts_defaults import (
    DEFAULT_LANGUAGE,
    DEFAULT_SPEAKER,
    DEFAULT_TTS_MODEL,
)

_ENV_REF = re.compile(r"^\$([A-Za-z_][A-Za-z0-9_]*)$")


@dataclass(frozen=True)
class OpenClawConfig:
    base_url: str
    token: str
    default_agent: str
    agents: tuple[str, ...]
    tts_model: str
    tts_speaker: str
    tts_instruct: str | None
    tts_language: str
    tts_voice_design: str | None
    filter_base_url: str
    filter_model: str
    filter_prompt: str | None
    filter_enabled: bool
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


def _resolve_model(value: str | None, *, base: Path) -> str:
    """Return HF repo id as-is, or resolve a local path relative to config."""
    if not value:
        return DEFAULT_TTS_MODEL
    raw = value.strip()
    if not raw:
        return DEFAULT_TTS_MODEL
    # Local paths: absolute, relative, or existing on disk.
    looks_local = (
        raw.startswith(("/", "./", "../", "~"))
        or Path(raw).expanduser().exists()
    )
    if looks_local:
        expanded = _expand_path(raw, base=base)
        return str(expanded) if expanded else DEFAULT_TTS_MODEL
    return raw


def load_config(path: Path) -> OpenClawConfig:
    if not path.exists():
        raise FileNotFoundError(f"oc-interactive config not found: {path}")

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

    tts_model = _resolve_model(raw.get("ttsModel"), base=path.parent)
    tts_speaker = str(raw.get("ttsSpeaker") or DEFAULT_SPEAKER).strip() or DEFAULT_SPEAKER

    instruct_raw = raw.get("ttsInstruct")
    if isinstance(instruct_raw, str):
        tts_instruct = instruct_raw.strip() or None
    else:
        tts_instruct = None

    language_raw = raw.get("ttsLanguage")
    if isinstance(language_raw, str) and language_raw.strip():
        tts_language = language_raw.strip()
    else:
        tts_language = DEFAULT_LANGUAGE

    voice_design_raw = raw.get("ttsVoiceDesign")
    if isinstance(voice_design_raw, str):
        tts_voice_design = voice_design_raw.strip() or None
    else:
        tts_voice_design = None

    filter_base_url_raw = raw.get("filterBaseURL")
    filter_base_url = (
        str(filter_base_url_raw).strip().rstrip("/")
        if isinstance(filter_base_url_raw, str) and filter_base_url_raw.strip()
        else DEFAULT_FILTER_BASE_URL
    )

    filter_model_raw = raw.get("filterModel")
    filter_model = (
        str(filter_model_raw).strip()
        if isinstance(filter_model_raw, str) and filter_model_raw.strip()
        else DEFAULT_FILTER_MODEL
    )

    # filterPrompt: default system prompt for the /filter LLM. json.load already
    # decodes any "\n" written in the config file into a real newline, same as
    # any other JSON string -- no extra unescaping needed here (unlike the
    # run-chat single-line-input workaround handled in daemon.py).
    filter_prompt_raw = raw.get("filterPrompt")
    filter_prompt = (
        filter_prompt_raw.strip() or None
        if isinstance(filter_prompt_raw, str)
        else None
    )

    # filterEnabled: default on/off state for a session that hasn't explicitly
    # run /filter on or /filter off yet. Explicit session state (see session.py)
    # always overrides this once set.
    filter_enabled_raw = raw.get("filterEnabled")
    filter_enabled = bool(filter_enabled_raw) if isinstance(filter_enabled_raw, bool) else False

    return OpenClawConfig(
        base_url=base_url,
        token=token,
        default_agent=default_agent,
        agents=agents,
        tts_model=tts_model,
        tts_speaker=tts_speaker,
        tts_instruct=tts_instruct,
        tts_language=tts_language,
        tts_voice_design=tts_voice_design,
        filter_base_url=filter_base_url,
        filter_model=filter_model,
        filter_prompt=filter_prompt,
        filter_enabled=filter_enabled,
        raw=raw,
    )


def update_config_file(
    path: Path,
    updates: dict[str, Any],
    *,
    seed: dict[str, Any] | None = None,
) -> None:
    """Merge ``updates`` into the on-disk JSON config, preserving every other key.

    Used by `/save-config` (see daemon.py) so `defaultAgent` / `ttsLanguage` /
    `ttsVoiceDesign` / `filterEnabled` / `filterPrompt` changes become this
    config file's new defaults going forward -- not just a per-session
    override in session.json. Writes atomically (temp file + rename), same
    pattern as session.py.

    ``path`` is always the *default* config path (``~/.config/oc-interactive/
    oc-interactive.json``, see ``paths.default_config_path``) -- never
    whichever file was passed via `-c`/`--config`, which oc-interactive must
    never write to. If that default file doesn't exist yet, it's created
    from a copy of ``seed`` (the currently active config's raw contents)
    with ``updates`` applied on top, so the result is an immediately usable
    standalone config rather than a partial one missing required keys like
    `openclawBaseURL`/`openclawToken`. Without a ``seed``, a missing file is
    an error.
    """
    if path.exists():
        with path.open(encoding="utf-8") as f:
            raw: dict[str, Any] = json.load(f)
    elif seed is not None:
        raw = dict(seed)
    else:
        raise FileNotFoundError(f"oc-interactive config not found: {path}")
    raw.update(updates)

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(raw, indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
