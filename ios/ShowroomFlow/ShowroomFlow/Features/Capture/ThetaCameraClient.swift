import Foundation
import NetworkExtension
import UIKit

struct ThetaCameraInfo: Decodable, Sendable {
    let model: String
    let serialNumber: String
}

enum ThetaCameraError: LocalizedError {
    case invalidResponse
    case httpStatus(Int, String?)
    case camera(String)
    case missingCommandID
    case missingFileURL
    case captureTimedOut
    case invalidImage

    var errorDescription: String? {
        switch self {
        case .invalidResponse:
            return "Die THETA hat eine ungültige Antwort gesendet."
        case let .httpStatus(status, message):
            if let message, !message.isEmpty {
                return "THETA-Fehler (HTTP \(status)): \(message)"
            }
            return "THETA-Fehler (HTTP \(status))."
        case let .camera(message):
            return "THETA-Fehler: \(message)"
        case .missingCommandID:
            return "Die THETA hat keine Vorgangs-ID zurückgegeben."
        case .missingFileURL:
            return "Die THETA hat keine Bildadresse zurückgegeben."
        case .captureTimedOut:
            return "Die THETA-Aufnahme hat zu lange gedauert."
        case .invalidImage:
            return "Die THETA hat keine gültige JPEG-Datei geliefert."
        }
    }
}

actor ThetaCameraClient {
    private struct CommandResponse: Decodable {
        struct Results: Decodable {
            let fileUrl: String?
        }

        struct CameraError: Decodable {
            let message: String
        }

        let id: String?
        let state: String?
        let results: Results?
        let error: CameraError?
    }

    private let baseURL = URL(string: "http://192.168.1.1")!
    private let session: URLSession

    init() {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 15
        configuration.timeoutIntervalForResource = 120
        configuration.waitsForConnectivity = false
        session = URLSession(configuration: configuration)
    }

    func cameraInfo() async throws -> ThetaCameraInfo {
        let data = try await request(path: "/osc/info", method: "GET")
        return try JSONDecoder().decode(ThetaCameraInfo.self, from: data)
    }

    func capturePhoto() async throws -> Data {
        try? await setImageCaptureMode()

        let command = try await executeCommand(
            name: "camera.takePicture",
            parameters: [:]
        )
        let completed = try await waitUntilCompleted(command)

        guard
            let fileURLString = completed.results?.fileUrl,
            let fileURL = URL(string: fileURLString)
        else {
            throw ThetaCameraError.missingFileURL
        }

        var request = URLRequest(url: fileURL)
        request.timeoutInterval = 120
        let (data, response) = try await session.data(for: request)
        try validate(response: response, data: data)

        guard data.count > 4, data[0] == 0xFF, data[1] == 0xD8 else {
            throw ThetaCameraError.invalidImage
        }
        return data
    }

    private func setImageCaptureMode() async throws {
        _ = try await executeCommand(
            name: "camera.setOptions",
            parameters: [
                "options": [
                    "captureMode": "image"
                ]
            ]
        )
    }

    private func executeCommand(
        name: String,
        parameters: [String: Any]
    ) async throws -> CommandResponse {
        var body: [String: Any] = ["name": name]
        if !parameters.isEmpty {
            body["parameters"] = parameters
        }

        let data = try await request(
            path: "/osc/commands/execute",
            method: "POST",
            jsonBody: body
        )
        let response = try JSONDecoder().decode(CommandResponse.self, from: data)
        if let error = response.error {
            throw ThetaCameraError.camera(error.message)
        }
        return response
    }

    private func waitUntilCompleted(_ initial: CommandResponse) async throws -> CommandResponse {
        if initial.state == "done" {
            return initial
        }
        guard let commandID = initial.id else {
            throw ThetaCameraError.missingCommandID
        }

        for _ in 0..<120 {
            try await Task.sleep(for: .milliseconds(750))
            let data = try await request(
                path: "/osc/commands/status",
                method: "POST",
                jsonBody: ["id": commandID]
            )
            let response = try JSONDecoder().decode(CommandResponse.self, from: data)
            if let error = response.error {
                throw ThetaCameraError.camera(error.message)
            }
            if response.state == "done" {
                return response
            }
        }
        throw ThetaCameraError.captureTimedOut
    }

    private func request(
        path: String,
        method: String,
        jsonBody: [String: Any]? = nil
    ) async throws -> Data {
        var request = URLRequest(url: baseURL.appending(path: path))
        request.httpMethod = method
        request.setValue("application/json;charset=utf-8", forHTTPHeaderField: "Content-Type")
        if let jsonBody {
            request.httpBody = try JSONSerialization.data(withJSONObject: jsonBody)
        }

        let (data, response) = try await session.data(for: request)
        try validate(response: response, data: data)
        return data
    }

    private func validate(response: URLResponse, data: Data) throws {
        guard let httpResponse = response as? HTTPURLResponse else {
            throw ThetaCameraError.invalidResponse
        }
        guard (200..<300).contains(httpResponse.statusCode) else {
            let message = String(data: data, encoding: .utf8)
            throw ThetaCameraError.httpStatus(httpResponse.statusCode, message)
        }
    }
}

