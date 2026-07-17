import SwiftUI

struct LoginView: View {
    @State private var email = ""
    @State private var password = ""
    @State private var isLoading = false
    @State private var errorMessage: String?
    let onLogin: (String, String) async throws -> Void

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("E-Mail-Adresse", text: $email)
                        .textContentType(.emailAddress)
                        .keyboardType(.emailAddress)
                        .textInputAutocapitalization(.never)
                    SecureField("Passwort", text: $password)
                        .textContentType(.password)
                }

                if let errorMessage {
                    Section {
                        Text(errorMessage)
                            .foregroundStyle(.red)
                    }
                }

                Button {
                    Task { await login() }
                } label: {
                    HStack {
                        if isLoading {
                            ProgressView()
                        }
                        Text("Anmelden")
                    }
                    .frame(maxWidth: .infinity)
                }
                .disabled(email.isEmpty || password.isEmpty || isLoading)
            }
            .navigationTitle("ShowroomFlow")
            .safeAreaInset(edge: .bottom) {
                Text("by Promotekk")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .padding()
            }
        }
    }

    @MainActor
    private func login() async {
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }

        do {
            try await onLogin(email, password)
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

#Preview {
    LoginView { _, _ in }
}
