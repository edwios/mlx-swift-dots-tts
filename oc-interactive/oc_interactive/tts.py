"""Synthesize speech via dots-tts and play through speakers."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from oc_interactive.speakable import tag_for_tts


class TTSError(Exception):
    pass


def synthesize_and_play(
    text: str,
    *,
    refaudio: str,
    model: str,
    dots_tts_bin: Path,
    language: str = "EN",
) -> None:
    if not dots_tts_bin.exists():
        raise TTSError(
            f"dots-tts binary not found at {dots_tts_bin}; "
            "build it with: cd app && make build"
        )

    tagged = tag_for_tts(text, language=language)
    binary_dir = dots_tts_bin.parent

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav_path = Path(tmp.name)

    try:
        cmd = [
            str(dots_tts_bin),
            "-t",
            tagged,
            "-r",
            refaudio,
            "-m",
            model,
            "-o",
            str(wav_path),
            "-l",
            language.upper(),
        ]
        result = subprocess.run(
            cmd,
            cwd=str(binary_dir),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "unknown error").strip()
            raise TTSError(err)

        play = subprocess.run(
            ["afplay", str(wav_path)],
            capture_output=True,
            text=True,
        )
        if play.returncode != 0:
            err = (play.stderr or play.stdout or "afplay failed").strip()
            raise TTSError(err)
    finally:
        wav_path.unlink(missing_ok=True)
