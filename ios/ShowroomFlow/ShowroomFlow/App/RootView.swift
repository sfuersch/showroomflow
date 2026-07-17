import SwiftUI

struct RootView: View {
    @State private var isAuthenticated = false

    var body: some View {
        Group {
            if isAuthenticated {
                JobListView()
            } else {
                LoginView {
                    isAuthenticated = true
                }
            }
        }
        .tint(.indigo)
    }
}

#Preview {
    RootView()
}
