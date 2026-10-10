// The native Settings window, part 1: the store. One @MainActor object holds everything the screens show and every
// call they make to the local service. Reads are polled while the window is visible (status every 3 s, folders every
// 9-12 s, the section that is open as often as it needs); writes go out at once (switches, pickers) or after a short
// pause (steppers, sliders, text), merged into one POST /api/settings. A value the user is changing is never
// overwritten by a poll. The service's own refusal text is shown next to the control that caused it.
//
// Nothing here prints or stores an API key: keys are sent once to POST /api/ask/providers (the service puts them in
// the Keychain) and the field is cleared by the caller.

import Cocoa
import SwiftUI

typealias SWJSON = [String: Any]

extension Dictionary where Key == String, Value == Any {
    func swS(_ k: String) -> String? { self[k] as? String }
    func swI(_ k: String) -> Int? { (self[k] as? NSNumber)?.intValue }
    func swD(_ k: String) -> Double? { (self[k] as? NSNumber)?.doubleValue }
    func swB(_ k: String) -> Bool? { self[k] as? Bool }
    func swO(_ k: String) -> SWJSON { (self[k] as? SWJSON) ?? [:] }
    func swR(_ k: String) -> [SWJSON] { (self[k] as? [SWJSON]) ?? [] }
    func swA(_ k: String) -> [String] { ((self[k] as? [Any]) ?? []).compactMap { $0 as? String } }
}

enum SW {
    /// The service's sentence without the Python exception name in front of it.
    static func clean(_ error: Error) -> String {
        var m = (error as? MSEServerMessage)?.message ?? error.localizedDescription
        for p in ["ValueError: ", "KeyError: ", "PermissionError: "] where m.hasPrefix(p) { m.removeFirst(p.count) }
        return m
    }

    static func bytes(_ n: Double) -> String {
        ByteCountFormatter.string(fromByteCount: Int64(n), countStyle: .file)
    }

    static func count(_ n: Int, _ noun: String, plural: String? = nil) -> String {
        let f = NumberFormatter(); f.numberStyle = .decimal
        let s = f.string(from: NSNumber(value: n)) ?? "\(n)"
        return "\(s) \(n == 1 ? noun : (plural ?? noun + "s"))"
    }

    static func num(_ n: Int) -> String {
        let f = NumberFormatter(); f.numberStyle = .decimal
        return f.string(from: NSNumber(value: n)) ?? "\(n)"
    }

    /// "3 min ago" from epoch seconds (a number or a numeric string); "never" when there is none.
    static func ago(_ v: Any?) -> String {
        var t: Double?
        if let n = v as? NSNumber { t = n.doubleValue } else if let s = v as? String { t = Double(s) }
        guard let t, t > 0 else { return "never" }
        let d = max(0, Date().timeIntervalSince1970 - t)
        if d < 60 { return "just now" }
        if d < 3600 { return "\(Int(d / 60)) min ago" }
        if d < 86400 { return "\(Int(d / 3600)) h ago" }
        return "\(Int(d / 86400)) d ago"
    }

    static func tilde(_ path: String) -> String { (path as NSString).abbreviatingWithTildeInPath }

    static func expand(_ path: String) -> String { (path as NSString).expandingTildeInPath }

    /// A RAM figure for the local answer model: about 1.15 x its weights on disk, to the nearest half GB.
    static func localRAM(sizeMB: Double) -> String {
        let gb = (sizeMB * 1.15 / 1024 * 2).rounded() / 2
        return gb == gb.rounded() ? "\(Int(gb)) GB" : String(format: "%.1f GB", gb)
    }
}

// MARK: - sections and the search index

enum SWSection: String, CaseIterable, Identifiable {
    case folders, groups, search, schedule, ask, wholeMac, favourites, agents, privacy, macApp, about
    var id: String { rawValue }

    var title: String {
        switch self {
        case .folders: return "Folders"
        case .groups: return "Groups"
        case .search: return "Search"
        case .schedule: return "Schedule & Engine"
        case .ask: return "Ask"
        case .wholeMac: return "Whole-Mac Search"
        case .favourites: return "Favourites"
        case .agents: return "AI Agents"
        case .privacy: return "Privacy"
        case .macApp: return "Mac App"
        case .about: return "About"
        }
    }

    var symbol: String {
        switch self {
        case .folders: return "folder.fill"
        case .groups: return "square.stack.3d.up.fill"
        case .search: return "magnifyingglass"
        case .schedule: return "moon.stars.fill"
        case .ask: return "sparkles"
        case .wholeMac: return "internaldrive.fill"
        case .favourites: return "star.fill"
        case .agents: return "cpu.fill"
        case .privacy: return "hand.raised.fill"
        case .macApp: return "macwindow"
        case .about: return "info.circle.fill"
        }
    }

    var tint: Color {
        switch self {
        case .folders: return .blue
        case .groups: return .indigo
        case .search: return .teal
        case .schedule: return .purple
        case .ask: return .pink
        case .wholeMac: return .orange
        case .favourites: return .yellow
        case .agents: return .green
        case .privacy: return .gray
        case .macApp: return .cyan
        case .about: return .secondary
        }
    }

