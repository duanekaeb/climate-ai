import SwiftUI
import UIKit

/// Climate AI for iPhone and iPad: a thin WKWebView wrapper around the web app served by your
/// home server. It stores one setting (the server URL) and injects nothing into the page.
@main
struct ClimateAIApp: App {
    var body: some Scene {
        WindowGroup {
            RootView()
        }
    }
}

struct RootView: View {
    @AppStorage(ServerConfig.storageKey) private var savedURL: String = ""
    @State private var showSettings = false

    var body: some View {
        Group {
            if let url = ServerConfig.normalize(savedURL) {
                WebContainer(url: url, openSettings: { showSettings = true })
                    .id(url) // a new server URL builds a fresh web view
            } else {
                // First run: ask for the server before showing anything else.
                SettingsView(isFirstRun: true)
            }
        }
        .sheet(isPresented: $showSettings) {
            SettingsView(isFirstRun: false)
        }
        .onReceive(NotificationCenter.default.publisher(for: .climateDeviceDidShake)) { _ in
            // Shake the phone to reach the server settings from anywhere.
            if ServerConfig.normalize(savedURL) != nil {
                showSettings = true
            }
        }
    }
}

extension Notification.Name {
    static let climateDeviceDidShake = Notification.Name("ClimateAI.deviceDidShake")
}

// The web view is the first responder almost all the time, so shakes travel up the
// responder chain to the window; turn them into a notification the SwiftUI root listens to.
extension UIWindow {
    open override func motionEnded(_ motion: UIEvent.EventSubtype, with event: UIEvent?) {
        if motion == .motionShake {
            NotificationCenter.default.post(name: .climateDeviceDidShake, object: nil)
        }
        super.motionEnded(motion, with: event)
    }
}
