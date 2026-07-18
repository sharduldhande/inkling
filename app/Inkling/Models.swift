import Foundation

enum Tab: Int, Hashable {
    case today
    case chat
    case log
}

enum ChatMessageState: String, Codable {
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

    var id: UUID { localID }
    var isUser: Bool { role == "user" }
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

    var isOverdue: Bool { overdue == true }
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
    }

    private static func url(for name: String) -> URL {
        let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        return documents.appendingPathComponent(name, isDirectory: false)
    }
}

extension Double {
    var inklingNumber: String {
        formatted(.number.precision(.fractionLength(0...1)))
    }
}

extension Notification.Name {
    static let inklingUnauthorized = Notification.Name("InklingUnauthorized")
    static let inklingDataShouldRefresh = Notification.Name("InklingDataShouldRefresh")
    static let inklingOpenURL = Notification.Name("InklingOpenURL")
    static let inklingCachesCleared = Notification.Name("InklingCachesCleared")
}
