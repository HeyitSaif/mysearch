// Ask, native: the data and the state behind AskViewController (the Claude Code–style transcript).
// AskModel talks to the local service: GET /api/status, /api/ask/providers, /api/ask/local-models, /api/collections,
// /api/tree, POST /api/settings and the streaming POST /api/ask. Chats are kept on this Mac (see AskChatStore).
// The semantics follow the web Ask in mse/ui/app.js: Cloud/Local is per chat, the folder is fixed per chat, a
// follow-up sends the last three answered turns as history, a model its provider may train on needs a one-time confirmation.

import Cocoa
import SwiftUI

// MARK: - palette (the web Ask's --tm-* colours)

enum AskColors {
    // Native macOS system colours and materials only, so the Ask view follows light and dark mode like Finder.
    private static func sys(_ c: NSColor, alpha: CGFloat? = nil) -> Color {
        Color(nsColor: alpha.map { c.withAlphaComponent($0) } ?? c)
    }
    static let bg = sys(.windowBackgroundColor)            // window chrome: header, composer, sidebar fallback
    static let well = sys(.textBackgroundColor)            // content areas and fields (Finder's white list area)
    static let panel = sys(.controlBackgroundColor)        // grouped blocks: code, pickers
    static let ink = sys(.labelColor)
    static let ink2 = sys(.secondaryLabelColor)
    static let dim = sys(.secondaryLabelColor, alpha: 0.72)
    static let rule = sys(.separatorColor)
    static let prompt = sys(.controlAccentColor)           // the accent colour
    static let run = sys(.tertiaryLabelColor)
    static let ok = sys(.systemGreen)
    static let err = sys(.systemRed)
    static let warn = sys(.systemOrange)
    static let glow = sys(.controlAccentColor, alpha: 0.14)
    static let okSoft = sys(.systemGreen, alpha: 0.16)
    static let warnSoft = sys(.systemOrange, alpha: 0.16)
    static let hover = sys(.labelColor, alpha: 0.07)
    static let selected = sys(.unemphasizedSelectedContentBackgroundColor)
}

// MARK: - data

struct AskSource: Codable, Hashable, Identifiable {
    var n: Int
    var docid: String?
    var collection: String
    var path: String
    var title: String?
    var page: Int?
    var ts: String?
    var snippet: String?
    var id: Int { n }

    static func parse(_ d: [String: Any]) -> AskSource? {
        guard let n = (d["n"] as? NSNumber)?.intValue, let path = d["path"] as? String else { return nil }
        return AskSource(n: n, docid: d["docid"] as? String, collection: (d["collection"] as? String) ?? "", path: path,
                         title: d["title"] as? String, page: (d["page"] as? NSNumber)?.intValue,
                         ts: d["ts"] as? String, snippet: d["snippet"] as? String)
    }
}

enum AskStepState: String, Codable { case run, ok, err }

struct AskStep: Codable, Identifiable {
    var id = UUID()
    var kind: String
    var title: String
    var res: String?
    var ms: Int?
    var state: AskStepState
    var inferred = false
    enum CodingKeys: String, CodingKey { case kind, title, res, ms, state, inferred }
}

struct AskTurn: Codable, Identifiable {
    var id = UUID().uuidString
    var q: String
    var a = ""
    var sources: [AskSource] = []
    var citations: [Int] = []
    var steps: [AskStep] = []
    var ms: Int?
    var model: String?
    var provider: String?
    var answeredWhere: String?          // "cloud" | "local"
    var fallback: String?
    var training = false
    var error: String?
    var stopped = false
    var at = Date()
    var scope = ""
}

struct AskChat: Codable, Identifiable {
    var id: String
    var title: String
    var created: Double
    var updated: Double
    var turns: [AskTurn] = []
    var route: String?                  // "local" keeps this chat on this Mac
    var scope = "all"
    var path = ""
}

struct TreeFolder: Hashable, Identifiable {
    let name: String
    let path: String
    let children: Bool
    var id: String { path }
}

// MARK: - service state

struct AskStatusInfo {
    var available = false
    var enabled = false
    var hasRoute = false                // false: a service from before cloud models
    var route = "cloud"
    var provider: String?
    var model: String?
    var localModel: String?
    var online: Bool?
    var whereNext: String?
    var nextModel: String?
    var nextProblem: String?
    var localFallback = false

    static func parse(_ d: [String: Any]) -> AskStatusInfo {
        var s = AskStatusInfo()
        s.available = (d["available"] as? Bool) ?? false
        s.enabled = (d["enabled"] as? Bool) ?? false
        s.hasRoute = d["route"] != nil
        s.route = (d["route"] as? String) ?? "cloud"
        s.provider = d["provider"] as? String
        s.model = d["model"] as? String
        s.localModel = (d["local_model"] as? String) ?? (d["provider"] == nil ? d["model"] as? String : nil)
        s.online = d["online"] as? Bool
        s.whereNext = d["where_next"] as? String
        s.nextModel = d["next_model"] as? String
        s.nextProblem = d["next_problem"] as? String
        s.localFallback = (d["local_fallback"] as? Bool) ?? false
        return s
    }
}

struct AskProvider: Identifiable {
    let id: String
    let label: String
    let kind: String
    let hasKey: Bool
    let needsKey: Bool?
    let status: String
    let models: [String]
    let names: [String: String]
    let trainingModels: [String]            // glob patterns of the model ids its provider may use inputs of for training
    var usable: Bool { hasKey || needsKey == false }
    func trains(_ model: String?) -> Bool { model.map { m in trainingModels.contains { AskText.glob($0, matches: m) } } ?? false }
}

