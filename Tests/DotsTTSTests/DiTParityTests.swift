import Foundation
import MLX
import XCTest
@testable import DotsTTS

/// Parity check: the Swift DiT must match the torch fp32 reference output
/// (dit_reference.safetensors) within fp32 cross-framework drift (~3.6% rel was
/// observed for the Python MLX port; allow a small margin).
///
/// Needs the converted weights + fixtures, pointed to by env vars so the test is
/// skipped (not failed) on machines without them:
///   DOTS_MODEL_REPO  -> the dots.tts-soar-mlx checkout (contains dit/)
///   DOTS_FIXTURES    -> dir holding dit_reference.safetensors
final class DiTParityTests: XCTestCase {
    func testDiTMatchesTorchReference() throws {
        let env = ProcessInfo.processInfo.environment
        let repo = env["DOTS_MODEL_REPO"] ?? "/Users/samm/git/sammcj/dots.tts-soar-mlx"
        let fixtures = env["DOTS_FIXTURES"] ?? "/Users/samm/git/dots-mlx-spike"
        let ditDir = URL(fileURLWithPath: repo).appendingPathComponent("dit")
        let refURL = URL(fileURLWithPath: fixtures).appendingPathComponent("dit_reference.safetensors")
        try XCTSkipUnless(
            FileManager.default.fileExists(atPath: ditDir.appendingPathComponent("model.safetensors").path)
                && FileManager.default.fileExists(atPath: refURL.path),
            "DiT weights or reference fixture not present"
        )

        let ref = try MLX.loadArrays(url: refURL)
        let x = ref["x"]!, t = ref["t"]!, g = ref["g_cond"]!, y = ref["y"]!

        let dit = DiT()
        try WeightLoading.load(dit, from: ditDir)

        let out = dit(x, timesteps: t, gCond: g)
        eval(out)

        let maxAbs = abs(out - y).max().item(Float.self)
        let refScale = abs(y).max().item(Float.self)
        let rel = maxAbs / refScale
        print("DiT parity: maxAbs \(maxAbs)  rel \(rel)  (ref scale \(refScale))")
        XCTAssertLessThan(rel, 0.05, "DiT output diverges from torch reference")
    }
}
