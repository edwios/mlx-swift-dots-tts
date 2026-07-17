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
from oc_interactive.config import OpenClawConfig, load_config
from oc_interactive.io import eprint
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
    build_api_messages,
    load_session,
    new_session,
    save_session,
)
from oc_interactive.slash import (
    SlashKind,
    confirmation_text,
    parse_slash_command,
)
from oc_interactive.speakable import agent_error_line, ensure_utf8
from oc_interactive.tts import TTSError, synthesize_and_play
from oc_interactive.tts_defaults import (
    DEFAULT_LANGUAGE,
    DEFAULT_MODE,
    DEFAULT_SPEAKER,
    MODE_CLONE,
    MODE_CUSTOM_VOICE,
    MODE_VOICE_DESIGN,
)


@dataclass
class RequestResult:
    reply: str | None = None
    error: str | None = None


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
    result = _process_request(request)
    response: dict[str, Any] = {"ok": True}
    if result.reply is not None:
        response["reply"] = result.reply
    if result.error is not None:
        response["error"] = result.error
    return response


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


def _process_request(req: dict[str, Any]) -> RequestResult:
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
    if slash is not None:
        _handle_slash(
            slash,
            agent=agent,
            openclaw_config=str(config_path),
            voice=voice,
            debug=debug,
            quiet=quiet,
        )
        return RequestResult()

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
    agent: str,
    openclaw_config: str,
    voice: VoiceContext,
    debug: bool,
    quiet: bool,
) -> None:
    session = load_session()

    if slash.kind == SlashKind.UNKNOWN:
        spoken = agent_error_line(f"unknown command {slash.raw_verb!r}")
        _speak(spoken, voice=voice, debug=debug, quiet=quiet)
        raise ValueError(spoken)

    if slash.kind == SlashKind.NEW_SESSION:
        session = new_session(keep_system_prompt=True)
        _cache_tts_paths(
            session,
            openclaw_config=openclaw_config,
            voice=voice,
            agent=agent,
        )
        spoken = confirmation_text(slash)
        eprint(f"[oc-interactive] new session {session.user_id}")
        _speak(spoken, voice=voice, debug=debug, quiet=quiet)
        return

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
        _speak(spoken, voice=voice, debug=debug, quiet=quiet)
        return

    if slash.kind == SlashKind.HELP:
        _cache_tts_paths(
            session,
            openclaw_config=openclaw_config,
            voice=voice,
            agent=agent,
        )
        spoken = confirmation_text(slash)
        _speak(spoken, voice=voice, debug=debug, quiet=quiet)
        return

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
        _speak(spoken, voice=voice, debug=debug, quiet=quiet)
        return

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

    _speak(spoken_raw, voice=voice, debug=debug, quiet=quiet)
    if openclaw_failed:
        return RequestResult(error=spoken_raw)
    return RequestResult(reply=spoken_raw)


def _speak(
    text: str,
    *,
    voice: VoiceContext,
    debug: bool,
    quiet: bool = False,
) -> None:
    if not quiet:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
        sys.stdout.flush()
    try:
        synthesize_and_play(
            text,
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
