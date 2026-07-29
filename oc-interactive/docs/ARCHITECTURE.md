# oc-interactive — Architecture

This document describes the internal architecture of `oc-interactive` as of the
`feature/oc-interactive-qwen-tts-dev` branch: a macOS CLI/TUI that sends text to
an [OpenClaw](https://docs.openclaw.ai/) agent and speaks the reply through
speakers using Qwen3-TTS via [mlx-audio](https://github.com/Blaizzy/mlx-audio).
It complements the [README](../README.md), which is the user-facing reference;
this doc focuses on *how the pieces fit together* and *why*.

## Goals and constraints

- **Each CLI invocation is a separate short-lived process** (`oc-interactive -t
  "..."`), so anything that must survive between turns (conversation history,
  cached voice settings, a loaded TTS model) has to live outside that process.
- **Model load is expensive** (seconds to minutes for Qwen3-TTS), so synthesis
  must not pay that cost on every turn.
- **Multiple "personas"** (`main`, `news`, `eileen`, ...) must be able to run
  concurrently or be switched between without cross-talk in conversation
  history, voice, or filter settings.
- **A long-running chat UI** (Textual) must share all of the above with the
  one-shot CLI instead of duplicating logic.

These constraints drive the two-daemon design below.

## Process topology

```
                    ┌─────────────────────┐
   oc-interactive   │  orchestration       │        ┌───────────────────┐
   (one-shot CLI)   │  daemon              │        │  OpenClaw gateway  │
        │           │  (daemon.py)         │───────▶│  /v1/chat/completions
        │  UDS      │                       │        └───────────────────┘
        ▼           │  - slash commands     │
┌───────────────┐   │  - session state       │        ┌───────────────────┐
│ oc-interactive-│──▶│  - filter routing      │───────▶│  local filter LLM  │
│ chat (Textual) │   │  - TTS orchestration   │        │  (LM Studio, etc.) │
└───────────────┘   └──────────┬────────────┘        └───────────────────┘
                                │  UDS
                                ▼
                     ┌──────────────────────┐
                     │  Qwen3-TTS daemon     │
                     │  (qwen_tts_daemon.py) │──▶ afplay (client-side)
                     │  - cached mlx-audio   │
                     │    model in memory    │
                     └──────────────────────┘
```

Two background daemons, both auto-started on demand and both self-terminating
after 30 minutes idle:

1. **Orchestration daemon** — `python -m oc_interactive --daemon`
   (`daemon.py::run_daemon`). Owns OpenClaw chat, slash-command handling,
   session persistence, the local text-filter, and playback (`afplay`). One
   Unix domain socket, one connection per turn.
2. **TTS daemon** — `python -m oc_interactive.qwen_tts_daemon`
   (`qwen_tts_daemon.py::run_daemon`). Owns exactly one cached mlx-audio
   `model` object in memory (`_TTSSession.ensure_model`), keyed by model id/
   path. A second Unix domain socket. The orchestration daemon is its only
   client (`tts.py::synthesize_and_play` → `tts_daemon.py::synthesize_to_wav`).

Both daemons are lazily spawned and health-checked with the same pattern
(pid file + liveness check via `os.kill(pid, 0)` + stale-socket cleanup +
`subprocess.Popen(..., start_new_session=True)` + poll until the socket
accepts connections): see `client.py::ensure_daemon_running` and
`tts_daemon.py::ensure_tts_daemon_running`. Splitting them into two daemons
means a TTS-only slowdown/crash doesn't take down agent chat and vice versa,
and the (heavier, GPU/Metal-resident) TTS process can be restarted
independently (`pkill -f qwen_tts_daemon`) after code changes without losing
conversation state.

### IPC framing

Both sockets use the same trivial length-prefixed JSON framing
(`ipc.py::send_json`, duplicated inline in `client.py`/`daemon.py` for the
orchestration socket): a 4-byte big-endian length header followed by a UTF-8
JSON body. The orchestration protocol additionally supports **event frames**
sent before the final response — the daemon can emit `{"event": "ttsText",
...}` as soon as text is ready to speak, then a terminating frame containing
`"ok"`, so a client can print/render the reply before audio playback (which
can take a few seconds) finishes. Both `cli.py::main` and
`chat_app.py::_turn_worker` register an `on_event` callback with
`client.py::send_request` for this.

## Entry points

| Entry point | Module | Nature |
|---|---|---|
| `oc-interactive` | `cli.py::main` | One-shot: parse args → resolve config/agent/voice → build payload → send one request → print/exit |
| `oc-interactive-chat` | `chat_app.py::main` | Long-running Textual app: resolve config/agent/voice once at startup, then one request per submitted line for the life of the process |
| `--daemon` flag | `daemon.py::main` | Internal; spawned by `client.py`, not invoked directly by users |
| `--tts-daemon` flag | `qwen_tts_daemon.py::main` | Internal; spawned by `tts_daemon.py` |

`cli.py` and `chat_app.py` share almost all resolution logic through
`turn.py` (`resolve_config_path`, `resolve_voice`, `voice_from_session`,
`build_payload`) so the two front-ends can never disagree about precedence
rules. The one meaningful difference: the CLI resolves voice **once** per
process (it *is* one turn), while the chat UI re-derives `self.voice` from
`session.json` after every turn (`turn.py::voice_from_session`) because a
slash command mid-session (e.g. `/voice-design`) changes state on the
daemon side that a purely in-memory snapshot would miss.

## Configuration and precedence

`config.py::load_config` reads `oc-interactive.json` (default path
`~/.config/oc-interactive/oc-interactive.json`, override with `-c`/`--config`
or `OC_INTERACTIVE_STATE_DIR`) into an immutable `OpenClawConfig`. Values in
this file are **defaults only** — never mutated automatically except by the
explicit `/save-config` slash command, which always targets the *default*
config path regardless of which `-c` file is active for the current session
(see `config.py::write_default_config_snapshot`).

Effective settings for a given turn are resolved through three layers of
precedence, poorest → strongest:

```
config file  <  per-agent session.json cache  <  CLI flags / slash commands
```

- **Config** supplies the durable default (`ttsModel`, `ttsSpeaker`,
  `filterEnabled`, `defaultAgent`, ...).
- **Session cache** (`session.json` per agent) remembers whatever won on the
  *last* successful turn for that agent, so subsequent turns can omit flags.
- **CLI flags / slash commands** override for that turn (and are then cached
  back into the session by `daemon.py::_cache_tts_paths`).

`turn.py::resolve_voice` implements this precedence for voice settings
specifically (mode, model, speaker, instruct, voice design, clone
reference); `daemon.py::_effective_filter_enabled`/`_effective_filter_prompt`
implement the analogous rule for the text filter (session override wins,
else config default).

## Session model (per-agent state)

Each OpenClaw agent (`main`, `news`, `eileen`, ...) has a fully independent
`Session` (`session.py`), persisted at
`~/.config/oc-interactive/agents/<agent>/session.json`:

- Conversation history (`messages`: role/content/timestamp/agent).
- System prompt.
- Cached voice settings (`last_voice_mode`, `last_tts_model`, `last_speaker`,
  `last_instruct`, `last_voice_design`, `last_refaudio`/`last_reftext`,
  `last_language`).
- Cached config path (`last_openclaw_config`) — so a later invocation that
  omits `-c` still resolves the right config, before that config is even
  loaded (`turn.py::resolve_config_path`).
- Filter override (`filter_enabled`, `filter_prompt`) — `None` means "defer
  to config default"; an explicit `/filter on|off|<description>` sets it.

A separate one-line pointer file, `active_agent.json`
(`session.py::save_active_agent`/`load_active_agent`), records which agent
was last talked to by *any* invocation (CLI or chat UI), so relaunching with
no `--agent` resumes the same agent/conversation rather than snapping back to
`defaultAgent`. This pointer is shared across every `-c`/`--config` file; if
it names an agent not in the *current* config's `agents` list,
`OpenClawConfig.resolve_agent_or_default` silently falls back to that
config's own `defaultAgent` (an explicit invalid `--agent` still errors
loudly).

Two distinct reset semantics map onto this model:

- **`--new` / `/new`** (`session.py::archive_and_new_session`): archive the
  *current* agent's session if non-empty, start a blank one for that agent,
  keep system prompt + voice + filter cache. Other agents untouched.
- **`--init`** (`session.py::reset_all_agents`): archive + fully blank
  *every* agent's session (no cache carried over) and clear the active-agent
  pointer, so the next launch falls back to config defaults.

A one-time migration (`session.py::_migrate_legacy_session`) moves a
pre-per-agent flat `session.json` into `agents/<agent>/session.json` the
first time any command runs after upgrade, then renames the old file to
`session.json.migrated`.

## Request lifecycle (one chat turn)

1. **`cli.py::main`** reads text (stdin wins over `-t`), loads config
   (`turn.py::resolve_config_path` → `config.py::load_config`), ensures the
   config's optional SSH tunnel is healthy (`ssh_tunnel.py::ensure_ssh_tunnel`
   — see below), resolves the agent
   (`OpenClawConfig.resolve_agent_or_default`, falling back to
   `active_agent.json`), loads that agent's session, resolves voice
   (`turn.py::resolve_voice`), and builds a JSON payload
   (`turn.py::build_payload`).
