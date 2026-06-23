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
from oc_interactive.io import CliError, eprint
from oc_interactive.paths import default_config_path, debug_enabled
from oc_interactive.session import load_session
from oc_interactive.slash import (
    is_dump_command,
    is_slash_command,
    parse_slash_command,
)


def _read_stdin_text() -> str | None:
    """Return piped stdin text, or None when stdin is a TTY or empty."""
    if sys.stdin.isatty():
        return None
    data = sys.stdin.read()
    if not data:
        return None
    return data.removesuffix("\n")


def _resolve_text(args: argparse.Namespace, parser: argparse.ArgumentParser) -> str:
    """stdin wins over -t/--text when both are present."""
    stdin_text = _read_stdin_text()
    if stdin_text is not None:
        text = stdin_text
    elif args.text:
        text = args.text
    else:
        parser.error("text is required via stdin or -t/--text")
    if not text:
        parser.error("text is required via stdin or -t/--text")
    return text


def _build_parser() -> argparse.ArgumentParser:
    class Parser(argparse.ArgumentParser):
        def error(self, message: str) -> None:
            self.exit(2, f"error: {message}\n")

    p = Parser(
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
        help="UTF-8 text to send, or a slash command (e.g. /new, /history). "
        "Omit when piping text on stdin; stdin wins if both are given.",
    )
    p.add_argument(
        "-r",
        "--refaudio",
        help="Reference audio for voice cloning (required on first TTS turn).",
    )
    p.add_argument(
        "--reftext",
        help="Transcript of the reference audio (required with --refaudio; ignored otherwise).",
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
        "-c",
        "--config",
        "--openclaw-config",
        dest="openclaw_config",
        default=str(default_config_path()),
        metavar="PATH",
        help=f"Path to openclaw.json gateway config (default: {default_config_path()}).",
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
        "-v",
        "--verbose",
        action="store_true",
        default=False,
        help="Print the OpenClaw agent reply to stdout.",
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
) -> tuple[str, str | None, str, Path]:
    session = load_session()

    refaudio = args.refaudio
    # --reftext is only honored together with --refaudio; otherwise ignored.
    if refaudio:
        refaudio = str(Path(refaudio).expanduser().resolve())
        reftext_arg = (args.reftext or "").strip()
        if not reftext_arg:
            raise CliError("--reftext is required when --refaudio is provided")
        reftext = reftext_arg
    elif session.last_refaudio:
        refaudio = session.last_refaudio
        reftext = session.last_reftext
    else:
        raise CliError(
            "--refaudio is required on the first turn (no cached reference audio)"
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

    return refaudio, reftext, tts_model, dots_path


def _handle_dump(agent: str) -> int:
    session = load_session()
    doc = session.dump_document(agent=agent)
    sys.stdout.write(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    eprint(f"[oc-interactive] dumped {doc['messageCount']} messages")
    return 0


def _report_error(message: str) -> int:
    text = str(message).strip()
    if text and not text.startswith("error:"):
        text = f"error: {text}"
    eprint(text)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.daemon:
        return daemon_main()

    text = _resolve_text(args, parser)

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
        return _report_error(str(e))

    try:
        agent = cfg.resolve_agent(args.agent)
    except ValueError as e:
        return _report_error(str(e))

    try:
        refaudio, reftext, tts_model, dots_path = _resolve_paths(args, cfg)
    except CliError as e:
        return _report_error(str(e))

    if not dots_path.exists():
        return _report_error(
            f"dots-tts not found at {dots_path}; build with: cd app && make build"
        )

    payload = {
        "text": text,
        "refaudio": refaudio,
        "reftext": reftext,
        "ttsModel": tts_model,
        "agent": agent,
        "openclawConfig": str(config_path),
        "dotsTtsBinary": str(dots_path),
        "openclawToken": cfg.token,
        "debug": debug_enabled(args.debug),
    }

    eprint("[oc-interactive] waiting for agent reply and TTS…")
    try:
        resp = send_request(payload)
    except (TimeoutError, socket.timeout):
        return _report_error(
            "timed out waiting for daemon (OpenClaw + TTS can take several minutes on first run); "
            "audio may still play — check daemon.log"
        )
    except OSError as e:
        if "timed out" in str(e).lower():
            return _report_error(
                "timed out waiting for daemon (OpenClaw + TTS can take several minutes on first run); "
                "audio may still play — check daemon.log"
            )
        return _report_error(str(e))
    except Exception as e:
        return _report_error(str(e))

    if not resp.get("ok"):
        return _report_error(str(resp.get("error", "unknown error")))

    if err := resp.get("error"):
        eprint(err)

    if args.verbose:
        reply = resp.get("reply")
        if isinstance(reply, str) and reply:
            sys.stdout.write(reply + "\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
