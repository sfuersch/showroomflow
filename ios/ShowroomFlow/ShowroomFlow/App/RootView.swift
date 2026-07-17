import SwiftUI

struct RootView: View {
    @StateObject private var session = SessionStore()

    var body: some View {
        Group {
            if session.isRestoring {
                ProgressView("Sitzung wird geladen …")
            } else if session.isAuthenticated {
                JobListView(
                    loadJobs: session.loadJobs,
                    loadLocations: session.loadLocations,
                    loadConfiguration: session.loadConfiguration,
                    createJob: session.createJob,
                    loadCaptureSession: session.loadCaptureSession,
                    uploadCapturedPhoto: session.uploadCapturedPhoto,
                    onLogout: session.logout
                )
            } else {
                LoginView { email, password in
                    try await session.login(email: email, password: password)
                }
            }
        }
        .tint(.indigo)
        .task {
            await session.restore()
        }
    }
}

#Preview {
    RootView()
}
