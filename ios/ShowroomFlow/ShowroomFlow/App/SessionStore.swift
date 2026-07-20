import CryptoKit
import Foundation
import Network

struct PendingPhotoUpload: Codable, Identifiable, Equatable {
    let id: UUID
    let jobID: UUID
    let captureStepID: UUID
    let fileURL: URL
    let createdAt: Date
}

private struct OfflineQueueState: Codable {
    var uploads: [PendingPhotoUpload] = []
    var completionJobIDs: Set<UUID> = []
}

private final class OfflineCaptureStore {
    private let rootURL: URL
    private let stateURL: URL
    private var state: OfflineQueueState
    private let encoder: JSONEncoder
    private let decoder: JSONDecoder

    init(scope: String, fileManager: FileManager = .default) throws {
        let applicationSupport = try fileManager.url(
            for: .applicationSupportDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: true
        )
        rootURL = applicationSupport
            .appending(path: "ShowroomFlowOffline", directoryHint: .isDirectory)
            .appending(path: scope, directoryHint: .isDirectory)
        stateURL = rootURL.appending(path: "queue.json")
        try fileManager.createDirectory(
            at: rootURL.appending(path: "photos", directoryHint: .isDirectory),
            withIntermediateDirectories: true
        )
        try fileManager.createDirectory(
            at: rootURL.appending(path: "cache", directoryHint: .isDirectory),
            withIntermediateDirectories: true
        )

        let jsonEncoder = JSONEncoder()
        jsonEncoder.dateEncodingStrategy = .iso8601
        let jsonDecoder = JSONDecoder()
        jsonDecoder.dateDecodingStrategy = .iso8601
        encoder = jsonEncoder
        decoder = jsonDecoder
        state = (try? Data(contentsOf: stateURL)).flatMap {
            try? jsonDecoder.decode(OfflineQueueState.self, from: $0)
        } ?? OfflineQueueState()
        state.uploads.removeAll { !fileManager.fileExists(atPath: $0.fileURL.path) }
        try persistState()
    }

    var pendingUploadCount: Int { state.uploads.count }

    func pendingUploads(jobID: UUID? = nil) -> [PendingPhotoUpload] {
        state.uploads
            .filter { jobID == nil || $0.jobID == jobID }
            .sorted { $0.createdAt < $1.createdAt }
    }

    func enqueuePhoto(jobID: UUID, captureStepID: UUID, data: Data) throws -> PendingPhotoUpload {
        let fileURL = rootURL
            .appending(path: "photos", directoryHint: .isDirectory)
            .appending(path: "\(UUID().uuidString).jpg")
        try data.write(to: fileURL, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])

        let replaced = state.uploads.filter {
            $0.jobID == jobID && $0.captureStepID == captureStepID
        }
        state.uploads.removeAll {
            $0.jobID == jobID && $0.captureStepID == captureStepID
        }
        replaced.forEach { try? FileManager.default.removeItem(at: $0.fileURL) }

