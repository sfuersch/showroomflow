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
    @State private var isCompletingCapture = false
    @State private var showCompletionConfirmation = false
    @State private var isRetakingExistingPhoto = false
    @State private var errorMessage: String?

    let job: VehicleJob
    let loadCaptureSession: (UUID) async throws -> CaptureSession
    let uploadCapturedPhoto: (UUID, UUID, Data) async throws -> CapturedPhoto
    let completeCapture: (UUID) async throws -> VehicleJob

    var body: some View {
        NavigationStack {
            Group {
                if isLoading {
                    VStack(spacing: 14) {
                        ShowroomFlowBrandMark(size: 76)
                        ProgressView()
                            .controlSize(.large)
                            .tint(.white)
                        Text("Fotoablauf wird geladen …")
                            .font(.subheadline.weight(.medium))
                            .foregroundStyle(.white.opacity(0.8))
                    }
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
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(captureBackground.ignoresSafeArea())
            .toolbar(.hidden, for: .navigationBar)
        }
        .task { await prepare() }
        .onDisappear { camera.stop() }
        .confirmationDialog(
            "Aufnahme abschließen?",
            isPresented: $showCompletionConfirmation,
            titleVisibility: .visible
        ) {
            Button("Verbindlich abschließen") {
                Task { await finishCapture() }
            }
        } message: {
            Text(
                "Danach können keine weiteren Fotos aufgenommen werden. "
                    + "Sobald alle Bilder verarbeitet sind, startet der automatische Export."
            )
        }
    }

    @ViewBuilder
    private func captureContent(_ data: CaptureSession) -> some View {
        if data.job.captureCompletedAt != nil {
            completedCaptureView
        } else {
            let step = data.captureSteps[currentIndex]
            GeometryReader { proxy in
                if !camera.isLandscape && camera.errorMessage == nil {
                    ZStack {
                        captureBackground.ignoresSafeArea()
                        landscapeHint
                    }
                } else {
                    HStack(spacing: 12) {
                        stepRail(data)
                            .frame(width: 108)
                        viewfinder(step: step, data: data)
                            .frame(maxWidth: .infinity, maxHeight: .infinity)
                        controlRail(step: step, data: data)
                            .frame(width: 136)
                    }
                    .padding(10)
                    .frame(width: proxy.size.width, height: proxy.size.height)
                    .background(captureBackground.ignoresSafeArea())
                }
            }
        }
    }

    private var completedCaptureView: some View {
        VStack(spacing: 18) {
            Image(systemName: "checkmark.seal.fill")
                .font(.system(size: 64))
                .foregroundStyle(.mint)
            Text("Aufnahme abgeschlossen")
                .font(.title.bold())
            Text(
                job.autoExport
                    ? "Der Export startet automatisch, sobald alle Bilder verarbeitet sind."
                    : "Die Bilder stehen jetzt zur Prüfung und manuellen Weiterverarbeitung bereit."
            )
            .multilineTextAlignment(.center)
            .foregroundStyle(.white.opacity(0.75))
            Button("Zur Fahrzeugübersicht", systemImage: "chevron.left") {
                dismiss()
            }
            .buttonStyle(.borderedProminent)
            .tint(.mint)
        }
        .foregroundStyle(.white)
        .padding(30)
        .frame(maxWidth: 480)
        .background(.ultraThinMaterial, in: .rect(cornerRadius: 28))
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(30)
    }

    private var captureBackground: LinearGradient {
        LinearGradient(
            colors: [
                Color(red: 0.055, green: 0.065, blue: 0.11),
                Color(red: 0.10, green: 0.09, blue: 0.22),
                Color(red: 0.045, green: 0.05, blue: 0.085),
            ],
            startPoint: .topLeading,
            endPoint: .bottomTrailing
        )
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
            .clipShape(.rect(cornerRadius: 20))
            .overlay {
                RoundedRectangle(cornerRadius: 20)
                    .stroke(
                        LinearGradient(
                            colors: [.white.opacity(0.32), .indigo.opacity(0.55)],
                            startPoint: .topLeading,
                            endPoint: .bottomTrailing
                        ),
                        lineWidth: 1.5
                    )
            }
            .shadow(color: .black.opacity(0.42), radius: 18, y: 10)
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
                .background(.ultraThinMaterial, in: .rect(cornerRadius: 12))
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
                        .background(.ultraThinMaterial, in: .capsule)
                        .padding(10)
                }
            }
            .overlay(alignment: .bottomLeading) {
                Text("\(job.vin) · V\(job.version)")
                    .font(.caption.monospaced().bold())
                    .foregroundStyle(.white)
                    .padding(8)
                    .background(.ultraThinMaterial, in: .rect(cornerRadius: 9))
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
        CachedAsyncImage(url: photo.displayImageURL) { phase in
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
            Label(
                photo.isOptimized ? "Optimiertes Bild" : "Aufnahme vorhanden",
                systemImage: photo.isOptimized ? "sparkles" : "checkmark.circle.fill"
            )
                .font(.caption.bold())
                .foregroundStyle(.white)
                .padding(9)
                .background(
                    photo.isOptimized ? Color.indigo.opacity(0.9) : Color.green.opacity(0.85),
                    in: .capsule
                )
                .padding(10)
        }
    }

    private var landscapeHint: some View {
        ZStack(alignment: .topLeading) {
            Button {
                dismiss()
            } label: {
                Label("Zurück", systemImage: "chevron.left")
                    .font(.headline)
                    .foregroundStyle(.white)
                    .padding(.horizontal, 16)
                    .frame(height: 48)
                    .background(.ultraThinMaterial, in: .capsule)
            }
            .disabled(isUploading)
            .padding(20)

            VStack(spacing: 18) {
                ShowroomFlowBrandMark(size: 82)
                Image(systemName: "iphone.gen3.landscape")
                    .font(.system(size: 58, weight: .semibold))
                    .symbolEffect(.pulse)
                    .foregroundStyle(.mint)
                VStack(spacing: 7) {
                    Text("Bitte ins Querformat drehen")
                        .font(.title2.bold())
                    Text("Die Bedienelemente werden eingeblendet, sobald das Gerät quer gehalten wird.")
                        .font(.subheadline)
                        .foregroundStyle(.white.opacity(0.72))
                }
                Text("\(job.vin) · V\(job.version)")
                    .font(.caption.monospaced().bold())
                    .foregroundStyle(.white.opacity(0.62))
            }
            .multilineTextAlignment(.center)
            .foregroundStyle(.white)
            .padding(28)
            .frame(maxWidth: 440)
            .background(.ultraThinMaterial, in: .rect(cornerRadius: 28))
            .overlay {
                RoundedRectangle(cornerRadius: 28)
                    .stroke(Color.white.opacity(0.12))
            }
            .shadow(color: .black.opacity(0.28), radius: 24, y: 12)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .padding(30)
        }
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
        .background(.ultraThinMaterial, in: .rect(cornerRadius: 20))
        .overlay {
            RoundedRectangle(cornerRadius: 20)
                .stroke(Color.white.opacity(0.1))
        }
    }

    @ViewBuilder
    private func stepThumbnail(_ step: ConfiguredCaptureStep) -> some View {
        if let photo = existingPhoto(for: step.id) {
            CachedAsyncImage(url: photo.displayThumbnailURL) { phase in
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
            ShowroomFlowBrandMark(size: 38)
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
            Button {
                showCompletionConfirmation = true
            } label: {
                if isCompletingCapture {
                    ProgressView()
                        .tint(.white)
                        .frame(maxWidth: .infinity)
                } else {
                    Label("Abschließen", systemImage: "checkmark.seal.fill")
                        .font(.caption.bold())
                        .multilineTextAlignment(.center)
                        .frame(maxWidth: .infinity)
                }
            }
            .buttonStyle(.borderedProminent)
            .tint(.mint)
            .disabled(!requiredPhotosComplete || isUploading || isCompletingCapture)
            .accessibilityHint(
                requiredPhotosComplete
                    ? "Beendet die Aufnahme verbindlich"
                    : "Zuerst müssen alle Pflichtfotos aufgenommen werden"
            )
        }
        .padding(12)
        .background(.ultraThinMaterial, in: .rect(cornerRadius: 20))
        .overlay {
            RoundedRectangle(cornerRadius: 20)
                .stroke(Color.white.opacity(0.1))
        }
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
        .background(Color.indigo.opacity(0.28), in: .rect(cornerRadius: 12))
        .overlay {
            RoundedRectangle(cornerRadius: 12)
                .stroke(Color.white.opacity(0.1))
        }
    }

    private var completedCount: Int {
        guard let captureSession else { return 0 }
        let activeStepIDs = Set(captureSession.captureSteps.map(\.id))
        return captureSession.photos.count { activeStepIDs.contains($0.captureStepID) }
    }

    private var requiredPhotosComplete: Bool {
        guard let captureSession else { return false }
        let photographedStepIDs = Set(captureSession.photos.map(\.captureStepID))
        return captureSession.captureSteps
            .filter(\.isRequired)
            .allSatisfy { photographedStepIDs.contains($0.id) }
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

    private func finishCapture() async {
        isCompletingCapture = true
        errorMessage = nil
        defer { isCompletingCapture = false }
        do {
            _ = try await completeCapture(job.id)
            dismiss()
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
