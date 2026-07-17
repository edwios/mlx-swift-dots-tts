"""IPC client: ensure daemon is running and send requests."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from oc_interactive.paths import (
    daemon_pid_path,
    daemon_sock_path,
    ensure_state_dir,
)

IDLE_TIMEOUT_SEC = 30 * 60
STARTUP_TIMEOUT_SEC = 30
# None = wait indefinitely for OpenClaw + TTS + afplay (override with --timeout).
REQUEST_TIMEOUT_SEC: float | None = None

EventHandler = Callable[[dict[str, Any]], None]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_pid() -> int | None:
    path = daemon_pid_path()
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def _socket_responds(sock_path: Path, timeout: float = 1.0) -> bool:
    if not sock_path.exists():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(sock_path))
            return True
    except OSError:
        return False


def _cleanup_stale_daemon() -> None:
    sock = daemon_sock_path()
    pid = _read_pid()
    if pid and _pid_alive(pid) and not _socket_responds(sock):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        time.sleep(0.2)
    if sock.exists() and not _socket_responds(sock):
        try:
            sock.unlink()
        except OSError:
            pass
    pid_path = daemon_pid_path()
    if pid_path.exists() and not _socket_responds(sock):
        pid_path.unlink(missing_ok=True)


def _spawn_daemon() -> None:
    ensure_state_dir()
    log_path = ensure_state_dir() / "daemon.log"
    log_file = open(log_path, "a", encoding="utf-8")
    env = os.environ.copy()
    env.setdefault("OC_INTERACTIVE_STATE_DIR", str(ensure_state_dir()))
    subprocess.Popen(
        [sys.executable, "-m", "oc_interactive", "--daemon"],
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=log_file,
        start_new_session=True,
        env=env,
    )
    log_file.close()


def ensure_daemon_running() -> None:
    ensure_state_dir()
    sock = daemon_sock_path()
    pid = _read_pid()
    if pid and _pid_alive(pid) and _socket_responds(sock):
        return

    _cleanup_stale_daemon()

    _spawn_daemon()
    deadline = time.monotonic() + STARTUP_TIMEOUT_SEC
    while time.monotonic() < deadline:
        if _socket_responds(sock, timeout=0.5):
            return
        time.sleep(0.1)

    raise RuntimeError(
        f"daemon did not become ready within {STARTUP_TIMEOUT_SEC}s; "
        f"see {ensure_state_dir() / 'daemon.log'}"
    )


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("daemon closed connection")
        buf.extend(chunk)
    return bytes(buf)


def _recv_json(sock: socket.socket) -> dict[str, Any]:
    resp_header = _recv_exact(sock, 4)
    length = int.from_bytes(resp_header, "big")
    body = _recv_exact(sock, length)
    return json.loads(body.decode("utf-8"))


def _send_raw(
    sock_path: Path,
    payload: dict[str, Any],
    timeout: float | None,
    *,
    on_event: EventHandler | None = None,
) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    header = len(data).to_bytes(4, "big")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(sock_path))
        s.sendall(header + data)
        # Daemon may send one or more event frames (e.g. ttsText) before the
        # final response that includes an "ok" field.
        while True:
            msg = _recv_json(s)
            if "ok" in msg:
                return msg
            if on_event is not None:
                on_event(msg)


def send_request(
    payload: dict[str, Any],
    *,
    timeout: float | None = REQUEST_TIMEOUT_SEC,
    on_event: EventHandler | None = None,
) -> dict[str, Any]:
    ensure_daemon_running()
    return _send_raw(daemon_sock_path(), payload, timeout, on_event=on_event)
