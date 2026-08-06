import INSCoreMedia
import INSCameraServiceSDK
import INSCameraSDK
import ImageIO
import SwiftUI
import UIKit

private enum Insta360PanoramaFormat {
    // Insta360 uses this exact output size for X4 panoramas in the SDK 1.9.2
    // sample. The raw camera file remains untouched and is downloaded in the
    // resolution selected by the X4 itself.
    static let width = 11_904
    static let height = 5_952
}

struct Insta360CaptureView: View {
    @Environment(\.dismiss) private var dismiss
    @StateObject private var camera = Insta360CameraController()

    let onCaptured: (Data) -> Void

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()

            if let renderView = camera.previewRenderView {
                Insta360Preview(renderView: renderView)
                    .ignoresSafeArea()

                if !camera.isConnected {
                    previewStartupOverlay
                }
            } else if camera.isConnecting {
                previewStartupOverlay
            } else if camera.isConnected {
                VStack(spacing: 14) {
                    ProgressView()
                        .tint(.white)
                    Text("Live-Vorschau wird gestartet …")
                        .foregroundStyle(.white)
                }
            } else {
                connectionHelp
            }

            VStack {
                HStack {
                    Button {
                        dismiss()
                    } label: {
                        Label("Schließen", systemImage: "xmark")
                            .padding(.horizontal, 14)
                            .padding(.vertical, 10)
                            .background(.black.opacity(0.65), in: .capsule)
                    }
                    Spacer()
                    connectionBadge
                }
                .foregroundStyle(.white)
                .padding()

                Spacer()

                if camera.isConnected {
                    if let captureStatus = camera.captureStatus {
                        Text(captureStatus)
                            .font(.callout.bold())
                            .foregroundStyle(.white)
                            .padding(.horizontal, 16)
                            .padding(.vertical, 10)
                            .background(.black.opacity(0.72), in: .capsule)
                            .padding(.bottom, 12)
                    }

                    Button {
                        Task {
                            if let data = await camera.capturePhoto() {
                                onCaptured(data)
                            }
                        }
                    } label: {
                        ZStack {
                            Circle()
                                .fill(.white)
                                .frame(width: 78, height: 78)
                            Circle()
                                .stroke(.black, lineWidth: 3)
                                .frame(width: 65, height: 65)
                            if camera.isCapturing {
                                ProgressView().tint(.black)
                            }
                        }
                    }
                    .disabled(camera.isCapturing)
                    .accessibilityLabel("360-Grad-Foto aufnehmen")
                    .padding(.bottom, 28)
                }
            }
        }
        .task { await camera.connect() }
        .alert(
            "Insta360",
            isPresented: Binding(
                get: { camera.errorMessage != nil },
                set: { if !$0 { camera.errorMessage = nil } }
            )
        ) {
            Button("Erneut versuchen") { Task { await camera.connect() } }
            Button("Abbrechen", role: .cancel) {}
        } message: {
            Text(camera.errorMessage ?? "")
        }
        .onDisappear { camera.stop() }
    }

    private var previewStartupOverlay: some View {
        VStack(spacing: 14) {
            ProgressView()
                .tint(.white)
            Text("Verbindung und Live-Vorschau werden aufgebaut …")
                .font(.callout.bold())
                .foregroundStyle(.white)
        }
        .padding(.horizontal, 22)
        .padding(.vertical, 16)
        .background(.black.opacity(0.72), in: .rect(cornerRadius: 18))
    }

    private var connectionHelp: some View {
        VStack(spacing: 18) {
            Image(systemName: "wifi")
                .font(.system(size: 56, weight: .semibold))
                .foregroundStyle(.blue)
            Text("Insta360 X4 verbinden")
                .font(.title2.bold())
            Text(
                "Schalten Sie die Kamera ein und verbinden Sie das iPhone in den "
                    + "WLAN-Einstellungen mit dem WLAN der Insta360. Kehren Sie danach "
                    + "zu ShowroomFlow zurück."
            )
            .multilineTextAlignment(.center)
            .foregroundStyle(.secondary)
            .frame(maxWidth: 430)

            Button("WLAN-Einstellungen öffnen") {
                guard let url = URL(string: UIApplication.openSettingsURLString) else { return }
                UIApplication.shared.open(url)
            }
            .buttonStyle(.borderedProminent)

            Button("Verbindung prüfen") {
                Task { await camera.connect() }
            }
            .buttonStyle(.bordered)
        }
        .padding(28)
        .foregroundStyle(.white)
    }

    private var connectionBadge: some View {
        Label(
            camera.isConnected ? "X4 verbunden" : "Nicht verbunden",
            systemImage: camera.isConnected ? "checkmark.circle.fill" : "wifi.slash"
        )
        .font(.caption.bold())
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(.black.opacity(0.65), in: .capsule)
    }
}

