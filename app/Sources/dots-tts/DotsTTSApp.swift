import Foundation

@main
enum DotsTTSApp {
    static func main() async {
        if CommandLine.arguments.contains("--tts-daemon") {
            do {
                try await DotsTTSDaemon.run()
            } catch {
                fputs("dots-tts-daemon error: \(error)\n", stderr)
                Foundation.exit(1)
            }
            return
        }

        do {
            try await DotsTTSCLI.main()
        } catch {
            fputs("dots-tts error: \(error)\n", stderr)
            Foundation.exit(1)
        }
    }
}
