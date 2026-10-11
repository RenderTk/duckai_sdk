// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "DuckAI",
    platforms: [.iOS(.v15), .macOS(.v12)],
    products: [.library(name: "DuckAI", targets: ["DuckAI"])],
    dependencies: [.package(url: "https://github.com/weichsel/ZIPFoundation.git", from: "0.9.20")],
    targets: [
        .target(name: "DuckAI", dependencies: ["ZIPFoundation"], resources: [.process("Resources")]),
        .testTarget(name: "DuckAITests", dependencies: ["DuckAI"], resources: [.copy("Fixtures")])
    ]
)
