"""Synthesize speech via cached dots-tts daemon and play through speakers."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

from oc_interactive.speakable import tag_for_tts
from oc_interactive.tts_daemon import synthesize_to_wav


class TTSError(Exception):
    pass


def synthesize_and_play(
    text: str,
    *,
    refaudio: str,
    reftext: str | None,
    model: str,
    dots_tts_bin: Path,
    language: str = "EN",
    debug: bool = False,
) -> dict[str, float | bool]:
    """Return TTS timing metrics from the dots-tts daemon."""
    if not dots_tts_bin.exists():
        raise TTSError(
            f"dots-tts binary not found at {dots_tts_bin}; "
            "build it with: cd app && make build"
        )

    tagged = tag_for_tts(text, language=language)

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav_path = Path(tmp.name)

    t0 = time.monotonic()
    try:
        resp = synthesize_to_wav(
            text=tagged,
            refaudio=refaudio,
            reftext=reftext,
            model=model,
            output=wav_path,
            dots_tts_bin=dots_tts_bin,
            language=language.upper(),
            debug=debug,
        )
        if not resp.get("ok"):
            raise TTSError(str(resp.get("error", "TTS daemon failed")))

        if debug:
            model_reloaded = resp.get("modelReloaded", False)
            ref_reloaded = resp.get("refaudioReloaded", False)
            load_ms = resp.get("loadMs", 0)
            synth_ms = resp.get("synthMs", 0)
            print(
                f"[oc-interactive] tts-daemon modelReloaded={model_reloaded} "
                f"refaudioReloaded={ref_reloaded} loadMs={load_ms:.0f} synthMs={synth_ms:.0f}",
                file=sys.stderr,
            )

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

    wall_ms = (time.monotonic() - t0) * 1000
    return {
        "modelReloaded": bool(resp.get("modelReloaded")),
        "refaudioReloaded": bool(resp.get("refaudioReloaded")),
        "loadMs": float(resp.get("loadMs", 0)),
        "synthMs": float(resp.get("synthMs", 0)),
        "wallMs": wall_ms,
    }
