import SwiftUI

struct AgendaView: View {
    @Bindable var store: AgendaStore
    let tapped: (AgendaItem) -> Void

    private var overdue: [AgendaItem] { store.items.filter { $0.isOverdue && !$0.isDone } }
    private var upcoming: [AgendaItem] { store.items.filter { !$0.isOverdue && !$0.isDone } }
    private var done: [AgendaItem] { store.items.filter(\.isDone) }

    var body: some View {
        List {
            if store.items.isEmpty {
                emptyUpcoming
                    .listRowBackground(Color.clear)
                    .listRowSeparator(.hidden)
            } else {
                if !overdue.isEmpty {
                    Section("Overdue") {
                        ForEach(overdue) { row(for: $0) }
                    }
                }
                Section("Next 48 hours") {
                    if upcoming.isEmpty {
                        emptyUpcoming
                    } else {
                        ForEach(upcoming) { row(for: $0) }
                    }
                }
                if !done.isEmpty {
                    Section("Done") {
                        ForEach(done) { row(for: $0) }
                    }
                }
            }
        }
        .listStyle(.insetGrouped)
        .refreshable { await store.refresh() }
        .overlay(alignment: .top) {
            if store.showRefreshBanner { refreshBanner }
        }
    }

    private func row(for item: AgendaItem) -> some View {
        Button { tapped(item) } label: {
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 6) {
                    if item.isDone {
                        Image(systemName: "checkmark.circle.fill")
                            .font(.caption)
                            .foregroundStyle(.green)
                    } else if item.kind == "calendar" {
                        Image(systemName: "calendar")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    } else if item.kind == "recurring" {
                        Image(systemName: "repeat")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    } else {
                        Image(systemName: "circle")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    Text(item.title)
                        .font(.body)
                        .lineLimit(1)
                        .truncationMode(.tail)
                }
                Text(caption(for: item))
                    .font(.caption)
                    .lineLimit(1)
                    .foregroundStyle(item.isOverdue ? Color.red : Color.secondary)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private var emptyUpcoming: some View {
        VStack(spacing: 7) {
            Image(systemName: "calendar")
                .font(.title2)
            Text("Nothing due in the next 48 hours.")
                .font(.caption)
        }
        .foregroundStyle(.secondary)
        .frame(maxWidth: .infinity)
        .padding(.vertical, store.items.isEmpty ? 100 : 22)
    }

    private var refreshBanner: some View {
        Text("Couldn't refresh — showing cached data.")
            .font(.caption)
            .padding(.horizontal, 12)
            .padding(.vertical, 7)
            .glassEffect()
            .padding(.top, 8)
    }

    private func caption(for item: AgendaItem) -> String {
        let relative = RelativeDateTimeFormatter()
        relative.dateTimeStyle = .named
        relative.unitsStyle = .full
        if item.isDone {
            guard let doneDate = item.doneDate else { return "Done" }
            return "Done \(relative.localizedString(for: doneDate, relativeTo: Date()))"
        }
        let relativeText = relative.localizedString(for: item.eventDate, relativeTo: Date())
        if item.isOverdue {
            return "Overdue — was due \(relativeText)"
        }

        let within24Hours = item.eventDate.timeIntervalSinceNow <= 24 * 60 * 60
        var text = within24Hours
            ? relativeText
            : item.eventDate.formatted(Date.FormatStyle().weekday(.abbreviated).hour().minute())

        if item.kind == "calendar", let end = item.endDate,
           Calendar.current.isDate(item.eventDate, inSameDayAs: end) {
            let endText = end.formatted(Date.FormatStyle().hour().minute())
            text += within24Hours ? " until \(endText)" : "–\(endText)"
        }
        return text
    }
}
