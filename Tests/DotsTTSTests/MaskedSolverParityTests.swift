import Foundation
import MLX
import MLXNN
import XCTest
@testable import DotsTTS

/// Parity for the FM solver with the STRUCTURED FM-decode attention mask (the
/// live pipeline path; SolverParityTests covers only full attention). Builds the
/// additive mask (0 keep / -inf drop) from the stored bool fixture and runs the
/// same 10-step Euler+CFG solve as the torch reference.
final class MaskedSolverParityTests: XCTestCase {
    func testMaskedSolverMatchesTorchReference() throws {
        let env = ProcessInfo.processInfo.environment
        let repo = env["DOTS_MODEL_REPO"] ?? "/Users/samm/git/sammcj/dots.tts-soar-mlx"
        let fixtures = env["DOTS_FIXTURES"] ?? "/Users/samm/git/dots-mlx-spike"
        let ditDir = URL(fileURLWithPath: repo).appendingPathComponent("dit")
        let headsURL = URL(fileURLWithPath: repo).appendingPathComponent("heads/model.safetensors")
        let refURL = URL(fileURLWithPath: fixtures).appendingPathComponent("masked_solver_reference.safetensors")
        try XCTSkipUnless(
            FileManager.default.fileExists(atPath: ditDir.appendingPathComponent("model.safetensors").path)
                && FileManager.default.fileExists(atPath: headsURL.path)
                && FileManager.default.fileExists(atPath: refURL.path),
            "dit weights, heads, or masked solver reference not present")

        let dit = DiT()
        try WeightLoading.load(dit, from: ditDir)
        let solver = EulerSolver(dit: dit)
        let heads = try MLX.loadArrays(url: headsURL)
        var coord: [String: MLXArray] = [:]
        for (k, v) in heads where k.hasPrefix("coordinate_proj.") {
            coord[String(k.dropFirst("coordinate_proj.".count))] = v
        }
        try solver.coordinateProj.update(parameters: ModuleParameters.unflattened(coord), verify: .all)
        eval(solver)

        let ref = try MLX.loadArrays(url: refURL)
        // bool fixture (1,L,L): 1.0 keep / 0.0 drop -> additive 0 / -inf, (1,1,L,L) for head broadcast.
        let keep = ref["attn_mask"]!.asType(.float32)
        let additive = MLX.where(keep .> 0.5, MLXArray(Float(0)), MLXArray(-Float.infinity))
            .expandedDimensions(axis: 1)
        let steps = ref["num_steps"]!.item(Int.self)
        let guidance = ref["guidance"]!.item(Float.self)
        let out = solver.solve(
            noise: ref["noise"]!.asType(.float32),
            inputSeq: ref["input_seq"]!.asType(.float32),
            cfgSeq: ref["cfg_seq"]!.asType(.float32),
            gCond: ref["g_cond"]!.asType(.float32),
            numSteps: steps, guidance: guidance, mask: additive)
        eval(out)

        let expected = ref["out_latent"]!.asType(.float32)
        let diff = abs(out - expected)
        let rel = (sqrt((diff * diff).sum()) / sqrt((expected * expected).sum())).item(Float.self)
        let cos = ((out * expected).sum()
            / (sqrt((out * out).sum()) * sqrt((expected * expected).sum()))).item(Float.self)
        print("[masked solver parity] rel=\(rel) cos=\(cos)")
        XCTAssertLessThan(rel, 0.05, "masked solver rel error \(rel) exceeds 0.05")
        XCTAssertGreaterThan(cos, 0.999, "masked solver cosine \(cos) below 0.999")
    }
}
