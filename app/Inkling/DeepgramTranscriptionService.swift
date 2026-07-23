import AVFoundation
import Foundation
import Observation

/// Live streaming STT (§C4): AVAudioEngine tap → 16 kHz mono linear16 PCM →
/// Deepgram listen WebSocket. Finals accumulate; the latest interim is held
/// separately so the UI shows `finalizedText + " " + interimText` live.
@MainActor
@Observable
final class DeepgramTranscriptionService {
    var isRecording = false
    var finalizedText = ""
    var interimText = ""
    /// Brief, non-blocking failure hint ("Transcription failed"). Cleared on
    /// the next start.
    var errorHint: String?
    var permissionDenied = false

    @ObservationIgnored private var engine: AVAudioEngine?
    @ObservationIgnored private var socket: URLSessionWebSocketTask?
    @ObservationIgnored private var converter: AVAudioConverter?
    @ObservationIgnored private var outputFormat: AVAudioFormat?
    @ObservationIgnored private var receiveTask: Task<Void, Never>?
    /// Fulfilled when Deepgram acknowledges CloseStream (or errors); stop
    /// waits on it up to 1 s so the last final result isn't dropped.
    @ObservationIgnored private var drained: CheckedContinuation<Void, Never>?

    var liveTranscript: String {
        let joined = [finalizedText, interimText]
            .filter { !$0.isEmpty }
            .joined(separator: " ")
        return joined
    }

    /// Requests mic permission and starts capture + socket. Returns false
    /// (with `permissionDenied` or `errorHint` set) when nothing started.
    func start(apiKey: String) async -> Bool {
        guard !isRecording else { return true }
        errorHint = nil
        permissionDenied = false

        let granted = await AVAudioApplication.requestRecordPermission()
        guard granted else {
            permissionDenied = true
            return false
        }

        finalizedText = ""
        interimText = ""

        do {
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(
                .playAndRecord, mode: .default,
                options: [.defaultToSpeaker, .allowBluetooth]
            )
            try session.setActive(true)
        } catch {
            errorHint = "Microphone unavailable"
            return false
        }

        openSocket(apiKey: apiKey)

        let engine = AVAudioEngine()
        let input = engine.inputNode
        let inputFormat = input.outputFormat(forBus: 0)
        guard let target = AVAudioFormat(
            commonFormat: .pcmFormatInt16, sampleRate: 16_000,
            channels: 1, interleaved: true
        ), let converter = AVAudioConverter(from: inputFormat, to: target) else {
            errorHint = "Audio setup failed"
            teardownAudio()
            closeSocket()
            return false
        }
        self.converter = converter
        self.outputFormat = target

        input.installTap(onBus: 0, bufferSize: 1024, format: inputFormat) { [weak self] buffer, _ in
            // Tap runs on an audio thread; conversion is cheap, do it here
            // and hop to the main actor only to enqueue the send.
            guard let self else { return }
            guard let data = Self.convert(buffer: buffer, converter: converter, to: target) else { return }
            Task { @MainActor in
                self.sendAudio(data)
            }
        }

        do {
            engine.prepare()
            try engine.start()
        } catch {
            errorHint = "Microphone unavailable"
            teardownAudio()
            closeSocket()
            return false
        }

        self.engine = engine
        isRecording = true
        return true
    }

