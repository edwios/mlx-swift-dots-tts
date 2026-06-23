# oc-interactive

Python CLI that sends text to an [OpenClaw](https://docs.openclaw.ai/) agent and speaks the reply through your speakers using the Swift [`dots-tts`](../app/) voice-cloning binary.

Each conversational turn is a **separate shell invocation**. A background daemon auto-starts to orchestrate OpenClaw requests and TTS playback.

> A native Swift implementation with in-process MLX TTS is planned separately; this Python app delegates synthesis to `dots-tts`.

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
./run -t "Hello" -r path/to/reference.wav -m ../dots.tts-soar-mlx/4bit
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

After the first turn, `-r` and `-m` are optional (cached in `~/.config/oc-interactive/session.json`).

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

## State files

Under `~/.config/oc-interactive/` (override with `OC_INTERACTIVE_STATE_DIR`):

| File | Purpose |
|------|---------|
| `session.json` | Conversation history, system prompt, cached paths |
| `daemon.sock` | Unix socket IPC |
| `daemon.pid` | Background daemon PID |
| `daemon.log` | Daemon logs |

The daemon shuts down after 30 minutes idle; the next invocation restarts it.

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
