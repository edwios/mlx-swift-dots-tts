# dots-tts CLI

Command-line voice cloning with [mlx-swift-dots-tts](../README.md). Runs the full dots.tts-soar pipeline natively on Apple Silicon (no Python).

## Requirements

- macOS 15+ on Apple Silicon (M-series)
- Xcode 16+ (builds the MLX Metal kernels; `swift build` alone is not sufficient)
- A [dots.tts-soar-mlx](https://huggingface.co/smcleod/dots.tts-soar-mlx) model checkout (default: `./dots.tts-soar-mlx` at the repo root)

## Build

From this directory:

```bash
make build
```

This runs `xcodebuild`, which compiles the Swift code **and** the MLX Metal kernels (`mlx-swift_Cmlx.bundle`). Plain `swift build` is not sufficient for a runnable binary (see the [parent README](../README.md#build)). Outputs land in `app/.build/`:

- `dots-tts` — the CLI binary
- `mlx-swift_Cmlx.bundle` — MLX GPU kernels (must stay next to the binary)

Release build:

```bash
make CONFIG=release build
```

For a quick compile-only check (no runnable MLX binary):

```bash
make compile
```

## Usage

```bash
./.build/dots-tts \
  --text "Hello world." \
  --refaudio path/to/reference.wav \
  --output ./output.wav \
  --model ../dots.tts-soar-mlx
```

With an explicit language tag (prefixed automatically as `[EN]`):

```bash
./.build/dots-tts -t "Hello world." -r reference.wav -l EN
```

With an inline language tag in the text (omit `-l`):

```bash
./.build/dots-tts -t "[SV] Hej, världen!" -r reference.wav
```

Short flags:

```bash
./.build/dots-tts -t "Hello world." -r reference.wav -l EN -o output.wav -m ../dots.tts-soar-mlx
```

### Arguments

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `-t`, `--text` | yes | — | UTF-8 text to synthesise (may include an inline `[CODE]` tag when `-l` is omitted) |
| `-r`, `--refaudio` | yes | — | Reference audio (WAV/AIFF/MP3/etc. or `.safetensors` with `ref_audio_48k`) |
| `-l`, `--language` | no | — | Language tag to prefix (`EN`, `sv`, `cantonese`, …). Omit when already in `--text` |
| `-o`, `--output` | no | `./output.wav` | Output WAV path (48 kHz mono) |
| `-m`, `--model` | no | `./dots.tts-soar-mlx` | Model repo directory |

### Language tags

- **`-l` set** — the runtime prefixes `[CODE]` to the text (e.g. `-l sv` → `[SV]Hej…`).
- **`-l` omitted** — text is used as-is; put the tag in `--text` if needed (e.g. `"[SWE] Hej, världen!"`).
- Do not combine both: `-l sv` plus `[SWE]` in the text will produce conflicting tags.

### Reference transcript (sidecar)

Continuation cloning (reference audio **and** transcript) gives the best match to the reference voice. Place one of these next to the reference audio file:

- `reference.txt` — plain UTF-8 transcript of the reference clip
- `reference.json` — `{"transcript": "..."}` (same shape as the e2e fixtures)

If no sidecar is found, the app uses an empty transcript (timbre-only cloning).

### Inference defaults

These match the library README example and `DotsTTSPipeline.Params` defaults (see [EndToEndTests.swift](../Tests/DotsTTSTests/EndToEndTests.swift) for the chunked render path):

| Parameter | Default |
|-----------|---------|
| `numSteps` | 10 (Euler steps; for MeanFlow checkpoints this is the NFE) |
| `guidance` | 1.2 |
| `speakerScale` | 1.5 |
| `odeMethod` | `euler` |
| `eosThreshold` | 0.8 |
| `maxOutputPatches` | 600 |
| `seed` | 1 |

Long input text is split on sentence boundaries (`.`, `!`, `?`) and rendered chunk-by-chunk with a short gap, mirroring the e2e test — this bounds peak memory during vocoder decode.

### Model variants

Point `--model` at any self-contained variant from [dots.tts-soar-mlx](https://huggingface.co/smcleod/dots.tts-soar-mlx):

| Variant | Path | Notes |
|---------|------|-------|
| Default | repo root | 4-bit backbone, F32 acoustics (~4 GB) |
| Full int4 | `4bit/` | Smallest footprint (~1.6 GB) |
| Full int8 | `8bit/` | Balanced (~2.6 GB) |

## License

Apache-2.0 (same as the parent package).
