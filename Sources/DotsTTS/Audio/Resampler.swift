import Foundation
import MLX

/// Sinc-interpolation resampler matching torchaudio.functional.resample with
/// resampling_method="sinc_interp_kaiser", lowpass_filter_width=64, rolloff=0.95.
///
/// The kaiser-windowed sinc kernel is precomputed in Python (torchaudio's
/// _get_sinc_resample_kernel) and supplied here, so the Swift side only runs the
/// pad -> strided conv -> reshape -> trim that _apply_sinc_resample_kernel does.
/// For 48k -> 16k: gcd 16000, origFreq = orig/gcd = 3 (decimation stride),
/// newFreq = new/gcd = 1, kernel (newFreq, 1, kernelLen).
public struct Resampler {
    let origRate: Int
    let newRate: Int
    let origFreq: Int   // orig / gcd  (conv stride)
    let newFreq: Int    // new / gcd   (phases per step)
    let width: Int      // left pad; right pad = width + origFreq
    let kernel: MLXArray // MLX conv weight layout (newFreq, kernelLen, 1)

    /// torchKernel: torchaudio kernel (newFreq, 1, kernelLen) -> transposed to
    /// MLX channels-last (newFreq, kernelLen, 1).
    public init(origRate: Int, newRate: Int, gcd: Int, width: Int, torchKernel: MLXArray) {
        self.origRate = origRate
        self.newRate = newRate
        self.origFreq = origRate / gcd
        self.newFreq = newRate / gcd
        self.width = width
        self.kernel = torchKernel.transposed(0, 2, 1)
    }

    /// waveform: (T,) or (1, T). Returns (newLen,) 1-D.
    public func callAsFunction(_ waveform: MLXArray) -> MLXArray {
        var w = waveform
        if w.ndim == 2 { w = w.reshaped(w.dim(1)) }
        let length = w.dim(0)
        // pad left `width`, right `width + origFreq`; present as (1, paddedLen, 1).
        let x = padded(w, widths: [.init((width, width + origFreq))])
            .reshaped(1, length + 2 * width + origFreq, 1)
        // strided conv with the multiphase kernel -> (1, steps, newFreq).
        let conv = conv1d(x, kernel, stride: origFreq)
        // interleave phases back into time: (1, steps, newFreq) -> (steps*newFreq,).
        let resampled = conv.reshaped(-1)
        let target = Int((Double(newRate) * Double(length) / Double(origRate)).rounded(.up))
        return resampled[0 ..< min(target, resampled.dim(0))]
    }
}
