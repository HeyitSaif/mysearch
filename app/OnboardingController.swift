// The setup assistant (first run), part 3: the window, when it opens by itself, and the off-screen snapshot.
//
// It opens on launch when it has not been finished or skipped and there is something to set up: no folder yet, or the minimal
// models missing (GET /api/setup/status says so), or a fresh install whose background service does not exist yet. Settings >
// Mac App > "Setup Assistant…" opens it again whenever you like.

import Cocoa
import SwiftUI

@MainActor
final class OnboardingController: NSObject, NSWindowDelegate {
    static let shared = OnboardingController()
    private static let completedKey = "OnboardingCompleted"
    private static let skippedKey = "OnboardingSkipped"

    private var window: NSWindow?
    private var model: OnboardingModel?

    /// Opens the window (a new one each time it was closed). `step` is where it starts.
    func show(step: OBStep = .welcome) {
        if let window, let model {
            model.step = step
            NSApp.activate(ignoringOtherApps: true)
            window.makeKeyAndOrderFront(nil)
            return
        }
        let m = OnboardingModel()
        m.step = step
        m.onSkip = { [weak self] in self?.close(mark: Self.skippedKey) }
        m.onFinish = { [weak self] in
            self?.close(mark: Self.completedKey)
            (NSApp.delegate as? AppDelegate)?.showMain(focus: true)
        }
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 780, height: 560),
                         styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        w.title = "MySearch Setup"
        w.isReleasedWhenClosed = false
        w.tabbingMode = .disallowed
        w.delegate = self
        let host = NSHostingController(rootView: OnboardingView(m: m))
        host.sizingOptions = []
        w.contentViewController = host
        w.setContentSize(NSSize(width: 780, height: 560))
        w.center()
        m.window = w
        window = w
        model = m
        m.start()
        NSApp.activate(ignoringOtherApps: true)
        w.makeKeyAndOrderFront(nil)
    }

    /// Called once at launch: opens the assistant if it was neither finished nor skipped and something is left to set up.
    func showIfNeeded() {
        let d = UserDefaults.standard
        if d.bool(forKey: Self.completedKey) || d.bool(forKey: Self.skippedKey) { return }
        Task { @MainActor in
            // the service may still be starting after a login: give it a few seconds before judging
            for attempt in 0..<8 {
                if let s = try? await MSEClient.shared.get("/api/setup/status") as? SWJSON {
                    if s.swB("needs_setup") == true { self.show() }
                    return
                }
                if attempt == 0, self.freshInstall { self.show(); return }
                try? await Task.sleep(nanoseconds: 1_500_000_000)
            }
        }
    }

    /// A first launch of an app that carries its own runtime: no config yet, or no background service installed.
    private var freshInstall: Bool {
        guard OnboardingModel.bundledPython != nil else { return false }
        let fm = FileManager.default
        return !fm.fileExists(atPath: ServiceConfig.fileURL.path) || !fm.fileExists(atPath: OnboardingModel.launchAgentPlist.path)
    }

    private func close(mark key: String) {
        UserDefaults.standard.set(true, forKey: key)
        window?.close()
    }

    func windowWillClose(_ notification: Notification) {
        // closing the window without finishing counts as skipping: it does not come back by itself
        if !UserDefaults.standard.bool(forKey: Self.completedKey) { UserDefaults.standard.set(true, forKey: Self.skippedKey) }
        model?.stop()
        model = nil
        window = nil
    }
}

// MARK: - snapshot (--snapshot out.png --state onboarding[-welcome|-folders|-models|-access|-done])

extension SnapshotOptions.State {
    /// The assistant's step for the onboarding states; nil for every other state. Plain `onboarding` shows Models.
    var onboardingStep: OBStep? {
        switch self {
        case .onboarding, .onboardingModels: return .models
        case .onboardingWelcome: return .welcome
        case .onboardingFolders: return .folders
        case .onboardingAccess: return .access
        case .onboardingDone: return .done
        default: return nil
        }
    }
}

