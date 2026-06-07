import Foundation
import MLX
import MLXNN
import XCTest
@testable import DotsTTS

/// Self-consistency: incremental KV-cached decode (prefill chunk + token-by-token
/// via embeddings) must match the one-shot full forward hidden states. Proves the
/// cache append, RoPE offset, and offset causal mask are correct. No Python ref.
final class KVCacheConsistencyTests: XCTestCase {
    struct ConfigFile: Codable { let quantization: QuantizationSettings.Config? }

    func testCachedDecodeMatchesFullForward() throws {
        let env = ProcessInfo.processInfo.environment
        let repo = env["DOTS_MODEL_REPO"] ?? "/Users/samm/git/sammcj/dots.tts-soar-mlx"
        let fixtures = env["DOTS_FIXTURES"] ?? "/Users/samm/git/dots-mlx-spike"
        let dir = URL(fileURLWithPath: repo).appendingPathComponent("backbone")
        let refURL = URL(fileURLWithPath: fixtures).appendingPathComponent("backbone_reference.safetensors")
        try XCTSkipUnless(
            FileManager.default.fileExists(atPath: dir.appendingPathComponent("model.safetensors").path)
                && FileManager.default.fileExists(atPath: refURL.path),
            "backbone weights or reference fixture not present")

        let cfgData = try Data(contentsOf: dir.appendingPathComponent("config.json"))
        let quant = QuantizationSettings(from: try JSONDecoder().decode(ConfigFile.self, from: cfgData).quantization)
        let backbone = Qwen2Backbone()
        if quant.enabled { quantize(model: backbone, groupSize: quant.groupSize, bits: quant.bits) }
        try WeightLoading.load(backbone, from: dir)

        let ids = try MLX.loadArrays(url: refURL)["input_ids"]!  // (1, L)
        let L = ids.dim(1)
        let full = backbone.hidden(ids)
        eval(full)

        // Prefill the first half in one chunk, decode the rest one token at a time.
        let split = L / 2
        let cache = backbone.makeCache()
        let prefill = backbone.step(embeds: backbone.embed(ids[0..., 0 ..< split]), cache: cache)
        eval(prefill)
        var maxRel = relError(prefill[0..., (split - 1) ..< split], full[0..., (split - 1) ..< split])

        for i in split ..< L {
            let h = backbone.step(embeds: backbone.embed(ids[0..., i ..< (i + 1)]), cache: cache)
            eval(h)
            maxRel = max(maxRel, relError(h, full[0..., i ..< (i + 1)]))
        }
        print("[kv cache consistency] maxRel over \(L) positions = \(maxRel)")
        XCTAssertLessThan(maxRel, 2e-3, "cached decode diverges from full forward")
    }

    private func relError(_ a: MLXArray, _ b: MLXArray) -> Float {
        let d = abs(a - b)
        return (sqrt((d * d).sum()) / sqrt((b * b).sum())).item(Float.self)
    }
}
