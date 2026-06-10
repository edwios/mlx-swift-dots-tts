import Foundation
import Tokenizers

/// Special-token IDs the dots schedule + decode loop key on. Resolved from the
/// loaded tokenizer's added vocab (Qwen2 base + dots audio tokens).
public struct DotsSpecialTokens: Sendable {
    public let audioGenStart: Int
    public let audioGenSpan: Int
    public let audioCompSpan: Int
    public let textCondEnd: Int

    static let audioGenStartToken = "<|audio_gen_start|>"
    static let audioGenSpanToken = "<|audio_gen_span|>"
    static let audioCompSpanToken = "<|audio_comp_span|>"
    static let textCondEndToken = "<|text_cond_end|>"

    public init(tokenizer: Tokenizer) throws {
        func id(_ token: String) throws -> Int {
            guard let v = tokenizer.convertTokenToId(token) else {
                throw DotsTextError.missingSpecialToken(token)
            }
            return v
        }
        self.audioGenStart = try id(Self.audioGenStartToken)
        self.audioGenSpan = try id(Self.audioGenSpanToken)
        self.audioCompSpan = try id(Self.audioCompSpanToken)
        self.textCondEnd = try id(Self.textCondEndToken)
    }
}

public enum DotsTextError: Error {
    case missingSpecialToken(String)
    case missingWeight(String)
}

/// Resolves a user-supplied language string to the model-side tag content that
/// gets wrapped as `[CODE]` and prefixed to the text, mirroring the runtime's
/// `attach_language_tag` / `normalize_language_code`. Returns nil when no tag
/// should be attached (the default - upstream's `none`).
///
/// This is a lightweight resolver, not the full `langcodes` path: it accepts the
/// codes/names the app surfaces (plus any bare 2-3 letter ISO code), maps the
/// Cantonese special case to the runtime's accent tag, and treats `auto_detect`
/// as a coarse CJK-vs-not heuristic (no `lingua` dependency).
public enum DotsLanguageTag {
    /// The bracket content (e.g. "EN", "ZH", "口音:粤语"), or nil for no tag.
    public static func code(for language: String?, text: String) -> String? {
        guard let raw = language?.trimmingCharacters(in: .whitespacesAndNewlines), !raw.isEmpty
        else { return nil }
        let lower = raw.lowercased()
        if lower == "none" || lower == "unknown" { return nil }
        if raw.hasPrefix("口音:") { return raw }
        if lower == "auto_detect" || lower == "auto" {
            return DotsTemplate.containsCJK(text) ? "ZH" : "EN"
        }
        switch lower {
        case "en", "english": return "EN"
        case "zh", "chinese", "mandarin", "zh-cn", "putonghua": return "ZH"
        case "yue", "cantonese", "zh-yue": return "口音:粤语"
        case "ja", "jp", "japanese": return "JA"
        case "ko", "korean": return "KO"
        default:
            // Bare ISO-ish code (2-3 letters): pass through uppercased.
            if (2...3).contains(raw.count), raw.allSatisfy({ $0.isLetter }) {
                return raw.uppercased()
            }
            return nil
        }
    }
}

/// Builds the generation schedule for the default "tts" template
/// `[文本]{text}[文本对应语音]{audio}`. Mirrors build_generation_schedule:
/// each literal segment and the text are tokenized independently
/// (add_special_tokens=False), then the audio block expands to one
/// audio_gen_start followed by `maxAudioTokens` audio_gen_span tokens.
///
/// For voice cloning the reference transcript is prepended to the target text
/// (runtime concatenates `prompt_text + text`); the first `prompt_patch_count`
/// of the audio_gen_span positions are consumed by prefill as the reference
/// audio, the rest are generated.
public enum DotsTemplate {
    public static let textPrefix = "[文本]"
    public static let audioPrefix = "[文本对应语音]"

    public static func generationSchedule(
        promptText: String?,
        targetText: String,
        maxAudioTokens: Int,
        tokenizer: Tokenizer,
        special: DotsSpecialTokens,
        language: String? = nil
    ) -> [Int] {
        precondition(maxAudioTokens > 0, "maxAudioTokens must be positive")
        // Mirror runtime `_process_prompt_text` / `_process_text`: strip both, and
        // for non-CJK prompts append a trailing space before concatenating with the
        // target. Without the space the boundary (".The") tokenises differently from
        // Python's ". The", shifting the whole prefill by a token and corrupting the
        // KV cache, which degrades the cloned voice and runs generation long.
        let prompt = (promptText ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let target = targetText.trimmingCharacters(in: .whitespacesAndNewlines)
        var text: String
        if prompt.isEmpty {
            text = target
        } else {
            let separator = containsCJK(prompt) ? "" : " "
            text = prompt + separator + target
        }
        // Optional language tag: the runtime prefixes `[CODE]` to the front of the
        // combined prompt+target text (on prompt_text when present, else target).
        // Off by default; only attached when the caller asks for a language.
        if let code = DotsLanguageTag.code(for: language, text: text) {
            let tag = "[\(code)]"
            if !text.hasPrefix(tag) { text = tag + text }
        }
        var ids: [Int] = []
        ids += tokenizer.encode(text: textPrefix, addSpecialTokens: false)
        ids += tokenizer.encode(text: text, addSpecialTokens: false)
        ids += tokenizer.encode(text: audioPrefix, addSpecialTokens: false)
        ids.append(special.audioGenStart)
        ids += Array(repeating: special.audioGenSpan, count: maxAudioTokens)
        return ids
    }

    /// True if the text contains CJK ideographs or Japanese kana. Used to match
    /// the runtime's language rule for whether to insert a prompt/target space.
    static func containsCJK(_ s: String) -> Bool {
        for scalar in s.unicodeScalars {
            let v = scalar.value
            if (0x4E00...0x9FFF).contains(v)      // CJK Unified Ideographs
                || (0x3400...0x4DBF).contains(v)  // CJK Ext A
                || (0x3040...0x309F).contains(v)  // Hiragana
                || (0x30A0...0x30FF).contains(v)  // Katakana
                || (0xF900...0xFAFF).contains(v)  // CJK Compatibility Ideographs
            { return true }
        }
        return false
    }
}
