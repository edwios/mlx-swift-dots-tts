"""Background daemon: OpenClaw + TTS orchestration."""

from __future__ import annotations

import json
import os
import signal
import socket
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from oc_interactive.client import IDLE_TIMEOUT_SEC, REQUEST_TIMEOUT_SEC
from oc_interactive.config import OpenClawConfig, load_config, update_config_file
from oc_interactive.io import eprint
from oc_interactive.llm_filter import FilterError, filter_text
from oc_interactive.openclaw import OpenClawError, chat_completion
from oc_interactive.paths import (
    daemon_pid_path,
    daemon_sock_path,
    debug_enabled,
    ensure_state_dir,
)
from oc_interactive.session import (
    Session,
    append_assistant_message,
    append_user_message,
    archive_and_new_session,
    build_api_messages,
    load_session,
    save_session,
)
from oc_interactive.slash import (
    SlashKind,
    confirmation_text,
    parse_slash_command,
)
from oc_interactive.speakable import agent_error_line, ensure_utf8, select_tts_text
from oc_interactive.tts import TTSError, synthesize_and_play
from oc_interactive.tts_defaults import (
    DEFAULT_LANGUAGE,
    DEFAULT_MODE,
    DEFAULT_SPEAKER,
    MODE_CLONE,
    MODE_CUSTOM_VOICE,
    MODE_VOICE_DESIGN,
)
from oc_interactive.turn import ensure_model_matches_mode


@dataclass
class RequestResult:
    reply: str | None = None
    error: str | None = None
    tts_text: str | None = None
    # Set only by bare `/voice-design`: the chat UI prefills its input box
    # with this text so the current description is easy to tweak.
    voice_design_prefill: str | None = None
    # Set only by bare `/filter`: the chat UI prefills its input box with
    # `/filter <description>` so the current description is easy to tweak.
    filter_prefill: str | None = None


@dataclass(frozen=True)
class FilterContext:
    """Local LLM text-filter settings for one turn (see llm_filter.py)."""

    enabled: bool
    base_url: str
    model: str
    prompt: str | None


def _effective_filter_enabled(cfg: OpenClawConfig, session: Session) -> bool:
    """Session override (explicit /filter on|off) wins; else config default."""
    if session.filter_enabled is not None:
        return session.filter_enabled
    return cfg.filter_enabled


def _effective_filter_prompt(cfg: OpenClawConfig, session: Session) -> str | None:
    """Session override (explicit /filter <description>) wins; else config default."""
    return session.filter_prompt if session.filter_prompt else cfg.filter_prompt




@dataclass(frozen=True)
class VoiceContext:
    mode: str
    tts_model: str
    language: str
    speaker: str | None = None
    instruct: str | None = None
    voice_design: str | None = None
    refaudio: str | None = None
    reftext: str | None = None


def run_daemon() -> int:
    ensure_state_dir()
    sock_path = daemon_sock_path()
    pid_path = daemon_pid_path()

    if sock_path.exists():
        sock_path.unlink()

    pid_path.write_text(str(os.getpid()), encoding="utf-8")

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(5)
    server.settimeout(1.0)

    last_activity = time.monotonic()
    eprint("[oc-interactive-daemon] ready", flush=True)

    try:
        while True:
            if time.monotonic() - last_activity > IDLE_TIMEOUT_SEC:
                eprint("[oc-interactive-daemon] idle timeout, shutting down", flush=True)
                break
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            last_activity = time.monotonic()
            with conn:
                conn.settimeout(REQUEST_TIMEOUT_SEC)
                try:
                    response = _handle_connection(conn)
                except Exception as e:
                    response = {"ok": False, "error": str(e)}
                _send_json(conn, response)
    finally:
        server.close()
        sock_path.unlink(missing_ok=True)
        pid_path.unlink(missing_ok=True)

    return 0


