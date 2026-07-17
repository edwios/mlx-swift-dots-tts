"""Persistent Qwen3-TTS daemon client (cached mlx-audio model)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from oc_interactive.ipc import send_json
from oc_interactive.paths import (
    ensure_state_dir,
    tts_daemon_pid_path,
    tts_daemon_sock_path,
)

TTS_STARTUP_TIMEOUT_SEC = 120
TTS_REQUEST_TIMEOUT_SEC: float | None = None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_pid() -> int | None:
    path = tts_daemon_pid_path()
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def _daemon_ready(sock_path: Path, pid: int | None) -> bool:
    """True when the TTS daemon pid is alive and its socket file exists."""
    return bool(pid and _pid_alive(pid) and sock_path.exists())


def _cleanup_stale_tts_daemon() -> None:
    sock = tts_daemon_sock_path()
    pid = _read_pid()
    pid_path = tts_daemon_pid_path()

    if pid and not _pid_alive(pid):
        sock.unlink(missing_ok=True)
        pid_path.unlink(missing_ok=True)
        return

    if sock.exists() and not (pid and _pid_alive(pid)):
        sock.unlink(missing_ok=True)
        pid_path.unlink(missing_ok=True)


def ensure_tts_daemon_running() -> None:
    ensure_state_dir()
    sock = tts_daemon_sock_path()
    pid = _read_pid()
    if _daemon_ready(sock, pid):
        return

    _cleanup_stale_tts_daemon()

    lock_path = ensure_state_dir() / "tts-daemon.lock"
    lock_file = open(lock_path, "w", encoding="utf-8")
    try:
        try:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        except ImportError:
            pass

        pid = _read_pid()
        if _daemon_ready(sock, pid):
            return

        log_path = ensure_state_dir() / "tts-daemon.log"
        env = os.environ.copy()
        env.setdefault("OC_INTERACTIVE_STATE_DIR", str(ensure_state_dir()))
        log_file = open(log_path, "a", encoding="utf-8")
        subprocess.Popen(
            [sys.executable, "-m", "oc_interactive.qwen_tts_daemon"],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
            env=env,
        )
        log_file.close()

        deadline = time.monotonic() + TTS_STARTUP_TIMEOUT_SEC
        while time.monotonic() < deadline:
            if _daemon_ready(sock, _read_pid()):
                return
            time.sleep(0.1)

        raise RuntimeError(
            f"TTS daemon did not become ready within {TTS_STARTUP_TIMEOUT_SEC}s; "
            f"see {log_path}"
        )
    finally:
        try:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except ImportError:
            pass
        lock_file.close()


def synthesize_to_wav(
    *,
    text: str,
    model: str,
    output: Path,
    mode: str,
    language: str = "English",
    speaker: str | None = None,
    instruct: str | None = None,
    refaudio: str | None = None,
    reftext: str | None = None,
    debug: bool = False,
) -> dict[str, Any]:
    ensure_tts_daemon_running()
    payload: dict[str, Any] = {
        "text": text,
        "model": model,
        "mode": mode,
        "language": language,
        "output": str(output),
        "debug": debug,
    }
    if speaker:
        payload["speaker"] = speaker
    if instruct:
        payload["instruct"] = instruct
    if refaudio:
        payload["refaudio"] = refaudio
    if reftext:
        payload["reftext"] = reftext
    return send_json(
        str(tts_daemon_sock_path()),
        payload,
        timeout=TTS_REQUEST_TIMEOUT_SEC,
    )
