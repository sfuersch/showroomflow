import AVFoundation
import SwiftUI
import VisionKit

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
    @State private var isShowingVINScanner = false
    @State private var scannerErrorMessage: String?
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
                            Task { await openVINScanner() }
                        }
                        .labelStyle(.iconOnly)
                        .accessibilityHint("Öffnet die Kamera und erkennt die Fahrgestellnummer")
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
            .scrollContentBackground(.hidden)
            .background(Color(.systemGroupedBackground))
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
                ToolbarItem(placement: .principal) {
                    ShowroomFlowCompactHeader(subtitle: "Neuer Auftrag")
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
            .fullScreenCover(isPresented: $isShowingVINScanner) {
                VINScannerView(
                    onRecognized: { recognizedVIN in
                        vin = recognizedVIN
                        isShowingVINScanner = false
                    },
                    onCancel: {
                        isShowingVINScanner = false
                    },
                    onError: { message in
                        isShowingVINScanner = false
                        scannerErrorMessage = message
                    }
                )
            }
            .alert(
                "Scanner nicht verfügbar",
                isPresented: Binding(
                    get: { scannerErrorMessage != nil },
                    set: { if !$0 { scannerErrorMessage = nil } }
                )
            ) {
                Button("OK", role: .cancel) {}
            } message: {
                Text(scannerErrorMessage ?? "Die Fahrgestellnummer kann manuell eingegeben werden.")
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

    @MainActor
    private func openVINScanner() async {
        guard DataScannerViewController.isSupported else {
            scannerErrorMessage = "Dieses Gerät unterstützt den Live-Scanner nicht. Bitte geben Sie die Fahrgestellnummer manuell ein."
            return
        }

        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized:
            break
        case .notDetermined:
            guard await AVCaptureDevice.requestAccess(for: .video) else {
                scannerErrorMessage = "Der Kamerazugriff wurde nicht erlaubt. Sie können ihn in den iOS-Einstellungen für ShowroomFlow aktivieren."
                return
            }
        case .denied, .restricted:
            scannerErrorMessage = "ShowroomFlow darf nicht auf die Kamera zugreifen. Bitte erlauben Sie den Zugriff in den iOS-Einstellungen."
            return
        @unknown default:
            scannerErrorMessage = "Der Kamerazugriff konnte nicht geprüft werden."
            return
        }

        guard DataScannerViewController.isAvailable else {
            scannerErrorMessage = "Der Live-Scanner ist momentan nicht verfügbar. Bitte schließen Sie andere Kamera-Apps und versuchen Sie es erneut."
            return
        }

        isShowingVINScanner = true
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

private struct VINScannerView: View {
    let onRecognized: (String) -> Void
    let onCancel: () -> Void
    let onError: (String) -> Void

    var body: some View {
        GeometryReader { proxy in
            let scanFrame = VINScanLayout.scanFrame(in: proxy.size)

            ZStack {
                VINDataScanner(
                    scanRegion: scanFrame,
                    onRecognized: onRecognized,
                    onError: onError
                )

                RoundedRectangle(cornerRadius: 18)
                    .stroke(
                        Color.mint,
                        style: StrokeStyle(lineWidth: 3, dash: [14, 8])
                    )
                    .frame(width: scanFrame.width, height: scanFrame.height)
                    .overlay {
                        Image(systemName: "viewfinder")
                            .font(.system(size: 48, weight: .light))
                            .foregroundStyle(.mint.opacity(0.9))
                    }
                    .shadow(color: .black.opacity(0.55), radius: 10)
                    .position(x: scanFrame.midX, y: scanFrame.midY)
                    .allowsHitTesting(false)

                VStack {
                    HStack(spacing: 12) {
                        Button(action: onCancel) {
                            Label("Zurück", systemImage: "chevron.left")
                                .font(.headline)
                                .foregroundStyle(.white)
                                .padding(.horizontal, 16)
                                .frame(height: 46)
                                .background(.ultraThinMaterial, in: .capsule)
                        }

                        Spacer()

                        HStack(spacing: 9) {
                            ShowroomFlowBrandMark(size: 34)
                            VStack(alignment: .leading, spacing: 1) {
                                Text("VIN scannen")
                                    .font(.subheadline.bold())
                                Text("Nummer vollständig in den Rahmen halten")
                                    .font(.caption2)
                                    .foregroundStyle(.white.opacity(0.72))
                            }
                        }
                        .foregroundStyle(.white)
                        .padding(.horizontal, 13)
                        .frame(height: 50)
                        .background(.ultraThinMaterial, in: .capsule)
                    }
                    .padding(.horizontal, 16)
                    .padding(.top, proxy.safeAreaInsets.top + 8)

                    Spacer()

                    Label(
                        "Erkennung nur innerhalb des Rahmens",
                        systemImage: "viewfinder.circle"
                    )
                    .font(.subheadline.bold())
                    .foregroundStyle(.white)
                    .padding(.horizontal, 18)
                    .frame(height: 48)
                    .background(.ultraThinMaterial, in: .capsule)
                    .padding(.bottom, proxy.safeAreaInsets.bottom + 20)
                }
            }
        }
        .ignoresSafeArea()
        .background(.black)
    }
}

private enum VINScanLayout {
    static func scanFrame(in size: CGSize) -> CGRect {
        let horizontalMargin: CGFloat = 28
        let width = min(max(size.width - horizontalMargin * 2, 220), 520)
        let height = min(max(width * 0.23, 92), 118)
        return CGRect(
            x: (size.width - width) / 2,
            y: (size.height - height) / 2,
            width: width,
            height: height
        )
    }
}

private struct VINDataScanner: UIViewControllerRepresentable {
    let scanRegion: CGRect
    let onRecognized: (String) -> Void
    let onError: (String) -> Void

    func makeCoordinator() -> Coordinator {
        Coordinator(onRecognized: onRecognized)
    }

    func makeUIViewController(context: Context) -> DataScannerViewController {
        let scanner = DataScannerViewController(
            recognizedDataTypes: [.text(languages: ["de-DE", "en-US"])],
            qualityLevel: .accurate,
            recognizesMultipleItems: true,
            isHighFrameRateTrackingEnabled: false,
            isPinchToZoomEnabled: true,
            isGuidanceEnabled: true,
            isHighlightingEnabled: true
        )
        scanner.delegate = context.coordinator
        context.coordinator.update(scanRegion: scanRegion, on: scanner)
        do {
            try scanner.startScanning()
        } catch {
            Task { @MainActor in
                onError("Die Kamera konnte nicht gestartet werden: \(error.localizedDescription)")
            }
        }
        return scanner
    }

    func updateUIViewController(
        _ uiViewController: DataScannerViewController,
        context: Context
    ) {
        context.coordinator.update(scanRegion: scanRegion, on: uiViewController)
    }

    static func dismantleUIViewController(
        _ uiViewController: DataScannerViewController,
        coordinator: Coordinator
    ) {
        uiViewController.stopScanning()
    }

    final class Coordinator: NSObject, DataScannerViewControllerDelegate {
        private let onRecognized: (String) -> Void
        private var didFinish = false

        init(onRecognized: @escaping (String) -> Void) {
            self.onRecognized = onRecognized
        }

        func update(scanRegion: CGRect, on scanner: DataScannerViewController) {
            guard scanRegion.width > 0, scanRegion.height > 0 else { return }
            scanner.regionOfInterest = scanRegion
        }

        func dataScanner(
            _ dataScanner: DataScannerViewController,
            didTapOn item: RecognizedItem
        ) {
            guard case let .text(text) = item,
                  let candidate = VINCandidate.from(text.transcript) else { return }
            finish(with: candidate)
        }

        func dataScanner(
            _ dataScanner: DataScannerViewController,
            didAdd addedItems: [RecognizedItem],
            allItems: [RecognizedItem]
        ) {
            for item in addedItems {
                guard case let .text(text) = item,
                      let candidate = VINCandidate.from(text.transcript),
                      candidate.count == 17 else { continue }
                finish(with: candidate)
                return
            }
        }

        private func finish(with candidate: String) {
            guard !didFinish else { return }
            didFinish = true
            Task { @MainActor in
                onRecognized(candidate)
            }
        }
    }
}

private enum VINCandidate {
    static func from(_ text: String) -> String? {
        let candidates = text
            .uppercased()
            .components(separatedBy: CharacterSet.alphanumerics.inverted)
            .filter { !$0.isEmpty }

        if let exactVIN = candidates.first(where: { $0.count == 17 }) {
            return exactVIN
        }

        let combined = candidates.joined()
        if combined.count == 17 {
            return combined
        }

        return candidates
            .filter { $0.count >= 8 }
            .max(by: { $0.count < $1.count })
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