private final class Insta360PreviewDelegate: NSObject,
    INSCameraPreviewPlayerDelegate,
    @unchecked Sendable
{
    private let stateLock = NSLock()
    private var fallbackMediaOffset: String?
    private let onFirstFrame: @Sendable () -> Void

    init(onFirstFrame: @escaping @Sendable () -> Void) {
        self.onFirstFrame = onFirstFrame
        super.init()
    }

    func updateMediaOffset(_ mediaOffset: String?) {
        stateLock.lock()
        fallbackMediaOffset = mediaOffset
        stateLock.unlock()
    }

    func offset(toPlay player: INSCameraPreviewPlayer) -> String? {
        if let currentOffset = player.renderView.render.offset,
           !currentOffset.isEmpty {
            return currentOffset
        }

        stateLock.lock()
        let mediaOffset = fallbackMediaOffset
        stateLock.unlock()
        return mediaOffset
    }

    func player(
        _ player: INSCameraPreviewPlayer,
        willRenderImage image: INSPlayerImage
    ) -> Bool {
        onFirstFrame()
        return true
    }

    func player(
        _ player: INSCameraPreviewPlayer,
        onPlayeErrorMaxCount count: Int
    ) {
        print("[ShowroomFlow/Insta360] Vorschau-Decoder meldet zu viele Fehler (\(count)).")
    }
}

@MainActor
final class Insta360CameraController: NSObject, ObservableObject {
    @Published var isConnected = false
    @Published private(set) var isConnecting = false
    @Published var isCapturing = false
    @Published var errorMessage: String?
    @Published private(set) var previewRenderView: UIView?
    @Published private(set) var captureStatus: String?

    fileprivate let mediaSession = INSCameraMediaSession()
    private var previewPlayer: INSCameraPreviewPlayer?
    private var observesCameraState = false
    private var pendingDisconnectTask: Task<Void, Never>?
    private var wantsConnection = false
    private var previewMediaOffset: String?
    private var receivedPreviewFrame = false
    private lazy var previewDelegate = Insta360PreviewDelegate { [weak self] in
        Task { @MainActor [weak self] in
            self?.receivedPreviewFrame = true
        }
    }

    override init() {
        super.init()
        startObservingCameraState()
    }

    deinit {
        pendingDisconnectTask?.cancel()
        if observesCameraState {
            INSCameraManager.socket().removeObserver(
                self,
                forKeyPath: #keyPath(INSCameraManager.cameraState)
            )
        }
    }