    var blurb: String {
        switch self {
        case .folders: return "Each folder has its own index. Your files are only read, never changed."
        case .groups: return "A group searches several folders together and can get its own tab."
        case .search: return "How results are found and ordered. Changes apply to the next search."
        case .schedule: return "Changed files are indexed within seconds. Heavy work waits for the night run or Sync now."
        case .ask: return "Short answers with numbered references, written from your top results."
        case .wholeMac: return "File names across this Mac, never contents. Private places are always skipped."
        case .favourites: return "Results you starred come first the next time you, or an agent, search for them."
        case .agents: return "Connect AI apps so they search your folders through MCP."
        case .privacy: return "Everything stays on this Mac. Nothing is uploaded, and searches are not logged."
        case .macApp: return "Quick Search, the menu-bar item and what happens at login."
        case .about: return "Versions, where things live, and the models on disk."
        }
    }
}

struct SWIndexItem: Identifiable {
    let section: SWSection
    let id: String
    let title: String
    let keywords: String
}

enum SWIndex {
    static let items: [SWIndexItem] = [
        .init(section: .folders, id: "folders.add", title: "Add a folder", keywords: "picker choose new index"),
        .init(section: .folders, id: "folders.syncAll", title: "Sync all folders now", keywords: "scan refresh index everything"),
        .init(section: .folders, id: "folder.sync", title: "Sync a folder now", keywords: "scan refresh"),
        .init(section: .folders, id: "folder.remove", title: "Remove a folder", keywords: "delete forget disconnect agents"),
        .init(section: .folders, id: "folder.enabled", title: "Pause or enable a folder", keywords: "on off paused"),
        .init(section: .folders, id: "folder.inAll", title: "Include in All", keywords: "global tab search everything"),
        .init(section: .folders, id: "folder.aliases", title: "Short names (aliases)", keywords: "w: shortcut"),
        .init(section: .folders, id: "folder.description", title: "Description shown to agents", keywords: "notes"),
        .init(section: .folders, id: "folder.media", title: "Read images, audio and video", keywords: "ocr pictures transcribe media"),
        .init(section: .folders, id: "folder.pdf", title: "PDF page images", keywords: "scans scanned pages ocr"),
        .init(section: .folders, id: "folder.size", title: "Largest file to read", keywords: "max file size mb limit"),
        .init(section: .folders, id: "folder.exclude", title: "Skip these paths", keywords: "ignore exclude rules patterns"),
        .init(section: .folders, id: "folder.nameonly", title: "File names only", keywords: "metadata only never read contents"),
        .init(section: .folders, id: "folder.contexts", title: "Context for agents", keywords: "prefix describe"),
        .init(section: .folders, id: "folder.cloud", title: "Allow cloud answers", keywords: "ask private keep on this mac"),
        .init(section: .folders, id: "folder.agents", title: "AI agents in this folder", keywords: "connect disconnect mcp claude cursor codex"),
        .init(section: .folders, id: "folder.problems", title: "Unreadable files and retry", keywords: "errors failed problems waiting embed ocr"),
        .init(section: .groups, id: "groups.new", title: "New group", keywords: "combine folders tab"),
        .init(section: .groups, id: "groups.tab", title: "Show a group as a tab", keywords: "hidden menu"),
        .init(section: .search, id: "search.limit", title: "Results per search", keywords: "limit count agents cli"),
        .init(section: .search, id: "search.bridge", title: "Translate between English and German", keywords: "language bridge vocabulary rechnung"),
        .init(section: .search, id: "search.rerank", title: "Re-check close calls", keywords: "reranker re-rank ties"),
        .init(section: .search, id: "search.hub", title: "Overview files rank lower", keywords: "summary readme index penalty hub"),
        .init(section: .schedule, id: "sched.night", title: "Night run", keywords: "nightly time ocr embeddings enable"),
        .init(section: .schedule, id: "sched.ac", title: "Only when plugged in", keywords: "battery power ac"),
        .init(section: .schedule, id: "sched.scan", title: "Check folders for changes every", keywords: "interval minutes scan"),
        .init(section: .schedule, id: "sched.live", title: "Embed right away below", keywords: "live limit pieces batch"),
        .init(section: .schedule, id: "sched.idle", title: "Unload the models after", keywords: "idle memory ram minutes"),
        .init(section: .schedule, id: "sched.jobs", title: "Jobs and progress", keywords: "queue stop cancel running"),
        .init(section: .schedule, id: "sched.engine", title: "Engine and memory", keywords: "workers whisper model service"),
        .init(section: .schedule, id: "sched.learning", title: "Search quality and learning", keywords: "finetune tune quality favourites"),
        .init(section: .ask, id: "ask.enabled", title: "Answer questions", keywords: "ask on off"),
        .init(section: .ask, id: "ask.route", title: "Where answers are written", keywords: "cloud local route"),
        .init(section: .ask, id: "ask.fallback", title: "Use the local model when no cloud model is reachable", keywords: "fallback offline"),
        .init(section: .ask, id: "ask.redact", title: "Mask personal identifiers before sending to the cloud", keywords: "privacy masking iban passport"),
        .init(section: .ask, id: "ask.model", title: "Cloud model", keywords: "menu provider claude gpt training"),
        .init(section: .ask, id: "ask.local", title: "Model on this Mac", keywords: "local picker gb memory"),
        .init(section: .ask, id: "ask.providers", title: "Cloud providers and API keys", keywords: "keychain key key file test refresh list deepseek openrouter ollama lm studio"),
        .init(section: .wholeMac, id: "mac.enabled", title: "Search file names across this Mac", keywords: "whole mac enable"),
        .init(section: .wholeMac, id: "mac.roots", title: "Folders to look in", keywords: "roots"),
        .init(section: .wholeMac, id: "mac.exclude", title: "Excluded folders", keywords: "skip leave out"),
        .init(section: .wholeMac, id: "mac.always", title: "Always excluded", keywords: "built-in privacy rules read-only secrets mail messages"),
        .init(section: .wholeMac, id: "mac.fda", title: "Full Disk Access", keywords: "permission protected folders privacy security"),
        .init(section: .wholeMac, id: "mac.rescan", title: "Rescan now", keywords: "crawl progress"),
        .init(section: .wholeMac, id: "mac.protected", title: "Protected folders", keywords: "skip_protected consent"),
        .init(section: .favourites, id: "fav.list", title: "Favourites by search", keywords: "starred best not relevant remove filter"),
        .init(section: .agents, id: "agents.global", title: "Connect AI apps everywhere", keywords: "claude desktop cursor grok codex antigravity mcp"),
        .init(section: .agents, id: "agents.folders", title: "Connect AI agents per folder", keywords: "mcp project"),
        .init(section: .agents, id: "agents.snippets", title: "Setup commands", keywords: "claude mcp add json toml binary path"),
        .init(section: .privacy, id: "privacy.redact", title: "Mask personal identifiers for agents", keywords: "mcp output iban passport tax id"),
        .init(section: .privacy, id: "privacy.nameonly", title: "File-name-only rules", keywords: "metadata never indexed"),
        .init(section: .privacy, id: "privacy.secrets", title: "Always skipped secrets", keywords: "patterns credentials keys read-only"),
        .init(section: .macApp, id: "app.login", title: "Open at login", keywords: "startup launch"),
        .init(section: .macApp, id: "app.quick", title: "Quick Search shortcut", keywords: "hotkey option space"),
        .init(section: .macApp, id: "app.launchers", title: "Open in… (Terminal, VS Code, Cursor, Claude Code, Codex)", keywords: "launcher editor terminal iterm refresh finder"),
        .init(section: .about, id: "about.versions", title: "Versions", keywords: "version service uptime port"),
        .init(section: .about, id: "about.models", title: "Models and their sizes", keywords: "search model reranker whisper speech disk"),
        .init(section: .about, id: "about.paths", title: "Data folder and mse path", keywords: "logs binary location"),
    ]

