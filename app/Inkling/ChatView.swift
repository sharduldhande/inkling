import SwiftUI
import UIKit

struct ChatView: View {
    @Bindable var store: ChatStore
    let settings: AppSettings
    @FocusState private var inputFocused: Bool
    @State private var nearBottom = true
    @State private var showNewPill = false

    // Voice (Part C, STT only)
    @State private var transcriber = DeepgramTranscriptionService()
    @State private var draftBeforeRecording = ""
    @State private var micPressStartedRecording = false
    @State private var micPressBegan: Date?
    @State private var showNoKeyHint = false
    @State private var showMicDeniedAlert = false
    /// Tap vs hold threshold (§C3).
    private static let holdThreshold: TimeInterval = 0.3

    // Camera
    @State private var showCamera = false
    @State private var showNoCameraAlert = false

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
                                MessageBubble(
                                    message: message,
                                    retry: { store.retry(message) },
                                    loadPhoto: message.isPhoto
                                        ? { await store.photo(for: message) }
                                        : nil
                                )
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
                    .defaultScrollAnchor(.bottom)
                    .scrollDismissesKeyboard(.interactively)
                    .onPreferenceChange(ChatBottomPreferenceKey.self) { bottom in
                        nearBottom = bottom - viewport.size.height < 100
                        if nearBottom { showNewPill = false }
                    }
                    .onChange(of: store.scrollToBottomRequestID) { _, _ in
                        // Runs after the system scroll-to-top from the tab re-tap.
                        DispatchQueue.main.async {
                            withAnimation { proxy.scrollTo("chat-bottom", anchor: .bottom) }
                            showNewPill = false
                        }
                    }
                    .onChange(of: store.messages) { old, new in
                        // The user's own send ALWAYS follows to the bottom:
                        // nearBottom goes stale during keyboard transitions
                        // (viewport and bottom-marker update out of sync),
                        // and a sent bubble landing behind the keyboard
                        // reads as a lost message.
                        let sentByUser = new.count > old.count && new.last?.isUser == true
                        if nearBottom || sentByUser {
                            showNewPill = false
                            withAnimation { proxy.scrollTo("chat-bottom", anchor: .bottom) }
                        } else {
                            showNewPill = true
                        }
                    }
                    .overlay(alignment: .bottom) {
                        // Floating glass pill over the transcript, like the
                        // "New" pill — a layout row here carves an opaque
                        // band out of the window background under the bar.
                        if let hint = hintText {
                            Text(hint.text)
                                .font(.caption)
                                .foregroundStyle(hint.isError ? Color.red : Color.secondary)
                                .padding(.horizontal, 12)
                                .padding(.vertical, 7)
                                .glassEffect()
                                .padding(.bottom, 8)
                                .transition(.opacity)
                        }
                    }
                    .animation(.easeInOut(duration: 0.2), value: hintText?.text)
                    .overlay(alignment: .bottomLeading) {
                        stagedPhotoChip
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
            // safeAreaInset, not a VStack row: the transcript scrolls UNDER
            // the glass bar (true floating); a sibling row leaves the bar
            // sitting on an opaque strip of window background.
            .safeAreaInset(edge: .bottom) { inputBar }
        }
        .onChange(of: transcriber.liveTranscript) { _, live in
            // Live words appear in the field as Deepgram streams results.
            guard transcriber.isRecording else { return }
            store.draft = draftBeforeRecording.isEmpty
                ? live
                : (live.isEmpty ? draftBeforeRecording : draftBeforeRecording + " " + live)
        }
        .onChange(of: transcriber.permissionDenied) { _, denied in
            if denied { showMicDeniedAlert = true }
        }
        .fullScreenCover(isPresented: $showCamera) {
            CameraPicker { jpeg in
                // Staged, not sent: the user captions it (or not) and hits send.
                store.pendingPhoto = jpeg
            }
            .ignoresSafeArea()
        }
        .alert("Microphone access is off", isPresented: $showMicDeniedAlert) {
            Button("Open Settings") {
                if let url = URL(string: UIApplication.openSettingsURLString) {
                    UIApplication.shared.open(url)
                }
            }
            Button("Cancel", role: .cancel) { }
        } message: {
            Text("Allow microphone access for Inkling in iOS Settings to use voice input.")
        }
        .alert("No camera available on this device.", isPresented: $showNoCameraAlert) {
            Button("OK", role: .cancel) { }
        }
        .animation(.easeInOut(duration: 0.2), value: store.healthCaption)
        .onChange(of: store.focusRequestID) { _, _ in
            inputFocused = true
        }
        .onAppear {
            if !store.draft.isEmpty { inputFocused = true }
        }
    }

    // The staged-photo chip must NOT live inside the safeAreaInset: growing
    // the inset while the camera cover dismisses displaced the scroll
    // position and shoved the whole transcript off-screen (user video —
    // same safe-area-mutation gremlin as the tab-flash saga). It floats
    // over the transcript instead, like the hint pill.
    private var stagedPhotoChip: some View {
        Group {
            if let staged = store.pendingPhoto, let thumb = UIImage(data: staged) {
                ZStack(alignment: .topTrailing) {
                    Image(uiImage: thumb)
                        .resizable()
                        .scaledToFill()
                        .frame(width: 64, height: 64)
                        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                    Button {
                        store.pendingPhoto = nil
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .font(.body)
                            .foregroundStyle(.white, .black.opacity(0.6))
                    }
                    .offset(x: 7, y: -7)
                    .accessibilityLabel("Remove photo")
                }
                .padding(.leading, 20)
                .padding(.bottom, 8)
                .transition(.scale.combined(with: .opacity))
            }
        }
        .animation(.easeInOut(duration: 0.15), value: store.pendingPhoto != nil)
    }

    private var inputBar: some View {
        GlassEffectContainer {
            inputRow
        }
        .padding(.horizontal)
        .padding(.vertical, 9)
    }

    private var inputRow: some View {
            HStack(alignment: .bottom, spacing: 10) {
                    if transcriber.isRecording {
                        Button {
                            cancelRecording()
                        } label: {
                            Image(systemName: "xmark")
                                .font(.title3)
                                .frame(width: 26, height: 26)
                        }
                        .buttonStyle(.glass)
                        .buttonBorderShape(.circle)
                        .accessibilityLabel("Cancel recording")
                    } else {
                        Button {
                            openCamera()
                        } label: {
                            Image(systemName: "camera.fill")
                                .font(.title3)
                                .frame(width: 26, height: 26)
                        }
                        .buttonStyle(.glass)
                        .buttonBorderShape(.circle)
                        .accessibilityLabel("Take a photo")
                    }

                    TextField(
                        transcriber.isRecording ? "Listening…" : "Message Inkling",
                        text: $store.draft, axis: .vertical
                    )
                        .lineLimit(1...5)
                        .textFieldStyle(.plain)
                        .padding(.horizontal, 16)
                        .padding(.vertical, 9)
                        .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
                        .focused($inputFocused)
                        .submitLabel(.send)
                        .disabled(transcriber.isRecording)
                        .onSubmit {
                            if canSend { store.send() }
                        }

                    micButton

                    Button { store.send() } label: {
                        Image(systemName: "paperplane.fill")
                            .font(.title3)
                            .frame(width: 26, height: 26)
                    }
                    .buttonStyle(.glassProminent)
                    .buttonBorderShape(.circle)
                    .disabled(!canSend)
                    .accessibilityLabel("Send")
                }
    }

    /// A staged photo can always go (uploads don't occupy the chat turn);
    /// bare text needs content and a free turn slot.
    private var canSend: Bool {
        guard !transcriber.isRecording else { return false }
        if store.pendingPhoto != nil { return true }
        let text = store.draft.trimmingCharacters(in: .whitespacesAndNewlines)
        return !text.isEmpty && !store.isTurnInFlight
    }

    private var hintText: (text: String, isError: Bool)? {
        if let hint = transcriber.errorHint { return (hint, true) }
        if showNoKeyHint { return ("Add a Deepgram API key in Settings.", false) }
        return nil
    }

    // MARK: - Mic (§C3: press-and-hold OR tap-to-toggle, one button)

    private var micEnabled: Bool {
        settings.hasDeepgramKey && !store.isTurnInFlight
    }

    private var micButton: some View {
        Image(systemName: "mic.fill")
            .font(.title3)
            // symbolEffect pulses the GLYPH only. The previous
            // scaleEffect + .animation(.repeatForever) leaked into layout:
            // once the keyboard moved the bar, the forever-animation applied
            // to the mic's position and it drifted around the transcript.
            .symbolEffect(.pulse, isActive: transcriber.isRecording)
            .frame(width: 26, height: 26)
            .padding(9)
            .foregroundStyle(
                transcriber.isRecording ? Color.white : (micEnabled ? Color.primary : Color.secondary.opacity(0.5))
            )
            .glassEffect(
                transcriber.isRecording ? .regular.tint(.red) : .regular,
                in: Circle()
            )
            // The padded glass circle is not hit-testable on its own; without
            // this only the 26pt glyph accepts touches (verified in sim).
            .contentShape(Circle())
            .gesture(
                DragGesture(minimumDistance: 0)
                    .onChanged { _ in
                        guard micPressBegan == nil else { return }
                        micPressBegan = Date()
                        micPressStartedRecording = false
                        guard micEnabled else { return }
                        if !transcriber.isRecording {
                            micPressStartedRecording = true
                            startRecording()
                        }
                    }
                    .onEnded { _ in
                        let began = micPressBegan ?? Date()
                        micPressBegan = nil
                        guard micEnabled else {
                            hintNoKeyIfNeeded()
                            return
                        }
                        let held = Date().timeIntervalSince(began) >= Self.holdThreshold
                        if micPressStartedRecording && !held {
                            // Quick tap started it: leave it running (toggle on).
                            return
                        }
                        // Hold released, or a tap while already recording.
                        finishRecording()
                    }
            )
            .accessibilityLabel(transcriber.isRecording ? "Stop and send" : "Record a voice message")
    }

    private func hintNoKeyIfNeeded() {
        guard !settings.hasDeepgramKey else { return }
        showNoKeyHint = true
        Task {
            try? await Task.sleep(for: .seconds(3))
            showNoKeyHint = false
        }
    }

    private func startRecording() {
        draftBeforeRecording = store.draft
        Task {
            let started = await transcriber.start(apiKey: settings.currentDeepgramKey())
            if !started { micPressStartedRecording = false }
        }
    }

    private func finishRecording() {
        guard transcriber.isRecording else { return }
        Task {
            let transcript = await transcriber.stopAndFinish()
            let prefix = draftBeforeRecording
            draftBeforeRecording = ""
            let combined = [prefix, transcript]
                .filter { !$0.isEmpty }
                .joined(separator: " ")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            store.draft = combined
            guard !combined.isEmpty else { return }
            store.send(voiceOriginated: true)
        }
    }

    private func cancelRecording() {
        transcriber.cancel()
        store.draft = draftBeforeRecording
        draftBeforeRecording = ""
    }

    private func openCamera() {
        guard UIImagePickerController.isSourceTypeAvailable(.camera) else {
            showNoCameraAlert = true
            return
        }
        showCamera = true
    }
}

private struct ChatBottomPreferenceKey: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        value = nextValue()
    }
}
