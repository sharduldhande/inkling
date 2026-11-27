import Foundation
import Observation

@MainActor
@Observable
final class AgendaStore {
    var items: [AgendaItem]
    var showRefreshBanner = false
    var bannerText = "Couldn't refresh — showing cached data."

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
            showFailureBanner("Couldn't refresh — showing cached data.")
        }
    }

    /// The Today tab's one write (POST /agenda/done). The server logs a
    /// "✓ Marked done" line into chat; returns whether it landed so the
    /// caller can sync the chat to show it.
    func markDone(_ item: AgendaItem) async -> Bool {
        do {
            try await settings.client().markDone(id: item.id)
        } catch InklingClientError.unauthorized {
            NotificationCenter.default.post(name: .inklingUnauthorized, object: nil)
            return false
        } catch InklingClientError.rejected(let reason) {
            showFailureBanner("Couldn't mark done: \(reason)")
            await refresh()
            return false
        } catch {
            showFailureBanner("Couldn't mark done — check your connection.")
            return false
        }
        await refresh()
        return true
    }

    func clearLocalCache() {
        items = []
    }

    private func showFailureBanner(_ text: String) {
        bannerTask?.cancel()
        bannerText = text
        showRefreshBanner = true
        bannerTask = Task {
            try? await Task.sleep(for: .seconds(3))
            guard !Task.isCancelled else { return }
            showRefreshBanner = false
        }
    }
}
