import AVFoundation
import Foundation

final class CameraController: NSObject, ObservableObject, @unchecked Sendable {
    let session = AVCaptureSession()

    @Published private(set) var isReady = false
    @Published private(set) var errorMessage: String?

    private let photoOutput = AVCapturePhotoOutput()
    private let sessionQueue = DispatchQueue(label: "com.promotekk.showroomflow.camera")
    private var isConfigured = false
    private var photoContinuation: CheckedContinuation<Data, Error>?

    func start() async {
        guard await cameraAccessGranted() else {
            await MainActor.run {
                errorMessage = "Bitte erlauben Sie ShowroomFlow den Kamerazugriff in den Einstellungen."
            }
            return
        }

        do {
            try await withCheckedThrowingContinuation {
                (continuation: CheckedContinuation<Void, Error>) in
                sessionQueue.async { [weak self] in
                    guard let self else {
                        continuation.resume(throwing: CameraError.unavailable)
                        return
                    }
                    do {
                        try self.configureIfNeeded()
                        if !self.session.isRunning {
                            self.session.startRunning()
                        }
                        continuation.resume()
                    } catch {
                        continuation.resume(throwing: error)
                    }
                }
            }
            await MainActor.run { isReady = true }
        } catch {
            await MainActor.run { errorMessage = error.localizedDescription }
        }
    }

    func stop() {
        sessionQueue.async { [weak self] in
            guard let self, self.session.isRunning else { return }
            self.session.stopRunning()
        }
    }

    func capturePhoto() async throws -> Data {
        guard isReady else { throw CameraError.unavailable }
        return try await withCheckedThrowingContinuation { continuation in
            sessionQueue.async { [weak self] in
                guard let self else {
                    continuation.resume(throwing: CameraError.unavailable)
                    return
                }
                guard self.photoContinuation == nil else {
                    continuation.resume(throwing: CameraError.captureInProgress)
                    return
                }
                self.photoContinuation = continuation
                let settings = AVCapturePhotoSettings(
                    format: [AVVideoCodecKey: AVVideoCodecType.jpeg]
                )
                settings.photoQualityPrioritization = .quality
                self.photoOutput.capturePhoto(with: settings, delegate: self)
            }
        }
    }

    private func cameraAccessGranted() async -> Bool {
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized:
            return true
        case .notDetermined:
            return await withCheckedContinuation { continuation in
                AVCaptureDevice.requestAccess(for: .video) { granted in
                    continuation.resume(returning: granted)
                }
            }
        default:
            return false
        }
    }

    private func configureIfNeeded() throws {
        guard !isConfigured else { return }
        session.beginConfiguration()
        defer { session.commitConfiguration() }
        session.sessionPreset = .photo

        guard let device = AVCaptureDevice.default(
            .builtInWideAngleCamera,
            for: .video,
            position: .back
        ) else {
            throw CameraError.unavailable
        }
        let input = try AVCaptureDeviceInput(device: device)
        guard session.canAddInput(input), session.canAddOutput(photoOutput) else {
            throw CameraError.configurationFailed
        }
        session.addInput(input)
        session.addOutput(photoOutput)
        photoOutput.maxPhotoQualityPrioritization = .quality
        isConfigured = true
    }
}

extension CameraController: AVCapturePhotoCaptureDelegate {
    func photoOutput(
        _ output: AVCapturePhotoOutput,
        didFinishProcessingPhoto photo: AVCapturePhoto,
        error: Error?
    ) {
        let continuation = photoContinuation
        photoContinuation = nil
        if let error {
            continuation?.resume(throwing: error)
        } else if let data = photo.fileDataRepresentation() {
            continuation?.resume(returning: data)
        } else {
            continuation?.resume(throwing: CameraError.invalidPhoto)
        }
    }
}

private enum CameraError: LocalizedError {
    case unavailable
    case configurationFailed
    case captureInProgress
    case invalidPhoto

    var errorDescription: String? {
        switch self {
        case .unavailable:
            "Auf diesem Gerät ist keine Kamera verfügbar."
        case .configurationFailed:
            "Die Kamera konnte nicht eingerichtet werden."
        case .captureInProgress:
            "Eine Aufnahme wird bereits verarbeitet."
        case .invalidPhoto:
            "Das Foto konnte nicht gelesen werden."
        }
    }
}