    func connect() async {
        wantsConnection = true
        guard !isConnecting else { return }
        pendingDisconnectTask?.cancel()
        pendingDisconnectTask = nil
        isConnecting = true
        defer { isConnecting = false }

        errorMessage = nil
        let socket = INSCameraManager.socket()
        if socket.cameraState != .connected {
            print("[ShowroomFlow/Insta360] Kamera-Socket wird aufgebaut (Status: \(socket.cameraState.rawValue)).")
            socket.setup()
        } else {
            print("[ShowroomFlow/Insta360] Vorhandene Kamera-Verbindung wird weiterverwendet.")
        }

        for _ in 0..<48 {
            guard wantsConnection else { return }
            if socket.cameraState == .connected {
                do {
                    try await startPreview()
                    try await waitForFirstPreviewFrame()
                    return
                } catch {
                    tearDownPreview()
                    isConnected = false
                    errorMessage = "Die Verbindung zur Insta360 steht, aber die Live-Vorschau konnte nicht gestartet werden: \(error.localizedDescription)"
                    return
                }
            }
            try? await Task.sleep(for: .milliseconds(250))
        }

        isConnected = false
        errorMessage = "Keine Insta360-Kamera gefunden. Bitte zuerst das iPhone mit dem WLAN der Kamera verbinden."
    }

    func capturePhoto() async -> Data? {
        guard isConnected,
              mediaSession.running,
              INSCameraManager.socket().cameraState == .connected else {
            errorMessage = Insta360CaptureError.previewConnectionUnstable.localizedDescription
            return nil
        }
        isCapturing = true
        errorMessage = nil
        defer {
            isCapturing = false
            captureStatus = nil
        }

        let identifier = UUID().uuidString
        let sourceURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("showroomflow-insta360-source-\(identifier).jpg")
        let panoramaURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("showroomflow-insta360-panorama-\(identifier).jpg")
        defer {
            try? FileManager.default.removeItem(at: sourceURL)
            try? FileManager.default.removeItem(at: panoramaURL)
        }

        do {
            captureStatus = "360°-Foto wird aufgenommen …"
            let uri = try await takePictureURI()

            captureStatus = "Originaldatei wird in voller Auflösung übertragen …"
            try await downloadResource(uri: uri, destination: sourceURL)

            captureStatus = "2:1-Panorama wird in maximaler Auflösung erstellt …"
            try await Self.stitchPanorama(sourceURL: sourceURL, panoramaURL: panoramaURL)
            try Self.validatePanorama(at: panoramaURL)

            let panoramaData = try Data(contentsOf: panoramaURL, options: .mappedIfSafe)
            print(
                "[ShowroomFlow/Insta360] Panorama erfolgreich exportiert "
                    + "(\(Insta360PanoramaFormat.width) × \(Insta360PanoramaFormat.height), "
                    + "\(panoramaData.count) Bytes)."
            )
            return panoramaData
        } catch {
            print("[ShowroomFlow/Insta360] Aufnahme oder Export fehlgeschlagen: \(error.localizedDescription)")
            errorMessage = error.localizedDescription
            return nil
        }
    }

    private func waitForFirstPreviewFrame() async throws {
        for _ in 0..<40 {
            guard wantsConnection,
                  INSCameraManager.socket().cameraState == .connected,
                  mediaSession.running else {
                throw Insta360CaptureError.previewConnectionUnstable
            }
            if receivedPreviewFrame {
                isConnected = true
                print("[ShowroomFlow/Insta360] Erstes Vorschaubild wurde gerendert; Kamera ist einsatzbereit.")
                return
            }
            try? await Task.sleep(for: .milliseconds(250))
        }

        throw Insta360CaptureError.previewDidNotRender
    }

    private func takePictureURI() async throws -> String {
        try await withCheckedThrowingContinuation { continuation in
            INSCameraManager.shared().commandManager.takePicture(with: nil) { error, photo in
                if let error {
                    continuation.resume(throwing: error)
                } else if let uri = photo?.uri {
                    continuation.resume(returning: uri)
                } else {
                    continuation.resume(throwing: Insta360CaptureError.missingPhotoURI)
                }
            }
        }
    }

    private func downloadResource(uri: String, destination: URL) async throws {
        try await withCheckedThrowingContinuation {
            (continuation: CheckedContinuation<Void, Error>) in
            INSCameraManager.socket().commandManager.fetchResource(
                withURI: uri,
                toLocalFile: destination,
                progress: { [weak self] progress in
                    Task { @MainActor in
                        guard let progress else { return }
                        self?.captureStatus = "Originaldatei wird übertragen: \(Int(progress.fractionCompleted * 100)) %"
                    }
                }
            ) { error in
                if let error {
                    continuation.resume(throwing: error)
                } else {
                    continuation.resume()
                }
            }
        }
    }

