import AVFoundation
import SwiftUI

struct CameraPreview: UIViewRepresentable {
    let session: AVCaptureSession
    let rotationAngle: CGFloat

    func makeUIView(context: Context) -> PreviewView {
        let view = PreviewView()
        view.previewLayer.session = session
        view.previewLayer.videoGravity = .resizeAspect
        applyRotation(to: view.previewLayer)
        return view
    }

    func updateUIView(_ uiView: PreviewView, context: Context) {
        uiView.previewLayer.session = session
        applyRotation(to: uiView.previewLayer)
    }

    private func applyRotation(to previewLayer: AVCaptureVideoPreviewLayer) {
        guard let connection = previewLayer.connection,
              connection.isVideoRotationAngleSupported(rotationAngle) else {
            return
        }
        connection.videoRotationAngle = rotationAngle
    }
}

final class PreviewView: UIView {
    override class var layerClass: AnyClass { AVCaptureVideoPreviewLayer.self }

    var previewLayer: AVCaptureVideoPreviewLayer {
        layer as! AVCaptureVideoPreviewLayer
    }
}
