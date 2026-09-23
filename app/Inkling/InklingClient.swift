import Foundation
import Observation

@MainActor
@Observable
final class AppSettings {
    static let defaultServerURL = "https://inkling.example.com"
    static let serverURLKey = "serverURL"

    var serverURLString: String
    @ObservationIgnored private var bearerToken: String?
    private var deepgramKey: String?

    init() {
        serverURLString = UserDefaults.standard.string(forKey: Self.serverURLKey) ?? Self.defaultServerURL
        bearerToken = KeychainHelper.getToken()
        deepgramKey = KeychainHelper.get(account: KeychainHelper.deepgramAccount)
    }

    var hasToken: Bool { !(bearerToken ?? "").isEmpty }
    var hasDeepgramKey: Bool { !(deepgramKey ?? "").isEmpty }

    func client() -> InklingClient {
        InklingClient(baseURL: URL(string: serverURLString)!, token: bearerToken ?? "")
    }

    func save(serverURL: String, newToken: String?) {
        serverURLString = serverURL
        UserDefaults.standard.set(serverURL, forKey: Self.serverURLKey)
        if let newToken, !newToken.isEmpty, KeychainHelper.setToken(newToken) {
            bearerToken = newToken
        }
    }

    func currentToken() -> String { bearerToken ?? "" }

    func currentDeepgramKey() -> String { deepgramKey ?? "" }

    func saveDeepgramKey(_ key: String) {
        guard !key.isEmpty, KeychainHelper.set(key, account: KeychainHelper.deepgramAccount) else { return }
        deepgramKey = key
    }
}

enum ChatEvent: Equatable {
    case text(String)
    case done
    case exit(Int)
}

enum InklingClientError: Error, Equatable {
    case unauthorized
    case busy429
    case transport
    case status(Int)
    /// 4xx/422 from /documents with a server-provided reason to show inline.
    case rejected(String)
}

struct DocumentUploadResponse: Decodable {
    let documentId: Int
    let title: String

    enum CodingKeys: String, CodingKey {
        case documentId = "document_id"
        case title
    }
}

struct InklingClient: @unchecked Sendable {
    private let baseURL: URL
    private let token: String
    private let session: URLSession

    init(baseURL: URL, token: String, session: URLSession = .shared) {
        self.baseURL = baseURL
        self.token = token
        self.session = session
    }

    func health() async throws -> HealthResponse {
        try await get(path: "health", queryItems: [], authenticated: false)
    }

    func fetchHistory(since: Int) async throws -> HistoryResponse {
        try await get(path: "history", queryItems: [URLQueryItem(name: "since", value: String(since))])
    }

    func fetchAgenda() async throws -> AgendaResponse {
        try await get(path: "agenda")
    }

    func fetchMeasurements() async throws -> MeasurementsResponse {
        try await get(path: "measurements")
    }

    func fetchDays() async throws -> DaysResponse {
        try await get(path: "days")
    }

    func streamChat(
        message: String,
        responseOpened: @escaping () async -> Void
    ) -> AsyncThrowingStream<ChatEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    var request = try request(path: "chat", authenticated: true)
                    request.httpMethod = "POST"
                    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                    request.httpBody = try JSONEncoder().encode(["message": message])
                    let (bytes, response): (URLSession.AsyncBytes, URLResponse)
                    do {
                        (bytes, response) = try await session.bytes(for: request)
                    } catch {
                        throw InklingClientError.transport
                    }
                    guard let http = response as? HTTPURLResponse else {
                        throw InklingClientError.transport
                    }
                    guard http.statusCode == 200 else {
                        let body = try await collect(bytes)
                        if http.statusCode == 401 { throw InklingClientError.unauthorized }
                        if http.statusCode == 429,
                           body == Data("brain is busy, try again in a minute".utf8) {
                            throw InklingClientError.busy429
                        }
                        throw InklingClientError.status(http.statusCode)
                    }

