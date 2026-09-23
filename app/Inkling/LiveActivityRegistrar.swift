import ActivityKit
import Foundation

/// The app's half of the "next up" Live Activity: the server starts and
/// updates the card over APNs, and this reports the tokens it needs
/// (`POST /live-activity`):
///
/// - the push-to-start token, so the server can put a card up while the app
///   isn't running;
/// - the on-screen activity's own update token. After a server start iOS
///   wakes the app in the background just long enough to report it, which is
///   why this starts from `didFinishLaunching` rather than a view.
///
/// It also ends duplicates: if a report was lost, the server starts a second
/// card, and whichever arrived last is the one it is now pushing to.
@MainActor
final class LiveActivityRegistrar {
    static let shared = LiveActivityRegistrar()

    private var started = false
    private var startToken: String?
    private var observed: Set<String> = []
    /// Activity ids in the order this process saw them; the last live one wins.
    private var arrival: [String] = []

    private init() {}

    func start() {
        guard !started else { return }
        started = true
        for activity in Activity<NextUpAttributes>.activities {
            observe(activity)
        }
        Task {
            for await data in Activity<NextUpAttributes>.pushToStartTokenUpdates {
                startToken = data.hexString
                await report()
            }
        }
        Task {
            for await activity in Activity<NextUpAttributes>.activityUpdates {
                observe(activity)
                await endDuplicates()
                await report()
            }
        }
        Task { await report() }
    }

    private func observe(_ activity: Activity<NextUpAttributes>) {
        guard observed.insert(activity.id).inserted else { return }
        arrival.append(activity.id)
        Task {
            for await _ in activity.pushTokenUpdates {
                await report()
            }
        }
        Task {
            for await state in activity.activityStateUpdates
            where state == .ended || state == .dismissed {
                await report()
            }
        }
    }

    private var liveActivities: [Activity<NextUpAttributes>] {
        Activity<NextUpAttributes>.activities.filter {
            $0.activityState == .active || $0.activityState == .stale
        }
    }

    /// The live activity seen most recently (unseen ones rank first-seen).
    private var current: Activity<NextUpAttributes>? {
        liveActivities.max { a, b in
            (arrival.firstIndex(of: a.id) ?? -1) < (arrival.firstIndex(of: b.id) ?? -1)
        }
    }

    private func endDuplicates() async {
        guard let keep = current else { return }
        for activity in liveActivities where activity.id != keep.id {
            await activity.end(nil, dismissalPolicy: .immediate)
        }
    }

    /// Tell the server what is on screen. Failures are fine to drop: the next
    /// launch or token change reports again, and the server's own restart
    /// logic covers a card it can't reach.
    func report() async {
        let settings = AppSettings()
        guard settings.hasToken else { return }
        let activity = current
        let registration = LiveActivityRegistration(
            environment: PushRegistrar.apsEnvironment(),
            enabled: ActivityAuthorizationInfo().areActivitiesEnabled,
            startToken: startToken,
            activity: activity.flatMap { activity in
                activity.pushToken.map {
                    LiveActivityRegistration.Current(id: activity.id, token: $0.hexString)
                }
            }
        )
        // An activity whose token hasn't arrived yet isn't "nothing on
        // screen": skip, pushTokenUpdates will report again in a moment.
        if activity != nil, registration.activity == nil { return }
        try? await settings.client().registerLiveActivity(registration)
    }
}

extension Data {
    var hexString: String { map { String(format: "%02x", $0) }.joined() }
}