/// Renders the assistant off-screen with fictional data and no request to any service, then exits.
@MainActor
func runOnboardingSnapshot(_ o: SnapshotOptions, step: OBStep) {
    NSApp.appearance = NSAppearance(named: o.dark ? .darkAqua : .aqua)
    let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 780, height: 560), styleMask: [.titled], backing: .buffered, defer: false)
    window.appearance = NSApp.appearance
    window.title = "MySearch Setup"
    let model = OnboardingModel(fixture: step)
    let host = NSHostingController(rootView: OnboardingView(m: model))
    host.sizingOptions = []
    window.contentViewController = host
    window.setContentSize(NSSize(width: 780, height: 560))      // the content, not the frame: the title bar comes on top
    window.setFrameOrigin(NSPoint(x: -30000, y: -30000))        // far off-screen, never ordered in
    Task { @MainActor in
        try? await Task.sleep(nanoseconds: 600_000_000)
        guard let frameView = window.contentView?.superview else { snapshotFail("the assistant has no frame view") }
        window.layoutIfNeeded()
        frameView.layoutSubtreeIfNeeded()
        let bounds = frameView.bounds
        guard let rep = frameView.bitmapImageRepForCachingDisplay(in: bounds) else { snapshotFail("could not create a bitmap") }
        window.effectiveAppearance.performAsCurrentDrawingAppearance { frameView.cacheDisplay(in: bounds, to: rep) }
        guard let data = snapshotPNGData(rep) else { snapshotFail("PNG encoding failed") }
        let url = URL(fileURLWithPath: o.output)
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        do { try data.write(to: url) } catch { snapshotFail("could not write \(o.output)") }
        print(url.path)
        exit(0)
    }
}

// MARK: - Self-test (headless: --selftest-onboarding)

/// Reads what the assistant reads from a DEV service (MSE_API_BASE with MSE_API_TOKEN; never port 8737) through the real
/// OnboardingModel, so a key the service names differently from the app shows up here: the service state, the model manifest
/// and tiers, the job, Full Disk Access, adding a folder, and a cancel with nothing running. It starts no download and makes
/// no network request of its own. Opens no window and no alert.
@MainActor
enum OnboardingSelfTest {
    static func run() -> Never {
        let env = ProcessInfo.processInfo.environment
        guard let base = env["MSE_API_BASE"], let url = URL(string: base), url.port != 8737,
              let token = env["MSE_API_TOKEN"], !token.isEmpty else {
            print("SELFTEST refused: set MSE_API_BASE (a dev instance, not port 8737) and MSE_API_TOKEN")
            exit(3)
        }
        print("SELFTEST base \(base)")
        Task { @MainActor in exit(await steps()) }
        dispatchMain()
    }

    private static func steps() async -> Int32 {
        var failures = 0
        func check(_ label: String, _ ok: Bool, _ detail: String = "") {
            print("SELFTEST \(ok ? "PASS" : "FAIL") \(label)\(detail.isEmpty ? "" : ": " + detail)")
            if !ok { failures += 1 }
        }
        let m = OnboardingModel()
        await m.refreshService()
        if case .running(let v) = m.service { check("the service answers", !v.isEmpty, "version \(v)") } else { check("the service answers", false, "\(m.service)") }
        check("Continue is allowed on Welcome once the service runs", m.canContinue)
        await m.refreshModels()
        check("the manifest has the models of the service", m.models.count >= 4 && m.models.contains { $0.folder == "embeddinggemma-2-8bit" && $0.installable && $0.sizeBytes > 1_000_000_000 },
              "\(m.models.count) models")
        let minimal = m.tiers["minimal"], full = m.tiers["full"]
        check("tier sizes come through", (minimal?.totalBytes ?? 0) > 1_000_000_000 && (full?.totalBytes ?? 0) > (minimal?.totalBytes ?? 0),
              "minimal \(Int(minimal?.totalBytes ?? 0)) full \(Int(full?.totalBytes ?? 0))")
        check("the full set lists the model that has no public source as manual", full?.manual.contains("qwen3-reranker-0.6b-8bit") == true)
        check("no download is running", m.job.state == "idle" || m.job.state == "done", m.job.state)
        check("free disk space is reported", (m.freeBytes ?? 0) > 0)
        await m.refreshStatus()
        check("Full Disk Access is one of granted, not_granted, unknown", ["granted", "not_granted", "unknown"].contains(m.fullDiskAccess), m.fullDiskAccess)
        let before = m.folders.count
        let dir = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("mse-onboarding-selftest-\(ProcessInfo.processInfo.processIdentifier)", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        try? "hello".write(to: dir.appendingPathComponent("note.txt"), atomically: true, encoding: .utf8)
        m.addFolder(dir)
        var added = false
        for _ in 0..<40 {
            try? await Task.sleep(nanoseconds: 250_000_000)
            await m.refreshFolders()
            if m.folders.count > before { added = true; break }
        }
        check("a folder added through the assistant shows up in the list", added, "\(before) -> \(m.folders.count)")
        check("its path is the one chosen", m.folders.contains { ($0.path as NSString).resolvingSymlinksInPath == (dir.path as NSString).resolvingSymlinksInPath })
        m.cancelDownload()
        try? await Task.sleep(nanoseconds: 500_000_000)
        await m.refreshModels()
        check("a cancel with nothing running changes nothing", m.job.state != "running", m.job.state)
        print("SELFTEST RESULT: \(failures == 0 ? "all passed" : "\(failures) failed")")
        return failures == 0 ? 0 : 1
    }
}