    private nonisolated static func stitchPanorama(
        sourceURL: URL,
        panoramaURL: URL
    ) async throws {
        try await Task.detached(priority: .userInitiated) {
            let exporter = INSExportImageSimplify()
            exporter.width = Int32(Insta360PanoramaFormat.width)
            exporter.height = Int32(Insta360PanoramaFormat.height)
            exporter.opticalFlowType = .disflow
            let exportError = exporter.exportImage(
                withInputUrl: sourceURL,
                outputUrl: panoramaURL
            ) as NSError
            if exportError.code != 0 {
                throw exportError
            }
        }.value
    }

    private nonisolated static func validatePanorama(at url: URL) throws {
        guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
              let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil)
                as? [CFString: Any],
              let width = properties[kCGImagePropertyPixelWidth] as? Int,
              let height = properties[kCGImagePropertyPixelHeight] as? Int,
              width == Insta360PanoramaFormat.width,
              height == Insta360PanoramaFormat.height else {
            throw Insta360CaptureError.invalidPanorama
        }
    }

    fileprivate func startPreview() async throws {
        if previewPlayer != nil, mediaSession.running {
            return
        }

        tearDownPreview()
        receivedPreviewFrame = false

        // The Insta360 renderer creates its OpenGL framebuffer during init. A
        // zero-sized frame makes that setup fail before SwiftUI has laid out
        // the representable view.
        let screenBounds = UIScreen.main.bounds
        let previewFrame = CGRect(
            x: 0,
            y: 0,
            width: max(screenBounds.width, 1),
            height: max(screenBounds.height, 1)
        )
        let player = INSCameraPreviewPlayer(
            frame: previewFrame,
            renderType: .sphericalPanoRender
        )
        previewDelegate.updateMediaOffset(
            INSCameraManager.socket().currentCamera?.settings?.mediaOffsetV3
                ?? INSCameraManager.shared().currentCamera?.settings?.mediaOffsetV3
        )
        player.delegate = previewDelegate
        player.play(withGyroTimestampAdjust: 34)
        previewPlayer = player
        previewRenderView = player.renderView
        mediaSession.plug(player)

        // Diese Werte entsprechen der von Insta360 für X3/X4 verwendeten
        // Beispielkonfiguration. Ohne erwartete Auflösungen und expliziten
        // Nebenstream baut die Kamera den Vorschaukanal kurz auf und beendet
        // ihn anschließend wieder.
        mediaSession.expectedVideoResolution = INSVideoResolution1024x512x15
        mediaSession.expectedVideoResolutionSecondary = INSVideoResolution960x480x30
        mediaSession.previewStreamType = INSPreviewStreamTypeWithValue(1)

        // Die X4 kann ihren Vorschaukanal je nach aktiver Kameraeinstellung
        // als H.264 oder H.265 liefern. Wird hier der SDK-Standard statt des
        // tatsächlich ausgehandelten Codecs verwendet, erscheint kurz ein
        // Bild und die Kamera beendet anschließend den Medienkanal. Auch der
        // Crop-Offset muss vor dem Start auf die aktuelle Fenstergröße der
        // Kamera umgerechnet werden.
        try await configurePreviewFromCamera()

        try await withCheckedThrowingContinuation {
            (continuation: CheckedContinuation<Void, Error>) in
            mediaSession.startRunning { error in
                if let error {
                    continuation.resume(throwing: error)
                } else {
                    continuation.resume()
                }
            }
        }
    }

    private func configurePreviewFromCamera() async throws {
        var optionTypes = [
            NSNumber(value: INSCameraOptionsType.videoEncode.rawValue),
            NSNumber(value: INSCameraOptionsType.windowCropInfo.rawValue),
        ]

        // Insta360 fragt diesen Wert im Referenzprojekt ausschließlich bei
        // der ONE R ab. Die X4 kann die zusätzliche Optionsabfrage ablehnen;
        // dadurch wird ihr kurz zuvor gestarteter Medienkanal wieder beendet.
        let requiresGyroTimestamp = INSCameraManager.socket().currentCamera?.cameraType
            == kInsta360CameraNameOneR
        if requiresGyroTimestamp {
            optionTypes.append(
                NSNumber(value: INSCameraOptionsType.gyroTimestamp.rawValue)
            )
        }

        try await withCheckedThrowingContinuation {
            (continuation: CheckedContinuation<Void, Error>) in
            INSCameraManager.shared().commandManager.getOptionsWithTypes(optionTypes) {
                [weak self] error, options, _ in
                Task { @MainActor in
                    guard let self else {
                        continuation.resume(throwing: Insta360CaptureError.previewConfigurationUnavailable)
                        return
                    }
                    guard let options else {
                        if let error {
                            print("[ShowroomFlow/Insta360] Vorschauoptionen konnten nicht gelesen werden: \(error.localizedDescription)")
                        }
                        continuation.resume(throwing: error ?? Insta360CaptureError.previewConfigurationUnavailable)
                        return
                    }

                    self.mediaSession.videoStreamEncode = options.videoEncode

                    // Nur die ONE R liefert diesen Wert in der offiziellen
                    // Preview-Sequenz. Bei der X4 bleibt der feste Startwert
                    // bestehen, damit keine nicht angefragte Option verwendet
                    // wird.
                    if requiresGyroTimestamp, options.gyroTimestamp != 0 {
                        self.previewPlayer?.play(
                            withGyroTimestampAdjust: options.gyroTimestamp
                        )
                    }

                    // Die aktive WLAN-Kamera lebt am Socket-Manager. Der
                    // allgemeine Manager kann während des Verbindungsaufbaus
                    // noch eine veraltete oder leere Kameraeinstellung halten.
                    var mediaOffset = INSCameraManager.socket().currentCamera?
                        .settings?.mediaOffsetV3
                    if let crop = options.windowCropInfo,
                       let sourceOffset = mediaOffset,
                       !sourceOffset.isEmpty {
                        mediaOffset = INSOffsetCalculator.cropOffset(
                            sourceOffset,
                            srcWidth: Int32(crop.srcWidth),
                            srcHeight: Int32(crop.srcHeight),
                            dstWidth: Int32(crop.dstWidth),
                            dstHeight: Int32(crop.dstHeight),
                            xOffset: crop.cropOffsetX,
                            yOffset: crop.cropOffsetY
                        )
                    }

                    self.previewMediaOffset = mediaOffset
                    self.previewDelegate.updateMediaOffset(mediaOffset)
                    if let mediaOffset, !mediaOffset.isEmpty {
                        self.previewPlayer?.play(withOffset: mediaOffset)
                    }

                    print(
                        "[ShowroomFlow/Insta360] Vorschau konfiguriert "
                            + "(Codec: \(options.videoEncode.rawValue), "
                            + "Gyro: \(options.gyroTimestamp), "
                            + "Crop: \(options.windowCropInfo != nil), "
                            + "Offset: \(mediaOffset?.isEmpty == false))."
                    )
                    continuation.resume()
                }
            }
        }
    }

    func stop() {
        wantsConnection = false
        pendingDisconnectTask?.cancel()
        pendingDisconnectTask = nil
        tearDownPreview()
        isConnecting = false
        isConnected = false
    }

    private func tearDownPreview() {
        if mediaSession.running {
            mediaSession.stopRunning { _ in }
        }
        mediaSession.unplugAll()
        previewPlayer?.delegate = nil
        previewPlayer?.closeVirtualGimbal()
        previewRenderView = nil
        previewPlayer = nil
        previewMediaOffset = nil
        previewDelegate.updateMediaOffset(nil)
        receivedPreviewFrame = false
    }

    private func startObservingCameraState() {
        guard !observesCameraState else { return }
        INSCameraManager.socket().addObserver(
            self,
            forKeyPath: #keyPath(INSCameraManager.cameraState),
            options: [.new],
            context: nil
        )
        observesCameraState = true
    }

    nonisolated override func observeValue(
        forKeyPath keyPath: String?,
        of object: Any?,
        change: [NSKeyValueChangeKey: Any]?,
        context: UnsafeMutableRawPointer?
    ) {
        guard keyPath == #keyPath(INSCameraManager.cameraState),
              let rawValue = change?[.newKey] as? UInt,
              let state = INSCameraState(rawValue: rawValue) else {
            return
        }

        Task { @MainActor [weak self] in
            guard let self else { return }

            switch state {
            case .connected:
                self.pendingDisconnectTask?.cancel()
                self.pendingDisconnectTask = nil
                if self.wantsConnection,
                   !self.isConnecting,
                   (self.previewPlayer == nil || !self.mediaSession.running) {
                    Task { await self.connect() }
                }

            case .found, .synchronized:
                // Das sind Zwischenzustände der Aushandlung, keine stabile
                // Kamera-Verbindung. Sie dürfen weder die UI auf „verbunden“
                // setzen noch einen laufenden Player sofort zerstören.
                self.pendingDisconnectTask?.cancel()
                self.pendingDisconnectTask = nil

            case .connectFailed, .noConnection:
                self.scheduleDisconnectVerification()

            @unknown default:
                break
            }
        }
    }

    private func scheduleDisconnectVerification() {
        guard isConnected || previewPlayer != nil else { return }

        pendingDisconnectTask?.cancel()
        pendingDisconnectTask = Task { @MainActor [weak self] in
            // The X4 briefly reports `noConnection` while switching its
            // command and preview sockets. Only treat a sustained terminal
            // state as a real loss of the camera WLAN.
            try? await Task.sleep(for: .seconds(3))
            guard !Task.isCancelled, let self else { return }

            let currentState = INSCameraManager.socket().cameraState
            guard currentState == .connectFailed || currentState == .noConnection else {
                return
            }

            self.tearDownPreview()
            self.isConnected = false
            self.errorMessage = "Die Verbindung zur Insta360 wurde unterbrochen. Bitte prüfen Sie das Kamera-WLAN und verbinden Sie die Kamera erneut."
            self.pendingDisconnectTask = nil
        }
    }
}

