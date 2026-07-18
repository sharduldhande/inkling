import SwiftUI

struct SettingsSheet: View {
    @Environment(\.dismiss) private var dismiss
    let settings: AppSettings

    @State private var serverURL: String
    @State private var token = ""
    @State private var validationError: String?
    @State private var testResult: ConnectionTestResult?
    @State private var testing = false
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
        token = ""
        dismiss()
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
        guard let url = URL(string: serverURL), url.scheme?.lowercased() == "https" else { return nil }
        return url
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
