"""oc-interactive command-line interface."""

from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path

from oc_interactive.client import send_request
from oc_interactive.config import load_config
from oc_interactive.daemon import main as daemon_main
from oc_interactive.paths import default_config_path, debug_enabled
from oc_interactive.session import load_session
from oc_interactive.slash import (
    is_dump_command,
    is_slash_command,
    parse_slash_command,
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="oc-interactive",
        description=(
            "Send text to an OpenClaw agent and speak the reply via dots-tts. "
            "Each invocation is one turn; a background daemon orchestrates TTS."
        ),
    )
    p.add_argument(
        "-t",
        "--text",
        required=False,
        help="UTF-8 text to send, or a slash command (e.g. /new, /history).",
    )
    p.add_argument(
        "-r",
        "--refaudio",
        help="Reference audio for voice cloning (required on first TTS turn).",
    )
    p.add_argument(
        "-l",
        "--language",
        help="Language tag (parity with dots-tts; agent replies use EN).",
    )
    p.add_argument(
        "-o",
        "--output",
        default="./output.wav",
        help="Ignored; oc-interactive plays audio only.",
    )
    p.add_argument(
        "-m",
        "--model",
        default="./dots.tts-soar-mlx",
        help="Path to the dots.tts-soar-mlx model directory.",
    )
    p.add_argument(
        "--agent",
        default=None,
        help="OpenClaw agent short name (main, news, eileen). Default from config.",
    )
    p.add_argument(
        "--openclaw-config",
        default=str(default_config_path()),
        help="Path to openclaw.json gateway config.",
    )
    p.add_argument(
        "--dots-tts",
        default=None,
        help="Path to dots-tts binary (default from config or app/.build/dots-tts).",
    )
    p.add_argument(
        "--debug",
        action="store_true",
        help="Log timing and TTS model cache status (or set OC_INTERACTIVE_DEBUG=1).",
    )
    p.add_argument(
        "--daemon",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return p


def _resolve_paths(
    args: argparse.Namespace,
    cfg,
) -> tuple[str, str, Path]:
    session = load_session()

    refaudio = args.refaudio
    if refaudio:
        refaudio = str(Path(refaudio).expanduser().resolve())
    elif session.last_refaudio:
        refaudio = session.last_refaudio
    else:
        raise SystemExit(
            "error: --refaudio is required on the first turn (no cached reference audio)"
        )

    tts_model = args.model
    if session.last_tts_model:
        tts_model = session.last_tts_model
    elif tts_model:
        tts_model = str(Path(tts_model).expanduser().resolve())
    else:
        tts_model = str(Path("./dots.tts-soar-mlx").expanduser().resolve())

    dots_bin = args.dots_tts
    if dots_bin:
        dots_path = Path(dots_bin).expanduser().resolve()
    elif session.last_dots_tts:
        dots_path = Path(session.last_dots_tts)
    elif cfg.dots_tts_binary:
        dots_path = cfg.dots_tts_binary
    else:
        # Default relative to repo layout when run from oc-interactive/
        dots_path = (
            Path(__file__).resolve().parent.parent.parent / "app" / ".build" / "dots-tts"
        ).resolve()

    return refaudio, tts_model, dots_path


def _handle_dump(agent: str) -> int:
    session = load_session()
    doc = session.dump_document(agent=agent)
    sys.stdout.write(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    print(
        f"[oc-interactive] dumped {doc['messageCount']} messages",
        file=sys.stderr,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.daemon:
        return daemon_main()

    if not args.text:
        parser.error("the following arguments are required: -t/--text")

    text = args.text

    if is_slash_command(text):
        slash = parse_slash_command(text)
        if slash and is_dump_command(slash):
            session = load_session()
            dump_agent = args.agent or session.last_agent or "main"
            return _handle_dump(dump_agent)

    config_path = Path(args.openclaw_config).expanduser().resolve()
    try:
        cfg = load_config(config_path)
    except (FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    try:
        agent = cfg.resolve_agent(args.agent)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    # Spoken commands and chat need refaudio/model/dots-tts
    try:
        refaudio, tts_model, dots_path = _resolve_paths(args, cfg)
    except SystemExit as e:
        print(str(e), file=sys.stderr)
        return 1

    if not dots_path.exists():
        print(
            f"error: dots-tts not found at {dots_path}; build with: cd app && make build",
            file=sys.stderr,
        )
        return 1

    payload = {
        "text": text,
        "refaudio": refaudio,
        "ttsModel": tts_model,
        "agent": agent,
        "openclawConfig": str(config_path),
        "dotsTtsBinary": str(dots_path),
        "openclawToken": cfg.token,
        "debug": debug_enabled(args.debug),
    }

    print("[oc-interactive] waiting for agent reply and TTS…", file=sys.stderr)
    try:
        resp = send_request(payload)
    except (TimeoutError, socket.timeout) as e:
        print(
            "error: timed out waiting for daemon (OpenClaw + TTS can take several minutes on first run); "
            "audio may still play — check daemon.log",
            file=sys.stderr,
        )
        return 1
    except OSError as e:
        if "timed out" in str(e).lower():
            print(
                "error: timed out waiting for daemon (OpenClaw + TTS can take several minutes on first run); "
                "audio may still play — check daemon.log",
                file=sys.stderr,
            )
            return 1
        print(f"error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if not resp.get("ok"):
        err = resp.get("error", "unknown error")
        print(f"error: {err}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
