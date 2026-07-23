import Foundation
import Observation
import UIKit

enum HealthCaption: Equatable {
    case online
    case unreachable
}

@MainActor
@Observable
final class ChatStore {
    var messages: [ChatMessage]
    var draft = ""
    /// A snapped photo staged as an attachment; sent (with the draft as its
    /// caption) on the next send. Nil once sent or discarded.
    var pendingPhoto: Data?
    var focusRequestID = UUID()
    var scrollToBottomRequestID = UUID()
    var isTurnInFlight = false
    var healthCaption: HealthCaption?

    @ObservationIgnored private let settings: AppSettings
    @ObservationIgnored private var isSyncing = false
    @ObservationIgnored private var syncDeferred = false
    @ObservationIgnored private var openedRequests = Set<UUID>()
    @ObservationIgnored private var queuedPollTask: Task<Void, Never>?
    @ObservationIgnored private var incompleteSyncTask: Task<Void, Never>?
    @ObservationIgnored private var healthCaptionTask: Task<Void, Never>?
    @ObservationIgnored private var streamTask: Task<Void, Never>?
    /// Bytes for photos still uploading or failed-and-retryable. Deliberately
    /// not persisted: a relaunch turns .sending into .failed (see init).
    @ObservationIgnored private var pendingPhotos: [UUID: Data] = [:]
    @ObservationIgnored private var watchdogTask: Task<Void, Never>?
    @ObservationIgnored private var lastFrameAt = Date()

    /// No stream frame for this long → the turn is presumed lost (URLSession's
    /// own timeout resets across app suspensions, so it alone is not enough —
    /// seen live as a bubble that kept its typing dots for 6+ minutes after a
    /// server crash, with isTurnInFlight stuck blocking all sends).
    private static let stallSeconds: TimeInterval = 90

    init(settings: AppSettings) {
        self.settings = settings
        var cached = Array((CacheFiles.load([ChatMessage].self, named: CacheFiles.chat) ?? []).suffix(200))
        // A cold launch has no task behind any in-flight state: a cached
        // .streaming bubble would show typing dots forever, and a cached
        // .sending photo has no upload (or bytes) behind it anymore.
        for i in cached.indices {
            if cached[i].state == .streaming { cached[i].state = .incomplete }
            if cached[i].state == .sending { cached[i].state = .failed }
        }
        messages = cached
    }

    func setPrefill(_ text: String) {
        draft = text
        focusRequestID = UUID()
    }

    func requestScrollToBottom() {
        scrollToBottomRequestID = UUID()
    }

    func send(voiceOriginated: Bool = false) {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        if let jpeg = pendingPhoto {
            // Photo (with the text as caption, possibly empty) — independent
            // of the chat turn pipeline, so isTurnInFlight doesn't gate it.
            pendingPhoto = nil
            draft = ""
            sendPhoto(jpeg, caption: text)
            return
        }
        guard !text.isEmpty, !isTurnInFlight else { return }
        draft = ""
        let user = ChatMessage(
            localID: UUID(), serverID: nil, role: "user", text: text,
            date: Date(), serverTS: nil, state: .sent, voiceOriginated: voiceOriginated
        )
        messages.append(user)
        beginPost(for: user.localID)
    }

    func retry(_ message: ChatMessage) {
        guard message.state == .failed, !isTurnInFlight,
              let index = messages.firstIndex(where: { $0.localID == message.localID }) else { return }
        if message.isPhoto {
            guard photoBytes(for: message.localID) != nil else {
                messages[index].failureReason = "Photo bytes lost — take it again."
                persist()
                return
            }
            messages[index].state = .sending
            messages[index].failureReason = nil
            messages[index].date = Date()
            persist()
            Task { await uploadPhoto(message.localID) }
            return
        }
        messages[index].state = .sent
        messages[index].date = Date()
        messages[index].serverID = nil
        messages[index].serverTS = nil
        beginPost(for: message.localID)
    }

