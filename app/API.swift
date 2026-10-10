// The app's link to the local MySearch service (http://127.0.0.1:<port>). The port and token come from the
// service's config.json, read on every call so a restarted service is picked up. The token is never printed.
// The app only does GET requests here.

import Foundation

/// The service's port and token. MSE_PORT in the environment overrides the port (for development).
struct ServiceConfig {
    let port: Int
    let token: String

    static let fileURL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/MySearch/config.json")

    static func load() -> ServiceConfig? {
        // Test hook: MSE_API_TOKEN (with MSE_API_BASE) is used instead of the service's config.json, which is then
        // never opened, so a snapshot against a development instance cannot send the real token anywhere.
        if let t = ProcessInfo.processInfo.environment["MSE_API_TOKEN"], !t.isEmpty {
            return ServiceConfig(port: Int(ProcessInfo.processInfo.environment["MSE_PORT"] ?? "") ?? 8737, token: t)
        }
        guard let data = try? Data(contentsOf: fileURL),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let token = obj["token"] as? String, !token.isEmpty else { return nil }
        let fromEnv = Int(ProcessInfo.processInfo.environment["MSE_PORT"] ?? "")
        let fromFile = (obj["port"] as? NSNumber)?.intValue
        return ServiceConfig(port: fromEnv ?? fromFile ?? 8737, token: token)
    }
}

enum APIError: Error, LocalizedError {
    case noConfig
    case http(Int)

    var errorDescription: String? {
        switch self {
        case .noConfig: return "The search service is not set up (no config.json)."
        case .http(let code): return "The search service answered with HTTP \(code)."
        }
    }
}

/// A MySearch collection (a folder the service indexes), as /api/collections lists it.
struct IndexedCollection: Hashable {
    let name: String
    let path: String
    let enabled: Bool
    let exists: Bool
    let files: Int

    var url: URL { URL(fileURLWithPath: path) }
}

/// A named group of collections. The service may return none; the sidebar hides the group list then.
struct IndexedGroup: Hashable {
    let name: String
    let members: [String]          // collection names
}

struct IndexedLibrary {
    let collections: [IndexedCollection]
    let groups: [IndexedGroup]
}

struct ServiceStatus {
    let version: String
    let uptimeSeconds: Int
}

final class MSEClient {
    static let shared = MSEClient()

    /// Base URL of the local service, from the config (or MSE_PORT). 8737 when there is no config yet.
    var port: Int { ServiceConfig.load()?.port ?? Int(ProcessInfo.processInfo.environment["MSE_PORT"] ?? "") ?? 8737 }

    /// MSE_API_BASE (test hook, e.g. "http://127.0.0.1:18737" for a stub server used by snapshots) replaces the
    /// service's base URL everywhere in the app, including the web pages, so no snapshot reaches the live service.
    static var baseOverride: URL? {
        ProcessInfo.processInfo.environment["MSE_API_BASE"].flatMap { URL(string: $0) }
    }

    /// The root of the service: the override when MSE_API_BASE is set, else the configured local port.
    var root: URL {
        if let base = MSEClient.baseOverride { return base }
        return URL(string: "http://127.0.0.1:\(port)")!
    }

    /// The web UI at the root of the service. The service serves the page only to a request that carries the token,
    /// so the URL carries it as `t`. This is a page load, not an API call: WebKit sends a plain request for a page and
    /// reloads (⌘R, location.reload) do not resend custom headers, so `t` is the page's only credential here.
    func pageURL() -> URL {
        var c = URLComponents(url: root, resolvingAgainstBaseURL: false) ?? URLComponents()
        c.path = "/"
        c.queryItems = [URLQueryItem(name: "t", value: ServiceConfig.load()?.token ?? "")]
        return c.url ?? URL(string: "http://127.0.0.1:\(port)/")!
    }

    /// The Quick Search page (the panel's own page, /?mini=1). It is a page like the web UI, so it carries the token too.
    func miniURL() -> URL {
        var c = URLComponents(url: root, resolvingAgainstBaseURL: false) ?? URLComponents()
        c.path = "/"
        c.queryItems = [URLQueryItem(name: "mini", value: "1"), URLQueryItem(name: "t", value: ServiceConfig.load()?.token ?? "")]
        return c.url ?? URL(string: "http://127.0.0.1:\(port)/?mini=1")!
    }

    /// GET `path` with optional query items. Throws APIError.http(code) on anything but 200 (404 = not installed).
    func get(_ path: String, query: [URLQueryItem] = []) async throws -> Any {
        let cfg = ServiceConfig.load()
        guard cfg != nil || MSEClient.baseOverride != nil else { throw APIError.noConfig }
        var c = URLComponents(url: root, resolvingAgainstBaseURL: false) ?? URLComponents()
        c.path = path
        c.queryItems = query.isEmpty ? nil : query
        var req = URLRequest(url: c.url ?? root)
        req.httpMethod = "GET"
        req.timeoutInterval = 60                       // /api/collections can take several seconds
        req.setValue(cfg?.token ?? "", forHTTPHeaderField: "X-MSE-Token")
        let (data, resp) = try await URLSession.shared.data(for: req)
        guard let http = resp as? HTTPURLResponse, http.statusCode == 200 else {
            throw APIError.http((resp as? HTTPURLResponse)?.statusCode ?? 0)
        }
        return try JSONSerialization.jsonObject(with: data)
    }

