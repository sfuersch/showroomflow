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

    func loadLocations() async throws -> [LocationSummary] {
        try await withAccessToken { token in
            try await apiClient.locations(accessToken: token)
        }
    }

    func loadJobs() async throws -> [VehicleJob] {
        try await withAccessToken { token in
            try await apiClient.jobs(accessToken: token)
        }
    }

    func createJob(locationID: UUID, vin: String, brand: String) async throws -> VehicleJob {
        try await withAccessToken { token in
            try await apiClient.createJob(
                locationID: locationID,
                vin: vin,
                brand: brand,
                accessToken: token
            )
        }
    }

    private func withAccessToken<Value>(
        _ operation: (String) async throws -> Value
    ) async throws -> Value {
        guard let tokens = keychain.loadTokens() else {
            throw APIError.unauthorized
        }

        do {
            return try await operation(tokens.accessToken)
        } catch APIError.unauthorized {
            let renewedTokens = try await apiClient.refresh(refreshToken: tokens.refreshToken)
            try keychain.save(tokens: renewedTokens)
            return try await operation(renewedTokens.accessToken)
        }
    }
}