    /// §B8.7 flow, photo flavor: local thumbnail bubble immediately,
    /// multipart upload (caption rides along), then attach to the ingestion
    /// turn's live stream so the proposal renders like a chat reply.
    /// Independent of the chat turn pipeline — no 429 rules.
    func sendPhoto(_ jpeg: Data, caption: String = "") {
        let message = ChatMessage(
            localID: UUID(), serverID: nil, role: "user", text: caption,
            date: Date(), serverTS: nil, state: .sending, voiceOriginated: false,
            failureReason: nil, photoFile: CacheFiles.savePhoto(jpeg)
        )
        pendingPhotos[message.localID] = jpeg
        messages.append(message)
        persist()
        Task { await uploadPhoto(message.localID) }
    }

    private func uploadPhoto(_ id: UUID) async {
        guard let jpeg = photoBytes(for: id) else { return }
        let caption = messages.first(where: { $0.localID == id })?.text ?? ""
        let filename = "photo-\(Int(Date().timeIntervalSince1970)).jpg"
        do {
            let response = try await settings.client().uploadPhoto(
                jpeg: jpeg, filename: filename, caption: caption
            )
            guard let index = messages.firstIndex(where: { $0.localID == id }) else { return }
            messages[index].state = .sent
            // Synthetic serverID: exempts the bubble from §B8.6 healing (the
            // server transcript has no matching user line) while keeping it
            // out of duplicate-append paths.
            messages[index].serverID = "doc-\(response.documentId)"
            pendingPhotos[id] = nil
            persist()
            streamProposal(documentID: response.documentId)
        } catch {
            guard let index = messages.firstIndex(where: { $0.localID == id }) else { return }
            messages[index].state = .failed
            if case InklingClientError.rejected(let reason) = error {
                messages[index].failureReason = reason
            }
            if error as? InklingClientError == .unauthorized {
                NotificationCenter.default.post(name: .inklingUnauthorized, object: nil)
            }
            persist()
        }
    }

    /// Render the ingestion turn live (GET /documents/{id}/stream): typing
    /// dots, streamed tokens, done — the same shape as a chat reply. A
    /// dropped stream degrades to .incomplete and the ordinary history sync
    /// heals it (ingestion replies ARE in the server transcript).
    private func streamProposal(documentID: Int) {
        let assistant = ChatMessage(
            localID: UUID(), serverID: nil, role: "assistant", text: "",
            date: Date(), serverTS: nil, state: .streaming, voiceOriginated: false
        )
        messages.append(assistant)
        persist()
        Task {
            var receivedDone = false
            do {
                for try await event in settings.client().streamDocument(id: documentID) {
                    guard let index = messages.firstIndex(where: { $0.localID == assistant.localID }) else { return }
                    switch event {
                    case .text(let chunk):
                        messages[index].text += chunk
                    case .done:
                        receivedDone = true
                        messages[index].state = .done
                        NotificationCenter.default.post(name: .inklingDataShouldRefresh, object: nil)
                    case .exit(let code):
                        if code != 0 { messages[index].state = .errored }
                    }
                    persist()
                }
            } catch {
                // fall through to the incomplete handling below
            }
            if !receivedDone,
               let index = messages.firstIndex(where: { $0.localID == assistant.localID }),
               messages[index].state == .streaming {
                if messages[index].text.isEmpty {
                    // Nothing arrived (connection died early): drop the dots;
                    // the sync below appends the reply as a normal message.
                    messages.remove(at: index)
                } else {
                    messages[index].state = .incomplete
                }
                scheduleIncompleteSync()
            }
            persist()
        }
    }

    /// The staged/failed photo's bytes: in-memory first, then the thumbnail
    /// file — so retry works even after a relaunch.
    private func photoBytes(for id: UUID) -> Data? {
        if let jpeg = pendingPhotos[id] { return jpeg }
        guard let file = messages.first(where: { $0.localID == id })?.photoFile else { return nil }
        return try? Data(contentsOf: CacheFiles.photoURL(file))
    }