        let upload = PendingPhotoUpload(
            id: UUID(),
            jobID: jobID,
            captureStepID: captureStepID,
            fileURL: fileURL,
            createdAt: Date()
        )
        state.uploads.append(upload)
        try persistState()
        return upload
    }

    func removeUpload(id: UUID) throws {
        guard let upload = state.uploads.first(where: { $0.id == id }) else { return }
        state.uploads.removeAll { $0.id == id }
        try persistState()
        try? FileManager.default.removeItem(at: upload.fileURL)
    }

    func requestCompletion(jobID: UUID) throws {
        state.completionJobIDs.insert(jobID)
        try persistState()
    }

    func pendingCompletionJobIDs() -> [UUID] {
        Array(state.completionJobIDs)
    }

    func clearCompletion(jobID: UUID) throws {
        state.completionJobIDs.remove(jobID)
        try persistState()
    }

    func cacheJobs(_ jobs: [VehicleJob]) throws {
        try write(jobs, to: cacheURL("jobs.json"))
    }

    func cacheJob(_ job: VehicleJob) throws {
        var jobs = cachedJobs() ?? []
        jobs.removeAll { $0.id == job.id }
        jobs.insert(job, at: 0)
        try cacheJobs(jobs)
    }

    func cachedJobs() -> [VehicleJob]? {
        read([VehicleJob].self, from: cacheURL("jobs.json"))
    }

    func cacheLocations(_ locations: [LocationSummary]) throws {
        try write(locations, to: cacheURL("locations.json"))
    }

    func cachedLocations() -> [LocationSummary]? {
        read([LocationSummary].self, from: cacheURL("locations.json"))
    }

    func cacheConfiguration(_ configuration: AppConfiguration, locationID: UUID) throws {
        try write(configuration, to: cacheURL("configuration-\(locationID.uuidString).json"))
    }

    func cachedConfiguration(locationID: UUID) -> AppConfiguration? {
        read(
            AppConfiguration.self,
            from: cacheURL("configuration-\(locationID.uuidString).json")
        )
    }

    func cacheCaptureSession(_ session: CaptureSession) throws {
        try write(session, to: captureSessionURL(session.job.id))
    }

    func cachedCaptureSession(jobID: UUID) -> CaptureSession? {
        read(CaptureSession.self, from: captureSessionURL(jobID))
    }

    func cacheUploadedPhoto(_ photo: CapturedPhoto, jobID: UUID) throws {
        guard let session = cachedCaptureSession(jobID: jobID) else { return }
        let updated = CaptureSession(
            job: session.job,
            captureSteps: session.captureSteps,
            photos: session.photos.filter { $0.captureStepID != photo.captureStepID } + [photo]
        )
        try cacheCaptureSession(updated)
    }

    func cacheCompletedJob(_ job: VehicleJob) throws {
        if var jobs = cachedJobs(), let index = jobs.firstIndex(where: { $0.id == job.id }) {
            jobs[index] = job
            try cacheJobs(jobs)
        }
        if let session = cachedCaptureSession(jobID: job.id) {
            try cacheCaptureSession(
                CaptureSession(job: job, captureSteps: session.captureSteps, photos: session.photos)
            )
        }
    }

    private func cacheURL(_ name: String) -> URL {
        rootURL.appending(path: "cache", directoryHint: .isDirectory).appending(path: name)
    }

    private func captureSessionURL(_ jobID: UUID) -> URL {
        cacheURL("capture-\(jobID.uuidString).json")
    }

    private func persistState() throws {
        try write(state, to: stateURL)
    }

    private func write<Value: Encodable>(_ value: Value, to url: URL) throws {
        let data = try encoder.encode(value)
        try data.write(to: url, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
    }

    private func read<Value: Decodable>(_ type: Value.Type, from url: URL) -> Value? {
        guard let data = try? Data(contentsOf: url) else { return nil }
        return try? decoder.decode(type, from: data)
    }
}

@MainActor
final class SessionStore: ObservableObject {
    @Published private(set) var isAuthenticated: Bool
    @Published private(set) var isRestoring = true
    @Published private(set) var pendingUploadCount = 0
    @Published private(set) var lastUploadError: String?

    private let apiClient: APIClient
    private let keychain: KeychainStore
    private let pathMonitor = NWPathMonitor()
    private let monitorQueue = DispatchQueue(label: "com.promotekk.showroomflow.network")
    private var offlineStore: OfflineCaptureStore?
    private var isSynchronizing = false

    init(apiClient: APIClient = APIClient(), keychain: KeychainStore = KeychainStore()) {
        self.apiClient = apiClient
        self.keychain = keychain
        isAuthenticated = false
        pathMonitor.pathUpdateHandler = { [weak self] path in
            guard path.status == .satisfied else { return }
            Task { @MainActor [weak self] in
                await self?.synchronizePendingUploads()
            }
        }
        pathMonitor.start(queue: monitorQueue)
    }

    deinit {
        pathMonitor.cancel()
    }