                    await responseOpened()
                    try await Self.relaySSE(bytes, into: continuation)
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    /// Attach to a photo's ingestion-turn stream (GET /documents/{id}/stream)
    /// — the same SSE frames as /chat, so photo proposals render live.
    func streamDocument(id: Int) -> AsyncThrowingStream<ChatEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    let request = try request(path: "documents/\(id)/stream", authenticated: true)
                    let (bytes, response): (URLSession.AsyncBytes, URLResponse)
                    do {
                        (bytes, response) = try await session.bytes(for: request)
                    } catch {
                        throw InklingClientError.transport
                    }
                    guard let http = response as? HTTPURLResponse else {
                        throw InklingClientError.transport
                    }
                    if http.statusCode == 401 { throw InklingClientError.unauthorized }
                    guard http.statusCode == 200 else {
                        throw InklingClientError.status(http.statusCode)
                    }
                    try await Self.relaySSE(bytes, into: continuation)
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    /// Fetch a document's stored original (the photo bytes) for re-caching.
    func fetchDocumentOriginal(id: Int) async throws -> Data {
        let request = try request(path: "documents/\(id)/original", authenticated: true)
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw InklingClientError.transport
        }
        guard let http = response as? HTTPURLResponse else { throw InklingClientError.transport }
        if http.statusCode == 401 { throw InklingClientError.unauthorized }
        guard http.statusCode == 200 else { throw InklingClientError.status(http.statusCode) }
        return data
    }

    private static func relaySSE(
        _ bytes: URLSession.AsyncBytes,
        into continuation: AsyncThrowingStream<ChatEvent, Error>.Continuation
    ) async throws {
        do {
            for try await line in bytes.lines {
                guard !line.isEmpty, line.hasPrefix("data: ") else { continue }
                let payload = String(line.dropFirst(6))
                guard let data = payload.data(using: .utf8),
                      let decoded = try? JSONSerialization.jsonObject(with: data),
                      let object = decoded as? [String: Any] else {
                    continue
                }
                if let text = object["text"] as? String {
                    continuation.yield(.text(text))
                } else if object["done"] as? Bool == true {
                    continuation.yield(.done)
                } else if let exit = object["exit"] as? Int {
                    continuation.yield(.exit(exit))
                }
            }
        } catch {
            throw InklingClientError.transport
        }
    }

    /// POST /devices: register this install's APNs token (§11). 200 → done;
    /// 409/422 carry an `error` string → `.rejected(reason)`.
    func registerDevice(token: String, environment: String, appVersion: String) async throws {
        var request = try request(path: "devices", authenticated: true)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(
            DeviceRegistration(token: token, environment: environment, appVersion: appVersion)
        )
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw InklingClientError.transport
        }
        guard let http = response as? HTTPURLResponse else { throw InklingClientError.transport }
        if http.statusCode == 401 { throw InklingClientError.unauthorized }
        guard http.statusCode == 200 else {
            if (400..<500).contains(http.statusCode),
               let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let reason = object["error"] as? String {
                throw InklingClientError.rejected(reason)
            }
            throw InklingClientError.status(http.statusCode)
        }
    }

    /// POST /live-activity: the "next up" card's tokens (see LiveActivityRegistrar).
    func registerLiveActivity(_ registration: LiveActivityRegistration) async throws {
        var request = try request(path: "live-activity", authenticated: true)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(registration)
        let response: URLResponse
        do {
            (_, response) = try await session.data(for: request)
        } catch {
            throw InklingClientError.transport
        }
        guard let http = response as? HTTPURLResponse else { throw InklingClientError.transport }
        if http.statusCode == 401 { throw InklingClientError.unauthorized }
        guard http.statusCode == 200 else { throw InklingClientError.status(http.statusCode) }
    }

    /// POST /documents with the photo as multipart/form-data (§B8.7 shape).
    /// 202 → parsed body; 4xx with an `error` string → `.rejected(reason)`.
    func uploadPhoto(
        jpeg: Data, filename: String, caption: String = ""
    ) async throws -> DocumentUploadResponse {
        var request = try request(path: "documents", authenticated: true)
        request.httpMethod = "POST"
        let boundary = "inkling-\(UUID().uuidString)"
        request.setValue(
            "multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type"
        )
        var body = Data()
        if !caption.isEmpty {
            body.append(Data("--\(boundary)\r\n".utf8))
            body.append(Data("Content-Disposition: form-data; name=\"caption\"\r\n\r\n".utf8))
            body.append(Data(caption.utf8))
            body.append(Data("\r\n".utf8))
        }
        body.append(Data("--\(boundary)\r\n".utf8))
        body.append(Data(
            "Content-Disposition: form-data; name=\"file\"; filename=\"\(filename)\"\r\n".utf8
        ))
        body.append(Data("Content-Type: image/jpeg\r\n\r\n".utf8))
        body.append(jpeg)
        body.append(Data("\r\n--\(boundary)--\r\n".utf8))
        request.httpBody = body

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw InklingClientError.transport
        }
        guard let http = response as? HTTPURLResponse else { throw InklingClientError.transport }
        if http.statusCode == 401 { throw InklingClientError.unauthorized }
        guard http.statusCode == 202 else {
            if (400..<500).contains(http.statusCode),
               let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let reason = object["error"] as? String {
                throw InklingClientError.rejected(reason)
            }
            throw InklingClientError.status(http.statusCode)
        }
        do {
            return try JSONDecoder().decode(DocumentUploadResponse.self, from: data)
        } catch {
            throw InklingClientError.transport
        }
    }

    private func get<T: Decodable>(
        path: String,
        queryItems: [URLQueryItem] = [],
        authenticated: Bool = true
    ) async throws -> T {
        let request = try request(path: path, queryItems: queryItems, authenticated: authenticated)
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw InklingClientError.transport
        }
        guard let http = response as? HTTPURLResponse else { throw InklingClientError.transport }
        if http.statusCode == 401 { throw InklingClientError.unauthorized }
        guard http.statusCode == 200 else { throw InklingClientError.status(http.statusCode) }
        do {
            return try JSONDecoder().decode(T.self, from: data)
        } catch {
            throw InklingClientError.transport
        }
    }

    private func request(
        path: String,
        queryItems: [URLQueryItem] = [],
        authenticated: Bool
    ) throws -> URLRequest {
        var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)
        let basePath = components?.path.trimmingCharacters(in: CharacterSet(charactersIn: "/")) ?? ""
        components?.path = "/" + [basePath, path].filter { !$0.isEmpty }.joined(separator: "/")
        components?.queryItems = queryItems.isEmpty ? nil : queryItems
        guard let url = components?.url else { throw InklingClientError.transport }
        var request = URLRequest(url: url)
        request.timeoutInterval = 60
        if authenticated || !token.isEmpty {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        return request
    }

    private func collect(_ bytes: URLSession.AsyncBytes) async throws -> Data {
        var data = Data()
        for try await byte in bytes { data.append(byte) }
        return data
    }
}
