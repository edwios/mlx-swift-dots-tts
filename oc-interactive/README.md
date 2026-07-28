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
  "ttsVoiceDesign": null,
  "filterBaseURL": null,
  "filterModel": null,
  "filterPrompt": null,
  "filterEnabled": false
}
```

- `openclawToken`: use `$VAR_NAME` to read from the environment.
- `agents`: allowlist validated against `--agent` (CLI value is prefixed with `openclaw/` automatically).
- `ttsModel`: Hugging Face model id or local path (default CustomVoice 8-bit). Use a VoiceDesign model when `ttsVoiceDesign` is set.
- `ttsSpeaker`: default CustomVoice speaker when `--speaker` is omitted.
- `ttsInstruct`: optional default emotion/style instruction for CustomVoice (used when `--instruct` is omitted).
- `ttsLanguage`: TTS language code (default `English`). Qwen3-TTS supports language ids such as `English`, `Chinese`, `Japanese`, `Korean`, `German`, `French`, `Spanish`, `Italian`, `Portuguese`, `Russian` — not regional variants like “British English”; put accent/dialect in `ttsInstruct` or `ttsVoiceDesign` instead.
- `ttsVoiceDesign`: optional natural-language voice description. If set, VoiceDesign mode is used by default. **Takes priority over `ttsSpeaker` when both are defined.**
- `filterBaseURL` / `filterModel`: optional overrides for the local LLM text-filter server (see [Text filter](#text-filter-local-llm)). Defaults to an LM Studio instance at `http://10.0.1.29:22206` running `qwen3.5-2b-uncensored-hauhaucs-aggressive-mlx` when omitted/`null`.
- `filterPrompt` / `filterEnabled`: optional defaults for the filter's system prompt and on/off state. Used only until a session explicitly runs `/filter <description>` / `/filter on` / `/filter off`, at which point the session value wins (same precedence as `ttsVoiceDesign` vs. `/voice-design`). `--init` clears the session override and reverts to these config defaults. Run `/save-config` (below) to make a session's current filter settings — or agent/language/voice-design — the new config default.
- `ssh` block: optional. When present, oc-interactive automatically establishes and repairs a local SSH port-forward tunnel on every invocation — see [SSH tunnel management](#ssh-tunnel-management).

**CLI options override config.** Values in `oc-interactive.json` are defaults. Flags such as `--speaker`, `--instruct`, `--voice-design`, `-m` / `--model`, `-l` / `--language`, and `--agent` take precedence for that turn. Successful turns then cache the effective settings in that agent's own `agents/<agent>/session.json` (see [Sessions are per-agent](#sessions-are-per-agent)), so later turns can omit those flags until you override them again on the CLI.

### Saving settings back to the config file

Everything above (`ttsVoiceDesign`, `ttsLanguage`, `defaultAgent`, `filterPrompt`/`filterEnabled`) is read from the config file as a *default*, then only ever cached per-agent in that agent's `session.json` — the config file itself is never modified automatically. To make your current agent's settings the new default, run:

```bash
oc-interactive -t "/save-config"
```

**This always writes to the *default* config path, `~/.config/oc-interactive/oc-interactive.json` (override with `OC_INTERACTIVE_STATE_DIR`) — never to whichever file was passed via `-c`/`--config`.** oc-interactive never modifies a `-c`/`--config` file (e.g. a named per-persona config like `eileen.conf`/`victoria.conf`); that file is yours to edit by hand. If you're running with `-c` day to day (as the `*_init.sh` launcher scripts do), `/save-config` still only affects `oc-interactive.json`, so re-running with `-c` again won't pick up what you just saved unless you also add it to that `-c` file yourself.

If `oc-interactive.json` doesn't exist yet, `/save-config` creates it by copying the full contents of the config currently in use (so it's an immediately usable standalone config, not missing `openclawBaseURL`/`openclawToken`), then applies the updates below. If it already exists, only these keys are merged in — everything else in it is left untouched:

| Key written | From |
|-------------|------|
| `defaultAgent` | The agent currently in use this turn |
| `ttsLanguage` | The current language |
| `ttsVoiceDesign` | The current voice design description — only written if VoiceDesign mode is currently active; left alone otherwise |
| `filterEnabled` / `filterPrompt` | The filter's current effective on/off state and description |

`ttsSpeaker` / `ttsInstruct` / `ttsModel` / clone (`refaudio`/`reftext`) settings are **not** currently written by `/save-config` — if you're using CustomVoice or Clone mode day to day, those still need to be edited into the config file by hand (or passed as CLI flags each time).

