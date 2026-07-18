import SwiftUI

struct LogView: View {
    @Bindable var store: LogStore
    let tapped: (LogEntry) -> Void

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 22) {
                quotaSection
                HeatmapsView(days: store.days, kcalQuota: store.targets.kcalQuota)
                ledgerSection
            }
            .padding()
        }
        .refreshable { await store.refresh() }
        .overlay(alignment: .top) {
            if store.showRefreshBanner {
                Text("Couldn't refresh — showing cached data.")
                    .font(.caption)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 7)
                    .glassEffect()
                    .padding(.top, 8)
            }
        }
    }

    private var today: DaySummary? { store.days.last }

    @ViewBuilder
    private var quotaSection: some View {
        VStack(spacing: 12) {
            HStack(spacing: 12) {
                QuotaTile(
                    title: "Calories",
                    total: today?.kcal ?? 0,
                    quota: store.targets.kcalQuota,
                    unit: "kcal",
                    leftSuffix: "left",
                    todaySuffix: "kcal today"
                )
                QuotaTile(
                    title: "Protein",
                    total: today?.proteinG ?? 0,
                    quota: store.targets.proteinQuotaG,
                    unit: "g",
                    leftSuffix: "g left",
                    todaySuffix: "g today"
                )
            }
            if let weight = store.weight {
                HStack(spacing: 10) {
                    Image(systemName: "scalemass")
                        .foregroundStyle(.secondary)
                    Text("\(weight.value.inklingNumber) \(weight.unit)")
                        .font(.headline)
                    Spacer()
                    Text("weighed \(relativeText(for: weight.date))")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                .padding(12)
                .background(Color(uiColor: .secondarySystemBackground), in: RoundedRectangle(cornerRadius: 14))
            }
        }
    }

    @ViewBuilder
    private var ledgerSection: some View {
        VStack(alignment: .leading, spacing: 14) {
            if store.entries.isEmpty {
                VStack(spacing: 9) {
                    Image(systemName: "fork.knife")
                        .font(.title2)
                    Text("Nothing logged in the last 7 days. Tell Inkling what you ate or did.")
                        .font(.caption)
                        .multilineTextAlignment(.center)
                }
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 28)
            } else {
                ForEach(ledgerGroups) { group in
                    VStack(alignment: .leading, spacing: 0) {
                        Text(header(for: group.date))
                            .font(.headline)
                            .padding(.bottom, 7)
                        ForEach(group.entries) { entry in
                            Button { tapped(entry) } label: {
                                LedgerRow(entry: entry)
                                    .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                            if entry.id != group.entries.last?.id { Divider().padding(.leading, 32) }
                        }
                    }
                }
            }
        }
    }

    private var ledgerGroups: [LedgerGroup] {
        let calendar = Calendar.current
        let grouped = Dictionary(grouping: store.entries) { calendar.startOfDay(for: $0.date) }
        return grouped.keys.sorted(by: >).map { date in
            LedgerGroup(date: date, entries: (grouped[date] ?? []).sorted { $0.ts > $1.ts })
        }
    }

    private func header(for date: Date) -> String {
        let calendar = Calendar.current
        if calendar.isDateInToday(date) { return "Today" }
        if calendar.isDateInYesterday(date) { return "Yesterday" }
        return date.formatted(Date.FormatStyle().weekday(.wide).day().month())
    }

    private func relativeText(for date: Date) -> String {
        let formatter = RelativeDateTimeFormatter()
        formatter.dateTimeStyle = .named
        formatter.unitsStyle = .full
        return formatter.localizedString(for: date, relativeTo: Date())
    }
}

private struct LedgerGroup: Identifiable {
    let date: Date
    let entries: [LogEntry]
    var id: Date { date }
}

private struct QuotaTile: View {
    let title: String
    let total: Double
    let quota: Double?
    let unit: String
    let leftSuffix: String
    let todaySuffix: String

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(title)
                .font(.caption)
                .foregroundStyle(.secondary)
            if let quota {
                let remaining = quota - total
                if remaining >= 0 {
                    Text("\(Int(remaining.rounded()).formatted()) \(leftSuffix)")
                        .font(.title2.weight(.semibold))
                        .minimumScaleFactor(0.75)
                        .lineLimit(1)
                } else {
                    HStack(alignment: .firstTextBaseline, spacing: 5) {
                        Text(Int((-remaining).rounded()).formatted())
                            .foregroundStyle(.orange)
                        Text(unit == "g" ? "g over" : "over")
                    }
                        .font(.title2.weight(.semibold))
                        .minimumScaleFactor(0.75)
                        .lineLimit(1)
                }
                Text("of \(Int(quota.rounded()).formatted()) \(unit)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                Text("\(Int(total.rounded()).formatted()) \(todaySuffix)")
                    .font(.title2.weight(.semibold))
                    .minimumScaleFactor(0.75)
                    .lineLimit(1)
                Text("Set a quota in chat")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(13)
        .background(Color(uiColor: .secondarySystemBackground), in: RoundedRectangle(cornerRadius: 14))
    }
}

private struct HeatmapsView: View {
    let days: [DaySummary]
    let kcalQuota: Double?

