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
                    ProgressView("Fahrzeuge werden geladen …")
                } else if jobs.isEmpty {
                    ContentUnavailableView(
                        "Noch keine Fahrzeuge",
                        systemImage: "car.side",
                        description: Text(errorMessage ?? "Erstellen Sie den ersten Fotoauftrag.")
                    )
                } else {
                    List(jobs) { job in
                        Button {
                            captureJob = job
                        } label: {
                            HStack {
                                VStack(alignment: .leading, spacing: 5) {
                                    HStack {
                                        Text(job.brand)
                                            .font(.headline)
                                        Text("Version \(job.version)")
                                            .font(.caption)
                                            .foregroundStyle(.secondary)
                                    }
                                    Text(job.vin)
                                        .font(.subheadline.monospaced())
                                    Text(job.localizedStatus)
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                                Spacer()
                                Image(systemName: "camera.fill")
                                    .foregroundStyle(.tint)
                            }
                            .contentShape(.rect)
                        }
                        .buttonStyle(.plain)
                        .padding(.vertical, 3)
                    }
                    .refreshable { await reload() }
                }
            }
            .navigationTitle("Fahrzeuge")
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button(
                        "Abmelden",
                        systemImage: "rectangle.portrait.and.arrow.right",
                        action: onLogout
                    )
                }
                ToolbarItem(placement: .primaryAction) {
                    Button("Neuer Auftrag", systemImage: "plus") {
                        isCreatingJob = true
                    }
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
