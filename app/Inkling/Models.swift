import Foundation

enum Tab: Int, Hashable {
    case today
    case chat
    case log
}

enum ChatMessageState: String, Codable {
    case sending
    case sent
    case queued
    case failed
    case streaming
    case done
    case incomplete
    case errored
}

struct ChatMessage: Identifiable, Codable, Equatable {
    var localID: UUID
    var serverID: String?
    var role: String
    var text: String
    var date: Date
    var serverTS: Int?
    var state: ChatMessageState
    var voiceOriginated: Bool
    /// Server-provided rejection reason shown under a failed upload (§B8.7).
    var failureReason: String? = nil
    /// Filename under Documents/photos/ for the thumbnail; text is the
    /// caption (may be empty). Optional so pre-thumbnail caches decode.
    var photoFile: String? = nil

    var id: UUID { localID }
    var isUser: Bool { role == "user" }
    /// Photo bubbles live outside history-sync healing: their synthetic
    /// serverID ("doc-<id>") never matches a transcript row.
    var isPhoto: Bool { photoFile != nil || text.hasPrefix("[photo") }
    var documentID: Int? {
        guard let serverID, serverID.hasPrefix("doc-") else { return nil }
        return Int(serverID.dropFirst(4))
    }
}

struct HistoryMessage: Decodable {
    let id: String
    let role: String
    let text: String
    let ts: Int
}

struct HistoryResponse: Decodable {
    let messages: [HistoryMessage]
}

struct HealthResponse: Decodable {
    let status: String
    let busy: Bool
}

struct AgendaItem: Identifiable, Codable, Equatable {
    let id: String
    let kind: String
    let title: String
    let body: String?
    let due: Int?
    let overdue: Bool?
    let starts: Int?
    let ends: Int?
    let done: Bool?
    let doneTs: Int?
    let allDay: Bool?

    enum CodingKeys: String, CodingKey {
        case id, kind, title, body, due, overdue, starts, ends, done
        case doneTs = "done_ts"
        case allDay = "all_day"
    }

    var isDone: Bool { done == true }
    var doneDate: Date? {
        doneTs.map { Date(timeIntervalSince1970: Double($0) / 1_000) }
    }
    var isOverdue: Bool { overdue == true }
    var isAllDay: Bool { allDay == true }
    var eventDate: Date {
        Date(timeIntervalSince1970: Double(starts ?? due ?? 0) / 1_000)
    }
    var endDate: Date? {
        ends.map { Date(timeIntervalSince1970: Double($0) / 1_000) }
    }

    var chatPrefill: String {
        if kind == "calendar" {
            return "About the \"\(title)\" calendar event: "
        }
        if let body {
            return "About \"\(title)\" (\(body)): "
        }
        return "About \"\(title)\": "
    }
}

struct AgendaResponse: Codable {
    let items: [AgendaItem]
}

struct LogEntry: Identifiable, Codable, Equatable {
    let id: String
    let type: String
    let note: String?
    let kcal: Double?
    let proteinG: Double?
    let carbG: Double?
    let fatG: Double?
    let ts: Int
    let activity: String?
    let durationMin: Double?
    let kcalBurned: Double?
    let distanceKm: Double?
    let kind: String?
    let value: Double?
    let unit: String?

    enum CodingKeys: String, CodingKey {
        case id, type, note, kcal, ts, activity, kind, value, unit
        case proteinG = "protein_g"
        case carbG = "carb_g"
        case fatG = "fat_g"
        case durationMin = "duration_min"
        case kcalBurned = "kcal_burned"
        case distanceKm = "distance_km"
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        id = try values.decode(String.self, forKey: .id)
        type = try values.decode(String.self, forKey: .type)
        guard ["meal", "workout", "metric"].contains(type) else {
            throw DecodingError.dataCorruptedError(forKey: .type, in: values, debugDescription: "Unknown log entry type")
        }
        ts = try values.decode(Int.self, forKey: .ts)
        note = try values.decodeIfPresent(String.self, forKey: .note)
        kcal = try values.decodeIfPresent(Double.self, forKey: .kcal)
        proteinG = try values.decodeIfPresent(Double.self, forKey: .proteinG)
        carbG = try values.decodeIfPresent(Double.self, forKey: .carbG)
        fatG = try values.decodeIfPresent(Double.self, forKey: .fatG)
        activity = try values.decodeIfPresent(String.self, forKey: .activity)
        durationMin = try values.decodeIfPresent(Double.self, forKey: .durationMin)
        kcalBurned = try values.decodeIfPresent(Double.self, forKey: .kcalBurned)
        distanceKm = try values.decodeIfPresent(Double.self, forKey: .distanceKm)
        kind = try values.decodeIfPresent(String.self, forKey: .kind)
        value = try values.decodeIfPresent(Double.self, forKey: .value)
        unit = try values.decodeIfPresent(String.self, forKey: .unit)

        switch type {
        case "meal":
            guard note != nil else { throw Self.missing("note", decoder: decoder) }
        case "workout":
            guard activity != nil, durationMin != nil else { throw Self.missing("workout fields", decoder: decoder) }
        case "metric":
            guard kind != nil, value != nil, unit != nil else { throw Self.missing("metric fields", decoder: decoder) }
        default:
            break
        }
    }

