# Code Review 1 (2026-06-11)

Multi-agent review of the full library and test suite. Four reviewers covered the codebase by subsystem (pipeline/backbone, DiT/flow-matching/reference encoders, vocoder/speaker/audio DSP, support/config/packaging/tests), then three independent adversarial validators re-checked every finding against the code, including the mlx-swift kernel sources where claims depended on MLX behaviour. Findings that did not survive validation were dropped; corrections from validation are folded in below.

Core numerics held up well. The CFG combine, RK and MeanFlow schedules, RoPE/masking, conv padding arithmetic, SnakeBeta, resampler polyphase maths, LSTM gate order and pooling statistics were all checked against the reference semantics and no high-severity numerical defects were found. The issues below are mostly input-validation crashes, two real front-end parity deviations in the speaker fbank, and test-suite gaps.

## Action checklist

### Medium severity

- [x] Validate reference audio length at the top of `generate()` and throw on clips too short to produce at least one latent patch, instead of force-unwrap crashing (`DotsTTSPipeline.swift`) - done as a `precondition(pc >= 1)` (see Resolution note on preconditions vs throws)
- [x] Interpolate mel filterbank triangle weights in mel space rather than Hz to match `torchaudio.compliance.kaldi` (`Fbank.swift`)
- [x] Change the log-energy floor from `Float.leastNormalMagnitude` to `Float.ulpOfOne` to match the Kaldi/torchaudio epsilon floor (`Fbank.swift`)
- [ ] Add threshold assertions to the four assertion-free tests, or rename and gate them as opt-in diagnostics (`PrefillBufferParityTests`, `PipelineStageTests.testPatchEncoderStages`, `VocoderParityTests.testVocoderStageDiagnostics`, `ScheduleDumpTests`) - deferred (threshold tuning is error-prone without per-metric expected ranges; risk of flaky tests)
- [ ] Fix skip guards so every file a test reads is covered by `XCTSkipUnless` (a fresh clone currently fails on `ScheduleDumpTests`; partial fixture sets fail two more) - deferred (this machine has the fixtures, so the fresh-clone red state can't be reproduced/verified here)
- [ ] Add an env-gated strict mode (e.g. `DOTS_REQUIRE_FIXTURES=1`) so fixture-equipped machines and any future CI fail rather than silently skip the entire numerical suite - deferred (pairs with the skip-guard work above)

### Low severity

- [x] Validate `Params.maxOutputPatches >= 1` (0 currently traps on a span index) (`DotsTTSPipeline.swift`) - done as `precondition`
- [x] Replace the five force-unwrapped resampler safetensors keys with the throwing guard pattern already used for the heads file (`DotsTTSPipeline.swift`)
- [x] Enforce or document the mono shape contract for `refAudio48k`; a (channels, N) stereo array currently dies in an MLX reshape (`DotsTTSPipeline.swift`) - done as `precondition` in `padTo` and `speakerCond`
- [ ] Surface early-EOS empty output to the caller (throw, return optional, or warn) instead of returning a silent zero-length buffer (`DotsTTSPipeline.swift`) - deferred (the natural fix is API-shaped; see Resolution note)
- [x] Pass guidance into `solverStep` as a parameter and delete the mutable `guidanceScale` instance state (`EulerSolver.swift`)
- [x] Precondition `numSteps > 0` and `nfe > 0` in the solver entry points; 0 currently returns raw noise silently (`EulerSolver.swift`)
- [ ] Make solver/checkpoint mode mismatch loud: `solve` should reject a MeanFlow-built DiT and `solveMeanFlow` should require one (`EulerSolver.swift`, `DiT.swift`) - deferred (a mode guard could trip the `debugSolveStep` test hook; needs DiT to expose its mode and careful test coverage)
- [x] Precondition input length divisibility in `PatchEncoder.callAsFunction` instead of trapping in a reshape
- [x] Correct the `PatchEncoder` header doc (the attention has no rotary and no qk-norm; the header claims both)
- [x] Guard `numFrames` against sub-frame waveforms in `Fbank`; clips of 241-399 samples currently gather out of bounds
- [ ] Consolidate test helpers into one `TestSupport.swift` (shared `Meta`, fixture path resolution, and a single pair of clearly named `relL2`/`relMax` metrics) - deferred (test-only cleanup, touches many files)
- [x] Change the mlx-swift dependency from `from: "0.31.4"` to `.upToNextMinor` to match the comment's stated 0.31.x pin (`Package.swift`)
- [ ] Throw a descriptive error when a `quantization` config block is present but `bits` is missing, instead of silently loading unquantised (`Quantization.swift`) - deferred (the throwing variant ripples through the non-throwing `quantOf`/`init(from:)`; the defaulting variant risks loading quantised when it should not)
- [ ] Make the Makefile metallib pick deterministic (newest mtime) and echo the chosen source path - deferred (build-tooling, not verifiable from the Swift test suite)

## Resolution (2026-06-11)

Actioned the input-validation guards, the two front-end numerics fixes, the solver-state cleanup, and the dependency pin. All build clean and the full test suite passes (32 tests, 5 skipped, 0 failures) on a fixture-equipped machine.

Applied: M1, M2, M3, L1, L2, L3, L5, L6, L8, L9, L10, L11.

Deferred with reasons inline above: M4, M5, M6 (test-infra; the fresh-clone failure can't be reproduced here and assertion-threshold tuning is risky without fixtures-free runs), L4, L7, L12, L13, L14.

Verification:

- The two `Fbank` fixes are confirmed against the parity fixtures, not just asserted. Mel-space triangle interpolation (M2) plus the epsilon floor (M3) cut the speaker fbank parity rel error from 0.00334 to 0.000188 (~18x closer to `torchaudio.compliance.kaldi`), and the assertion-free `stage fbank` diagnostic from rel 1.76 to 5.3e-5.
- The `EulerSolver` guidance refactor (L5) is behaviour-preserving: solver parity rel is byte-identical at 0.0031110162 before and after.
- Removed the now-dead `invMelScale` helper that M2 orphaned.

Note on preconditions vs throws. The review suggested throwing typed errors for the `generate()` input-validation findings (M1, L1, L3) and for early-EOS (L4). `generate()` is a non-throwing public API (`-> MLXArray`), so converting it to `throws` is a source-breaking change for the consumer (Cloney's `DotsSwiftSynthesiser`). To keep this pass non-breaking, these are implemented as `precondition`s with descriptive messages: a deep, inscrutable trap becomes a clear early failure at the call boundary, without changing the signature. L4 (early-EOS, which returns rather than traps) was left for a coordinated API change since a precondition there would crash a currently-returning path. Converting `generate()` to `throws` for true recoverability is a sensible follow-up to coordinate with the consumer. The resampler-key fix (L2) does throw, because `init` is already `throws`.

## Findings

### Pipeline and backbone

#### M1. Reference clips of about 0.16 s or less crash the process

`Sources/DotsTTS/Pipeline/DotsTTSPipeline.swift` (medium, validated)

The AudioVAE encoder downsamples by a factor of 1920, and `generate()` pads the reference to a multiple of one patch (4 latents = 7680 samples) before trimming the final patch off the latents. A clip of 7680 samples or fewer therefore yields exactly one patch, the trim leaves zero latents, and the prefill loop never populates the flow-matching history. The first decode step then force-unwraps `s.cond!` on nil (validation traced an even earlier possible trap: the patch encoder running on a (1, 0, 128) array). A very short or empty reference recording is a plausible user input and currently produces an unrecoverable crash rather than a thrown error.

Fix: validate the post-trim patch count at the top of `generate()` and throw a typed error.

#### L1. `Params.maxOutputPatches = 0` traps on a span index

`Sources/DotsTTS/Pipeline/DotsTTSPipeline.swift` (low, validated)

The text schedule produces `pc + maxOutputPatches` spans, so with the public, settable `maxOutputPatches` at 0 the lookup `spans[pc]` is out of range and Swift traps. The `maxAudioTokens > 0` precondition in `TextSchedule` passes whenever there is at least one prompt patch, so it does not protect this path.

Fix: validate or clamp `maxOutputPatches >= 1`.

#### L2. Force-unwrapped resampler tensor keys bypass the throwing init

`Sources/DotsTTS/Pipeline/DotsTTSPipeline.swift` (low, validated)

The init is `throws` and loads the heads file through a throwing guard helper, but the five resampler keys (`orig`, `new`, `gcd`, `width`, `kernel`) are force-unwrapped. A truncated or stale `resampler_48k_16k.safetensors` (a realistic partial-download condition) traps instead of throwing.

Fix: use the same guard-and-throw pattern as the heads loader.

#### L3. Stereo input dies in an MLX reshape with no shape contract

`Sources/DotsTTS/Pipeline/DotsTTSPipeline.swift` (low, validated)

Both `padTo` and `speakerCond` flatten 2-D input with a reshape that is only valid for (1, N). A (channels, N) array from a typical audio loader fails the reshape, and in mlx-swift that is a fatal runtime error rather than a catchable one. Nothing documents or enforces the mono contract on `refAudio48k`.

Fix: validate the shape with a clear error message (or explicitly downmix).

#### L4. Early EOS returns silent empty audio

`Sources/DotsTTS/Pipeline/DotsTTSPipeline.swift` (low, validated)

Because the first decoded patch is always dropped, EOS on the first or second step yields an empty patch list and `generate()` returns a zero-length buffer. The only diagnostics sit behind `DOTS_DEBUG_EOS=1`. This condition usually indicates a bad reference/transcript pairing, which the caller would want to know about.

Fix: throw, return an optional, or at minimum log a warning on the non-debug path.

### DiT and flow matching

#### L5. Solver guidance is mutable instance state

`Sources/DotsTTS/FlowMatching/EulerSolver.swift` (low, validated with adjustment)

`solve()` writes its `guidance` parameter into the instance var `guidanceScale`, which `solverStep` reads. Two concurrent `solve()` calls on a shared solver race on the scale (silently wrong CFG weighting). Validation narrowed the original claim: `solverStep` is private, so stale-value misuse requires either concurrent calls (the pipeline is documented single-actor-owned) or a future internal edit. Still worth fixing because the fix is trivial and removes a footgun.

Fix: pass guidance down the call chain and delete the instance var.

#### L6. `numSteps = 0` silently returns raw noise

`Sources/DotsTTS/FlowMatching/EulerSolver.swift` (low, validated with adjustment)

`solve(numSteps: 0)` computes `dt = +inf` (no trap in Float), runs zero iterations, and returns the input noise unchanged; `solveMeanFlow(nfe: 0)` behaves the same. The result is pure-noise audio with no error, which is painful to diagnose downstream. Validation corrected one detail: negative values trap in the range construction, so 0 is the only silent case.

Fix: `precondition(numSteps > 0)` and `precondition(nfe > 0)` at the solver entry points.

#### L7. MeanFlow checkpoint run through the CFG path degrades silently

`Sources/DotsTTS/DiT/DiT.swift`, `EulerSolver.swift` (low, validated)

The DiT conditioning uses `if let durationEmbedder, let duration`, which makes "embedder present, duration absent" silently legal. `EulerSolver.solve` never passes `duration`, so running a MeanFlow-built DiT through the CFG path drops the dt conditioning without any diagnostic and additionally applies CFG to a distilled model that expects none. `generate()` gates correctly on the mode, but the public solver API and the `debugSolveStep` test hook do not.

Fix: have `solve` reject a MeanFlow-built DiT and `solveMeanFlow` require one.

#### L8. PatchEncoder traps on latent lengths not divisible by 4

`Sources/DotsTTS/Reference/PatchEncoder.swift` (low, validated)

After the stride-2 downsample, an input length that is not a multiple of 4 produces an odd token count and the projection reshape raises a fatal MLX shape error. The normal pipeline path trims first, so reachability is limited to the public `callAsFunction`/`debugStages` surface, whose doc comment only hints at the requirement.

Fix: precondition the divisibility with a descriptive message.

#### L9. PatchEncoder header doc contradicts the implementation

`Sources/DotsTTS/Reference/PatchEncoder.swift` (low, validated)

The class header describes the transformer as "NeoX rotary theta 10000, affine-free qk-norm", but the attention implementation deliberately has neither, and its own inline comment records that enabling either breaks parity by three orders of magnitude. A maintainer trusting the header could "fix" attention and silently destroy parity.

Fix: correct the header to state no rotary and no qk-norm.

### Speaker front-end (fbank)

#### M2. Mel filterbank triangles interpolated in Hz instead of mel

`Sources/DotsTTS/Speaker/Fbank.swift` (medium, validated with adjustment)

The triangle endpoints match the reference exactly, but the weight ramps inside each triangle are computed linearly in Hz, where Kaldi and `torchaudio.compliance.kaldi.get_mel_banks` interpolate in mel space. Validation quantified the deviation: roughly 0.8% absolute weight error per bin, essentially uniform across all 80 bands (not low-frequency-dominant as first reported), so it is one contributor to the loose 0.25 front-end test tolerance rather than the main one. It stays medium because it silently contradicts the documented parity contract ("matches torchaudio.compliance.kaldi.fbank") and perturbs every speaker embedding.

Fix: convert each FFT bin frequency to mel and interpolate against the mel endpoints.

#### M3. Log-energy floor is denormal-min instead of epsilon

`Sources/DotsTTS/Speaker/Fbank.swift` (medium, validated)

Mel energies are floored at `Float.leastNormalMagnitude` (~1.2e-38) before the log; the reference floors at float epsilon (~1.2e-7). Digitally silent frames (exact zeros after per-frame DC removal, common in trimmed or padded clips) therefore produce log values near -87.3 where the reference produces -15.9, and the subsequent cepstral mean normalisation spreads that shift into every frame's features, degrading the speaker embedding.

Fix: floor at `Float.ulpOfOne`.

#### L10. Sub-frame waveforms gather out of bounds

`Sources/DotsTTS/Speaker/Fbank.swift` (low, validated)

Swift's truncating division makes `numFrames` equal 1 for waveforms of 241-399 samples, so the "too short" precondition passes and frame extraction gathers indices 0-399 from a shorter array. Validation confirmed against the MLX Metal kernel source that gather performs no upper-bound check or clamp, so this silently reads adjacent memory and produces a garbage speaker embedding rather than trapping. Kaldi's snip_edges semantics define zero frames for this case.

Fix: require `n >= frameLength` explicitly.

### Support, packaging and configuration

#### L11. mlx-swift dependency is not the pin the comment claims

`Package.swift` (low, validated)

The comment says "mlx-swift 0.31.x (matches Cloney's pin)" but `from: "0.31.4"` is up-to-next-major, permitting anything below 1.0.0. `Package.resolved` is not honoured by downstream consumers of the library product, and the Makefile's metallib workaround explicitly requires the same mlx-swift version, so a drifted resolution can produce runtime kernel mismatches, not just compile errors.

Fix: `.upToNextMinor(from: "0.31.4")`.

#### L12. Quantization block without `bits` silently loads as unquantised

`Sources/DotsTTS/Support/Quantization.swift` (low, validated)

A `quantization` config block missing `bits` yields `enabled = false`, so plain `Linear` layers are built and weight loading later fails on unexpected `scales`/`biases` keys with a generic key-verification error that never mentions quantisation. The diagnostic lands far from the cause.

Fix: throw a descriptive error (or default `bits` the way `groupSize` is defaulted) when the block is present but incomplete.

#### L13. Makefile picks an arbitrary metallib

`Makefile` (low, validated)

The `METALLIB` discovery takes the first `default.metallib` that `find` enumerates across all DerivedData builds and a hardcoded sibling repo path, with no mtime ordering, no version correlation with the resolved mlx-swift checkout, and no echo of which file was chosen. A stale pick produces missing-kernel failures or wrong numerics that look like porting bugs.

Fix: sort candidates by mtime, take the newest, and echo the chosen source path.

### Test suite

#### M4. Four tests have no assertions and can never fail

`Tests/DotsTTSTests` (medium, validated)

`testPrefillDecodeBufferMatchesPython`, `testPatchEncoderStages`, `testVocoderStageDiagnostics` and `testDumpSchedulePrefix` compute parity metrics and only print them; none contains an `XCTAssert` or `XCTFail`, including in helper closures. The first is named "MatchesPython" yet a total regression in the prefill buffer would still pass. They provide the appearance of regression coverage without any.

Fix: add threshold assertions, or rename them as dumps and gate them behind an opt-in env var.

#### M5. Skip guards do not cover everything the tests read

`Tests/DotsTTSTests` (medium, validated with adjustment)

`ScheduleDumpTests` has no skip guard at all and loads a tokenizer from a hardcoded default path, so it errors (rather than skips) on every machine without the private model repo, meaning a fresh clone currently fails `swift test`. `PrefillBufferParityTests` and `PipelineStageTests` guard some but not all of the files they read, so they hard-fail on machines with partial fixture sets. This breaks the suite's stated contract that fixture-dependent tests skip cleanly.

Fix: cover every file each test reads (tokenizer directory, trajectory fixtures, model repo) with `XCTSkipUnless`, matching the pattern used elsewhere.

#### M6. Without private fixtures, the numerical suite silently vanishes

`Tests/DotsTTSTests` (medium, validated with adjustment)

Eighteen of twenty-one test files skip when fixtures under hardcoded personal default paths (`~/git/dots-mlx-spike`, `~/git/sammcj/dots.tts-soar-mlx`) are absent. Validation found fixture-free coverage is even thinner than first reported: only the five language-tag tests and `testVersionPresent` assert anything, and the end-to-end render test is additionally gated behind `DOTS_RUN_E2E=1`. Once M5 is fixed, a fixture-less run goes green while verifying none of the numerics, which would make any future CI permanently green and useless without anyone noticing.

Fix: add a strict mode (`DOTS_REQUIRE_FIXTURES=1`) that turns missing fixtures into failures on equipped machines/CI, and consider committing one tiny fixture for a cheap always-on parity gate.

#### L14. Duplicated test helpers with two incompatible "rel" metrics

`Tests/DotsTTSTests` (low, validated)

The identical `Meta` struct is declared five times, cosine and relative-error helpers are re-implemented per file, and two genuinely different definitions ("rel" as L2-relative vs max-abs over scale) are asserted against similarly named thresholds, making tolerances non-comparable across tests. The fixture-path defaults are also resolved per file, which is exactly how the M5 guard gap crept in.

Fix: one `TestSupport.swift` with shared `Meta`, path resolution, `cosine`, `relL2` and `relMax`.

## Validation notes

Every finding above survived an independent adversarial validation pass; none were refuted. Material corrections from validation are recorded inline: the solver-state race requires API misuse to trigger (M5-adjacent L5), negative solver step counts trap rather than failing silently (L6), the mel interpolation error is uniform across bands and sub-1% per bin (M2), the MLX gather out-of-bounds behaviour was confirmed against the Metal kernel source rather than assumed (L10), and a fresh clone is currently red rather than silently green (M5/M6).