    /// The bubble's image: local cache first, else refetch the original from
    /// the server (photos survive cache clears server-side) and re-cache.
    func photo(for message: ChatMessage) async -> UIImage? {
        if let file = message.photoFile,
           let image = UIImage(contentsOfFile: CacheFiles.photoURL(file).path) {
            return image
        }
        guard let documentID = message.documentID,
              let data = try? await settings.client().fetchDocumentOriginal(id: documentID),
              let image = UIImage(data: data) else {
            return nil
        }
        if let name = CacheFiles.savePhoto(data),
           let index = messages.firstIndex(where: { $0.localID == message.localID }) {
            messages[index].photoFile = name
            persist()
        }
        return image
    }

    func syncHistory() async {
        guard settings.hasToken else { return }
        if isTurnInFlight {
            syncDeferred = true
            return
        }
        guard !isSyncing else { return }
        isSyncing = true
        defer { isSyncing = false }
        let since = messages.compactMap(\.serverTS).max() ?? 0
        do {
            let response = try await settings.client().fetchHistory(since: since)
            var changed = false
            var knownIDs = Set(messages.compactMap(\.serverID))

            for fetched in response.messages where !knownIDs.contains(fetched.id) {
                let fetchedDate = Date(timeIntervalSince1970: Double(fetched.ts) / 1_000)
                if let localIndex = messages.firstIndex(where: { local in
                    guard local.serverID == nil,
                          local.role == fetched.role,
                          abs(local.date.timeIntervalSince(fetchedDate)) <= 180 else { return false }
                    return fetched.text == local.text || fetched.text.hasPrefix(local.text) || local.text.isEmpty
                }) {
                    messages[localIndex].serverID = fetched.id
                    messages[localIndex].serverTS = fetched.ts
                    messages[localIndex].date = fetchedDate
                    messages[localIndex].text = fetched.text
                    messages[localIndex].state = fetched.role == "user" ? .sent : .done
                } else {
                    messages.append(ChatMessage(
                        localID: UUID(), serverID: fetched.id, role: fetched.role,
                        text: fetched.text, date: fetchedDate, serverTS: fetched.ts,
                        state: fetched.role == "user" ? .sent : .done,
                        voiceOriginated: false
                    ))
                }
                knownIDs.insert(fetched.id)
                changed = true
            }

            messages.sort { lhs, rhs in
                let left = lhs.serverTS.map(Double.init) ?? lhs.date.timeIntervalSince1970 * 1_000
                let right = rhs.serverTS.map(Double.init) ?? rhs.date.timeIntervalSince1970 * 1_000
                if left == right {
                    // A turn's user message and reply share one server ts
                    // (written in one transaction); the bigint id is the
                    // server's true order. A UUID tiebreak here is a coin
                    // flip that visibly swapped a message under its reply.
                    if let li = lhs.serverID.flatMap({ Int($0) }),
                       let ri = rhs.serverID.flatMap({ Int($0) }) {
                        return li < ri
                    }
                    return lhs.localID.uuidString < rhs.localID.uuidString
                }
                return left < right
            }
            persist()
            if changed {
                NotificationCenter.default.post(name: .inklingDataShouldRefresh, object: nil)
            }
        } catch InklingClientError.unauthorized {
            NotificationCenter.default.post(name: .inklingUnauthorized, object: nil)
        } catch {
            // Cached transcript remains visible; foreground and pull-to-refresh can try again.
        }
    }

    func checkHealthAndSync() async {
        do {
            _ = try await settings.client().health()
            showHealthCaption(.online)
            await syncHistory()
        } catch {
            showHealthCaption(.unreachable)
        }
    }

    func clearLocalCache() {
        messages = []
    }

