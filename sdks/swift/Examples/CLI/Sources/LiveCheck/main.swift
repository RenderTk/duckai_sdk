import AppKit
import DuckAI

let app = NSApplication.shared
app.setActivationPolicy(.prohibited)
Task { @MainActor in
    do {
        let ai = try DuckAI(model: Model.mistralSmall4)
        let chat = try ai.conversation()
        let first = try await chat.chat("Reply with: native session works")
        guard first.done, !first.text.isEmpty else { fatalError("Empty or incomplete live reply") }
        print(first.text)
        let second = try await chat.chat("What were the last three words in your previous answer?")
        guard second.done, !second.text.isEmpty, chat.messages.count == 4 else { fatalError("Conversation live check failed") }
        print(second.text)
        await ai.close()
        print("LIVE CHECK PASSED")
        app.terminate(nil)
    } catch { print("LIVE CHECK FAILED: \(error)"); exit(1) }
}

app.run()
