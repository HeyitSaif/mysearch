// The setup assistant (first run), part 1: the store. One @MainActor object holds what the five screens show and every call
// they make to the local service (GET /api/setup/status, GET and POST /api/setup/models, /api/collections), plus the one
// thing the app does itself: starting the background service when the app carries its own Python runtime.
//
// It is shown by OnboardingController.swift when there is no folder yet or the minimal models are missing, and can be
// reopened from Settings > Mac App. Nothing here prints or stores a credential. In snapshot mode (`--state onboarding-*`)
// it is filled with fictional data and makes no request at all.

import Cocoa
import SwiftUI

enum OBStep: Int, CaseIterable, Identifiable {
    case welcome, folders, models, access, done
    var id: Int { rawValue }

    var title: String {
        switch self {
        case .welcome: return "Welcome"
        case .folders: return "Folders"
        case .models: return "Models"
        case .access: return "Full Disk Access"
        case .done: return "Done"
        }
    }

    var symbol: String {
        switch self {
        case .welcome: return "magnifyingglass"
        case .folders: return "folder.fill"
        case .models: return "square.and.arrow.down.fill"
        case .access: return "lock.shield.fill"
        case .done: return "checkmark.seal.fill"
        }
    }

    var tint: Color {
        switch self {
        case .welcome: return .teal
        case .folders: return .blue
        case .models: return .purple
        case .access: return .orange
        case .done: return .green
        }
    }
}

enum OBService: Equatable {
    case unknown
    case notSetUp                      // the app has its own runtime and no background service is installed yet
    case starting
    case running(String)               // the service's version
    case failed(String)                // a sentence for the user
}

struct OBFolder: Identifiable, Equatable {
    let name: String, path: String
    let files: Int
    var id: String { name }
}

struct OBModelRow: Identifiable, Equatable {
    let folder: String, role: String, tier: String, status: String
    let sizeBytes: Double, installable: Bool
    var id: String { folder }
}

struct OBTier: Equatable {
    var todo: [String] = []
    var manual: [String] = []
    var needBytes: Double = 0
    var totalBytes: Double = 0
    var ready = false
}

struct OBJob: Equatable {
    var state = "idle"                 // idle | running | done | error | cancelled
    var tier: String?
    var totalBytes: Double = 0, doneBytes: Double = 0, percent: Double = 0
    var current: String?
    var rateBps: Double?, etaSeconds: Double?
    var error: String?
    var message = ""
    var models: [(folder: String, state: String)] = []

    var running: Bool { state == "running" }

    static func == (a: OBJob, b: OBJob) -> Bool {
        a.state == b.state && a.tier == b.tier && a.totalBytes == b.totalBytes && a.doneBytes == b.doneBytes
            && a.current == b.current && a.error == b.error && a.message == b.message
            && a.models.map { "\($0.folder)/\($0.state)" } == b.models.map { "\($0.folder)/\($0.state)" }
    }

    init() {}

    init(_ j: SWJSON) {
        state = j.swS("state") ?? "idle"
        tier = j.swS("tier")
        totalBytes = j.swD("total_bytes") ?? 0
        doneBytes = j.swD("done_bytes") ?? 0
        percent = j.swD("percent") ?? 0
        current = j.swS("current")
        rateBps = j.swD("rate_bps")
        etaSeconds = j.swD("eta_s")
        error = j.swS("error")
        message = j.swS("message") ?? ""
        models = j.swR("models").map { ($0.swS("folder") ?? "", $0.swS("state") ?? "pending") }
    }
}

@MainActor
final class OnboardingModel: ObservableObject {
    @Published var step: OBStep = .welcome
    @Published var service: OBService = .unknown
    @Published var folders: [OBFolder] = []
    @Published var tier = "minimal"
    @Published var models: [OBModelRow] = []
    @Published var tiers: [String: OBTier] = [:]
    @Published var job = OBJob()
    @Published var freeBytes: Double?
    @Published var fullDiskAccess = "unknown"          // granted | not_granted | unknown
    @Published var notice: String?
    @Published var addingFolder = false