    func restore() async {
        guard isRestoring else { return }
        defer { isRestoring = false }
        guard let storedTokens = keychain.loadTokens() else { return }
        activateOfflineStore(tokens: storedTokens)

        do {
            let renewedTokens = try await apiClient.refresh(
                refreshToken: storedTokens.refreshToken
            )
            try keychain.save(tokens: renewedTokens)
            activateOfflineStore(tokens: renewedTokens)
            isAuthenticated = true
            await synchronizePendingUploads()
        } catch APIError.unauthorized {
            keychain.deleteTokens()
            offlineStore = nil
        } catch {
            // Bei fehlendem Netz oder einem vorübergehenden Serverfehler bleibt
            // die lokale Sitzung verfügbar. Der Token wird beim nächsten Sync erneuert.
            isAuthenticated = true
        }
    }

    func login(email: String, password: String) async throws {
        let tokens = try await apiClient.login(email: email, password: password)
        try keychain.save(tokens: tokens)
        activateOfflineStore(tokens: tokens)
        isAuthenticated = true
        isRestoring = false
        await synchronizePendingUploads()
    }

    func logout() {
        let refreshToken = keychain.loadTokens()?.refreshToken
        keychain.deleteTokens()
        isAuthenticated = false
        offlineStore = nil
        pendingUploadCount = 0

        if let refreshToken {
            Task {
                try? await apiClient.logout(refreshToken: refreshToken)
            }
        }
    }

    func loadLocations() async throws -> [LocationSummary] {
        do {
            let locations = try await withAccessToken { token in
                try await apiClient.locations(accessToken: token)
            }
            try? offlineStore?.cacheLocations(locations)
            return locations
        } catch {
            if let cached = offlineStore?.cachedLocations() { return cached }
            throw error
        }
    }

    func loadJobs() async throws -> [VehicleJob] {
        do {
            let jobs = try await withAccessToken { token in
                try await apiClient.jobs(accessToken: token)
            }
            try? offlineStore?.cacheJobs(jobs)
            return jobs
        } catch {
            if let cached = offlineStore?.cachedJobs() { return cached }
            throw error
        }
    }

    func loadConfiguration(locationID: UUID) async throws -> AppConfiguration {
        do {
            let configuration = try await withAccessToken { token in
                try await apiClient.configuration(locationID: locationID, accessToken: token)
            }
            try? offlineStore?.cacheConfiguration(configuration, locationID: locationID)
            return configuration
        } catch {
            if let cached = offlineStore?.cachedConfiguration(locationID: locationID) {
                return cached
            }
            throw error
        }
    }

    func createJob(
        locationID: UUID,
        vin: String,
        brandID: UUID,
        brand: String,
        backgroundID: UUID?
    ) async throws -> VehicleJob {
        let job = try await withAccessToken { token in
            try await apiClient.createJob(
                locationID: locationID,
                vin: vin,
                brandID: brandID,
                brand: brand,
                backgroundID: backgroundID,
                accessToken: token
            )
        }
        try? offlineStore?.cacheJob(job)
        return job
    }

    func loadCaptureSession(jobID: UUID) async throws -> CaptureSession {
        do {
            let session = try await withAccessToken { token in
                try await apiClient.captureSession(jobID: jobID, accessToken: token)
            }
            try? offlineStore?.cacheCaptureSession(session)
            return session
        } catch {
            if let cached = offlineStore?.cachedCaptureSession(jobID: jobID) { return cached }
            throw error
        }
    }

    func pendingCapturedPhotos(jobID: UUID) -> [PendingPhotoUpload] {
        offlineStore?.pendingUploads(jobID: jobID) ?? []
    }

    func queueCapturedPhoto(
        jobID: UUID,
        captureStepID: UUID,
        data: Data
    ) async throws -> PendingPhotoUpload {
        guard let offlineStore else { throw APIError.unauthorized }
        let upload = try offlineStore.enqueuePhoto(
            jobID: jobID,
            captureStepID: captureStepID,
            data: data
        )
        updatePendingUploadCount()
        Task { await synchronizePendingUploads() }
        return upload
    }