struct AskProviders {
    var route = "cloud"
    var provider: String?
    var model: String?
    var online: Bool?
    var allowTraining: [String] = []
    var providers: [AskProvider] = []

    static func parse(_ d: [String: Any]) -> AskProviders {
        var p = AskProviders()
        p.route = (d["route"] as? String) ?? "cloud"
        p.provider = d["provider"] as? String
        p.model = d["model"] as? String
        p.online = d["online"] as? Bool
        p.allowTraining = (d["allow_training_models"] as? [String]) ?? []
        func modelId(_ x: Any) -> String? {
            if let s = x as? String { return s }
            if let o = x as? [String: Any] { return (o["id"] as? String) ?? (o["name"] as? String) }
            return nil
        }
        p.providers = ((d["providers"] as? [[String: Any]]) ?? []).compactMap { r in
            guard let id = r["id"] as? String else { return nil }
            var ids: [String] = []
            for m in ((r["models"] as? [Any]) ?? []) + ((r["custom_models"] as? [Any]) ?? []) {
                if let s = modelId(m), !ids.contains(s) { ids.append(s) }
            }
            var names: [String: String] = [:]
            for (k, v) in (r["model_info"] as? [String: Any]) ?? [:] {
                if let n = (v as? [String: Any])?["name"] as? String { names[k] = n }
            }
            return AskProvider(id: id, label: (r["label"] as? String) ?? id, kind: (r["kind"] as? String) ?? "",
                               hasKey: (r["has_key"] as? Bool) ?? false, needsKey: r["needs_key"] as? Bool,
                               status: (r["status"] as? String) ?? "", models: ids, names: names,
                               trainingModels: (r["training_models"] as? [String]) ?? [])
        }
        return p
    }
}

struct AskLocalModel: Hashable { let name: String; let sizeMB: Int }
struct AskLocalModels { var current: String?; var models: [AskLocalModel] = [] }

/// One row of the model menu.
struct AskModelEntry: Identifiable, Hashable {
    let provider: String                // "local" for a model on this Mac
    let model: String
    let name: String
    let local: Bool
    var id: String { "\(provider)|\(model)" }
}

struct AskModelSection: Identifiable {
    let head: String
    let note: String?
    let subs: [(sub: String?, items: [AskModelEntry])]
    var id: String { head }
}

struct AskPrediction {
    var local: Bool
    var model: String
    var problem: String?
    var why: String?
}

enum AskText {
    static let trainText = "The provider may use inputs for training — check the provider's terms"
    static let trainWhy = "This provider may use the inputs and outputs of this model for training. Ask would send your question, the top passages from your documents and the last turns of the chat. Folders with Allow cloud answers off are never sent."
    /// A training_models pattern: * = any characters, ? = one character, case-insensitive (config.trains_on_inputs).
    static func glob(_ pattern: String, matches s: String) -> Bool {
        var rx = "^"
        for ch in pattern {
            switch ch {
            case "*": rx += ".*"
            case "?": rx += "."
            default: rx += NSRegularExpression.escapedPattern(for: String(ch))
            }
        }
        return s.range(of: rx + "$", options: [.regularExpression, .caseInsensitive]) != nil
    }

    static let problem: [String: String] = [
        "no_key": "the chosen provider has no key", "offline": "the provider is not reachable",
        "unknown_provider": "the chosen provider is not set up", "no_model": "no model is chosen",
    ]
    static let fallback: [String: String] = [
        "offline": "offline", "no_key": "no key for the cloud model", "ask_cloud_off": "this folder keeps answers on this Mac",
        "unknown_provider": "the cloud provider is not set up", "no_model": "no cloud model chosen",
        "insecure_url": "the provider's address is not https", "keychain": "the key could not be read from the Keychain",
        "error": "the cloud model failed",
        "training_unconfirmed": "this model's provider may use inputs for training — check the provider's terms, and that is not confirmed",
    ]
    static func fallbackText(_ r: String) -> String {
        let k = r.split(separator: ":").first.map(String.init)?.trimmingCharacters(in: .whitespaces) ?? r
        return "answered locally: \(fallback[k] ?? r.replacingOccurrences(of: "_", with: " "))"
    }
    static func ago(_ t: Double) -> String {
        let s = max(0, Date().timeIntervalSince1970 - t)
        if s < 90 { return "just now" }
        if s < 3600 { return "\(Int(s / 60)) min ago" }
        if s < 86400 { return "\(Int(s / 3600)) h ago" }
        return "\(Int(s / 86400)) d ago"
    }
    static func fmtMs(_ ms: Int) -> String { ms < 1000 ? "\(ms) ms" : String(format: "%.1f s", Double(ms) / 1000) }
}

// MARK: - chat storage

/// Chats live in Application Support/MySearch/ask-chats.json (the last 30). A test run against a stub service
/// (MSE_API_BASE) keeps them in memory only, unless MSE_ASK_CHATS names a file, so it never touches the real ones.
enum AskChatStore {
    static let maxChats = 30
    static var fileURL: URL? {
        if let p = ProcessInfo.processInfo.environment["MSE_ASK_CHATS"], !p.isEmpty { return URL(fileURLWithPath: p) }
        if MSEClient.baseOverride != nil { return nil }
        return FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/MySearch/ask-chats.json")
    }
    static func load() -> [AskChat] {
        guard let url = fileURL, let data = try? Data(contentsOf: url),
              let chats = try? JSONDecoder().decode([AskChat].self, from: data) else { return [] }
        return chats
    }
    static func save(_ chats: [AskChat]) {
        guard let url = fileURL else { return }
        let enc = JSONEncoder()
        enc.outputFormatting = [.sortedKeys]
        guard let data = try? enc.encode(Array(chats.prefix(maxChats))) else { return }
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? data.write(to: url, options: .atomic)
    }
}

