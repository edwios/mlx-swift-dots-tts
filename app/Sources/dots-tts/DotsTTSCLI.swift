import ArgumentParser
import DotsTTS
import Foundation
import MLX
import Tokenizers

@main
struct DotsTTSCLI: AsyncParsableCommand {
    static let configuration = CommandConfiguration(
        commandName: "dots-tts",
        abstract: "Zero-shot voice cloning TTS with dots.tts-soar on Apple Silicon.",
        discussion: """
        Synthesises speech from UTF-8 text using a reference voice sample. The model \
        repo defaults to ./dots.tts-soar-mlx (see smcleod/dots.tts-soar-mlx on Hugging Face).

        For best continuation cloning, place a sidecar transcript next to the reference \
        audio: {refaudio}.txt (plain text) or {refaudio}.json with a "transcript" field. \
        Without a sidecar, timbre-only cloning is used (empty transcript).

        Language: pass -l to have a [CODE] tag prefixed automatically (e.g. -l sv -> [SV]). \
        Omit -l when the text already includes an inline tag (e.g. "[SWE] Hej, världen!").
        """
    )

    @Option(name: [.short, .long], help: "UTF-8 text to synthesise.")
    var text: String

    @Option(name: [.short, .long], help: "Reference audio (WAV, AIFF, MP3, etc., or safetensors).")
    var refaudio: String

    @Option(name: [.short, .long], help: "Language tag to prefix (e.g. EN, sv, cantonese). Omit when the tag is already in --text.")
    var language: String?

    @Option(name: [.short, .long], help: "Output WAV path.")
    var output: String = "./output.wav"

    @Option(name: [.short, .long], help: "Path to the dots.tts-soar-mlx model directory.")
    var model: String = "./dots.tts-soar-mlx"

    mutating func run() async throws {
        let modelURL = URL(fileURLWithPath: (model as NSString).expandingTildeInPath)
        let refaudioURL = URL(fileURLWithPath: (refaudio as NSString).expandingTildeInPath)
        let outputURL = URL(fileURLWithPath: (output as NSString).expandingTildeInPath)

        guard FileManager.default.fileExists(atPath: modelURL.path) else {
            throw ValidationError("model directory not found: \(modelURL.path)")
        }
        guard FileManager.default.fileExists(atPath: refaudioURL.path) else {
            throw ValidationError("reference audio not found: \(refaudioURL.path)")
        }

        let reference = try ReferenceSample.load(path: refaudioURL)
        if reference.transcript.isEmpty {
            fputs("warning: no sidecar transcript found; using timbre-only cloning\n", stderr)
        }

        let tokenizer = try await AutoTokenizer.from(
            modelFolder: modelURL.appendingPathComponent("backbone")
        )
        let pipeline = try DotsTTSPipeline(modelRepo: modelURL, tokenizer: tokenizer)

        var params = DotsTTSPipeline.Params()
        params.numSteps = 10
        params.guidance = 1.2
        params.speakerScale = 1.5
        params.odeMethod = .euler
        params.language = language
        params.eosThreshold = 0.8
        params.maxOutputPatches = 600
        params.seed = 1

        MLX.Memory.cacheLimit = 4 * 1024 * 1024 * 1024

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
            fputs(
                "[dots-tts] chunk \(index + 1)/\(chunks.count) samples=\(piece.dim(0)): \(chunk)\n",
                stderr
            )
            if index > 0 { pieces.append(gap) }
            pieces.append(piece)
        }

        let samples = concatenated(pieces, axis: 0)
        eval(samples)
        let duration = Double(samples.dim(0)) / WAVIO.targetSampleRate
        fputs(
            "[dots-tts] wrote \(samples.dim(0)) samples (\(String(format: "%.2f", duration))s) -> \(outputURL.path)\n",
            stderr
        )
        try WAVIO.writeMono48k(samples: samples, path: outputURL)
    }
}
