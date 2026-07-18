import SwiftUI

struct JobListView: View {
    @State private var jobs: [VehicleJob] = []
    @State private var isCreatingJob = false
    @State private var captureJob: VehicleJob?
    @State private var isLoading = true
    @State private var errorMessage: String?

    let loadJobs: () async throws -> [VehicleJob]
    let loadLocations: () async throws -> [LocationSummary]
    let loadConfiguration: (UUID) async throws -> AppConfiguration
    let createJob: (UUID, String, UUID, String, UUID?) async throws -> VehicleJob
    let loadCaptureSession: (UUID) async throws -> CaptureSession
    let uploadCapturedPhoto: (UUID, UUID, Data) async throws -> CapturedPhoto
    let onLogout: () -> Void

    var body: some View {
        NavigationStack {
            Group {
                if isLoading && jobs.isEmpty {
                    VStack(spacing: 14) {
                        ProgressView()
                            .controlSize(.large)
                        Text("Fahrzeuge werden geladen …")
                            .foregroundStyle(.secondary)
                    }
                } else if jobs.isEmpty {
                    ContentUnavailableView {
                        Label("Noch keine Fahrzeuge", systemImage: "car.side")
                    } description: {
                        Text(errorMessage ?? "Erstellen Sie den ersten Fotoauftrag.")
                    } actions: {
                        Button("Ersten Auftrag anlegen", systemImage: "plus") {
                            isCreatingJob = true
                        }
                        .buttonStyle(.borderedProminent)
                    }
                } else {
                    ScrollView {
                        LazyVStack(spacing: 12) {
                            if let errorMessage {
                                Label(errorMessage, systemImage: "wifi.exclamationmark")
                                    .font(.footnote)
                                    .foregroundStyle(.red)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .padding(13)
                                    .background(Color.red.opacity(0.08), in: .rect(cornerRadius: 13))
                            }

                            ForEach(jobs) { job in
                                jobCard(job)
                            }
                        }
                        .padding(16)
                    }
                    .refreshable { await reload() }
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(Color(.systemGroupedBackground))
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button(action: onLogout) {
                        Image(systemName: "rectangle.portrait.and.arrow.right")
                    }
                    .accessibilityLabel("Abmelden")
                }
                ToolbarItem(placement: .principal) {
                    ShowroomFlowCompactHeader(subtitle: "Fahrzeuge")
                }
                ToolbarItem(placement: .primaryAction) {
                    Button {
                        isCreatingJob = true
                    } label: {
                        Image(systemName: "plus")
                    }
                    .accessibilityLabel("Neuer Auftrag")
                }
            }
            .sheet(isPresented: $isCreatingJob) {
                NewJobView(
                    loadLocations: loadLocations,
                    loadConfiguration: loadConfiguration,
                    createJob: createJob,
                    onCreated: { job in jobs.insert(job, at: 0) }
                )
            }
            .fullScreenCover(item: $captureJob, onDismiss: {
                Task { await reload() }
            }) { job in
                CaptureFlowView(
                    job: job,
                    loadCaptureSession: loadCaptureSession,
                    uploadCapturedPhoto: uploadCapturedPhoto
                )
            }
            .task { await reload() }
        }
    }

    private func jobCard(_ job: VehicleJob) -> some View {
        Button {
            captureJob = job
        } label: {
            HStack(spacing: 14) {
                jobThumbnail(job)

                VStack(alignment: .leading, spacing: 5) {
                    HStack(spacing: 8) {
                        Text(job.brand)
                            .font(.headline)
                        Text("V\(job.version)")
                            .font(.caption2.weight(.bold))
                            .foregroundStyle(.secondary)
                            .padding(.horizontal, 7)
                            .padding(.vertical, 3)
                            .background(Color.secondary.opacity(0.1), in: .capsule)
                    }
                    Text(job.vin)
                        .font(.subheadline.monospaced())
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                    Label(job.localizedStatus, systemImage: job.statusIcon)
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(job.statusColor)
                }

                Spacer(minLength: 8)

                Image(systemName: "camera.fill")
                    .font(.body.weight(.semibold))
                    .foregroundStyle(.white)
                    .frame(width: 38, height: 38)
                    .background(.indigo, in: .circle)
            }
            .padding(15)
            .background(.background, in: .rect(cornerRadius: 18))
            .overlay {
                RoundedRectangle(cornerRadius: 18)
                    .stroke(Color.primary.opacity(0.06))
            }
            .shadow(color: Color.black.opacity(0.045), radius: 10, y: 5)
            .contentShape(.rect)
        }
        .buttonStyle(.plain)
    }

    @ViewBuilder
    private func jobThumbnail(_ job: VehicleJob) -> some View {
        if let thumbnailURL = job.thumbnailURL {
            CachedAsyncImage(url: thumbnailURL) { phase in
                if case let .success(image) = phase {
                    image
                        .resizable()
                        .scaledToFill()
                } else {
                    thumbnailPlaceholder
                }
            }
            .frame(width: 76, height: 57)
            .clipShape(.rect(cornerRadius: 12))
            .overlay {
                RoundedRectangle(cornerRadius: 12)
                    .stroke(Color.primary.opacity(0.08))
            }
        } else {
            thumbnailPlaceholder
                .frame(width: 76, height: 57)
                .clipShape(.rect(cornerRadius: 12))
        }
    }

    private var thumbnailPlaceholder: some View {
        ZStack {
            Color.indigo.opacity(0.1)
            Image(systemName: "car.side.fill")
                .font(.title3)
                .foregroundStyle(.indigo)
        }
    }

    private func reload() async {
        isLoading = true
        defer { isLoading = false }
        do {
            jobs = try await loadJobs()
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

private extension VehicleJob {
    var localizedStatus: String {
        switch status {
        case "draft": "Entwurf"
        case "capturing": "Aufnahme läuft"
        case "uploading": "Upload läuft"
        case "processing": "Verarbeitung läuft"
        case "review_required": "Prüfung erforderlich"
        case "exporting": "Export läuft"
        case "completed": "Abgeschlossen"
        case "failed": "Fehlgeschlagen"
        default: status
        }
    }

    var statusIcon: String {
        switch status {
        case "completed": "checkmark.circle.fill"
        case "failed", "review_required": "exclamationmark.circle.fill"
        case "processing", "uploading", "exporting": "arrow.triangle.2.circlepath"
        case "capturing": "camera.fill"
        default: "circle.dashed"
        }
    }

    var statusColor: Color {
        switch status {
        case "completed": .green
        case "failed", "review_required": .red
        case "processing", "uploading", "exporting": .orange
        default: .indigo
        }
    }
}

#Preview {
    JobListView(
        loadJobs: { [] },
        loadLocations: { [] },
        loadConfiguration: { _ in
            AppConfiguration(brands: [], backgrounds: [], captureSteps: [])
        },
        createJob: { _, _, _, _, _ in throw APIError.invalidResponse },
        loadCaptureSession: { _ in throw APIError.invalidResponse },
        uploadCapturedPhoto: { _, _, _ in throw APIError.invalidResponse },
        onLogout: {}
    )
}