// MARK: - POST helper (JSON in, JSON out)

struct AskHTTPError: Error, LocalizedError {
    let status: Int
    let message: String
    var errorDescription: String? { message }
}

extension MSEClient {
    /// POST a JSON body and read the JSON answer. A 4xx/5xx carries the service's own message ({"error": …}).
    func askPostJSON(_ path: String, _ body: [String: Any]) async throws -> Any {
        let cfg = ServiceConfig.load()
        guard cfg != nil || MSEClient.baseOverride != nil else { throw APIError.noConfig }
        var c = URLComponents(url: root, resolvingAgainstBaseURL: false) ?? URLComponents()
        c.path = path
        var req = URLRequest(url: c.url ?? root)
        req.httpMethod = "POST"
        req.timeoutInterval = 60
        req.setValue(cfg?.token ?? "", forHTTPHeaderField: "X-MSE-Token")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, resp) = try await URLSession.shared.data(for: req)
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        let obj = try? JSONSerialization.jsonObject(with: data)
        guard code == 200 else {
            let msg = ((obj as? [String: Any])?["error"] as? String) ?? "The search service answered with HTTP \(code)."
            throw AskHTTPError(status: code, message: msg)
        }
        return obj ?? [:]
    }
}

// MARK: - the model

@MainActor
final class AskModel: ObservableObject {
    enum Overlay { case none, folder, model }

    // service state
    @Published var status: AskStatusInfo?
    @Published var statusFailed = false
    @Published var providers: AskProviders?
    @Published var providersAPI: Bool?                // nil: not asked yet; false: a service without cloud models
    @Published var localModels = AskLocalModels()
    @Published var library: IndexedLibrary?

    // chats
    @Published var chats: [AskChat] = []
    @Published var currentID: String?
    @Published var draft = ""
    @Published var draftScope = "all"
    @Published var draftPath = ""
    @Published var draftRoute = "cloud"
    @Published var streaming = false
    @Published var liveIndex = -1
    @Published var tokenCount = 0                     // tokens of the live answer (snapshot hook)
    @Published var expanded: Set<String> = []         // turn ids whose steps are open
    @Published var openSource: (turn: String, source: AskSource)?
    @Published var overlay: Overlay = .none
    @Published var notice: String?

    // folder picker
    @Published var pickerScope: String?               // nil: the top level (groups and folders)
    @Published var pickerPath = ""
    @Published var pickerFilter = ""
    @Published var pickerRows: [TreeFolder] = []
    @Published var pickerLoading = false

    // @ mention
    @Published var mentionItems: [MentionItem] = []
    @Published var mentionSel = 0
    @Published var mentionOpen = false
    var mentionStart = 0
    private var folderIndex: [String: MentionItem] = [:]
    private var indexSeq = 0

    // model menu
    @Published var modelFilter = ""

    let snapshot: Bool
    private var loading = 0
    /// A snapshot hook sets this until the state it wants on screen has been reached.
    @Published var snapshotWaiting = false
    private var streamTask: Task<Void, Never>?
    private var treeCache: [String: [TreeFolder]] = [:]
    private var pollTask: Task<Void, Never>?

    var isBusy: Bool { loading > 0 || snapshotWaiting }

    struct MentionItem: Identifiable, Hashable {
        let scope: String
        let path: String
        let label: String
        let sub: String
        var id: String { "\(scope)|\(path)|\(sub)" }
    }

    init(snapshot: Bool) {
        self.snapshot = snapshot
        chats = AskChatStore.load().sorted { $0.updated > $1.updated }
        if !snapshot, let last = UserDefaults.standard.string(forKey: "ask.currentChat"), chats.contains(where: { $0.id == last }) {
            currentID = last
        }
    }

    // MARK: loading

    private func busy<T>(_ work: () async -> T) async -> T {
        loading += 1
        defer { loading -= 1 }
        return await work()
    }

    /// Everything the view shows: the status, the providers and local models, the folders. Called when Ask opens.
    func refresh() {
        Task {
            await busy {
                async let a: Void = refreshStatus()
                async let b: Void = refreshProviders()
                async let c: Void = refreshLocal()
                async let d: Void = refreshLibrary()
                _ = await (a, b, c, d)
            }
        }
    }

