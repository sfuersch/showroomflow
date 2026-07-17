import SwiftUI

struct NewJobView: View {
    @Environment(\.dismiss) private var dismiss
    @State private var vin = ""
    @State private var selectedBrand = ""
    @State private var selectedBackground = ""

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
                    TextField("Marke", text: $selectedBrand)
                    TextField("Hintergrund", text: $selectedBackground)
                }

                Section {
                    Text("Eine erneut verwendete Fahrgestellnummer erzeugt automatisch eine neue Auftragsversion.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            }
            .navigationTitle("Neuer Auftrag")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Abbrechen") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Weiter") {
                        // Persisting the job and opening guided capture follows with the API.
                    }
                    .disabled(vin.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
        }
    }
}

#Preview {
    NewJobView()
}
