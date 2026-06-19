// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "dots-tts-app",
    platforms: [
        .macOS(.v15),
    ],
    products: [
        .executable(name: "dots-tts", targets: ["dots-tts"]),
    ],
    dependencies: [
        .package(path: ".."),
        .package(url: "https://github.com/apple/swift-argument-parser.git", from: "1.5.0"),
    ],
    targets: [
        .executableTarget(
            name: "dots-tts",
            dependencies: [
                .product(name: "DotsTTS", package: "mlx-swift-dots-tts"),
                .product(name: "ArgumentParser", package: "swift-argument-parser"),
            ]
        ),
    ]
)
