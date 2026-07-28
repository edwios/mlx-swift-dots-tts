"""Textual chat UI for oc-interactive.

Unlike ``cli.py`` (one process per turn), this is a single long-running
process: it loads existing session history once, then keeps a Unix-socket
connection cycle open to the same orchestration daemon (``send_request`` in
``client.py``) for every subsequent turn, rendering the dialogue as a
scrolling chat log with the input pinned to the bottom.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Header, Input, Static

from oc_interactive.client import send_request
from oc_interactive.config import OpenClawConfig, load_config
from oc_interactive.io import CliError, eprint
from oc_interactive.session import (
    archive_and_new_session,
    load_active_agent,
    load_session,
    reset_all_agents,
    save_active_agent,
    save_session,
)
from oc_interactive.slash import (
    SlashKind,
    is_dump_command,
    is_slash_command,
    parse_slash_command,
)
from oc_interactive.ssh_tunnel import ensure_ssh_tunnel
from oc_interactive.turn import (
    VoiceSettings,
    build_payload,
    resolve_config_path,
    resolve_voice,
    voice_from_session,
)
from oc_interactive.paths import default_config_path, state_dir
from oc_interactive.tts_defaults import DEFAULT_LANGUAGE, DEFAULT_SPEAKER, DEFAULT_TTS_MODEL


def _parse_agent_switch(text: str) -> str | None:
    """Return the requested agent name for '/agent <name>', '' for bare
    '/agent', or None if this isn't the (chat-app-only) /agent command."""
    stripped = text.strip()
    if stripped == "/agent":
        return ""
    if stripped.lower().startswith("/agent "):
        return stripped[len("/agent ") :].strip()
    return None


def _export_history(*, agent: str) -> Path:
    """Write the current session as JSON under exports/ and return the path.

    Chat-app equivalent of ``cli._handle_dump``: that CLI helper prints JSON
    to stdout, which would corrupt a full-screen Textual app, so this writes
    to a file instead.
    """
    session = load_session(agent)
    doc = session.dump_document(agent=agent)
    exports_dir = state_dir() / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = exports_dir / f"{stamp}.json"
    dest.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return dest


class ChatBubble(Static):
    """One chat turn, aligned/colored by who said it."""

    DEFAULT_CSS = """
    ChatBubble {
        width: 100%;
        margin: 0 1;
        padding: 0 1;
    }
    ChatBubble.user {
        content-align: right middle;
        color: cyan;
        text-style: bold;
    }
    ChatBubble.agent {
        content-align: left middle;
    }
    ChatBubble.system {
        content-align: center middle;
        color: grey;
        text-style: italic;
    }
    ChatBubble.error {
        content-align: center middle;
        color: red;
        text-style: bold;
    }
    """


