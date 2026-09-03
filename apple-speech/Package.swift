// swift-tools-version: 6.2

import PackageDescription

let package = Package(
    name: "AppleSpeechCLI",
    platforms: [.macOS(.v26)],
    products: [
        .executable(name: "apple-speech", targets: ["AppleSpeechCLI"]),
    ],
    targets: [
        .executableTarget(name: "AppleSpeechCLI"),
    ]
)
