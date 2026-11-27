import SwiftUI

@main
struct InklingApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        WindowGroup {
            RootView()
                .onOpenURL { _ in
                    NotificationCenter.default.post(name: .inklingOpenURL, object: nil)
                }
        }
    }
}
