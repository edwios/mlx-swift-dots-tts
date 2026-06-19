@preconcurrency import AVFoundation
import Foundation
import MLX

enum WAVIOError: LocalizedError {
    case unsupportedFormat(String)
    case conversionFailed(String)

    var errorDescription: String? {
        switch self {
        case .unsupportedFormat(let msg): msg
        case .conversionFailed(let msg): msg
        }
    }
}

enum WAVIO {
    static let targetSampleRate = 48_000.0

    /// Load a mono 48 kHz float waveform from a WAV (or other AVFoundation-readable) file.
    static func loadMono48k(path: URL) throws -> MLXArray {
        let file = try AVAudioFile(forReading: path)
        guard let targetFormat = AVAudioFormat(
            commonFormat: .pcmFormatFloat32,
            sampleRate: targetSampleRate,
            channels: 1,
            interleaved: false
        ) else {
            throw WAVIOError.unsupportedFormat("could not create 48 kHz mono float format")
        }

        let inputFormat = file.processingFormat
        let frameCount = AVAudioFrameCount(file.length)
        guard frameCount > 0 else {
            throw WAVIOError.unsupportedFormat("audio file is empty: \(path.path)")
        }

        guard let inputBuffer = AVAudioPCMBuffer(pcmFormat: inputFormat, frameCapacity: frameCount) else {
            throw WAVIOError.conversionFailed("could not allocate input buffer")
        }
        try file.read(into: inputBuffer)

        let monoInput: AVAudioPCMBuffer
        if inputFormat.channelCount == 1 {
            monoInput = inputBuffer
        } else {
            guard let downmixFormat = AVAudioFormat(
                commonFormat: inputFormat.commonFormat,
                sampleRate: inputFormat.sampleRate,
                channels: 1,
                interleaved: false
            ) else {
                throw WAVIOError.conversionFailed("could not create mono downmix format")
            }
            guard let downmixed = AVAudioPCMBuffer(
                pcmFormat: downmixFormat,
                frameCapacity: frameCount
            ) else {
                throw WAVIOError.conversionFailed("could not allocate downmix buffer")
            }
            guard let converter = AVAudioConverter(from: inputFormat, to: downmixFormat) else {
                throw WAVIOError.conversionFailed("could not create channel downmix converter")
            }
            var consumed = false
            var error: NSError?
            converter.convert(to: downmixed, error: &error) { _, outStatus in
                if consumed {
                    outStatus.pointee = .noDataNow
                    return nil
                }
                consumed = true
                outStatus.pointee = .haveData
                return inputBuffer
            }
            if let error { throw error }
            monoInput = downmixed
        }

        let resampled: AVAudioPCMBuffer
        if monoInput.format.sampleRate == targetSampleRate {
            resampled = monoInput
        } else {
            guard let converter = AVAudioConverter(from: monoInput.format, to: targetFormat) else {
                throw WAVIOError.conversionFailed("could not create resampler")
            }
            let ratio = targetSampleRate / monoInput.format.sampleRate
            let outCapacity = AVAudioFrameCount((Double(monoInput.frameLength) * ratio).rounded(.up))
            guard let outBuffer = AVAudioPCMBuffer(pcmFormat: targetFormat, frameCapacity: outCapacity) else {
                throw WAVIOError.conversionFailed("could not allocate resample buffer")
            }
            var consumed = false
            var error: NSError?
            converter.convert(to: outBuffer, error: &error) { _, outStatus in
                if consumed {
                    outStatus.pointee = .noDataNow
                    return nil
                }
                consumed = true
                outStatus.pointee = .haveData
                return monoInput
            }
            if let error { throw error }
            resampled = outBuffer
        }

        guard let channelData = resampled.floatChannelData?[0] else {
            throw WAVIOError.conversionFailed("missing float channel data after conversion")
        }
        let count = Int(resampled.frameLength)
        let samples = Array(UnsafeBufferPointer(start: channelData, count: count))
        return MLXArray(samples).asType(.float32)
    }

    /// Write mono float samples to a 48 kHz WAV file.
    static func writeMono48k(samples: MLXArray, path: URL) throws {
        let floats = samples.reshaped(-1).asArray(Float.self)
        let count = floats.count

        guard let format = AVAudioFormat(
            commonFormat: .pcmFormatFloat32,
            sampleRate: targetSampleRate,
            channels: 1,
            interleaved: false
        ) else {
            throw WAVIOError.unsupportedFormat("could not create output WAV format")
        }

        let parent = path.deletingLastPathComponent()
        if !parent.path.isEmpty && parent.path != "/" {
            try FileManager.default.createDirectory(at: parent, withIntermediateDirectories: true)
        }

        let file = try AVAudioFile(
            forWriting: path,
            settings: format.settings,
            commonFormat: .pcmFormatFloat32,
            interleaved: false
        )
        guard let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(count)) else {
            throw WAVIOError.conversionFailed("could not allocate output buffer")
        }
        buffer.frameLength = AVAudioFrameCount(count)
        floats.withUnsafeBufferPointer { src in
            guard let dst = buffer.floatChannelData?[0] else { return }
            dst.update(from: src.baseAddress!, count: count)
        }
        try file.write(from: buffer)
    }
}
