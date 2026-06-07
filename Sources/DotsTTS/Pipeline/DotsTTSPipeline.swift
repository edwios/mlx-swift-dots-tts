import Foundation
import MLX
import MLXNN
import MLXRandom
import Tokenizers

/// End-to-end dots.tts inference in MLX, mirroring DotsTtsModel/_generate_latents_stream.
///
/// Voice cloning (ICL): the reference clip drives BOTH the CAM++ speaker
/// embedding (g_cond into the DiT) AND, with a transcript, the prefill that
/// seeds the AR/FM history with reference patches. The decode loop interleaves a
/// KV-cached Qwen2 step with a masked flow-matching solve per latent patch, the
/// patch encoder re-runs over the causal latent history to produce each LLM
/// input embedding (prefix-stable, so equivalent to the streaming decode_patch),
/// and the EOS head stops generation. Generated latents are denormalised and the
/// BigVGAN/AudioVAE decoder renders 48 kHz audio.
///
/// See the verified algorithm notes for the FM buffer layout and CFG asymmetry.
public final class DotsTTSPipeline {
    public struct Params: Sendable {
        public var numSteps = 10
        public var guidance: Float = 1.2          // runtime default (NOT core.py's 3.0)
        public var speakerScale: Float = 1.5
        public var eosThreshold: Float = 0.8
        public var maxOutputPatches = 600
        public var seed: UInt64 = 0
        public init() {}
    }

    let backbone: Qwen2Backbone
    let dit: DiT
    let solver: EulerSolver
    let vocoder: Vocoder
    let speaker: CAMPPlus
    let fbank: KaldiFbank
    let audioVAE: AudioVAEEncoder
    let patchEncoder: PatchEncoder
    let resampler: Resampler
    let tokenizer: Tokenizer
    let special: DotsSpecialTokens

    // projection heads (raw weights; small, kept as arrays)
    let hiddenProjW, hiddenProjB: MLXArray
    let latentProjW, latentProjB: MLXArray
    let xvecLinW, xvecLinB, xvecLnW, xvecLnB: MLXArray
    let eos0W, eos0B, eos2W, eos2B: MLXArray
    let latentMean, latentStd: MLXArray

    let latentDim = 128
    let patchSize = 4
    let hiddenPatchSize = 1
    let hopSize = 1920

    public init(modelRepo: URL, tokenizer: Tokenizer) throws {
        struct CfgFile: Codable { let quantization: QuantizationSettings.Config? }
        let backboneDir = modelRepo.appendingPathComponent("backbone")
        let q = QuantizationSettings(from: try JSONDecoder().decode(
            CfgFile.self, from: Data(contentsOf: backboneDir.appendingPathComponent("config.json"))).quantization)
        let bb = Qwen2Backbone()
        if q.enabled { quantize(model: bb, groupSize: q.groupSize, bits: q.bits) }
        try WeightLoading.load(bb, from: backboneDir)
        self.backbone = bb

        let dit = DiT()
        try WeightLoading.load(dit, from: modelRepo.appendingPathComponent("dit"))
        self.dit = dit
        self.solver = EulerSolver(dit: dit)

        let voc = Vocoder()
        try WeightLoading.load(voc, from: modelRepo.appendingPathComponent("vocoder"))
        self.vocoder = voc

        let spk = CAMPPlus()
        try WeightLoading.load(spk, from: modelRepo.appendingPathComponent("speaker"))
        self.speaker = spk
        self.fbank = KaldiFbank()

        let vae = AudioVAEEncoder()
        try WeightLoading.load(vae, from: modelRepo.appendingPathComponent("audiovae_encoder"))
        self.audioVAE = vae

        let pe = PatchEncoder()
        try WeightLoading.load(pe, from: modelRepo.appendingPathComponent("patch_encoder"))
        self.patchEncoder = pe

        let heads = try MLX.loadArrays(url: modelRepo.appendingPathComponent("heads/model.safetensors"))
        func h(_ k: String) throws -> MLXArray {
            guard let v = heads[k] else { throw DotsTextError.missingSpecialToken(k) }
            return v.asType(.float32)
        }
        self.hiddenProjW = try h("hidden_proj.weight"); self.hiddenProjB = try h("hidden_proj.bias")
        self.latentProjW = try h("latent_proj.weight"); self.latentProjB = try h("latent_proj.bias")
        self.xvecLinW = try h("xvec_proj.0.weight"); self.xvecLinB = try h("xvec_proj.0.bias")
        self.xvecLnW = try h("xvec_proj.1.weight"); self.xvecLnB = try h("xvec_proj.1.bias")
        self.eos0W = try h("eos_proj.0.weight"); self.eos0B = try h("eos_proj.0.bias")
        self.eos2W = try h("eos_proj.2.weight"); self.eos2B = try h("eos_proj.2.bias")
        // coordinate_proj into the solver.
        var coord: [String: MLXArray] = [:]
        for (k, v) in heads where k.hasPrefix("coordinate_proj.") {
            coord[String(k.dropFirst("coordinate_proj.".count))] = v.asType(.float32)
        }
        try solver.coordinateProj.update(parameters: ModuleParameters.unflattened(coord), verify: .all)

        let stats = try JSONDecoder().decode(
            LatentStats.self, from: Data(contentsOf: modelRepo.appendingPathComponent("latent_stats.json")))
        self.latentMean = MLXArray(stats.mean)
        self.latentStd = sqrt(MLXArray(stats.var))

        let rs = try MLX.loadArrays(url: modelRepo.appendingPathComponent("resampler_48k_16k.safetensors"))
        self.resampler = Resampler(
            origRate: rs["orig"]!.item(Int.self), newRate: rs["new"]!.item(Int.self),
            gcd: rs["gcd"]!.item(Int.self), width: rs["width"]!.item(Int.self),
            torchKernel: rs["kernel"]!.asType(.float32))

        self.tokenizer = tokenizer
        self.special = try DotsSpecialTokens(tokenizer: tokenizer)
        eval(bb, dit, voc, spk, vae, pe, solver)
    }

