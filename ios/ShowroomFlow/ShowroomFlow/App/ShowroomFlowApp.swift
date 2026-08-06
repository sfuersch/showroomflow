import INSCoreMedia
import INSCameraServiceSDK
import INSCameraSDK
import SwiftUI

@main
struct ShowroomFlowApp: App {
    init() {
        INSCameraManager.shared().setup()
    }

    var body: some Scene {
        WindowGroup {
            RootView()
        }
    }
}
