import SwiftUI

struct MessageBubble: View {
    let message: ChatMessage
    let retry: () -> Void

    var body: some View {
        HStack {
            if message.isUser { Spacer(minLength: 52) }
            VStack(alignment: message.isUser ? .trailing : .leading, spacing: 4) {
                bubble
                Text(message.date.formatted(Date.FormatStyle().hour().minute()))
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                stateCaption
            }
            if !message.isUser { Spacer(minLength: 52) }
        }
        .frame(maxWidth: .infinity)
    }

    @ViewBuilder
    private var bubble: some View {
        Group {
            if message.state == .streaming && message.text.isEmpty {
                TypingDots()
                    .frame(width: 44, height: 18)
            } else {
                Text(message.text)
                    .textSelection(.enabled)
            }
        }
        .padding(.horizontal, 13)
        .padding(.vertical, 9)
        .foregroundStyle(message.isUser ? Color.white : Color.primary)
        .glassEffect(
            message.isUser ? .regular.tint(.accentColor) : .regular,
            in: RoundedRectangle(cornerRadius: 16, style: .continuous)
        )
    }

    @ViewBuilder
    private var stateCaption: some View {
        switch message.state {
        case .sending:
            Text("Uploading…")
                .font(.caption2)
                .foregroundStyle(.secondary)
        case .queued:
            Text("Queued — Inkling is busy; it'll answer shortly.")
                .font(.caption2)
                .foregroundStyle(.secondary)
        case .failed:
            VStack(alignment: .trailing, spacing: 2) {
                HStack(spacing: 6) {
                    Text("Not sent.")
                        .foregroundStyle(.secondary)
                    Button("Retry", action: retry)
                }
                if let reason = message.failureReason {
                    Text(reason)
                        .foregroundStyle(.red)
                }
            }
            .font(.caption2)
        case .incomplete:
            Text("connection dropped — reply may be incomplete")
                .font(.caption2)
                .foregroundStyle(.secondary)
        case .errored:
            Text("Something went wrong on the server.")
                .font(.caption2)
                .foregroundStyle(.red)
        default:
            EmptyView()
        }
    }
}

private struct TypingDots: View {
    @State private var animated = false

    var body: some View {
        HStack(spacing: 5) {
            ForEach(0..<3, id: \.self) { index in
                Circle()
                    .fill(.secondary)
                    .frame(width: 7, height: 7)
                    .opacity(animated ? 0.3 : 1)
                    .animation(
                        .easeInOut(duration: 0.55).repeatForever(autoreverses: true).delay(Double(index) * 0.16),
                        value: animated
                    )
            }
        }
        .onAppear { animated = true }
    }
}