private final class ThetaLivePreviewStream: NSObject, URLSessionDataDelegate, @unchecked Sendable {
    private static let jpegStart = Data([0xFF, 0xD8])
    private static let jpegEnd = Data([0xFF, 0xD9])
    private static let maximumBufferSize = 8 * 1_024 * 1_024

    private let onFrame: @Sendable (Data) -> Void
    private let onFailure: @Sendable (Error) -> Void
    private var buffer = Data()
    private var session: URLSession?
    private var task: URLSessionDataTask?
    private var wasCancelled = false

    init(
        onFrame: @escaping @Sendable (Data) -> Void,
        onFailure: @escaping @Sendable (Error) -> Void
    ) {
        self.onFrame = onFrame
        self.onFailure = onFailure
    }

    func start() {
        stop()
        wasCancelled = false
        buffer.removeAll(keepingCapacity: true)

        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 30
        configuration.timeoutIntervalForResource = 60 * 60
        configuration.waitsForConnectivity = false
        let session = URLSession(
            configuration: configuration,
            delegate: self,
            delegateQueue: nil
        )

        var request = URLRequest(
            url: URL(string: "http://192.168.1.1/osc/commands/execute")!
        )
        request.httpMethod = "POST"
        request.setValue(
            "application/json;charset=utf-8",
            forHTTPHeaderField: "Content-Type"
        )
        request.httpBody = try? JSONSerialization.data(
            withJSONObject: [
                "name": "camera.getLivePreview",
                "parameters": [:],
            ]
        )

        self.session = session
        let task = session.dataTask(with: request)
        self.task = task
        task.resume()
    }

    func stop() {
        wasCancelled = true
        task?.cancel()
        session?.invalidateAndCancel()
        task = nil
        session = nil
        buffer.removeAll(keepingCapacity: false)
    }

    func urlSession(
        _ session: URLSession,
        dataTask: URLSessionDataTask,
        didReceive response: URLResponse,
        completionHandler: @escaping (URLSession.ResponseDisposition) -> Void
    ) {
        guard let response = response as? HTTPURLResponse else {
            completionHandler(.cancel)
            onFailure(ThetaCameraError.invalidResponse)
            return
        }
        guard (200..<300).contains(response.statusCode) else {
            completionHandler(.cancel)
            onFailure(ThetaCameraError.httpStatus(response.statusCode, nil))
            return
        }
        completionHandler(.allow)
    }

    func urlSession(
        _ session: URLSession,
        dataTask: URLSessionDataTask,
        didReceive data: Data
    ) {
        buffer.append(data)
        extractJPEGFrames()
    }

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        didCompleteWithError error: Error?
    ) {
        guard !wasCancelled, let error else { return }
        onFailure(error)
    }

    private func extractJPEGFrames() {
        while let startRange = buffer.range(of: Self.jpegStart) {
            let searchStart = startRange.lowerBound
            guard let endRange = buffer.range(
                of: Self.jpegEnd,
                in: searchStart..<buffer.endIndex
            ) else {
                discardMultipartHeaders(before: searchStart)
                limitBufferSize()
                return
            }

            let frameEnd = endRange.upperBound
            let frame = buffer.subdata(in: searchStart..<frameEnd)
            buffer.removeSubrange(buffer.startIndex..<frameEnd)
            onFrame(frame)
        }
        limitBufferSize()
    }

    private func discardMultipartHeaders(before frameStart: Data.Index) {
        guard frameStart > buffer.startIndex else { return }
        buffer.removeSubrange(buffer.startIndex..<frameStart)
    }

    private func limitBufferSize() {
        guard buffer.count > Self.maximumBufferSize else { return }
        buffer = Data(buffer.suffix(Self.maximumBufferSize / 4))
    }
}