class ChatApp(App):
    """Long-running chat window for one oc-interactive session."""

    TITLE = "oc-interactive"

    CSS = """
    #message-log {
        height: 1fr;
        padding: 1 2;
    }
    #chat-input {
        dock: bottom;
        margin: 0 2 1 2;
    }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(
        self,
        *,
        cfg: OpenClawConfig,
        config_path: Path,
        voice: VoiceSettings,
        agent: str,
        debug: bool,
    ) -> None:
        super().__init__()
        self.cfg = cfg
        self.config_path = config_path
        self.voice = voice
        self.current_agent = agent
        self.oc_debug = debug
        self._turn_in_flight = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(id="message-log")
        yield Input(placeholder=self._input_placeholder(), id="chat-input")

    def _input_placeholder(self) -> str:
        return f"Message {self.current_agent}… (/help for commands)"

    def _update_input_placeholder(self) -> None:
        self.query_one("#chat-input", Input).placeholder = self._input_placeholder()

    def _prefill_input(self, text: str) -> None:
        """Fill the chat input with ``text`` and place the cursor at the end.

        Used after a bare ``/voice-design`` so the current description is
        ready to tweak instead of having to be retyped from scratch.
        """
        input_widget = self.query_one("#chat-input", Input)
        input_widget.value = text
        input_widget.cursor_position = len(text)

    def on_mount(self) -> None:
        self.sub_title = f"agent: {self.current_agent}"
        self._render_history()
        self.query_one("#chat-input", Input).focus()

    def action_quit(self) -> None:
        self.exit()

    # -- rendering -----------------------------------------------------

    def _message_log(self) -> VerticalScroll:
        return self.query_one("#message-log", VerticalScroll)

    def _render_history(self) -> None:
        session = load_session(self.current_agent)
        log = self._message_log()
        for msg in session.messages:
            role = msg.get("role")
            content = msg.get("content")
            if not isinstance(content, str) or not content:
                continue
            if role == "user":
                log.mount(ChatBubble(content, classes="user"))
            elif role == "assistant":
                agent_name = msg.get("agent") or self.current_agent
                log.mount(ChatBubble(f"{agent_name}: {content}", classes="agent"))
        log.scroll_end(animate=False)

    def _append_bubble(self, text: str, *, classes: str) -> None:
        log = self._message_log()
        log.mount(ChatBubble(text, classes=classes))
        log.scroll_end(animate=False)

    def _clear_message_log(self) -> None:
        self._message_log().remove_children()

    def _set_waiting(self, waiting: bool) -> None:
        self._turn_in_flight = waiting
        input_widget = self.query_one("#chat-input", Input)
        input_widget.disabled = waiting
        if waiting:
            self.sub_title = f"waiting for {self.current_agent}…"
        else:
            self.sub_title = f"agent: {self.current_agent}"
            input_widget.focus()

    # -- input handling --------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if self._turn_in_flight:
            return
        text = event.value.strip()
        if not text:
            return

        input_widget = self.query_one("#chat-input", Input)
        input_widget.value = ""

        agent_request = _parse_agent_switch(text)
        if agent_request is not None:
            self._handle_agent_switch(agent_request)
            return

        if is_slash_command(text):
            slash = parse_slash_command(text)
            if slash and is_dump_command(slash):
                dest = _export_history(agent=self.current_agent)
                self._append_bubble(f"History exported → {dest}", classes="system")
                return
        else:
            self._append_bubble(text, classes="user")

        self._set_waiting(True)
        self._run_turn(text)

    def _handle_agent_switch(self, name: str) -> None:
        if not name:
            self._append_bubble("Usage: /agent <name>", classes="error")
            return
        try:
            resolved = self.cfg.resolve_agent(name)
        except ValueError as e:
            self._append_bubble(str(e), classes="error")
            return
        self.current_agent = resolved
        save_active_agent(resolved)
        # Sessions are per-agent: switching agents means switching to that
        # agent's own conversation and cached voice, not just relabeling the
        # one on screen -- so the visible log is replaced with its history.
        self._clear_message_log()
        self._render_history()
        self.voice = voice_from_session(load_session(resolved))
        self.sub_title = f"agent: {self.current_agent}"
        self._update_input_placeholder()
        self._append_bubble(f"Switched to agent {resolved}.", classes="system")

    # -- daemon round-trip -------------------------------------------------

    def _run_turn(self, text: str) -> None:
        self.run_worker(
            lambda: self._turn_worker(text),
            thread=True,
            exclusive=True,
            group="turn",
        )

    def _turn_worker(self, text: str) -> None:
        slash = parse_slash_command(text) if is_slash_command(text) else None
        is_new_session = slash is not None and slash.kind == SlashKind.NEW_SESSION

        payload = build_payload(
            text,
            voice=self.voice,
            agent=self.current_agent,
            config_path=self.config_path,
            token=self.cfg.token,
            debug=self.oc_debug,
            quiet=True,
            extra_help_commands=["agent"],
        )

        def on_event(msg: dict) -> None:
            if msg.get("event") != "ttsText":
                return
            text_out = msg.get("ttsText")
            if not isinstance(text_out, str) or not text_out:
                return
            if msg.get("isError"):
                self.call_from_thread(self._append_bubble, text_out, classes="error")
            elif slash is not None:
                self.call_from_thread(self._append_bubble, text_out, classes="system")
            else:
                self.call_from_thread(
                    self._append_bubble,
                    f"{self.current_agent}: {text_out}",
                    classes="agent",
                )

        try:
            resp = send_request(payload, timeout=None, on_event=on_event)
        except Exception as e:  # noqa: BLE001 - surfaced to the user as a bubble
            self.call_from_thread(self._append_bubble, f"Connection error: {e}", classes="error")
            resp = None

        if resp is not None and resp.get("ok"):
            if is_new_session:
                self.call_from_thread(self._clear_message_log)
            # Re-sync from session.json: slash commands like /voice-design
            # change the voice server-side, and self.voice must not keep
            # reusing the snapshot resolved once at startup, or later turns
            # (and a later bare /voice-design) would silently use stale data.
            self.voice = voice_from_session(load_session(self.current_agent))
            prefill = resp.get("voiceDesignPrefill") or resp.get("filterPrefill")
            if isinstance(prefill, str) and prefill:
                self.call_from_thread(self._prefill_input, prefill)

        self.call_from_thread(self._set_waiting, False)


def _build_parser() -> argparse.ArgumentParser:
    class Parser(argparse.ArgumentParser):
        def error(self, message: str) -> None:
            self.exit(2, f"error: {message}\n")

    p = Parser(
        prog="oc-interactive-chat",
        description=(
            "Chat UI for oc-interactive: OpenClaw agent + Qwen3-TTS (mlx-audio), "
            "rendered as a scrolling dialogue with a bottom input bar. A single "
            "background daemon still orchestrates TTS across turns."
        ),
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
        help=f"Language for TTS (default from config ttsLanguage, else {DEFAULT_LANGUAGE}).",
    )
    p.add_argument(
        "-m",
        "--model",
        default=None,
        help=f"Qwen3-TTS mlx-audio model id or local path (default: {DEFAULT_TTS_MODEL}).",
    )
    p.add_argument(
        "--agent",
        default=None,
        help="OpenClaw agent short name (main, news, eileen). Default from config. "
        "Switch mid-session with /agent <name>.",
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
            "Reset every agent's session (archiving each one first if it has "
            "messages) and reload config defaults. Clears all agents' "
            "sessions, not just the current one."
        ),
    )
    p.add_argument(
        "--new",
        action="store_true",
        help=(
            "Archive the current agent's session (if it has messages) and "
            "start a new one for that agent before opening the chat. Other "
            "agents' sessions are untouched."
        ),
    )
    p.add_argument(
        "--debug",
        action="store_true",
        help="Log timing and TTS model cache status (or set OC_INTERACTIVE_DEBUG=1).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Config must load before --new/--init can act: --init needs the full
    # agent list, --new needs cfg.resolve_agent to pick which agent's
    # session to touch.
    config_path = resolve_config_path(args)
    try:
        cfg = load_config(config_path)
    except (FileNotFoundError, ValueError) as e:
        eprint(f"error: {e}")
        return 1

    if args.init:
        archived = reset_all_agents(list(cfg.agents))
        for agent_name, archive_path in archived.items():
            if archive_path:
                eprint(f"[oc-interactive-chat] archived {agent_name} session → {archive_path}")
        eprint("[oc-interactive-chat] cleared sessions for all agents")
        default_agent = cfg.default_agent
        session = load_session(default_agent)
        session.last_openclaw_config = str(config_path)
        save_session(session, default_agent)
        save_active_agent(default_agent)

    if args.new:
        try:
            new_agent = cfg.resolve_agent(args.agent or load_active_agent())
        except ValueError as e:
            eprint(f"error: {e}")
            return 1
        session, archived = archive_and_new_session(new_agent, keep_system_prompt=True)
        if archived:
            eprint(f"[oc-interactive-chat] archived session → {archived}")
        eprint(f"[oc-interactive-chat] new session {session.user_id} (agent {new_agent})")
        save_active_agent(new_agent)

    try:
        ensure_ssh_tunnel(cfg, debug=args.debug)
    except RuntimeError as e:
        eprint(f"error: {e}")
        return 1

    try:
        # Fall back to the agent the last turn (anywhere) was talking to, so
        # restoring on restart doesn't silently snap back to the config's
        # default agent.
        agent = cfg.resolve_agent(args.agent or load_active_agent())
    except ValueError as e:
        eprint(f"error: {e}")
        return 1
    save_active_agent(agent)

    session = load_session(agent)
    try:
        voice = resolve_voice(args, cfg, session)
    except CliError as e:
        eprint(f"error: {e}")
        return 1

    app = ChatApp(cfg=cfg, config_path=config_path, voice=voice, agent=agent, debug=args.debug)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
