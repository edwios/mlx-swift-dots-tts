"""oc-interactive command-line interface."""

from __future__ import annotations

import argparse
import json
import socket
import sys

from oc_interactive.client import send_request
from oc_interactive.config import load_config
from oc_interactive.daemon import main as daemon_main
from oc_interactive.io import CliError, eprint
from oc_interactive.paths import default_config_path
from oc_interactive.session import (
    archive_and_new_session,
    clear_cached_settings,
    load_session,
    save_session,
)
from oc_interactive.slash import (
    is_dump_command,
    is_slash_command,
    parse_slash_command,
)
from oc_interactive.ssh_tunnel import (
    ensure_ssh_tunnel,
    load_tunnel_spec,
    teardown_tunnel,
    tunnel_status,
)
from oc_interactive.turn import (
    build_payload,
    resolve_config_path,
    resolve_voice,
)
from oc_interactive.tts_defaults import (
    DEFAULT_LANGUAGE,
    DEFAULT_SPEAKER,
    DEFAULT_TTS_MODEL,
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
            "Send text to an OpenClaw agent and speak the reply via Qwen3-TTS "
            "(mlx-audio). Each invocation is one turn; a background daemon "
            "orchestrates TTS."
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
        help="Reference audio for voice cloning (Base model). Implies clone mode.",
    )
    p.add_argument(
        "--reftext",
        help="Transcript of the reference audio (required with --refaudio).",
    )
    p.add_argument(
        "--speaker",
        help=f"CustomVoice speaker (default: {DEFAULT_SPEAKER}). English: Ryan, Aiden.",
    )
    p.add_argument(
        "--instruct",
        help="Emotion/style instruction for CustomVoice (e.g. 'calm and warm'). "
        "Default from config ttsInstruct when omitted.",
    )
    p.add_argument(
        "--voice-design",
        dest="voice_design",
        help="Natural-language voice description (VoiceDesign model). Implies voice_design mode.",
    )
    p.add_argument(
        "-l",
        "--language",
        help=f"Language for TTS (default from config ttsLanguage, else {DEFAULT_LANGUAGE}). "
        "Qwen3-TTS uses codes like English, Chinese — not regional variants.",
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
        default=None,
        help=(
            "Qwen3-TTS mlx-audio model id or local path "
            f"(default: {DEFAULT_TTS_MODEL})."
        ),
    )
    p.add_argument(
        "--agent",
        default=None,
        help="OpenClaw agent short name (main, news, eileen). Default from config.",
    )
    p.add_argument(
        "-c",
        "--config",
        dest="openclaw_config",
        default=None,
        metavar="PATH",
        help=f"Path to oc-interactive.json gateway config (default: {default_config_path()}; "
        "cached after first turn).",
    )
    p.add_argument(
        "--init",
        action="store_true",
        help=(
            "Forget cached voice/agent/config settings and reload defaults from "
            f"{default_config_path()} (or -c/--config if given). "
            "Can be used alone or with a turn."
        ),
    )
    p.add_argument(
        "--new",
        action="store_true",
        help=(
            "Archive the current session (if it has messages) under "
            "~/.config/oc-interactive/sessions/ and start a new one. "
            "Keeps system prompt and cached voice settings. "
            "Can be used alone or with a turn."
        ),
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=None,
        metavar="SECONDS",
        help="Seconds to wait for agent reply and TTS (default: no timeout).",
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
        "-q",
        "--quiet",
        action="store_true",
        default=False,
        help="Do not print the text sent to TTS (printed to stdout by default).",
    )
    p.add_argument(
        "--ssh-tunnel-status",
        action="store_true",
        help="Print JSON health of the ssh tunnel declared in the config's ssh block, then exit.",
    )
    p.add_argument(
        "--ssh-tunnel-teardown",
        action="store_true",
        help="Tear down the managed ssh tunnel declared in the config's ssh block, then exit.",
    )
    p.add_argument(
        "--daemon",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    p.add_argument(
        "--tts-daemon",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return p


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


def _handle_ssh_tunnel_flags(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(args)
    try:
        cfg = load_config(config_path)
    except (FileNotFoundError, ValueError) as e:
        return _report_error(str(e))

    try:
        spec = load_tunnel_spec(cfg.raw)
    except ValueError as e:
        return _report_error(str(e))

    if spec is None:
        eprint("[oc-interactive] no ssh block in config; nothing to manage")
        return 0

    if args.ssh_tunnel_teardown:
        _, state = tunnel_status(spec)
        had_tunnel = state is not None
        teardown_tunnel(spec)
        if had_tunnel:
            eprint(f"[oc-interactive] tunnel on port {spec.local_port} torn down")
        else:
            eprint(f"[oc-interactive] no managed tunnel on port {spec.local_port}")
        return 0

    healthy, state = tunnel_status(spec)
    print(json.dumps({"healthy": healthy, "state": state}, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.daemon:
        return daemon_main()
    if args.tts_daemon:
        from oc_interactive.qwen_tts_daemon import main as tts_daemon_main

        return tts_daemon_main()

    if args.ssh_tunnel_status or args.ssh_tunnel_teardown:
        return _handle_ssh_tunnel_flags(args)

    if args.new:
        session, archived = archive_and_new_session(keep_system_prompt=True)
        if archived:
            eprint(f"[oc-interactive] archived session → {archived}")
        else:
            eprint("[oc-interactive] no messages to archive")
        eprint(f"[oc-interactive] new session {session.user_id}")

    if args.init:
        clear_cached_settings()
        eprint(
            "[oc-interactive] cleared cached settings; "
            f"defaults from {resolve_config_path(args)}"
        )

    stdin_text = _read_stdin_text()
    # Allow `oc-interactive --new` / `--init` with no turn text.
    if (args.new or args.init) and args.text is None and stdin_text is None:
        config_path = resolve_config_path(args)
        try:
            cfg = load_config(config_path)
        except (FileNotFoundError, ValueError) as e:
            return _report_error(str(e))
        try:
            ensure_ssh_tunnel(cfg, debug=args.debug)
        except RuntimeError as e:
            return _report_error(str(e))
        # Persist config_path even without a turn, so later commands that omit
        # -c/--config (e.g. run-chat, talk_eileen.sh) keep using this config
        # instead of silently falling back to the default one.
        session = load_session()
        session.last_openclaw_config = str(config_path)
        save_session(session)
        return 0

    if stdin_text is not None:
        text = stdin_text
    elif args.text:
        text = args.text
    else:
        parser.error("text is required via stdin or -t/--text")
    if not text:
        parser.error("text is required via stdin or -t/--text")

    if is_slash_command(text):
        slash = parse_slash_command(text)
        if slash and is_dump_command(slash):
            session = load_session()
            dump_agent = args.agent or session.last_agent or "main"
            return _handle_dump(dump_agent)

    config_path = resolve_config_path(args)
    try:
        cfg = load_config(config_path)
    except (FileNotFoundError, ValueError) as e:
        return _report_error(str(e))

    try:
        ensure_ssh_tunnel(cfg, debug=args.debug)
    except RuntimeError as e:
        return _report_error(str(e))

    try:
        # Fall back to the agent the session was last talking to (set by
        # /agent or a previous --agent) so restoring a session on restart
        # doesn't silently snap back to the config's default agent.
        agent = cfg.resolve_agent(args.agent or load_session().last_agent)
    except ValueError as e:
        return _report_error(str(e))

    try:
        voice = resolve_voice(args, cfg)
    except CliError as e:
        return _report_error(str(e))

    payload = build_payload(
        text,
        voice=voice,
        agent=agent,
        config_path=config_path,
        token=cfg.token,
        debug=args.debug,
        quiet=args.quiet,
    )

    if args.timeout is not None and args.timeout <= 0:
        return _report_error("--timeout must be a positive integer")

    eprint("[oc-interactive] waiting for agent reply and TTS…")

    printed_tts: str | None = None

    def _on_daemon_event(msg: dict) -> None:
        nonlocal printed_tts
        if msg.get("event") != "ttsText":
            return
        text_out = msg.get("ttsText")
        if not isinstance(text_out, str) or not text_out:
            return
        # Agent-failure speech is reported on stderr via the final response.
        if msg.get("isError"):
            return
        if args.quiet:
            return
        sys.stdout.write(text_out if text_out.endswith("\n") else text_out + "\n")
        sys.stdout.flush()
        printed_tts = text_out

    try:
        resp = send_request(payload, timeout=args.timeout, on_event=_on_daemon_event)
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
        # Agent failures: stderr only (also spoken by the daemon).
        eprint(err)
    else:
        # Fallback for older daemons that only return ttsText on the final frame.
        tts_text = resp.get("ttsText")
        if (
            printed_tts is None
            and isinstance(tts_text, str)
            and tts_text
            and not args.quiet
        ):
            sys.stdout.write(tts_text if tts_text.endswith("\n") else tts_text + "\n")
            printed_tts = tts_text

        if args.verbose:
            reply = resp.get("reply")
            if isinstance(reply, str) and reply and (args.quiet or reply != printed_tts):
                sys.stdout.write(reply if reply.endswith("\n") else reply + "\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
