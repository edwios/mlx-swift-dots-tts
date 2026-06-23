"""Background daemon: OpenClaw + TTS orchestration."""

from __future__ import annotations

import json
import os
import signal
import socket
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from oc_interactive.client import CONNECT_TIMEOUT_SEC, IDLE_TIMEOUT_SEC
from oc_interactive.config import OpenClawConfig, load_config
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
from oc_interactive.speakable import agent_error_line, ensure_utf8, tag_for_tts
from oc_interactive.tts import TTSError, synthesize_and_play


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
    print("[oc-interactive-daemon] ready", file=sys.stderr, flush=True)

    try:
        while True:
            if time.monotonic() - last_activity > IDLE_TIMEOUT_SEC:
                print(
                    "[oc-interactive-daemon] idle timeout, shutting down",
                    file=sys.stderr,
                    flush=True,
                )
                break
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            last_activity = time.monotonic()
            with conn:
                conn.settimeout(CONNECT_TIMEOUT_SEC)
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
    _process_request(request)
    return {"ok": True}


def _process_request(req: dict[str, Any]) -> None:
    text = str(req.get("text", ""))
    agent_name = str(req.get("agent", "main"))
    refaudio = str(req.get("refaudio", ""))
    reftext = req.get("reftext")
    if isinstance(reftext, str):
        reftext = reftext.strip() or None
    else:
        reftext = None
    tts_model = str(req.get("ttsModel", ""))
    config_path = Path(str(req.get("openclawConfig", "")))
    dots_tts_bin = Path(str(req.get("dotsTtsBinary", "")))
    token = str(req.get("openclawToken", ""))

    cfg = load_config(config_path)
    if not token:
        token = cfg.token

    agent = cfg.resolve_agent(agent_name)
    openclaw_model = cfg.openclaw_model(agent)

    if not refaudio:
        raise ValueError("refaudio is required")
    if not tts_model:
        raise ValueError("tts model path is required")
    if not dots_tts_bin.exists():
        raise FileNotFoundError(f"dots-tts binary not found: {dots_tts_bin}")

    slash = parse_slash_command(text)
    debug = bool(req.get("debug")) or debug_enabled()
    if slash is not None:
        _handle_slash(
            slash,
            agent=agent,
            refaudio=refaudio,
            reftext=reftext,
            tts_model=tts_model,
            dots_tts_bin=dots_tts_bin,
            debug=debug,
        )
        return

    _handle_chat(
        text,
        cfg=cfg,
        token=token,
        agent=agent,
        openclaw_model=openclaw_model,
        refaudio=refaudio,
        reftext=reftext,
        tts_model=tts_model,
        dots_tts_bin=dots_tts_bin,
        debug=debug,
    )


def _cache_tts_paths(
    session: Session,
    *,
    refaudio: str,
    reftext: str | None,
    tts_model: str,
    agent: str,
    dots_tts_bin: Path,
) -> None:
    session.last_refaudio = refaudio
    session.last_reftext = reftext
    session.last_tts_model = tts_model
    session.last_agent = agent
    session.last_dots_tts = str(dots_tts_bin)
    save_session(session)


