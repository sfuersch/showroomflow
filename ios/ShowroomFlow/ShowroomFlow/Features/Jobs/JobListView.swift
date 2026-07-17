import SwiftUI

struct JobListView: View {
    @State private var isCreatingJob = false
    let onLogout: () -> Void

    var body: some View {
        NavigationStack {
            ContentUnavailableView(
                "Noch keine Fahrzeuge",
                systemImage: "car.side",
                description: Text("Erstellen Sie den ersten Fotoauftrag.")
            )
            .navigationTitle("Fahrzeuge")
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("Abmelden", systemImage: "rectangle.portrait.and.arrow.right", action: onLogout)
                }
                ToolbarItem(placement: .primaryAction) {
                    Button("Neuer Auftrag", systemImage: "plus") {
                        isCreatingJob = true
                    }
                }
            }
            .sheet(isPresented: $isCreatingJob) {
                NewJobView()
            }
        }
    }
}

#Preview {
    JobListView(onLogout: {})
}
