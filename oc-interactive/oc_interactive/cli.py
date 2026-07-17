"""oc-interactive command-line interface."""

from __future__ import annotations

import argparse
import json
import socket
import sys
from dataclasses import dataclass
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
from oc_interactive.tts_defaults import (
    DEFAULT_LANGUAGE,
    DEFAULT_MODE,
    DEFAULT_MODEL_BY_MODE,
    DEFAULT_SPEAKER,
    DEFAULT_TTS_MODEL,
    MODE_CLONE,
    MODE_CUSTOM_VOICE,
    MODE_VOICE_DESIGN,
)


@dataclass(frozen=True)
class VoiceSettings:
    mode: str
    tts_model: str
    language: str
    speaker: str | None = None
    instruct: str | None = None
    voice_design: str | None = None
    refaudio: str | None = None
    reftext: str | None = None


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


def _resolve_model_string(model: str) -> str:
    """Keep HF repo ids as-is; resolve local paths."""
    raw = model.strip()
    if not raw:
        return DEFAULT_TTS_MODEL
    looks_local = (
        raw.startswith(("/", "./", "../", "~"))
        or Path(raw).expanduser().exists()
    )
    if looks_local:
        return str(Path(raw).expanduser().resolve())
    return raw


def _is_mlx_audio_compatible_model(model: str) -> bool:
    """False for legacy dots.tts paths/checkpoints that mlx-audio cannot load."""
    raw = (model or "").strip()
    if not raw:
        return False
    lower = raw.lower().replace("\\", "/")
    if "dots.tts" in lower or "dots_tts" in lower or "/dots.tts-" in lower:
        return False

    path = Path(raw).expanduser()
    if not path.exists() or not path.is_dir():
        # HF repo ids and missing paths: assume OK (download / fail later).
        return True

    for candidate in (
        path / "config.json",
        path / "backbone" / "config.json",
        path / "model_index.json",
    ):
        if not candidate.is_file():
            continue
        try:
            import json

            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        model_type = str(
            data.get("model_type")
            or data.get("model_type".upper())
            or data.get("architectures", [""])[0]
            or ""
        ).lower()
        if "dots" in model_type:
            return False
    return True


def _resolve_tts_model(args: argparse.Namespace, cfg, session) -> str:
    """CLI > compatible session cache > config > built-in default."""
    if args.model:
        return _resolve_model_string(args.model)

    cached = session.last_tts_model
    if cached and _is_mlx_audio_compatible_model(cached):
        return cached

    return cfg.tts_model or DEFAULT_TTS_MODEL


def _infer_qwen_model_kind(model: str) -> str | None:
    """Return MODE_* when the id/path names a known Qwen3-TTS variant."""
    lower = (model or "").lower().replace("\\", "/")
    if "voicedesign" in lower or "voice-design" in lower or "voice_design" in lower:
        return MODE_VOICE_DESIGN
    if "customvoice" in lower or "custom-voice" in lower or "custom_voice" in lower:
        return MODE_CUSTOM_VOICE
    # Base clone models: require "base" but not the other variant names.
    if "base" in lower and "qwen3-tts" in lower:
        return MODE_CLONE
    if "/base" in lower or lower.endswith("-base") or lower.endswith("-base-8bit") or lower.endswith("-base-bf16"):
        return MODE_CLONE
    return None


def _ensure_model_matches_mode(
    model: str,
    mode: str,
    *,
    explicit_cli_model: bool,
) -> str:
    """If mode and checkpoint disagree, pick the default HF id for that mode.

    Explicit ``-m`` is left alone (caller gets a clear mlx-audio error). Otherwise
    mlx-audio will download the matching VoiceDesign / CustomVoice / Base weights.
    """
    kind = _infer_qwen_model_kind(model)
    if kind == mode:
        return model
    if explicit_cli_model:
        return model
    default = DEFAULT_MODEL_BY_MODE.get(mode)
    if not default:
        return model
    if kind is None or kind != mode:
        return default
    return model


