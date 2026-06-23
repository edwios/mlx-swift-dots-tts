"""State directory and default config paths."""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_CONFIG_NAME = "openclaw.json"
DAEMON_SOCK = "daemon.sock"
DAEMON_PID = "daemon.pid"
DAEMON_LOG = "daemon.log"
SESSION_FILE = "session.json"
TTS_DAEMON_SOCK = "tts-daemon.sock"
TTS_DAEMON_PID = "tts-daemon.pid"
TTS_DAEMON_LOG = "tts-daemon.log"


def state_dir() -> Path:
    override = os.environ.get("OC_INTERACTIVE_STATE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".config" / "oc-interactive"


def default_config_path() -> Path:
    return state_dir() / DEFAULT_CONFIG_NAME


def daemon_sock_path() -> Path:
    return state_dir() / DAEMON_SOCK


def daemon_pid_path() -> Path:
    return state_dir() / DAEMON_PID


def daemon_log_path() -> Path:
    return state_dir() / DAEMON_LOG


def session_path() -> Path:
    return state_dir() / SESSION_FILE


def tts_daemon_sock_path() -> Path:
    return state_dir() / TTS_DAEMON_SOCK


def tts_daemon_pid_path() -> Path:
    return state_dir() / TTS_DAEMON_PID


def tts_daemon_log_path() -> Path:
    return state_dir() / TTS_DAEMON_LOG


def debug_enabled(explicit: bool | None = None) -> bool:
    if explicit is not None:
        return explicit
    return os.environ.get("OC_INTERACTIVE_DEBUG", "").lower() in ("1", "true", "yes")


def ensure_state_dir() -> Path:
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d
