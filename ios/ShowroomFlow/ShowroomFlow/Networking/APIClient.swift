import Foundation

struct APIClient {
    #if DEBUG
    var baseURL = URL(string: "http://localhost:8000/api/v1")!
    #else
    var baseURL = URL(string: "https://showroomflow.promotekk.com/api/v1")!
    #endif

    func appInfo() async throws -> AppInfo {
        let url = baseURL.appending(path: "app-info")
        let (data, response) = try await URLSession.shared.data(from: url)

        guard let httpResponse = response as? HTTPURLResponse,
              200..<300 ~= httpResponse.statusCode else {
            throw APIError.invalidResponse
        }

        return try JSONDecoder().decode(AppInfo.self, from: data)
    }

    func login(email: String, password: String) async throws -> TokenPair {
        let url = baseURL.appending(path: "auth/login")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(LoginPayload(email: email, password: password))

        let (data, response) = try await URLSession.shared.data(for: request)
        guard let httpResponse = response as? HTTPURLResponse else {
            throw APIError.invalidResponse
        }
        guard 200..<300 ~= httpResponse.statusCode else {
            let message = (try? JSONDecoder().decode(ErrorPayload.self, from: data).detail)
            throw APIError.server(message ?? "Anmeldung fehlgeschlagen")
        }
        return try JSONDecoder().decode(TokenPair.self, from: data)
    }

    func refresh(refreshToken: String) async throws -> TokenPair {
        try await tokenRequest(path: "auth/refresh", refreshToken: refreshToken)
    }

    func logout(refreshToken: String) async throws {
        let url = baseURL.appending(path: "auth/logout")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(RefreshPayload(refreshToken: refreshToken))
        let (_, response) = try await URLSession.shared.data(for: request)
        guard let httpResponse = response as? HTTPURLResponse,
              200..<300 ~= httpResponse.statusCode else {
            throw APIError.invalidResponse
        }
    }

    private func tokenRequest(path: String, refreshToken: String) async throws -> TokenPair {
        let url = baseURL.appending(path: path)
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(RefreshPayload(refreshToken: refreshToken))

        let (data, response) = try await URLSession.shared.data(for: request)
        guard let httpResponse = response as? HTTPURLResponse else {
            throw APIError.invalidResponse
        }
        guard 200..<300 ~= httpResponse.statusCode else {
            let message = (try? JSONDecoder().decode(ErrorPayload.self, from: data).detail)
            throw APIError.server(message ?? "Sitzung abgelaufen")
        }
        return try JSONDecoder().decode(TokenPair.self, from: data)
    }
}

enum APIError: LocalizedError {
    case invalidResponse
    case server(String)

    var errorDescription: String? {
        switch self {
        case .invalidResponse:
            return "Der Server hat ungueltig geantwortet."
        case let .server(message):
            return message
        }
    }
}

private struct LoginPayload: Encodable {
    let email: String
    let password: String
}

private struct RefreshPayload: Encodable {
    let refreshToken: String

    enum CodingKeys: String, CodingKey {
        case refreshToken = "refresh_token"
    }
}

private struct ErrorPayload: Decodable {
    let detail: String
}

struct TokenPair: Codable {
    let accessToken: String
    let refreshToken: String
    let tokenType: String
    let expiresIn: Int

    enum CodingKeys: String, CodingKey {
        case accessToken = "access_token"
        case refreshToken = "refresh_token"
        case tokenType = "token_type"
        case expiresIn = "expires_in"
    }
}

struct AppInfo: Decodable {
    let name: String
    let version: String
    let minimumIOSVersion: String
    let outputWidth: Int
    let outputHeight: Int

    enum CodingKeys: String, CodingKey {
        case name, version
        case minimumIOSVersion = "minimum_ios_version"
        case outputWidth = "output_width"
        case outputHeight = "output_height"
    }
}
