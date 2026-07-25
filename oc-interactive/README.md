# oc-interactive

Python CLI that sends text to an [OpenClaw](https://docs.openclaw.ai/) agent and speaks the reply through your speakers using [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS) via [mlx-audio](https://github.com/Blaizzy/mlx-audio).

Each conversational turn is a **separate shell invocation**. Two background daemons auto-start:

1. **Orchestration daemon** (`oc_interactive --daemon`) — OpenClaw chat, slash commands, session state, afplay.
2. **TTS daemon** (`oc_interactive.qwen_tts_daemon`) — cached mlx-audio Qwen3-TTS model for synthesis.

## Requirements

- macOS on Apple Silicon
- Python 3.11+
- OpenClaw gateway reachable — if remote, add an `ssh` block to your config and oc-interactive will establish/repair the tunnel automatically (see [SSH tunnel management](#ssh-tunnel-management))
- `ELLO_GATEWAY_TOKEN` environment variable

## Install

```bash
cd oc-interactive
make install
```

This creates `.venv/`, installs `oc-interactive` plus `mlx-audio[tts]`, and puts the command at `.venv/bin/oc-interactive`.

## How to run

**First launch of a model is slow — how slow depends on whether the weights are already on disk:**

| Case | What happens | Typical wait |
|------|----------------|--------------|
| **Model not cached yet** | mlx-audio downloads several GB from Hugging Face, then loads into the TTS daemon | Often **many minutes** (network + load) |
| **Model already on disk** (HF cache or local `-m` path), but daemon has not loaded it yet | Load weights into Metal memory only — no download | Still **slow** (often tens of seconds to a couple of minutes), but much faster than a cold download |
| **Later turns** (same model, daemon still running) | Synthesis only | Fast |

Switching modes (e.g. CustomVoice → VoiceDesign) can hit either of the first two cases for the other checkpoint. See [Model caching (performance)](#model-caching-performance) for daemon idle reload details.

**Option A — launcher script (easiest, no PATH changes):**

```bash
cd oc-interactive
./run --debug -t "Hello" --speaker Ryan --instruct "friendly and upbeat"
```

**Option B — full path:**

```bash
oc-interactive/.venv/bin/oc-interactive -t "Hello" --speaker Ryan
```

**Option C — activate the venv:**

```bash
cd oc-interactive
source .venv/bin/activate
oc-interactive -t "Hello" --speaker Ryan
deactivate
```

**Option D — make run:**

```bash
cd oc-interactive
make run ARGS='-t "Hello" --speaker Ryan --instruct "calm and warm"'
```

**Option E — stdin (omit `-t`):**

```bash
echo "Hello" | oc-interactive --speaker Ryan
```

Piped stdin wins over `-t` when both are present.

## Configuration

Copy the example config:

```bash
mkdir -p ~/.config/oc-interactive
cp config/oc-interactive.example.json ~/.config/oc-interactive/oc-interactive.json
```

Override the config path with `-c` / `--config` (default: `~/.config/oc-interactive/oc-interactive.json`):

```bash
oc-interactive -c /path/to/oc-interactive.json -t "Hello" --speaker Ryan
```

Edit paths as needed. Example:

```json
{
  "openclawBaseURL": "http://127.0.0.1:18789",
  "openclawToken": "$ELLO_GATEWAY_TOKEN",
  "defaultAgent": "main",
  "agents": ["main", "news", "eileen"],
  "ttsModel": "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit",
  "ttsSpeaker": "Ryan",
  "ttsInstruct": null,
  "ttsLanguage": "English",
  "ttsVoiceDesign": null
}
```

- `openclawToken`: use `$VAR_NAME` to read from the environment.
- `agents`: allowlist validated against `--agent` (CLI value is prefixed with `openclaw/` automatically).
- `ttsModel`: Hugging Face model id or local path (default CustomVoice 8-bit). Use a VoiceDesign model when `ttsVoiceDesign` is set.
- `ttsSpeaker`: default CustomVoice speaker when `--speaker` is omitted.
- `ttsInstruct`: optional default emotion/style instruction for CustomVoice (used when `--instruct` is omitted).
- `ttsLanguage`: TTS language code (default `English`). Qwen3-TTS supports language ids such as `English`, `Chinese`, `Japanese`, `Korean`, `German`, `French`, `Spanish`, `Italian`, `Portuguese`, `Russian` — not regional variants like “British English”; put accent/dialect in `ttsInstruct` or `ttsVoiceDesign` instead.
- `ttsVoiceDesign`: optional natural-language voice description. If set, VoiceDesign mode is used by default. **Takes priority over `ttsSpeaker` when both are defined.**
- `ssh` block: optional. When present, oc-interactive automatically establishes and repairs a local SSH port-forward tunnel on every invocation — see [SSH tunnel management](#ssh-tunnel-management).

**CLI options override config.** Values in `oc-interactive.json` are defaults. Flags such as `--speaker`, `--instruct`, `--voice-design`, `-m` / `--model`, `-l` / `--language`, and `--agent` take precedence for that turn. Successful turns then cache the effective settings in `session.json`, so later turns can omit those flags until you override them again on the CLI.

To discard the session cache and reload from the config file (default `~/.config/oc-interactive/oc-interactive.json`, or `-c` if given):

```bash
oc-interactive --init
# or reset and speak in one turn:
oc-interactive --init -t "Hello"
```

Conversation history and the system prompt are kept; only cached voice/agent/config paths are cleared.

To archive the current conversation and start a fresh session (keeps system prompt and voice settings):

```bash
oc-interactive --new
# reset voice defaults and start a new session:
oc-interactive --new --init
# archive, then speak the first turn of the new session:
oc-interactive --new -t "Hello"
```

Archives are written to `~/.config/oc-interactive/sessions/` when the current session has messages.

```bash
export ELLO_GATEWAY_TOKEN=your-token
```

## SSH tunnel management

If your OpenClaw gateway runs on another machine, add an `ssh` block to the config:

```json
"ssh": {
  "enabled": true,
  "host": "ello.local",
  "user": "fsoc4",
  "identityFile": "~/.ssh/id_ed25519",
  "localPort": 18789,
  "remoteHost": "127.0.0.1",
  "remotePort": 18789
}
```

`oc-interactive` and `oc-interactive-chat` check this block automatically on every invocation, right after loading the config — no separate command to remember:

- If a matching tunnel is already up (same host/user/identity/remote target, port responding), nothing happens.
- If the port is dead, the recorded process is gone, or the target has changed (e.g. you switched config files), the stale tunnel is torn down and a fresh one is established via `ssh -N -L <localPort>:<remoteHost>:<remotePort> [user@]host`.
- If `enabled` is `false`, or there's no `ssh` block at all, this is a silent no-op — nothing changes for configs that don't need tunneling.
- If `localPort` is already bound by something oc-interactive doesn't manage, or the tunnel can't be established (bad host, auth failure, etc.), the invocation fails with a clear error instead of silently proceeding against an unreachable gateway.

Because this runs on every invocation — not just at session start — a dropped tunnel self-heals on the next turn without having to rerun an init script.

State is kept per `localPort` under `~/.config/oc-interactive/`: `ssh-tunnel-<port>.json` (host/user/pid bookkeeping) and `ssh-tunnel-<port>.log` (the `ssh` process's own stdout/stderr, useful when a tunnel fails to establish).

Manual inspection/control:

```bash
oc-interactive -c eileen.conf --ssh-tunnel-status
oc-interactive -c eileen.conf --ssh-tunnel-teardown
```

## Voice modes

| Mode | How to select | Model needed | Notes |
|------|---------------|--------------|-------|
| **CustomVoice** (default) | `--speaker` + optional `--instruct`, or config `ttsSpeaker` | CustomVoice | Emotion/style via natural language |
| **Clone** | `--refaudio` + `--reftext` | Base | Zero-shot voice cloning |
| **VoiceDesign** | `--voice-design "..."` or config `ttsVoiceDesign` | VoiceDesign | Create a voice from a description; config key wins over `ttsSpeaker` if both are set. If `ttsModel` is still a CustomVoice/Base id, oc-interactive auto-switches to the VoiceDesign HF default (downloaded on first use). |

Suggested models:

| Use case | Model id |
|----------|----------|
| Emotion + presets (default) | `mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit` |
| Faster cloning | `mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit` |
| Higher-quality cloning | `mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit` |
| Voice design | `mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-8bit` |

English CustomVoice speakers: `Ryan`, `Aiden`. Also available: `Vivian`, `Serena`, `Uncle_Fu`, `Dylan`, `Eric`, `Ono_Anna`, `Sohee`.

### Examples

CustomVoice with emotion:

```bash
oc-interactive -t "Hello" --speaker Ryan --instruct "friendly and upbeat"
```

Voice cloning (Base model):

```bash
oc-interactive -t "Hello" \
  -m mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit \
  -r path/to/reference.wav \
  --reftext "Exact words spoken in the reference clip"
```

VoiceDesign:

```bash
oc-interactive -t "Hello" \
  -m mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-8bit \
  --voice-design "A warm British woman with a soft, calm tone"
```

## Usage

### Multi-turn chat

```bash
oc-interactive -t "Hello" --speaker Ryan --instruct "warm and conversational"
oc-interactive -t "Yea, me too. What's up?"
oc-interactive -t "Well, what do you expect living in the middle of the Pacific?"
```

After the first turn, voice settings (`--speaker`, `--instruct`, `-r`/`--reftext`, `--voice-design`, `-m`, `-c`) are optional (cached in `~/.config/oc-interactive/session.json`).

### Text input

Provide the user message with `-t` / `--text`, or pipe it on stdin:

```bash
echo "What's the weather?" | oc-interactive
cat prompt.txt | oc-interactive -v
```

If both stdin and `-t` are given, **stdin wins** and `-t` is ignored.

### TTS text selection

Before any text is handed to the speech synthesizer, oc-interactive checks the **first line** of it:

- If that line is enclosed in square brackets (e.g. `[laughs]`, `[whispers something]`), only that line — with the `[` and `]` stripped — is actually synthesized into audio.
- Otherwise, the **full text** is synthesized unchanged.

This only affects what gets spoken out loud. Everywhere else — stdout, the chat UI transcript, `-v`/`--verbose`, and `session.json` history — always shows/stores the **full, unmodified** text, regardless of whether a bracketed first line caused only part of it to be spoken. It applies to every kind of spoken text: agent replies, slash-command confirmations (`/new`, `/system prompt`, `/help`, `/status`), and agent-error lines.

### Verbose and quiet output

By default, the full reply text is printed to **stdout** as soon as the agent reply is ready (before synthesis/playback) — this is the same full text described in [TTS text selection](#tts-text-selection), even if only part of it (a bracketed first line) ends up being spoken. Use `-q` / `--quiet` to suppress that.

Use `-v` / `--verbose` to also print the OpenClaw agent reply to **stdout** after a successful chat turn:

```bash
oc-interactive -t "Hello" -v
oc-interactive -t "Hello" -q
echo "Hello" | oc-interactive -v
```

Status and progress messages always go to **stderr**. Errors always go to **stderr** (including agent failures, even with `-v`).

### Model caching (performance)

Synthesis uses a persistent **Qwen3-TTS daemon** that keeps the mlx-audio model loaded in memory. Distinguish **on-disk** cache (Hugging Face / local path) from **in-memory** cache (the running daemon):

| Situation | Download? | Load into daemon? | What you notice |
|-----------|-----------|-------------------|-----------------|
| First use, model **not** in HF cache (or new auto-switched checkpoint) | Yes (several GB) | Yes | Longest wait — often many minutes |
| First use (or after daemon restart), model **already** on disk | No | Yes | Still a noticeable wait for Metal load; no network fetch |
| Later turns, same model, daemon still up | No | No (`modelReloaded=False`) | Synthesis only — fast |
| Daemon idle timeout (30 minutes), then next spoken turn | No (if HF cache warm) | Yes once (`modelReloaded=True`) | One-time reload cost, then fast again |

Use `--debug` (or `OC_INTERACTIVE_DEBUG=1`) to print timing in `daemon.log`:

```
[oc-interactive] openclawMs=1234
[oc-interactive] tts-daemon mode=custom_voice modelReloaded=False loadMs=0 synthMs=1100
```

- `modelReloaded=True` means weights were loaded into the daemon (first use or after idle/restart); `False` means the in-memory cache was reused.
- OpenClaw round-trip is separate (`openclawMs`) and may be 30–60s depending on the agent.

### Agent selection

```bash
oc-interactive -t "What's in the news?" --speaker Ryan --agent news
```

Permitted agents: `main`, `news`, `eileen` (from config). Default: `main` → `openclaw/main`.

### Slash commands

| Command | Effect |
|---------|--------|
| `/new`, `/clear`, `/clean all` | New session (archives current if it has messages; clears history; keeps system prompt) |
| `/system prompt …` | Set multi-line system prompt (empty clears it) |
| `/help` | Spoken command summary |
| `/status` | Spoken session summary |
| `/dump`, `/dump all`, `/history` | JSON conversation history on **stdout** (no audio) |

```bash
oc-interactive -t "/new" --speaker Ryan
oc-interactive -t $'/system prompt\nYou are concise.\nUse British English.' --speaker Ryan
oc-interactive -t "/history" > conversation.json
```

### CLI flags

| Flag | Description |
|------|-------------|
| `-t` / `--text` | User message or slash command (optional when piping text on stdin) |
| `--speaker` | CustomVoice speaker (default `Ryan`) |
| `--instruct` | Emotion/style instruction for CustomVoice (default from config `ttsInstruct`) |
| `--voice-design` | Voice description for VoiceDesign mode |
| `-r` / `--refaudio` | Reference audio for clone mode |
| `--reftext` | Transcript of the reference clip (required with `--refaudio`) |
| `-m` / `--model` | mlx-audio Qwen3-TTS model id or local path |
| `-l` / `--language` | TTS language (default from config `ttsLanguage`, else `English`) |
| `-o` / `--output` | Ignored (play-only) |
| `-v` / `--verbose` | Print successful OpenClaw agent reply to stdout |
| `-q` / `--quiet` | Do not print the text sent to TTS (full TTS text is printed to stdout by default) |
| `--agent` | OpenClaw agent short name |
| `-c` / `--config` | Path to `oc-interactive.json` (default: `~/.config/oc-interactive/oc-interactive.json`; cached after first turn) |
| `--init` | Forget cached voice/agent/config settings and reload defaults from the config file (alone or with a turn) |
| `--new` | Archive the current session under `sessions/` (if it has messages) and start a new one; keeps system prompt and voice cache (alone or with a turn) |
| `--timeout SECONDS` | Max seconds to wait for agent reply and TTS (default: no timeout) |
| `--debug` | Log OpenClaw/TTS timing and cache status (`OC_INTERACTIVE_DEBUG=1`) |
| `--ssh-tunnel-status` | Print JSON health of the config's `ssh` tunnel and exit (see [SSH tunnel management](#ssh-tunnel-management)) |
| `--ssh-tunnel-teardown` | Tear down the managed `ssh` tunnel and exit |

## Chat UI

A [Textual](https://github.com/Textualize/textual)-based chat window that
stays open for the whole conversation, instead of one process per turn. User
messages are right-aligned, agent replies are left-aligned, and existing
`session.json` history is rendered on startup.

```bash
cd oc-interactive
./run-chat --speaker Ryan --instruct "warm and conversational"
```

Or via `make`:

```bash
make run-chat ARGS='--speaker Ryan'
```

It accepts the same voice/config flags as `oc-interactive` (`--speaker`,
`--instruct`, `--voice-design`, `-r`/`--refaudio`, `--reftext`, `-m`/`--model`,
`-l`/`--language`, `--agent`, `-c`/`--config`, `--init`, `--new`, `--debug`) —
just no `-t`/`--text`, since text is typed into the chat input instead. Voice
playback still happens through the same background TTS daemon as the CLI; the
chat window is a visual transcript layered on top, not a silent mode.

Type a message and press Enter to send it. While a turn is in flight the
input is disabled and the header subtitle shows `waiting for <agent>…`; the
reply bubble appears as soon as the agent's text is ready, which is generally
*before* its audio finishes playing.

Slash commands work the same as the CLI's (`/new`, `/clear`, `/clean all`,
`/system prompt …`, `/help`, `/status`), with two chat-only differences:

- `/dump`, `/dump all`, `/history` write the JSON conversation history to
  `~/.config/oc-interactive/exports/<timestamp>.json` instead of stdout (a
  full-screen app can't print to stdout mid-session), and show the saved path
  as a system message.
- `/agent <name>` switches the agent used for subsequent turns without
  restarting the app (validated against the same `agents` allowlist as
  `--agent`). This command is local to the chat UI and isn't sent to the
  daemon.

Quit with `ctrl+q` or `ctrl+c`.

Note: the chat input is a single-line box, so a multi-line `/system prompt`
(as shown in [Slash commands](#slash-commands) above) is easiest to set from
the one-shot CLI rather than typed directly into the chat window.

## State files

Under `~/.config/oc-interactive/` (override with `OC_INTERACTIVE_STATE_DIR`):

| File | Purpose |
|------|---------|
| `session.json` | Conversation history, system prompt, cached voice settings / model / config |
| `sessions/` | Archived sessions from `--new` / `/new` (timestamped JSON copies) |
| `daemon.sock` | Unix socket IPC |
| `daemon.pid` | Background daemon PID |
| `daemon.log` | Background orchestration daemon logs |
| `tts-daemon.sock` | Unix socket to cached TTS daemon |
| `tts-daemon.pid` | TTS daemon PID |
| `tts-daemon.log` | TTS daemon logs (model load / synth timing) |
| `ssh-tunnel-<port>.json` | Managed SSH tunnel bookkeeping (host/user/pid) for a config's `ssh.localPort`, if used |
| `ssh-tunnel-<port>.log` | `ssh` process stdout/stderr for that tunnel |

The orchestration daemon shuts down after 30 minutes idle; the next invocation restarts it. The TTS daemon has the same idle timeout but is restarted automatically when speech is needed.

## Troubleshooting

| Symptom | What to check |
|---------|----------------|
| `Model type dots_tts not supported` | Stale `lastTtsModel` in `~/.config/oc-interactive/session.json` from the old dots-tts era. Pass `-m mlx-community/Qwen3-TTS-…`, or delete `lastTtsModel` from the session file. oc-interactive now ignores dots paths automatically after upgrade. |
| Very slow every turn (`modelReloaded=True` always) | Ensure `tts-daemon.log` shows the daemon staying alive between turns. Restart with `pkill -f qwen_tts_daemon`. |
| `peer closed connection` / no audio | Stale socket after a crashed daemon — remove `tts-daemon.sock` and `tts-daemon.pid`, or restart. Check `tts-daemon.log`. |
| Model type / speaker errors | Match `-m` to the mode (CustomVoice vs Base vs VoiceDesign). List speakers for CustomVoice in the mlx-audio Qwen3-TTS docs. |
| Client times out but audio plays | Long replies or first model load can exceed `--timeout`; omit it for no limit. Audio may still play — check `daemon.log`. |

After code changes, restart the orchestration daemon so it picks up the installed package:

```bash
pkill -f "oc_interactive --daemon"
pkill -f "oc_interactive.qwen_tts_daemon"
```

## Errors and I/O

All error messages are written to **stderr** and the process exits with a non-zero status.

Agent failures are also spoken as: `Something wrong with the agent, <reason>` (that text is printed to stderr, not stdout).

Unknown slash commands and invalid `--agent` values exit with an error on stderr.

**stdout** is used for:

- TTS text (unless `-q` / `--quiet`)
- `/dump`, `/history` JSON output
- `-v` / `--verbose` agent replies on successful chat turns

Use `-q` / `--quiet` to suppress the default TTS text on stdout.

## Development

```bash
cd oc-interactive
pip install -e .
python -m oc_interactive -t "/history"
```

## License

Apache-2.0 (same as the parent package).
