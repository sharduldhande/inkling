import SwiftUI
import UIKit

struct MessageBubble: View {
    let message: ChatMessage
    let retry: () -> Void
    /// Photo bubbles resolve their image through the store (local cache,
    /// then a server refetch); nil for text-only callers.
    var loadPhoto: (() async -> UIImage?)? = nil

    @State private var photo: UIImage?

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
            if message.isPhoto && loadPhoto != nil {
                VStack(alignment: .leading, spacing: 8) {
                    Group {
                        if let photo {
                            Image(uiImage: photo)
                                .resizable()
                                .scaledToFill()
                        } else {
                            ProgressView()
                        }
                    }
                    .frame(maxWidth: 220, maxHeight: 260)
                    .frame(minWidth: 120, minHeight: 90)
                    .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                    if !message.text.isEmpty, !message.text.hasPrefix("[photo") {
                        Text(message.text)
                            .textSelection(.enabled)
                    }
                }
                .task(id: message.photoFile) {
                    photo = await loadPhoto?()
                }
            } else if message.state == .streaming && message.text.isEmpty {
                TypingDots()
                    .frame(width: 44, height: 18)
            } else if message.state == .streaming {
                // Text is arriving but the turn isn't done: keep a small
                // pulse under it so a pause between chunks doesn't read as
                // a finished (or cut-off) reply.
                VStack(alignment: .leading, spacing: 6) {
                    Text(message.text)
                        .textSelection(.enabled)
                    TypingDots(dotSize: 5)
                        .frame(height: 10)
                }
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
    var dotSize: CGFloat = 7
    @State private var animated = false

    var body: some View {
        HStack(spacing: dotSize * 0.7) {
            ForEach(0..<3, id: \.self) { index in
                Circle()
                    .fill(.secondary)
                    .frame(width: dotSize, height: dotSize)
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
