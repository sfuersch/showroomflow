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
            .navigationTitle("\(job.vin) · V\(job.version)")
            .navigationBarTitleDisplayMode(.inline)
            .toolbarColorScheme(.dark, for: .navigationBar)
            .toolbarBackground(.black, for: .navigationBar)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Schließen") { dismiss() }
                        .disabled(isUploading)
                }
            }
        }
        .task { await prepare() }
        .onDisappear { camera.stop() }
    }

    @ViewBuilder
    private func captureContent(_ data: CaptureSession) -> some View {
        let step = data.captureSteps[currentIndex]
        ZStack {
            Color.black.ignoresSafeArea()
            if let pendingPhotoData, let image = UIImage(data: pendingPhotoData) {
                Image(uiImage: image)
                    .resizable()
                    .scaledToFit()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if camera.isReady {
                CameraPreview(session: camera.session)
                    .ignoresSafeArea(edges: .bottom)
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

            VStack(spacing: 0) {
                instructionPanel(step: step, data: data)
                Spacer()
                if let errorMessage {
                    Text(errorMessage)
                        .font(.footnote)
                        .foregroundStyle(.white)
                        .padding(10)
                        .background(.red.opacity(0.86), in: .rect(cornerRadius: 10))
                        .padding(.horizontal)
                }
                stepSelector(data)
                controls(step: step)
            }
        }
    }

    private func instructionPanel(
        step: ConfiguredCaptureStep,
        data: CaptureSession
    ) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("\(currentIndex + 1) von \(data.captureSteps.count)")
                    .font(.caption.bold())
                Spacer()
                Text(step.isRequired ? "Pflichtfoto" : "Optional")
                    .font(.caption.bold())
            }
            ProgressView(value: Double(completedCount), total: Double(data.captureSteps.count))
                .tint(.green)
            Text(step.name)
                .font(.title2.bold())
            if !step.instruction.isEmpty {
                Text(step.instruction)
                    .font(.subheadline)
            }
        }
        .foregroundStyle(.white)
        .padding()
        .background(.black.opacity(0.68))
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
            RoundedRectangle(cornerRadius: 28)
                .stroke(.white.opacity(0.58), style: StrokeStyle(lineWidth: 3, dash: [14, 10]))
                .padding(.horizontal, 26)
                .padding(.vertical, 120)
                .allowsHitTesting(false)
        }
    }

    private func stepSelector(_ data: CaptureSession) -> some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 9) {
                ForEach(Array(data.captureSteps.enumerated()), id: \.element.id) { index, step in
                    Button {
                        guard pendingPhotoData == nil, !isUploading else { return }
                        currentIndex = index
                        errorMessage = nil
                    } label: {
                        HStack(spacing: 5) {
                            Image(systemName: isCompleted(step.id) ? "checkmark.circle.fill" : "circle")
                            Text("\(step.captureOrder)")
                        }
                        .font(.caption.bold())
                        .foregroundStyle(index == currentIndex ? .black : .white)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 8)
                        .background(index == currentIndex ? .white : .black.opacity(0.58))
                        .clipShape(.capsule)
                    }
                }
            }
            .padding(.horizontal)
        }
        .padding(.vertical, 8)
        .background(.black.opacity(0.42))
    }

    @ViewBuilder
    private func controls(step: ConfiguredCaptureStep) -> some View {
        if let pendingPhotoData {
            HStack(spacing: 18) {
                Button("Wiederholen", systemImage: "arrow.counterclockwise") {
                    self.pendingPhotoData = nil
                    errorMessage = nil
                }
                .buttonStyle(.bordered)
                .disabled(isUploading)

                Button {
                    Task { await usePhoto(pendingPhotoData, step: step) }
                } label: {
                    if isUploading {
                        ProgressView().tint(.white)
                    } else {
                        Label("Foto verwenden", systemImage: "checkmark")
                    }
                }
                .buttonStyle(.borderedProminent)
                .disabled(isUploading)
            }
            .padding()
            .frame(maxWidth: .infinity)
            .background(.black.opacity(0.78))
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
            .disabled(!camera.isReady || isCapturing)
            .padding(.vertical, 16)
            .frame(maxWidth: .infinity)
            .background(.black.opacity(0.78))
        }
    }

    private var completedCount: Int {
        guard let captureSession else { return 0 }
        let activeStepIDs = Set(captureSession.captureSteps.map(\.id))
        return captureSession.photos.count { activeStepIDs.contains($0.captureStepID) }
    }

    private func isCompleted(_ stepID: UUID) -> Bool {
        captureSession?.photos.contains(where: { $0.captureStepID == stepID }) == true
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
        }
    }
}