2. **`client.py::send_request`** ensures the orchestration daemon is running,
   then opens a UDS connection and sends the payload, registering an
   `on_event` callback for streamed `ttsText` events.
3. **`daemon.py::_process_request`** loads the config fresh from the payload's
   `openclawConfig` path (daemon is stateless between requests; all state is
   file-based), resolves the agent, updates `active_agent.json`, and either:
   - routes to **`_handle_slash`** if `text` starts with `/`, or
   - routes to **`_handle_chat`** otherwise.
4. **`_handle_chat`**: builds OpenAI-style messages from session history
   (`session.py::build_api_messages`), calls the OpenClaw gateway
   (`openclaw.py::chat_completion`, a plain `/v1/chat/completions` POST with
   a bearer token), appends both the user and assistant turns to the session,
   and calls **`_speak`**.
5. **`_speak`**: emits the full text back to the client immediately via the
   `ttsText` event (so stdout/chat UI can render before audio finishes),
   then — unless this is a slash-command confirmation (`synthesize=False`) —
   derives what actually gets vocalized:
   - **bracket rule** (`speakable.py::select_tts_text`): if the *first line*
     of the text is wrapped in `[...]`, only that line (unbracketed) is
     spoken; otherwise the full text is spoken.
   - **text filter** (`llm_filter.py::filter_text`, only for real agent
     replies, never for slash confirmations or agent-error lines): if
     enabled, the (bracket-selected) text is POSTed to a local OpenAI-
     compatible `/v1/chat/completions` endpoint (e.g. LM Studio) with the
     stored filter system prompt, and its reply becomes the spoken text. A
     filter failure (`FilterError`) falls back to unfiltered text rather than
     blocking speech.
   - the resulting text is handed to **`tts.py::synthesize_and_play`**.