    private static func missing(_ field: String, decoder: Decoder) -> DecodingError {
        .dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "Missing \(field)"))
    }

    var date: Date { Date(timeIntervalSince1970: Double(ts) / 1_000) }

    var chatPrefill: String {
        let relative = RelativeDateTimeFormatter()
        relative.dateTimeStyle = .named
        relative.unitsStyle = .full
        let age = relative.localizedString(for: date, relativeTo: Date())
        switch type {
        case "meal":
            let amount = kcal.map { " (\($0.inklingNumber) kcal)" } ?? ""
            return "About the \"\(note ?? "")\" meal\(amount) from \(age): "
        case "workout":
            return "About the \(activity ?? "") workout (\((durationMin ?? 0).inklingNumber) min) from \(age): "
        case "metric":
            return "About the \((value ?? 0).inklingNumber) \(unit ?? "") \(kind ?? "") entry from \(age): "
        default:
            return ""
        }
    }
}

private struct Lossy<Value: Decodable>: Decodable {
    let value: Value?

    init(from decoder: Decoder) throws {
        value = try? Value(from: decoder)
    }
}

struct MeasurementsResponse: Codable {
    let entries: [LogEntry]

    enum CodingKeys: String, CodingKey { case entries }

    init(entries: [LogEntry]) {
        self.entries = entries
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        entries = try values.decode([Lossy<LogEntry>].self, forKey: .entries).compactMap(\.value)
    }
}

struct DaySummary: Codable, Equatable, Identifiable {
    let date: String
    let kcal: Double
    let proteinG: Double
    let exercised: Bool
    let logged: Bool

    var id: String { date }

    enum CodingKeys: String, CodingKey {
        case date, kcal, exercised, logged
        case proteinG = "protein_g"
    }
}

struct Targets: Codable, Equatable {
    let kcalQuota: Double?
    let proteinQuotaG: Double?

    enum CodingKeys: String, CodingKey {
        case kcalQuota = "kcal_quota"
        case proteinQuotaG = "protein_quota_g"
    }

    static let empty = Targets(kcalQuota: nil, proteinQuotaG: nil)
}

struct LatestWeight: Codable, Equatable {
    let value: Double
    let unit: String
    let ts: Int

    var date: Date { Date(timeIntervalSince1970: Double(ts) / 1_000) }
}

struct DaysResponse: Codable, Equatable {
    let days: [DaySummary]
    let targets: Targets
    let weight: LatestWeight?
}

struct LogCache: Codable {
    let measurements: MeasurementsResponse
    let days: DaysResponse
}

enum CacheFiles {
    static let chat = "chat-history.json"
    static let agenda = "agenda-cache.json"
    static let log = "log-cache.json"

    static func load<T: Decodable>(_ type: T.Type, named name: String) -> T? {
        guard let data = try? Data(contentsOf: url(for: name)) else { return nil }
        return try? JSONDecoder().decode(type, from: data)
    }

    static func save<T: Encodable>(_ value: T, named name: String) {
        guard let data = try? JSONEncoder().encode(value) else { return }
        try? data.write(to: url(for: name), options: .atomic)
    }

    static func removeAll() {
        for name in [chat, agenda, log] {
            try? FileManager.default.removeItem(at: url(for: name))
        }
        try? FileManager.default.removeItem(at: photosDirectory)
    }

    static var photosDirectory: URL {
        documentsDirectory.appendingPathComponent("photos", isDirectory: true)
    }

    static func photoURL(_ name: String) -> URL {
        photosDirectory.appendingPathComponent(name, isDirectory: false)
    }

    /// Write photo bytes under Documents/photos/; returns the filename.
    static func savePhoto(_ data: Data) -> String? {
        let name = "\(UUID().uuidString).jpg"
        do {
            try FileManager.default.createDirectory(
                at: photosDirectory, withIntermediateDirectories: true
            )
            try data.write(to: photoURL(name), options: .atomic)
            return name
        } catch {
            return nil
        }
    }

    private static var documentsDirectory: URL {
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
    }

    private static func url(for name: String) -> URL {
        documentsDirectory.appendingPathComponent(name, isDirectory: false)
    }
}

extension Double {
    var inklingNumber: String {
        formatted(.number.precision(.fractionLength(0...1)))
    }
}

/// Body of `POST /devices` (§11): this install's APNs token and which host
/// Apple expects it on.
struct DeviceRegistration: Encodable {
    let token: String
    let environment: String
    let platform = "ios"
    let appVersion: String

    enum CodingKeys: String, CodingKey {
        case token, environment, platform
        case appVersion = "app_version"
    }
}

struct LiveActivityRegistration: Encodable {
    struct Current: Encodable {
        let id: String
        let token: String
    }

    let environment: String
    let enabled: Bool
    let startToken: String?
    let activity: Current?

    enum CodingKeys: String, CodingKey {
        case environment, enabled, activity
        case startToken = "start_token"
    }

    // Explicit so a nil activity goes over the wire as null ("nothing on
    // screen"), not as a missing key.
    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(environment, forKey: .environment)
        try container.encode(enabled, forKey: .enabled)
        try container.encodeIfPresent(startToken, forKey: .startToken)
        try container.encode(activity, forKey: .activity)
    }
}

extension Notification.Name {
    static let inklingUnauthorized = Notification.Name("InklingUnauthorized")
    static let inklingDataShouldRefresh = Notification.Name("InklingDataShouldRefresh")
    static let inklingOpenURL = Notification.Name("InklingOpenURL")
    static let inklingCachesCleared = Notification.Name("InklingCachesCleared")
}
