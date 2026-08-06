import PhotosUI
import SwiftUI

struct Camera360ProviderSelectionView: View {
    @Environment(\.dismiss) private var dismiss
    @Binding var selectedDJIPhoto: PhotosPickerItem?

    let onSelectTheta: () -> Void
    let onSelectInsta360: () -> Void

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("Kamerahersteller auswählen")
                            .font(.title2.bold())
                        Text(
                            "Wählen Sie, wie die 360°-Innenaufnahme erstellt werden soll."
                        )
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                    }

                    Button(action: onSelectTheta) {
                        providerCard(
                            title: "Ricoh THETA",
                            subtitle: "Direkt verbinden, Live-Vorschau anzeigen und in ShowroomFlow auslösen.",
                            systemImage: "camera.aperture",
                            badge: "Direkt verbunden",
                            tint: .mint
                        )
                    }
                    .buttonStyle(.plain)
                    .accessibilityHint(
                        "Öffnet die WLAN-Verbindung und Live-Vorschau der Ricoh THETA"
                    )

                    Button(action: onSelectInsta360) {
                        providerCard(
                            title: "Insta360 X4",
                            subtitle: "Kamera-WLAN verbinden, Live-Vorschau anzeigen und direkt auslösen.",
                            systemImage: "camera.viewfinder",
                            badge: "Direkt verbunden",
                            tint: .blue
                        )
                    }
                    .buttonStyle(.plain)
                    .accessibilityHint(
                        "Öffnet die Verbindung und Live-Vorschau der Insta360 X4"
                    )

                    PhotosPicker(
                        selection: $selectedDJIPhoto,
                        matching: .images,
                        photoLibrary: .shared()
                    ) {
                        providerCard(
                            title: "DJI Osmo 360",
                            subtitle: "Mit der Kamera oder DJI Mimo aufnehmen und das fertige 360°-Foto übernehmen.",
                            systemImage: "camera.fill",
                            badge: "Über Fotomediathek",
                            tint: .orange
                        )
                    }
                    .buttonStyle(.plain)
                    .accessibilityHint(
                        "Öffnet die Fotomediathek zur Auswahl einer DJI-360-Grad-Aufnahme"
                    )

                    Label(
                        "DJI stellt derzeit keine öffentlich dokumentierte iOS-Schnittstelle "
                            + "für Live-Vorschau und Fernauslösung der Osmo 360 bereit. "
                            + "Die Auswahl ist bereits herstellerneutral aufgebaut und kann "
                            + "später um eine direkte DJI-Verbindung ergänzt werden.",
                        systemImage: "info.circle"
                    )
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .padding(14)
                    .background(.quaternary, in: .rect(cornerRadius: 14))
                }
                .padding(20)
            }
            .navigationTitle("360°-Kamera")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Abbrechen") { dismiss() }
                }
            }
            .onChange(of: selectedDJIPhoto) { _, item in
                if item != nil {
                    dismiss()
                }
            }
        }
    }

    private func providerCard(
        title: String,
        subtitle: String,
        systemImage: String,
        badge: String,
        tint: Color
    ) -> some View {
        HStack(spacing: 15) {
            Image(systemName: systemImage)
                .font(.system(size: 25, weight: .semibold))
                .foregroundStyle(tint)
                .frame(width: 48, height: 48)
                .background(tint.opacity(0.13), in: .rect(cornerRadius: 14))

            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Text(title)
                        .font(.headline)
                        .foregroundStyle(.primary)
                    Spacer()
                    Text(badge)
                        .font(.caption2.bold())
                        .foregroundStyle(tint)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 4)
                        .background(tint.opacity(0.12), in: .capsule)
                }
                Text(subtitle)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.leading)
            }

            Image(systemName: "chevron.right")
                .font(.caption.bold())
                .foregroundStyle(.tertiary)
        }
        .padding(16)
        .background(.background, in: .rect(cornerRadius: 18))
        .overlay {
            RoundedRectangle(cornerRadius: 18)
                .stroke(Color.primary.opacity(0.1))
        }
        .contentShape(.rect)
    }
}
