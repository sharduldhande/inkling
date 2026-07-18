import Foundation
import Observation

@MainActor
@Observable
final class AgendaStore {
    var items: [AgendaItem]
    var showRefreshBanner = false

    @ObservationIgnored private let settings: AppSettings
    @ObservationIgnored private var isRefreshing = false
    @ObservationIgnored private var bannerTask: Task<Void, Never>?

    init(settings: AppSettings) {
        self.settings = settings
        items = CacheFiles.load(AgendaResponse.self, named: CacheFiles.agenda)?.items ?? []
    }

    func refresh() async {
        guard !isRefreshing, settings.hasToken else { return }
        isRefreshing = true
        defer { isRefreshing = false }
        do {
            let response = try await settings.client().fetchAgenda()
            items = response.items
            CacheFiles.save(response, named: CacheFiles.agenda)
        } catch InklingClientError.unauthorized {
            NotificationCenter.default.post(name: .inklingUnauthorized, object: nil)
        } catch {
            showFailureBanner()
        }
    }

    func clearLocalCache() {
        items = []
    }

    private func showFailureBanner() {
        bannerTask?.cancel()
        showRefreshBanner = true
        bannerTask = Task {
            try? await Task.sleep(for: .seconds(3))
            guard !Task.isCancelled else { return }
            showRefreshBanner = false
        }
    }
}
