"""Shared per-turn resolution: voice settings, config path, and daemon payload.

Used by both the one-shot CLI (``cli.py``) and the long-running chat app
(``chat_app.py``) so the two entry points resolve CLI/session/config
precedence identically.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from oc_interactive.config import OpenClawConfig
from oc_interactive.io import CliError
from oc_interactive.paths import default_config_path, debug_enabled, normalize_agent_name
from oc_interactive.session import Session, load_active_agent, load_session
from oc_interactive.tts_defaults import (
    DEFAULT_LANGUAGE,
    DEFAULT_MODE,
    DEFAULT_MODEL_BY_MODE,
    DEFAULT_SPEAKER,
    DEFAULT_TTS_MODEL,
    MODE_CLONE,
    MODE_CUSTOM_VOICE,
    MODE_VOICE_DESIGN,
)


@dataclass(frozen=True)
class VoiceSettings:
    mode: str
    tts_model: str
    language: str
    speaker: str | None = None
    instruct: str | None = None
    voice_design: str | None = None
    refaudio: str | None = None
    reftext: str | None = None


def voice_from_session(session: Session) -> VoiceSettings:
    """Rebuild ``VoiceSettings`` straight from cached session fields.

    ``resolve_voice`` is only correct for resolving the *first* turn of a
    process (CLI flags vs. session cache vs. config). Long-running clients
    (the chat UI) must not keep reusing that first resolution forever:
    slash commands like ``/voice-design`` change the voice server-side and
    persist the result via the daemon's ``_cache_tts_paths``, so after any
    successful turn the session file — not a stale in-memory snapshot — is
    the source of truth for what to send next.
    """
    return VoiceSettings(
        mode=session.last_voice_mode or DEFAULT_MODE,
        tts_model=session.last_tts_model or DEFAULT_TTS_MODEL,
        language=session.last_language or DEFAULT_LANGUAGE,
        speaker=session.last_speaker,
        instruct=session.last_instruct,
        voice_design=session.last_voice_design,
        refaudio=session.last_refaudio,
        reftext=session.last_reftext,
    )



def resolve_model_string(model: str) -> str:
    """Keep HF repo ids as-is; resolve local paths."""
    raw = model.strip()
    if not raw:
        return DEFAULT_TTS_MODEL
    looks_local = (
        raw.startswith(("/", "./", "../", "~"))
        or Path(raw).expanduser().exists()
    )
    if looks_local:
        return str(Path(raw).expanduser().resolve())
    return raw


def is_mlx_audio_compatible_model(model: str) -> bool:
    """False for legacy dots.tts paths/checkpoints that mlx-audio cannot load."""
    raw = (model or "").strip()
    if not raw:
        return False
    lower = raw.lower().replace("\\", "/")
    if "dots.tts" in lower or "dots_tts" in lower or "/dots.tts-" in lower:
        return False

    path = Path(raw).expanduser()
    if not path.exists() or not path.is_dir():
        # HF repo ids and missing paths: assume OK (download / fail later).
        return True

    for candidate in (
        path / "config.json",
        path / "backbone" / "config.json",
        path / "model_index.json",
    ):
        if not candidate.is_file():
            continue
        try:
            import json

            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        model_type = str(
            data.get("model_type")
            or data.get("model_type".upper())
            or data.get("architectures", [""])[0]
            or ""
        ).lower()
        if "dots" in model_type:
            return False
    return True


def resolve_tts_model(args: argparse.Namespace, cfg: OpenClawConfig, session: Session) -> str:
    """CLI > compatible session cache > config > built-in default."""
    if args.model:
        return resolve_model_string(args.model)

    cached = session.last_tts_model
    if cached and is_mlx_audio_compatible_model(cached):
        return cached

    return cfg.tts_model or DEFAULT_TTS_MODEL


def infer_qwen_model_kind(model: str) -> str | None:
    """Return MODE_* when the id/path names a known Qwen3-TTS variant."""
    lower = (model or "").lower().replace("\\", "/")
    if "voicedesign" in lower or "voice-design" in lower or "voice_design" in lower:
        return MODE_VOICE_DESIGN
    if "customvoice" in lower or "custom-voice" in lower or "custom_voice" in lower:
        return MODE_CUSTOM_VOICE
    # Base clone models: require "base" but not the other variant names.
    if "base" in lower and "qwen3-tts" in lower:
        return MODE_CLONE
    if "/base" in lower or lower.endswith("-base") or lower.endswith("-base-8bit") or lower.endswith("-base-bf16"):
        return MODE_CLONE
    return None


def ensure_model_matches_mode(
    model: str,
    mode: str,
    *,
    explicit_cli_model: bool,
) -> str:
    """If mode and checkpoint disagree, pick the default HF id for that mode.

    Explicit ``-m`` is left alone (caller gets a clear mlx-audio error). Otherwise
    mlx-audio will download the matching VoiceDesign / CustomVoice / Base weights.
    """
    kind = infer_qwen_model_kind(model)
    if kind == mode:
        return model
    if explicit_cli_model:
        return model
    default = DEFAULT_MODEL_BY_MODE.get(mode)
    if not default:
        return model
    if kind is None or kind != mode:
        return default
    return model


def resolve_voice(args: argparse.Namespace, cfg: OpenClawConfig, session: Session) -> VoiceSettings:
    """Resolve voice settings from CLI flags, ``session`` (the resolved
    agent's own cache), and config, in that precedence order.

    ``session`` must already be the session for the agent this turn is
    talking to -- voice caching is per-agent, so the caller resolves the
    agent and loads its session before calling this.
    """
    cfg_voice_design = getattr(cfg, "tts_voice_design", None)

    # Mode: explicit CLI flags win; otherwise config VoiceDesign beats session
    # (ttsVoiceDesign takes priority over ttsSpeaker / cached custom_voice).
    if args.voice_design:
        mode = MODE_VOICE_DESIGN
    elif args.refaudio:
        mode = MODE_CLONE
    elif args.speaker is not None or args.instruct is not None:
        mode = MODE_CUSTOM_VOICE
    elif cfg_voice_design:
        mode = MODE_VOICE_DESIGN
    elif session.last_voice_mode:
        mode = session.last_voice_mode
    else:
        mode = DEFAULT_MODE

    resolved = resolve_tts_model(args, cfg, session)
    tts_model = ensure_model_matches_mode(
        resolved,
        mode,
        explicit_cli_model=bool(args.model),
    )
    if tts_model != resolved:
        from oc_interactive.io import eprint

        eprint(
            f"[oc-interactive] mode={mode} needs a matching checkpoint; "
            f"using {tts_model} (downloaded on first use if missing)"
        )

    if args.language:
        language = args.language
    elif session.last_language:
        language = session.last_language
    else:
        language = getattr(cfg, "tts_language", None) or DEFAULT_LANGUAGE

    speaker: str | None = None
    instruct: str | None = None
    voice_design: str | None = None
    refaudio: str | None = None
    reftext: str | None = None

    if mode == MODE_VOICE_DESIGN:
        if args.voice_design:
            voice_design = args.voice_design.strip()
        elif session.last_voice_design:
            voice_design = session.last_voice_design
        elif cfg_voice_design:
            voice_design = cfg_voice_design
        else:
            raise CliError(
                "--voice-design or config ttsVoiceDesign is required for VoiceDesign mode"
            )
        if not voice_design:
            raise CliError("voice design description must not be empty")
        instruct = voice_design

    elif mode == MODE_CLONE:
        if args.refaudio:
            refaudio = str(Path(args.refaudio).expanduser().resolve())
            reftext_arg = (args.reftext or "").strip()
            if not reftext_arg:
                raise CliError("--reftext is required when --refaudio is provided")
            reftext = reftext_arg
        elif session.last_refaudio:
            refaudio = session.last_refaudio
            reftext = session.last_reftext
        else:
            raise CliError(
                "--refaudio is required for clone mode (no cached reference audio)"
            )
        if not reftext:
            raise CliError("reftext is required for clone mode")

    else:
        mode = MODE_CUSTOM_VOICE
        if args.speaker is not None:
            speaker = args.speaker.strip() or None
        elif session.last_speaker:
            speaker = session.last_speaker
        else:
            speaker = cfg.tts_speaker or DEFAULT_SPEAKER
        if not speaker:
            speaker = DEFAULT_SPEAKER

        if args.instruct is not None:
            instruct = args.instruct.strip() or None
        elif session.last_instruct:
            instruct = session.last_instruct
        else:
            cfg_instruct = getattr(cfg, "tts_instruct", None)
            instruct = cfg_instruct if isinstance(cfg_instruct, str) else None
            if instruct is not None:
                instruct = instruct.strip() or None

    return VoiceSettings(
        mode=mode,
        tts_model=tts_model,
        language=language,
        speaker=speaker,
        instruct=instruct,
        voice_design=voice_design,
        refaudio=refaudio,
        reftext=reftext,
    )


def resolve_config_path(args: argparse.Namespace) -> Path:
    """Resolve the gateway config path: -c/--config wins, else whichever
    agent's session has a cached path (the one named by --agent if given,
    else the last active agent), else the built-in default.

    This runs before a config is loaded (it's how a config gets loaded in
    the first place), so the candidate agent name is only normalized, never
    validated against a config's agent list.
    """
    if args.openclaw_config:
        return Path(args.openclaw_config).expanduser().resolve()
    candidate = normalize_agent_name(getattr(args, "agent", None)) or load_active_agent()
    if candidate:
        session = load_session(candidate)
        if session.last_openclaw_config:
            return Path(session.last_openclaw_config)
    return default_config_path()


def build_payload(
    text: str,
    *,
    voice: VoiceSettings,
    agent: str,
    config_path: Path,
    token: str,
    debug: bool,
    quiet: bool,
    extra_help_commands: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "text": text,
        "ttsModel": voice.tts_model,
        "mode": voice.mode,
        "language": voice.language,
        "speaker": voice.speaker,
        "instruct": voice.instruct,
        "voiceDesign": voice.voice_design,
        "refaudio": voice.refaudio,
        "reftext": voice.reftext,
        "agent": agent,
        "openclawConfig": str(config_path),
        "openclawToken": token,
        "debug": debug_enabled(debug),
        "quiet": bool(quiet),
        "extraHelpCommands": list(extra_help_commands or []),
    }
