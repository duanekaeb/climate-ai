import SwiftUI

/// The one setting this app has: where the Climate AI server lives.
enum ServerConfig {
    static let storageKey = "serverURL"

    /// Turns what the user typed into a base URL without a trailing slash.
    /// "192.168.1.20:8470" -> http://192.168.1.20:8470, "climate.example.home" -> https://...
    /// Plain http is only allowed by iOS for local addresses (IP addresses, *.local and
    /// single-label names), so a bare dotted host name defaults to https.
    static func normalize(_ raw: String) -> URL? {
        var text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        if !text.contains("://") {
            text = (looksLocal(text) ? "http://" : "https://") + text
        }
        while text.hasSuffix("/") {
            text.removeLast()
        }
        guard let url = URL(string: text),
              let scheme = url.scheme?.lowercased(), scheme == "http" || scheme == "https",
              let host = url.host, !host.isEmpty
        else { return nil }
        return url
    }

    static func healthURL(for base: URL) -> URL {
        base.appendingPathComponent("api").appendingPathComponent("health")
    }

    private static func looksLocal(_ hostAndMore: String) -> Bool {
        let host = hostAndMore
            .split(separator: "/", maxSplits: 1, omittingEmptySubsequences: false).first
            .map { String($0) } ?? hostAndMore
        let name = (host.split(separator: ":").first.map { String($0) } ?? host).lowercased()
        if name.hasSuffix(".local") || !name.contains(".") { return true }
        // IPv4 literal
        let parts = name.split(separator: ".")
        return parts.count == 4 && parts.allSatisfy { UInt8($0) != nil }
    }
}

/// Response of GET /api/health on the Climate AI server.
struct HealthResponse: Decodable {
    let ok: Bool
    let version: String?
    let db: Bool?
}

enum HealthCheck {
    static func run(base: URL) async throws -> HealthResponse {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 8
        config.waitsForConnectivity = false
        let session = URLSession(configuration: config)
        defer { session.finishTasksAndInvalidate() }
        var request = URLRequest(url: ServerConfig.healthURL(for: base))
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw URLError(.badServerResponse) }
        guard http.statusCode == 200 else {
            throw HealthError.status(http.statusCode)
        }
        do {
            return try JSONDecoder().decode(HealthResponse.self, from: data)
        } catch {
            throw HealthError.notClimateAI
        }
    }
}

enum HealthError: LocalizedError {
    case status(Int)
    case notClimateAI

    var errorDescription: String? {
        switch self {
        case .status(let code):
            return "The server answered HTTP \(code) for /api/health."
        case .notClimateAI:
            return "Something answered, but it does not look like Climate AI."
        }
    }
}

struct SettingsView: View {
    let isFirstRun: Bool

    @AppStorage(ServerConfig.storageKey) private var savedURL: String = ""
    @Environment(\.dismiss) private var dismiss
    @State private var draft: String = ""
    @State private var status: Status = .idle

    enum Status: Equatable {
        case idle
        case checking
        case ok(String)
        case failed(String)
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("http://192.168.1.20:8470", text: $draft)
                        .keyboardType(.URL)
                        .textContentType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .submitLabel(.go)
                        .onSubmit { Task { await testAndSave() } }
                        .onChange(of: draft) { _, _ in
                            if status != .checking { status = .idle }
                        }
                } header: {
                    Text("Server")
                } footer: {
                    Text("The address you open Climate AI at in a browser, e.g. https://climate.example.home behind your nginx, or http://<server-ip>:8470 on your home network.")
                }

                Section {
                    Button {
                        Task { await testAndSave() }
                    } label: {
                        HStack {
                            Text("Test and save")
                            Spacer()
                            if status == .checking {
                                ProgressView()
                            }
                        }
                    }
                    .disabled(status == .checking || ServerConfig.normalize(draft) == nil)

                    statusRow

                    if case .failed = status, ServerConfig.normalize(draft) != nil {
                        Button("Save anyway") { save() }
                    }
                }

                Section {
                    Label("Shake your phone to come back here.", systemImage: "iphone.radiowaves.left.and.right")
                    Label("If iOS asks for Local Network access, allow it, then test again.", systemImage: "network")
                    Label("Away from home, use Tailscale. Don't expose the server to the internet.", systemImage: "lock.shield")
                } header: {
                    Text("Tips")
                }
            }
            .navigationTitle(isFirstRun ? "Climate AI" : "Server")
            .toolbar {
                if !isFirstRun {
                    ToolbarItem(placement: .cancellationAction) {
                        Button("Close") { dismiss() }
                    }
                }
            }
            .onAppear {
                if draft.isEmpty { draft = savedURL }
            }
        }
    }

    @ViewBuilder
    private var statusRow: some View {
        switch status {
        case .idle:
            EmptyView()
        case .checking:
            Text("Checking…").foregroundStyle(.secondary)
        case .ok(let message):
            Label(message, systemImage: "checkmark.circle.fill").foregroundStyle(.green)
        case .failed(let message):
            Label(message, systemImage: "exclamationmark.triangle.fill").foregroundStyle(.orange)
        }
    }

    @MainActor
    private func testAndSave() async {
        guard let base = ServerConfig.normalize(draft) else {
            status = .failed("Enter an address like http://192.168.1.20:8470")
            return
        }
        status = .checking
        do {
            let health = try await HealthCheck.run(base: base)
            if health.ok {
                status = .ok("Connected" + (health.version.map { " (v\($0))" } ?? ""))
                save()
            } else {
                status = .failed("Climate AI answered, but its database is not ready yet.")
            }
        } catch {
            status = .failed(error.localizedDescription)
        }
    }

    private func save() {
        guard let base = ServerConfig.normalize(draft) else { return }
        draft = base.absoluteString
        savedURL = base.absoluteString
        if !isFirstRun { dismiss() }
    }
}

#Preview {
    SettingsView(isFirstRun: true)
}