6. **`tts.py::synthesize_and_play`** calls the TTS daemon over its own UDS
   (`tts_daemon.py::synthesize_to_wav`), writes the response to a temp WAV,
   and plays it with `afplay` (blocking on that subprocess).
7. **`qwen_tts_daemon.py::_TTSSession.synthesize`** loads (or reuses) the
   mlx-audio model for the requested model id, calls the appropriate
   generation method for the mode (`generate_custom_voice` /
   `generate_voice_design` / `generate` for clone), concatenates any
   streamed audio chunks, and writes a WAV (`mlx_audio.audio_io.write`).
8. Control returns up the stack; `daemon.py::_cache_tts_paths` has already
   persisted the effective voice/config/agent onto the session so the next
   turn can omit flags.

Throughout, **only real chat replies and agent-error lines are ever spoken**;
slash-command confirmations are always displayed as text (stdout / chat
transcript) but never handed to the TTS engine (`_speak(..., synthesize=False)`).
Display text (stdout, chat transcript, `session.json`) always shows the full,
unmodified text regardless of what subset the bracket rule or filter chose to
actually vocalize — the transformations are audio-only and never affect what
is stored or printed.

## Slash commands

`slash.py::parse_slash_command` turns a leading `/` into a `SlashCommand`
(`kind`, `value`, `raw_verb`), matching against a fixed verb table (longest
match first, e.g. `clean all` before `clear`). Three verbs
(`SET_SYSTEM_PROMPT`, `SET_VOICE_DESIGN`, `SET_FILTER`) accept a
value that can span multiple lines (same-line text plus following lines),
since e.g. `/system prompt` needs a real multi-line body.