    struct LatentStats: Codable { let mean: [Float]; let `var`: [Float] }

    // MARK: projection helpers (channels-last x @ W^T + b)
    private func linear(_ x: MLXArray, _ w: MLXArray, _ b: MLXArray) -> MLXArray { matmul(x, w.T) + b }
    private func hiddenProj(_ x: MLXArray) -> MLXArray { linear(x, hiddenProjW, hiddenProjB) }
    private func latentProj(_ x: MLXArray) -> MLXArray { linear(x, latentProjW, latentProjB) }
    private func xvecProj(_ x: MLXArray) -> MLXArray {
        let h = linear(x, xvecLinW, xvecLinB)
        let mu = h.mean(axis: -1, keepDims: true)
        let centered = h - mu
        let v = (centered * centered).mean(axis: -1, keepDims: true)
        return centered * rsqrt(v + 1e-5) * xvecLnW + xvecLnB
    }
    private func normalize(_ x: MLXArray) -> MLXArray { (x - latentMean) / latentStd }
    private func denormalize(_ x: MLXArray) -> MLXArray { x * latentStd + latentMean }
    private func sampleFromLatent(_ meanLogstd: MLXArray) -> MLXArray {
        // meanLogstd: (1, 256, L). chunk on channel dim, sample, -> (1, L, 128).
        let parts = split(meanLogstd, parts: 2, axis: 1)
        let mean = parts[0], logStd = parts[1]
        let z = mean + MLXRandom.normal(mean.shape) * exp(logStd)
        return z.transposed(0, 2, 1)
    }

    /// Speaker conditioning g_cond (1, 1024) from a 48 kHz mono reference clip.
    public func speakerCond(refAudio48k: MLXArray, scale: Float) -> MLXArray {
        let mono16k = resampler(refAudio48k)
        let fb = fbank(mono16k).expandedDimensions(axis: 0)   // (1, T, 80)
        let xvec = speaker(fb).reshaped(1, 512) * scale       // (1, 512)
        return xvecProj(xvec)                                  // (1, 1024)
    }

    // MARK: FM history buffers (interleaved hidden(1)/latent(patchSize))
    private final class State {
        var cond: MLXArray? = nil       // (1, T, 1024)
        var uncond: MLXArray? = nil     // (1, T, 1024)
        var len = 0
        var unnormLatents: MLXArray? = nil  // (1, T*4, 128) for patch-encoder recompute
        var llmCache: [KVCache] = []
        var llmHidden: MLXArray? = nil  // (1, 1, 1536)
    }

