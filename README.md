# mlx-swift-dots-tts

Native [MLX](https://github.com/ml-explore/mlx-swift) port of [dots.tts-soar](https://huggingface.co/rednote-hilab/dots.tts-soar), a continuous autoregressive TTS system with flow-matching synthesis and voice cloning.

Runs the full pipeline on Apple silicon with no Python daemon: Qwen2 AR backbone, flow-matching DiT, BigVGAN/AudioVAE vocoder, and CAM++ x-vector speaker conditioning. Converted and (optionally) quantised weights are published at [smcleod/dots.tts-soar-mlx](https://huggingface.co/smcleod/dots.tts-soar-mlx).

## Status

All components are ported and numerically parity-checked against the PyTorch reference, and the end-to-end pipeline renders voice-cloned speech. The backbone, DiT and patch encoder support per-component MLX quantisation (int4/int8); the vocoder and AudioVAE encoder can run at full or reduced (bf16/fp16) precision.

## Usage

```swift
import DotsTTS
import Tokenizers

let tokenizer = try await AutoTokenizer.from(modelFolder: modelRepo.appendingPathComponent("backbone"))
let pipeline = try DotsTTSPipeline(modelRepo: modelRepo, tokenizer: tokenizer)

var params = DotsTTSPipeline.Params()
params.numSteps = 10
params.seed = 1
let audio48k = pipeline.generate(
    targetText: "Hello world.",
    refAudio48k: referenceSamples,   // MLXArray, mono 48 kHz
    refTranscript: "the reference transcript",
    params: params)
```

`modelRepo` is a directory with one subdirectory per component (`backbone/`, `dit/`, `patch_encoder/`, `vocoder/`, `speaker/`, `audiovae_encoder/`, `heads/`) plus shared config, as published at [smcleod/dots.tts-soar-mlx](https://huggingface.co/smcleod/dots.tts-soar-mlx).

## Why MLX

On an M5 Max, the AR backbone decodes ~2x faster in MLX than PyTorch-MPS at fp32 and ~3.8x at int4 (it's memory-bandwidth-bound). The flow-matching DiT - the dominant cost - runs ~2-2.8x faster in MLX fp32 (compute-bound, so quantisation there saves memory, not time). Quantisation shrinks the 8.2GB fp32 core to ~2-3GB.

## Compared to the reference implementation

### Performance

The speedups come from native execution and quantisation, not from changing the decode algorithm:

- **Native MLX/Metal kernels** - no Python or PyTorch-MPS dispatch overhead. ~2x faster backbone decode and ~2-2.8x faster DiT vs PyTorch-MPS at fp32 on an M5 Max (see [Why MLX](#why-mlx)).
- **Per-component integer quantisation** (int4/int8) - the upstream runtime is float-only (bf16/fp16/fp32). Quantising the bandwidth-bound AR backbone to int4 gives ~3.8x decode throughput and shrinks the 8.2GB fp32 core to ~2-3GB.
- **MeanFlow few-step path** - when the loaded checkpoint is the NFE=4 distilled DiT, it's auto-detected from the config (no CFG, fewer solver steps), cutting the dominant DiT cost ~2.3x vs the 10-step flow-matching path with no measurable quality loss.
- **fp16 vocoder/AudioVAE** - half-precision decode of the dominant non-quantisable stage, lower peak RAM with no audible loss.
- **Fused attention** via MLX's `scaledDotProductAttention`, plus KV-cached incremental decode (one growing cache per layer).

No speculative decoding (in either implementation). It doesn't map cleanly onto dots: the AR head denoises a continuous VAE latent per step via flow-matching rather than sampling from a discrete token vocabulary, so there's no cheap discrete draft to verify in parallel. The win on this architecture is making each step cheaper (quantisation, MeanFlow), not proposing steps speculatively.

What this port deliberately leaves out: streaming, in-runtime text normalisation, and any training path. Language tags are supported as an optional `[CODE]` prefix, but without the reference's `langcodes`/`lingua` name resolution and auto-detection.

### Features

This port targets one use case - low-latency, low-memory zero-shot voice cloning on Apple silicon - rather than the full research surface of the [official PyTorch repo](https://github.com/rednote-hilab/dots.tts). It reuses the published model architecture and weights; everything below is about runtime capability, not model quality (the underlying checkpoints are the same).

| Capability                                          | dots.tts (official, PyTorch)                                                                         | This port (MLX / Swift)                                                           |
| --------------------------------------------------- | ---------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| Hardware                                            | CUDA, Apple MPS, CPU                                                                                 | Apple silicon only (Metal)                                                        |
| Runtime                                             | Python + PyTorch                                                                                     | Native Swift, no Python                                                           |
| Continuation cloning (reference audio + transcript) | Yes                                                                                                  | Yes                                                                               |
| CAM++ x-vector speaker conditioning                 | Yes                                                                                                  | Yes                                                                               |
| Timbre cloning without a transcript                 | Yes (x-vector-only)                                                                                  | Yes (empty-transcript path)                                                       |
| Random-voice (no reference)                         | Yes (fine-tuned single-speaker checkpoint)                                                           | No                                                                                |
| Output sample rate                                  | 48 kHz                                                                                               | 48 kHz                                                                            |
| Flow-matching checkpoints (Pretrain / SCA)          | Yes                                                                                                  | Yes                                                                               |
| MeanFlow few-step checkpoint (NFE=4)                | Yes                                                                                                  | Yes (auto-detected from config)                                                   |
| ODE solvers                                         | euler                                                                                                | euler, midpoint, rk4                                                              |
| Weight precision                                    | bf16 / fp16 / fp32                                                                                   | fp32 + per-component int4/int8; fp16/bf16 vocoder                                 |
| Streaming (1T1A interleaved, low-latency)           | Yes                                                                                                  | No                                                                                |
| Multilingual language tags (24 languages)           | Yes                                                                                                  | Opt-in `[CODE]` tag; auto-detect is a coarse CJK heuristic (no langcodes/lingua)   |
| Text normalisation                                  | Yes (`--normalize-text`)                                                                             | No (expects pre-normalised text)                                                  |
| Instruction / emotion template                      | Exposed (`instruction_tts`)\*                                                                        | No                                                                                |
| Fine-tuning / training                              | Yes (fine-tune entry point)                                                                          | No (inference only)                                                               |
| Web UI                                              | Gradio app                                                                                           | No (library)                                                                      |
| Tuneable params                                     | num-steps, guidance, speaker-scale, ode-method, language, seed, max-length, normalize-text, template | numSteps, guidance, speakerScale, odeMethod, language, eosThreshold, maxOutputPatches, seed |

\* The `instruction_tts` template exists in the reference runtime, but the released soar checkpoint has no instruction/style channel - prompted directives are spoken verbatim rather than acted on - so this port does not surface it.

## Build

```
swift build
swift test
```

`swift build` compiles, but the SwiftPM debug/test binary crashes on the first MLX op because the Metal kernels aren't compiled into it. Run MLX tests with `mlx.metallib` colocated next to the test runner, or consume the package from an app built with xcodebuild. Heavy parity and end-to-end tests are gated behind `DOTS_RUN_E2E=1` / `DOTS_RUN_DECODE_PARITY=1` and read fixtures from paths set by `DOTS_MODEL_REPO` / `DOTS_FIXTURES`.

## License

Apache-2.0. Model weights and architecture derive from rednote-hilab's dots.tts-soar (Apache-2.0).
