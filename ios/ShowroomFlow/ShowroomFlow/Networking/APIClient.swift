import Foundation

struct APIClient {
    var baseURL: URL

    init(baseURL: URL? = nil) {
        let configuredURL = ProcessInfo.processInfo.environment["SHOWROOMFLOW_API_BASE_URL"]
            .flatMap(URL.init(string:))
        self.baseURL = baseURL
            ?? configuredURL
            ?? URL(string: "https://showroomflow.promotekk.com/api/v1")!
    }

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

    func locations(accessToken: String) async throws -> [LocationSummary] {
        let data = try await authorizedRequest(path: "locations", accessToken: accessToken)
        return try JSONDecoder().decode([LocationSummary].self, from: data)
    }

    func jobs(accessToken: String) async throws -> [VehicleJob] {
        let data = try await authorizedRequest(path: "jobs", accessToken: accessToken)
        return try JSONDecoder().decode([VehicleJob].self, from: data)
    }

    func createJob(
        locationID: UUID,
        vin: String,
        brand: String,
        accessToken: String
    ) async throws -> VehicleJob {
        let body = try JSONEncoder().encode(
            CreateJobPayload(locationID: locationID, vin: vin, brand: brand)
        )
        let data = try await authorizedRequest(
            path: "jobs",
            method: "POST",
            accessToken: accessToken,
            body: body
        )
        return try JSONDecoder().decode(VehicleJob.self, from: data)
    }

    private func authorizedRequest(
        path: String,
        method: String = "GET",
        accessToken: String,
        body: Data? = nil
    ) async throws -> Data {
        let url = baseURL.appending(path: path)
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("Bearer \(accessToken)", forHTTPHeaderField: "Authorization")
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = body
        }

        let (data, response) = try await URLSession.shared.data(for: request)
        guard let httpResponse = response as? HTTPURLResponse else {
            throw APIError.invalidResponse
        }
        if httpResponse.statusCode == 401 {
            throw APIError.unauthorized
        }
        guard 200..<300 ~= httpResponse.statusCode else {
            let message = (try? JSONDecoder().decode(ErrorPayload.self, from: data).detail)
            throw APIError.server(message ?? "Der Auftrag konnte nicht verarbeitet werden.")
        }
        return data
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
    case unauthorized
    case server(String)

    var errorDescription: String? {
        switch self {
        case .invalidResponse:
            return "Der Server hat ungueltig geantwortet."
        case .unauthorized:
            return "Die Sitzung ist abgelaufen."
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

struct LocationSummary: Decodable, Identifiable, Hashable {
    let id: UUID
    let dealershipID: UUID
    let name: String

    enum CodingKeys: String, CodingKey {
        case id, name
        case dealershipID = "dealership_id"
    }
}

struct VehicleJob: Decodable, Identifiable {
    let id: UUID
    let dealershipID: UUID
    let locationID: UUID
    let vin: String
    let version: Int
    let brand: String
    let status: String
    let autoExport: Bool

    enum CodingKeys: String, CodingKey {
        case id, vin, version, brand, status
        case dealershipID = "dealership_id"
        case locationID = "location_id"
        case autoExport = "auto_export"
    }
}

private struct CreateJobPayload: Encodable {
    let locationID: UUID
    let vin: String
    let brand: String

    enum CodingKeys: String, CodingKey {
        case vin, brand
        case locationID = "location_id"
    }
}
