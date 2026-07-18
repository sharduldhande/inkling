import Foundation
import Observation

@MainActor
@Observable
final class LogStore {
    var entries: [LogEntry]
    var days: [DaySummary]
    var targets: Targets
    var weight: LatestWeight?
    var showRefreshBanner = false

    @ObservationIgnored private let settings: AppSettings
    @ObservationIgnored private var isRefreshing = false
    @ObservationIgnored private var bannerTask: Task<Void, Never>?

    init(settings: AppSettings) {
        self.settings = settings
        let cache = CacheFiles.load(LogCache.self, named: CacheFiles.log)
        entries = cache?.measurements.entries ?? []
        days = cache?.days.days ?? []
        targets = cache?.days.targets ?? .empty
        weight = cache?.days.weight
    }

    func refresh() async {
        guard !isRefreshing, settings.hasToken else { return }
        isRefreshing = true
        defer { isRefreshing = false }
        let client = settings.client()
        do {
            async let measurements = client.fetchMeasurements()
            async let summaries = client.fetchDays()
            let (newMeasurements, newDays) = try await (measurements, summaries)
            entries = newMeasurements.entries
            days = newDays.days
            targets = newDays.targets
            weight = newDays.weight
            CacheFiles.save(LogCache(measurements: newMeasurements, days: newDays), named: CacheFiles.log)
        } catch InklingClientError.unauthorized {
            NotificationCenter.default.post(name: .inklingUnauthorized, object: nil)
        } catch {
            showFailureBanner()
        }
    }

    func clearLocalCache() {
        entries = []
        days = []
        targets = .empty
        weight = nil
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
