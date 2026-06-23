"""State directory and default config paths."""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_CONFIG_NAME = "openclaw.json"
DAEMON_SOCK = "daemon.sock"
DAEMON_PID = "daemon.pid"
DAEMON_LOG = "daemon.log"
SESSION_FILE = "session.json"


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


def ensure_state_dir() -> Path:
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d
