import ActivityKit
import Foundation

/// The "next up" Live Activity. The server starts and updates it over APNs
/// (`inkling/delivery/live_activity.py`); the JSON `content-state` it sends
/// decodes straight into `ContentState`, so the property names and types here
/// ARE the wire contract. `due` is unix seconds — a `Date` would decode with
/// Foundation's 2001 reference epoch and land 31 years off.
struct NextUpAttributes: ActivityAttributes {
    struct ContentState: Codable, Hashable {
        /// Empty = nothing due in the next 14 days.
        var title: String
        var due: Double?
        var overdue: Bool
        /// "commitment" (Inkling's own) or "deadline" (a Canvas item).
        var kind: String?
        /// Other items due within 7 days, overdue included.
        var more: Int

        var dueDate: Date? { due.map { Date(timeIntervalSince1970: $0) } }
    }
}
