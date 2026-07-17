import SwiftUI
import UIKit

struct CaptureFlowView: View {
    @Environment(\.dismiss) private var dismiss
    @StateObject private var camera = CameraController()
    @State private var captureSession: CaptureSession?
    @State private var currentIndex = 0
    @State private var pendingPhotoData: Data?
    @State private var isLoading = true
    @State private var isCapturing = false
    @State private var isUploading = false
    @State private var isRetakingExistingPhoto = false
    @State private var errorMessage: String?

    let job: VehicleJob
    let loadCaptureSession: (UUID) async throws -> CaptureSession
    let uploadCapturedPhoto: (UUID, UUID, Data) async throws -> CapturedPhoto

    var body: some View {
        NavigationStack {
            Group {
                if isLoading {
                    ProgressView("Fotoablauf wird geladen …")
                } else if let captureSession, !captureSession.captureSteps.isEmpty {
                    captureContent(captureSession)
                } else {
                    ContentUnavailableView(
                        "Kein Fotoablauf",
                        systemImage: "camera.metering.none",
                        description: Text("Legen Sie zuerst Fotopositionen im Backend an.")
                    )
                }
            }
            .background(.black)
            .toolbar(.hidden, for: .navigationBar)
        }
        .task { await prepare() }
        .onDisappear { camera.stop() }
    }

    @ViewBuilder
    private func captureContent(_ data: CaptureSession) -> some View {
        let step = data.captureSteps[currentIndex]
        GeometryReader { proxy in
            if !camera.isLandscape && camera.errorMessage == nil {
                ZStack {
                    Color.black.ignoresSafeArea()
                    landscapeHint
                }
            } else {
                HStack(spacing: 10) {
                    stepRail(data)
                        .frame(width: 104)
                    viewfinder(step: step, data: data)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                    controlRail(step: step, data: data)
                        .frame(width: 128)
                }
                .padding(8)
                .frame(width: proxy.size.width, height: proxy.size.height)
                .background(Color(white: 0.12).ignoresSafeArea())
            }
        }
    }