    private func append(_ s: State, cond: MLXArray, uncond: MLXArray) {
        s.cond = s.cond.map { concatenated([$0, cond], axis: 1) } ?? cond
        s.uncond = s.uncond.map { concatenated([$0, uncond], axis: 1) } ?? uncond
        s.len += cond.dim(1)
    }
    private func appendHidden(_ s: State, _ hidden: MLXArray) {
        let proj = hiddenProj(hidden)
        append(s, cond: proj, uncond: hiddenProj(MLXArray.zeros(like: hidden)))
    }
    private func appendLatent(_ s: State, _ latent: MLXArray) {
        let proj = latentProj(latent)  // same in both branches
        append(s, cond: proj, uncond: proj)
    }
    private func appendUnnormLatent(_ s: State, _ unnorm: MLXArray) {
        s.unnormLatents = s.unnormLatents.map { concatenated([$0, unnorm], axis: 1) } ?? unnorm
    }

    /// Structured FM-decode attention mask -> additive (1,1,total,total).
    private func fmMask(len: Int, total: Int, dtype: DType) -> MLXArray {
        let latentStart = Int32(total - patchSize)
        let blockStart = Int32(len - hiddenPatchSize)
        let lenA = Int32(len)
        let idx = MLXArray(0 ..< Int32(total))
        let rows = idx.reshaped(total, 1), cols = idx.reshaped(1, total)
        // context = keys in [0..len) (history prefix) OR [latentStart..) (latent block)
        let context = (cols .< lenA) .|| (cols .>= latentStart)
        // last hidden block rows [blockStart..len) and latent rows [latentStart..)
        // both attend to the full context.
        let hidRows = (rows .>= blockStart) .&& (rows .< lenA)
        let latRows = rows .>= latentStart
        var keep = (hidRows .|| latRows) .&& context
        // history-prefix rows [0..blockStart) are causal among themselves.
        let causal = (cols .<= rows) .&& (rows .< blockStart)
        keep = keep .|| causal
        let additive = MLX.where(keep, MLXArray(Float(0), dtype: dtype), MLXArray(-Float.infinity, dtype: dtype))
        return additive.reshaped(1, 1, total, total)
    }

    /// Solve one latent patch from the current FM history.
    private func decodeNextPatch(_ s: State, gCond: MLXArray, p: Params) -> MLXArray {
        let total = s.len + patchSize
        let pad = MLXArray.zeros([1, patchSize, 1024])
        let inputSeq = concatenated([s.cond!, pad], axis: 1)
        let cfgSeq = concatenated([s.uncond!, pad], axis: 1)
        let mask = fmMask(len: s.len, total: total, dtype: inputSeq.dtype)
        let noise = MLXRandom.normal([1, patchSize, latentDim])
        return solver.solve(noise: noise, inputSeq: inputSeq, cfgSeq: cfgSeq, gCond: gCond,
                            numSteps: p.numSteps, guidance: p.guidance, mask: mask)
    }

    /// EOS stop: softmax(eos_proj(hidden))[...,1] > threshold.
    private func shouldStop(_ hidden: MLXArray, threshold: Float) -> Bool {
        let h = silu(linear(hidden, eos0W, eos0B))
        let logits = linear(h, eos2W, eos2B)               // (1,1,2)
        let prob = softmax(logits, axis: -1)[0, 0, 1]
        return prob.item(Float.self) > threshold
    }

    /// New LLM input embedding for the just-generated patch: re-run the patch
    /// encoder over the whole (causal) unnorm latent history, take the last token.
    private func patchEmbedding(_ s: State) -> MLXArray {
        let emb = patchEncoder(s.unnormLatents!)            // (1, K, 1536)
        return emb[0..., (emb.dim(1) - 1) ..< emb.dim(1)]   // (1, 1, 1536)
    }