    private func beginPost(for userID: UUID) {
        guard let user = messages.first(where: { $0.localID == userID }) else { return }
        let assistant = ChatMessage(
            localID: UUID(), serverID: nil, role: "assistant", text: "",
            date: Date(), serverTS: nil, state: .streaming, voiceOriginated: false
        )
        messages.append(assistant)
        isTurnInFlight = true
        lastFrameAt = Date()
        persist()

        watchdogTask?.cancel()
        watchdogTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(10))
                guard let self, !Task.isCancelled else { return }
                if self.isTurnInFlight, Date().timeIntervalSince(self.lastFrameAt) > Self.stallSeconds {
                    self.streamTask?.cancel()
                    return
                }
            }
        }

        streamTask = Task {
            var receivedDone = false
            var receivedServerError = false
            let stream = settings.client().streamChat(message: user.text) { [weak self] in
                await self?.markOpened(userID)
            }
            do {
                for try await event in stream {
                    lastFrameAt = Date()
                    guard let assistantIndex = messages.firstIndex(where: { $0.localID == assistant.localID }) else { continue }
                    switch event {
                    case .text(let chunk):
                        messages[assistantIndex].text += chunk
                    case .done:
                        receivedDone = true
                        messages[assistantIndex].state = .done
                        NotificationCenter.default.post(name: .inklingDataShouldRefresh, object: nil)
                    case .exit(let code):
                        if code != 0 {
                            receivedServerError = true
                            messages[assistantIndex].state = .errored
                        }
                    }
                    persist()
                }
                if !receivedDone && !receivedServerError {
                    markIncomplete(assistant.localID)
                }
                finishPost(userID)
            } catch {
                handlePostError(error, userID: userID, assistantID: assistant.localID, receivedDone: receivedDone)
            }
        }
    }

    private func markOpened(_ userID: UUID) {
        openedRequests.insert(userID)
    }

    private func handlePostError(_ error: Error, userID: UUID, assistantID: UUID, receivedDone: Bool) {
        let opened = openedRequests.contains(userID)
        if error as? InklingClientError == .busy429 {
            removeMessage(assistantID)
            updateUser(userID, state: .queued)
            startQueuedPollingIfNeeded()
        } else if error as? InklingClientError == .unauthorized {
            removeMessage(assistantID)
            updateUser(userID, state: .failed)
            NotificationCenter.default.post(name: .inklingUnauthorized, object: nil)
        } else if opened {
            if !receivedDone { markIncomplete(assistantID) }
        } else {
            removeMessage(assistantID)
            updateUser(userID, state: .failed)
        }
        finishPost(userID)
    }

    private func markIncomplete(_ assistantID: UUID) {
        guard let index = messages.firstIndex(where: { $0.localID == assistantID }) else { return }
        messages[index].state = .incomplete
        scheduleIncompleteSync()
    }

    private func finishPost(_ userID: UUID) {
        openedRequests.remove(userID)
        isTurnInFlight = false
        watchdogTask?.cancel()
        watchdogTask = nil
        streamTask = nil
        persist()
        if syncDeferred {
            syncDeferred = false
            Task { await syncHistory() }
        }
    }

    private func updateUser(_ id: UUID, state: ChatMessageState) {
        guard let index = messages.firstIndex(where: { $0.localID == id }) else { return }
        messages[index].state = state
    }

    private func removeMessage(_ id: UUID) {
        messages.removeAll { $0.localID == id }
    }

    private func startQueuedPollingIfNeeded() {
        guard queuedPollTask == nil else { return }
        queuedPollTask = Task {
            for _ in 0..<18 {
                try? await Task.sleep(for: .seconds(10))
                guard !Task.isCancelled else { return }
                if let health = try? await settings.client().health(), !health.busy {
                    break
                }
            }
            queuedPollTask = nil
            await syncHistory()
        }
    }

    private func scheduleIncompleteSync() {
        guard incompleteSyncTask == nil else { return }
        incompleteSyncTask = Task {
            try? await Task.sleep(for: .seconds(5))
            guard !Task.isCancelled else { return }
            incompleteSyncTask = nil
            await syncHistory()
        }
    }

    private func showHealthCaption(_ caption: HealthCaption) {
        healthCaptionTask?.cancel()
        healthCaption = caption
        healthCaptionTask = Task {
            try? await Task.sleep(for: .seconds(2))
            guard !Task.isCancelled else { return }
            healthCaption = nil
        }
    }

    private func persist() {
        CacheFiles.save(Array(messages.suffix(200)), named: CacheFiles.chat)
    }
}
