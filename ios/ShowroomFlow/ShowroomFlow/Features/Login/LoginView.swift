import SwiftUI

struct LoginView: View {
    @State private var email = ""
    @State private var password = ""
    let onLogin: () -> Void

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

                Button("Anmelden", action: onLogin)
                    .frame(maxWidth: .infinity)
                    .disabled(email.isEmpty || password.isEmpty)
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
}

#Preview {
    LoginView(onLogin: {})
}
