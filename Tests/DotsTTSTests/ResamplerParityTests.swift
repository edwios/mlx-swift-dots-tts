import Foundation
import MLX
import XCTest
@testable import DotsTTS

/// Parity: the Swift sinc resampler must match torchaudio.functional.resample
/// (sinc_interp_kaiser) using the precomputed kernel from resample_reference.
final class ResamplerParityTests: XCTestCase {
    func testResamplerMatchesTorchaudio() throws {
        let env = ProcessInfo.processInfo.environment
        let fixtures = env["DOTS_FIXTURES"] ?? "/Users/samm/git/dots-mlx-spike"
        let refURL = URL(fileURLWithPath: fixtures).appendingPathComponent("resample_reference.safetensors")
        try XCTSkipUnless(FileManager.default.fileExists(atPath: refURL.path), "resample reference not present")

        let ref = try MLX.loadArrays(url: refURL)
        let resampler = Resampler(
            origRate: ref["orig"]!.item(Int.self),
            newRate: ref["new"]!.item(Int.self),
            gcd: ref["gcd"]!.item(Int.self),
            width: ref["width"]!.item(Int.self),
            torchKernel: ref["kernel"]!.asType(.float32))

        let out = resampler(ref["x"]!.asType(.float32))
        let expected = ref["y"]!.asType(.float32).reshaped(-1)
        eval(out)
        XCTAssertEqual(out.dim(0), expected.dim(0), "resampled length mismatch")
        let diff = abs(out - expected)
        let maxAbs = diff.max().item(Float.self)
        let rel = (sqrt((diff * diff).sum()) / sqrt((expected * expected).sum())).item(Float.self)
        print("[resampler parity] rel=\(rel) maxAbs=\(maxAbs) len=\(out.dim(0))")
        XCTAssertLessThan(rel, 1e-4, "resampler rel error \(rel) exceeds 1e-4")
    }
}