private struct Insta360Preview: UIViewRepresentable {
    let renderView: UIView

    func makeUIView(context: Context) -> UIView {
        renderView.backgroundColor = .black
        renderView.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        return renderView
    }

    func updateUIView(_ uiView: UIView, context: Context) {}
}

private enum Insta360CaptureError: LocalizedError {
    case previewConnectionUnstable
    case previewDidNotRender
    case previewConfigurationUnavailable
    case missingPhotoURI
    case invalidPanorama

    var errorDescription: String? {
        switch self {
        case .previewConnectionUnstable:
            "Die Insta360 X4 hat den Vorschaustream direkt nach dem Verbindungsaufbau wieder beendet. Bitte Kamera-WLAN prüfen und erneut verbinden."
        case .previewDidNotRender:
            "Die Insta360 X4 ist erreichbar, hat aber kein Vorschaubild geliefert. Bitte Kamera eingeschaltet lassen und die Verbindung erneut versuchen."
        case .previewConfigurationUnavailable:
            "Die Vorschaukonfiguration der Insta360 X4 konnte nicht gelesen werden. Bitte die Kamera erneut verbinden."
        case .missingPhotoURI:
            "Die Kamera hat keine Bilddatei zurückgegeben."
        case .invalidPanorama:
            "Das Insta360-Foto konnte nicht als hochauflösendes 2:1-Panorama erstellt werden."
        }
    }
}
