// Update check: tells the user that a newer release exists. It never downloads or installs anything and uses no third-party
// framework.
//
// What it does: at most once a day, and only while Settings > Mac App > "Check for updates" is on (the default), it asks
//   GET https://api.github.com/repos/HeyitSaif/mysearch/releases/latest
// and compares the answer's tag_name with this app's CFBundleShortVersionString. If the release is newer, Settings and the
// menu-bar item say "Update available — <version>" and a button opens the release page in the browser.
//
// When: any reply from GitHub completes the day's check, whatever it says (a release, 404 because the repository has no release
// yet, 403 because of a rate limit, a 5xx, an answer that is not a release): the next check is a day away. Only a request that got
// no reply at all (offline, timeout, DNS) is tried again, and then no sooner than an hour later. So the request goes out at most
// once a day, even when GitHub keeps answering with an error.
//
// What the request carries: the method and the URL above, and the headers `User-Agent: MySearch/<version>` and
// `Accept-Language: en` (a fixed value, so the system's language list is not sent). No cookie, no credential, no identifier,
// no query string, no body; the session is ephemeral (nothing is cached or stored by it). GitHub sees the Mac's IP address,
// as any website does. It is one of the only three outbound requests MySearch makes: this, the model downloads that the
// user starts (Hugging Face), and the cloud answer of the optional Ask. Documented in README.md and SECURITY.md.
//
// MSE_NO_UPDATE_CHECK=1 in the environment switches the check off entirely (tests, CI).

import Combine
import Foundation
#if canImport(AppKit)
import AppKit
#endif

// MARK: - versions

/// Comparing release tags. Pure, so a test can call it without the app.
enum UpdateVersion {
    struct Parsed: Equatable {
        let core: [Int]          // 0.1.2 -> [0, 1, 2]
        let pre: [String]        // 0.2.0-rc.1 -> ["rc", "1"]
    }

