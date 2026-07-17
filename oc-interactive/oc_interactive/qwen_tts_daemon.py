"""Persistent Qwen3-TTS daemon (mlx-audio) with cached model."""

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

import numpy as np

from oc_interactive.client import IDLE_TIMEOUT_SEC
from oc_interactive.paths import (
    ensure_state_dir,
    tts_daemon_log_path,
    tts_daemon_pid_path,
    tts_daemon_sock_path,
)

# Re-exported for clarity; matches orchestration daemon idle policy.
_IDLE_TIMEOUT_SEC = IDLE_TIMEOUT_SEC


class _TTSSession:
    def __init__(self) -> None:
        self._model = None
        self._model_id: str | None = None

    def ensure_model(self, model_id: str) -> tuple[Any, bool, float]:
        """Return (model, reloaded, load_ms)."""
        if self._model is not None and self._model_id == model_id:
            return self._model, False, 0.0

        from mlx_audio.tts.utils import load_model

        t0 = time.monotonic()
        self._model = load_model(model_id)
        self._model_id = model_id
        load_ms = (time.monotonic() - t0) * 1000
        return self._model, True, load_ms

    def synthesize(self, req: dict[str, Any]) -> dict[str, Any]:
        text = str(req.get("text", "")).strip()
        if not text:
            return {"ok": False, "error": "text is required"}

        output = str(req.get("output", "")).strip()
        if not output:
            return {"ok": False, "error": "output path is required"}

        model_id = str(req.get("model", "")).strip()
        if not model_id:
            return {"ok": False, "error": "model is required"}

        mode = str(req.get("mode", "custom_voice")).strip().lower()
        language = str(req.get("language") or "English")
        speaker = req.get("speaker")
        instruct = req.get("instruct")
        if isinstance(instruct, str):
            instruct = instruct.strip() or None
        else:
            instruct = None
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

        try:
            model, reloaded, load_ms = self.ensure_model(model_id)
        except Exception as e:
            return {"ok": False, "error": f"failed to load model: {e}"}

        t0 = time.monotonic()
        try:
            audio, sample_rate = _generate_audio(
                model,
                text=text,
                mode=mode,
                language=language,
                speaker=speaker if isinstance(speaker, str) else None,
                instruct=instruct,
                refaudio=refaudio,
                reftext=reftext,
            )
            _write_wav(Path(output), audio, sample_rate)
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            return {"ok": False, "error": str(e)}
        synth_ms = (time.monotonic() - t0) * 1000

        return {
            "ok": True,
            "modelReloaded": reloaded,
            "refaudioReloaded": False,
            "loadMs": load_ms,
            "synthMs": synth_ms,
        }


def _generate_audio(
    model: Any,
    *,
    text: str,
    mode: str,
    language: str,
    speaker: str | None,
    instruct: str | None,
    refaudio: str | None,
    reftext: str | None,
) -> tuple[np.ndarray, int]:
    """Run mlx-audio generation and return (mono float/int samples, sample_rate)."""
    if mode == "voice_design":
        if not instruct:
            raise ValueError("voice_design mode requires instruct / voice description")
        results = list(
            model.generate_voice_design(
                text=text,
                instruct=instruct,
                language=language,
                verbose=False,
            )
        )
    elif mode == "clone":
        if not refaudio:
            raise ValueError("clone mode requires refaudio")
        if not reftext:
            raise ValueError("clone mode requires reftext")
        results = list(
            model.generate(
                text=text,
                ref_audio=refaudio,
                ref_text=reftext,
                lang_code=language,
                verbose=False,
            )
        )
    elif mode == "custom_voice":
        if not speaker:
            raise ValueError("custom_voice mode requires speaker")
        results = list(
            model.generate_custom_voice(
                text=text,
                speaker=speaker,
                language=language,
                instruct=instruct,
                verbose=False,
            )
        )
    else:
        raise ValueError(f"unknown TTS mode: {mode!r}")

    if not results:
        raise RuntimeError("TTS produced no audio")

    chunks: list[np.ndarray] = []
    sample_rate = int(getattr(results[0], "sample_rate", 24000))
    for result in results:
        sample_rate = int(getattr(result, "sample_rate", sample_rate))
        audio = result.audio
        if hasattr(audio, "tolist") and not isinstance(audio, np.ndarray):
            arr = np.asarray(audio.tolist(), dtype=np.float32)
        else:
            arr = np.asarray(audio, dtype=np.float32)
        if arr.ndim > 1:
            arr = arr.reshape(-1)
        chunks.append(arr)

    if len(chunks) == 1:
        return chunks[0], sample_rate
    return np.concatenate(chunks, axis=0), sample_rate


def _write_wav(path: Path, audio: np.ndarray, sample_rate: int) -> None:
    from mlx_audio.audio_io import write as audio_write

    path.parent.mkdir(parents=True, exist_ok=True)
    audio_write(str(path), audio, sample_rate, format="wav")


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


def run_daemon() -> int:
    ensure_state_dir()
    sock_path = tts_daemon_sock_path()
    pid_path = tts_daemon_pid_path()

    if sock_path.exists():
        sock_path.unlink()

    pid_path.write_text(str(os.getpid()), encoding="utf-8")

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(5)
    server.settimeout(1.0)

    session = _TTSSession()
    last_activity = time.monotonic()
    print(f"[qwen-tts-daemon] ready on {sock_path}", file=sys.stderr, flush=True)

    try:
        while True:
            if time.monotonic() - last_activity > _IDLE_TIMEOUT_SEC:
                print(
                    "[qwen-tts-daemon] idle timeout, shutting down",
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
                conn.settimeout(None)
                try:
                    header = _recv_exact(conn, 4)
                    length = int.from_bytes(header, "big")
                    body = _recv_exact(conn, length)
                    request = json.loads(body.decode("utf-8"))
                    response = session.synthesize(request)
                except Exception as e:
                    traceback.print_exc(file=sys.stderr)
                    response = {"ok": False, "error": str(e)}
                _send_json(conn, response)
    finally:
        server.close()
        sock_path.unlink(missing_ok=True)
        pid_path.unlink(missing_ok=True)

    return 0


def _shutdown_handler(signum, frame) -> None:
    raise SystemExit(0)


def main() -> int:
    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)
    # Ensure logs go to the TTS daemon log when launched under Popen with redirected fds.
    _ = tts_daemon_log_path()
    return run_daemon()


if __name__ == "__main__":
    raise SystemExit(main())