`daemon.py::_handle_slash` is the single dispatcher for all of these, shared
by both entry points (the CLI sends slash text through the same daemon
request path as a chat turn; only `/dump`/`/history` is special-cased locally
in `cli.py`/`chat_app.py` since it needs to write to stdout/a file rather
than be spoken):

| Kind | Behavior |
|---|---|
| `NEW_SESSION` (`/new`, `/clear`, `/clean all`) | `archive_and_new_session`, keep system prompt/voice/filter |
| `SET_SYSTEM_PROMPT` | Set/clear `session.system_prompt` |
| `SET_VOICE_DESIGN` | Bare: cite current description (+ chat-UI prefill). `reset`/`default`: revert to config's `ttsVoiceDesign` or CustomVoice. Else: switch to VoiceDesign mode, auto-selecting a matching checkpoint (`turn.py::ensure_model_matches_mode`) unless `-m` was explicit |
| `SET_FILTER` | Bare: report effective on/off + description (+ prefill). `on`/`off`: toggle `session.filter_enabled`. Else: set `session.filter_prompt` (with `\n` un/re-escaping for the chat UI's single-line input box) |
| `SAVE_CONFIG` (`/save-config`) | **Replace** (not merge) the default config file with a full snapshot of the active config's raw JSON, with `defaultAgent`/`ttsLanguage`/`filterEnabled`/`filterPrompt`/(`ttsVoiceDesign` if active) overridden by current effective session values (`config.py::write_default_config_snapshot`) |
| `HELP` | Text-only command summary; chat UI appends `/agent` via `extraHelpCommands` |
| `STATUS` | Session summary (agent, message count, system-prompt state) |
| `DUMP_HISTORY` (`/dump`, `/dump all`, `/history`) | Handled locally by each entry point, not the daemon: CLI prints JSON to stdout (`cli.py::_handle_dump`), chat UI writes to `~/.config/oc-interactive/exports/<timestamp>.json` (`chat_app.py::_export_history`) since a full-screen TUI can't print to stdout mid-session |
| `UNKNOWN` | Errors out (spoken as an agent-error line, raised as an exception to the daemon frame) |

Every slash-command path that mutates state re-persists cached voice/config
fields via `_cache_tts_paths` before speaking its confirmation, so the
session file stays the single source of truth even for state changes that
don't touch an agent.

## Voice modes

`tts_defaults.py` defines three mutually exclusive modes and one default
checkpoint per mode (`DEFAULT_MODEL_BY_MODE`):

| Mode | Trigger | Required fields | Model family |
|---|---|---|---|
| `custom_voice` (default) | `--speaker`/`--instruct` or config `ttsSpeaker` | `speaker` (+ optional `instruct`) | CustomVoice |
| `clone` | `--refaudio` + `--reftext` | `refaudio`, `reftext` | Base |
| `voice_design` | `--voice-design` or config `ttsVoiceDesign` | `voice_design` description | VoiceDesign |

`turn.py::infer_qwen_model_kind` sniffs a model id/path string for
`customvoice`/`voicedesign`/`base` substrings; `ensure_model_matches_mode`
auto-swaps in the matching HF default when the resolved mode and the cached/
configured checkpoint disagree (unless the user passed `-m` explicitly, in
which case a mismatch is left alone so mlx-audio can raise a clear error).
`turn.py::is_mlx_audio_compatible_model` additionally rejects stale
`dots.tts`/`dots_tts` checkpoints left over from a pre-Qwen3-TTS version of
this tool, falling back to config/default instead of trying to load an
incompatible model type.

Config's `ttsVoiceDesign` **takes priority over** `ttsSpeaker` when both are
set (`turn.py::resolve_voice`), mirroring how an explicit `/voice-design`
session override takes priority over `/filter`'s config default — "the more
specific / more recently set thing wins."

## SSH tunnel management

`ssh_tunnel.py` gives configs an optional declarative `ssh` block (host,
user, identity file, local/remote port) for gateways that live on another
machine. Both `cli.py::main` and `chat_app.py::main` call
`ensure_ssh_tunnel(cfg, debug=...)` on **every invocation**, immediately
after loading the config — not just at session start — so a dropped tunnel
self-heals on the next turn:

- Healthy (state file matches spec, pid alive, port responds) → no-op.
- Stale (pid dead, or host/user/identity/target changed) → tear down, then
  respawn `ssh -N -L <local>:<remoteHost>:<remotePort> [user@]host`.
- Port already bound by something un-managed → hard error rather than
  silently proceeding against an unreachable/wrong gateway.

State (`ssh-tunnel-<port>.json`) and logs (`ssh-tunnel-<port>.log`) are keyed
by `localPort` so multiple configs with different tunnels can coexist.
`--ssh-tunnel-status`/`--ssh-tunnel-teardown` (`cli.py::_handle_ssh_tunnel_flags`)
expose manual inspection/control without going through a full chat turn.

## Chat UI (Textual)

`chat_app.py::ChatApp` is a single long-running process built on
[Textual](https://github.com/Textualize/textual): a `VerticalScroll` message
log of `ChatBubble` widgets (right-aligned/cyan for user, left-aligned for
agent, centered/grey for system messages, centered/red for errors) plus a
bottom-docked `Input`. It resolves config/agent/voice once at startup exactly
like the CLI (sharing `turn.py`), then re-uses the *same* orchestration
daemon over the *same* IPC protocol for every subsequent turn — it is a
visual layer on top of the daemon, not an alternate backend.

Key differences from the one-shot CLI, all consequences of being
long-running rather than architectural divergence:

- Turns run in a background worker thread (`run_worker(..., thread=True,
  exclusive=True, group="turn")`) so the Textual event loop stays responsive;
  UI updates from that thread go through `call_from_thread`.
- `self.voice` is **not** trusted as a permanent cache: after every
  successful turn it's rebuilt from the agent's `session.json`
  (`turn.py::voice_from_session`), since slash commands mutate voice
  server-side and a stale in-memory snapshot would silently diverge.
- `/agent <name>` is a **chat-UI-only, purely local** command
  (`_handle_agent_switch`): it validates against `cfg.resolve_agent`, updates
  `active_agent.json`, swaps `self.current_agent`, and replaces the on-screen
  log with that agent's own history — it's never sent to the daemon.
- Bare `/voice-design` / `/filter` additionally pre-fill the input box with
  the current description (`_prefill_input`) using the `voiceDesignPrefill`/
  `filterPrefill` fields the daemon returns, so the one-shot CLI (no input
  box to prefill) and the chat UI diverge only in that cosmetic way.
- `/dump`/`/history` write to `~/.config/oc-interactive/exports/` instead of
  stdout, since a full-screen app can't safely interleave raw output.

## State layout on disk

All state lives under `~/.config/oc-interactive/` (override with
`OC_INTERACTIVE_STATE_DIR`; see `paths.py`):

```
oc-interactive.json              default config (mutated only by /save-config)
agents/<agent>/session.json      per-agent conversation + cached voice/filter/config
agents/<agent>/sessions/         per-agent archives (--new / /new / --init)
active_agent.json                pointer: which agent's session is "current"
session.json.migrated            pre-per-agent flat session, renamed after migration
exports/<timestamp>.json         chat-UI /dump output
daemon.sock / daemon.pid / daemon.log            orchestration daemon
tts-daemon.sock / tts-daemon.pid / tts-daemon.log  Qwen3-TTS daemon
ssh-tunnel-<port>.json / .log    per-port managed SSH tunnel bookkeeping
```

`paths.py::_agent_slug` normalizes agent names (lowercase, strip
`openclaw/` prefix, sanitize path-hostile characters) into directory names,
mirrored by `OpenClawConfig.resolve_agent`'s own normalization so a session
can be located before a config is even loaded.

## Idle lifecycle and failure handling

- Both daemons share the same 30-minute idle timeout (`client.py::
  IDLE_TIMEOUT_SEC`) and shut themselves down when unused; the next
  invocation transparently respawns them.
- `client.py::_cleanup_stale_daemon` / `tts_daemon.py::_cleanup_stale_tts_daemon`
  detect a dead pid with a leftover socket file (or vice versa) and clean up
  before respawning, so a crashed daemon self-heals on the next call instead
  of requiring manual `rm`.
- OpenClaw failures (`OpenClawError`, unauthorized, connection errors,
  malformed replies) and TTS failures (`TTSError`) are turned into a spoken
  line via `speakable.py::agent_error_line` and reported on **stderr only**
  (never stdout) — an agent failure is heard, not silently swallowed, but
  doesn't pollute `-v`/stdout output or get cached as a "real" reply that
  would confuse the next turn's conversation history in a misleading way
  (it *is* still appended to `session.json` as an assistant message, for a
  faithful conversational record — see `daemon.py::_handle_chat`).
- The one-shot CLI treats a socket-level timeout distinctly from other
  errors, warning that audio may still be playing server-side even though
  the client gave up waiting (`cli.py::main`, `except (TimeoutError,
  socket.timeout)`).

## Text filter (optional local LLM pass)

`llm_filter.py::filter_text` is a small, independent client for any
OpenAI-compatible `/v1/chat/completions` endpoint (default: an LM Studio
instance running a small local model, see `filter_defaults.py`). It exists
to let the *spoken* audio diverge from the agent's actual reply — for tone
rewriting, redaction, simplification — without touching what's stored/
displayed. It is deliberately kept separate from `openclaw.py` (no auth
token, no conversation/user-id state, no retry/backoff needed since a
failure just falls back to unfiltered speech). On/off state and the prompt
persist per-agent in `session.json` exactly like the system prompt, with the
config-file value as the fallback default when a session hasn't explicitly
toggled it.

## Experimental / not-yet-integrated

Two standalone scripts at the top of `oc-interactive/` (`capture_whisper_stream.py`,
`use_whisper_server.py`) are early prototypes for a voice **input** path
(streaming microphone audio into a local [whisper.cpp](https://github.com/ggml-org/whisper.cpp)
process or server to get transcribed text) — the natural counterpart to the
existing Qwen3-TTS voice **output** path. Neither is imported by the
`oc_interactive` package or wired into the daemons/CLI yet; they're
free-standing scripts for manual experimentation, along with a couple of
example prompt files under `examples/`.

## Key modules at a glance

| Module | Responsibility |
|---|---|
| `cli.py` | One-shot entry point, arg parsing, request/response glue |
| `chat_app.py` | Textual chat UI entry point and widget/worker logic |
| `daemon.py` | Orchestration daemon: request dispatch, slash commands, chat turns, speak pipeline |
| `client.py` | Orchestration-daemon lifecycle (spawn/health-check) + `send_request` |
| `tts_daemon.py` | TTS-daemon lifecycle (spawn/health-check) + `synthesize_to_wav` client |
| `qwen_tts_daemon.py` | TTS daemon process: cached mlx-audio model, generation dispatch by mode, WAV writing |
| `tts.py` | Calls the TTS daemon, writes a temp WAV, plays it with `afplay` |
| `turn.py` | Shared CLI/chat-UI resolution: voice precedence, config path, payload building |
| `config.py` | `oc-interactive.json` parsing/validation, `/save-config` snapshot writer |
| `session.py` | Per-agent `Session` persistence, archiving, active-agent pointer, legacy migration |
| `slash.py` | Slash-command parsing and generic confirmation text |
| `speakable.py` | Bracket-selection rule, UTF-8 validation, agent-error phrasing |
| `llm_filter.py` | Optional local-LLM text filter client |
| `openclaw.py` | OpenClaw gateway chat-completions client |
| `ssh_tunnel.py` | Declarative SSH port-forward tunnel management |
| `ipc.py` | Shared length-prefixed JSON socket framing |
| `paths.py` | State-directory layout and agent-name normalization |
| `tts_defaults.py` / `filter_defaults.py` | Shared default constants |