    var body: some View {
        HStack(alignment: .top, spacing: 16) {
            heatmap(title: "Calories", mode: .calories)
            heatmap(title: "Exercise", mode: .exercise)
        }
        .frame(maxWidth: .infinity)
        .padding(13)
        .background(Color(uiColor: .secondarySystemBackground), in: RoundedRectangle(cornerRadius: 14))
    }

    private func heatmap(title: String, mode: HeatmapMode) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title)
                .font(.headline)
            HStack(spacing: 3) {
                ForEach(0..<12, id: \.self) { column in
                    VStack(spacing: 3) {
                        ForEach(0..<7, id: \.self) { row in
                            let slot = column * 7 + row
                            let value = alignedCells[slot]
                            HeatmapCell(
                                day: value?.day,
                                originalIndex: value?.index,
                                firstDataIndex: firstDataIndex,
                                todayID: days.last?.date,
                                kcalQuota: kcalQuota,
                                mode: mode
                            )
                        }
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var firstDataIndex: Int? {
        days.firstIndex { $0.logged || $0.exercised }
    }

    private var alignedCells: [(day: DaySummary, index: Int)?] {
        guard let last = days.last,
              let lastDate = Self.dayFormatter.date(from: last.date) else {
            return Array(repeating: nil, count: 84)
        }
        let calendar = Self.utcCalendar
        let mondayRow = (calendar.component(.weekday, from: lastDate) + 5) % 7
        guard let start = calendar.date(byAdding: .day, value: -(mondayRow + 77), to: lastDate) else {
            return Array(repeating: nil, count: 84)
        }
        let indexed = Dictionary(uniqueKeysWithValues: days.enumerated().map {
            ($0.element.date, (day: $0.element, index: $0.offset))
        })
        return (0..<84).map { offset in
            guard let date = calendar.date(byAdding: .day, value: offset, to: start) else { return nil }
            return indexed[Self.dayFormatter.string(from: date)]
        }
    }

    private static var utcCalendar: Calendar = {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!
        return calendar
    }()

    private static var dayFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.calendar = utcCalendar
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter
    }()
}

private enum HeatmapMode {
    case calories
    case exercise
}

private struct HeatmapCell: View {
    let day: DaySummary?
    let originalIndex: Int?
    let firstDataIndex: Int?
    let todayID: String?
    let kcalQuota: Double?
    let mode: HeatmapMode

    private var isToday: Bool {
        guard let day, let todayID else { return false }
        return day.date == todayID
    }
    private var isNeutral: Bool {
        guard let firstDataIndex, let originalIndex else { return true }
        return originalIndex < firstDataIndex
    }

    private static let unfilled = Color(uiColor: .quaternarySystemFill)

    private var fill: Color {
        guard let day else { return .clear }
        guard !isNeutral else { return Self.unfilled }
        switch mode {
        case .exercise:
            return day.exercised ? Color("HeatmapGreen") : Self.unfilled
        case .calories:
            if !day.logged { return isToday ? Self.unfilled : Color("HeatmapRed") }
            if let kcalQuota, day.kcal > kcalQuota { return Color("HeatmapOrange") }
            return Color("HeatmapGreen")
        }
    }

    var body: some View {
        RoundedRectangle(cornerRadius: 2)
            .fill(fill)
            .frame(width: 8, height: 8)
            .overlay {
                if isToday {
                    RoundedRectangle(cornerRadius: 3)
                        .stroke(.primary, lineWidth: 1.5)
                        .padding(-1)
                }
            }
    }
}

private struct LedgerRow: View {
    let entry: LogEntry

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: symbol)
                .frame(width: 22)
                .foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 3) {
                Text(primaryText)
                    .lineLimit(2)
                Text(entry.date.formatted(Date.FormatStyle().hour().minute()))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                if let macros {
                    Text(macros)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            Spacer(minLength: 8)
            Text(trailingText)
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.trailing)
        }
        .padding(.vertical, 10)
    }

    private var symbol: String {
        switch entry.type {
        case "meal": "fork.knife"
        case "workout": "figure.run"
        default: "scalemass"
        }
    }

    private var primaryText: String {
        switch entry.type {
        case "meal": entry.note ?? ""
        case "workout": entry.note ?? entry.activity ?? ""
        default: entry.kind ?? ""
        }
    }

    private var trailingText: String {
        switch entry.type {
        case "meal": return entry.kcal.map { "\($0.inklingNumber) kcal" } ?? ""
        case "workout":
            var text = "\((entry.durationMin ?? 0).inklingNumber) min"
            if let burned = entry.kcalBurned { text += " · \(burned.inklingNumber) kcal" }
            return text
        default:
            return "\((entry.value ?? 0).inklingNumber) \(entry.unit ?? "")"
        }
    }

    private var macros: String? {
        guard entry.type == "meal" else { return nil }
        var parts: [String] = []
        if let protein = entry.proteinG { parts.append("P \(protein.inklingNumber)") }
        if let carbs = entry.carbG { parts.append("C \(carbs.inklingNumber)") }
        if let fat = entry.fatG { parts.append("F \(fat.inklingNumber)") }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }
}