    func library() async throws -> IndexedLibrary {
        guard let obj = try await get("/api/collections") as? [String: Any] else { return IndexedLibrary(collections: [], groups: []) }
        let rows = (obj["collections"] as? [[String: Any]]) ?? []
        let collections: [IndexedCollection] = rows.compactMap { r in
            guard let name = r["name"] as? String, let path = r["path"] as? String else { return nil }
            let stats = r["stats"] as? [String: Any]
            return IndexedCollection(name: name, path: path,
                                     enabled: (r["enabled"] as? Bool) ?? true,
                                     exists: (r["exists"] as? Bool) ?? true,
                                     files: (stats?["files"] as? NSNumber)?.intValue ?? 0)
        }
        // Groups are read leniently: a name, and the member collections under "collections" or "members".
        let groups: [IndexedGroup] = ((obj["groups"] as? [[String: Any]]) ?? []).compactMap { g in
            guard let name = g["name"] as? String else { return nil }
            let raw = (g["collections"] as? [Any]) ?? (g["members"] as? [Any]) ?? []
            let members = raw.compactMap { ($0 as? String) ?? (($0 as? [String: Any])?["name"] as? String) }
            return IndexedGroup(name: name, members: members)
        }
        return IndexedLibrary(collections: collections, groups: groups)
    }

    func status() async throws -> ServiceStatus {
        guard let obj = try await get("/api/status") as? [String: Any] else { throw APIError.http(0) }
        return ServiceStatus(version: (obj["version"] as? String) ?? "", uptimeSeconds: (obj["uptime_s"] as? NSNumber)?.intValue ?? 0)
    }
}

/// A refusal from the service with its own text (the "error" of a 400, e.g. "ValueError: a group needs at least one
/// collection"). Used by the group editor, which shows the text as it is.
struct MSEServerMessage: Error, LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

extension MSEClient {
    /// POST `path` with a JSON body and return the answer's JSON object. Anything but 200 throws MSEServerMessage with
    /// the service's own "error" text, or the HTTP status when it sends none. Sends the token the same way `get` does.
    func postJSON(_ path: String, body: [String: Any]) async throws -> [String: Any] {
        let cfg = ServiceConfig.load()
        guard cfg != nil || MSEClient.baseOverride != nil else { throw APIError.noConfig }
        var c = URLComponents(url: root, resolvingAgainstBaseURL: false) ?? URLComponents()
        c.path = path
        var req = URLRequest(url: c.url ?? root)
        req.httpMethod = "POST"
        req.timeoutInterval = 60
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue(cfg?.token ?? "", forHTTPHeaderField: "X-MSE-Token")
        // One request per connection: the service closes after each answer, and a pooled socket reused after that
        // was seen to answer a later POST with a stray "501 Unsupported method" (the dev service's /remove routes do not read the body).
        req.setValue("close", forHTTPHeaderField: "Connection")
        req.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, resp) = try await URLSession.shared.data(for: req)
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        guard code == 200 else {
            if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any], let msg = obj["error"] as? String {
                throw MSEServerMessage(message: msg)
            }
            throw MSEServerMessage(message: APIError.http(code).localizedDescription)
        }
        return (try JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
    }
}

/// A streaming POST that the service answered with something other than 200 (409 = Ask is switched off).
struct StreamHTTPError: Error, LocalizedError {
    let status: Int
    let body: String
    var errorDescription: String? { "The search service answered with HTTP \(status)." }
}

extension MSEClient {
    /// POST a JSON body and yield the answer line by line as it arrives (application/x-ndjson: POST /api/ask).
    /// Cancelling the consuming task (or breaking out of the loop) closes the connection, which stops the service's
    /// generation. A non-200 answer throws StreamHTTPError with the status and the start of the body.
    func streamNDJSON(_ path: String, body: [String: Any]) -> AsyncThrowingStream<String, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    let cfg = ServiceConfig.load()
                    guard cfg != nil || MSEClient.baseOverride != nil else { throw APIError.noConfig }
                    var c = URLComponents(url: self.root, resolvingAgainstBaseURL: false) ?? URLComponents()
                    c.path = path
                    var req = URLRequest(url: c.url ?? self.root)
                    req.httpMethod = "POST"
                    req.timeoutInterval = 300                    // a local model can take a while to load before the first token
                    req.setValue("application/json", forHTTPHeaderField: "Content-Type")
                    req.setValue(cfg?.token ?? "", forHTTPHeaderField: "X-MSE-Token")
                    req.httpBody = try JSONSerialization.data(withJSONObject: body)
                    let (bytes, resp) = try await URLSession.shared.bytes(for: req)
                    let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
                    guard code == 200 else {
                        var data = Data()
                        for try await b in bytes { data.append(b); if data.count >= 4096 { break } }
                        throw StreamHTTPError(status: code, body: String(decoding: data, as: UTF8.self))
                    }
                    for try await line in bytes.lines {
                        try Task.checkCancellation()
                        if !line.isEmpty { continuation.yield(line) }
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }
}