    /// Set for a snapshot: fictional data, no requests, no polling.
    let isFixture: Bool
    var onFinish: (() -> Void)?
    var onSkip: (() -> Void)?
    weak var window: NSWindow?

    private var loop: Task<Void, Never>?
    private var startingService = false
    private var noticeTask: Task<Void, Never>?

    init(fixture step: OBStep? = nil) {
        isFixture = step != nil
        if let step { loadFixture(step) }
    }

    // MARK: where the app runs from

    /// The Python runtime inside this app, when the app was built with `--bundle-runtime`.
    static var bundledPython: URL? {
        let py = Bundle.main.resourceURL?.appendingPathComponent("runtime/bin/python3")
        return py.flatMap { FileManager.default.isExecutableFile(atPath: $0.path) ? $0 : nil }
    }

    /// Why the background service must not be installed from where this app is, or nil. A disk image goes away when ejected,
    /// and a quarantined app is run from a random read-only copy.
    static var unstableLocation: String? {
        let p = Bundle.main.bundlePath
        if p.hasPrefix("/Volumes/") { return "MySearch is running from a disk image or an external drive. Drag it to Applications first, then open it from there." }
        if p.contains("/AppTranslocation/") { return "macOS is running MySearch from a temporary copy. Move it to Applications, then open it again." }
        return nil
    }

