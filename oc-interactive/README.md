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
./run --debug -t "Hello" -r path/to/reference.wav -m ../dots.tts-soar-mlx/4bit --dots-tts ../app/.build/dots-tts
```

**Option B — full path:**

```bash
oc-interactive/.venv/bin/oc-interactive -t "Hello" -r reference.wav
```

**Option C — activate the venv:**

```bash
cd oc-interactive
source .venv/bin/activate
oc-interactive -t "Hello" -r reference.wav
deactivate
```

**Option D — make run:**

```bash
cd oc-interactive
make run ARGS='-t "Hello" -r reference.wav -m ../dots.tts-soar-mlx/4bit'
```

## Configuration

Copy the example config:

```bash
mkdir -p ~/.config/oc-interactive
cp config/openclaw.example.json ~/.config/oc-interactive/openclaw.json
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
oc-interactive -t "Hello" -r path/to/reference.wav -m ../dots.tts-soar-mlx
oc-interactive -t "Yea, me too. What's up?"
oc-interactive -t "Well, what do you expect living in the middle of the Pacific?"
```

After the first turn, `-r`, `-m`, and `--dots-tts` are optional (cached in `~/.config/oc-interactive/session.json`).

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
oc-interactive -t "What's in the news?" -r reference.wav --agent news
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
oc-interactive -t "/new" -r reference.wav
oc-interactive -t $'/system prompt\nYou are concise.\nUse British English.' -r reference.wav
oc-interactive -t "/history" > conversation.json
```

### CLI flags

| Flag | Description |
|------|-------------|
| `-t` / `--text` | User message or slash command |
| `-r` / `--refaudio` | Reference audio (required first TTS turn) |
| `-m` / `--model` | dots.tts-soar-mlx model directory |
| `-l` / `--language` | Parity with dots-tts (agent replies use `EN`) |
| `-o` / `--output` | Ignored (play-only) |
| `--agent` | OpenClaw agent short name |
| `--openclaw-config` | Path to `openclaw.json` |
| `--dots-tts` | Path to `dots-tts` binary |
| `--debug` | Log OpenClaw/TTS timing and cache status (`OC_INTERACTIVE_DEBUG=1`) |

## State files

Under `~/.config/oc-interactive/` (override with `OC_INTERACTIVE_STATE_DIR`):

| File | Purpose |
|------|---------|
| `session.json` | Conversation history, system prompt, cached `lastRefaudio` / `lastTtsModel` / `lastDotsTts` |
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

## Errors

Agent failures are spoken as: `Something wrong with the agent, <reason>`.

Unknown slash commands and invalid `--agent` values exit with a non-zero status and a stderr message.

## Development

```bash
cd oc-interactive
pip install -e .
python -m oc_interactive -t "/history"
```

Stdlib only — no third-party dependencies.

## License

Apache-2.0 (same as the parent package).
