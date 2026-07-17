import SwiftUI

struct NewJobView: View {
    @Environment(\.dismiss) private var dismiss
    @State private var vin = ""
    @State private var locations: [LocationSummary] = []
    @State private var selectedLocationID: UUID?
    @State private var configuration: AppConfiguration?
    @State private var selectedBrandID: UUID?
    @State private var selectedBackgroundID: UUID?
    @State private var isLoadingLocations = true
    @State private var isLoadingConfiguration = false
    @State private var isSaving = false
    @State private var errorMessage: String?

    let loadLocations: () async throws -> [LocationSummary]
    let loadConfiguration: (UUID) async throws -> AppConfiguration
    let createJob: (UUID, String, UUID, String, UUID?) async throws -> VehicleJob
    let onCreated: (VehicleJob) -> Void

    var body: some View {
        NavigationStack {
            Form {
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
                    if isLoadingConfiguration {
                        ProgressView("Konfiguration wird geladen …")
                    } else if let configuration, !configuration.brands.isEmpty {
                        Picker("Marke", selection: $selectedBrandID) {
                            ForEach(configuration.brands) { brand in
                                Text(brand.name).tag(Optional(brand.id))
                            }
                        }
                    } else {
                        Text("Für diesen Standort sind noch keine Marken konfiguriert.")
                            .foregroundStyle(.secondary)
                    }
                }

                if !availableBackgrounds.isEmpty {
                    Section("Virtueller Showroom") {
                        Picker("Hintergrund", selection: $selectedBackgroundID) {
                            ForEach(availableBackgrounds) { background in
                                Text(background.name).tag(Optional(background.id))
                            }
                        }
                        if let background = selectedBackground {
                            AsyncImage(url: background.imageURL) { image in
                                image.resizable().scaledToFill()
                            } placeholder: {
                                ProgressView()
                            }
                            .frame(maxWidth: .infinity, minHeight: 150, maxHeight: 190)
                            .clipShape(.rect(cornerRadius: 12))
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
            .task(id: selectedLocationID) {
                await fetchConfiguration()
            }
            .onChange(of: selectedBrandID) {
                selectedBackgroundID = availableBackgrounds.first?.id
            }
        }
        .interactiveDismissDisabled(isSaving)
    }

    private var canSave: Bool {
        !isSaving
            && selectedLocationID != nil
            && selectedBrandID != nil
            && !vin.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private var availableBackgrounds: [ConfiguredBackground] {
        guard let configuration, let selectedBrandID else { return [] }
        return configuration.backgrounds.filter {
            $0.brandID == nil || $0.brandID == selectedBrandID
        }
    }

    private var selectedBackground: ConfiguredBackground? {
        availableBackgrounds.first { $0.id == selectedBackgroundID }
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

    private func fetchConfiguration() async {
        guard let selectedLocationID else {
            configuration = nil
            selectedBrandID = nil
            selectedBackgroundID = nil
            return
        }
        isLoadingConfiguration = true
        errorMessage = nil
        defer { isLoadingConfiguration = false }
        do {
            let loadedConfiguration = try await loadConfiguration(selectedLocationID)
            configuration = loadedConfiguration
            selectedBrandID = loadedConfiguration.brands.first?.id
            selectedBackgroundID = availableBackgrounds.first?.id
        } catch {
            configuration = nil
            selectedBrandID = nil
            selectedBackgroundID = nil
            errorMessage = error.localizedDescription
        }
    }

    private func save() async {
        guard let selectedLocationID,
              let selectedBrandID,
              let brand = configuration?.brands.first(where: { $0.id == selectedBrandID }) else {
            return
        }
        isSaving = true
        errorMessage = nil
        defer { isSaving = false }
        do {
            let job = try await createJob(
                selectedLocationID,
                vin,
                selectedBrandID,
                brand.name,
                selectedBackgroundID
            )
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
        loadConfiguration: { _ in
            AppConfiguration(brands: [], backgrounds: [], captureSteps: [])
        },
        createJob: { _, _, _, _, _ in throw APIError.invalidResponse },
        onCreated: { _ in }
    )
}
