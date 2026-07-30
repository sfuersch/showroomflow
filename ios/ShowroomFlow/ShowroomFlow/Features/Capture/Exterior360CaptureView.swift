import ARKit
import CoreImage
import ImageIO
import SwiftUI
import UIKit
import simd

struct Exterior360CaptureView: View {
    @Environment(\.dismiss) private var dismiss
    @StateObject private var guide = Exterior360CaptureGuide()
    @State private var isUploading = false
    @State private var errorMessage: String?
    @State private var showsIntroduction = true

    let steps: [ConfiguredCaptureStep]
    let completedStepIDs: Set<UUID>
    let uploadPhoto: (ConfiguredCaptureStep, CapturedCameraPhoto) async throws -> Void

    private var orderedSteps: [ConfiguredCaptureStep] {
        steps.sorted { $0.orientationInstanceIndex < $1.orientationInstanceIndex }
    }

    var body: some View {
        ZStack {
            Exterior360ARView(session: guide.session)
                .ignoresSafeArea()

            LinearGradient(
                colors: [.black.opacity(0.62), .clear, .black.opacity(0.55)],
                startPoint: .leading,
                endPoint: .trailing
            )
            .ignoresSafeArea()
            .allowsHitTesting(false)

            if guide.phase == .complete {
                completionCard
            } else {
                captureInterface
            }

            if showsIntroduction {
                introductionCard
                    .transition(.scale.combined(with: .opacity))
            }
        }
        .preferredColorScheme(.dark)
        .statusBarHidden()
        .onAppear {
            guide.start(completedInstanceIndexes: completedInstanceIndexes)
        }
        .onDisappear { guide.stop() }
        .alert(
            "360°-Aufnahme",
            isPresented: Binding(
                get: { errorMessage != nil },
                set: { if !$0 { errorMessage = nil } }
            )
        ) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(errorMessage ?? "")
        }
    }

    private var completedInstanceIndexes: Set<Int> {
        Set(
            orderedSteps.compactMap { step in
                completedStepIDs.contains(step.id) ? step.orientationInstanceIndex : nil
            }
        )
    }

    private var introductionCard: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(spacing: 12) {
                Image(systemName: "viewfinder.circle.fill")
                    .font(.system(size: 38))
                    .foregroundStyle(.mint)
                VStack(alignment: .leading, spacing: 3) {
                    Text("360° außen aufnehmen")
                        .font(.title2.bold())
                    Text("Die App führt dich automatisch um das Fahrzeug.")
                        .foregroundStyle(.white.opacity(0.72))
                }
            }

            introductionStep(
                number: 1,
                title: "Front markieren",
                text: "Mittig vor dem Fahrzeug aufstellen und das iPhone auf gleicher Höhe halten."
            )
            introductionStep(
                number: 2,
                title: "Heck markieren",
                text: "Gerade nach hinten gehen und ungefähr den gleichen Abstand beibehalten."
            )
            introductionStep(
                number: 3,
                title: "Grünen Punkten folgen",
                text: "Der Auslöser wird erst aktiv, wenn Abstand, Winkel und Höhe stimmen."
            )

            HStack(spacing: 8) {
                Image(systemName: "iphone.radiowaves.left.and.right")
                    .foregroundStyle(.mint)
                Text("Ein kurzes Vibrieren bestätigt jede richtige Fotoposition.")
                    .font(.caption)
                    .foregroundStyle(.white.opacity(0.78))
            }

            Button("Aufnahme starten") {
                withAnimation(.easeOut(duration: 0.2)) {
                    showsIntroduction = false
                }
            }
            .buttonStyle(.borderedProminent)
            .tint(.mint)
            .frame(maxWidth: .infinity)
        }
        .padding(26)
        .frame(maxWidth: 540)
        .background(.ultraThinMaterial, in: .rect(cornerRadius: 28))
        .overlay {
            RoundedRectangle(cornerRadius: 28)
                .stroke(.white.opacity(0.15), lineWidth: 1)
        }
        .padding(24)
    }

    private func introductionStep(number: Int, title: String, text: String) -> some View {
        HStack(alignment: .top, spacing: 13) {
            Text("\(number)")
                .font(.headline.bold())
                .foregroundStyle(.black)
                .frame(width: 30, height: 30)
                .background(.mint, in: .circle)
            VStack(alignment: .leading, spacing: 3) {
                Text(title)
                    .font(.headline)
                Text(text)
                    .font(.subheadline)
                    .foregroundStyle(.white.opacity(0.72))
            }
        }
    }

    private var captureInterface: some View {
        HStack(spacing: 0) {
            leftPanel
                .frame(width: 250)
                .padding(.leading, 18)

            Spacer(minLength: 12)

            VStack {
                statusPill
                Spacer()
                instructionPill
                    .padding(.bottom, 22)
            }
            .padding(.vertical, 16)

            Spacer(minLength: 12)

            rightPanel
                .frame(width: 190)
                .padding(.trailing, 18)
        }
    }

    private var leftPanel: some View {
        VStack(alignment: .leading, spacing: 16) {
            Button {
                dismiss()
            } label: {
                Label("Zurück", systemImage: "chevron.left")
                    .font(.headline)
            }
            .buttonStyle(.bordered)
            .tint(.white)

            VStack(alignment: .leading, spacing: 5) {
                Text("360° außen")
                    .font(.title2.bold())
                Text("12 gleichmäßige Aufnahmen")
                    .font(.subheadline)
                    .foregroundStyle(.white.opacity(0.72))
            }

            OrbitProgressView(
                currentInstance: guide.currentInstanceIndex,
                completedInstances: guide.completedInstanceIndexes
            )
            .frame(height: 210)

            if guide.phase == .capturing {
                Text("\(guide.completedInstanceIndexes.count) von \(orderedSteps.count) aufgenommen")
                    .font(.caption.bold())
                    .foregroundStyle(.white.opacity(0.8))
            }

            Spacer()
        }
        .padding(18)
        .background(.black.opacity(0.5), in: .rect(cornerRadius: 24))
        .padding(.vertical, 16)
    }

    private var rightPanel: some View {
        VStack(spacing: 14) {
            Spacer()

            if guide.phase == .frontCalibration {
                calibrationButton(
                    title: "Frontposition\nfestlegen",
                    systemImage: "car.front.waves.up",
                    enabled: guide.isCalibrationReady
                ) {
                    guide.markFrontPosition()
                }
            } else if guide.phase == .rearCalibration {
                calibrationButton(
                    title: "Heckposition\nfestlegen",
                    systemImage: "car.rear.waves.up",
                    enabled: guide.isCalibrationReady
                ) {
                    guide.markRearPosition()
                }
            } else {
                shutterButton
            }

            if guide.phase == .capturing {
                Text(
                    guide.isInTolerance
                        ? "Position passt"
                        : "Auslöser wird am Ziel aktiv"
                )
                .font(.caption.bold())
                .multilineTextAlignment(.center)
                .foregroundStyle(
                    guide.isInTolerance ? Color.mint : Color.white.opacity(0.72)
                )
            }

            Spacer()
        }
        .padding(16)
        .background(.black.opacity(0.5), in: .rect(cornerRadius: 24))
        .padding(.vertical, 16)
    }

    private var statusPill: some View {
        HStack(spacing: 10) {
            Circle()
                .fill(guide.statusColor)
                .frame(width: 10, height: 10)
            Text(guide.shortStatus)
                .font(.headline)
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 10)
        .background(.black.opacity(0.62), in: .capsule)
    }

    private var instructionPill: some View {
        VStack(spacing: 5) {
            Image(systemName: guide.instructionSymbol)
                .font(.title2.bold())
                .foregroundStyle(guide.statusColor)
            Text(guide.instruction)
                .font(.headline)
                .multilineTextAlignment(.center)
            if let detail = guide.measurementDetail {
                Text(detail)
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.white.opacity(0.76))
            }
        }
        .padding(.horizontal, 22)
        .padding(.vertical, 12)
        .background(.black.opacity(0.68), in: .rect(cornerRadius: 18))
        .frame(maxWidth: 420)
    }

    private func calibrationButton(
        title: String,
        systemImage: String,
        enabled: Bool,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            VStack(spacing: 9) {
                Image(systemName: systemImage)
                    .font(.title.bold())
                Text(title)
                    .font(.caption.bold())
                    .multilineTextAlignment(.center)
            }
            .frame(width: 128, height: 112)
        }
        .buttonStyle(.borderedProminent)
        .tint(enabled ? .mint : .gray)
        .disabled(!enabled)
    }

    private var shutterButton: some View {
        Button {
            Task { await captureCurrentPosition() }
        } label: {
            ZStack {
                Circle()
                    .fill(guide.isInTolerance ? Color.mint : Color.white.opacity(0.28))
                    .frame(width: 98, height: 98)
                Circle()
                    .stroke(.white, lineWidth: 4)
                    .frame(width: 84, height: 84)
                if isUploading {
                    ProgressView()
                        .controlSize(.large)
                        .tint(.white)
                } else {
                    Image(systemName: "camera.fill")
                        .font(.title)
                        .foregroundStyle(.white)
                }
            }
        }
        .disabled(!guide.isInTolerance || isUploading)
        .accessibilityLabel("360-Grad-Außenfoto aufnehmen")
        .accessibilityHint(
            guide.isInTolerance
                ? "Nimmt die aktuelle Position auf"
                : "Wird aktiv, sobald die richtige Position erreicht ist"
        )
    }

    private var completionCard: some View {
        VStack(spacing: 18) {
            Image(systemName: "checkmark.circle.fill")
                .font(.system(size: 64))
                .foregroundStyle(.mint)
            Text("360°-Außenaufnahme vollständig")
                .font(.title2.bold())
            Text("Alle zwölf Perspektiven wurden gespeichert.")
                .foregroundStyle(.white.opacity(0.72))
            Button("Zurück zum Fotoauftrag") {
                dismiss()
            }
            .buttonStyle(.borderedProminent)
            .tint(.mint)
        }
        .padding(30)
        .background(.ultraThinMaterial, in: .rect(cornerRadius: 28))
    }

    @MainActor
    private func captureCurrentPosition() async {
        guard let instanceIndex = guide.currentInstanceIndex,
              let step = orderedSteps.first(where: {
                  $0.orientationInstanceIndex == instanceIndex
              }) else {
            errorMessage = "Die aktuelle 360°-Position ist nicht konfiguriert."
            return
        }

        isUploading = true
        defer { isUploading = false }
        do {
            let photo = try guide.capturePhoto()
            try await uploadPhoto(step, photo)
            guide.markCurrentPositionCompleted()
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

private struct Exterior360ARView: UIViewRepresentable {
    let session: ARSession

    func makeUIView(context: Context) -> ARSCNView {
        let view = ARSCNView(frame: .zero)
        view.session = session
        view.automaticallyUpdatesLighting = true
        view.contentMode = .scaleAspectFill
        return view
    }

    func updateUIView(_ uiView: ARSCNView, context: Context) {}
}

private struct OrbitProgressView: View {
    let currentInstance: Int?
    let completedInstances: Set<Int>

    var body: some View {
        GeometryReader { proxy in
            let size = min(proxy.size.width, proxy.size.height)
            let radius = size * 0.38
            let center = CGPoint(x: proxy.size.width / 2, y: proxy.size.height / 2)
            ZStack {
                Circle()
                    .stroke(.white.opacity(0.22), style: StrokeStyle(lineWidth: 2, dash: [6]))
                    .frame(width: radius * 2, height: radius * 2)
                    .position(center)

                Image(systemName: "car.top")
                    .font(.system(size: 34))
                    .foregroundStyle(.white.opacity(0.8))
                    .position(center)

                ForEach(1...12, id: \.self) { index in
                    let angle = Angle.degrees(Double(index - 1) * 30 - 90)
                    let point = CGPoint(
                        x: center.x + radius * cos(angle.radians),
                        y: center.y + radius * sin(angle.radians)
                    )
                    ZStack {
                        Circle()
                            .fill(color(for: index))
                            .frame(
                                width: currentInstance == index ? 27 : 20,
                                height: currentInstance == index ? 27 : 20
                            )
                        Text("\(index)")
                            .font(.system(size: 9, weight: .bold))
                            .foregroundStyle(.black)
                    }
                    .position(point)
                }
            }
        }
    }

    private func color(for index: Int) -> Color {
        if completedInstances.contains(index) { return .mint }
        if currentInstance == index { return .yellow }
        return .white.opacity(0.65)
    }
}

@MainActor
private final class Exterior360CaptureGuide: NSObject, ObservableObject, ARSessionDelegate {
    enum Phase {
        case frontCalibration
        case rearCalibration
        case capturing
        case complete
    }

    let session = ARSession()

    @Published private(set) var phase: Phase = .frontCalibration
    @Published private(set) var currentInstanceIndex: Int?
    @Published private(set) var completedInstanceIndexes: Set<Int> = []
    @Published private(set) var isInTolerance = false
    @Published private(set) var isCalibrationReady = false
    @Published private(set) var instruction = "Mittig vor dem Fahrzeug aufstellen"
    @Published private(set) var measurementDetail: String?
    @Published private(set) var trackingReady = false

    private let context = CIContext(options: [.useSoftwareRenderer: false])
    private var latestFrame: ARFrame?
    private var frontPosition: SIMD3<Float>?
    private var vehicleCenter: SIMD3<Float>?
    private var frontDirection: SIMD2<Float>?
    private var orbitRadius: Float?
    private var referenceHeight: Float?
    private var targetSequence: [Int] = []
    private var lastToleranceState = false

    override init() {
        super.init()
        session.delegate = self
    }

    var statusColor: Color {
        if phase == .capturing && isInTolerance { return .mint }
        if trackingReady { return .yellow }
        return .red
    }

    var shortStatus: String {
        switch phase {
        case .frontCalibration:
            return "1. Front kalibrieren"
        case .rearCalibration:
            return "2. Heck kalibrieren"
        case .capturing:
            return currentInstanceIndex.map { "Position \($0) von 12" } ?? "Kreisaufnahme"
        case .complete:
            return "Fertig"
        }
    }

    var instructionSymbol: String {
        switch phase {
        case .frontCalibration, .rearCalibration:
            return isCalibrationReady ? "checkmark.circle.fill" : "move.3d"
        case .capturing:
            if isInTolerance { return "checkmark.circle.fill" }
            return "location.north.circle.fill"
        case .complete:
            return "checkmark.circle.fill"
        }
    }

    func start(completedInstanceIndexes: Set<Int>) {
        self.completedInstanceIndexes = completedInstanceIndexes
        let configuration = ARWorldTrackingConfiguration()
        configuration.worldAlignment = .gravity
        configuration.isAutoFocusEnabled = true
        session.run(configuration, options: [.resetTracking, .removeExistingAnchors])
    }

    func stop() {
        session.pause()
    }

    nonisolated func session(_ session: ARSession, didUpdate frame: ARFrame) {
        Task { @MainActor [weak self] in
            self?.update(frame: frame)
        }
    }

    func markFrontPosition() {
        guard let frame = latestFrame, isCalibrationReady else { return }
        frontPosition = cameraPosition(frame)
        referenceHeight = cameraPosition(frame).y
        phase = .rearCalibration
        isCalibrationReady = false
        instruction = "Gerade hinter das Fahrzeug gehen"
        measurementDetail = "Gleiche Entfernung und Kamerahöhe beibehalten"
        UIImpactFeedbackGenerator(style: .medium).impactOccurred()
    }

    func markRearPosition() {
        guard let frame = latestFrame,
              let frontPosition,
              isCalibrationReady else { return }
        let rearPosition = cameraPosition(frame)
        let center = (frontPosition + rearPosition) / 2
        let frontVector = SIMD2<Float>(
            frontPosition.x - center.x,
            frontPosition.z - center.z
        )
        let radius = simd_length(frontVector)
        guard radius > 1.5 else { return }

        vehicleCenter = center
        frontDirection = simd_normalize(frontVector)
        orbitRadius = radius
        phase = .capturing
        targetSequence = captureSequence(startingNear: rearPosition)
        advanceToNextIncompleteTarget()
        UIImpactFeedbackGenerator(style: .medium).impactOccurred()
    }

    func capturePhoto() throws -> CapturedCameraPhoto {
        guard isInTolerance, let frame = latestFrame else {
            throw Exterior360Error.positionNotReady
        }
        let image = try normalizedJPEG(from: frame)
        let transform = frame.camera.transform
        let forward = SIMD3<Float>(-transform.columns.2.x, -transform.columns.2.y, -transform.columns.2.z)
        let horizontal = sqrt(forward.x * forward.x + forward.z * forward.z)
        let pitch = atan2(forward.y, max(horizontal, 0.0001)) * 180 / .pi
        let yaw = atan2(forward.x, -forward.z) * 180 / .pi
        let roll = frame.camera.eulerAngles.z * 180 / .pi
        return CapturedCameraPhoto(
            data: image,
            metadata: CameraCaptureMetadata(
                horizonAngleDegrees: Double(roll),
                verticalAngleDegrees: Double(pitch),
                yawAngleDegrees: Double(yaw),
                fieldOfViewDegrees: 65,
                motionAvailable: true
            )
        )
    }

    func markCurrentPositionCompleted() {
        guard let currentInstanceIndex else { return }
        completedInstanceIndexes.insert(currentInstanceIndex)
        targetSequence.removeAll { $0 == currentInstanceIndex }
        advanceToNextIncompleteTarget()
    }

    private func update(frame: ARFrame) {
        latestFrame = frame
        trackingReady = frame.camera.trackingState.isNormal
        guard trackingReady else {
            isCalibrationReady = false
            isInTolerance = false
            instruction = "iPhone ruhig halten – Position wird erfasst"
            measurementDetail = nil
            return
        }

        switch phase {
        case .frontCalibration:
            updateFrontCalibration(frame)
        case .rearCalibration:
            updateRearCalibration(frame)
        case .capturing:
            updateOrbitGuidance(frame)
        case .complete:
            break
        }
    }

    private func updateFrontCalibration(_ frame: ARFrame) {
        let roll = abs(frame.camera.eulerAngles.z * 180 / .pi)
        let pitch = abs(verticalViewAngleDegrees(frame))
        isCalibrationReady = roll <= 2 && pitch <= 8
        if roll > 2 {
            instruction = "iPhone waagerecht halten"
        } else if pitch > 8 {
            instruction = "Kamera gerade auf die Fahrzeugmitte richten"
        } else {
            instruction = "Fahrzeug mittig ausrichten und Frontposition festlegen"
        }
        measurementDetail = String(
            format: "Waagerecht %.1f° · Blickhöhe %.1f°",
            roll,
            pitch
        )
    }

    private func updateRearCalibration(_ frame: ARFrame) {
        guard let frontPosition, let referenceHeight else { return }
        let position = cameraPosition(frame)
        let separation = horizontalDistance(position, frontPosition)
        let heightError = abs(position.y - referenceHeight)
        let facingError = facingErrorDegrees(frame: frame, target: frontPosition)
        let roll = abs(frame.camera.eulerAngles.z * 180 / .pi)

        isCalibrationReady = separation >= 3
            && heightError <= 0.10
            && facingError <= 5
            && roll <= 2

        if separation < 3 {
            instruction = "Weiter bis hinter das Fahrzeug gehen"
        } else if heightError > 0.10 {
            instruction = position.y > referenceHeight
                ? "iPhone etwas tiefer halten"
                : "iPhone etwas höher halten"
        } else if facingError > 5 {
            instruction = "Kamera mittig auf das Fahrzeug richten"
        } else if roll > 2 {
            instruction = "iPhone waagerecht halten"
        } else {
            instruction = "Heckposition passt – jetzt festlegen"
        }
        measurementDetail = String(
            format: "Front–Heck %.1f m · Höhe %+.0f cm",
            separation,
            (position.y - referenceHeight) * 100
        )
    }

    private func updateOrbitGuidance(_ frame: ARFrame) {
        guard let targetIndex = currentInstanceIndex,
              let vehicleCenter,
              let frontDirection,
              let orbitRadius,
              let referenceHeight else { return }
        let position = cameraPosition(frame)
        let target = orbitPoint(
            instanceIndex: targetIndex,
            center: vehicleCenter,
            frontDirection: frontDirection,
            radius: orbitRadius,
            height: referenceHeight
        )

        let radialError = horizontalDistance(position, vehicleCenter) - orbitRadius
        let angularError = orbitAngleErrorDegrees(
            position: position,
            center: vehicleCenter,
            target: target
        )
        let heightError = position.y - referenceHeight
        let facingError = facingErrorDegrees(frame: frame, target: vehicleCenter)
        let roll = frame.camera.eulerAngles.z * 180 / .pi

        let ready = abs(radialError) <= 0.15
            && abs(angularError) <= 3
            && abs(heightError) <= 0.10
            && facingError <= 5
            && abs(roll) <= 2
        isInTolerance = ready

        if ready {
            instruction = "Perfekt – Foto aufnehmen"
        } else if abs(angularError) > 3 {
            instruction = angularError > 0
                ? "Auf dem Kreis nach rechts gehen"
                : "Auf dem Kreis nach links gehen"
        } else if abs(radialError) > 0.15 {
            instruction = radialError > 0
                ? "Etwas näher zum Fahrzeug"
                : "Etwas weiter vom Fahrzeug weg"
        } else if abs(heightError) > 0.10 {
            instruction = heightError > 0
                ? "iPhone etwas tiefer halten"
                : "iPhone etwas höher halten"
        } else if facingError > 5 {
            instruction = "Kamera zur Fahrzeugmitte drehen"
        } else {
            instruction = "iPhone waagerecht halten"
        }

        measurementDetail = String(
            format: "Abstand %+.0f cm · Winkel %+.1f° · Höhe %+.0f cm",
            radialError * 100,
            angularError,
            heightError * 100
        )
        if ready && !lastToleranceState {
            UINotificationFeedbackGenerator().notificationOccurred(.success)
        }
        lastToleranceState = ready
    }

    private func advanceToNextIncompleteTarget() {
        while let first = targetSequence.first,
              completedInstanceIndexes.contains(first) {
            targetSequence.removeFirst()
        }
        guard let first = targetSequence.first else {
            currentInstanceIndex = nil
            isInTolerance = false
            phase = .complete
            UINotificationFeedbackGenerator().notificationOccurred(.success)
            return
        }
        currentInstanceIndex = first
        isInTolerance = false
        lastToleranceState = false
    }

    private func captureSequence(startingNear position: SIMD3<Float>) -> [Int] {
        guard let center = vehicleCenter,
              let direction = frontDirection,
              let radius = orbitRadius,
              let referenceHeight else {
            return Array(1...12)
        }
        let closest = (1...12).min { lhs, rhs in
            horizontalDistance(
                position,
                orbitPoint(
                    instanceIndex: lhs,
                    center: center,
                    frontDirection: direction,
                    radius: radius,
                    height: referenceHeight
                )
            ) < horizontalDistance(
                position,
                orbitPoint(
                    instanceIndex: rhs,
                    center: center,
                    frontDirection: direction,
                    radius: radius,
                    height: referenceHeight
                )
            )
        } ?? 7
        return (0..<12).map { ((closest - 1 + $0) % 12) + 1 }
    }

    private func orbitPoint(
        instanceIndex: Int,
        center: SIMD3<Float>,
        frontDirection: SIMD2<Float>,
        radius: Float,
        height: Float
    ) -> SIMD3<Float> {
        let angle = Float(instanceIndex - 1) * 30 * .pi / 180
        let rotated = SIMD2<Float>(
            frontDirection.x * cos(angle) - frontDirection.y * sin(angle),
            frontDirection.x * sin(angle) + frontDirection.y * cos(angle)
        )
        return SIMD3<Float>(
            center.x + rotated.x * radius,
            height,
            center.z + rotated.y * radius
        )
    }

    private func orbitAngleErrorDegrees(
        position: SIMD3<Float>,
        center: SIMD3<Float>,
        target: SIMD3<Float>
    ) -> Float {
        let current = atan2(position.z - center.z, position.x - center.x)
        let desired = atan2(target.z - center.z, target.x - center.x)
        var delta = (desired - current) * 180 / .pi
        while delta > 180 { delta -= 360 }
        while delta < -180 { delta += 360 }
        return delta
    }

    private func facingErrorDegrees(frame: ARFrame, target: SIMD3<Float>) -> Float {
        let transform = frame.camera.transform
        let position = cameraPosition(frame)
        var forward = SIMD2<Float>(-transform.columns.2.x, -transform.columns.2.z)
        var desired = SIMD2<Float>(target.x - position.x, target.z - position.z)
        guard simd_length(forward) > 0.001, simd_length(desired) > 0.001 else {
            return 180
        }
        forward = simd_normalize(forward)
        desired = simd_normalize(desired)
        let value = min(max(simd_dot(forward, desired), -1), 1)
        return acos(value) * 180 / .pi
    }

    private func cameraPosition(_ frame: ARFrame) -> SIMD3<Float> {
        let transform = frame.camera.transform
        return SIMD3<Float>(
            transform.columns.3.x,
            transform.columns.3.y,
            transform.columns.3.z
        )
    }

    private func verticalViewAngleDegrees(_ frame: ARFrame) -> Float {
        let transform = frame.camera.transform
        let forward = SIMD3<Float>(
            -transform.columns.2.x,
            -transform.columns.2.y,
            -transform.columns.2.z
        )
        let horizontal = hypot(forward.x, forward.z)
        return atan2(forward.y, max(horizontal, 0.0001)) * 180 / .pi
    }

    private func horizontalDistance(_ lhs: SIMD3<Float>, _ rhs: SIMD3<Float>) -> Float {
        hypot(lhs.x - rhs.x, lhs.z - rhs.z)
    }

    private func normalizedJPEG(from frame: ARFrame) throws -> Data {
        var image = CIImage(cvPixelBuffer: frame.capturedImage)
        let orientation = currentImageOrientation()
        image = image.oriented(orientation)

        let extent = image.extent.integral
        let targetRatio: CGFloat = 3 / 2
        let crop: CGRect
        if extent.width / extent.height > targetRatio {
            let width = extent.height * targetRatio
            crop = CGRect(
                x: extent.midX - width / 2,
                y: extent.minY,
                width: width,
                height: extent.height
            )
        } else {
            let height = extent.width / targetRatio
            crop = CGRect(
                x: extent.minX,
                y: extent.midY - height / 2,
                width: extent.width,
                height: height
            )
        }
        guard let cgImage = context.createCGImage(image.cropped(to: crop), from: crop) else {
            throw Exterior360Error.imageEncodingFailed
        }
        let source = UIImage(cgImage: cgImage)
        let maximumWidth: CGFloat = 3240
        let maximumHeight: CGFloat = 2160
        var outputWidth = min(maximumWidth, crop.width)
        var outputHeight = outputWidth / 1.5
        if outputHeight > maximumHeight {
            outputHeight = maximumHeight
            outputWidth = outputHeight * 1.5
        }
        outputWidth = max(2, floor(outputWidth / 2) * 2)
        outputHeight = max(2, floor(outputHeight / 2) * 2)
        let targetSize = CGSize(width: outputWidth, height: outputHeight)
        let rendererFormat = UIGraphicsImageRendererFormat()
        // The target size is expressed in output pixels. The default renderer
        // inherits the iPhone screen scale (often 3x), which previously turned
        // 3240 × 2160 captures into 9720 × 6480 files.
        rendererFormat.scale = 1
        rendererFormat.opaque = true
        let renderer = UIGraphicsImageRenderer(
            size: targetSize,
            format: rendererFormat
        )
        let normalized = renderer.image { _ in
            source.draw(in: CGRect(origin: .zero, size: targetSize))
        }
        var quality: CGFloat = 0.92
        var data = normalized.jpegData(compressionQuality: quality)
        while let current = data, current.count > 4_500_000, quality > 0.68 {
            quality -= 0.06
            data = normalized.jpegData(compressionQuality: quality)
        }
        guard let data, data.count <= 10_000_000 else {
            throw Exterior360Error.imageEncodingFailed
        }
        return data
    }

    private func currentImageOrientation() -> CGImagePropertyOrientation {
        guard let orientation = UIApplication.shared.connectedScenes
            .compactMap({ $0 as? UIWindowScene })
            .first?.interfaceOrientation else {
            return .up
        }
        switch orientation {
        case .landscapeLeft:
            return .down
        case .portrait:
            return .right
        case .portraitUpsideDown:
            return .left
        default:
            return .up
        }
    }
}

private extension ARCamera.TrackingState {
    var isNormal: Bool {
        if case .normal = self { return true }
        return false
    }
}

private enum Exterior360Error: LocalizedError {
    case positionNotReady
    case imageEncodingFailed

    var errorDescription: String? {
        switch self {
        case .positionNotReady:
            return "Bitte zuerst die angezeigte Zielposition einnehmen."
        case .imageEncodingFailed:
            return "Das 360°-Außenfoto konnte nicht gespeichert werden."
        }
    }
}
