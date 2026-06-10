import Foundation
import MLX

/// Kaldi-compatible log-mel fbank front-end (matches
/// `torchaudio.compliance.kaldi.fbank` with the dots.tts-soar speaker params).
///
/// 16 kHz mono waveform -> `(T, 80)` mean-normalised log-mel features. The rfft
/// runs in float32 (Metal has no reduced-precision rfft and the whole speaker
/// path stays fp32).
///
/// Params (Kaldi defaults that matter): frame 25ms (400 samples), hop 10ms (160),
/// povey window (hann^0.85), preemph 0.97, remove DC per frame, n_fft 512,
/// snip_edges, mel scale `1127*ln(1+f/700)`, low_freq 20, high_freq Nyquist,
/// natural-log mel energies, then per-bin time-mean subtraction (CMN).
public struct KaldiFbank {
    public let sampleRate: Int
    public let numMelBins: Int
    public let frameLength: Int      // samples (400)
    public let frameShift: Int       // samples (160)
    public let nFFT: Int             // 512
    public let preemph: Float
    public let lowFreq: Float
    public let highFreq: Float
    public let meanNorm: Bool

    private let window: MLXArray       // (400,) povey window, fp32
    private let melBank: MLXArray      // (257, 80) triangular mel filters, fp32

    public init(sampleRate: Int = 16000, numMelBins: Int = 80,
                frameLengthMs: Float = 25.0, frameShiftMs: Float = 10.0,
                preemph: Float = 0.97, lowFreq: Float = 20.0, highFreq: Float = 0.0,
                meanNorm: Bool = true) {
        self.sampleRate = sampleRate
        self.numMelBins = numMelBins
        self.frameLength = Int((frameLengthMs * Float(sampleRate)) / 1000.0)   // 400
        self.frameShift = Int((frameShiftMs * Float(sampleRate)) / 1000.0)     // 160
        // round_to_power_of_two: next pow2 >= frameLength
        var n = 1
        while n < self.frameLength { n <<= 1 }
        self.nFFT = n                                                          // 512
        self.preemph = preemph
        self.lowFreq = lowFreq
        self.highFreq = highFreq > 0 ? highFreq : Float(sampleRate) / 2.0      // Nyquist 8000
        self.meanNorm = meanNorm

        self.window = KaldiFbank.poveyWindow(frameLength)
        self.melBank = KaldiFbank.melFilterBank(
            nFFT: self.nFFT, sampleRate: sampleRate, numMelBins: numMelBins,
            lowFreq: self.lowFreq, highFreq: self.highFreq)
    }

    /// waveform: `(N,)` fp32 at `sampleRate`. Returns `(T, 80)`.
    public func callAsFunction(_ waveform: MLXArray) -> MLXArray {
        let wave = waveform.asType(.float32)
        let n = wave.dim(0)
        // snip_edges frame count. Guard on the sample count, not numFrames:
        // truncating division makes numFrames == 1 for any 241-399 samples, but
        // frame extraction would then gather indices up to frameLength-1 from a
        // shorter array (MLX gather does no bounds check). Kaldi's snip_edges
        // defines zero frames below one full window.
        precondition(n >= frameLength,
                     "waveform of \(n) samples is shorter than one \(frameLength)-sample frame")
        let numFrames = 1 + (n - frameLength) / frameShift

        // Build frame matrix (numFrames, frameLength) via strided gather indices.
        var rowIdx = [Int32]()
        rowIdx.reserveCapacity(numFrames * frameLength)
        for f in 0..<numFrames {
            let start = f * frameShift
            for j in 0..<frameLength { rowIdx.append(Int32(start + j)) }
        }
        let idx = MLXArray(rowIdx).reshaped(numFrames, frameLength)
        var frames = wave[idx]                                  // (T, 400)

        // 2. remove DC offset per frame (subtract frame mean).
        frames = frames - frames.mean(axis: -1, keepDims: true)

        // 3. pre-emphasis on the dc-removed frame: y[i] = x[i] - 0.97*x[i-1],
        //    x[-1] = x[0] (Kaldi shifts with the first sample replicated).
        let shifted = concatenated(
            [frames[0..., 0..<1], frames[0..., 0..<(frameLength - 1)]], axis: -1)  // (T, 400)
        frames = frames - preemph * shifted

        // 4. povey window.
        frames = frames * window                                // broadcast (400,)

        // 5. zero-pad to nFFT and rfft (fp32).
        if nFFT > frameLength {
            let pad = MLXArray.zeros([numFrames, nFFT - frameLength])
            frames = concatenated([frames, pad], axis: -1)      // (T, 512)
        }
        let spectrum = rfft(frames, axis: -1)                   // (T, 257) complex
        // 6. power spectrum |FFT|^2.
        let re = spectrum.realPart()
        let im = spectrum.imaginaryPart()
        let power = re * re + im * im                           // (T, 257)

        // 7. mel filterbank then natural log (floored).
        var mel = power.matmul(melBank)                         // (T, 80)
        // Kaldi/torchaudio floor mel energies at float epsilon before the log,
        // not the denormal minimum: a digitally silent frame must map to
        // log(eps) ~= -15.9, not log(leastNormalMagnitude) ~= -87.3, or CMN
        // spreads the offset into every frame's features.
        let floorVal = MLXArray(Float.ulpOfOne)                 // ~1.19e-7
        mel = MLX.maximum(mel, floorVal)
        mel = MLX.log(mel)                                      // (T, 80)

        // 8. mean-norm (per-bin time mean subtraction / CMN).
        if meanNorm {
            mel = mel - mel.mean(axis: 0, keepDims: true)
        }
        return mel
    }