    /// Stop, flush the socket cleanly, and return the final transcript
    /// (whitespace-trimmed; empty means "send nothing").
    func stopAndFinish() async -> String {
        guard isRecording else { return "" }
        stopAudioOnly()

        // Ask Deepgram to flush; wait up to 1 s for the last final result.
        if let socket {
            socket.send(.string(#"{"type":"CloseStream"}"#)) { _ in }
            await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
                drained = continuation
                Task { @MainActor [weak self] in
                    try? await Task.sleep(for: .seconds(1))
                    self?.finishDrain()
                }
            }
        }
        closeSocket()
        deactivateSession()
        isRecording = false

        let transcript = liveTranscript.trimmingCharacters(in: .whitespacesAndNewlines)
        finalizedText = ""
        interimText = ""
        return transcript
    }

    /// Discard everything, send nothing.
    func cancel() {
        stopAudioOnly()
        closeSocket()
        deactivateSession()
        isRecording = false
        finalizedText = ""
        interimText = ""
    }

    // MARK: - Socket

    private func openSocket(apiKey: String) {
        var components = URLComponents(string: "wss://api.deepgram.com/v1/listen")!
        components.queryItems = [
            URLQueryItem(name: "model", value: "nova-3"),
            URLQueryItem(name: "encoding", value: "linear16"),
            URLQueryItem(name: "sample_rate", value: "16000"),
            URLQueryItem(name: "channels", value: "1"),
            URLQueryItem(name: "interim_results", value: "true"),
            URLQueryItem(name: "smart_format", value: "true"),
            URLQueryItem(name: "endpointing", value: "300"),
        ]
        var request = URLRequest(url: components.url!)
        request.setValue("Token \(apiKey)", forHTTPHeaderField: "Authorization")
        let socket = URLSession.shared.webSocketTask(with: request)
        self.socket = socket
        socket.resume()

        receiveTask = Task { [weak self] in
            while let self, let socket = self.socket {
                do {
                    let message = try await socket.receive()
                    self.handle(message)
                } catch {
                    // Socket failure (network drop, 401). Keep captured text.
                    if self.isRecording {
                        self.errorHint = "Transcription failed"
                        self.stopAudioOnly()
                        self.isRecording = false
                        self.deactivateSession()
                    }
                    self.finishDrain()
                    self.socket = nil
                    return
                }
            }
        }
    }

    private func handle(_ message: URLSessionWebSocketTask.Message) {
        guard case .string(let text) = message,
              let data = text.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return
        }
        guard object["type"] as? String == "Results",
              let channel = object["channel"] as? [String: Any],
              let alternatives = channel["alternatives"] as? [[String: Any]],
              let transcript = alternatives.first?["transcript"] as? String else {
            if object["type"] as? String == "Metadata" {
                // Arrives after CloseStream: the stream is drained.
                finishDrain()
            }
            return
        }
        let isFinal = object["is_final"] as? Bool ?? false
        if isFinal {
            if !transcript.isEmpty {
                finalizedText = finalizedText.isEmpty
                    ? transcript
                    : finalizedText + " " + transcript
            }
            interimText = ""
        } else {
            interimText = transcript
        }
    }

    private func sendAudio(_ data: Data) {
        guard isRecording, let socket else { return }
        socket.send(.data(data)) { _ in }
    }

    private func finishDrain() {
        drained?.resume()
        drained = nil
    }

    // MARK: - Audio plumbing

    private nonisolated static func convert(
        buffer: AVAudioPCMBuffer, converter: AVAudioConverter, to format: AVAudioFormat
    ) -> Data? {
        let ratio = format.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 16
        guard let output = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: capacity) else {
            return nil
        }
        var served = false
        var conversionError: NSError?
        converter.convert(to: output, error: &conversionError) { _, status in
            if served {
                status.pointee = .noDataNow
                return nil
            }
            served = true
            status.pointee = .haveData
            return buffer
        }
        guard conversionError == nil, output.frameLength > 0,
              let channel = output.int16ChannelData else {
            return nil
        }
        return Data(
            bytes: channel[0],
            count: Int(output.frameLength) * MemoryLayout<Int16>.size
        )
    }

    private func stopAudioOnly() {
        engine?.inputNode.removeTap(onBus: 0)
        engine?.stop()
        engine = nil
        converter = nil
        outputFormat = nil
    }

    private func teardownAudio() {
        stopAudioOnly()
        deactivateSession()
    }

    private func deactivateSession() {
        try? AVAudioSession.sharedInstance().setActive(
            false, options: .notifyOthersOnDeactivation
        )
    }

    private func closeSocket() {
        receiveTask?.cancel()
        receiveTask = nil
        socket?.cancel(with: .normalClosure, reason: nil)
        socket = nil
    }

    /// Key check for Settings (STT-only build: no TTS to play "test" with).
    /// Deepgram's token grant endpoint 200s iff the key is valid.
    static func validateKey(_ key: String) async -> Bool {
        var request = URLRequest(url: URL(string: "https://api.deepgram.com/v1/auth/token")!)
        request.setValue("Token \(key)", forHTTPHeaderField: "Authorization")
        request.timeoutInterval = 15
        guard let (_, response) = try? await URLSession.shared.data(for: request),
              let http = response as? HTTPURLResponse else {
            return false
        }
        return (200..<300).contains(http.statusCode)
    }
}