    static var launchAgentPlist: URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/LaunchAgents/com.mysearch.daemon.plist")
    }

    // MARK: polling

    func start() {
        guard !isFixture else { return }
        stop()
        loop = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                if self.window?.isVisible ?? true { await self.refresh() }
                try? await Task.sleep(nanoseconds: 1_000_000_000)
            }
        }
    }

    func stop() { loop?.cancel(); loop = nil }

    func refresh() async {
        await refreshService()
        guard case .running = service else { return }
        if step == .folders || step == .done { await refreshFolders() }
        if step == .models || step == .done || job.running { await refreshModels() }
        if step == .access || step == .done { await refreshStatus() }
    }

    @discardableResult
    func refreshStatus() async -> SWJSON? {
        guard let s = try? await MSEClient.shared.get("/api/setup/status") as? SWJSON else { return nil }
        fullDiskAccess = s.swS("full_disk_access") ?? "unknown"
        return s
    }

    func refreshService() async {
        if startingService { return }
        if let s = await refreshStatus() {
            service = .running(s.swS("version") ?? "")
            return
        }
        if case .running = service { service = .failed("The search service stopped answering."); return }
        if let why = Self.unstableLocation { service = .failed(why); return }
        if case .failed = service { return }
        service = Self.bundledPython != nil ? .notSetUp
            : .failed("The search service is not running. It starts from the project's Python environment: run `mse service install` in Terminal.")
    }

    func refreshFolders() async {
        guard let o = try? await MSEClient.shared.get("/api/collections") as? SWJSON else { return }
        folders = o.swR("collections").compactMap { r in
            guard let name = r.swS("name"), let path = r.swS("path") else { return nil }
            return OBFolder(name: name, path: path, files: r.swO("stats").swI("files") ?? 0)
        }
    }

    func refreshModels() async {
        guard let o = try? await MSEClient.shared.get("/api/setup/models") as? SWJSON else { return }
        apply(models: o)
    }

    private func apply(models o: SWJSON) {
        models = o.swR("models").map {
            OBModelRow(folder: $0.swS("folder") ?? "", role: $0.swS("role") ?? "", tier: $0.swS("tier") ?? "", status: $0.swS("status") ?? "",
                       sizeBytes: $0.swD("download_bytes") ?? 0, installable: $0.swB("installable") ?? false)
        }
        var t: [String: OBTier] = [:]
        for (name, v) in o.swO("tiers") {
            guard let j = v as? SWJSON else { continue }
            t[name] = OBTier(todo: j.swA("todo"), manual: j.swA("manual"), needBytes: j.swD("need_bytes") ?? 0,
                             totalBytes: j.swD("total_bytes") ?? 0, ready: j.swB("ready") ?? false)
        }
        tiers = t
        freeBytes = o.swD("free_bytes")
        job = OBJob(o.swO("job"))
    }

    // MARK: actions

    func say(_ text: String) {
        notice = text
        noticeTask?.cancel()
        noticeTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 5_000_000_000)
            if !Task.isCancelled { self?.notice = nil }
        }
    }

    /// Welcome > "Start MySearch": installs the LaunchAgent with the app's own Python (`mse service install`), which launchd then
    /// starts as `MySearch --daemon`, and waits until the service answers.
    func startService() {
        guard !isFixture, !startingService else { return }
        if let why = Self.unstableLocation { service = .failed(why); return }
        guard let python = Self.bundledPython, let resources = Bundle.main.resourceURL else {
            service = .failed("This build of MySearch has no Python runtime of its own. Run `mse service install` in Terminal.")
            return
        }
        startingService = true
        service = .starting
        let exe = Bundle.main.executablePath ?? ""
        Task { [weak self] in
            let result: (Int32, String) = await Task.detached(priority: .userInitiated) {
                let p = Process()
                p.executableURL = python
                p.arguments = ["-m", "mse", "service", "install"]
                var env = ProcessInfo.processInfo.environment
                env["PYTHONHOME"] = resources.appendingPathComponent("runtime").path
                env["PYTHONPATH"] = resources.path
                env["PYTHONNOUSERSITE"] = "1"
                env["PYTHONDONTWRITEBYTECODE"] = "1"
                env["MSE_APP_EXECUTABLE"] = exe
                p.environment = env
                let pipe = Pipe()
                p.standardOutput = pipe
                p.standardError = pipe
                do { try p.run() } catch { return (1, error.localizedDescription) }
                let out = pipe.fileHandleForReading.readDataToEndOfFile()
                p.waitUntilExit()
                return (p.terminationStatus, String(decoding: out, as: UTF8.self))
            }.value
            guard let self else { return }
            if result.0 != 0 {
                self.startingService = false
                let line = result.1.split(separator: "\n").last.map(String.init) ?? "the installer failed"
                self.service = .failed("The background service could not be set up: \(line)")
                return
            }
            // launchd starts it; the first start creates config.json (with the access token), then the port opens
            for _ in 0..<60 {
                try? await Task.sleep(nanoseconds: 500_000_000)
                if await self.refreshStatus() != nil { break }
            }
            self.startingService = false
            await self.refreshService()
            if case .running = self.service { return }
            self.service = .failed("The service did not answer within 30 seconds. See ~/Library/Logs/MySearch/launchd.log.")
        }
    }

    func addFolder(_ url: URL) {
        guard !isFixture else { return }
        addingFolder = true
        Task { [weak self] in
            do {
                _ = try await MSEClient.shared.postJSON("/api/collections", body: ["path": url.path])
                self?.say("Added \(url.lastPathComponent). Text is searchable within minutes.")
            } catch { self?.say(SW.clean(error)) }
            await self?.refreshFolders()
            self?.addingFolder = false
        }
    }

    func chooseFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = true
        panel.prompt = "Add"
        panel.message = "Choose the folders MySearch should index. Your files are only read, never changed."
        let done: (NSApplication.ModalResponse) -> Void = { [weak self] r in
            guard r == .OK else { return }
            for u in panel.urls { self?.addFolder(u) }
        }
        if let w = window { panel.beginSheetModal(for: w, completionHandler: done) } else { done(panel.runModal()) }
    }

    /// Folders worth offering with one click: the ones that exist and are not indexed yet.
    var suggestions: [URL] {
        let home = FileManager.default.homeDirectoryForCurrentUser
        let have = Set(folders.map { ($0.path as NSString).standardizingPath })
        return ["Documents", "Desktop"].map { home.appendingPathComponent($0, isDirectory: true) }
            .filter { FileManager.default.fileExists(atPath: $0.path) && !have.contains(($0.path as NSString).standardizingPath) }
    }

    func startDownload() {
        guard !isFixture else { return }
        let chosen = tier
        Task { [weak self] in
            do {
                let r = try await MSEClient.shared.postJSON("/api/setup/models", body: ["tier": chosen])
                self?.job = OBJob(r)
            } catch { self?.say(SW.clean(error)) }
            await self?.refreshModels()
        }
    }

    func cancelDownload() {
        guard !isFixture else { return }
        Task { [weak self] in
            _ = try? await MSEClient.shared.postJSON("/api/setup/models/cancel", body: [:])
            await self?.refreshModels()
        }
    }

    /// System Settings > Privacy & Security > Full Disk Access (the same pane the Whole-Mac settings open).
    func openFullDiskAccess() {
        if let u = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles") { NSWorkspace.shared.open(u) }
    }

    func revealApp() { NSWorkspace.shared.activateFileViewerSelecting([Bundle.main.bundleURL]) }

    // MARK: moving between steps

    var minimalReady: Bool { tiers["minimal"]?.ready ?? false }
    var canContinue: Bool {
        switch step {
        case .welcome: if case .running = service { return true } else { return false }
        case .folders, .access, .done: return true
        case .models: return minimalReady && !job.running
        }
    }

    func next() {
        if let n = OBStep(rawValue: step.rawValue + 1) { step = n } else { onFinish?() }
    }

    func back() {
        if let p = OBStep(rawValue: step.rawValue - 1) { step = p }
    }

    // MARK: fictional data for the snapshots

    private func loadFixture(_ s: OBStep) {
        step = s
        service = .running("0.1.2")
        folders = [OBFolder(name: "notes", path: "~/Documents/Notes", files: 1284),
                   OBFolder(name: "receipts-2025", path: "~/Documents/Receipts 2025", files: 342),
                   OBFolder(name: "alpha", path: "~/Projects/Alpha", files: 5120)]
        let rows: [(String, String, String, String, Double)] = [
            ("embeddinggemma-2-8bit", "text, images and pages", "minimal", "missing", 1_266_584_562),
            ("whisper-large-v3-turbo-8bit", "speech to text", "full", "missing", 869_093_446),
            ("qwen3-4b-instruct-2507-4bit", "local answers for Ask", "full", "missing", 2_278_972_236),
        ]
        models = rows.map { OBModelRow(folder: $0.0, role: $0.1, tier: $0.2, status: $0.3, sizeBytes: $0.4, installable: true) }
        tiers = ["minimal": OBTier(todo: ["embeddinggemma-2-8bit"], manual: [], needBytes: 1_266_584_562, totalBytes: 1_266_584_562, ready: false),
                 "full": OBTier(todo: rows.map { $0.0 }, manual: ["qwen3-reranker-0.6b-8bit"], needBytes: 4_414_650_244, totalBytes: 4_414_650_244, ready: false)]
        freeBytes = 212_000_000_000
        tier = "minimal"
        fullDiskAccess = "not_granted"
        if s == .models {
            var j = OBJob()
            j.state = "running"; j.tier = "minimal"
            j.totalBytes = 1_266_584_562; j.doneBytes = 481_000_000; j.percent = 38
            j.current = "embeddinggemma-2-8bit"; j.rateBps = 26_000_000; j.etaSeconds = 30
            j.message = "Downloading embeddinggemma-2-8bit"
            j.models = [("embeddinggemma-2-8bit", "downloading")]
            job = j
        }
        if s == .done {
            tiers["minimal"]?.ready = true
            tiers["minimal"]?.todo = []
            fullDiskAccess = "granted"
        }
    }
}
