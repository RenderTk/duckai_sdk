import DuckAI
import SwiftUI

@main struct DuckAIExampleApp: App {
    var body: some Scene {
        WindowGroup { ChatView() }
    }
}

struct ChatView: View {
    @Environment(\.scenePhase) private var scenePhase
    @State private var prompt = "Explain recursion with a short example."
    @State private var output = "Ready"
    @State private var task: Task<Void, Never>?
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Duck.ai Swift SDK").font(.title)
            TextField("Message", text: $prompt).textFieldStyle(.roundedBorder)
            HStack {
                Button("Send") { send() }.disabled(task != nil)
                Button("Cancel") { task?.cancel() }.disabled(task == nil)
            }
            ScrollView { Text(output).frame(maxWidth: .infinity, alignment: .leading).accessibilityIdentifier("response") }
        }
        .padding()
        .task { if ProcessInfo.processInfo.arguments.contains("--live-check") { prompt = "Reply with: native mobile session works"; send() } }
        .onDisappear { task?.cancel() }
        .onChange(of: scenePhase) { if $0 == .background { task?.cancel() } }
    }

    @MainActor private func send() {
        output = ""
        task = Task { @MainActor in
            defer { task = nil }
            var ai: DuckAI?
            do {
                let client = try DuckAI(model: Model.mistralSmall4); ai = client
                if ProcessInfo.processInfo.arguments.contains("--live-check") {
                    let url = Bundle.main.url(forResource: "smoke-files", withExtension: "json")!
                    let fixtures = try JSONDecoder().decode(JSONValue.self, from: Data(contentsOf: url)).array!
                    let parts = try client.prepareFiles(fixtures.map { Attachment(data: Data(base64Encoded: $0["data"]!.string!)!, filename: $0["filename"]!.string!) }, model: Model.gpt6Luna)
                    guard parts.contains(where: { $0["type"]?.string == "image" }), parts.contains(where: { $0["type"]?.string == "file" }) else { throw DuckAIError(detail: "Native attachment preparation failed") }
                    print("NATIVE ATTACHMENT CHECK PASSED")
                }
                let chat = try client.conversation()
                let stream = chat.stream(prompt); defer { stream.close() }
                for try await delta in stream {
                    output += delta
                }
                if ProcessInfo.processInfo.arguments.contains("--live-check") {
                    let followup = try await chat.chat("What were the last three words in your previous answer?")
                    output += "\nFollow-up: " + followup.text
                    output += "\nLIVE CHECK PASSED: \(chat.messages.count) history messages"
                    print(output)
                }
            } catch is CancellationError { output += "\nCancelled" }
            catch { output += "\n\(error)"; print("LIVE CHECK FAILED: \(error)") }
            await ai?.close()
        }
    }
}
