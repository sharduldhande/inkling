import SwiftUI
import UIKit
import UserNotifications

/// Native push (APNs). The server addresses pushes to the user; this file's
/// only job is to get this device's token onto the server (`POST /devices`)
/// and to route a notification tap into the existing `inklingOpenURL` path.
///
/// Registration runs on every launch: tokens can rotate, and a token the
/// server revoked (APNs said Unregistered) comes back to life on re-POST.
final class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        // Here, not in a view: a server push-to-start wakes the app in the
        // background with no UI, and the new card's token must still reach
        // the server.
        Task { @MainActor in LiveActivityRegistrar.shared.start() }
        return true
    }

    func application(
        _ application: UIApplication,
        didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data
    ) {
        let hex = deviceToken.map { String(format: "%02x", $0) }.joined()
        Task { @MainActor in PushRegistrar.shared.tokenReceived(hex) }
    }

    func application(
        _ application: UIApplication,
        didFailToRegisterForRemoteNotificationsWithError error: Error
    ) {
        let message = error.localizedDescription
        Task { @MainActor in PushRegistrar.shared.registrationFailed(message) }
    }

    /// Foreground: still show the banner — a reminder is the point.
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification
    ) async -> UNNotificationPresentationOptions {
        [.banner, .sound]
    }

    /// Tap: same behaviour as the `inkling://` deep link (Chat tab + sync).
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse
    ) async {
        await MainActor.run {
            NotificationCenter.default.post(name: .inklingOpenURL, object: nil)
        }
    }
}

@MainActor
@Observable
final class PushRegistrar {
    static let shared = PushRegistrar()
    private static let lastRegisteredKey = "pushLastRegisteredAt"

    enum Status: Equatable {
        case idle
        case denied
        case awaitingToken
        case registering
        case registered(Date)
        case failed(String)
    }

    var status: Status = .idle
    @ObservationIgnored private var deviceToken: String?
    @ObservationIgnored private var settings: AppSettings?

    private init() {
        if let date = UserDefaults.standard.object(forKey: Self.lastRegisteredKey) as? Date {
            status = .registered(date)
        }
    }

    var statusText: String {
        switch status {
        case .idle: "Not registered"
        case .denied: "Denied — enable in iOS Settings"
        case .awaitingToken: "Waiting for Apple…"
        case .registering: "Registering…"
        case .registered(let date):
            "Registered \(date.formatted(date: .abbreviated, time: .shortened))"
        case .failed(let message): "Failed: \(message)"
        }
    }

    /// Ask for permission (a no-op after the first answer) and request a
    /// token. iOS calls the delegate with the token every time, even when it
    /// has not changed, so the upload always happens from `tokenReceived`.
    func ensureRegistered(settings: AppSettings) async {
        self.settings = settings
        guard settings.hasToken else { return }
        let granted: Bool
        do {
            granted = try await UNUserNotificationCenter.current()
                .requestAuthorization(options: [.alert, .sound, .badge])
        } catch {
            status = .failed(error.localizedDescription)
            return
        }
        guard granted else {
            status = .denied
            return
        }
        if case .registered = status {} else { status = .awaitingToken }
        UIApplication.shared.registerForRemoteNotifications()
    }

    func tokenReceived(_ hex: String) {
        deviceToken = hex
        Task { await upload() }
    }

    func registrationFailed(_ message: String) {
        status = .failed(message)
    }

    private func upload() async {
        guard let settings, let deviceToken else { return }
        status = .registering
        let version = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "?"
        do {
            try await settings.client().registerDevice(
                token: deviceToken,
                environment: Self.apsEnvironment(),
                appVersion: version
            )
            let now = Date()
            UserDefaults.standard.set(now, forKey: Self.lastRegisteredKey)
            status = .registered(now)
        } catch InklingClientError.unauthorized {
            status = .failed("token rejected by server")
        } catch InklingClientError.rejected(let reason) {
            status = .failed(reason)
        } catch {
            status = .failed("server unreachable")
        }
    }

    /// Which APNs host the server must use for this install. It is a
    /// property of the signing profile, not the build configuration: a
    /// Release build signed with a development profile is still sandbox.
    /// The embedded profile is a CMS blob wrapping a plaintext plist, so a
    /// Latin-1 decode and a substring search are enough.
    static func apsEnvironment() -> String {
        guard let path = Bundle.main.path(forResource: "embedded", ofType: "mobileprovision"),
              let data = FileManager.default.contents(atPath: path),
              let text = String(data: data, encoding: .isoLatin1),
              let key = text.range(of: "<key>aps-environment</key>") else {
            return "development"  // simulator / no profile
        }
        let rest = text[key.upperBound...]
        guard let open = rest.range(of: "<string>"),
              let close = rest.range(of: "</string>", range: open.upperBound..<rest.endIndex) else {
            return "development"
        }
        let value = rest[open.upperBound..<close.lowerBound]
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return value == "production" ? "production" : "development"
    }
}