    static func matches(_ q: String) -> [SWIndexItem] {
        let words = q.lowercased().split(separator: " ").map(String.init)
        guard !words.isEmpty else { return [] }
        return items.filter { it in
            let hay = (it.title + " " + it.keywords + " " + it.section.title).lowercased()
            return words.allSatisfy { hay.contains($0) }
        }
    }
}

enum SWSheet: Identifiable {
    case addFolder(URL)
    case addProvider
    var id: String {
        switch self {
        case .addFolder(let u): return "folder:" + u.path
        case .addProvider: return "provider"
        }
    }
}

// MARK: - the folder as the screens see it

struct SWStats {
    var files = 0, bytes = 0.0, items = 0, embedPending = 0, heavyPending = 0, nameOnly = 0, errors = 0
    var lastScan: Any?, lastHeavy: Any?

    init?(_ r: SWJSON?) {
        guard let r else { return nil }
        files = r.swI("files") ?? 0; bytes = r.swD("bytes") ?? 0; items = r.swI("items") ?? 0
        embedPending = r.swI("embed_pending") ?? 0; heavyPending = r.swI("heavy_pending") ?? 0
        nameOnly = r.swI("metadata_only") ?? 0; errors = r.swI("errors") ?? 0
        lastScan = r["last_scan"]; lastHeavy = r["last_heavy"]
    }
}

struct SWFolder: Identifiable {
    let name: String, path: String, desc: String
    let inAll: Bool, enabled: Bool, exists: Bool
    let aliases: [String], exclude: [String], nameOnly: [String]
    let contexts: [(String, String)]
    let images: Bool, audio: Bool, video: Bool, pdfPages: String
    let askCloud: Bool?                 // nil: this service does not report it
    let stats: SWStats?
    let hasAliases: Bool
    var id: String { name }

