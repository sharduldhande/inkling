import SwiftUI

struct AgendaView: View {
    @Bindable var store: AgendaStore
    let openInChat: (AgendaItem) -> Void
    let markDone: (AgendaItem) -> Void

    /// The row whose action dialog is up: tap asks "Mark done" or "Open in chat".
    @State private var selected: AgendaItem?

    // Vertical split: realtime tasks (one-shots + calendar) above, recurring
    // habits below. Server order holds within each filter: live recurring by
    // fire time, done recurring appended newest-completion-first.
    private var tasks: [AgendaItem] { store.items.filter { $0.kind != "recurring" } }
    private var overdue: [AgendaItem] { tasks.filter { $0.isOverdue && !$0.isDone } }
    private var upcoming: [AgendaItem] { tasks.filter { !$0.isOverdue && !$0.isDone } }
    private var doneTasks: [AgendaItem] { tasks.filter(\.isDone) }
    private var recurring: [AgendaItem] { store.items.filter { $0.kind == "recurring" } }

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 22) {
                if store.items.isEmpty {
                    emptyUpcoming
                } else {
                    if !overdue.isEmpty {
                        section("Overdue", items: overdue)
                    }
                    section("Next 48 hours", items: upcoming)
                    if !doneTasks.isEmpty {
                        section("Done", items: doneTasks)
                    }
                    if !recurring.isEmpty {
                        section("Recurring", items: recurring)
                    }
                }
            }
            .padding()
        }
        .refreshable { await store.refresh() }
        .confirmationDialog(
            selected?.title ?? "",
            isPresented: Binding(
                get: { selected != nil },
                set: { if !$0 { selected = nil } }
            ),
            titleVisibility: .visible,
            presenting: selected
        ) { item in
            if !item.isDone {
                Button("Mark done") { markDone(item) }
            }
            Button("Open in chat") { openInChat(item) }
            Button("Cancel", role: .cancel) {}
        }
        .overlay(alignment: .top) {
            if store.showRefreshBanner { refreshBanner }
        }
    }

    @ViewBuilder
    private func section(_ title: String, items: [AgendaItem]) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            Text(title)
                .font(.headline)
            VStack(alignment: .leading, spacing: 0) {
                if items.isEmpty {
                    emptyUpcoming
                } else {
                    ForEach(items) { item in
                        row(for: item)
                        if item.id != items.last?.id { Divider().padding(.leading, 24) }
                    }
                }
            }
            .padding(.horizontal, 13)
            .padding(.vertical, 4)
            .frame(maxWidth: .infinity, alignment: .leading)
            .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        }
    }

    private func row(for item: AgendaItem) -> some View {
        Button { selected = item } label: {
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
            .padding(.vertical, 10)
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
        Text(store.bannerText)
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

        if item.kind == "calendar", item.isAllDay {
            // The server sends local midnight for all-day rows; a clock time
            // would be noise. "All day" today, "Sat, all day" beyond today.
            if Calendar.current.isDateInToday(item.eventDate) { return "All day" }
            if Calendar.current.isDateInTomorrow(item.eventDate) { return "Tomorrow, all day" }
            return item.eventDate.formatted(Date.FormatStyle().weekday(.abbreviated)) + ", all day"
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