def _recv_exact(conn: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("client closed connection")
        buf.extend(chunk)
    return bytes(buf)


def _send_json(conn: socket.socket, payload: dict[str, Any]) -> None:
    data = json.dumps(payload).encode("utf-8")
    try:
        conn.sendall(len(data).to_bytes(4, "big") + data)
    except BrokenPipeError:
        pass


def _handle_connection(conn: socket.socket) -> dict[str, Any]:
    header = _recv_exact(conn, 4)
    length = int.from_bytes(header, "big")
    body = _recv_exact(conn, length)
    request = json.loads(body.decode("utf-8"))
    result = _process_request(request, conn=conn)
    response: dict[str, Any] = {"ok": True}
    if result.reply is not None:
        response["reply"] = result.reply
    if result.error is not None:
        response["error"] = result.error
    # ttsText is streamed before playback; include on final for older clients.
    if result.tts_text is not None:
        response["ttsText"] = result.tts_text
    if result.voice_design_prefill is not None:
        response["voiceDesignPrefill"] = result.voice_design_prefill
    if result.filter_prefill is not None:
        response["filterPrefill"] = result.filter_prefill
    return response


def _emit_tts_text(
    conn: socket.socket | None,
    text: str,
    *,
    is_error: bool = False,
) -> None:
    """Send spoken text to the client before synthesis/playback starts."""
    if conn is None:
        return
    _send_json(
        conn,
        {
            "event": "ttsText",
            "ttsText": text,
            "isError": bool(is_error),
        },
    )


def _parse_voice(req: dict[str, Any]) -> VoiceContext:
    mode = str(req.get("mode") or DEFAULT_MODE).strip().lower()
    tts_model = str(req.get("ttsModel") or "").strip()
    if not tts_model:
        raise ValueError("ttsModel is required")

    language = str(req.get("language") or DEFAULT_LANGUAGE).strip() or DEFAULT_LANGUAGE

    speaker = req.get("speaker")
    if isinstance(speaker, str):
        speaker = speaker.strip() or None
    else:
        speaker = None

    instruct = req.get("instruct")
    if isinstance(instruct, str):
        instruct = instruct.strip() or None
    else:
        instruct = None

    voice_design = req.get("voiceDesign")
    if isinstance(voice_design, str):
        voice_design = voice_design.strip() or None
    else:
        voice_design = None

    refaudio = req.get("refaudio")
    if isinstance(refaudio, str):
        refaudio = refaudio.strip() or None
    else:
        refaudio = None

    reftext = req.get("reftext")
    if isinstance(reftext, str):
        reftext = reftext.strip() or None
    else:
        reftext = None

    if mode == MODE_VOICE_DESIGN:
        if not (voice_design or instruct):
            raise ValueError("voiceDesign / instruct is required for voice_design mode")
        instruct = voice_design or instruct
        voice_design = instruct
    elif mode == MODE_CLONE:
        if not refaudio:
            raise ValueError("refaudio is required for clone mode")
        if not reftext:
            raise ValueError("reftext is required for clone mode")
    else:
        mode = MODE_CUSTOM_VOICE
        if not speaker:
            speaker = DEFAULT_SPEAKER

    return VoiceContext(
        mode=mode,
        tts_model=tts_model,
        language=language,
        speaker=speaker,
        instruct=instruct,
        voice_design=voice_design,
        refaudio=refaudio,
        reftext=reftext,
    )


def _process_request(
    req: dict[str, Any],
    *,
    conn: socket.socket | None = None,
) -> RequestResult:
    text = str(req.get("text", ""))
    agent_name = str(req.get("agent", "main"))
    config_path = Path(str(req.get("openclawConfig", "")))
    token = str(req.get("openclawToken", ""))

    cfg = load_config(config_path)
    if not token:
        token = cfg.token

    agent = cfg.resolve_agent(agent_name)
    openclaw_model = cfg.openclaw_model(agent)
    voice = _parse_voice(req)

    slash = parse_slash_command(text)
    debug = bool(req.get("debug")) or debug_enabled()
    quiet = bool(req.get("quiet"))
    extra_help_commands_raw = req.get("extraHelpCommands") or []
    extra_help_commands = [str(c) for c in extra_help_commands_raw if str(c).strip()]
    if slash is not None:
        return _handle_slash(
            slash,
            cfg=cfg,
            agent=agent,
            openclaw_config=str(config_path),
            voice=voice,
            debug=debug,
            quiet=quiet,
            conn=conn,
            extra_help_commands=extra_help_commands,
        )

    return _handle_chat(
        text,
        cfg=cfg,
        token=token,
        agent=agent,
        openclaw_model=openclaw_model,
        openclaw_config=str(config_path),
        voice=voice,
        debug=debug,
        quiet=quiet,
        conn=conn,
    )


def _cache_tts_paths(
    session: Session,
    *,
    openclaw_config: str,
    voice: VoiceContext,
    agent: str,
) -> None:
    session.last_openclaw_config = openclaw_config
    session.last_tts_model = voice.tts_model
    session.last_voice_mode = voice.mode
    session.last_language = voice.language
    session.last_speaker = voice.speaker
    session.last_instruct = voice.instruct
    session.last_voice_design = voice.voice_design
    session.last_refaudio = voice.refaudio
    session.last_reftext = voice.reftext
    session.last_agent = agent
    save_session(session)


def _handle_slash(
    slash,
    *,
    cfg: OpenClawConfig,
    agent: str,
    openclaw_config: str,
    voice: VoiceContext,
    debug: bool,
    quiet: bool,
    conn: socket.socket | None = None,
    extra_help_commands: list[str] | None = None,
) -> RequestResult:
    session = load_session()

    if slash.kind == SlashKind.UNKNOWN:
        spoken = agent_error_line(f"unknown command {slash.raw_verb!r}")
        _speak(spoken, voice=voice, debug=debug, quiet=quiet, conn=conn, is_error=True)
        raise ValueError(spoken)

    if slash.kind == SlashKind.NEW_SESSION:
        session, archived = archive_and_new_session(keep_system_prompt=True)
        _cache_tts_paths(
            session,
            openclaw_config=openclaw_config,
            voice=voice,
            agent=agent,
        )
        spoken = confirmation_text(slash)
        if archived:
            eprint(f"[oc-interactive] archived session → {archived}")
        eprint(f"[oc-interactive] new session {session.user_id}")
        _speak(spoken, voice=voice, debug=debug, quiet=quiet, conn=conn)
        return RequestResult(tts_text=spoken)

    if slash.kind == SlashKind.SET_SYSTEM_PROMPT:
        session.system_prompt = slash.value or None
        _cache_tts_paths(
            session,
            openclaw_config=openclaw_config,
            voice=voice,
            agent=agent,
        )
        spoken = confirmation_text(slash)
        eprint(
            f"[oc-interactive] system prompt {'set' if slash.value else 'cleared'}"
        )
        _speak(spoken, voice=voice, debug=debug, quiet=quiet, conn=conn)
        return RequestResult(tts_text=spoken)

    if slash.kind == SlashKind.SET_VOICE_DESIGN:
        description = slash.value.strip()
        lower = description.lower()

        if not description:
            # Bare: cite the current description back, no state change.
            if voice.mode == MODE_VOICE_DESIGN and voice.voice_design:
                current = voice.voice_design
                spoken = f"Current voice design: {current}"
                prefill = f"/voice-design {current}"
            else:
                fallback_speaker = voice.speaker or DEFAULT_SPEAKER
                spoken = (
                    "No voice design is currently set; using "
                    f"{fallback_speaker}, CustomVoice."
                )
                prefill = "/voice-design "
            _cache_tts_paths(
                session,
                openclaw_config=openclaw_config,
                voice=voice,
                agent=agent,
            )
            _speak(spoken, voice=voice, debug=debug, quiet=quiet, conn=conn)
            return RequestResult(tts_text=spoken, voice_design_prefill=prefill)

        if lower in ("reset", "default"):
            cfg_design = cfg.tts_voice_design
            if cfg_design:
                model = ensure_model_matches_mode(
                    voice.tts_model, MODE_VOICE_DESIGN, explicit_cli_model=False
                )
                new_voice = VoiceContext(
                    mode=MODE_VOICE_DESIGN,
                    tts_model=model,
                    language=voice.language,
                    speaker=None,
                    instruct=cfg_design,
                    voice_design=cfg_design,
                    refaudio=None,
                    reftext=None,
                )
                spoken = f"Voice design reset to config default: {cfg_design}"
                eprint(f"[oc-interactive] voice design reset to config default: {cfg_design!r}")
            else:
                speaker = cfg.tts_speaker or DEFAULT_SPEAKER
                instruct = cfg.tts_instruct
                model = ensure_model_matches_mode(
                    voice.tts_model, MODE_CUSTOM_VOICE, explicit_cli_model=False
                )
                new_voice = VoiceContext(
                    mode=MODE_CUSTOM_VOICE,
                    tts_model=model,
                    language=voice.language,
                    speaker=speaker,
                    instruct=instruct,
                    voice_design=None,
                    refaudio=None,
                    reftext=None,
                )
                spoken = f"No config voice design; reset to {speaker}, CustomVoice."
                eprint(
                    "[oc-interactive] voice design reset; no config default, "
                    "reverted to CustomVoice"
                )
            _cache_tts_paths(
                session,
                openclaw_config=openclaw_config,
                voice=new_voice,
                agent=agent,
            )
            _speak(spoken, voice=new_voice, debug=debug, quiet=quiet, conn=conn)
            return RequestResult(tts_text=spoken)

        # Normal set: switch to VoiceDesign mode with this description.
        model = ensure_model_matches_mode(
            voice.tts_model, MODE_VOICE_DESIGN, explicit_cli_model=False
        )
        new_voice = VoiceContext(
            mode=MODE_VOICE_DESIGN,
            tts_model=model,
            language=voice.language,
            speaker=None,
            instruct=description,
            voice_design=description,
            refaudio=None,
            reftext=None,
        )
        spoken = "Voice design updated."
        eprint(f"[oc-interactive] voice design set: {description!r}")
        _cache_tts_paths(
            session,
            openclaw_config=openclaw_config,
            voice=new_voice,
            agent=agent,
        )
        _speak(spoken, voice=new_voice, debug=debug, quiet=quiet, conn=conn)
        return RequestResult(tts_text=spoken)

    if slash.kind == SlashKind.SET_FILTER:
        description = slash.value.strip()
        lower = description.lower()

        if not description:
            # Bare `/filter`: report on/off state + current description,
            # no state change. Prefill lets the user tweak the description
            # without retyping it (mirrors bare `/voice-design`). Uses the
            # *effective* value (session override, else config default) so
            # this reflects what will actually be used, not just what this
            # session has explicitly overridden.
            effective_enabled = _effective_filter_enabled(cfg, session)
            effective_prompt = _effective_filter_prompt(cfg, session)
            state = "on" if effective_enabled else "off"
            if effective_prompt:
                spoken = f"Filter is {state}. Description: {effective_prompt}"
                # The chat UI's input box is single-line, so real newlines in
                # the stored description are re-escaped to literal "\n" here
                # (the inverse of the unescaping below) so the prefilled text
                # round-trips through the box intact instead of being cut at
                # the first line.
                prefill_desc = effective_prompt.replace("\n", "\\n")
                prefill = f"/filter {prefill_desc}"
            else:
                spoken = f"Filter is {state}. No description set."
                prefill = "/filter "
            _cache_tts_paths(
                session,
                openclaw_config=openclaw_config,
                voice=voice,
                agent=agent,
            )
            _speak(spoken, voice=voice, debug=debug, quiet=quiet, conn=conn)
            return RequestResult(tts_text=spoken, filter_prefill=prefill)

        if lower in ("on", "off"):
            session.filter_enabled = lower == "on"
            spoken = f"Filter {lower}."
            eprint(
                f"[oc-interactive] filter {'enabled' if session.filter_enabled else 'disabled'}"
            )
            _cache_tts_paths(
                session,
                openclaw_config=openclaw_config,
                voice=voice,
                agent=agent,
            )
            _speak(spoken, voice=voice, debug=debug, quiet=quiet, conn=conn)
            return RequestResult(tts_text=spoken)

        # Anything else: set the description. On/off state is independent
        # and unchanged (use `/filter on` / `/filter off` to toggle).
        # The chat UI (run-chat) input box is single-line and can't take a
        # real line break, so a literal "\n" (backslash + n) typed there is
        # treated as a genuine line break — not stored/escaped as "\n" text.
        description = description.replace("\\n", "\n")
        session.filter_prompt = description
        spoken = "Filter description updated."
        eprint(f"[oc-interactive] filter description set: {description!r}")
        _cache_tts_paths(
            session,
            openclaw_config=openclaw_config,
            voice=voice,
            agent=agent,
        )
        _speak(spoken, voice=voice, debug=debug, quiet=quiet, conn=conn)
        return RequestResult(tts_text=spoken)

    if slash.kind == SlashKind.SAVE_CONFIG:
        # Snapshot the current effective settings into the active config
        # file (whichever oc-interactive.json / *.conf this session is
        # using), so they become that config's new defaults going forward
        # -- an explicit, on-demand version of what /filter used to do
        # automatically. Deliberately narrow in scope: agent, language,
        # voice design (only if that's the mode in use), and the filter.
        # Speaker/instruct/model/clone settings are left untouched for now.
        effective_enabled = _effective_filter_enabled(cfg, session)
        effective_prompt = _effective_filter_prompt(cfg, session)

        updates: dict[str, Any] = {
            "defaultAgent": agent,
            "ttsLanguage": voice.language,
            "filterEnabled": effective_enabled,
            "filterPrompt": effective_prompt,
        }
        saved_voice_design = voice.mode == MODE_VOICE_DESIGN and bool(voice.voice_design)
        if saved_voice_design:
            updates["ttsVoiceDesign"] = voice.voice_design

        try:
            update_config_file(Path(openclaw_config), updates)
        except (OSError, ValueError) as e:
            spoken = agent_error_line(f"failed to save config, {e}")
            eprint(f"[oc-interactive] /save-config failed for {openclaw_config}: {e}")
            _cache_tts_paths(
                session,
                openclaw_config=openclaw_config,
                voice=voice,
                agent=agent,
            )
            _speak(spoken, voice=voice, debug=debug, quiet=quiet, conn=conn, is_error=True)
            return RequestResult(error=spoken)

        parts = [f"agent {agent}", f"language {voice.language}"]
        if saved_voice_design:
            parts.append("voice design")
        parts.append(f"filter {'on' if effective_enabled else 'off'}")
        spoken = f"Config saved: {', '.join(parts)}."
        eprint(f"[oc-interactive] config saved to {openclaw_config}: {updates!r}")
        _cache_tts_paths(
            session,
            openclaw_config=openclaw_config,
            voice=voice,
            agent=agent,
        )
        _speak(spoken, voice=voice, debug=debug, quiet=quiet, conn=conn)
        return RequestResult(tts_text=spoken)

    if slash.kind == SlashKind.HELP:
        _cache_tts_paths(
            session,
            openclaw_config=openclaw_config,
            voice=voice,
            agent=agent,
        )
        spoken = confirmation_text(slash, extra_commands=extra_help_commands)
        _speak(spoken, voice=voice, debug=debug, quiet=quiet, conn=conn)
        return RequestResult(tts_text=spoken)

    if slash.kind == SlashKind.STATUS:
        prompt_set = "set" if session.system_prompt else "not set"
        count = len(session.messages)
        spoken = (
            f"Session active. Agent {agent}. "
            f"{count} messages. System prompt is {prompt_set}."
        )
        eprint(
            f"[oc-interactive] session={session.user_id} agent={agent} "
            f"messages={count} system_prompt={prompt_set}"
        )
        _cache_tts_paths(
            session,
            openclaw_config=openclaw_config,
            voice=voice,
            agent=agent,
        )
        _speak(spoken, voice=voice, debug=debug, quiet=quiet, conn=conn)
        return RequestResult(tts_text=spoken)

    raise ValueError(f"unhandled slash command: {slash.kind}")


def _handle_chat(
    text: str,
    *,
    cfg: OpenClawConfig,
    token: str,
    agent: str,
    openclaw_model: str,
    openclaw_config: str,
    voice: VoiceContext,
    debug: bool,
    quiet: bool,
    conn: socket.socket | None = None,
) -> RequestResult:
    session = load_session()
    _cache_tts_paths(
        session,
        openclaw_config=openclaw_config,
        voice=voice,
        agent=agent,
    )

    api_messages = build_api_messages(session, text)

    spoken_raw: str
    openclaw_failed = False
    openclaw_start = time.monotonic()
    try:
        reply = chat_completion(
            base_url=cfg.base_url,
            token=token,
            model=openclaw_model,
            user_id=session.user_id,
            messages=api_messages,
        )
        spoken_raw = ensure_utf8(reply)
    except OpenClawError as e:
        openclaw_failed = True
        spoken_raw = agent_error_line(str(e))
        eprint(f"[oc-interactive] openclaw error: {e}")
    except Exception as e:
        openclaw_failed = True
        spoken_raw = agent_error_line(str(e))
        eprint(f"[oc-interactive] chat error: {e}")
        traceback.print_exc(file=sys.stderr)
    openclaw_ms = (time.monotonic() - openclaw_start) * 1000
    if debug:
        eprint(f"[oc-interactive] openclawMs={openclaw_ms:.0f}")

    append_user_message(session, text)
    append_assistant_message(session, spoken_raw, agent=agent)
    save_session(session)

    filter_ctx = FilterContext(
        enabled=_effective_filter_enabled(cfg, session),
        base_url=cfg.filter_base_url,
        model=cfg.filter_model,
        prompt=_effective_filter_prompt(cfg, session),
    )
    _speak(
        spoken_raw,
        voice=voice,
        debug=debug,
        quiet=quiet,
        conn=conn,
        is_error=openclaw_failed,
        filter_ctx=filter_ctx,
    )
    if openclaw_failed:
        # Agent failures are spoken but reported on stderr only (not stdout).
        return RequestResult(error=spoken_raw)
    return RequestResult(reply=spoken_raw, tts_text=spoken_raw)


def _speak(
    text: str,
    *,
    voice: VoiceContext,
    debug: bool,
    quiet: bool = False,
    conn: socket.socket | None = None,
    is_error: bool = False,
    filter_ctx: FilterContext | None = None,
) -> None:
    _ = quiet  # CLI enforces quiet when printing the streamed ttsText event
    # The full text is always displayed/printed/stored; only the audio
    # synthesis input is trimmed (bracket rule, then optionally the LLM
    # filter). filter_ctx is only passed for real agent replies — slash
    # command confirmations (filter_ctx=None) are never routed through it.
    _emit_tts_text(conn, text, is_error=is_error)
    spoken_text = select_tts_text(text)
    if filter_ctx is not None and filter_ctx.enabled and not is_error:
        try:
            spoken_text = filter_text(
                spoken_text,
                base_url=filter_ctx.base_url,
                model=filter_ctx.model,
                system_prompt=filter_ctx.prompt,
            )
        except FilterError as e:
            eprint(
                f"[oc-interactive] filter error, speaking unfiltered text: {e}"
            )
    try:
        synthesize_and_play(
            spoken_text,
            model=voice.tts_model,
            mode=voice.mode,
            language=voice.language,
            speaker=voice.speaker,
            instruct=voice.instruct,
            refaudio=voice.refaudio,
            reftext=voice.reftext,
            debug=debug,
        )
    except TTSError as e:
        raise RuntimeError(f"TTS failed: {e}") from e


def _shutdown_handler(signum, frame) -> None:
    raise SystemExit(0)


def main() -> int:
    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)
    return run_daemon()
