import DotsTTS
import Foundation
import MLX
import Tokenizers

struct TTSSynthMetrics: Codable, Sendable {
    var modelReloaded: Bool
    var refaudioReloaded: Bool
    var loadMs: Double
    var synthMs: Double
}

/// Cached MLX TTS pipeline keyed by model + reference audio paths.
@MainActor
final class TTSSession {
    private var cachedModelPath: String?
    private var cachedRefaudioPath: String?
    private var pipeline: DotsTTSPipeline?
    private var reference: ReferenceSample.Loaded?

    func synthesize(
        text: String,
        refaudioURL: URL,
        modelURL: URL,
        language: String?,
        outputURL: URL,
        debug: Bool
    ) async throws -> TTSSynthMetrics {
        let loadStart = CFAbsoluteTimeGetCurrent()
        var modelReloaded = false
        var refaudioReloaded = false

        let modelPath = modelURL.path
        let refPath = refaudioURL.path

        if pipeline == nil || cachedModelPath != modelPath {
            if debug {
                fputs("[dots-tts-daemon] loading MLX model: \(modelPath)\n", stderr)
            }
            let tokenizer = try await AutoTokenizer.from(
                modelFolder: modelURL.appendingPathComponent("backbone")
            )
            pipeline = try DotsTTSPipeline(modelRepo: modelURL, tokenizer: tokenizer)
            cachedModelPath = modelPath
            modelReloaded = true
            MLX.Memory.cacheLimit = 4 * 1024 * 1024 * 1024
        }

        if reference == nil || cachedRefaudioPath != refPath {
            if debug {
                fputs("[dots-tts-daemon] loading reference audio: \(refPath)\n", stderr)
            }
            reference = try ReferenceSample.load(path: refaudioURL)
            cachedRefaudioPath = refPath
            refaudioReloaded = true
        }

        guard let pipeline, let reference else {
            throw TTSSessionError.notReady
        }

        let loadMs = (CFAbsoluteTimeGetCurrent() - loadStart) * 1000
        if debug {
            fputs(
                String(
                    format: "[dots-tts-daemon] cache status modelReloaded=%@ refaudioReloaded=%@ loadMs=%.0f\n",
                    modelReloaded ? "yes" : "no",
                    refaudioReloaded ? "yes" : "no",
                    loadMs
                ),
                stderr
            )
        }

        let synthStart = CFAbsoluteTimeGetCurrent()

        var params = DotsTTSPipeline.Params()
        params.numSteps = 10
        params.guidance = 1.2
        params.speakerScale = 1.5
        params.odeMethod = .euler
        params.language = language
        params.eosThreshold = 0.8
        params.maxOutputPatches = 600
        params.seed = 1

        let chunks = Chunking.splitIntoChunks(text)
        let gap = MLXArray.zeros([Int(WAVIO.targetSampleRate * 0.12)], dtype: .float32)
        var pieces: [MLXArray] = []

        for (index, chunk) in chunks.enumerated() {
            let piece = pipeline.generate(
                targetText: chunk,
                refAudio48k: reference.audio48k,
                refTranscript: reference.transcript,
                params: params
            ).reshaped(-1)
            eval(piece)
            MLX.Memory.clearCache()
            if debug {
                fputs(
                    "[dots-tts-daemon] chunk \(index + 1)/\(chunks.count) samples=\(piece.dim(0))\n",
                    stderr
                )
            }
            if index > 0 { pieces.append(gap) }
            pieces.append(piece)
        }

        let samples = concatenated(pieces, axis: 0)
        eval(samples)
        try WAVIO.writeMono48k(samples: samples, path: outputURL)

        let synthMs = (CFAbsoluteTimeGetCurrent() - synthStart) * 1000
        if debug {
            fputs(
                String(format: "[dots-tts-daemon] synthesized in %.0fms -> %@\n", synthMs, outputURL.path),
                stderr
            )
        }

        return TTSSynthMetrics(
            modelReloaded: modelReloaded,
            refaudioReloaded: refaudioReloaded,
            loadMs: loadMs,
            synthMs: synthMs
        )
    }
}

enum TTSSessionError: Error, LocalizedError {
    case notReady

    var errorDescription: String? {
        switch self {
        case .notReady: "TTS session not ready"
        }
    }
}
