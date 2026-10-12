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

    /// A page URL made earlier, with the token the service has now. The config may have been written or rotated since the
    /// URL was made, so the `t` item is replaced and every other item (the path, mini=1) is kept. The token is never logged.
    func refreshingToken(_ url: URL) -> URL {
        guard var c = URLComponents(url: url, resolvingAgainstBaseURL: false),
              let items = c.queryItems, items.contains(where: { $0.name == "t" }) else { return url }
        let token = ServiceConfig.load()?.token ?? ""
        c.queryItems = items.map { $0.name == "t" ? URLQueryItem(name: "t", value: token) : $0 }
        return c.url ?? url
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

extension MSEClient {
    /// PATCH or DELETE `path` (with an optional JSON body) and return the answer's JSON object, or [:] for an empty
    /// answer. Anything but 200 throws MSEServerMessage with the service's "error" text, as postJSON does. Used by the
    /// saved searches (SavedSearches.swift); the snapshot states never reach this.
    func sendJSON(_ method: String, _ path: String, body: [String: Any]? = nil) async throws -> [String: Any] {
        let cfg = ServiceConfig.load()
        guard cfg != nil || MSEClient.baseOverride != nil else { throw APIError.noConfig }
        var c = URLComponents(url: root, resolvingAgainstBaseURL: false) ?? URLComponents()
        c.path = path
        var req = URLRequest(url: c.url ?? root)
        req.httpMethod = method
        req.timeoutInterval = 60
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue(cfg?.token ?? "", forHTTPHeaderField: "X-MSE-Token")
        req.setValue("close", forHTTPHeaderField: "Connection")
        if let body { req.httpBody = try JSONSerialization.data(withJSONObject: body) }
        let (data, resp) = try await URLSession.shared.data(for: req)
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        guard code == 200 else {
            if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any], let msg = obj["error"] as? String {
                throw MSEServerMessage(message: msg)
            }
            throw MSEServerMessage(message: APIError.http(code).localizedDescription)
        }
        return (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
    }
}

// MARK: - Quick Search (QuickSearchService.swift)

/// The service's config for Quick Search, read from its file once and again only after a 401 (the token was rotated).
/// ServiceConfig.load() reads a file, so it must not run on every keystroke.
enum QuickConfig {
    private static let lock = NSLock()
    nonisolated(unsafe) private static var cached: ServiceConfig?
    /// True until the config has been read: at the first use, after `markStale()`, and after a failed attempt.
    nonisolated(unsafe) private static var needsLoad = true

    /// The cached config, read from the file when it has not been read yet or was marked stale. Runs off the main actor.
    static func current() -> ServiceConfig? {
        lock.lock()
        defer { lock.unlock() }
        if needsLoad {
            cached = ServiceConfig.load()
            needsLoad = false
        }
        return cached
    }

    static func reload() -> ServiceConfig? {
        lock.lock()
        defer { lock.unlock() }
        cached = ServiceConfig.load()
        needsLoad = false
        return cached
    }

    /// Marks the cached config stale without reading the file (no I/O here, so it is safe on the main actor): the next
    /// request reads the file again, off the main actor. Retry uses it, so a service that has started since is found.
    static func markStale() {
        lock.lock()
        defer { lock.unlock() }
        needsLoad = true
    }
}

extension MSEClient {
    /// GET for Quick Search: the cached config, its own timeout, and one retry with the config re-read after a 401.
    /// Runs off the main actor (it is nonisolated); the caller's task cancellation cancels the request.
    func getQuick(_ path: String, query: [URLQueryItem], timeout: TimeInterval) async throws -> Any {
        var lastError: Error = APIError.noConfig
        for attempt in 0..<2 {
            // The first attempt uses the cached config. The second re-reads the file, after no config, a 401 or a refused
            // connection (the service may have started, rotated its token or moved its port since the cache was read).
            let cfg = attempt == 0 ? QuickConfig.current() : QuickConfig.reload()
            guard cfg != nil || MSEClient.baseOverride != nil else {
                lastError = APIError.noConfig
                continue
            }
            let base = MSEClient.baseOverride ?? URL(string: "http://127.0.0.1:\(cfg?.port ?? 8737)")!
            var c = URLComponents(url: base, resolvingAgainstBaseURL: false) ?? URLComponents()
            c.path = path
            c.queryItems = query.isEmpty ? nil : query
            guard let url = c.url else { throw APIError.http(0) }
            var req = URLRequest(url: url)
            req.httpMethod = "GET"
            req.timeoutInterval = timeout
            req.setValue(cfg?.token ?? "", forHTTPHeaderField: "X-MSE-Token")
            do {
                let (data, resp) = try await URLSession.shared.data(for: req)
                let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
                if code == 401 && attempt == 0 {
                    lastError = APIError.http(401)
                    continue
                }
                guard code == 200 else { throw APIError.http(code) }
                return try JSONSerialization.jsonObject(with: data)
            } catch let error as URLError where attempt == 0 && error.code != .cancelled {
                lastError = error
                continue
            }
        }
        throw lastError
    }
}

// MARK: - Whole-disk search (the contract: GET /api/disk/status, GET /api/disk/search, POST /api/disk/rescan, and the
// "wholedisk" key of GET/POST /api/settings). The UI reads these in SettingsWholeDisk.swift, Quick Search and the main window's
// Mac mode. A 404 from /api/disk/* means the service has no whole-disk search: the callers hide their section, with no error.

/// The "wholedisk" settings. The UI always saves the whole object (never a changed key alone).
struct WholeDiskSettings: Equatable {
    var enabled = false
    var roots: [String] = []
    var extraExcludes: [String] = []
    var priority = "low"                  // "low" (only when idle) or "balanced"
    var images = true
    var documents = true
    var ocrImages = true
    var maxTextKB = 8                     // the service's default; it accepts 1 to 64

    init() {}

    init(_ d: [String: Any]) {
        enabled = (d["enabled"] as? Bool) ?? false
        roots = ((d["roots"] as? [Any]) ?? []).compactMap { $0 as? String }
        extraExcludes = ((d["extra_excludes"] as? [Any]) ?? []).compactMap { $0 as? String }
        priority = (d["priority"] as? String) == "balanced" ? "balanced" : "low"
        images = (d["images"] as? Bool) ?? true
        documents = (d["documents"] as? Bool) ?? true
        ocrImages = (d["ocr_images"] as? Bool) ?? true
        maxTextKB = (d["max_text_kb"] as? NSNumber)?.intValue ?? 8
    }

    var json: [String: Any] {
        ["enabled": enabled, "roots": roots, "extra_excludes": extraExcludes, "priority": priority,
         "images": images, "documents": documents, "ocr_images": ocrImages, "max_text_kb": maxTextKB]
    }
}

/// GET /api/disk/status. Counts and state only; the progress bar and the ETA are read from it.
struct DiskStatus: Equatable {
    var enabled = false
    var roots: [String] = []
    var filesSeen = 0
    var filesDescribed = 0
    var imagesLabelled = 0
    var pdfsDescribed = 0
    var pending = 0
    var state = "idle"                    // idle | scanning | describing | paused
    var pausedReason: String?             // nil | user_active | on_battery | disabled
    var ratePerS: Double = 0
    var etaS: Double?
    var lastError: String?

    init() {}

    init(_ d: [String: Any]) {
        enabled = (d["enabled"] as? Bool) ?? false
        roots = ((d["roots"] as? [Any]) ?? []).compactMap { $0 as? String }
        filesSeen = (d["files_seen"] as? NSNumber)?.intValue ?? 0
        filesDescribed = (d["files_described"] as? NSNumber)?.intValue ?? 0
        imagesLabelled = (d["images_labelled"] as? NSNumber)?.intValue ?? 0
        pdfsDescribed = (d["pdfs_described"] as? NSNumber)?.intValue ?? 0
        pending = (d["pending"] as? NSNumber)?.intValue ?? 0
        state = (d["state"] as? String) ?? "idle"
        pausedReason = (d["paused_reason"] as? String).flatMap { $0.isEmpty ? nil : $0 }
        ratePerS = (d["rate_per_s"] as? NSNumber)?.doubleValue ?? 0
        etaS = (d["eta_s"] as? NSNumber)?.doubleValue
        lastError = (d["last_error"] as? String).flatMap { $0.isEmpty ? nil : $0 }
    }
}

/// One whole-disk hit. `snippet` is the service's text as sent (its <mark> tags are read by QuickSearchService.unmark).
struct DiskHit: Sendable, Equatable {
    let path: String
    let name: String
    let kind: String
    let size: Int64?
    let mtime: Date?
    let snippet: String
    let labels: [String]
    let why: String                       // name | label | text | vector

    static func parse(_ d: [String: Any]) -> DiskHit? {
        guard let path = d["path"] as? String, !path.isEmpty else { return nil }
        return DiskHit(path: path,
                       name: (d["name"] as? String) ?? (path as NSString).lastPathComponent,
                       kind: (d["kind"] as? String) ?? "",
                       size: (d["size"] as? NSNumber)?.int64Value,
                       mtime: (d["mtime"] as? NSNumber).map { Date(timeIntervalSince1970: $0.doubleValue) },
                       snippet: (d["snippet"] as? String) ?? "",
                       labels: ((d["labels"] as? [Any]) ?? []).compactMap { $0 as? String },
                       why: (d["why"] as? String) ?? "")
    }
}

struct DiskPage: Sendable {
    let items: [DiskHit]
    let hasMore: Bool
    let nextOffset: Int?
    let tookMs: Int
}

extension MSEClient {
    /// The "wholedisk" key of GET /api/settings, or nil when the service does not send it (the UI is then hidden).
    func wholeDiskSettings() async throws -> WholeDiskSettings? {
        guard let obj = try await get("/api/settings") as? [String: Any], let w = obj["wholedisk"] as? [String: Any] else { return nil }
        return WholeDiskSettings(w)
    }

    /// POST /api/settings with the whole "wholedisk" object. A refusal throws MSEServerMessage with the service's text.
    func saveWholeDisk(_ s: WholeDiskSettings) async throws {
        _ = try await postJSON("/api/settings", body: ["wholedisk": s.json])
    }

    func diskStatus() async throws -> DiskStatus {
        guard let obj = try await get("/api/disk/status") as? [String: Any] else { throw APIError.http(0) }
        return DiskStatus(obj)
    }

    /// GET /api/disk/search, one page. Uses the Quick Search request path (cached config, its own timeout, a retry after 401),
    /// and is cancelled with the calling task.
    func diskSearch(_ q: String, limit: Int, offset: Int, kind: String? = nil) async throws -> DiskPage {
        var items = [URLQueryItem(name: "q", value: q), URLQueryItem(name: "limit", value: String(limit)),
                     URLQueryItem(name: "offset", value: String(offset))]
        if let kind, !kind.isEmpty { items.append(URLQueryItem(name: "kind", value: kind)) }
        let obj = try await getQuick("/api/disk/search", query: items, timeout: 8)
        let d = (obj as? [String: Any]) ?? [:]
        return DiskPage(items: ((d["results"] as? [[String: Any]]) ?? []).compactMap(DiskHit.parse),
                        hasMore: (d["has_more"] as? NSNumber)?.boolValue ?? false,
                        nextOffset: (d["next_offset"] as? NSNumber)?.intValue,
                        tookMs: (d["took_ms"] as? NSNumber)?.intValue ?? 0)
    }

    func diskRescan() async throws {
        _ = try await postJSON("/api/disk/rescan", body: [:])
    }

    /// Deep-indexes a folder: the same POST /api/collections the Folders pane sends (the service names it from the path).
    func deepIndex(path: String) async throws {
        _ = try await postJSON("/api/collections", body: ["path": path])
    }
}
