import AVFoundation
import CoreMotion
import Foundation
import UIKit

struct CameraCaptureMetadata: Codable, Equatable {
    let horizonAngleDegrees: Double
    let verticalAngleDegrees: Double
    let yawAngleDegrees: Double
    let fieldOfViewDegrees: Double
    let motionAvailable: Bool

    enum CodingKeys: String, CodingKey {
        case horizonAngleDegrees = "horizon_angle_degrees"
        case verticalAngleDegrees = "vertical_angle_degrees"
        case yawAngleDegrees = "yaw_angle_degrees"
        case fieldOfViewDegrees = "field_of_view_degrees"
        case motionAvailable = "motion_available"
    }
}

struct CapturedCameraPhoto {
    let data: Data
    let metadata: CameraCaptureMetadata
}

final class CameraController: NSObject, ObservableObject, @unchecked Sendable {
    let session = AVCaptureSession()

    @Published private(set) var isReady = false
    @Published private(set) var isLandscape = false
    @Published private(set) var previewRotationAngle: CGFloat = 0
    @Published private(set) var horizonAngle: Double = 0
    @Published private(set) var verticalAngle: Double = 0
    @Published private(set) var yawAngle: Double = 0
    @Published private(set) var isMotionAvailable = false
    @Published private(set) var errorMessage: String?

    private let photoOutput = AVCapturePhotoOutput()
    private let motionManager = CMMotionManager()
    private let motionQueue: OperationQueue = {
        let queue = OperationQueue()
        queue.name = "com.promotekk.showroomflow.motion"
        queue.qualityOfService = .userInteractive
        return queue
    }()
    private let sessionQueue = DispatchQueue(label: "com.promotekk.showroomflow.camera")
    private var isConfigured = false
    private var photoContinuation: CheckedContinuation<CapturedCameraPhoto, Error>?
    private var pendingCaptureMetadata: CameraCaptureMetadata?
    private var fieldOfViewDegrees: Double = 65
    private var orientationObserver: NSObjectProtocol?

    override init() {
        super.init()
        UIDevice.current.beginGeneratingDeviceOrientationNotifications()
        orientationObserver = NotificationCenter.default.addObserver(
            forName: UIDevice.orientationDidChangeNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            self?.updateOrientation()
        }
        updateOrientation()
        startMotionUpdates()
    }

    deinit {
        if let orientationObserver {
            NotificationCenter.default.removeObserver(orientationObserver)
        }
        UIDevice.current.endGeneratingDeviceOrientationNotifications()
        motionManager.stopDeviceMotionUpdates()
    }

    func start() async {
        if !motionManager.isDeviceMotionActive {
            startMotionUpdates()
        }
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
        motionManager.stopDeviceMotionUpdates()
    }

    func capturePhoto() async throws -> CapturedCameraPhoto {
        guard isReady else { throw CameraError.unavailable }
        guard isLandscape else { throw CameraError.landscapeRequired }
        let captureMetadata = currentCaptureMetadata()
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
                self.pendingCaptureMetadata = captureMetadata
                let settings = AVCapturePhotoSettings(
                    format: [AVVideoCodecKey: AVVideoCodecType.jpeg]
                )
                settings.photoQualityPrioritization = .quality
                self.applyPhotoRotation(for: UIDevice.current.orientation)
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
        fieldOfViewDegrees = Double(device.activeFormat.videoFieldOfView)
        isConfigured = true
    }

    private func updateOrientation() {
        let orientation = UIDevice.current.orientation
        guard orientation == .portrait
                || orientation == .portraitUpsideDown
                || orientation == .landscapeLeft
                || orientation == .landscapeRight else {
            return
        }
        let isLandscape = orientation.isLandscape
        let rotationAngle = rotationAngle(for: orientation)
        DispatchQueue.main.async { [weak self] in
            self?.isLandscape = isLandscape
            if let rotationAngle {
                self?.previewRotationAngle = rotationAngle
            }
        }
        sessionQueue.async { [weak self] in
            self?.applyPhotoRotation(for: orientation)
        }
    }

    private func startMotionUpdates() {
        guard motionManager.isDeviceMotionAvailable, !motionManager.isDeviceMotionActive else {
            return
        }
        isMotionAvailable = true
        motionManager.deviceMotionUpdateInterval = 1.0 / 20.0
        motionManager.startDeviceMotionUpdates(to: motionQueue) { [weak self] motion, _ in
            guard let self, let motion else { return }
            let gravity = motion.gravity
            let horizonRadians = atan2(-gravity.y, abs(gravity.x))
            let verticalRadians = atan2(
                gravity.z,
                hypot(gravity.x, gravity.y)
            )
            let horizon = horizonRadians * 180 / .pi
            let vertical = verticalRadians * 180 / .pi
            let yaw = motion.attitude.yaw * 180 / .pi
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                horizonAngle = (horizonAngle * 0.72) + (horizon * 0.28)
                verticalAngle = (verticalAngle * 0.72) + (vertical * 0.28)
                yawAngle = yaw
            }
        }
    }

    private func currentCaptureMetadata() -> CameraCaptureMetadata {
        CameraCaptureMetadata(
            horizonAngleDegrees: horizonAngle,
            verticalAngleDegrees: verticalAngle,
            yawAngleDegrees: yawAngle,
            fieldOfViewDegrees: fieldOfViewDegrees,
            motionAvailable: isMotionAvailable
        )
    }

    private func applyPhotoRotation(for orientation: UIDeviceOrientation) {
        guard let connection = photoOutput.connection(with: .video) else { return }
        guard let rotationAngle = rotationAngle(for: orientation) else { return }
        guard connection.isVideoRotationAngleSupported(rotationAngle) else { return }
        connection.videoRotationAngle = rotationAngle
    }

    private func rotationAngle(for orientation: UIDeviceOrientation) -> CGFloat? {
        switch orientation {
        case .portrait:
            90
        case .portraitUpsideDown:
            270
        case .landscapeLeft:
            0
        case .landscapeRight:
            180
        default:
            nil
        }
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
        let metadata = pendingCaptureMetadata
        pendingCaptureMetadata = nil
        if let error {
            continuation?.resume(throwing: error)
        } else if let data = photo.fileDataRepresentation(), let metadata {
            continuation?.resume(returning: CapturedCameraPhoto(data: data, metadata: metadata))
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
    case landscapeRequired

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
        case .landscapeRequired:
            "Bitte halten Sie das iPhone für diese Aufnahme quer."
        }
    }
}
