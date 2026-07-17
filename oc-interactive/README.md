# oc-interactive

Python CLI that sends text to an [OpenClaw](https://docs.openclaw.ai/) agent and speaks the reply through your speakers using [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS) via [mlx-audio](https://github.com/Blaizzy/mlx-audio).

Each conversational turn is a **separate shell invocation**. Two background daemons auto-start:

1. **Orchestration daemon** (`oc_interactive --daemon`) — OpenClaw chat, slash commands, session state, afplay.
2. **TTS daemon** (`oc_interactive.qwen_tts_daemon`) — cached mlx-audio Qwen3-TTS model for synthesis.

## Requirements

- macOS on Apple Silicon
- Python 3.11+
- OpenClaw gateway reachable (SSH tunnel assumed already up if remote)
- `ELLO_GATEWAY_TOKEN` environment variable

## Install

```bash
cd oc-interactive
make install
```

This creates `.venv/`, installs `oc-interactive` plus `mlx-audio[tts]`, and puts the command at `.venv/bin/oc-interactive`.

On first synthesis, mlx-audio downloads the selected Hugging Face model into the HF cache (several GB for 1.7B 8-bit).

## How to run

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
cp config/openclaw.example.json ~/.config/oc-interactive/openclaw.json
```

Override the config path with `-c` / `--config` (default: `~/.config/oc-interactive/openclaw.json`):

```bash
oc-interactive -c /path/to/openclaw.json -t "Hello" --speaker Ryan
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
- `ssh` block is informational only; start your tunnel separately.

```bash
export ELLO_GATEWAY_TOKEN=your-token
```

## Voice modes

| Mode | How to select | Model needed | Notes |
|------|---------------|--------------|-------|
| **CustomVoice** (default) | `--speaker` + optional `--instruct`, or config `ttsSpeaker` | CustomVoice | Emotion/style via natural language |
| **Clone** | `--refaudio` + `--reftext` | Base | Zero-shot voice cloning |
| **VoiceDesign** | `--voice-design "..."` or config `ttsVoiceDesign` | VoiceDesign | Create a voice from a description; config key wins over `ttsSpeaker` if both are set |

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

### Verbose output

Use `-v` / `--verbose` to print the OpenClaw agent reply to **stdout** after a successful chat turn:

```bash
oc-interactive -t "Hello" -v
echo "Hello" | oc-interactive -v
```

Status and progress messages always go to **stderr**. Errors always go to **stderr** (including agent failures, even with `-v`).

### Model caching (performance)

Synthesis uses a persistent **Qwen3-TTS daemon** that keeps the mlx-audio model loaded in memory. The first spoken turn pays a one-time model load cost; later turns reuse the cache and only pay synthesis time.

Use `--debug` (or `OC_INTERACTIVE_DEBUG=1`) to print timing in `daemon.log`:

```
[oc-interactive] openclawMs=1234
[oc-interactive] tts-daemon mode=custom_voice modelReloaded=False loadMs=0 synthMs=1100
```

- `modelReloaded=True` on the first turn is expected; `False` on subsequent turns confirms caching.
- OpenClaw round-trip is separate (`openclawMs`) and may be 30–60s depending on the agent.

The TTS daemon idles out after 30 minutes; the next spoken turn reloads the model once.

### Agent selection

```bash
oc-interactive -t "What's in the news?" --speaker Ryan --agent news
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
| `--agent` | OpenClaw agent short name |
| `-c` / `--config` | Path to `openclaw.json` (default: `~/.config/oc-interactive/openclaw.json`; cached after first turn; `--openclaw-config` is an alias) |
| `--timeout SECONDS` | Max seconds to wait for agent reply and TTS (default: no timeout) |
| `--debug` | Log OpenClaw/TTS timing and cache status (`OC_INTERACTIVE_DEBUG=1`) |

## State files

Under `~/.config/oc-interactive/` (override with `OC_INTERACTIVE_STATE_DIR`):

| File | Purpose |
|------|---------|
| `session.json` | Conversation history, system prompt, cached voice settings / model / config |
| `daemon.sock` | Unix socket IPC |
| `daemon.pid` | Background daemon PID |
| `daemon.log` | Background orchestration daemon logs |
| `tts-daemon.sock` | Unix socket to cached TTS daemon |
| `tts-daemon.pid` | TTS daemon PID |
| `tts-daemon.log` | TTS daemon logs (model load / synth timing) |

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

**stdout** is used only for:

- `/dump`, `/history` JSON output
- `-v` / `--verbose` agent replies on successful chat turns

## Development

```bash
cd oc-interactive
pip install -e .
python -m oc_interactive -t "/history"
```

## License

Apache-2.0 (same as the parent package).