    // MARK: - window / mel construction

    /// povey window: `(0.5 - 0.5*cos(2*pi*n/(L-1)))^0.85`.
    private static func poveyWindow(_ length: Int) -> MLXArray {
        var w = [Float](repeating: 0, count: length)
        let denom = Float(length - 1)
        for nIdx in 0..<length {
            let hann = 0.5 - 0.5 * cos(2.0 * Float.pi * Float(nIdx) / denom)
            w[nIdx] = pow(hann, 0.85)
        }
        return MLXArray(w)
    }

    private static func melScale(_ freq: Float) -> Float { 1127.0 * log(1.0 + freq / 700.0) }

    /// Kaldi triangular mel filterbank. Returns `(nFFT/2+1, numMelBins)` so a
    /// `(T, bins) = power @ melBank` matmul applies the filters.
    private static func melFilterBank(nFFT: Int, sampleRate: Int, numMelBins: Int,
                                      lowFreq: Float, highFreq: Float) -> MLXArray {
        let numFftBins = nFFT / 2 + 1
        let nyquist = Float(sampleRate) / 2.0
        let fftBinWidth = Float(sampleRate) / Float(nFFT)     // Hz per fft bin
        let melLow = melScale(lowFreq)
        let melHigh = melScale(highFreq)
        // Kaldi: numMelBins+2 equally-spaced mel points; centers are the inner bins.
        let melDelta = (melHigh - melLow) / Float(numMelBins + 1)

        var bank = [Float](repeating: 0, count: numFftBins * numMelBins)
        for m in 0..<numMelBins {
            let leftMel = melLow + Float(m) * melDelta
            let centerMel = melLow + Float(m + 1) * melDelta
            let rightMel = melLow + Float(m + 2) * melDelta
            for k in 0..<numFftBins {
                let hz = Float(k) * fftBinWidth
                if hz < lowFreq || hz > nyquist { continue }
                // Kaldi/torchaudio ramp the triangle weights linearly in mel
                // space, not Hz. melScale is monotonic so the bin-membership
                // bounds are equivalent, but the interior weight differs.
                let mel = melScale(hz)
                var weight: Float = 0
                if mel >= leftMel && mel <= centerMel {
                    weight = (mel - leftMel) / (centerMel - leftMel)
                } else if mel > centerMel && mel <= rightMel {
                    weight = (rightMel - mel) / (rightMel - centerMel)
                }
                if weight > 0 { bank[k * numMelBins + m] = weight }
            }
        }
        return MLXArray(bank).reshaped(numFftBins, numMelBins)
    }
}
