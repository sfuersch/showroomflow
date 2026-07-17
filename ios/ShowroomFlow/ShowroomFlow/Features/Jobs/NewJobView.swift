import SwiftUI

struct NewJobView: View {
    @Environment(\.dismiss) private var dismiss
    @State private var vin = ""
    @State private var brand = ""
    @State private var locations: [LocationSummary] = []
    @State private var selectedLocationID: UUID?
    @State private var isLoadingLocations = true
    @State private var isSaving = false
    @State private var errorMessage: String?

    let loadLocations: () async throws -> [LocationSummary]
    let createJob: (UUID, String, String) async throws -> VehicleJob
    let onCreated: (VehicleJob) -> Void

    var body: some View {
        NavigationStack {
            Form {
                Section("Fahrzeug") {
                    HStack {
                        TextField("Fahrgestellnummer", text: $vin)
                            .textInputAutocapitalization(.characters)
                            .autocorrectionDisabled()
                        Button("Scannen", systemImage: "viewfinder") {
                            // VIN camera recognition follows in the camera milestone.
                        }
                        .labelStyle(.iconOnly)
                    }
                    TextField("Marke", text: $brand)
                }

                Section("Standort") {
                    if isLoadingLocations {
                        ProgressView("Standorte werden geladen …")
                    } else if locations.isEmpty {
                        ContentUnavailableView(
                            "Kein Standort",
                            systemImage: "mappin.slash",
                            description: Text("Legen Sie zuerst einen Standort im Backend an.")
                        )
                    } else {
                        Picker("Standort", selection: $selectedLocationID) {
                            ForEach(locations) { location in
                                Text(location.name).tag(Optional(location.id))
                            }
                        }
                    }
                }

                Section {
                    Text("Eine erneut verwendete Fahrgestellnummer erzeugt automatisch eine neue Auftragsversion.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }

                if let errorMessage {
                    Section {
                        Text(errorMessage)
                            .foregroundStyle(.red)
                    }
                }
            }
            .navigationTitle("Neuer Auftrag")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Abbrechen") { dismiss() }
                        .disabled(isSaving)
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Anlegen") {
                        Task { await save() }
                    }
                    .disabled(!canSave)
                }
            }
            .task {
                await fetchLocations()
            }
        }
        .interactiveDismissDisabled(isSaving)
    }

    private var canSave: Bool {
        !isSaving
            && selectedLocationID != nil
            && !vin.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && !brand.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private func fetchLocations() async {
        defer { isLoadingLocations = false }
        do {
            locations = try await loadLocations()
            selectedLocationID = locations.first?.id
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func save() async {
        guard let selectedLocationID else { return }
        isSaving = true
        errorMessage = nil
        defer { isSaving = false }
        do {
            let job = try await createJob(selectedLocationID, vin, brand)
            onCreated(job)
            dismiss()
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

#Preview {
    NewJobView(
        loadLocations: { [] },
        createJob: { _, _, _ in throw APIError.invalidResponse },
        onCreated: { _ in }
    )
}
