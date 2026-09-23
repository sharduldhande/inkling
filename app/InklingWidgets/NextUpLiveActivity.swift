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
            NextUpCard(state: context.state)
                .padding(.horizontal, 18)
                .padding(.vertical, 14)
                .widgetURL(URL(string: "inkling://"))
        } dynamicIsland: { context in
            DynamicIsland {
                DynamicIslandExpandedRegion(.bottom) {
                    NextUpCard(state: context.state)
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
        Image(systemName: state.kind == "deadline" ? "graduationcap.fill" : "checklist")
            .font(.caption.weight(.semibold))
            .foregroundStyle(state.overdue ? Color.red : Color.primary)
    }
}
