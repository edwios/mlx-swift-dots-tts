"""Manage the local SSH port-forward tunnel declared in an oc-interactive config.

The ``ssh`` block in a config file (see ``eileen.conf`` / ``victoria.conf``) is
optional and purely declarative:

    "ssh": {
      "enabled": true,
      "host": "ello.local",
      "user": "fsoc4",
      "identityFile": "~/.ssh/id_ed25519",
      "localPort": 18789,
      "remoteHost": "127.0.0.1",
      "remotePort": 18789
    }

This module establishes and repairs that tunnel, mirroring the daemon
lifecycle pattern in ``client.py`` (pid file + liveness check + stale
cleanup + spawn + wait-until-ready).
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from oc_interactive.io import eprint
from oc_interactive.paths import ensure_state_dir, ssh_tunnel_log_path, ssh_tunnel_state_path

if TYPE_CHECKING:
    from oc_interactive.config import OpenClawConfig

CONNECT_TIMEOUT_SEC = 0.5
STARTUP_TIMEOUT_SEC = 15.0
TERMINATE_GRACE_SEC = 2.0


@dataclass(frozen=True)
class SSHTunnelSpec:
    enabled: bool
    host: str
    user: str
    identity_file: str | None
    local_port: int
    remote_host: str
    remote_port: int


def load_tunnel_spec(raw: dict[str, Any]) -> SSHTunnelSpec | None:
    """Parse the ``ssh`` block out of an already-loaded config dict.

    Returns ``None`` when the config has no ``ssh`` block at all (a config
    that doesn't use tunneling is a silent no-op).
    """
    ssh_raw = raw.get("ssh")
    if not isinstance(ssh_raw, dict):
        return None

    host = str(ssh_raw.get("host") or "").strip()
    if not host:
        raise ValueError("ssh.host is required when an ssh block is present")

    local_port_raw = ssh_raw.get("localPort")
    remote_port_raw = ssh_raw.get("remotePort")
    if local_port_raw is None or remote_port_raw is None:
        raise ValueError("ssh.localPort and ssh.remotePort are required")
    try:
        local_port = int(local_port_raw)
        remote_port = int(remote_port_raw)
    except (TypeError, ValueError) as e:
        raise ValueError("ssh.localPort / ssh.remotePort must be integers") from e

    identity_file = ssh_raw.get("identityFile")
    if identity_file is not None:
        identity_file = str(identity_file).strip() or None

    user = str(ssh_raw.get("user") or "").strip()
    remote_host = str(ssh_raw.get("remoteHost") or "127.0.0.1").strip() or "127.0.0.1"
    enabled = bool(ssh_raw.get("enabled", True))

    return SSHTunnelSpec(
        enabled=enabled,
        host=host,
        user=user,
        identity_file=identity_file,
        local_port=local_port,
        remote_host=remote_host,
        remote_port=remote_port,
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _port_open(port: int, *, timeout: float = CONNECT_TIMEOUT_SEC) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _read_state(local_port: int) -> dict[str, Any] | None:
    path = ssh_tunnel_state_path(local_port)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _write_state(local_port: int, data: dict[str, Any]) -> None:
    ensure_state_dir()
    ssh_tunnel_state_path(local_port).write_text(json.dumps(data), encoding="utf-8")


def _clear_state(local_port: int) -> None:
    ssh_tunnel_state_path(local_port).unlink(missing_ok=True)


def _matches(spec: SSHTunnelSpec, state: dict[str, Any]) -> bool:
    return (
        state.get("host") == spec.host
        and state.get("user") == spec.user
        and state.get("identityFile") == spec.identity_file
        and state.get("remoteHost") == spec.remote_host
        and state.get("remotePort") == spec.remote_port
    )


def _kill(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + TERMINATE_GRACE_SEC
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def tunnel_status(spec: SSHTunnelSpec) -> tuple[bool, dict[str, Any] | None]:
    """Return (healthy, state). Healthy means our recorded pid is alive, its
    params match the spec, and the local port actually accepts connections."""
    state = _read_state(spec.local_port)
    if not state:
        return False, None
    pid = state.get("pid")
    if not isinstance(pid, int) or not _pid_alive(pid):
        return False, state
    if not _matches(spec, state):
        return False, state
    if not _port_open(spec.local_port):
        return False, state
    return True, state


def teardown_tunnel(spec: SSHTunnelSpec) -> None:
    """Kill the managed tunnel (if any) and clear its state file."""
    state = _read_state(spec.local_port)
    if state:
        pid = state.get("pid")
        if isinstance(pid, int) and _pid_alive(pid):
            _kill(pid)
    _clear_state(spec.local_port)


def _spawn(spec: SSHTunnelSpec) -> int:
    ensure_state_dir()
    cmd = [
        "ssh",
        "-N",
        "-o", "BatchMode=yes",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-o", "StrictHostKeyChecking=accept-new",
    ]
    if spec.identity_file:
        cmd += ["-i", str(Path(spec.identity_file).expanduser())]
    cmd += ["-L", f"{spec.local_port}:{spec.remote_host}:{spec.remote_port}"]
    target = f"{spec.user}@{spec.host}" if spec.user else spec.host
    cmd.append(target)

    log_path = ssh_tunnel_log_path(spec.local_port)
    log_file = open(log_path, "a", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )
    finally:
        log_file.close()
    return proc.pid


def ensure_tunnel(spec: SSHTunnelSpec, *, debug: bool = False) -> bool:
    """Ensure the tunnel is up and matches spec.

    Returns True if a new tunnel was (re)started, False if an already-healthy
    matching tunnel was found. Raises RuntimeError on failure to establish.
    """
    if not spec.enabled:
        if debug:
            eprint(f"[ssh-tunnel] disabled for port {spec.local_port}; skipping")
        return False

    healthy, state = tunnel_status(spec)
    if healthy:
        if debug:
            eprint(
                f"[ssh-tunnel] already up on 127.0.0.1:{spec.local_port} -> "
                f"{spec.user}@{spec.host}:{spec.remote_port}"
            )
        return False

    if state:
        pid = state.get("pid")
        if debug:
            eprint(f"[ssh-tunnel] stale/mismatched tunnel detected (pid={pid}); tearing down")
        teardown_tunnel(spec)
    elif _port_open(spec.local_port):
        raise RuntimeError(
            f"local port {spec.local_port} is already in use by a process oc-interactive "
            "does not manage; free the port or change ssh.localPort"
        )

    pid = _spawn(spec)
    _write_state(
        spec.local_port,
        {
            "host": spec.host,
            "user": spec.user,
            "identityFile": spec.identity_file,
            "remoteHost": spec.remote_host,
            "remotePort": spec.remote_port,
            "localPort": spec.local_port,
            "pid": pid,
            "startedAt": time.time(),
        },
    )

    deadline = time.monotonic() + STARTUP_TIMEOUT_SEC
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            _clear_state(spec.local_port)
            raise RuntimeError(
                f"ssh tunnel process exited immediately; see {ssh_tunnel_log_path(spec.local_port)}"
            )
        if _port_open(spec.local_port):
            if debug:
                eprint(
                    f"[ssh-tunnel] established 127.0.0.1:{spec.local_port} -> "
                    f"{spec.user}@{spec.host}:{spec.remote_port} (pid={pid})"
                )
            return True
        time.sleep(0.2)

    _kill(pid)
    _clear_state(spec.local_port)
    raise RuntimeError(
        f"ssh tunnel did not become ready within {STARTUP_TIMEOUT_SEC:.0f}s; "
        f"see {ssh_tunnel_log_path(spec.local_port)}"
    )


def ensure_ssh_tunnel(cfg: "OpenClawConfig", *, debug: bool = False) -> None:
    """Load the ssh spec from cfg.raw (if any) and ensure it's tunneled."""
    spec = load_tunnel_spec(cfg.raw)
    if spec is None:
        return
    ensure_tunnel(spec, debug=debug)
