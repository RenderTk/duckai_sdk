// swift-tools-version: 5.9
import PackageDescription
let package = Package(name:"DuckAILiveCheck", platforms:[.macOS(.v12)], dependencies:[.package(path:"../..")], targets:[.executableTarget(name:"LiveCheck",dependencies:[.product(name:"DuckAI",package:"swift")])])
