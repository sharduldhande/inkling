import SwiftUI

struct SettingsSheet: View {
    @Environment(\.dismiss) private var dismiss
    let settings: AppSettings

    @State private var serverURL: String
    @State private var token = ""
    @State private var deepgramKey = ""
    @State private var validationError: String?
    @State private var testResult: ConnectionTestResult?
    @State private var testing = false
    @State private var deepgramResult: Bool?
    @State private var testingDeepgram = false
    @State private var confirmingClear = false

    init(settings: AppSettings) {
        self.settings = settings
        _serverURL = State(initialValue: settings.serverURLString)
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Server URL", text: $serverURL)
                        .textInputAutocapitalization(.never)
                        .keyboardType(.URL)
                        .autocorrectionDisabled()
                    if let validationError {
                        Text(validationError)
                            .font(.caption)
                            .foregroundStyle(.red)
                    }
                } header: {
                    Text("Server URL")
                }

                Section {
                    SecureField(settings.hasToken ? "••••••• (saved)" : "Bearer token", text: $token)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                } header: {
                    Text("Bearer token")
                }

                Section {
                    Button {
                        Task { await testConnection() }
                    } label: {
                        HStack {
                            Text("Test connection")
                            Spacer()
                            if testing { ProgressView() }
                        }
                    }
                    .disabled(testing)

                    if let testResult {
                        Text(testResult.text)
                            .foregroundStyle(testResult == .ok ? Color.green : Color.red)
                    }
                }

                Section {
                    SecureField(
                        settings.hasDeepgramKey ? "••••••• (saved)" : "Deepgram API key",
                        text: $deepgramKey
                    )
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()

                    Button {
                        Task { await testDeepgram() }
                    } label: {
                        HStack {
                            Text("Test Deepgram key")
                            Spacer()
                            if testingDeepgram { ProgressView() }
                        }
                    }
                    .disabled(testingDeepgram || (deepgramKey.isEmpty && !settings.hasDeepgramKey))

                    if let deepgramResult {
                        Text(deepgramResult ? "OK" : "Key rejected")
                            .foregroundStyle(deepgramResult ? Color.green : Color.red)
                    }
                } header: {
                    Text("Voice (Deepgram)")
                } footer: {
                    Text("Enables the mic button in chat. The key stays in the Keychain on this device.")
                }

                Section {
                    HStack {
                        Text("Status")
                        Spacer()
                        Text(PushRegistrar.shared.statusText)
                            .foregroundStyle(.secondary)
                            .multilineTextAlignment(.trailing)
                    }
                    Button("Re-register") {
                        Task { await PushRegistrar.shared.ensureRegistered(settings: settings) }
                    }
                    .disabled(!settings.hasToken)
                } header: {
                    Text("Notifications")
                } footer: {
                    Text("Reminders arrive as native push notifications. Re-register if they stop arriving.")
                }

                Section {
                    Button("Clear local caches", role: .destructive) {
                        confirmingClear = true
                    }
                }
            }
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save", action: save)
                }
            }
            .confirmationDialog(
                "Clear local caches?",
                isPresented: $confirmingClear,
                titleVisibility: .visible
            ) {
                Button("Clear local caches", role: .destructive) {
                    CacheFiles.removeAll()
                    NotificationCenter.default.post(name: .inklingCachesCleared, object: nil)
                }
                Button("Cancel", role: .cancel) { }
            } message: {
                Text("This deletes only the chat, agenda, and log files on this device.")
            }
        }
    }

    private func save() {
        guard validatedURL() != nil else {
            validationError = "Must be an https:// URL"
            return
        }
        validationError = nil
        settings.save(serverURL: serverURL, newToken: token.isEmpty ? nil : token)
        if !deepgramKey.isEmpty { settings.saveDeepgramKey(deepgramKey) }
        token = ""
        deepgramKey = ""
        dismiss()
    }

    private func testDeepgram() async {
        testingDeepgram = true
        defer { testingDeepgram = false }
        let key = deepgramKey.isEmpty ? settings.currentDeepgramKey() : deepgramKey
        deepgramResult = await DeepgramTranscriptionService.validateKey(key)
    }

    private func testConnection() async {
        guard let url = validatedURL() else {
            validationError = "Must be an https:// URL"
            return
        }
        validationError = nil
        testing = true
        defer { testing = false }
        let currentToken = token.isEmpty ? settings.currentToken() : token
        let client = InklingClient(baseURL: url, token: currentToken)
        let now = Int(Date().timeIntervalSince1970 * 1_000)
        do {
            _ = try await client.fetchHistory(since: now)
            testResult = .ok
        } catch InklingClientError.unauthorized {
            testResult = .rejected
        } catch {
            testResult = .unreachable
        }
    }

    private func validatedURL() -> URL? {
        guard let url = URL(string: serverURL) else { return nil }
        let scheme = url.scheme?.lowercased()
        if scheme == "https" { return url }
        // Dev affordance: plain http is allowed for loopback only.
        if scheme == "http", let host = url.host(), ["localhost", "127.0.0.1"].contains(host) {
            return url
        }
        return nil
    }
}

private enum ConnectionTestResult: Equatable {
    case ok
    case rejected
    case unreachable

    var text: String {
        switch self {
        case .ok: "OK"
        case .rejected: "Token rejected"
        case .unreachable: "Unreachable"
        }
    }
}
