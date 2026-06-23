# oc-interactive

Python CLI that sends text to an [OpenClaw](https://docs.openclaw.ai/) agent and speaks the reply through your speakers using the Swift [`dots-tts`](../app/) voice-cloning binary.

Each conversational turn is a **separate shell invocation**. Two background daemons auto-start:

1. **Orchestration daemon** (`oc_interactive --daemon`) — OpenClaw chat, slash commands, session state, afplay.
2. **TTS daemon** (`dots-tts --tts-daemon`) — cached MLX model and reference audio for synthesis.

> A native Swift oc-interactive with fully in-process MLX is planned separately; this Python app delegates synthesis to the `dots-tts` TTS daemon.

## Requirements

- macOS 15+ on Apple Silicon
- Python 3.11+
- Built `dots-tts` binary (see below)
- OpenClaw gateway reachable (SSH tunnel assumed already up if remote)
- `ELLO_GATEWAY_TOKEN` environment variable

## Build dots-tts (prerequisite)

From the repo root:

```bash
cd app && make build
```

This produces `app/.build/dots-tts` and `app/.build/mlx-swift_Cmlx.bundle` (must stay alongside the binary).

## Install oc-interactive

```bash
cd oc-interactive
make install
```

This creates `.venv/` and installs the `oc-interactive` command into `.venv/bin/` (not on your global PATH).

### How to run

**Option A — launcher script (easiest, no PATH changes):**

```bash
cd oc-interactive
./run --debug -t "Hello" -r path/to/reference.wav --reftext "What the speaker says in the clip" -m ../dots.tts-soar-mlx/4bit --dots-tts ../app/.build/dots-tts
```

**Option B — full path:**

```bash
oc-interactive/.venv/bin/oc-interactive -t "Hello" -r reference.wav --reftext "What the speaker says in the clip"
```

**Option C — activate the venv:**

```bash
cd oc-interactive
source .venv/bin/activate
oc-interactive -t "Hello" -r reference.wav --reftext "What the speaker says in the clip"
deactivate
```

**Option D — make run:**

```bash
cd oc-interactive
make run ARGS='-t "Hello" -r reference.wav --reftext "What the speaker says in the clip" -m ../dots.tts-soar-mlx/4bit'
```

**Option E — stdin (omit `-t`):**

```bash
echo "Hello" | oc-interactive -r reference.wav --reftext "What the speaker says in the clip"
```

Piped stdin wins over `-t` when both are present.

## Configuration

Copy the example config:

```bash
mkdir -p ~/.config/oc-interactive
cp config/openclaw.example.json ~/.config/oc-interactive/openclaw.json
```

Override the config path with `-c` / `--config` (default: `~/.config/oc-interactive/openclaw.json`):

```bash
oc-interactive -c /path/to/openclaw.json -t "Hello" -r reference.wav --reftext "transcript"
```

Edit paths as needed. Example:

```json
{
  "openclawBaseURL": "http://127.0.0.1:18789",
  "openclawToken": "$ELLO_GATEWAY_TOKEN",
  "defaultAgent": "main",
  "agents": ["main", "news", "eileen"],
  "dotsTtsBinary": "/absolute/path/to/app/.build/dots-tts"
}
```

- `openclawToken`: use `$VAR_NAME` to read from the environment.
- `agents`: allowlist validated against `--agent` (CLI value is prefixed with `openclaw/` automatically).
- `dotsTtsBinary`: path to the built `dots-tts` binary. Use an **absolute** path when the config lives under `~/.config/oc-interactive/` (relative paths resolve against the config file's directory).
- `ssh` block is informational only; start your tunnel separately.

```bash
export ELLO_GATEWAY_TOKEN=your-token
```

## Usage

### Multi-turn chat

```bash
oc-interactive -t "Hello" -r path/to/reference.wav --reftext "What the speaker says in the clip" -m ../dots.tts-soar-mlx
oc-interactive -t "Yea, me too. What's up?"
oc-interactive -t "Well, what do you expect living in the middle of the Pacific?"
```

After the first turn, `-r`, `--reftext`, `-m`, `-c` / `--config`, and `--dots-tts` are optional (cached in `~/.config/oc-interactive/session.json`).

### Reference voice

Continuation cloning needs both the reference **audio** and its **transcript**. On the first spoken turn, pass them together:

```bash
oc-interactive -t "Hello" \
  -r path/to/reference.wav \
  --reftext "Exact words spoken in the reference clip"
```

`--reftext` is required whenever `--refaudio` is set; if you omit `-r` on later turns, any `--reftext` on the command line is ignored and the cached transcript is used.

### Text input

Provide the user message with `-t` / `--text`, or pipe it on stdin:

```bash
echo "What's the weather?" | oc-interactive
cat prompt.txt | oc-interactive -v
```

If both stdin and `-t` are given, **stdin wins** and `-t` is ignored.

### Verbose output

Use `-v` / `--verbose` to print the OpenClaw agent reply to **stdout** after a successful chat turn:

```bash
oc-interactive -t "Hello" -v
echo "Hello" | oc-interactive -v
```

Status and progress messages always go to **stderr**. Errors always go to **stderr** (including agent failures, even with `-v`).

### Model caching (performance)

Synthesis uses a persistent **`dots-tts --tts-daemon`** process that keeps the MLX model loaded in memory. The first spoken turn pays a one-time model load cost (~2–5s); later turns reuse the cache and only pay synthesis time (~1–3s for short replies, longer for big ones).

Use `--debug` (or `OC_INTERACTIVE_DEBUG=1`) to print timing in `daemon.log`:

```
[oc-interactive] openclawMs=1234
[oc-interactive] tts-daemon modelReloaded=False refaudioReloaded=False loadMs=0 synthMs=1100
```

- `modelReloaded=True` on the first turn is expected; `False` on subsequent turns confirms caching.
- OpenClaw round-trip is separate (`openclawMs`) and may be 30–60s depending on the agent.

The TTS daemon idles out after 30 minutes; the next spoken turn reloads the model once.

Typical timings with `--debug` (short reply, warm cache):

| Phase | First turn | Later turns |
|-------|------------|-------------|
| OpenClaw (`openclawMs`) | 30–60s | 30–60s |
| MLX model load (`loadMs`, when `modelReloaded=True`) | ~2–5s | ~0 |
| Synthesis (`synthMs`) | ~1–4s | ~1–4s |

Long agent replies (or `/help`) increase `synthMs` proportionally; that is not a model reload.

### Agent selection

```bash
oc-interactive -t "What's in the news?" -r reference.wav --reftext "Sample news intro." --agent news
```

Permitted agents: `main`, `news`, `eileen` (from config). Default: `main` → `openclaw/main`.

### Slash commands

| Command | Effect |
|---------|--------|
| `/new`, `/clear`, `/clean all` | New session (clears message history; keeps system prompt) |
| `/system prompt …` | Set multi-line system prompt (empty clears it) |
| `/help` | Spoken command summary |
| `/status` | Spoken session summary |
| `/dump`, `/dump all`, `/history` | JSON conversation history on **stdout** (no audio) |

```bash
oc-interactive -t "/new" -r reference.wav --reftext "What the speaker says in the clip"
oc-interactive -t $'/system prompt\nYou are concise.\nUse British English.' -r reference.wav --reftext "What the speaker says in the clip"
oc-interactive -t "/history" > conversation.json
```

### CLI flags

| Flag | Description |
|------|-------------|
| `-t` / `--text` | User message or slash command (optional when piping text on stdin) |
| `-r` / `--refaudio` | Reference audio (required first TTS turn) |
| `--reftext` | Transcript of the reference clip (required with `--refaudio`; ignored otherwise; cached after first turn) |
| `-m` / `--model` | dots.tts-soar-mlx model directory |
| `-l` / `--language` | Parity with dots-tts (agent replies use `EN`) |
| `-o` / `--output` | Ignored (play-only) |
| `-v` / `--verbose` | Print successful OpenClaw agent reply to stdout |
| `--agent` | OpenClaw agent short name |
| `-c` / `--config` | Path to `openclaw.json` (default: `~/.config/oc-interactive/openclaw.json`; cached after first turn; `--openclaw-config` is an alias) |
| `--dots-tts` | Path to `dots-tts` binary |
| `--debug` | Log OpenClaw/TTS timing and cache status (`OC_INTERACTIVE_DEBUG=1`) |

## State files

Under `~/.config/oc-interactive/` (override with `OC_INTERACTIVE_STATE_DIR`):

| File | Purpose |
|------|---------|
| `session.json` | Conversation history, system prompt, cached `lastRefaudio` / `lastReftext` / `lastTtsModel` / `lastDotsTts` / `lastOpenclawConfig` |
| `daemon.sock` | Unix socket IPC |
| `daemon.pid` | Background daemon PID |
| `daemon.log` | Background orchestration daemon logs |
| `tts-daemon.sock` | Unix socket to cached MLX TTS daemon |
| `tts-daemon.pid` | TTS daemon PID |
| `tts-daemon.log` | TTS daemon logs (model load / synth timing) |

The orchestration daemon shuts down after 30 minutes idle; the next invocation restarts it. The TTS daemon has the same idle timeout but is restarted automatically when speech is needed.

## Troubleshooting

| Symptom | What to check |
|---------|----------------|
| Very slow every turn (`modelReloaded=True` always) | Rebuild `dots-tts` (`cd app && make build`). Ensure `tts-daemon.log` shows the daemon staying alive between turns. |
| `peer closed connection` / no audio | Stale socket after a crashed daemon — remove `tts-daemon.sock` and `tts-daemon.pid`, or restart. Check `tts-daemon.log`. |
| `dots-tts not found` on turn 2+ | Pass `--dots-tts` on the first turn, or set an absolute `dotsTtsBinary` in config (cached as `lastDotsTts` in session). |
| Client times out but audio plays | OpenClaw + first model load can exceed older timeouts; current client allows 10 minutes. Check `daemon.log`. |

After code changes, restart the orchestration daemon so it picks up the installed package:

```bash
pkill -f "oc_interactive --daemon"
```

## Errors and I/O

All error messages are written to **stderr** and the process exits with a non-zero status.

Agent failures are also spoken as: `Something wrong with the agent, <reason>` (that text is printed to stderr, not stdout).

Unknown slash commands and invalid `--agent` values exit with an error on stderr.

**stdout** is used only for:

- `/dump`, `/history` JSON output
- `-v` / `--verbose` agent replies on successful chat turns

## Development

```bash
cd oc-interactive
pip install -e .
python -m oc_interactive -t "/history"
```

Stdlib only — no third-party dependencies.

## License

Apache-2.0 (same as the parent package).