@MainActor
final class ThetaCameraController: ObservableObject {
    @Published private(set) var cameraInfo: ThetaCameraInfo?
    @Published private(set) var isBusy = false
    @Published private(set) var statusMessage = "THETA einschalten und WLAN an der Kamera aktivieren."
    @Published private(set) var previewImage: UIImage?
    @Published private(set) var isPreviewRunning = false
    @Published private(set) var previewErrorMessage: String?
    @Published var errorMessage: String?

    private let client = ThetaCameraClient()
    private var previewStream: ThetaLivePreviewStream?

    func connectToWiFi(ssid: String, password: String) async {
        let trimmedSSID = ssid.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedSSID.isEmpty else {
            errorMessage = "Bitte die WLAN-SSID der THETA eingeben."
            return
        }

        isBusy = true
        errorMessage = nil
        statusMessage = "iPhone wird mit \(trimmedSSID) verbunden …"
        defer { isBusy = false }

        do {
            let configuration: NEHotspotConfiguration
            if password.isEmpty {
                configuration = NEHotspotConfiguration(ssid: trimmedSSID)
            } else {
                configuration = NEHotspotConfiguration(
                    ssid: trimmedSSID,
                    passphrase: password,
                    isWEP: false
                )
            }
            configuration.joinOnce = true

            try await apply(configuration)
            try await Task.sleep(for: .seconds(2))
            try await checkConnectionInternal()
        } catch {
            errorMessage = readableConnectionError(error)
            statusMessage = "Keine Verbindung zur THETA."
        }
    }

    func checkConnection() async {
        isBusy = true
        errorMessage = nil
        statusMessage = "THETA-Verbindung wird geprüft …"
        defer { isBusy = false }

        do {
            try await checkConnectionInternal()
        } catch {
            errorMessage = readableConnectionError(error)
            statusMessage = "Keine Verbindung zur THETA."
        }
    }

    func capturePhoto() async -> Data? {
        isBusy = true
        errorMessage = nil
        stopPreview()
        statusMessage = "360°-Foto wird aufgenommen und übertragen …"
        defer { isBusy = false }

        do {
            try await Task.sleep(for: .milliseconds(250))
            let data = try await client.capturePhoto()
            statusMessage = "360°-Foto wurde von der THETA übernommen."
            return data
        } catch {
            errorMessage = error.localizedDescription
            statusMessage = "Die THETA-Aufnahme ist fehlgeschlagen."
            startPreview()
            return nil
        }
    }

    func restartPreview() {
        guard cameraInfo != nil, !isBusy else { return }
        startPreview()
    }

    func stopPreview() {
        previewStream?.stop()
        previewStream = nil
        isPreviewRunning = false
    }

    private func checkConnectionInternal() async throws {
        let info = try await client.cameraInfo()
        cameraInfo = info
        statusMessage = "\(info.model) · \(info.serialNumber) ist verbunden."
        startPreview()
    }

    private func startPreview() {
        stopPreview()
        previewImage = nil
        previewErrorMessage = nil
        isPreviewRunning = true

        let stream = ThetaLivePreviewStream(
            onFrame: { [weak self] data in
                guard let image = UIImage(data: data) else { return }
                Task { @MainActor [weak self] in
                    self?.previewImage = image
                    self?.previewErrorMessage = nil
                }
            },
            onFailure: { [weak self] error in
                Task { @MainActor [weak self] in
                    guard let self else { return }
                    self.isPreviewRunning = false
                    self.previewErrorMessage =
                        "Die Live-Vorschau wurde unterbrochen: \(error.localizedDescription)"
                }
            }
        )
        previewStream = stream
        stream.start()
    }

    private func apply(_ configuration: NEHotspotConfiguration) async throws {
        try await withCheckedThrowingContinuation { continuation in
            NEHotspotConfigurationManager.shared.apply(configuration) { error in
                if let nsError = error as NSError?,
                   nsError.domain == NEHotspotConfigurationErrorDomain,
                   nsError.code == NEHotspotConfigurationError.alreadyAssociated.rawValue {
                    continuation.resume()
                } else if let error {
                    continuation.resume(throwing: error)
                } else {
                    continuation.resume()
                }
            }
        }
    }

    private func readableConnectionError(_ error: Error) -> String {
        if error is URLError {
            return "Die THETA ist unter 192.168.1.1 nicht erreichbar. Bitte prüfen, ob das iPhone mit dem WLAN der Kamera verbunden ist."
        }
        return error.localizedDescription
    }
}
