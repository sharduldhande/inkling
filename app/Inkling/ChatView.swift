import SwiftUI

struct ChatView: View {
    @Bindable var store: ChatStore
    @FocusState private var inputFocused: Bool
    @State private var nearBottom = true
    @State private var showNewPill = false

    var body: some View {
        VStack(spacing: 0) {
            if let caption = store.healthCaption {
                Label(
                    caption == .online ? "Inkling online" : "Inkling unreachable",
                    systemImage: caption == .online ? "checkmark.circle.fill" : "xmark.circle"
                )
                .font(.caption)
                .foregroundStyle(caption == .online ? Color.green : Color.secondary)
                .padding(.horizontal, 12)
                .padding(.vertical, 5)
                .glassEffect()
                .padding(.vertical, 4)
                .transition(.opacity)
            }

            GeometryReader { viewport in
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(spacing: 12) {
                            ForEach(store.messages) { message in
                                MessageBubble(message: message) {
                                    store.retry(message)
                                }
                                .id(message.localID)
                            }
                            Color.clear
                                .frame(height: 1)
                                .id("chat-bottom")
                                .background(
                                    GeometryReader { marker in
                                        Color.clear.preference(
                                            key: ChatBottomPreferenceKey.self,
                                            value: marker.frame(in: .named("chat-scroll")).maxY
                                        )
                                    }
                                )
                        }
                        .padding(.horizontal)
                        .padding(.vertical, 10)
                    }
                    .coordinateSpace(name: "chat-scroll")
                    .scrollDismissesKeyboard(.interactively)
                    .onPreferenceChange(ChatBottomPreferenceKey.self) { bottom in
                        nearBottom = bottom - viewport.size.height < 100
                        if nearBottom { showNewPill = false }
                    }
                    .onChange(of: store.messages) { _, _ in
                        if nearBottom {
                            withAnimation { proxy.scrollTo("chat-bottom", anchor: .bottom) }
                        } else {
                            showNewPill = true
                        }
                    }
                    .onAppear {
                        DispatchQueue.main.async {
                            proxy.scrollTo("chat-bottom", anchor: .bottom)
                        }
                    }
                    .overlay(alignment: .bottomTrailing) {
                        if showNewPill {
                            Button {
                                withAnimation { proxy.scrollTo("chat-bottom", anchor: .bottom) }
                                showNewPill = false
                            } label: {
                                Text("↓ New")
                                    .font(.caption.weight(.semibold))
                                    .padding(.horizontal, 12)
                                    .padding(.vertical, 7)
                            }
                            .buttonStyle(.glass)
                            .padding(12)
                        }
                    }
                }
            }

            GlassEffectContainer {
                HStack(alignment: .bottom, spacing: 10) {
                    TextField("Message Inkling", text: $store.draft, axis: .vertical)
                        .lineLimit(1...5)
                        .textFieldStyle(.plain)
                        .padding(.horizontal, 16)
                        .padding(.vertical, 9)
                        .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
                        .focused($inputFocused)
                        .submitLabel(.send)
                        .onSubmit {
                            if !store.isTurnInFlight { store.send() }
                        }

                    Button { store.send() } label: {
                        Image(systemName: "paperplane.fill")
                            .font(.title3)
                            .frame(width: 26, height: 26)
                    }
                    .buttonStyle(.glassProminent)
                    .buttonBorderShape(.circle)
                    .disabled(store.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || store.isTurnInFlight)
                    .accessibilityLabel("Send")
                }
            }
            .padding(.horizontal)
            .padding(.vertical, 9)
        }
        .animation(.easeInOut(duration: 0.2), value: store.healthCaption)
        .onChange(of: store.focusRequestID) { _, _ in
            inputFocused = true
        }
        .onAppear {
            if !store.draft.isEmpty { inputFocused = true }
        }
    }
}

private struct ChatBottomPreferenceKey: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        value = nextValue()
    }
}
