import Foundation
import MLX

enum ReferenceSampleError: LocalizedError {
    case unsupportedExtension(String)
    case missingArray(String)
    case invalidJSON(String)

    var errorDescription: String? {
        switch self {
        case .unsupportedExtension(let msg): msg
        case .missingArray(let msg): msg
        case .invalidJSON(let msg): msg
        }
    }
}

enum ReferenceSample {
    struct Loaded {
        let audio48k: MLXArray
        let transcript: String
    }

    /// Load reference audio from a WAV/AIFF/etc. file or a safetensors fixture.
    /// Transcript comes from `transcriptOverride` when non-empty; otherwise a sidecar
    /// `{stem}.txt` or `{stem}.json` (`transcript` field) when present; otherwise an
    /// empty transcript selects timbre-only cloning.
    static func load(path: URL, transcriptOverride: String? = nil) throws -> Loaded {
        let ext = path.pathExtension.lowercased()
        let audio: MLXArray
        switch ext {
        case "wav", "wave", "aiff", "aif", "caf", "m4a", "mp3", "flac":
            audio = try WAVIO.loadMono48k(path: path)
        case "safetensors":
            guard let loaded = try MLX.loadArrays(url: path)["ref_audio_48k"] else {
                throw ReferenceSampleError.missingArray("expected key ref_audio_48k in \(path.path)")
            }
            audio = loaded.asType(.float32)
        default:
            throw ReferenceSampleError.unsupportedExtension(
                "unsupported reference sample \(path.path); use WAV or safetensors"
            )
        }
        let transcript: String
        if let override = transcriptOverride?
            .trimmingCharacters(in: .whitespacesAndNewlines), !override.isEmpty
        {
            transcript = override
        } else {
            transcript = try loadTranscript(for: path)
        }
        return Loaded(audio48k: audio, transcript: transcript)
    }

    private static func loadTranscript(for samplePath: URL) throws -> String {
        let stem = samplePath.deletingPathExtension()
        let txt = stem.appendingPathExtension("txt")
        if FileManager.default.fileExists(atPath: txt.path) {
            return try String(contentsOf: txt, encoding: .utf8)
                .trimmingCharacters(in: .whitespacesAndNewlines)
        }

        let json = stem.appendingPathExtension("json")
        if FileManager.default.fileExists(atPath: json.path) {
            struct Meta: Decodable { let transcript: String? }
            let data = try Data(contentsOf: json)
            guard let meta = try? JSONDecoder().decode(Meta.self, from: data) else {
                throw ReferenceSampleError.invalidJSON("could not decode transcript from \(json.path)")
            }
            return (meta.transcript ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        }

        return ""
    }
}
