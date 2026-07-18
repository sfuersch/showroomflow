import SwiftUI

struct LoginView: View {
    @State private var email = ""
    @State private var password = ""
    @State private var isLoading = false
    @State private var errorMessage: String?
    @FocusState private var focusedField: Field?

    let onLogin: (String, String) async throws -> Void

    private enum Field {
        case email
        case password
    }

    var body: some View {
        NavigationStack {
            ZStack {
                Color(.systemGroupedBackground)
                    .ignoresSafeArea()

                Circle()
                    .fill(Color.indigo.opacity(0.12))
                    .frame(width: 360, height: 360)
                    .blur(radius: 8)
                    .offset(x: 170, y: -310)

                ScrollView {
                    VStack(spacing: 28) {
                        Spacer(minLength: 42)

                        VStack(spacing: 16) {
                            ShowroomFlowBrandMark(size: 112)

                            VStack(spacing: 7) {
                                Text("ShowroomFlow")
                                    .font(.system(size: 34, weight: .bold, design: .rounded))
                                    .foregroundStyle(.primary)
                                Text("Fahrzeugfotografie. Einfach im Flow.")
                                    .font(.subheadline)
                                    .foregroundStyle(.secondary)
                            }
                        }

                        VStack(spacing: 18) {
                            VStack(spacing: 12) {
                                credentialField(
                                    systemImage: "envelope.fill",
                                    title: "E-Mail-Adresse"
                                ) {
                                    TextField("E-Mail-Adresse", text: $email)
                                        .textContentType(.emailAddress)
                                        .keyboardType(.emailAddress)
                                        .textInputAutocapitalization(.never)
                                        .autocorrectionDisabled()
                                        .focused($focusedField, equals: .email)
                                        .submitLabel(.next)
                                        .onSubmit { focusedField = .password }
                                }

                                credentialField(systemImage: "lock.fill", title: "Passwort") {
                                    SecureField("Passwort", text: $password)
                                        .textContentType(.password)
                                        .focused($focusedField, equals: .password)
                                        .submitLabel(.go)
                                        .onSubmit { Task { await login() } }
                                }
                            }

                            if let errorMessage {
                                Label(errorMessage, systemImage: "exclamationmark.triangle.fill")
                                    .font(.footnote.weight(.medium))
                                    .foregroundStyle(.red)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .padding(13)
                                    .background(Color.red.opacity(0.08), in: .rect(cornerRadius: 13))
                            }

                            Button {
                                Task { await login() }
                            } label: {
                                HStack(spacing: 10) {
                                    if isLoading {
                                        ProgressView()
                                            .tint(.white)
                                    }
                                    Text(isLoading ? "Anmeldung läuft …" : "Anmelden")
                                }
                                .font(.headline)
                                .foregroundStyle(.white)
                                .frame(maxWidth: .infinity, minHeight: 52)
                                .background(showroomFlowGradient, in: .rect(cornerRadius: 15))
                                .shadow(color: Color.indigo.opacity(0.24), radius: 14, y: 8)
                            }
                            .buttonStyle(.plain)
                            .disabled(email.isEmpty || password.isEmpty || isLoading)
                            .opacity(email.isEmpty || password.isEmpty ? 0.55 : 1)
                        }
                        .padding(22)
                        .background(.background, in: .rect(cornerRadius: 24))
                        .overlay {
                            RoundedRectangle(cornerRadius: 24)
                                .stroke(Color.primary.opacity(0.06))
                        }
                        .shadow(color: Color.black.opacity(0.06), radius: 24, y: 12)

                        Text("by Promotekk")
                            .font(.footnote.weight(.medium))
                            .foregroundStyle(.tertiary)

                        Spacer(minLength: 24)
                    }
                    .frame(maxWidth: 480)
                    .padding(.horizontal, 22)
                    .frame(maxWidth: .infinity)
                }
                .scrollDismissesKeyboard(.interactively)
            }
            .toolbar(.hidden, for: .navigationBar)
        }
    }

    @ViewBuilder
    private func credentialField<Content: View>(
        systemImage: String,
        title: String,
        @ViewBuilder content: () -> Content
    ) -> some View {
        HStack(spacing: 12) {
            Image(systemName: systemImage)
                .foregroundStyle(.indigo)
                .frame(width: 22)
                .accessibilityHidden(true)
            content()
                .textFieldStyle(.plain)
                .accessibilityLabel(title)
        }
        .padding(.horizontal, 15)
        .frame(minHeight: 54)
        .background(Color(.secondarySystemGroupedBackground), in: .rect(cornerRadius: 14))
        .overlay {
            RoundedRectangle(cornerRadius: 14)
                .stroke(Color.primary.opacity(0.07))
        }
    }

    @MainActor
    private func login() async {
        guard !email.isEmpty, !password.isEmpty, !isLoading else { return }
        focusedField = nil
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

struct ShowroomFlowBrandMark: View {
    let size: CGFloat

    var body: some View {
        Image("ShowroomFlowLogo")
            .resizable()
            .scaledToFit()
            .frame(width: size, height: size)
            .clipShape(.rect(cornerRadius: size * 0.23))
            .shadow(color: Color.indigo.opacity(0.22), radius: size * 0.13, y: size * 0.07)
            .accessibilityLabel("ShowroomFlow")
    }
}

struct ShowroomFlowCompactHeader: View {
    let subtitle: String

    var body: some View {
        HStack(spacing: 9) {
            ShowroomFlowBrandMark(size: 34)
            VStack(alignment: .leading, spacing: 0) {
                Text("ShowroomFlow")
                    .font(.subheadline.weight(.bold))
                Text(subtitle)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
    }
}

private let showroomFlowGradient = LinearGradient(
    colors: [Color(red: 0.24, green: 0.29, blue: 0.72), .indigo],
    startPoint: .topLeading,
    endPoint: .bottomTrailing
)

#Preview {
    LoginView { _, _ in }
}