The daemon speaks back a summary of what was saved, including the path it wrote to (e.g. "Config saved to /Users/you/.config/oc-interactive/oc-interactive.json: agent victoria, language English, voice design, filter on."), or an error if the file couldn't be written (permissions, disk full, etc.) — the session itself is unaffected either way.

To reset **every** agent's session — conversation, system prompt, voice cache, filter, everything — and reload defaults from the config file (default `~/.config/oc-interactive/oc-interactive.json`, or `-c` if given):

```bash
oc-interactive --init
# or reset and speak in one turn (spoken with the config's defaultAgent):
oc-interactive --init -t "Hello"
```

Each agent listed in the config that has messages is archived first (see [Sessions are per-agent](#sessions-are-per-agent)), then replaced with a completely blank session. This is a harder reset than `--new` below — it clears *all* agents, not just the one you're currently talking to, and drops cached settings too instead of carrying them into the new session.

To archive the current agent's conversation and start a fresh one for that agent only (keeps system prompt and voice settings; other agents' sessions are untouched):

```bash
oc-interactive --new
# reset every agent's cache too, on top of starting fresh for this one:
oc-interactive --new --init
# archive, then speak the first turn of the new session:
oc-interactive --new -t "Hello"
```

Archives are written to `~/.config/oc-interactive/agents/<agent>/sessions/` when that agent's current session has messages.

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

After the first turn, voice settings (`--speaker`, `--instruct`, `-r`/`--reftext`, `--voice-design`, `-m`, `-c`) are optional (cached per-agent in `~/.config/oc-interactive/agents/<agent>/session.json`).

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

This only affects what gets spoken out loud. Everywhere else — stdout, the chat UI transcript, `-v`/`--verbose`, and `session.json` history — always shows/stores the **full, unmodified** text, regardless of whether a bracketed first line caused only part of it to be spoken. It applies to spoken text from real chat turns: agent replies and agent-error lines. Slash-command confirmations (`/new`, `/system prompt`, `/voice-design`, `/filter`, `/save-config`, `/help`, `/status`) are never sent to the TTS engine at all — they're always displayed as text (stdout / chat UI transcript) but never synthesized into audio.

### Text filter (local LLM)

Optionally, whatever text would be spoken (i.e. after the bracket-selection rule above) can be routed through a separate local LLM first, and its reply is what actually gets synthesized — for tone rewriting, redaction, simplification, etc. This uses an OpenAI-compatible `/v1/chat/completions` endpoint such as [LM Studio](https://lmstudio.ai/docs/developer/rest); the target server/model are set via `filterBaseURL` / `filterModel` in the config (see [Configuration](#configuration)).

Controlled with the `/filter` slash command:

| Command | Effect |
|---------|--------|
| `/filter on` | Enable routing: text to be spoken is sent to the filter LLM first, and its reply is what's synthesized. |
| `/filter off` (default) | Disable routing: text is sent to TTS directly, unchanged. |
| `/filter <description>` | Set the filter LLM's system prompt (the transformation to apply). Independent of on/off — setting a description does not itself enable the filter. |
| `/filter` (bare) | Speaks/reports the current on/off state and description, and (in the chat UI) pre-fills the input with `/filter <description>` so it's easy to tweak. |

```bash
oc-interactive -t "/filter Rewrite this to sound calm and reassuring, keep it brief." --speaker Ryan
oc-interactive -t "/filter on"
oc-interactive -t "/filter"    # report current state + description
oc-interactive -t "/filter off"
```

Notes:

- Only real agent replies are ever routed through the filter — agent-error lines are always spoken unfiltered, and slash-command confirmations aren't spoken (as audio) at all, so filtering never applies to them.
- The filter is display-transparent, same as the bracket rule: stdout, the chat transcript, and `session.json` history always show the agent's original, unfiltered reply. Only the audio actually synthesized reflects the filtered text.
- If the filter LLM is unreachable, times out, or returns a malformed reply, oc-interactive logs the error (`daemon.log`) and falls back to speaking the original unfiltered text for that turn — a filter-server hiccup never blocks speech.
- On/off state and the description persist in `session.json` like the system prompt (survive `/new`); only an explicit `/filter on` / `/filter off` / `/filter <description>` changes them for this session. `--init` clears the session override and reverts to the config's `filterEnabled`/`filterPrompt` defaults. None of this touches the config file itself — run `/save-config` (see [Saving settings back to the config file](#saving-settings-back-to-the-config-file)) if you want the current filter settings to become that config's new default.
- The chat UI's input box is single-line, so it can't take a real line break. Type a literal `\n` where you want one (e.g. `/filter Rewrite calmly.\nKeep it under two sentences.`) — it's stored as a genuine newline in the system prompt sent to the filter LLM, not as literal backslash-n text. Bare `/filter` reverses this when pre-filling the input, so an existing multi-line description round-trips back to the same `\n`-separated text instead of being cut at the first line.

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

### Sessions are per-agent

Each agent has its own independent session — its own conversation history, system prompt, cached voice settings, filter state, and cached config path — stored at `~/.config/oc-interactive/agents/<agent>/session.json`. Switching agents (via `--agent` or, in the chat UI, `/agent <name>`) switches to *that agent's own previous session*, picking up right where you last left off with it, rather than continuing the session you were just in under a different label.

A small pointer file, `~/.config/oc-interactive/active_agent.json`, records which agent was last talked to (by any invocation — CLI or chat UI). At launch, if `--agent` isn't given, the session is restored from this active agent's own session rather than falling back to the config's `defaultAgent` — so restarting `oc-interactive` / `oc-interactive-chat` (e.g. via `run-chat`) resumes the same agent and the same conversation you were last using. Every turn (including slash commands) updates this pointer to whichever agent it just talked to.

`--new` archives and resets only the current agent's session — other agents' sessions and the active-agent pointer for them are untouched. `--init` is a full reset: it archives and blanks out *every* agent's session and clears the active-agent pointer, so the next launch falls back to the config's `defaultAgent`. See [Saving settings back to the config file](#saving-settings-back-to-the-config-file) above for the exact `--new` / `--init` semantics.

If you're upgrading from a version of oc-interactive that used a single flat `session.json` for all agents, that file is migrated automatically the first time any command runs: its conversation is moved into `agents/<lastAgent-or-main>/session.json`, that agent becomes the active agent, and the old file is renamed to `session.json.migrated`.

### Slash commands

None of these produce spoken audio — every slash-command confirmation below is displayed as text only (stdout / chat UI transcript). Only real agent chat replies (and agent-error lines from a chat turn) are ever sent to the TTS engine.

| Command | Effect |
|---------|--------|
| `/new`, `/clear`, `/clean all` | New session (archives current if it has messages; clears history; keeps system prompt) |
| `/system prompt …` | Set multi-line system prompt (empty clears it) |
| `/voice-design …` (alias `/voice design …`) | Switch to VoiceDesign mode with this description — same effect as `--voice-design`, and it persists in `session.json` exactly as if passed on the CLI, so later turns keep using it with no flag needed. Auto-switches to a VoiceDesign checkpoint if the cached model doesn't already match. |
| `/voice-design` (bare) | Cites the current voice design back (or reports that none is set) without changing anything. |
| `/voice-design reset` / `/voice-design default` | Reset the voice design to the config's `ttsVoiceDesign`; if the config has none, reverts to CustomVoice using `ttsSpeaker`/`ttsInstruct`. |
| `/filter on` / `/filter off` | Enable/disable routing spoken text through the local filter LLM before TTS (see [Text filter](#text-filter-local-llm)) |
| `/filter <description>` | Set the filter LLM's system prompt; independent of on/off |
| `/filter` (bare) | Report current on/off state + description |
| `/save-config` (alias `/save config`) | Write the current agent, language, voice design (if active), and filter settings into the *default* `oc-interactive.json` as its new defaults — never into a `-c`/`--config` file — see [Saving settings back to the config file](#saving-settings-back-to-the-config-file) |
| `/help` | Command summary (text only, not spoken) |
| `/status` | Session summary (text only, not spoken) |
| `/dump`, `/dump all`, `/history` | JSON conversation history on **stdout** (no audio) |

```bash
oc-interactive -t "/new" --speaker Ryan
oc-interactive -t $'/system prompt\nYou are concise.\nUse British English.' --speaker Ryan
oc-interactive -t "/voice-design A warm British woman with a soft, calm tone" --speaker Ryan
oc-interactive -t "/voice-design"        # cite the current description
oc-interactive -t "/voice-design reset"  # back to the config's ttsVoiceDesign (or CustomVoice)
oc-interactive -t "/filter Rewrite calmly and briefly." --speaker Ryan
oc-interactive -t "/filter on"
oc-interactive -t "/save-config"  # bake the above into oc-interactive.json (never the -c file)
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
| `--init` | Reset **every** agent's session (archiving each if it has messages) and reload defaults from the config file (alone or with a turn) |
| `--new` | Archive the current agent's session under `agents/<agent>/sessions/` (if it has messages) and start a new one for that agent; keeps system prompt and voice cache; other agents untouched (alone or with a turn) |
| `--timeout SECONDS` | Max seconds to wait for agent reply and TTS (default: no timeout) |
| `--debug` | Log OpenClaw/TTS timing and cache status (`OC_INTERACTIVE_DEBUG=1`) |
| `--ssh-tunnel-status` | Print JSON health of the config's `ssh` tunnel and exit (see [SSH tunnel management](#ssh-tunnel-management)) |
| `--ssh-tunnel-teardown` | Tear down the managed `ssh` tunnel and exit |

## Chat UI

A [Textual](https://github.com/Textualize/textual)-based chat window that
stays open for the whole conversation, instead of one process per turn. User
messages are right-aligned, agent replies are left-aligned, and the current
agent's existing `session.json` history is rendered on startup.

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

These flags only seed the *first* turn. Voice settings resolved at startup are
resynced from the current agent's `session.json` after every turn (chat or
slash command), so mid-session changes — e.g. `/voice-design …` — take effect
immediately for the next reply and for a following bare `/voice-design`,
instead of the app holding onto its startup snapshot for the rest of the
session.

Type a message and press Enter to send it. While a turn is in flight the
input is disabled and the header subtitle shows `waiting for <agent>…`; the
reply bubble appears as soon as the agent's text is ready, which is generally
*before* its audio finishes playing.

Slash commands work the same as the CLI's (`/new`, `/clear`, `/clean all`,
`/system prompt …`, `/voice-design …`, `/filter …`, `/save-config`, `/help`,
`/status`), with three chat-only differences:

- `/dump`, `/dump all`, `/history` write the JSON conversation history to
  `~/.config/oc-interactive/exports/<timestamp>.json` instead of stdout (a
  full-screen app can't print to stdout mid-session), and show the saved path
  as a system message.
- `/agent <name>` switches to that agent's own session for subsequent turns,
  without restarting the app (validated against the same `agents` allowlist
  as `--agent`). Since sessions are per-agent (see
  [Sessions are per-agent](#sessions-are-per-agent)), this replaces the
  visible chat log with that agent's own history and its own cached voice
  settings — it isn't just relabeling the conversation you were just having.
  This command is local to the chat UI and isn't sent to the daemon.
- Bare `/voice-design` additionally pre-fills the chat input with
  `/voice-design <current description>` (or just `/voice-design ` if none is
  set), so you can tweak it without retyping it from scratch. The one-shot
  CLI has no input box to prefill, so only the spoken/citation part applies
  there.
- Bare `/filter` does the same for the filter description: pre-fills
  `/filter <current description>` (or `/filter ` if none is set).

Quit with `ctrl+q` or `ctrl+c`.

Note: the chat input is a single-line box, so a multi-line `/system prompt`
(as shown in [Slash commands](#slash-commands) above) is easiest to set from
the one-shot CLI rather than typed directly into the chat window.

## State files

Under `~/.config/oc-interactive/` (override with `OC_INTERACTIVE_STATE_DIR`):

| File | Purpose |
|------|---------|
| `agents/<agent>/session.json` | That agent's conversation history, system prompt, cached voice settings / model / config, filter on/off + description |
| `agents/<agent>/sessions/` | That agent's archived sessions from `--new` / `/new` / `--init` (timestamped JSON copies) |
| `active_agent.json` | Which agent's session is "current" — restored at launch when `--agent` is omitted; updated on every turn |
| `session.json.migrated` | Pre-per-agent flat session file, renamed here after the one-time automatic migration (see [Sessions are per-agent](#sessions-are-per-agent)) |
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
| `Model type dots_tts not supported` | Stale `lastTtsModel` in `~/.config/oc-interactive/agents/<agent>/session.json` from the old dots-tts era. Pass `-m mlx-community/Qwen3-TTS-…`, or delete `lastTtsModel` from that agent's session file. oc-interactive now ignores dots paths automatically after upgrade. |
| Very slow every turn (`modelReloaded=True` always) | Ensure `tts-daemon.log` shows the daemon staying alive between turns. Restart with `pkill -f qwen_tts_daemon`. |
| `peer closed connection` / no audio | Stale socket after a crashed daemon — remove `tts-daemon.sock` and `tts-daemon.pid`, or restart. Check `tts-daemon.log`. |
| Model type / speaker errors | Match `-m` to the mode (CustomVoice vs Base vs VoiceDesign). List speakers for CustomVoice in the mlx-audio Qwen3-TTS docs. |
| Client times out but audio plays | Long replies or first model load can exceed `--timeout`; omit it for no limit. Audio may still play — check `daemon.log`. |

After code changes, restart the orchestration daemon so it picks up the installed package:

```bash
./restart-daemon.sh
# or directly:
pkill -f "oc_interactive --daemon"
```

If the change also touches TTS (`tts.py`, `qwen_tts_daemon.py`), restart that daemon too:

```bash
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