    init(_ r: SWJSON) {
        name = r.swS("name") ?? ""; path = r.swS("path") ?? ""; desc = r.swS("description") ?? ""
        inAll = r.swB("in_global") ?? false; enabled = r.swB("enabled") ?? true; exists = r.swB("exists") ?? true
        hasAliases = r["aliases"] != nil
        aliases = r.swA("aliases"); exclude = r.swA("exclude"); nameOnly = r.swA("metadata_only")
        let ctx = r.swO("contexts")
        contexts = ctx.keys.sorted().map { ($0, (ctx[$0] as? String) ?? "") }
        let media = r.swO("media")
        images = media.swB("images") ?? false; audio = media.swB("audio") ?? false; video = media.swB("video") ?? false
        pdfPages = media.swS("pdf_pages") ?? "smart"
        askCloud = r.keys.contains("ask_cloud") ? ((r["ask_cloud"] as? Bool) ?? true) : nil   // the key with null means "allowed"
        stats = SWStats(r["stats"] as? SWJSON)
    }
}

enum SWTone { case ok, warn, bad, busy, neutral }

// MARK: - the store

@MainActor
final class SWModel: ObservableObject {
    // chosen screen
    @Published var section: SWSection = .folders
    @Published var query = ""
    @Published var highlight: String?
    @Published var sheet: SWSheet?
    @Published var notice: String?
    @Published var savedAt: Date?
    @Published var errors: [String: String] = [:]

    // service
    @Published var status: SWJSON = [:]
    @Published var statusState: Bool?              // nil: not asked yet, false: not answering
    @Published var settings: SWJSON = [:]
    @Published var collections: [SWJSON] = []
    @Published var groupRows: [(detail: GroupDetail, files: Int)] = []
    @Published var healthCache: [String: SWJSON] = [:]
    @Published var maxFileMB: [String: Int] = [:]
    @Published var openFolders: Set<String> = []

    // per-section data
    @Published var providers: SWJSON?
    @Published var providersAPI: Bool?
    @Published var providersLoading = false
    @Published var providerNote: [String: (text: String, ok: Bool)] = [:]
    @Published var busy: Set<String> = []
    @Published var localModels: SWJSON?
    @Published var wholeStatus: SWJSON?
    @Published var agentsAvailable: Bool?
    @Published var globalAgents: [SWJSON]?
    @Published var folderAgents: [String: [SWJSON]] = [:]
    @Published var mseBin = ""
    @Published var favourites: [SWJSON] = []
    @Published var favAPI: Bool?
    @Published var finetune: SWJSON?
    @Published var quality: SWJSON?
    @Published var modelSizes: [String: Double] = [:]

    weak var window: NSWindow?
    var openClassic: ((String?) -> Void)?

    private var loop: Task<Void, Never>?
    private var flushTask: Task<Void, Never>?
    private var flushChain: Task<Void, Never>?
    private var pending: SWJSON = [:]
    private var pendingKeys: Set<String> = []
    private var inflight = 0
    private var folderBusy = 0
    private var tick = 0
    private var noticeTask: Task<Void, Never>?
    private var highlightTask: Task<Void, Never>?

