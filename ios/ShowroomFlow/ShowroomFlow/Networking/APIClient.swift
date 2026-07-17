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
}

enum APIError: Error {
    case invalidResponse
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