def _resolve_voice(args: argparse.Namespace, cfg) -> VoiceSettings:
    session = load_session()
    cfg_voice_design = getattr(cfg, "tts_voice_design", None)

    # Mode: explicit CLI flags win; otherwise config VoiceDesign beats session
    # (ttsVoiceDesign takes priority over ttsSpeaker / cached custom_voice).
    if args.voice_design:
        mode = MODE_VOICE_DESIGN
    elif args.refaudio:
        mode = MODE_CLONE
    elif args.speaker is not None or args.instruct is not None:
        mode = MODE_CUSTOM_VOICE
    elif cfg_voice_design:
        mode = MODE_VOICE_DESIGN
    elif session.last_voice_mode:
        mode = session.last_voice_mode
    else:
        mode = DEFAULT_MODE

    resolved = _resolve_tts_model(args, cfg, session)
    tts_model = _ensure_model_matches_mode(
        resolved,
        mode,
        explicit_cli_model=bool(args.model),
    )
    if tts_model != resolved:
        eprint(
            f"[oc-interactive] mode={mode} needs a matching checkpoint; "
            f"using {tts_model} (downloaded on first use if missing)"
        )

    if args.language:
        language = args.language
    elif session.last_language:
        language = session.last_language
    else:
        language = getattr(cfg, "tts_language", None) or DEFAULT_LANGUAGE

    speaker: str | None = None
    instruct: str | None = None
    voice_design: str | None = None
    refaudio: str | None = None
    reftext: str | None = None

    if mode == MODE_VOICE_DESIGN:
        if args.voice_design:
            voice_design = args.voice_design.strip()
        elif session.last_voice_design:
            voice_design = session.last_voice_design
        elif cfg_voice_design:
            voice_design = cfg_voice_design
        else:
            raise CliError(
                "--voice-design or config ttsVoiceDesign is required for VoiceDesign mode"
            )
        if not voice_design:
            raise CliError("voice design description must not be empty")
        instruct = voice_design

    elif mode == MODE_CLONE:
        if args.refaudio:
            refaudio = str(Path(args.refaudio).expanduser().resolve())
            reftext_arg = (args.reftext or "").strip()
            if not reftext_arg:
                raise CliError("--reftext is required when --refaudio is provided")
            reftext = reftext_arg
        elif session.last_refaudio:
            refaudio = session.last_refaudio
            reftext = session.last_reftext
        else:
            raise CliError(
                "--refaudio is required for clone mode (no cached reference audio)"
            )
        if not reftext:
            raise CliError("reftext is required for clone mode")

    else:
        mode = MODE_CUSTOM_VOICE
        if args.speaker is not None:
            speaker = args.speaker.strip() or None
        elif session.last_speaker:
            speaker = session.last_speaker
        else:
            speaker = cfg.tts_speaker or DEFAULT_SPEAKER
        if not speaker:
            speaker = DEFAULT_SPEAKER

        if args.instruct is not None:
            instruct = args.instruct.strip() or None
        elif session.last_instruct:
            instruct = session.last_instruct
        else:
            cfg_instruct = getattr(cfg, "tts_instruct", None)
            instruct = cfg_instruct if isinstance(cfg_instruct, str) else None
            if instruct is not None:
                instruct = instruct.strip() or None

    return VoiceSettings(
        mode=mode,
        tts_model=tts_model,
        language=language,
        speaker=speaker,
        instruct=instruct,
        voice_design=voice_design,
        refaudio=refaudio,
        reftext=reftext,
    )


def _resolve_config_path(args: argparse.Namespace) -> Path:
    session = load_session()
    if args.openclaw_config:
        return Path(args.openclaw_config).expanduser().resolve()
    if session.last_openclaw_config:
        return Path(session.last_openclaw_config)
    return default_config_path()


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
    if args.tts_daemon:
        from oc_interactive.qwen_tts_daemon import main as tts_daemon_main

        return tts_daemon_main()

    text = _resolve_text(args, parser)

    if is_slash_command(text):
        slash = parse_slash_command(text)
        if slash and is_dump_command(slash):
            session = load_session()
            dump_agent = args.agent or session.last_agent or "main"
            return _handle_dump(dump_agent)

    config_path = _resolve_config_path(args)
    try:
        cfg = load_config(config_path)
    except (FileNotFoundError, ValueError) as e:
        return _report_error(str(e))

    try:
        agent = cfg.resolve_agent(args.agent)
    except ValueError as e:
        return _report_error(str(e))

    try:
        voice = _resolve_voice(args, cfg)
    except CliError as e:
        return _report_error(str(e))

    payload = {
        "text": text,
        "ttsModel": voice.tts_model,
        "mode": voice.mode,
        "language": voice.language,
        "speaker": voice.speaker,
        "instruct": voice.instruct,
        "voiceDesign": voice.voice_design,
        "refaudio": voice.refaudio,
        "reftext": voice.reftext,
        "agent": agent,
        "openclawConfig": str(config_path),
        "openclawToken": cfg.token,
        "debug": debug_enabled(args.debug),
        "quiet": bool(args.quiet),
    }

    if args.timeout is not None and args.timeout <= 0:
        return _report_error("--timeout must be a positive integer")

    eprint("[oc-interactive] waiting for agent reply and TTS…")
    try:
        resp = send_request(payload, timeout=args.timeout)
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
