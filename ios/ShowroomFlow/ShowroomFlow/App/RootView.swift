import SwiftUI

struct RootView: View {
    @Environment(\.scenePhase) private var scenePhase
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
                    queueCapturedPhoto: session.queueCapturedPhoto,
                    pendingCapturedPhotos: session.pendingCapturedPhotos,
                    pendingUploadCount: session.pendingUploadCount,
                    completeCapture: session.completeCapture,
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
        .onChange(of: scenePhase) { _, phase in
            guard phase == .active else { return }
            Task { await session.synchronizePendingUploads() }
        }
    }
}

#Preview {
    RootView()
}
