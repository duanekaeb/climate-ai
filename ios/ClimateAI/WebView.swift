import SwiftUI
import UIKit
import WebKit

/// The web app full screen, with an offline screen when the server can't be reached.
struct WebContainer: View {
    let url: URL
    let openSettings: () -> Void

    @State private var loadError: String?
    @State private var reloadToken = 0

    var body: some View {
        ZStack {
            Color(uiColor: .systemBackground).ignoresSafeArea()

            // Full bleed: the web app uses viewport-fit=cover and env(safe-area-inset-*) for the
            // notch and the home indicator, exactly as when it is installed as a PWA.
            WebView(url: url, loadError: $loadError, reloadToken: reloadToken)
                .ignoresSafeArea()

            if let loadError {
                OfflineView(
                    url: url,
                    message: loadError,
                    retry: {
                        self.loadError = nil
                        reloadToken += 1
                    },
                    openSettings: openSettings
                )
            }
        }
    }
}

struct OfflineView: View {
    let url: URL
    let message: String
    let retry: () -> Void
    let openSettings: () -> Void

    var body: some View {
        VStack(spacing: 16) {
            Image(systemName: "wifi.exclamationmark")
                .font(.system(size: 44))
                .foregroundStyle(.secondary)
            Text("Can't reach Climate AI")
                .font(.title3.weight(.semibold))
            Text(url.absoluteString)
                .font(.callout.monospaced())
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            Text(message)
                .font(.footnote)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            HStack(spacing: 12) {
                Button("Try again", action: retry)
                    .buttonStyle(.borderedProminent)
                Button("Server settings", action: openSettings)
                    .buttonStyle(.bordered)
            }
            .padding(.top, 4)
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(uiColor: .systemBackground))
    }
}

/// WKWebView with a persistent website data store (the owner's sign-in survives app restarts),
/// swipe back/forward, pull to refresh, JavaScript dialogs, and links to other sites opened
/// in Safari. Injects no scripts or styles, and stores no credentials natively: the web app
/// keeps its short-lived bearer token in memory and gets a fresh one on launch from the
/// HttpOnly refresh cookie (Path=/api/auth), which WebKit keeps in that store.
struct WebView: UIViewRepresentable {
    let url: URL
    @Binding var loadError: String?
    let reloadToken: Int

    func makeCoordinator() -> Coordinator {
        Coordinator(parent: self)
    }

    func makeUIView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        // The default (persistent) store keeps the HttpOnly refresh cookie between launches.
        configuration.websiteDataStore = WKWebsiteDataStore.default()
        configuration.applicationNameForUserAgent = "ClimateAI-iOS"
        configuration.allowsInlineMediaPlayback = true

        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = context.coordinator
        webView.uiDelegate = context.coordinator
        webView.allowsBackForwardNavigationGestures = true
        webView.allowsLinkPreview = false
        webView.isOpaque = false
        webView.backgroundColor = .systemBackground
        webView.scrollView.backgroundColor = .systemBackground
        // Let the page handle safe areas itself (env(safe-area-inset-*)).
        webView.scrollView.contentInsetAdjustmentBehavior = .never
        #if DEBUG
        webView.isInspectable = true // Safari > Develop menu
        #endif

        // Pull to refresh works even when the page itself is shorter than the screen.
        webView.scrollView.alwaysBounceVertical = true
        let refresh = UIRefreshControl()
        refresh.addTarget(context.coordinator, action: #selector(Coordinator.pulledToRefresh(_:)), for: .valueChanged)
        webView.scrollView.refreshControl = refresh

        context.coordinator.webView = webView
        webView.load(URLRequest(url: url))
        return webView
    }

    func updateUIView(_ webView: WKWebView, context: Context) {
        context.coordinator.parent = self
        if context.coordinator.lastReloadToken != reloadToken {
            context.coordinator.lastReloadToken = reloadToken
            context.coordinator.reload()
        }
    }

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
        var parent: WebView
        weak var webView: WKWebView?
        var lastReloadToken: Int

        init(parent: WebView) {
            self.parent = parent
            self.lastReloadToken = parent.reloadToken
        }

        // MARK: Loading

        func reload() {
            guard let webView else { return }
            if let current = webView.url, isAppURL(current) {
                webView.reload()
            } else {
                webView.load(URLRequest(url: parent.url))
            }
        }

        @objc func pulledToRefresh(_ sender: UIRefreshControl) {
            reload()
        }

        private func endRefreshing() {
            webView?.scrollView.refreshControl?.endRefreshing()
        }

