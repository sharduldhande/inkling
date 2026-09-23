import ActivityKit
import SwiftUI
import WidgetKit

/// Lock-screen card first. iOS gives an app no way to opt out of the Dynamic
/// Island while an activity runs, so the compact island is a single glyph
/// (the same view macOS shows in the menu bar); the expanded view
/// (long-press, click, or the start alert) reuses the lock-screen content.
struct NextUpLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: NextUpAttributes.self) { context in
            CardView(state: context.state)
                .padding(.horizontal, 18)
                .padding(.vertical, 14)
                .widgetURL(URL(string: "inkling://"))
        } dynamicIsland: { context in
            DynamicIsland {
                DynamicIslandExpandedRegion(.bottom) {
                    CardView(state: context.state)
                }
            } compactLeading: {
                // One small glyph (user choice): it's what the Mac menu bar
                // shows for this activity, and all the island ever shows.
                NextUpGlyph(state: context.state)
            } compactTrailing: {
                EmptyView()
            } minimal: {
                NextUpGlyph(state: context.state)
            }
            .widgetURL(URL(string: "inkling://"))
        }
    }
}

/// Now / Next when the server sends it; the older single-deadline card if not.
struct CardView: View {
    let state: NextUpAttributes.ContentState

    var body: some View {
        if state.nowKind != nil {
            NowNextCard(state: state)
        } else {
            NextUpCard(state: state)
        }
    }
}

struct NowNextCard: View {
    let state: NextUpAttributes.ContentState

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            VStack(alignment: .leading, spacing: 3) {
                Label(nowLabel, systemImage: nowSymbol)
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(.secondary)
                Text(state.nowKind == "clear" ? "All clear" : (state.nowTitle ?? ""))
                    .font(.headline)
                    .lineLimit(2)
                nowLine
            }
            if let title = state.nextTitle {
                Divider()
                HStack(spacing: 6) {
                    Text("NEXT")
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(.secondary)
                    if state.nextKind == "focus" {
                        Image(systemName: "scope")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    Text(title)
                        .font(.subheadline)
                        .lineLimit(1)
                    Spacer(minLength: 4)
                    // A focus has no time; events show their start, deadlines "due …".
                    if let at = state.nextDate {
                        Text(nextWhen(at))
                            .font(.subheadline.monospacedDigit())
                            .foregroundStyle(.secondary)
                            .fixedSize()
                    }
                }
            }
        }
    }

    private var nowLabel: String {
        switch state.nowKind {
        case "event": "NOW"
        case "focus": "NOW · FOCUS"
        case "deadline": "NOW · DEADLINE"
        default: "NOW"
        }
    }

    private var nowSymbol: String {
        switch state.nowKind {
        case "event": "calendar"
        case "focus": "scope"
        case "deadline": "graduationcap"
        default: "checkmark.circle"
        }
    }

    /// Second line of NOW; relative times tick on their own, no pushes.
    @ViewBuilder
    private var nowLine: some View {
        switch state.nowKind {
        case "event":
            HStack(spacing: 4) {
                if let room = state.nowDetail {
                    Text(room).lineLimit(1)
                    Text("·")
                }
                if let until = state.nowUntilDate {
                    Text("until \(until.formatted(.dateTime.hour().minute()))")
                        .fixedSize()
                }
            }
            .font(.subheadline)
            .foregroundStyle(.secondary)
        case "deadline":
            if let due = state.nowDueDate {
                if state.nowOverdue == true {
                    (Text("Overdue ") + Text(due, style: .relative))
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(.red)
                } else {
                    Text("Due \(clock(due))")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
            }
        case "clear":
            Text("Nothing scheduled or due")
                .font(.subheadline)
                .foregroundStyle(.secondary)
        default:
            EmptyView()
        }
    }

    /// "3:30 PM" today, "Fri 3:30 PM" later.
    private func clock(_ at: Date) -> String {
        Calendar.current.isDateInToday(at)
            ? at.formatted(.dateTime.hour().minute())
            : at.formatted(.dateTime.weekday(.abbreviated).hour().minute())
    }

    /// Deadlines say "due"; events are just their start time.
    private func nextWhen(_ at: Date) -> String {
        state.nextKind == "deadline" ? "due \(clock(at))" : clock(at)
    }
}

struct NextUpCard: View {
    let state: NextUpAttributes.ContentState

    var body: some View {
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                Label(
                    state.kind == "deadline" ? "DEADLINE" : "NEXT UP",
                    systemImage: state.kind == "deadline" ? "graduationcap" : "checklist"
                )
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.secondary)
                if state.title.isEmpty {
                    Text("All clear")
                        .font(.headline)
                    Text("Nothing due in the next two weeks")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                } else {
                    Text(state.title)
                        .font(.headline)
                        .lineLimit(2)
                    if let due = state.dueDate {
                        dueLine(due)
                    }
                }
            }
            Spacer(minLength: 0)
            if let due = state.dueDate, !state.title.isEmpty {
                VStack(alignment: .trailing, spacing: 3) {
                    Text(due, format: dueFormat(due))
                        .font(.subheadline.monospacedDigit())
                        .foregroundStyle(.secondary)
                    if state.more > 0 {
                        Text("+\(state.more) more")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
        }
    }

    /// "3:30 PM" today, "Fri 11:59 PM" later — Canvas items can be days out.
    private func dueFormat(_ due: Date) -> Date.FormatStyle {
        Calendar.current.isDateInToday(due)
            ? .dateTime.hour().minute()
            : .dateTime.weekday(.abbreviated).hour().minute()
    }

    /// Self-updating relative time: the countdown ticks with no pushes.
    @ViewBuilder
    private func dueLine(_ due: Date) -> some View {
        if state.overdue {
            (Text("Overdue ") + Text(due, style: .relative))
                .font(.subheadline.weight(.medium))
                .foregroundStyle(.red)
        } else {
            (Text("Due in ") + Text(due, style: .relative))
                .font(.subheadline)
                .foregroundStyle(.secondary)
        }
    }
}

struct NextUpGlyph: View {
    let state: NextUpAttributes.ContentState

    var body: some View {
        Image(systemName: symbol)
            .font(.caption.weight(.semibold))
            .foregroundStyle(overdue ? Color.red : Color.primary)
    }

    private var symbol: String {
        switch state.nowKind {
        case "event": "calendar"
        case "focus": "scope"
        case "deadline": "graduationcap.fill"
        case "clear": "checkmark"
        default: state.kind == "deadline" ? "graduationcap.fill" : "checklist"
        }
    }

    private var overdue: Bool {
        state.nowKind == nil ? state.overdue : state.nowOverdue == true
    }
}
