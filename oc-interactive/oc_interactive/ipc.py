"""Length-prefixed JSON framing over Unix domain sockets."""

from __future__ import annotations

import json
import socket
from typing import Any


def send_json(sock_path: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(sock_path)
        s.sendall(len(data).to_bytes(4, "big") + data)
        header = _recv_exact(s, 4)
        length = int.from_bytes(header, "big")
        body = _recv_exact(s, length)
    return json.loads(body.decode("utf-8"))


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed connection")
        buf.extend(chunk)
    return bytes(buf)
