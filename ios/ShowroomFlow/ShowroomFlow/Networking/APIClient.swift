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

    func configuration(locationID: UUID, accessToken: String) async throws -> AppConfiguration {
        let data = try await authorizedRequest(
            path: "configuration",
            accessToken: accessToken,
            queryItems: [URLQueryItem(name: "location_id", value: locationID.uuidString)]
        )
        return try JSONDecoder().decode(AppConfiguration.self, from: data)
    }

    func createJob(
        locationID: UUID,
        vin: String,
        brandID: UUID,
        brand: String,
        backgroundID: UUID?,
        accessToken: String
    ) async throws -> VehicleJob {
        let body = try JSONEncoder().encode(
            CreateJobPayload(
                locationID: locationID,
                vin: vin,
                brandID: brandID,
                brand: brand,
                backgroundID: backgroundID
            )
        )
        let data = try await authorizedRequest(
            path: "jobs",
            method: "POST",
            accessToken: accessToken,
            body: body
        )
        return try JSONDecoder().decode(VehicleJob.self, from: data)
    }

    func captureSession(jobID: UUID, accessToken: String) async throws -> CaptureSession {
        let data = try await authorizedRequest(
            path: "jobs/\(jobID.uuidString)/capture",
            accessToken: accessToken
        )
        return try JSONDecoder().decode(CaptureSession.self, from: data)
    }

    func requestPhotoUpload(
        jobID: UUID,
        captureStepID: UUID,
        sizeBytes: Int,
        accessToken: String
    ) async throws -> PhotoUploadTicket {
        let body = try JSONEncoder().encode(
            PhotoUploadPayload(
                captureStepID: captureStepID,
                contentType: "image/jpeg",
                sizeBytes: sizeBytes
            )
        )
        let data = try await authorizedRequest(
            path: "jobs/\(jobID.uuidString)/capture/uploads",
            method: "POST",
            accessToken: accessToken,
            body: body
        )
        return try JSONDecoder().decode(PhotoUploadTicket.self, from: data)
    }

    func uploadPhoto(_ data: Data, to uploadURL: URL) async throws {
        var request = URLRequest(url: uploadURL)
        request.httpMethod = "PUT"
        request.setValue("image/jpeg", forHTTPHeaderField: "Content-Type")
        request.httpBody = data
        let (_, response) = try await URLSession.shared.data(for: request)
        guard let httpResponse = response as? HTTPURLResponse,
              200..<300 ~= httpResponse.statusCode else {
            throw APIError.server("Das Foto konnte nicht hochgeladen werden.")
        }
    }

    func completePhotoUpload(
        jobID: UUID,
        photoID: UUID,
        accessToken: String
    ) async throws -> CapturedPhoto {
        let data = try await authorizedRequest(
            path: "jobs/\(jobID.uuidString)/capture/photos/\(photoID.uuidString)/complete",
            method: "POST",
            accessToken: accessToken
        )
        return try JSONDecoder().decode(CapturedPhoto.self, from: data)
    }

    private func authorizedRequest(
        path: String,
        method: String = "GET",
        accessToken: String,
        body: Data? = nil,
        queryItems: [URLQueryItem] = []
    ) async throws -> Data {
        var components = URLComponents(url: baseURL.appending(path: path), resolvingAgainstBaseURL: false)
        components?.queryItems = queryItems.isEmpty ? nil : queryItems
        guard let url = components?.url else { throw APIError.invalidResponse }
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
    let brandID: UUID?
    let backgroundID: UUID?
    let status: String
    let autoExport: Bool

    enum CodingKeys: String, CodingKey {
        case id, vin, version, brand, status
        case dealershipID = "dealership_id"
        case locationID = "location_id"
        case brandID = "brand_id"
        case backgroundID = "background_id"
        case autoExport = "auto_export"
    }
}

struct AppConfiguration: Decodable {
    let brands: [ConfiguredBrand]
    let backgrounds: [ConfiguredBackground]
    let captureSteps: [ConfiguredCaptureStep]

    enum CodingKeys: String, CodingKey {
        case brands, backgrounds
        case captureSteps = "capture_steps"
    }
}

struct ConfiguredBrand: Decodable, Identifiable, Hashable {
    let id: UUID
    let name: String
}

struct ConfiguredBackground: Decodable, Identifiable, Hashable {
    let id: UUID
    let name: String
    let brandID: UUID?
    let locationIDs: [UUID]
    let imageURL: URL

    enum CodingKeys: String, CodingKey {
        case id, name
        case brandID = "brand_id"
        case locationIDs = "location_ids"
        case imageURL = "image_url"
    }
}

struct ConfiguredCaptureStep: Decodable, Identifiable {
    let id: UUID
    let name: String
    let instruction: String
    let category: String
    let captureOrder: Int
    let exportOrder: Int?
    let isRequired: Bool
    let requiresProcessing: Bool
    let silhouetteURL: URL?

    enum CodingKeys: String, CodingKey {
        case id, name, instruction, category
        case captureOrder = "capture_order"
        case exportOrder = "export_order"
        case isRequired = "is_required"
        case requiresProcessing = "requires_processing"
        case silhouetteURL = "silhouette_url"
    }
}

struct CaptureSession: Decodable {
    let job: VehicleJob
    let captureSteps: [ConfiguredCaptureStep]
    let photos: [CapturedPhoto]

    enum CodingKeys: String, CodingKey {
        case job, photos
        case captureSteps = "capture_steps"
    }
}

struct CapturedPhoto: Decodable, Identifiable {
    let id: UUID
    let captureStepID: UUID
    let revision: Int
    let imageURL: URL
    let processedImageURL: URL?
    let uploadedAt: String

    var displayImageURL: URL {
        processedImageURL ?? imageURL
    }

    var isOptimized: Bool {
        processedImageURL != nil
    }

    enum CodingKeys: String, CodingKey {
        case id, revision
        case captureStepID = "capture_step_id"
        case imageURL = "image_url"
        case processedImageURL = "processed_image_url"
        case uploadedAt = "uploaded_at"
    }
}

struct PhotoUploadTicket: Decodable {
    let photoID: UUID
    let revision: Int
    let uploadURL: URL
    let expiresIn: Int

    enum CodingKeys: String, CodingKey {
        case revision
        case photoID = "photo_id"
        case uploadURL = "upload_url"
        case expiresIn = "expires_in"
    }
}

private struct CreateJobPayload: Encodable {
    let locationID: UUID
    let vin: String
    let brandID: UUID
    let brand: String
    let backgroundID: UUID?

    enum CodingKeys: String, CodingKey {
        case vin, brand
        case locationID = "location_id"
        case brandID = "brand_id"
        case backgroundID = "background_id"
    }
}

private struct PhotoUploadPayload: Encodable {
    let captureStepID: UUID
    let contentType: String
    let sizeBytes: Int

    enum CodingKeys: String, CodingKey {
        case captureStepID = "capture_step_id"
        case contentType = "content_type"
        case sizeBytes = "size_bytes"
    }
}
