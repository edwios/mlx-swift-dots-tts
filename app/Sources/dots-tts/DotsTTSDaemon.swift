import Foundation
import Darwin

struct TTSDaemonRequest: Codable {
    var text: String
    var refaudio: String
    var reftext: String?
    var model: String
    var language: String?
    var output: String
    var debug: Bool?
}

struct TTSDaemonResponse: Codable {
    var ok: Bool
    var error: String?
    var modelReloaded: Bool?
    var refaudioReloaded: Bool?
    var loadMs: Double?
    var synthMs: Double?
}

enum DotsTTSDaemon {
    private static let idleTimeoutSec: TimeInterval = 30 * 60
    @MainActor private static let session = TTSSession()

    @MainActor
    static func run() async throws {
        // Clients may disconnect before we send a response; ignore SIGPIPE (default kills the process).
        signal(SIGPIPE, SIG_IGN)

        let stateDir = daemonStateDir()
        try FileManager.default.createDirectory(at: stateDir, withIntermediateDirectories: true)
        let sockPath = stateDir.appendingPathComponent("tts-daemon.sock")
        let pidPath = stateDir.appendingPathComponent("tts-daemon.pid")

        if FileManager.default.fileExists(atPath: sockPath.path) {
            try? FileManager.default.removeItem(at: sockPath)
        }

        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { throw TTSDaemonError.socket("socket() failed") }

        let pathBytes = sockPath.path.utf8CString
        let bound = pathBytes.withUnsafeBufferPointer { ptr -> Bool in
            var addr = sockaddr_un()
            addr.sun_family = sa_family_t(AF_UNIX)
            guard ptr.count <= MemoryLayout.size(ofValue: addr.sun_path) else { return false }
            _ = withUnsafeMutablePointer(to: &addr.sun_path) { dest in
                dest.withMemoryRebound(to: CChar.self, capacity: ptr.count) { $0.assign(from: ptr.baseAddress!, count: ptr.count) }
            }
            return bind(fd, withUnsafePointer(to: &addr) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { $0 } }, socklen_t(MemoryLayout<sockaddr_un>.size)) == 0
        }
        guard bound else { throw TTSDaemonError.socket("bind() failed for \(sockPath.path)") }
        guard listen(fd, 5) == 0 else { throw TTSDaemonError.socket("listen() failed") }

        try String(ProcessInfo.processInfo.processIdentifier).write(to: pidPath, atomically: true, encoding: .utf8)
        fputs("[dots-tts-daemon] ready on \(sockPath.path)\n", stderr)

        defer {
            close(fd)
            try? FileManager.default.removeItem(at: sockPath)
            try? FileManager.default.removeItem(at: pidPath)
        }

        var lastActivity = Date()
        while true {
            if Date().timeIntervalSince(lastActivity) > idleTimeoutSec {
                fputs("[dots-tts-daemon] idle timeout, shutting down\n", stderr)
                break
            }

            var pollfd = pollfd(fd: fd, events: Int16(POLLIN), revents: 0)
            let ready = poll(&pollfd, 1, 1000)
            if ready <= 0 { continue }

            var clientAddr = sockaddr_un()
            var clientLen = socklen_t(MemoryLayout<sockaddr_un>.size)
            let clientFD = withUnsafeMutablePointer(to: &clientAddr) { ptr in
                ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { accept(fd, $0, &clientLen) }
            }
            guard clientFD >= 0 else { continue }

            lastActivity = Date()
            let response: TTSDaemonResponse
            do {
                let requestData = try readFrame(fd: clientFD)
                let request = try JSONDecoder().decode(TTSDaemonRequest.self, from: requestData)
                response = try await handle(request: request)
            } catch {
                response = TTSDaemonResponse(ok: false, error: error.localizedDescription)
            }
            try? writeFrame(fd: clientFD, payload: JSONEncoder().encode(response))
            close(clientFD)
        }
    }

    @MainActor
    private static func handle(request: TTSDaemonRequest) async throws -> TTSDaemonResponse {
        let debug = request.debug ?? (ProcessInfo.processInfo.environment["OC_INTERACTIVE_DEBUG"] == "1")
        let modelURL = URL(fileURLWithPath: (request.model as NSString).expandingTildeInPath)
        let refaudioURL = URL(fileURLWithPath: (request.refaudio as NSString).expandingTildeInPath)
        let outputURL = URL(fileURLWithPath: (request.output as NSString).expandingTildeInPath)

        guard FileManager.default.fileExists(atPath: modelURL.path) else {
            throw TTSDaemonError.invalidRequest("model not found: \(modelURL.path)")
        }
        guard FileManager.default.fileExists(atPath: refaudioURL.path) else {
            throw TTSDaemonError.invalidRequest("reference audio not found: \(refaudioURL.path)")
        }

        let metrics = try await session.synthesize(
            text: request.text,
            refaudioURL: refaudioURL,
            refTranscript: request.reftext,
            modelURL: modelURL,
            language: request.language,
            outputURL: outputURL,
            debug: debug
        )

        return TTSDaemonResponse(
            ok: true,
            modelReloaded: metrics.modelReloaded,
            refaudioReloaded: metrics.refaudioReloaded,
            loadMs: metrics.loadMs,
            synthMs: metrics.synthMs
        )
    }

    private static func daemonStateDir() -> URL {
        if let override = ProcessInfo.processInfo.environment["OC_INTERACTIVE_STATE_DIR"] {
            return URL(fileURLWithPath: (override as NSString).expandingTildeInPath)
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".config/oc-interactive")
    }

    private static func readExact(fd: Int32, count: Int) throws -> Data {
        var data = Data(count: count)
        var offset = 0
        while offset < count {
            let n = data.withUnsafeMutableBytes { raw -> Int in
                guard let base = raw.baseAddress else { return -1 }
                return recv(fd, base.advanced(by: offset), count - offset, 0)
            }
            if n <= 0 { throw TTSDaemonError.socket("recv() failed") }
            offset += n
        }
        return data
    }

    private static func readFrame(fd: Int32) throws -> Data {
        let header = try readExact(fd: fd, count: 4)
        let length = Int(UInt32(header[0]) << 24 | UInt32(header[1]) << 16 | UInt32(header[2]) << 8 | UInt32(header[3]))
        return try readExact(fd: fd, count: length)
    }

    private static func writeFrame(fd: Int32, payload: Data) throws {
        var length = UInt32(payload.count).bigEndian
        let header = withUnsafeBytes(of: &length) { Data($0) }
        try writeExact(fd: fd, data: header)
        try writeExact(fd: fd, data: payload)
    }

    private static func writeExact(fd: Int32, data: Data) throws {
        try data.withUnsafeBytes { raw in
            var offset = 0
            while offset < raw.count {
                let n = send(fd, raw.baseAddress!.advanced(by: offset), raw.count - offset, 0)
                if n <= 0 { throw TTSDaemonError.socket("send() failed") }
                offset += n
            }
        }
    }
}

enum TTSDaemonError: Error, LocalizedError {
    case socket(String)
    case invalidRequest(String)

    var errorDescription: String? {
        switch self {
        case .socket(let msg): msg
        case .invalidRequest(let msg): msg
        }
    }
}
