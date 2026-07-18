import SwiftUI

@main
struct InklingApp: App {
    var body: some Scene {
        WindowGroup {
            RootView()
                .onOpenURL { _ in
                    NotificationCenter.default.post(name: .inklingOpenURL, object: nil)
                }
        }
    }
}