    func startPolling() {
        guard !snapshot, pollTask == nil else { return }
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 6_000_000_000)
                guard let self, !Task.isCancelled else { return }
                if !self.streaming { await self.refreshStatus() }
            }
        }
    }

    func stopPolling() { pollTask?.cancel(); pollTask = nil }

    func refreshStatus() async {
        do {
            let obj = try await MSEClient.shared.get("/api/status") as? [String: Any]
            statusFailed = false
            if let ask = obj?["ask"] as? [String: Any] { status = AskStatusInfo.parse(ask) } else { status = nil }
        } catch {
            statusFailed = true
        }
    }

    func refreshProviders() async {
        do {
            let obj = try await MSEClient.shared.get("/api/ask/providers") as? [String: Any]
            providers = AskProviders.parse(obj ?? [:])
            providersAPI = true
        } catch APIError.http(let code) where code == 404 {
            providers = nil; providersAPI = false
        } catch {
            // the service is down or slow: keep what was shown
        }
    }

    func refreshLocal() async {
        do {
            guard let obj = try await MSEClient.shared.get("/api/ask/local-models") as? [String: Any] else { return }
            var lm = AskLocalModels()
            lm.current = obj["current"] as? String
            lm.models = ((obj["models"] as? [[String: Any]]) ?? []).compactMap {
                guard let n = $0["name"] as? String else { return nil }
                return AskLocalModel(name: n, sizeMB: ($0["size_mb"] as? NSNumber)?.intValue ?? 0)
            }
            localModels = lm
        } catch { /* a service without the list: the menu simply has no "On this Mac" section */ }
    }

    func refreshLibrary() async {
        if let lib = try? await MSEClient.shared.library() { library = lib }
    }

    // MARK: derived

    var ready: Bool { (status?.available ?? false) && (status?.enabled ?? false) }
    var cloudAPI: Bool { providersAPI == true && providers != nil }
    var routeKnown: Bool { status?.hasRoute ?? false }

    var currentChat: AskChat? { chats.first { $0.id == currentID } }

    /// Route, provider and model: the status over the providers list, like the web page's askCfg().
    var cfg: (route: String, provider: String?, model: String?, whereNext: String?, nextProblem: String?, online: Bool?) {
        var route = providers?.route ?? "cloud", provider = providers?.provider, model = providers?.model
        var online = providers?.online
        var whereNext: String?, problem: String?
        if let s = status {
            if s.hasRoute { route = s.route }
            provider = s.provider ?? provider; model = s.model ?? model
            online = s.online ?? online; whereNext = s.whereNext; problem = s.nextProblem
        }
        if route == "auto" { route = "cloud" }
        return (route, provider, model, whereNext, problem, online)
    }

    var activeProvider: AskProvider? {
        guard let id = cfg.provider else { return nil }
        return providers?.providers.first { $0.id == id }
    }

    /// What the model menu shows as chosen; nil = nothing chosen yet.
    var selection: AskModelEntry? {
        let c = cfg
        if c.route == "local" {
            let m = localModels.current ?? status?.localModel ?? "model on this Mac"
            return AskModelEntry(provider: "local", model: m, name: "", local: true)
        }
        guard let p = activeProvider, p.usable, let m = c.model, !m.isEmpty else { return nil }
        return AskModelEntry(provider: p.id, model: m, name: p.names[m] ?? "", local: false)
    }

    func providerLabel(_ id: String?) -> String {
        guard let id else { return "" }
        if let p = providers?.providers.first(where: { $0.id == id }) { return p.label }
        return ["anthropic": "Claude API"][id] ?? id
    }

    /// Whether the provider marks this model as one its provider may use inputs of for training.
    func trains(_ provider: String?, _ model: String?) -> Bool {
        providers?.providers.first(where: { $0.id == provider })?.trains(model) ?? false
    }

    var chatRoute: String { (currentChat?.route ?? (currentChat == nil ? draftRoute : "cloud")) == "local" ? "local" : "cloud" }
    var whereScope: String { currentChat?.scope ?? draftScope }
    var wherePath: String { currentChat?.path ?? draftPath }

    func label(scope: String, path: String) -> String {
        if scope.isEmpty || scope == "all" { return "All folders" }
        if !path.isEmpty { return ([scope] + path.split(separator: "/").map(String.init)).joined(separator: " › ") }
        return scope.replacingOccurrences(of: ",", with: ", ")
    }
    var whereLabel: String { label(scope: whereScope, path: wherePath) }

    /// Which model the next question reaches (the web page's predictWhere), nil for a service without routes.
    func predict() -> AskPrediction? {
        guard let a = status, a.hasRoute else { return nil }
        let c = cfg
        let localName = localModels.current ?? a.localModel ?? "the model on this Mac"
        func local(_ why: String) -> AskPrediction { AskPrediction(local: true, model: localName, problem: nil, why: why) }
        if c.route == "local" { return local("route") }
        if chatRoute == "local" { return local("chat") }
        if c.whereNext == "cloud" {
            let problem = c.nextProblem ?? (cloudAPI && selection == nil ? "no_model" : nil)
            return AskPrediction(local: false, model: a.nextModel ?? c.model ?? "a cloud model", problem: problem, why: nil)
        }
        return local(c.online == false ? "offline" : "nocloud")
    }

    var needsModel: Bool { ready && cloudAPI && routeKnown && cfg.route != "local" && selection == nil && chatRoute != "local" }

    // MARK: chats

    private func persist() {
        chats.sort { $0.updated > $1.updated }
        if chats.count > AskChatStore.maxChats { chats = Array(chats.prefix(AskChatStore.maxChats)) }
        AskChatStore.save(chats)
        if !snapshot { UserDefaults.standard.set(currentID ?? "", forKey: "ask.currentChat") }
    }

    func newChat() {
        stop()
        currentID = nil
        draftRoute = "cloud"; draftScope = "all"; draftPath = ""
        openSource = nil
        overlay = .none
        if !snapshot { UserDefaults.standard.set("", forKey: "ask.currentChat") }
    }

    func open(_ id: String) {
        guard id != currentID else { return }
        stop()
        currentID = id
        openSource = nil
        if !snapshot { UserDefaults.standard.set(id, forKey: "ask.currentChat") }
    }

    func remove(_ id: String) {
        if id == currentID { stop(); currentID = nil }
        chats.removeAll { $0.id == id }
        persist()
    }

    func setWhere(scope: String, path: String) {
        if let i = chats.firstIndex(where: { $0.id == currentID }) {
            chats[i].scope = scope; chats[i].path = path
            persist()
        } else {
            draftScope = scope; draftPath = path
        }
    }

    // MARK: asking

    private func update(_ chatID: String, _ turn: Int, _ f: (inout AskTurn) -> Void) {
        guard let ci = chats.firstIndex(where: { $0.id == chatID }), turn < chats[ci].turns.count else { return }
        f(&chats[ci].turns[turn])
    }

    func send(_ text: String, retryAt: Int? = nil) {
        let q = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !q.isEmpty, !streaming, ready else { return }
        var ci = chats.firstIndex { $0.id == currentID }
        if ci == nil {
            let now = Date().timeIntervalSince1970
            let c = AskChat(id: "\(Int(now * 1000).description)\(Int.random(in: 100...999))", title: String(q.prefix(80)),
                            created: now, updated: now, turns: [], route: draftRoute == "local" ? "local" : nil,
                            scope: draftScope, path: draftPath)
            chats.insert(c, at: 0)
            currentID = c.id
            ci = 0
        }
        guard let idx = ci else { return }
        let chat = chats[idx]
        let at = min(retryAt ?? chat.turns.count, chat.turns.count)
        let whereLabel = label(scope: chat.scope, path: chat.path)
        var turn = AskTurn(q: q)
        turn.scope = whereLabel
        turn.steps = [AskStep(kind: "search", title: "Search(\(whereLabel), \"\(q)\")", res: nil, ms: nil, state: .run, inferred: true)]
        if at < chat.turns.count { chats[idx].turns[at] = turn } else { chats[idx].turns.append(turn) }
        chats[idx].updated = Date().timeIntervalSince1970
        let turns = chats[idx].turns
        let history: [[String: String]] = turns[max(0, at - 3)..<at].filter { !$0.a.isEmpty && $0.error == nil }.map { ["q": $0.q, "a": $0.a] }
        var body: [String: Any] = ["q": q]
        if !chat.scope.isEmpty, chat.scope != "all" { body["collections"] = chat.scope.split(separator: ",").map(String.init) }
        if !chat.path.isEmpty { body["path"] = chat.path }
        if !history.isEmpty { body["history"] = history }
        if chat.route == "local", routeKnown { body["route"] = "local" }
        let local = chat.route == "local" && routeKnown
        let chatID = chat.id
        streaming = true; liveIndex = at; tokenCount = 0
        persist()
        let t0 = Date()
        streamTask = Task { [weak self] in
            guard let self else { return }
            do {
                for try await line in MSEClient.shared.streamNDJSON("/api/ask", body: body) {
                    if Task.isCancelled { break }
                    guard let data = line.data(using: .utf8),
                          let m = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { continue }
                    self.apply(m, chat: chatID, turn: at, local: local, started: t0)
                }
                if Task.isCancelled { self.finishStopped(chatID, at) }
            } catch is CancellationError {
                self.finishStopped(chatID, at)
            } catch let e as StreamHTTPError {
                self.update(chatID, at) { t in
                    if e.status == 409 {
                        t.error = "Ask is turned off."
                    } else {
                        t.error = (try? JSONSerialization.jsonObject(with: Data(e.body.utf8)) as? [String: Any])?["error"] as? String
                            ?? "The search service answered with HTTP \(e.status)."
                    }
                    Self.failSteps(&t)
                }
                if e.status == 409 { self.status?.enabled = false }
            } catch {
                if (error as? URLError)?.code == .cancelled { self.finishStopped(chatID, at) } else {
                    self.update(chatID, at) { t in t.error = error.localizedDescription; Self.failSteps(&t) }
                }
            }
            self.update(chatID, at) { t in Self.finishSteps(&t) }
            self.streaming = false; self.liveIndex = -1; self.streamTask = nil
            if let i = self.chats.firstIndex(where: { $0.id == chatID }) { self.chats[i].updated = Date().timeIntervalSince1970 }
            self.persist()
        }
    }

    func stop() {
        guard streaming else { return }
        streamTask?.cancel()
    }

    private func finishStopped(_ chatID: String, _ at: Int) {
        update(chatID, at) { t in t.stopped = true; Self.finishSteps(&t) }
    }

    func retry(_ index: Int) {
        guard let c = currentChat, index < c.turns.count else { return }
        send(c.turns[index].q, retryAt: index)
    }

    static func finishSteps(_ t: inout AskTurn) {
        for i in t.steps.indices where t.steps[i].state == .run { t.steps[i].state = .ok }
    }

    static func failSteps(_ t: inout AskTurn) {
        for i in t.steps.indices where t.steps[i].state == .run { t.steps[i].state = .ok }
        if !t.steps.isEmpty { t.steps[t.steps.count - 1].state = .err }
    }

    /// One streamed event, applied to the live turn.
    private func apply(_ m: [String: Any], chat chatID: String, turn at: Int, local: Bool, started t0: Date) {
        let type = (m["type"] as? String) ?? ""
        let hint = predict()?.model ?? "answer"
        update(chatID, at) { t in
            switch type {
            case "step":
                t.steps.removeAll { $0.inferred }                    // the service reports its own steps from now on
                Self.addStep(&t, m)
            case "sources":
                t.sources = ((m["sources"] as? [[String: Any]]) ?? []).compactMap(AskSource.parse)
                if let l = (m["scope"] as? [String: Any])?["label"] as? String { t.scope = l }
                Self.inferAtSources(&t)
            case "token":
                if !t.steps.contains(where: { $0.kind == "model" }) {
                    t.steps.append(AskStep(kind: "model", title: Self.stepTitle("model", hint), res: nil, ms: nil, state: .run, inferred: true))
                }
                t.a += (m["text"] as? String) ?? ""
                tokenCount += 1
            case "done":
                t.citations = ((m["citations"] as? [Any]) ?? []).compactMap { ($0 as? NSNumber)?.intValue }
                t.model = m["model"] as? String
                t.ms = ((m["timings"] as? [String: Any])?["total_ms"] as? NSNumber)?.intValue ?? Int(Date().timeIntervalSince(t0) * 1000)
                if let a = m["answer"] as? String, !a.isEmpty { t.a = a }
                if let w = m["where"] as? String {
                    t.answeredWhere = w
                    t.provider = m["provider"] as? String
                    t.fallback = m["fallback_reason"] as? String
                    t.training = (m["training"] as? Bool) ?? false
                }
                if let i = t.steps.firstIndex(where: { $0.kind == "model" && $0.inferred }), let model = t.model {
                    t.steps[i].title = Self.stepTitle("model", model)
                }
                Self.finishSteps(&t)
            case "error":
                t.error = (m["message"] as? String) ?? "The answer failed."
                Self.failSteps(&t)
            default: break
            }
        }
    }

    // MARK: steps (port of addStep / stepTitle / inferAtSources)

    static func stepTitle(_ kind: String, _ text: String) -> String {
        func unq(_ s: String) -> String { s.trimmingCharacters(in: CharacterSet(charactersIn: "\"\u{201C}\u{201D}\u{201E}")) }
        if kind == "search", let r = try? NSRegularExpression(pattern: "^Searching (.+?) for (.+)$", options: .caseInsensitive),
           let mt = r.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)),
           let a = Range(mt.range(at: 1), in: text), let b = Range(mt.range(at: 2), in: text) {
            return "Search(\(text[a]), \"\(unq(String(text[b])))\")"
        }
        let names = ["search": "Search", "read": "Read", "rewrite": "Rewrite", "model": "Ask"]
        let name = names[kind] ?? (kind.prefix(1).uppercased() + kind.dropFirst())
        var body = text
        for p in ["Reading ", "Asking ", "Searching ", "Found "] where body.lowercased().hasPrefix(p.lowercased()) {
            body = String(body.dropFirst(p.count)); break
        }
        if kind == "rewrite" {
            if body.lowercased().hasPrefix("the follow-up as:") { body = String(body.dropFirst("the follow-up as:".count)).trimmingCharacters(in: .whitespaces) }
            body = unq(body)
        }
        return "\(name)(\(body))"
    }

    static func addStep(_ t: inout AskTurn, _ s: [String: Any]) {
        let kind = (s["kind"] as? String) ?? "step"
        let count = (s["count"] as? NSNumber)?.intValue
        let ms = (s["ms"] as? NSNumber)?.intValue
        let text = (s["text"] as? String) ?? ""
        let plural: (Int, String) -> String = { n, w in "\(n) \(w)\(n == 1 ? "" : "s")" }
        // "Found n results" closes the running Search step instead of starting a line of its own
        if kind == "search", let n = count, s["title"] == nil,
           let i = t.steps.lastIndex(where: { $0.kind == "search" && $0.state == .run }) {
            t.steps[i].state = .ok; t.steps[i].inferred = false
            t.steps[i].res = (s["res"] as? String) ?? plural(n, "result")
            t.steps[i].ms = ms ?? t.steps[i].ms
            return
        }
        for i in t.steps.indices where t.steps[i].state == .run { t.steps[i].state = .ok }
        var res = s["res"] as? String
        if res == nil, let n = count { res = kind == "search" ? plural(n, "result") : kind == "read" ? plural(n, "passage") : String(n) }
        let done = (s["done"] as? Bool) == true || count != nil || ms != nil
        t.steps.append(AskStep(kind: kind, title: (s["title"] as? String) ?? stepTitle(kind, text), res: res, ms: ms,
                               state: done ? .ok : .run, inferred: (s["inferred"] as? Bool) == true))
    }

    static func inferAtSources(_ t: inout AskTurn) {
        let n = t.sources.count
        let docs = Set(t.sources.map { "\($0.collection)/\($0.path)" }).count
        if let i = t.steps.firstIndex(where: { $0.kind == "search" && $0.inferred }) {
            t.steps[i].state = .ok
            t.steps[i].res = "Found \(n) source\(n == 1 ? "" : "s")"
        }
        if !t.steps.contains(where: { $0.kind == "read" }) {
            t.steps.append(AskStep(kind: "read", title: "Read(\(n) passage\(n == 1 ? "" : "s") from \(docs) document\(docs == 1 ? "" : "s"))",
                                   res: nil, ms: nil, state: .ok, inferred: true))
        }
    }

    // MARK: settings

    private func postSettings(_ patch: [String: Any]) async -> Bool {
        do {
            _ = try await MSEClient.shared.askPostJSON("/api/settings", ["ask": patch])
            return true
        } catch {
            notice = (error as? AskHTTPError)?.message ?? error.localizedDescription
            return false
        }
    }

    func enableAsk() {
        Task {
            await busy {
                if await postSettings(["enabled": true]) { status?.enabled = true }
                await refreshStatus()
            }
        }
    }

    func setChatRoute(_ route: String) {
        let toLocal = route == "local"
        Task {
            if !toLocal, cfg.route == "local" { _ = await postSettings(["route": "cloud"]); await refreshStatus() }
        }
        if let i = chats.firstIndex(where: { $0.id == currentID }) {
            chats[i].route = toLocal ? "local" : nil
            persist()
        } else {
            draftRoute = toLocal ? "local" : "cloud"
        }
    }

    /// The model menu's sections: connected providers (grouped by family where that helps), then "On this Mac".
    func modelSections() -> [AskModelSection] {
        var out: [AskModelSection] = []
        let c = cfg
        for p in providers?.providers ?? [] where p.usable {
            var ids = p.models
            if c.provider == p.id, let m = c.model, !m.isEmpty, !ids.contains(m) { ids.append(m) }
            if ids.isEmpty { continue }
            let items = ids.map { AskModelEntry(provider: p.id, model: $0, name: p.names[$0] ?? "", local: false) }
            var subs: [(sub: String?, items: [AskModelEntry])]
            if ids.contains(where: { $0.contains("/") }) {
                var by: [String: [AskModelEntry]] = [:]
                for it in items { by[Self.familyOf(it.model), default: []].append(it) }
                subs = by.keys.sorted { (Self.rankFamily($0), $0) < (Self.rankFamily($1), $1) }.map { (sub: $0, items: by[$0]!) }
            } else if p.kind == "anthropic", ids.count > 4 {
                let order = ["Fable", "Mythos", "Opus", "Sonnet", "Haiku", "Other"]
                var by: [String: [AskModelEntry]] = [:]
                for it in items {
                    let fam = order.dropLast().first { it.model.lowercased().hasPrefix("claude-\($0.lowercased())") } ?? "Other"
                    by[fam, default: []].append(it)
                }
                subs = order.compactMap { f in by[f].map { (sub: f as String?, items: $0) } }
            } else {
                subs = [(sub: nil, items: items)]
            }
            out.append(AskModelSection(head: p.label, note: p.status == "offline" ? "not reachable now" : nil, subs: subs))
        }
        if !localModels.models.isEmpty {
            let gb = { (mb: Int) in String(format: "%.1f GB", Double(mb) / 1024) }
            let ram = localModels.models.first { $0.name == localModels.current }.map { String(format: "~%.1f GB", Double(max(1, Int((Double($0.sizeMB) * 1.15 / 512).rounded()))) / 2) } ?? "~2.5 GB"
            out.append(AskModelSection(head: "On this Mac", note: "local, loads \(ram) when used", subs: [(sub: nil,
                items: localModels.models.map { AskModelEntry(provider: "local", model: $0.name, name: gb($0.sizeMB), local: true) })]))
        }
        return out
    }

    static let familyNames = ["deepseek": "DeepSeek", "anthropic": "Claude", "claude": "Claude", "grok": "Grok", "xai": "Grok", "kimi": "Kimi",
        "moonshot": "Kimi", "qwen": "Qwen", "openai": "GPT", "gpt": "GPT", "minimax": "MiniMax",
        "glm": "GLM", "zai": "GLM", "zhipu": "GLM", "mistral": "Mistral", "gemini": "Gemini", "google": "Gemini", "llama": "Llama", "cohere": "Cohere"]
    static let familyOrder = ["GPT", "Claude", "DeepSeek", "Grok", "Kimi", "Qwen", "MiniMax", "GLM", "Gemini", "Mistral"]
    static func rankFamily(_ f: String) -> Int { familyOrder.firstIndex(of: f) ?? 100 }
    static func familyOf(_ id: String) -> String {
        let pre = id.contains("/") ? String(id.split(separator: "/")[0]) : ""
        let base = (pre.isEmpty ? id : pre).lowercased()
        let word = base.split(whereSeparator: { "-_.0123456789".contains($0) }).first.map(String.init) ?? base
        if let f = familyNames[word] { return f }
        if pre.isEmpty, id.range(of: "^(o\\d|chatgpt|codex)", options: [.regularExpression, .caseInsensitive]) != nil { return "GPT" }
        if !pre.isEmpty { return pre.prefix(1).uppercased() + pre.dropFirst() }
        return "Other"
    }

    func isCurrent(_ e: AskModelEntry) -> Bool {
        guard let s = selection else { return false }
        return s.provider == e.provider && s.model == e.model
    }

    /// Choosing a cloud model sets provider + model + route cloud; a local one sets route local + the local model.
    /// A model its provider may train on asks once first (and is then stored as allowed).
    func choose(_ e: AskModelEntry) {
        overlay = .none
        if isCurrent(e) { return }
        Task {
            await busy {
                if e.local {
                    _ = await postSettings(["route": "local", "local_model": e.model])
                } else {
                    var patch: [String: Any] = ["provider": e.provider, "model": e.model, "route": "cloud"]
                    let allowed = providers?.allowTraining ?? []
                    if trains(e.provider, e.model), !allowed.contains(e.model) {
                        guard confirmTraining(e.model) else { return }
                        patch["allow_training_models"] = allowed + [e.model]
                    }
                    _ = await postSettings(patch)
                }
                async let a: Void = refreshStatus(); async let b: Void = refreshProviders(); async let c: Void = refreshLocal()
                _ = await (a, b, c)
            }
        }
    }

    private func confirmTraining(_ model: String) -> Bool {
        if snapshot { return false }                    // a snapshot run never shows an alert
        let alert = NSAlert()
        alert.messageText = "Use \(model)?"
        alert.informativeText = "\(AskText.trainText). \(AskText.trainWhy)\n\n\(model) stays allowed until you remove ask.allow_training_models from config.json."
        alert.alertStyle = .warning
        alert.addButton(withTitle: "Allow and use")
        alert.addButton(withTitle: "Cancel")
        return alert.runModal() == .alertFirstButtonReturn
    }

    // MARK: folders: the tree, the picker, @ mentions

    func fetchTree(_ collection: String, _ path: String) async -> [TreeFolder] {
        let key = "\(collection)|\(path)"
        if let hit = treeCache[key] { return hit }
        let q = [URLQueryItem(name: "c", value: collection), URLQueryItem(name: "path", value: path)]
        guard let obj = try? await MSEClient.shared.get("/api/tree", query: q) as? [String: Any] else { return [] }
        let rows: [TreeFolder] = ((obj["folders"] as? [[String: Any]]) ?? []).compactMap {
            guard let n = $0["name"] as? String, let p = $0["path"] as? String else { return nil }
            return TreeFolder(name: n, path: p, children: ($0["children"] as? Bool) ?? false)
        }
        treeCache[key] = rows
        return rows
    }

    func openPicker() {
        guard ready else { return }
        overlay = .folder
        pickerScope = nil; pickerPath = ""; pickerFilter = ""; pickerRows = []
    }

    func toggle(_ o: Overlay) { overlay = overlay == o ? .none : o; if overlay == .model { modelFilter = "" } }

    /// Drills the picker into a folder (nil = back to the top level).
    func pickerGo(_ scope: String?, _ path: String = "") {
        pickerScope = scope; pickerPath = path; pickerFilter = ""; pickerRows = []
        guard let scope else { return }
        pickerLoading = true
        Task {
            let rows = await fetchTree(scope, path)
            if pickerScope == scope, pickerPath == path { pickerRows = rows; pickerLoading = false }
        }
    }

    func pickerUse(scope: String, path: String) {
        setWhere(scope: scope, path: path)
        overlay = .none
    }

    var pickerCrumbs: [(label: String, scope: String?, path: String)] {
        var out: [(String, String?, String)] = [("All folders", nil, "")]
        guard let s = pickerScope else { return out }
        out.append((s, s, ""))
        var acc = ""
        for part in pickerPath.split(separator: "/") {
            acc += (acc.isEmpty ? "" : "/") + part
            out.append((String(part), s, acc))
        }
        return out
    }

    func updateMention(text: String, caret: Int) {
        guard ready else { closeMention(); return }
        let head = String(text.prefix(caret))
        guard let r = try? NSRegularExpression(pattern: "(^|\\s)@([^\\s@]*)$"),
              let m = r.firstMatch(in: head, range: NSRange(head.startIndex..., in: head)),
              let q = Range(m.range(at: 2), in: head) else { closeMention(); return }
        mentionStart = head.distance(from: head.startIndex, to: q.lowerBound) - 1
        if !mentionOpen { mentionOpen = true; buildFolderIndex() }
        mentionSel = 0
        mentionItems = mentionMatches(String(head[q]))
        if mentionItems.isEmpty { mentionOpen = mentionOpen && true }
    }

    func closeMention() { mentionOpen = false; mentionItems = [] }

    private var lastMentionQuery = ""
    private func mentionMatches(_ q: String) -> [MentionItem] {
        lastMentionQuery = q
        let terms = q.lowercased().split(separator: " ").map(String.init)
        var all: [MentionItem] = []
        for c in library?.collections ?? [] where c.enabled && c.exists { all.append(MentionItem(scope: c.name, path: "", label: c.name, sub: "folder")) }
        for g in library?.groups ?? [] { all.append(MentionItem(scope: g.name, path: "", label: g.name, sub: "group")) }
        all += folderIndex.values.sorted { $0.label < $1.label }
        return Array(all.filter { it in terms.allSatisfy { it.label.lowercased().contains($0) } }.prefix(12))
    }

    private func buildFolderIndex() {
        indexSeq += 1
        let seq = indexSeq
        Task {
            var queue: [(String, String, Int)] = (library?.collections ?? []).filter { $0.enabled && $0.exists }.map { ($0.name, "", 0) }
            var budget = 120
            while !queue.isEmpty, budget > 0, seq == indexSeq, mentionOpen {
                budget -= 1
                let (c, p, depth) = queue.removeFirst()
                for k in await fetchTree(c, p) {
                    folderIndex["\(c)|\(k.path)"] = MentionItem(scope: c, path: k.path, label: ([c] + k.path.split(separator: "/").map(String.init)).joined(separator: " › "), sub: "folder")
                    if k.children, depth < 2 { queue.append((c, k.path, depth + 1)) }
                }
                if mentionOpen { mentionItems = mentionMatches(lastMentionQuery) }
            }
        }
    }

    /// Accepts mention `i`: removes "@text" from the draft and moves the chat to that folder.
    func acceptMention(_ i: Int) {
        guard i < mentionItems.count else { return }
        let it = mentionItems[i]
        let s = draft
        let start = s.index(s.startIndex, offsetBy: min(mentionStart, s.count))
        var end = start
        while end < s.endIndex, !s[end].isWhitespace { end = s.index(after: end) }
        draft = String(s[..<start]) + String(s[end...])
        setWhere(scope: it.scope, path: it.path)
        closeMention()
    }

    // MARK: sources

    func absoluteURL(for s: AskSource) -> URL {
        if s.path.hasPrefix("/") { return URL(fileURLWithPath: s.path) }
        let root = library?.collections.first { $0.name == s.collection }?.path
        return URL(fileURLWithPath: root ?? "/").appendingPathComponent(s.path)
    }

    func showSource(turn: String, n: Int) {
        guard let c = currentChat, let t = c.turns.first(where: { $0.id == turn }), let s = t.sources.first(where: { $0.n == n }) else { return }
        openSource = (turn, s)
    }

    func copyText(of t: AskTurn) -> String {
        let cited = t.sources.filter { t.citations.isEmpty || t.citations.contains($0.n) }.map { s -> String in
            let name = s.title ?? URL(fileURLWithPath: s.path).lastPathComponent
            return "[\(s.n)] \(name) — \(s.collection)/\(s.path)\(s.page.map { ", p. \($0)" } ?? "")\(s.ts.map { " at \($0)" } ?? "")"
        }
        return t.a.trimmingCharacters(in: .whitespacesAndNewlines) + (cited.isEmpty ? "" : "\n\nSources:\n" + cited.joined(separator: "\n"))
    }
}