    /// "v0.1.3", "0.1.3", "0.2.0-rc.1", "1.0.0+build5" -> its parts. nil for anything else (a tag the check does not understand
    /// is ignored, never offered).
    static func parse(_ text: String) -> Parsed? {
        var t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty, t.count <= 40 else { return nil }
        if t.hasPrefix("v") || t.hasPrefix("V") { t.removeFirst() }
        if let plus = t.firstIndex(of: "+") { t = String(t[..<plus]) }          // build metadata does not count
        var pre: [String] = []
        if let dash = t.firstIndex(of: "-") {
            let tail = String(t[t.index(after: dash)...])
            t = String(t[..<dash])
            pre = tail.split(whereSeparator: { $0 == "." || $0 == "-" }).map(String.init)
            if pre.isEmpty || pre.contains(where: { $0.range(of: #"^[0-9A-Za-z]+$"#, options: .regularExpression) == nil }) { return nil }
        }
        let parts = t.split(separator: ".", omittingEmptySubsequences: false)
        guard (1...4).contains(parts.count) else { return nil }
        var core: [Int] = []
        for p in parts {
            guard !p.isEmpty, p.count <= 6, p.allSatisfy({ $0.isASCII && $0.isNumber }), let n = Int(p) else { return nil }
            core.append(n)
        }
        return Parsed(core: core, pre: pre)
    }

    /// A tag worth offering and safe to put into a release URL.
    static func isValidTag(_ text: String) -> Bool { parse(text) != nil && text.range(of: #"^[vV]?[0-9A-Za-z.+\-]+$"#, options: .regularExpression) != nil }

    /// Semantic-version order: the numbers first (a missing one is 0), and a pre-release ("-rc.1") is older than the same
    /// version without one. False when either side is not a version.
    static func isNewer(_ remote: String, than local: String) -> Bool {
        guard let r = parse(remote), let l = parse(local) else { return false }
        let n = max(r.core.count, l.core.count)
        for i in 0..<n {
            let a = i < r.core.count ? r.core[i] : 0, b = i < l.core.count ? l.core[i] : 0
            if a != b { return a > b }
        }
        if r.pre.isEmpty != l.pre.isEmpty { return r.pre.isEmpty }          // 1.0.0 is newer than 1.0.0-rc.1
        for (a, b) in zip(r.pre, l.pre) where a != b {
            switch (Int(a), Int(b)) {
            case let (x?, y?): return x > y
            case (.some, nil): return false                                  // a number is older than a word
            case (nil, .some): return true
            default: return a > b
            }
        }
        return r.pre.count > l.pre.count
    }

    /// The tag_name of a GitHub "latest release" answer, or nil when the answer is not usable: not JSON, a draft or a
    /// pre-release, or a tag that is not a version.
    static func tag(fromReleaseJSON data: Data) -> String? {
        guard data.count < 1_000_000,
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              (obj["draft"] as? Bool) != true, (obj["prerelease"] as? Bool) != true,
              let tag = obj["tag_name"] as? String, isValidTag(tag) else { return nil }
        return tag
    }
}

/// What a reply from GitHub means for the check.
enum UpdateOutcome: Equatable {
    case release(String)       // a usable latest release: its tag
    case noRelease             // 404: the repository has no published release yet
    case unusable(String)      // another status, or an answer without a usable tag; the string is for the user
}

extension UpdateVersion {
    /// Pure: the meaning of an HTTP status and body from the "latest release" endpoint.
    static func outcome(status: Int, data: Data) -> UpdateOutcome {
        switch status {
        case 200:
            if let tag = tag(fromReleaseJSON: data) { return .release(tag) }
            return .unusable("GitHub's answer was not a usable release.")
        case 404: return .noRelease
        case 403, 429: return .unusable("GitHub is limiting requests right now.")
        default: return .unusable("GitHub answered with status \(status).")
        }
    }
}

/// When the next check is due. Pure.
enum UpdateSchedule {
    static let interval: TimeInterval = 24 * 3600        // one check a day after any reply from GitHub
    static let retry: TimeInterval = 3600                // after no reply at all (offline, timeout), at most once an hour

    /// lastCompleted: the last time GitHub answered, with whatever status. lastAttempt: the last time a request was sent.
    static func isDue(now: Date, lastCompleted: Date?, lastAttempt: Date?, enabled: Bool) -> Bool {
        guard enabled else { return false }
        if let lastCompleted, now.timeIntervalSince(lastCompleted) < interval { return false }
        if let lastAttempt, now.timeIntervalSince(lastAttempt) < retry { return false }
        return true
    }
}

// MARK: - the checker

@MainActor
final class UpdateChecker: ObservableObject {
    static let shared = UpdateChecker()
    static let releaseAPI = URL(string: "https://api.github.com/repos/HeyitSaif/mysearch/releases/latest")!
    static let releasesPage = "https://github.com/HeyitSaif/mysearch/releases"

    private enum Keys {
        static let enabled = "UpdateCheckEnabled"
        static let lastCompleted = "UpdateLastCompleted"
        static let lastAttempt = "UpdateLastAttempt"
        static let latest = "UpdateLatestTag"
    }

    /// How a request is sent. The real one is an ephemeral URLSession; a test passes its own and no network is used.
    typealias Fetch = (URLRequest) async throws -> (Data, URLResponse)

    let currentVersion: String
    private let defaults: UserDefaults
    private let fetch: Fetch
    private var timer: Timer?

    /// The newest release when it is newer than this app (e.g. "0.2.0"), else nil.
    @Published private(set) var available: String?
    @Published private(set) var lastChecked: Date?
    @Published private(set) var checking = false
    @Published private(set) var lastError: String?
    /// GitHub answered 404: this repository has no published release yet (not an error).
    @Published private(set) var noRelease = false
    /// Settings > Mac App > Check for updates. On by default.
    @Published var enabled: Bool {
        didSet {
            guard enabled != oldValue else { return }
            defaults.set(enabled, forKey: Keys.enabled)
            if enabled { Task { await checkIfDue() } }
        }
    }
    /// The menu-bar item listens here.
    var onChange: ((String?) -> Void)?

    static var disabledByEnvironment: Bool { ProcessInfo.processInfo.environment["MSE_NO_UPDATE_CHECK"] == "1" }

    init(defaults: UserDefaults = .standard,
         version: String = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "0",
         fetch: Fetch? = nil) {
        self.defaults = defaults
        self.fetch = fetch ?? { request in try await UpdateChecker.session().data(for: request) }
        self.currentVersion = version
        enabled = (defaults.object(forKey: Keys.enabled) as? Bool) ?? true
        lastChecked = defaults.object(forKey: Keys.lastCompleted) as? Date
        if let tag = defaults.string(forKey: Keys.latest), UpdateVersion.isNewer(tag, than: version) {
            available = Self.display(tag)
        }
    }

    /// Starts the daily schedule: a first look 30 seconds after launch, then every hour whether one is due.
    func start() {
        guard !Self.disabledByEnvironment, timer == nil else { return }
        onChange?(available)
        timer = Timer.scheduledTimer(withTimeInterval: 3600, repeats: true) { [weak self] _ in
            Task { @MainActor in await self?.checkIfDue() }
        }
        Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 30_000_000_000)
            await self?.checkIfDue()
        }
    }

    func checkIfDue() async {
        guard !Self.disabledByEnvironment, !checking else { return }
        let due = UpdateSchedule.isDue(now: Date(), lastCompleted: defaults.object(forKey: Keys.lastCompleted) as? Date,
                                       lastAttempt: defaults.object(forKey: Keys.lastAttempt) as? Date, enabled: enabled)
        if due { await check() }
    }

    /// "Check Now": the user asked, so the daily limit does not apply.
    func checkNow() async {
        guard !Self.disabledByEnvironment, !checking else { return }
        await check()
    }

    private func check() async {
        checking = true
        lastError = nil
        defaults.set(Date(), forKey: Keys.lastAttempt)
        defer { checking = false }
        let data: Data, response: URLResponse
        do {
            (data, response) = try await fetch(request())
        } catch {
            lastError = "Could not reach GitHub just now."       // no reply at all: tried again within the hour, not sooner
            return
        }
        // a reply of any kind completes today's check: the next one is a day away, whatever the reply says
        defaults.set(Date(), forKey: Keys.lastCompleted)
        lastChecked = Date()
        guard let http = response as? HTTPURLResponse else { lastError = "GitHub's answer could not be read."; return }
        noRelease = false
        switch UpdateVersion.outcome(status: http.statusCode, data: data) {
        case .release(let tag):
            defaults.set(tag, forKey: Keys.latest)
            setAvailable(UpdateVersion.isNewer(tag, than: currentVersion) ? Self.display(tag) : nil)
        case .noRelease:
            defaults.removeObject(forKey: Keys.latest)
            noRelease = true
            setAvailable(nil)
        case .unusable(let why):
            lastError = why
        }
    }

    private func setAvailable(_ version: String?) {
        guard version != available else { return }
        available = version
        onChange?(version)
    }

    /// The request, built in one place so what it carries can be read at a glance (see the top of this file).
    func request() -> URLRequest {
        var r = URLRequest(url: Self.releaseAPI, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 15)
        r.httpMethod = "GET"
        r.httpShouldHandleCookies = false
        r.setValue("MySearch/\(currentVersion)", forHTTPHeaderField: "User-Agent")
        r.setValue("en", forHTTPHeaderField: "Accept-Language")
        return r
    }

    fileprivate static func session() -> URLSession {
        let c = URLSessionConfiguration.ephemeral          // no cache, cookies or credentials are kept
        c.httpCookieStorage = nil
        c.urlCredentialStorage = nil
        c.urlCache = nil
        c.waitsForConnectivity = false
        c.tlsMinimumSupportedProtocolVersion = .TLSv12
        return URLSession(configuration: c)
    }

    /// "v0.2.0" -> "0.2.0"
    static func display(_ tag: String) -> String { tag.hasPrefix("v") || tag.hasPrefix("V") ? String(tag.dropFirst()) : tag }

    /// The release page for the version on offer: built here from the validated tag, never taken from the answer.
    var releaseURL: URL {
        guard let v = available ?? defaults.string(forKey: Keys.latest).map(Self.display),
              let tag = v.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed),
              let url = URL(string: "https://github.com/HeyitSaif/mysearch/releases/tag/v\(tag)") else {
            return URL(string: Self.releasesPage)!
        }
        return url
    }

    func openReleasePage() {
        #if canImport(AppKit)
        NSWorkspace.shared.open(releaseURL)
        #endif
    }
}
