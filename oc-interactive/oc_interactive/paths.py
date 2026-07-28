"""State directory and default config paths."""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_CONFIG_NAME = "oc-interactive.json"
DAEMON_SOCK = "daemon.sock"
DAEMON_PID = "daemon.pid"
DAEMON_LOG = "daemon.log"
SESSION_FILE = "session.json"
SESSIONS_DIR = "sessions"
TTS_DAEMON_SOCK = "tts-daemon.sock"
TTS_DAEMON_PID = "tts-daemon.pid"
TTS_DAEMON_LOG = "tts-daemon.log"
AGENTS_DIR = "agents"
ACTIVE_AGENT_FILE = "active_agent.json"


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
    """Legacy pre-per-agent session file location.

    Sessions moved to a per-agent layout (see ``agent_session_path``); this
    path is only consulted once, to migrate an old flat ``session.json``
    into that layout (see ``session._migrate_legacy_session``).
    """
    return state_dir() / SESSION_FILE


def sessions_dir() -> Path:
    """Legacy pre-per-agent archive directory. See ``session_path``."""
    return state_dir() / SESSIONS_DIR


def normalize_agent_name(name: str | None) -> str:
    """Canonicalize an agent name for use as a lookup key / directory slug.

    Mirrors ``OpenClawConfig.resolve_agent``'s normalization (lowercase,
    strip an ``openclaw/`` prefix) without requiring a loaded config, so
    the client can find a cached per-agent session before it knows whether
    the config even lists that agent.
    """
    n = (name or "").strip().lower()
    if n.startswith("openclaw/"):
        n = n[len("openclaw/") :]
    return n


def _agent_slug(name: str) -> str:
    n = normalize_agent_name(name) or "_default"
    return n.replace("/", "_").replace("\\", "_").replace(":", "_")


def agents_root_dir() -> Path:
    return state_dir() / AGENTS_DIR


def agent_dir(agent: str) -> Path:
    return agents_root_dir() / _agent_slug(agent)


def agent_session_path(agent: str) -> Path:
    return agent_dir(agent) / SESSION_FILE


def agent_sessions_archive_dir(agent: str) -> Path:
    return agent_dir(agent) / SESSIONS_DIR


def known_agent_slugs() -> list[str]:
    """Agent directory slugs that currently have a session on disk."""
    root = agents_root_dir()
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def active_agent_path() -> Path:
    return state_dir() / ACTIVE_AGENT_FILE


def tts_daemon_sock_path() -> Path:
    return state_dir() / TTS_DAEMON_SOCK


def tts_daemon_pid_path() -> Path:
    return state_dir() / TTS_DAEMON_PID


def tts_daemon_log_path() -> Path:
    return state_dir() / TTS_DAEMON_LOG


def ssh_tunnel_state_path(local_port: int) -> Path:
    return state_dir() / f"ssh-tunnel-{local_port}.json"


def ssh_tunnel_log_path(local_port: int) -> Path:
    return state_dir() / f"ssh-tunnel-{local_port}.log"


def debug_enabled(explicit: bool | None = None) -> bool:
    if explicit is not None:
        return explicit
    return os.environ.get("OC_INTERACTIVE_DEBUG", "").lower() in ("1", "true", "yes")


def ensure_state_dir() -> Path:
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d