    /// Generate a 48 kHz waveform (1, 1, samples). Voice cloning needs a
    /// reference clip + its transcript; targetText is what to speak.
    public func generate(targetText: String, refAudio48k: MLXArray, refTranscript: String, params: Params = Params()) -> MLXArray {
        MLXRandom.seed(params.seed)
        let gCond = speakerCond(refAudio48k: refAudio48k, scale: params.speakerScale)

        // reference -> sampled latents (unnorm), trim last patch, normalised patches.
        let pad = padTo(refAudio48k, multiple: patchSize * hopSize)
        var refLatents = sampleFromLatent(audioVAE(pad))           // (1, L, 128) unnorm
        refLatents = refLatents[0..., 0 ..< (refLatents.dim(1) - patchSize)]
        let pc = refLatents.dim(1) / patchSize
        let refLatentsTrim = refLatents[0..., 0 ..< (pc * patchSize)]
        let promptPatches = normalize(refLatentsTrim).reshaped(1, pc, patchSize, latentDim)

        // schedule + prefill embeddings (patch encoder over the reference latents).
        let maxAudio = pc + params.maxOutputPatches
        let schedule = DotsTemplate.generationSchedule(
            promptText: refTranscript, targetText: targetText, maxAudioTokens: maxAudio,
            tokenizer: tokenizer, special: special)
        let promptEmbed = patchEncoder(refLatentsTrim)             // (1, pc, 1536)

        // span positions (audio_gen_span); prefill consumes the first pc.
        let spans = schedule.enumerated().filter { $0.element == special.audioGenSpan }.map { $0.offset }
        let prefillEnd = spans[pc]   // first decode span

        let s = State()
        s.llmCache = backbone.makeCache()
        s.unnormLatents = refLatentsTrim

        // prefill: embeds for schedule[:prefillEnd] with prompt patches injected at spans[0..<pc].
        let ids = MLXArray(schedule[0 ..< prefillEnd].map { Int32($0) }).reshaped(1, prefillEnd)
        let tokEmbeds = backbone.embed(ids)
        // prompt span positions are contiguous (spans[0..<pc]); replace that trailing
        // block with the patch embeddings (prefillEnd == spans[0] + pc).
        let p0 = spans[0]
        let embeds = concatenated([tokEmbeds[0..., 0 ..< p0, 0...], promptEmbed], axis: 1)
        let prefillHidden = backbone.step(embeds: embeds, cache: s.llmCache)  // (1, prefillEnd, 1536)
        eval(prefillHidden)
        s.llmHidden = prefillHidden[0..., (prefillEnd - 1) ..< prefillEnd, 0...]

        // build FM history from the reference (mirrors _prefill).
        var cursor = 0
        for i in 0 ..< pc {
            let sp = spans[i]
            if sp > cursor { appendHidden(s, prefillHidden[0..., (sp - 1) ..< sp, 0...]) }
            appendLatent(s, promptPatches[0..., i])  // (1, patchSize, 128)
            appendHidden(s, prefillHidden[0..., sp ..< (sp + 1), 0...])  // next is always a span
            cursor = sp + 1
        }

        // decode loop.
        var outPatches: [MLXArray] = []
        var dropFirst = true   // prompt prefill regenerates the prompt tail
        let totalSpans = spans.count
        for step in 0 ..< (totalSpans - pc) {
            let stop = shouldStop(s.llmHidden!, threshold: params.eosThreshold)
            let z = decodeNextPatch(s, gCond: gCond, p: params)   // (1, patchSize, 128) normalised
            eval(z)
            // consume: append latent history (normalised), patch-encode, step LLM.
            appendLatent(s, z)
            let unnorm = denormalize(z)
            appendUnnormLatent(s, unnorm)
            let llmEmbed = patchEmbedding(s)
            s.llmHidden = backbone.step(embeds: llmEmbed, cache: s.llmCache)
            let isLast = (step == totalSpans - pc - 1)
            if !isLast { appendHidden(s, s.llmHidden!) }
            if dropFirst { dropFirst = false } else { outPatches.append(unnorm) }
            eval(s.llmHidden!)
            if stop { break }
        }

        guard !outPatches.isEmpty else { return MLXArray.zeros([1, 1, 0]) }
        let latents = concatenated(outPatches, axis: 1).transposed(0, 2, 1)  // (1, 128, T)
        let wav = vocoder(latents)
        eval(wav)
        return wav
    }

    private func padTo(_ audio: MLXArray, multiple: Int) -> MLXArray {
        var w = audio
        if w.ndim == 2 { w = w.reshaped(w.dim(w.ndim - 1)) }
        let n = w.dim(0)
        let target = Int((Double(n) / Double(multiple)).rounded(.up)) * multiple
        if target > n { w = padded(w, widths: [.init((0, target - n))]) }
        return w
    }
}