def _handle_slash(
    slash,
    *,
    agent: str,
    refaudio: str,
    reftext: str | None,
    tts_model: str,
    dots_tts_bin: Path,
    debug: bool,
) -> None:
    session = load_session()

    if slash.kind == SlashKind.UNKNOWN:
        spoken = agent_error_line(f"unknown command {slash.raw_verb!r}")
        _speak(spoken, refaudio=refaudio, reftext=reftext, tts_model=tts_model, dots_tts_bin=dots_tts_bin, debug=debug)
        raise ValueError(spoken)

    if slash.kind == SlashKind.NEW_SESSION:
        session = new_session(keep_system_prompt=True)
        _cache_tts_paths(
            session,
            refaudio=refaudio,
            reftext=reftext,
            tts_model=tts_model,
            agent=agent,
            dots_tts_bin=dots_tts_bin,
        )
        spoken = confirmation_text(slash)
        print(f"[oc-interactive] new session {session.user_id}", file=sys.stderr)
        _speak(spoken, refaudio=refaudio, reftext=reftext, tts_model=tts_model, dots_tts_bin=dots_tts_bin, debug=debug)
        return

    if slash.kind == SlashKind.SET_SYSTEM_PROMPT:
        session.system_prompt = slash.value or None
        _cache_tts_paths(
            session,
            refaudio=refaudio,
            reftext=reftext,
            tts_model=tts_model,
            agent=agent,
            dots_tts_bin=dots_tts_bin,
        )
        spoken = confirmation_text(slash)
        print(
            f"[oc-interactive] system prompt {'set' if slash.value else 'cleared'}",
            file=sys.stderr,
        )
        _speak(spoken, refaudio=refaudio, reftext=reftext, tts_model=tts_model, dots_tts_bin=dots_tts_bin, debug=debug)
        return

    if slash.kind == SlashKind.HELP:
        _cache_tts_paths(
            session,
            refaudio=refaudio,
            reftext=reftext,
            tts_model=tts_model,
            agent=agent,
            dots_tts_bin=dots_tts_bin,
        )
        spoken = confirmation_text(slash)
        _speak(spoken, refaudio=refaudio, reftext=reftext, tts_model=tts_model, dots_tts_bin=dots_tts_bin, debug=debug)
        return

    if slash.kind == SlashKind.STATUS:
        prompt_set = "set" if session.system_prompt else "not set"
        count = len(session.messages)
        spoken = (
            f"Session active. Agent {agent}. "
            f"{count} messages. System prompt is {prompt_set}."
        )
        print(
            f"[oc-interactive] session={session.user_id} agent={agent} "
            f"messages={count} system_prompt={prompt_set}",
            file=sys.stderr,
        )
        _cache_tts_paths(
            session,
            refaudio=refaudio,
            reftext=reftext,
            tts_model=tts_model,
            agent=agent,
            dots_tts_bin=dots_tts_bin,
        )
        _speak(spoken, refaudio=refaudio, reftext=reftext, tts_model=tts_model, dots_tts_bin=dots_tts_bin, debug=debug)
        return

    raise ValueError(f"unhandled slash command: {slash.kind}")


def _handle_chat(
    text: str,
    *,
    cfg: OpenClawConfig,
    token: str,
    agent: str,
    openclaw_model: str,
    refaudio: str,
    reftext: str | None,
    tts_model: str,
    dots_tts_bin: Path,
    debug: bool,
) -> None:
    session = load_session()
    session.last_refaudio = refaudio
    session.last_reftext = reftext
    session.last_tts_model = tts_model
    session.last_agent = agent
    session.last_dots_tts = str(dots_tts_bin)

    api_messages = build_api_messages(session, text)

    spoken_raw: str
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
        spoken_raw = agent_error_line(str(e))
        print(f"[oc-interactive] openclaw error: {e}", file=sys.stderr)
    except Exception as e:
        spoken_raw = agent_error_line(str(e))
        print(f"[oc-interactive] chat error: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
    openclaw_ms = (time.monotonic() - openclaw_start) * 1000
    if debug:
        print(f"[oc-interactive] openclawMs={openclaw_ms:.0f}", file=sys.stderr)

    append_user_message(session, text)
    append_assistant_message(session, spoken_raw, agent=agent)
    save_session(session)

    spoken = tag_for_tts(spoken_raw)
    print(f"[oc-interactive] speaking: {spoken[:80]}…", file=sys.stderr)
    _speak(
        spoken,
        refaudio=refaudio,
        reftext=reftext,
        tts_model=tts_model,
        dots_tts_bin=dots_tts_bin,
        debug=debug,
    )


def _speak(
    text: str,
    *,
    refaudio: str,
    reftext: str | None,
    tts_model: str,
    dots_tts_bin: Path,
    debug: bool,
) -> None:
    try:
        synthesize_and_play(
            text,
            refaudio=refaudio,
            reftext=reftext,
            model=tts_model,
            dots_tts_bin=dots_tts_bin,
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
