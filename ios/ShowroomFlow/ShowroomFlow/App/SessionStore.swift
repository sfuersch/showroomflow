import Foundation

@MainActor
final class SessionStore: ObservableObject {
    @Published private(set) var isAuthenticated: Bool
    @Published private(set) var isRestoring = true

    private let apiClient: APIClient
    private let keychain: KeychainStore

    init(apiClient: APIClient = APIClient(), keychain: KeychainStore = KeychainStore()) {
        self.apiClient = apiClient
        self.keychain = keychain
        isAuthenticated = false
    }

    func restore() async {
        guard isRestoring else { return }
        defer { isRestoring = false }
        guard let storedTokens = keychain.loadTokens() else { return }

        do {
            let renewedTokens = try await apiClient.refresh(
                refreshToken: storedTokens.refreshToken
            )
            try keychain.save(tokens: renewedTokens)
            isAuthenticated = true
        } catch {
            keychain.deleteTokens()
        }
    }

    func login(email: String, password: String) async throws {
        let tokens = try await apiClient.login(email: email, password: password)
        try keychain.save(tokens: tokens)
        isAuthenticated = true
        isRestoring = false
    }

    func logout() {
        let refreshToken = keychain.loadTokens()?.refreshToken
        keychain.deleteTokens()
        isAuthenticated = false

        if let refreshToken {
            Task {
                try? await apiClient.logout(refreshToken: refreshToken)
            }
        }
    }
}