    init() {
        let saved = UserDefaults.standard.string(forKey: "SWSection").flatMap(SWSection.init(rawValue:))
        if let saved { section = saved }
        openFolders = Set(UserDefaults.standard.stringArray(forKey: "SWOpenFolders") ?? [])
        NotificationCenter.default.addObserver(forName: GroupNotes.changed, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in await self?.refreshCollections(force: true) }
        }
    }

    // MARK: derived

    var folders: [SWFolder] { collections.map(SWFolder.init).filter { !$0.name.isEmpty } }
    var askStatus: SWJSON { status.swO("ask") }
    var askAvailable: Bool { !askStatus.isEmpty }
    var jobs: SWJSON { status.swO("jobs") }
    var currentJob: SWJSON? { jobs["current"] as? SWJSON }
    var visible: Bool { (window?.isVisible ?? false) && (window?.occlusionState.contains(.visible) ?? false) }
    var sectionsShown: [SWSection] { SWSection.allCases.filter { $0 != .ask || askAvailable || statusState == nil } }

    func setSection(_ s: SWSection) {
        section = s
        UserDefaults.standard.set(s.rawValue, forKey: "SWSection")
        onAppear(s)
    }

    /// Goes to a search hit and flashes the row.
    func reveal(_ item: SWIndexItem) {
        setSection(item.section)
        if item.id.hasPrefix("folder."), let first = collections.first?.swS("name") { openFolders.insert(first) }
        highlight = nil
        highlight = item.id
        highlightTask?.cancel()
        highlightTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 3_000_000_000)
            if !Task.isCancelled { self?.highlight = nil }
        }
    }

    func say(_ text: String) {
        notice = text
        noticeTask?.cancel()
        noticeTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 4_000_000_000)
            if !Task.isCancelled { self?.notice = nil }
        }
    }

    func flashSaved() {
        savedAt = Date()
        let at = savedAt
        Task { [weak self] in
            try? await Task.sleep(nanoseconds: 2_200_000_000)
            if self?.savedAt == at { self?.savedAt = nil }
        }
    }

    func setError(_ key: String, _ error: Error?) {
        errors[key] = error.map { SW.clean($0) }
    }

    // MARK: polling

    func start() {
        stop()
        tick = 0
        loop = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                if self.visible || self.tick == 0 { await self.pollOnce() }
                try? await Task.sleep(nanoseconds: 3_000_000_000)
            }
        }
    }

    func stop() { loop?.cancel(); loop = nil }

    private func pollOnce() async {
        let t = tick
        tick += 1
        await refreshStatus()
        if t == 0 {
            await refreshSettings(force: true)
            await refreshCollections(force: true)
            onAppear(section)
            return
        }
        let busyNow = currentJob != nil
        if t % (busyNow ? 2 : 4) == 0 { await refreshCollections() }
        if t % 4 == 0 { await refreshSettings() }
        switch section {
        case .wholeMac where t % 2 == 0: await loadWhole()
        case .schedule where t % 4 == 0: await loadQuality()
        default: break
        }
    }

    func refreshStatus() async {
        do {
            if let o = try await MSEClient.shared.get("/api/status") as? SWJSON { status = o; statusState = true; mseBin = o.swS("mse_bin") ?? mseBin }
        } catch { statusState = false }
    }

    func refreshSettings(force: Bool = false) async {
        guard force || (inflight == 0 && pending.isEmpty) else { return }
        if let o = try? await MSEClient.shared.get("/api/settings") as? SWJSON {
            guard force || (inflight == 0 && pending.isEmpty) else { return }
            settings = o
        }
    }

    func refreshCollections(force: Bool = false) async {
        guard force || folderBusy == 0 else { return }
        guard let o = try? await MSEClient.shared.get("/api/collections") as? SWJSON else { return }
        guard force || folderBusy == 0 else { return }
        collections = o.swR("collections")
        groupRows = o.swR("groups").compactMap { g in GroupDetail(json: g).map { ($0, g.swI("files") ?? 0) } }
    }

    /// What a screen needs when it opens.
    func onAppear(_ s: SWSection) {
        Task { [weak self] in
            guard let self else { return }
            switch s {
            case .ask: await self.loadProviders(); await self.loadLocalModels()
            case .wholeMac: await self.loadWhole()
            case .favourites: await self.loadFavourites()
            case .agents: await self.loadAgents()
            case .schedule: await self.loadQuality()
            case .about: self.computeModelSizes(); await self.loadLocalModels()
            case .folders: await self.probeAgents()
            default: break
            }
        }
    }

    // MARK: writing /api/settings

    /// Applies `values` to the shown settings at once, then sends them (merged with other pending changes) after
    /// `delay` seconds. `err` names the control that shows a refusal.
    func patch(_ group: String, _ values: SWJSON, err: String = "", delay: Double = 0) {
        var g = settings.swO(group)
        for (k, v) in values { g[k] = v }
        settings[group] = g
        if group == "ask" {
            var a = status.swO("ask")
            for (k, v) in values { a[k] = v }
            status["ask"] = a
            if var p = providers {
                for (k, v) in values where ["route", "provider", "model", "local_fallback", "allow_training_models"].contains(k) { p[k] = v }
                providers = p
            }
        }
        var p = pending.swO(group)
        for (k, v) in values { p[k] = v }
        pending[group] = p
        pendingKeys.insert(err)
        // Only the pause can be cancelled by a newer change; a POST already on its way is never cancelled, and
        // flushes run one after the other so two writes of one key cannot land out of order.
        flushTask?.cancel()
        flushTask = Task { [weak self] in
            if delay > 0 { try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000)) }
            if Task.isCancelled { return }
            guard let self else { return }
            let prev = self.flushChain
            self.flushChain = Task { [weak self] in
                await prev?.value
                await self?.flush()
            }
        }
    }

    private func flush() async {
        guard !pending.isEmpty else { return }
        let body = pending, keys = pendingKeys
        pending = [:]; pendingKeys = []
        inflight += 1
        defer { inflight -= 1 }
        do {
            let reply = try await MSEClient.shared.postJSON("/api/settings", body: body)
            for k in keys { errors[k] = nil }
            if pending.isEmpty {                          // the stored (normalised) values replace the optimistic ones
                for (k, v) in reply where v is SWJSON { settings[k] = v }
            }
            flashSaved()
            await refreshStatus()
            if body["mac_index"] != nil { await loadWhole() }
        } catch {
            let msg = SW.clean(error)
            for k in keys { errors[k] = msg }
            await refreshSettings(force: true)
            await refreshStatus()
        }
    }

    /// A binding onto settings[group][key] that saves through `patch`.
    func bind<T>(_ group: String, _ key: String, _ fallback: T, err: String = "", delay: Double = 0) -> Binding<T> {
        Binding(get: { (self.settings.swO(group)[key] as? T) ?? fallback },
                set: { self.patch(group, [key: $0], err: err.isEmpty ? "\(group).\(key)" : err, delay: delay) })
    }

    func bindNumber(_ group: String, _ key: String, _ fallback: Int, delay: Double = 0.6) -> Binding<Int> {
        Binding(get: { (self.settings.swO(group)[key] as? NSNumber)?.intValue ?? fallback },
                set: { self.patch(group, [key: $0], err: "\(group).\(key)", delay: delay) })
    }

    // MARK: folders

    func patchFolder(_ name: String, _ body: SWJSON, err: String) {
        if let i = collections.firstIndex(where: { $0.swS("name") == name }) {
            var r = collections[i]
            for (k, v) in body {
                if k == "media", let mv = v as? SWJSON {
                    var cur = r.swO("media"); for (a, b) in mv { cur[a] = b }; r["media"] = cur
                } else { r[k] = v }
            }
            collections[i] = r
        }
        folderBusy += 1
        Task { [weak self] in
            guard let self else { return }
            defer { self.folderBusy -= 1 }
            do {
                let r = try await MSEClient.shared.postJSON("/api/collections/\(name)", body: body)
                if let mf = r.swO("collection").swI("max_file_mb") { self.maxFileMB[name] = mf }
                self.errors[err] = nil
                self.flashSaved()
            } catch { self.errors[err] = SW.clean(error) }
            await self.refreshCollections(force: true)
        }
    }

    func syncFolder(_ name: String) {
        Task {
            do { _ = try await MSEClient.shared.postJSON("/api/sync", body: ["collection": name, "kind": "sync"]); say("Syncing \(name) now"); await refreshStatus() }
            catch { say(SW.clean(error)) }
        }
    }

    func syncAll() {
        Task {
            do { _ = try await MSEClient.shared.postJSON("/api/sync", body: ["kind": "sync"]); say("Syncing all folders now"); await refreshStatus() }
            catch { say(SW.clean(error)) }
        }
    }

    func cancelJob() {
        Task { _ = try? await MSEClient.shared.postJSON("/api/cancel", body: [:]); await refreshStatus() }
    }

    func addFolder(url: URL, name: String, connect: Bool) async -> String? {
        var body: SWJSON = ["path": url.path]
        if !name.isEmpty { body["name"] = name }
        if agentsAvailable == true { body["connect_agents"] = connect }
        do {
            let r = try await MSEClient.shared.postJSON("/api/collections", body: body)
            if let n = r.swO("collection").swS("name") { openFolders.insert(n); persistOpen() }
            var msg = "Added. Text is searchable within minutes; OCR and media run tonight or on Sync now."
            if let files = r.swO("agents")["files"] as? [SWJSON] { msg += " Agents connected (\(SWAgents.actions(files)))." }
            if let e = r.swS("agents_error") { msg += " The agents could not be connected: \(e)" }
            say(msg)
            await refreshCollections(force: true)
            await refreshStatus()
            return nil
        } catch { return SW.clean(error) }
    }

    func removeFolder(_ name: String, disconnect: Bool) async {
        do {
            var body: SWJSON = [:]
            if agentsAvailable == true { body["disconnect_agents"] = disconnect }
            let r = try await MSEClient.shared.postJSON("/api/collections/\(name)/remove", body: body)
            var msg = "Removed \(name). Its index was moved aside; your files are untouched."
            if let files = r.swO("agents")["files"] as? [SWJSON] { msg += " Agents disconnected (\(SWAgents.actions(files)))." }
            say(msg)
            openFolders.remove(name); persistOpen()
            await refreshCollections(force: true)
        } catch { say(SW.clean(error)) }
    }

    func toggleOpen(_ name: String) {
        if openFolders.contains(name) { openFolders.remove(name) } else { openFolders.insert(name) }
        persistOpen()
    }

    private func persistOpen() { UserDefaults.standard.set(Array(openFolders), forKey: "SWOpenFolders") }

    func loadHealth(_ name: String) async {
        if let o = try? await MSEClient.shared.get("/api/health", query: [URLQueryItem(name: "c", value: name)]) as? SWJSON { healthCache[name] = o }
    }

    func retryFailed(_ name: String) async {
        do {
            let r = try await MSEClient.shared.postJSON("/api/retry", body: ["collection": name])
            say("Retrying \(r.swI("retrying") ?? 0) files")
            await refreshStatus()
        } catch { say(SW.clean(error)) }
    }

    func testRule(_ name: String, _ patterns: [String]) async -> SWJSON? {
        try? await MSEClient.shared.postJSON("/api/test-rule", body: ["collection": name, "patterns": patterns])
    }

    // MARK: agents

    func probeAgents() async {
        guard agentsAvailable == nil else { return }
        do { let o = try await MSEClient.shared.get("/api/agents/global") as? SWJSON; agentsAvailable = true; globalAgents = o?.swR("agents"); mseBin = o?.swS("mse_bin") ?? mseBin }
        catch { if case APIError.http(404) = error { agentsAvailable = false } }
    }

    func loadAgents() async {
        await probeAgents()
        guard agentsAvailable == true else { return }
        if let o = try? await MSEClient.shared.get("/api/agents/global") as? SWJSON { globalAgents = o.swR("agents"); mseBin = o.swS("mse_bin") ?? mseBin }
        for f in folders { await loadFolderAgents(f.name) }
    }

    func loadFolderAgents(_ name: String) async {
        if let o = try? await MSEClient.shared.get("/api/collections/\(name)/agents") as? SWJSON { folderAgents[name] = o.swR("agents") }
    }

    func setFolderAgents(_ name: String, install: Bool) async {
        do {
            let r = try await MSEClient.shared.postJSON("/api/collections/\(name)/agents", body: ["install": install])
            say("\(install ? "Connected" : "Disconnected") \(name): \(SWAgents.actions(r["files"] as? [SWJSON] ?? [])).")
        } catch { say(SW.clean(error)) }
        await loadFolderAgents(name)
    }

    func setGlobalAgents(install: Bool) async {
        do {
            let r = try await MSEClient.shared.postJSON("/api/agents/global", body: ["install": install])
            say("\(install ? "Connected" : "Disconnected"): \(SWAgents.actions(r["files"] as? [SWJSON] ?? [])).")
        } catch { say(SW.clean(error)) }
        await loadAgents()
    }

    // MARK: favourites and quality

    func loadFavourites() async {
        do {
            let o = try await MSEClient.shared.get("/api/feedback") as? SWJSON
            favourites = o?.swR("items") ?? []; favAPI = true
        } catch { if case APIError.http(404) = error { favAPI = false } else if favAPI == nil { favAPI = nil } }
        finetune = try? await MSEClient.shared.get("/api/finetune") as? SWJSON
    }

    func removeFavourite(_ x: SWJSON) async {
        var body: SWJSON = [:]
        if let id = x.swI("id") { body["id"] = id }
        else { body = ["query": x["query"] ?? "", "collection": x["collection"] ?? "", "path": x["path"] ?? ""] }
        do { _ = try await MSEClient.shared.postJSON("/api/feedback/remove", body: body); await loadFavourites() }
        catch { say(SW.clean(error)) }
    }

    func loadQuality() async {
        quality = try? await MSEClient.shared.get("/api/quality") as? SWJSON
        if finetune == nil { finetune = try? await MSEClient.shared.get("/api/finetune") as? SWJSON }
    }

    // MARK: Ask providers

    func loadProviders() async {
        guard !providersLoading else { return }
        providersLoading = true
        defer { providersLoading = false }
        do { providers = try await MSEClient.shared.get("/api/ask/providers") as? SWJSON; providersAPI = true; errors["providers"] = nil }
        catch {
            if case APIError.http(404) = error { providersAPI = false; providers = nil }
            else { errors["providers"] = "The provider list could not be read: \(SW.clean(error))" }
        }
    }

    func loadLocalModels() async {
        localModels = try? await MSEClient.shared.get("/api/ask/local-models") as? SWJSON
    }

    func providerPost(_ path: String, _ body: SWJSON, key pid: String, doing: String) async -> SWJSON? {
        busy.insert(pid + doing)
        defer { busy.remove(pid + doing) }
        do { return try await MSEClient.shared.postJSON(path, body: body) }
        catch { providerNote[pid] = (SW.clean(error), false); return nil }
    }

    func saveKey(_ pid: String, _ key: String) async {
        guard !key.isEmpty else { return }
        if await providerPost("/api/ask/providers", ["id": pid, "key": key], key: pid, doing: "key") != nil {
            providerNote[pid] = ("Key saved in the macOS Keychain. It is never shown again.", true)
            flashSaved()
        }
        await loadProviders(); await refreshStatus()
    }

    func forgetKey(_ pid: String) async {
        if await providerPost("/api/ask/providers", ["id": pid, "forget_key": true], key: pid, doing: "forget") != nil {
            providerNote[pid] = ("Key removed from the Keychain.", true)
        }
        await loadProviders(); await refreshStatus()
    }

    func testProvider(_ pid: String, model: String?) async {
        var body: SWJSON = [:]
        if let model { body["model"] = model }
        guard let r = await providerPost("/api/ask/providers/\(pid)/test", body, key: pid, doing: "test") else { return }
        if r.swB("ok") == true {
            providerNote[pid] = ("Works: \(r.swS("model") ?? "model") answered in \(r.swI("ms") ?? 0) ms.", true)
        } else {
            providerNote[pid] = (r.swS("error") ?? "The test did not work.", false)
        }
    }

    func refreshModels(_ pid: String) async {
        guard let r = await providerPost("/api/ask/providers/\(pid)/models", [:], key: pid, doing: "models") else { return }
        providerNote[pid] = ("List refreshed: \((r["models"] as? [Any])?.count ?? r.swI("fetched") ?? 0) models.", true)
        await loadProviders()
    }

    /// A provider with a key file: the service reads the file, stores the key in the Keychain and lists the models.
    func connectProvider(_ pid: String) async {
        guard let r = await providerPost("/api/ask/providers/\(pid)/connect", [:], key: pid, doing: "connect") else { return }
        providerNote[pid] = ("Connected. The key stays in the Keychain." + ((r.swS("models_error")).map { " Models: \($0)" } ?? ""), true)
        await loadProviders(); await refreshStatus()
    }

    /// Whether the provider marks this model as one its provider may use inputs of for training (training_models patterns).
    func trainsOnInputs(provider: String, model: String) -> Bool {
        let row = (providers?.swR("providers") ?? []).first { $0.swS("id") == provider }
        return (row?.swA("training_models") ?? []).contains { AskText.glob($0, matches: model) }
    }

    func removeProvider(_ pid: String, forgetKey: Bool) async {
        if await providerPost("/api/ask/providers/\(pid)/remove", ["forget_key": forgetKey], key: pid, doing: "remove") != nil {
            say("Removed the provider \(pid).")
        }
        await loadProviders(); await refreshStatus()
    }

    func addProvider(_ body: SWJSON) async -> String? {
        do { _ = try await MSEClient.shared.postJSON("/api/ask/providers", body: body); await loadProviders(); await refreshStatus(); flashSaved(); return nil }
        catch { return SW.clean(error) }
    }

    // MARK: whole-Mac

    func loadWhole() async {
        if let o = try? await MSEClient.shared.get("/api/files/status") as? SWJSON { wholeStatus = o }
    }

    func rescanWhole() async {
        do {
            let r = try await MSEClient.shared.postJSON("/api/files/crawl", body: ["force": true])
            if r.swB("started") == false { say(r.swS("reason") ?? "A crawl is already running.") } else { say("Rescanning this Mac's file names.") }
        } catch { say(SW.clean(error)) }
        await loadWhole()
    }

    // MARK: about

    func computeModelSizes() {
        let model = settings.swO("model")
        let paths = [model.swS("path"), model.swS("reranker"), model.swO("asr").swS("path")].compactMap { $0 }.map(SW.expand)
        guard !paths.isEmpty else { return }
        Task.detached(priority: .utility) {
            var out: [String: Double] = [:]
            for p in paths { out[p] = SWModel.diskSize(p) }
            let result = out
            await MainActor.run { for (k, v) in result { self.modelSizes[k] = v } }
        }
    }

    nonisolated static func diskSize(_ path: String) -> Double {
        let fm = FileManager.default
        var isDir: ObjCBool = false
        guard fm.fileExists(atPath: path, isDirectory: &isDir) else { return -1 }
        if !isDir.boolValue { return ((try? fm.attributesOfItem(atPath: path))?[.size] as? NSNumber)?.doubleValue ?? 0 }
        var total = 0.0
        if let en = fm.enumerator(at: URL(fileURLWithPath: path), includingPropertiesForKeys: [.fileSizeKey], options: []) {
            for case let u as URL in en { total += Double((try? u.resourceValues(forKeys: [.fileSizeKey]))?.fileSize ?? 0) }
        }
        return total
    }
}