    private func viewfinder(
        step: ConfiguredCaptureStep,
        data: CaptureSession
    ) -> some View {
        GeometryReader { proxy in
            let width = min(proxy.size.width, proxy.size.height * 4 / 3)
            let height = width * 3 / 4
            ZStack {
                Color.black
                if let pendingPhotoData, let image = UIImage(data: pendingPhotoData) {
                    Image(uiImage: image)
                        .resizable()
                        .scaledToFit()
                } else if let photo = existingPhoto(for: step.id),
                          !isRetakingExistingPhoto {
                    existingPhotoView(photo, width: width, height: height)
                } else if camera.isReady {
                    CameraPreview(
                        session: camera.session,
                        rotationAngle: camera.previewRotationAngle
                    )
                    silhouette(for: step)
                } else if let cameraError = camera.errorMessage {
                    ContentUnavailableView(
                        "Kamera nicht verfügbar",
                        systemImage: "camera.fill",
                        description: Text(cameraError)
                    )
                    .foregroundStyle(.white)
                } else {
                    ProgressView("Kamera wird vorbereitet …")
                        .tint(.white)
                        .foregroundStyle(.white)
                }
            }
            .frame(width: width, height: height)
            .clipShape(.rect(cornerRadius: 16))
            .overlay(alignment: .top) {
                VStack(spacing: 5) {
                    Text(step.name)
                        .font(.headline)
                    Text("\(currentIndex + 1) von \(data.captureSteps.count)")
                        .font(.caption.bold())
                }
                .foregroundStyle(.white)
                .padding(.horizontal, 16)
                .padding(.vertical, 9)
                .background(.black.opacity(0.64), in: .rect(cornerRadius: 12))
                .padding(10)
            }
            .overlay(alignment: .bottom) {
                if !step.instruction.isEmpty {
                    Text(step.instruction)
                        .font(.subheadline.bold())
                        .multilineTextAlignment(.center)
                        .foregroundStyle(.white)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 8)
                        .background(.black.opacity(0.64), in: .capsule)
                        .padding(10)
                }
            }
            .overlay(alignment: .bottomLeading) {
                Text("\(job.vin) · V\(job.version)")
                    .font(.caption.monospaced().bold())
                    .foregroundStyle(.white)
                    .padding(8)
                    .background(.black.opacity(0.64), in: .rect(cornerRadius: 9))
                    .padding(10)
            }
            .overlay(alignment: .bottomTrailing) {
                if let errorMessage {
                    Text(errorMessage)
                        .font(.caption)
                        .foregroundStyle(.white)
                        .padding(9)
                        .background(.red.opacity(0.88), in: .rect(cornerRadius: 9))
                        .padding(10)
                }
            }
            .position(x: proxy.size.width / 2, y: proxy.size.height / 2)
        }
    }

    private func existingPhotoView(
        _ photo: CapturedPhoto,
        width: CGFloat,
        height: CGFloat
    ) -> some View {
        AsyncImage(url: photo.imageURL) { phase in
            switch phase {
            case let .success(image):
                image
                    .resizable()
                    .scaledToFit()
            case .failure:
                ContentUnavailableView(
                    "Foto nicht verfügbar",
                    systemImage: "photo.badge.exclamationmark",
                    description: Text("Das vorhandene Foto konnte nicht geladen werden.")
                )
                .foregroundStyle(.white)
            default:
                ProgressView("Vorhandenes Foto wird geladen …")
                    .tint(.white)
                    .foregroundStyle(.white)
            }
        }
        .frame(width: width, height: height)
        .background(.black)
        .clipShape(.rect(cornerRadius: 18))
        .overlay(alignment: .topTrailing) {
            Label("Aufnahme vorhanden", systemImage: "checkmark.circle.fill")
                .font(.caption.bold())
                .foregroundStyle(.white)
                .padding(9)
                .background(.green.opacity(0.85), in: .capsule)
                .padding(10)
        }
    }

    private var landscapeHint: some View {
        VStack(spacing: 12) {
            Image(systemName: "rotate.right")
                .font(.system(size: 44, weight: .semibold))
            Text("Für Fahrzeugfotos bitte quer halten")
                .font(.headline)
            Text("Die Aufnahme wird erst im Querformat freigegeben.")
                .font(.subheadline)
        }
        .multilineTextAlignment(.center)
        .foregroundStyle(.white)
        .padding(24)
        .background(.black.opacity(0.78), in: .rect(cornerRadius: 20))
        .padding(32)
    }

    @ViewBuilder
    private func silhouette(for step: ConfiguredCaptureStep) -> some View {
        if let url = step.silhouetteURL {
            AsyncImage(url: url) { phase in
                if case let .success(image) = phase {
                    image
                        .resizable()
                        .scaledToFit()
                        .padding(24)
                        .opacity(0.72)
                        .allowsHitTesting(false)
                }
            }
        } else {
            VehicleSilhouetteGuide(stepName: step.name)
                .allowsHitTesting(false)
        }
    }

    private func stepRail(_ data: CaptureSession) -> some View {
        VStack(spacing: 8) {
            Button {
                dismiss()
            } label: {
                Image(systemName: "chevron.left")
                    .font(.system(size: 26, weight: .bold))
                    .frame(width: 48, height: 42)
            }
            .foregroundStyle(.white)
            .disabled(isUploading)

            ScrollView(.vertical, showsIndicators: false) {
                LazyVStack(spacing: 12) {
                ForEach(Array(data.captureSteps.enumerated()), id: \.element.id) { index, step in
                    Button {
                        guard pendingPhotoData == nil, !isUploading else { return }
                        currentIndex = index
                        isRetakingExistingPhoto = false
                        errorMessage = nil
                    } label: {
                        ZStack {
                            stepThumbnail(step)
                                .frame(width: 70, height: 70)
                                .clipShape(.circle)
                            Circle()
                                .stroke(
                                    index == currentIndex ? Color.mint : .white.opacity(0.5),
                                    lineWidth: index == currentIndex ? 4 : 2
                                )
                            Text("\(step.captureOrder)")
                                .font(.caption2.bold())
                                .foregroundStyle(.white)
                                .padding(5)
                                .background(.black.opacity(0.76), in: .circle)
                                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottomLeading)
                            if isCompleted(step.id) {
                                Image(systemName: "checkmark.circle.fill")
                                    .font(.title2)
                                    .symbolRenderingMode(.palette)
                                    .foregroundStyle(.mint, .black)
                                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topTrailing)
                            }
                        }
                        .frame(width: 76, height: 76)
                    }
                    .accessibilityLabel("\(step.captureOrder). \(step.name)")
                    .accessibilityValue(isCompleted(step.id) ? "Aufgenommen" : "Offen")
                }
            }
            }

            Text("\(completedCount)/\(data.captureSteps.count)")
                .font(.caption.bold())
                .foregroundStyle(.white)
                .padding(.horizontal, 9)
                .padding(.vertical, 5)
                .background(.black.opacity(0.5), in: .capsule)
        }
        .padding(.vertical, 6)
        .background(Color(white: 0.19), in: .rect(cornerRadius: 18))
    }

    @ViewBuilder
    private func stepThumbnail(_ step: ConfiguredCaptureStep) -> some View {
        if let photo = existingPhoto(for: step.id) {
            AsyncImage(url: photo.imageURL) { phase in
                if case let .success(image) = phase {
                    image.resizable().scaledToFill()
                } else {
                    thumbnailPlaceholder
                }
            }
        } else if let url = step.silhouetteURL {
            AsyncImage(url: url) { phase in
                if case let .success(image) = phase {
                    image
                        .resizable()
                        .scaledToFit()
                        .padding(8)
                } else {
                    thumbnailPlaceholder
                }
            }
            .background(Color(white: 0.28))
        } else {
            thumbnailPlaceholder
        }
    }

    private var thumbnailPlaceholder: some View {
        ZStack {
            Color(white: 0.28)
            Image(systemName: "car.side.fill")
                .font(.system(size: 31))
                .foregroundStyle(.white.opacity(0.8))
        }
    }

    private func controlRail(
        step: ConfiguredCaptureStep,
        data: CaptureSession
    ) -> some View {
        VStack(spacing: 12) {
            MotionLevelIndicator(
                horizonAngle: camera.horizonAngle,
                verticalAngle: camera.verticalAngle,
                isAvailable: camera.isMotionAvailable
            )
            Spacer()
            controls(step: step)
            Spacer()
            Text(step.isRequired ? "Pflichtfoto" : "Optional")
                .font(.caption.bold())
                .foregroundStyle(.white.opacity(0.8))
            ProgressView(value: Double(completedCount), total: Double(data.captureSteps.count))
                .tint(.mint)
        }
        .padding(12)
        .background(Color(white: 0.19), in: .rect(cornerRadius: 18))
    }

    @ViewBuilder
    private func controls(step: ConfiguredCaptureStep) -> some View {
        if let pendingPhotoData {
            VStack(spacing: 14) {
                Button {
                    self.pendingPhotoData = nil
                    errorMessage = nil
                } label: {
                    railButtonLabel("Wiederholen", systemImage: "arrow.counterclockwise")
                }
                .foregroundStyle(.white)
                .disabled(isUploading)

                Button {
                    Task { await usePhoto(pendingPhotoData, step: step) }
                } label: {
                    if isUploading {
                        ProgressView().tint(.white)
                    } else {
                        railButtonLabel("Verwenden", systemImage: "checkmark")
                    }
                }
                .foregroundStyle(.mint)
                .disabled(isUploading)
            }
        } else if existingPhoto(for: step.id) != nil && !isRetakingExistingPhoto {
            Button {
                isRetakingExistingPhoto = true
                errorMessage = nil
            } label: {
                railButtonLabel("Neu aufnehmen", systemImage: "camera.rotate")
            }
            .foregroundStyle(.mint)
        } else {
            Button {
                Task { await takePhoto() }
            } label: {
                ZStack {
                    Circle().fill(.white).frame(width: 76, height: 76)
                    Circle().stroke(.black, lineWidth: 3).frame(width: 64, height: 64)
                    if isCapturing { ProgressView().tint(.black) }
                }
            }
            .accessibilityLabel("Foto aufnehmen")
            .disabled(!camera.isReady || !camera.isLandscape || isCapturing)
        }
    }

    private func railButtonLabel(_ title: String, systemImage: String) -> some View {
        VStack(spacing: 6) {
            Image(systemName: systemImage)
                .font(.title2.bold())
            Text(title)
                .font(.caption.bold())
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 9)
        .background(.black.opacity(0.42), in: .rect(cornerRadius: 12))
    }

    private var completedCount: Int {
        guard let captureSession else { return 0 }
        let activeStepIDs = Set(captureSession.captureSteps.map(\.id))
        return captureSession.photos.count { activeStepIDs.contains($0.captureStepID) }
    }

    private func isCompleted(_ stepID: UUID) -> Bool {
        captureSession?.photos.contains(where: { $0.captureStepID == stepID }) == true
    }

    private func existingPhoto(for stepID: UUID) -> CapturedPhoto? {
        captureSession?.photos.first(where: { $0.captureStepID == stepID })
    }

    private func prepare() async {
        async let cameraPreparation: Void = camera.start()
        do {
            let loadedSession = try await loadCaptureSession(job.id)
            captureSession = loadedSession
            currentIndex = loadedSession.captureSteps.firstIndex {
                step in !loadedSession.photos.contains { $0.captureStepID == step.id }
            } ?? 0
        } catch {
            errorMessage = error.localizedDescription
        }
        await cameraPreparation
        isLoading = false
    }

    private func takePhoto() async {
        isCapturing = true
        errorMessage = nil
        defer { isCapturing = false }
        do {
            pendingPhotoData = try await camera.capturePhoto()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func usePhoto(_ photoData: Data, step: ConfiguredCaptureStep) async {
        isUploading = true
        errorMessage = nil
        defer { isUploading = false }
        do {
            let uploadedPhoto = try await uploadCapturedPhoto(job.id, step.id, photoData)
            guard var updatedSession = captureSession else { return }
            updatedSession = CaptureSession(
                job: updatedSession.job,
                captureSteps: updatedSession.captureSteps,
                photos: updatedSession.photos.filter { $0.captureStepID != step.id } + [uploadedPhoto]
            )
            captureSession = updatedSession
            pendingPhotoData = nil
            isRetakingExistingPhoto = false
            moveToNextIncompleteStep(in: updatedSession)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func moveToNextIncompleteStep(in data: CaptureSession) {
        let laterSteps = data.captureSteps.indices.filter { $0 > currentIndex }
        let earlierSteps = data.captureSteps.indices.filter { $0 <= currentIndex }
        if let nextIndex = (laterSteps + earlierSteps).first(where: { index in
            !data.photos.contains { $0.captureStepID == data.captureSteps[index].id }
        }) {
            currentIndex = nextIndex
            isRetakingExistingPhoto = false
        }
    }
}

private struct MotionLevelIndicator: View {
    let horizonAngle: Double
    let verticalAngle: Double
    let isAvailable: Bool

    private var isLevel: Bool {
        abs(horizonAngle) <= 2 && abs(verticalAngle) <= 3
    }

    private var dotOffset: CGSize {
        CGSize(
            width: max(-26, min(26, horizonAngle / 12 * 26)),
            height: max(-26, min(26, verticalAngle / 12 * 26))
        )
    }

    var body: some View {
        VStack(spacing: 7) {
            Text("Ausrichtung")
                .font(.caption.bold())
            ZStack {
                Circle()
                    .fill(Color(white: 0.36))
                Path { path in
                    path.move(to: CGPoint(x: 44, y: 7))
                    path.addLine(to: CGPoint(x: 44, y: 81))
                    path.move(to: CGPoint(x: 7, y: 44))
                    path.addLine(to: CGPoint(x: 81, y: 44))
                }
                .stroke(.white.opacity(0.82), lineWidth: 1.5)
                Circle()
                    .stroke(.white.opacity(0.82), lineWidth: 1.5)
                    .frame(width: 28, height: 28)
                Circle()
                    .fill(isLevel ? .mint : .orange)
                    .frame(width: 18, height: 18)
                    .offset(dotOffset)
                    .animation(.linear(duration: 0.08), value: dotOffset)
            }
            .frame(width: 88, height: 88)
            .opacity(isAvailable ? 1 : 0.5)

            if isAvailable {
                HStack(spacing: 8) {
                    Label(String(format: "%.1f°", horizonAngle), systemImage: "arrow.left.and.right")
                    Label(String(format: "%.1f°", verticalAngle), systemImage: "arrow.up.and.down")
                }
                .font(.system(size: 9, weight: .semibold))
                .lineLimit(1)
            } else {
                Text("Sensor nicht verfügbar")
                    .font(.system(size: 9, weight: .semibold))
            }
        }
        .foregroundStyle(.white)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Geräteausrichtung")
        .accessibilityValue(
            isAvailable
                ? "Horizontal \(String(format: "%.1f", horizonAngle)) Grad, vertikal \(String(format: "%.1f", verticalAngle)) Grad"
                : "Sensor nicht verfügbar"
        )
    }
}

private struct VehicleSilhouetteGuide: View {
    let stepName: String

    var body: some View {
        ZStack(alignment: .bottom) {
            RoundedRectangle(cornerRadius: 28)
                .stroke(
                    .white.opacity(0.72),
                    style: StrokeStyle(lineWidth: 4, dash: [16, 10])
                )
            Image(systemName: "car.fill")
                .resizable()
                .scaledToFit()
                .foregroundStyle(.white.opacity(0.52))
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .padding(.horizontal, 44)
                .padding(.vertical, 54)
                .shadow(color: .black.opacity(0.7), radius: 8, y: 5)

            Label("Fahrzeug vollständig in diesem Rahmen", systemImage: "viewfinder")
                .font(.caption.bold())
                .foregroundStyle(.white)
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .background(.black.opacity(0.7), in: .capsule)
                .padding(.bottom, 10)
            Text(stepName)
                .font(.caption)
                .foregroundStyle(.white.opacity(0.82))
                .padding(.bottom, -12)
        }
    }
}