        /// Same host and port as the configured server = part of the app; anything else is
        /// an external link.
        private func isAppURL(_ url: URL) -> Bool {
            guard let scheme = url.scheme?.lowercased() else { return false }
            if scheme == "about" || scheme == "blob" || scheme == "data" { return true }
            guard scheme == "http" || scheme == "https" else { return false }
            return url.host?.lowercased() == parent.url.host?.lowercased()
                && effectivePort(url) == effectivePort(parent.url)
        }

        private func effectivePort(_ url: URL) -> Int {
            if let port = url.port { return port }
            return url.scheme?.lowercased() == "https" ? 443 : 80
        }

        private func openExternally(_ url: URL) {
            UIApplication.shared.open(url, options: [:], completionHandler: nil)
        }

        // MARK: WKNavigationDelegate

        func webView(
            _ webView: WKWebView,
            decidePolicyFor navigationAction: WKNavigationAction,
            decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
        ) {
            guard let url = navigationAction.request.url else {
                decisionHandler(.allow)
                return
            }
            if isAppURL(url) {
                decisionHandler(.allow)
                return
            }
            // Embedded frames from elsewhere (none expected) load in place; top-level
            // navigations to other sites, mailto:, tel: and the like leave the app.
            if let frame = navigationAction.targetFrame, !frame.isMainFrame {
                decisionHandler(.allow)
                return
            }
            openExternally(url)
            decisionHandler(.cancel)
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            endRefreshing()
            parent.loadError = nil
        }

        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
            endRefreshing()
            if Self.isBenign(error) { return }
            parent.loadError = error.localizedDescription
        }

        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
            // The page had already started showing; keep it and just stop the spinner.
            endRefreshing()
        }

        func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
            // iOS reclaimed the web process (memory pressure while backgrounded): reload.
            reload()
        }

        private static func isBenign(_ error: Error) -> Bool {
            let ns = error as NSError
            if ns.domain == NSURLErrorDomain && ns.code == NSURLErrorCancelled { return true }
            // "Frame load interrupted": we cancelled a navigation to hand a link to Safari.
            if ns.domain == "WebKitErrorDomain" && ns.code == 102 { return true }
            return false
        }

        // MARK: WKUIDelegate

        /// target="_blank" and window.open(): same-site loads here, other sites go to Safari.
        func webView(
            _ webView: WKWebView,
            createWebViewWith configuration: WKWebViewConfiguration,
            for navigationAction: WKNavigationAction,
            windowFeatures: WKWindowFeatures
        ) -> WKWebView? {
            if let url = navigationAction.request.url {
                if isAppURL(url) {
                    webView.load(navigationAction.request)
                } else {
                    openExternally(url)
                }
            }
            return nil
        }

        // Without these, WKWebView silently ignores alert() and answers false to confirm().

        func webView(
            _ webView: WKWebView,
            runJavaScriptAlertPanelWithMessage message: String,
            initiatedByFrame frame: WKFrameInfo,
            completionHandler: @escaping () -> Void
        ) {
            let alert = UIAlertController(title: nil, message: message, preferredStyle: .alert)
            alert.addAction(UIAlertAction(title: "OK", style: .default) { _ in completionHandler() })
            if !present(alert, over: webView) { completionHandler() }
        }

        func webView(
            _ webView: WKWebView,
            runJavaScriptConfirmPanelWithMessage message: String,
            initiatedByFrame frame: WKFrameInfo,
            completionHandler: @escaping (Bool) -> Void
        ) {
            let alert = UIAlertController(title: nil, message: message, preferredStyle: .alert)
            alert.addAction(UIAlertAction(title: "Cancel", style: .cancel) { _ in completionHandler(false) })
            alert.addAction(UIAlertAction(title: "OK", style: .default) { _ in completionHandler(true) })
            if !present(alert, over: webView) { completionHandler(false) }
        }

        func webView(
            _ webView: WKWebView,
            runJavaScriptTextInputPanelWithPrompt prompt: String,
            defaultText: String?,
            initiatedByFrame frame: WKFrameInfo,
            completionHandler: @escaping (String?) -> Void
        ) {
            let alert = UIAlertController(title: nil, message: prompt, preferredStyle: .alert)
            alert.addTextField { field in field.text = defaultText }
            alert.addAction(UIAlertAction(title: "Cancel", style: .cancel) { _ in completionHandler(nil) })
            alert.addAction(UIAlertAction(title: "OK", style: .default) { [weak alert] _ in
                completionHandler(alert?.textFields?.first?.text ?? "")
            })
            if !present(alert, over: webView) { completionHandler(nil) }
        }

        private func present(_ controller: UIViewController, over view: UIView) -> Bool {
            guard var top = view.window?.rootViewController else { return false }
            while let presented = top.presentedViewController {
                top = presented
            }
            top.present(controller, animated: true)
            return true
        }
    }
}