// MARK: - agents helpers shared by the Folders and Agents screens

enum SWAgents {
    /// "2 added, 1 already there" from the service's per-file actions.
    static func actions(_ files: [SWJSON]) -> String {
        var n: [String: Int] = [:]
        for f in files { n[f.swS("action") ?? "?", default: 0] += 1 }
        let words = ["added": "added", "updated": "updated", "present": "already there", "skipped": "skipped",
                     "removed": "removed", "absent": "not there", "kept": "kept"]
        let s = n.keys.sorted().map { "\(n[$0]!) \(words[$0] ?? $0)" }.joined(separator: ", ")
        return s.isEmpty ? "nothing to change" : s
    }

    struct Summary: Identifiable { let agent: String; let state: String; let detail: String; var id: String { agent } }

    /// One pill per app (Claude Code's approval file belongs to Claude Code).
    static func summary(_ rows: [SWJSON]) -> [Summary] {
        var order: [String] = [], by: [String: [SWJSON]] = [:]
        for r in rows {
            var a = r.swS("agent") ?? "?"
            if let range = a.range(of: " (approval)", options: [.caseInsensitive]) { a.removeSubrange(range) }
            if by[a] == nil { order.append(a) }
            by[a, default: []].append(r)
        }
        return order.map { a in
            let rs = by[a] ?? []
            let st = rs.map { $0.swS("state") ?? "" }
            let state = st.contains("unreadable") ? "unreadable" : st.contains("other") ? "other"
                : st.allSatisfy({ $0 == "connected" }) ? "connected" : st.contains("connected") ? "partial" : "missing"
            let detail = rs.map { r -> String in
                let note = r.swS("note").map { $0.isEmpty ? "" : " (\($0))" } ?? ""
                return "\(r.swS("file") ?? ""): \(r.swS("state") ?? "")\(note)"
            }.joined(separator: "\n")
            return Summary(agent: a, state: state, detail: detail)
        }
    }

    static func notes(_ rows: [SWJSON]) -> [String] {
        var out: [String] = []
        for r in rows { for n in (r.swS("note") ?? "").components(separatedBy: "; ") where !n.isEmpty {
            let t = n.prefix(1).uppercased() + n.dropFirst()
            if !out.contains(t) { out.append(t) }
        } }
        if rows.contains(where: { ($0.swS("agent") ?? "").lowercased().contains("codex") }), !out.contains(where: { $0.lowercased().contains("trust") }) {
            out.append("Codex reads a folder's own settings only in projects you have marked as trusted in Codex.")
        }
        return out
    }

    static func label(_ state: String) -> (String, SWTone) {
        switch state {
        case "connected": return ("connected", .ok)
        case "partial": return ("partly", .warn)
        case "missing": return ("not set up", .neutral)
        case "other": return ("set up differently", .warn)
        case "unreadable": return ("can't read", .bad)
        default: return (state, .neutral)
        }
    }
}