    func completeCapture(jobID: UUID) async throws -> VehicleJob? {
        guard let offlineStore else { throw APIError.unauthorized }
        if !offlineStore.pendingUploads(jobID: jobID).isEmpty {
            try offlineStore.requestCompletion(jobID: jobID)
            Task { await synchronizePendingUploads() }
            return nil
        }
        do {
            let job = try await completeCaptureNow(jobID: jobID)
            try? offlineStore.cacheCompletedJob(job)
            return job
        } catch is URLError {
            try offlineStore.requestCompletion(jobID: jobID)
            return nil
        }
    }

    func synchronizePendingUploads() async {
        guard !isSynchronizing, let offlineStore, keychain.loadTokens() != nil else { return }
        isSynchronizing = true
        defer {
            isSynchronizing = false
            updatePendingUploadCount()
        }
        lastUploadError = nil

        while let upload = offlineStore.pendingUploads().first {
            do {
                let data = try Data(contentsOf: upload.fileURL)
                let photo = try await uploadNow(
                    jobID: upload.jobID,
                    captureStepID: upload.captureStepID,
                    data: data
                )
                try offlineStore.cacheUploadedPhoto(photo, jobID: upload.jobID)
                try offlineStore.removeUpload(id: upload.id)
                updatePendingUploadCount()
            } catch {
                lastUploadError = error.localizedDescription
                return
            }
        }

        for jobID in offlineStore.pendingCompletionJobIDs() {
            guard offlineStore.pendingUploads(jobID: jobID).isEmpty else { continue }
            do {
                let job = try await completeCaptureNow(jobID: jobID)
                try offlineStore.cacheCompletedJob(job)
                try offlineStore.clearCompletion(jobID: jobID)
            } catch {
                lastUploadError = error.localizedDescription
                return
            }
        }
    }

    private func uploadNow(
        jobID: UUID,
        captureStepID: UUID,
        data: Data
    ) async throws -> CapturedPhoto {
        let ticket = try await withAccessToken { token in
            try await apiClient.requestPhotoUpload(
                jobID: jobID,
                captureStepID: captureStepID,
                sizeBytes: data.count,
                accessToken: token
            )
        }
        try await apiClient.uploadPhoto(data, to: ticket.uploadURL)
        return try await withAccessToken { token in
            try await apiClient.completePhotoUpload(
                jobID: jobID,
                photoID: ticket.photoID,
                accessToken: token
            )
        }
    }

    private func completeCaptureNow(jobID: UUID) async throws -> VehicleJob {
        try await withAccessToken { token in
            try await apiClient.completeCapture(jobID: jobID, accessToken: token)
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
            activateOfflineStore(tokens: renewedTokens)
            return try await operation(renewedTokens.accessToken)
        }
    }

    private func activateOfflineStore(tokens: TokenPair) {
        guard let scope = tokenSubject(tokens.accessToken) ?? tokenSubject(tokens.refreshToken) else {
            let digest = SHA256.hash(data: Data(tokens.refreshToken.utf8))
            let fallback = digest.prefix(12).map { String(format: "%02x", $0) }.joined()
            offlineStore = try? OfflineCaptureStore(scope: fallback)
            updatePendingUploadCount()
            return
        }
        offlineStore = try? OfflineCaptureStore(scope: scope)
        updatePendingUploadCount()
    }

    private func updatePendingUploadCount() {
        pendingUploadCount = offlineStore?.pendingUploadCount ?? 0
    }

    private func tokenSubject(_ token: String) -> String? {
        let parts = token.split(separator: ".")
        guard parts.count >= 2 else { return nil }
        var payload = String(parts[1]).replacingOccurrences(of: "-", with: "+")
            .replacingOccurrences(of: "_", with: "/")
        payload += String(repeating: "=", count: (4 - payload.count % 4) % 4)
        guard let data = Data(base64Encoded: payload),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let subject = object["sub"] as? String else {
            return nil
        }
        let safe = subject.filter { $0.isLetter || $0.isNumber || $0 == "-" || $0 == "_" }
        return safe.isEmpty ? nil : safe
    }
}
