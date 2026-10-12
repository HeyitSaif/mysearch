// Off-screen snapshot mode, for headless checks:
//   MySearch --snapshot <out.png> --size <W>x<H> [--state home|documents|indexed|ask|onboarding] [--dark]
// The main window is built in the requested state, positioned far off-screen and never ordered in. The app
// runs as a prohibited (no Dock icon, no menu bar) process: no hot keys, status item, URL handling, Quick Search
// or autosaved frames. When the content has settled, the whole window (toolbar included) is rendered into a PNG
// with cacheDisplay, the web pane is composited in from a WebKit snapshot, and the process exits.

import Cocoa
import WebKit

struct SnapshotOptions {
    /// search-mac / search-indexed / search-crawling: the native search view with the query "invoice"
    /// (Indexed adds two picked folders); crawling needs the stub run with --crawl.
    enum State: String {
        case home, documents, indexed, ask
        case searchMac = "search-mac", searchIndexed = "search-indexed", searchCrawling = "search-crawling"
        /// search-preview: Indexed content match "invoice" -> the web Search view with that file previewed (★ Favourite…).
        case searchPreview = "search-preview"
        /// list-documents: ~/Documents in the list view, sorted by Date Modified, with the project's grandparent folder expanded.
        case listDocuments = "list-documents"
        /// columns-deep: the column view at the project's parent folder, with app/Model.swift selected and previewed.
        case columnsDeep = "columns-deep"
        /// columns-file-then-folder: columns-deep, then the project's docs/ folder (the sibling of app/) is selected in the
        /// same column. No preview and no empty columns may be left behind, and the folder columns keep their normal width.
        case columnsFileThenFolder = "columns-file-then-folder"
        /// icons-documents: the project folder (service.project) in the icon view, with thumbnails.
        case iconsDocuments = "icons-documents"
        /// info: the Get Info panel for the project's app/ folder (its size is added up first).
        case info
        /// reveal-search: a whole-Mac search for "invoice", then ⌘↵ on a row (the stub's file is shown in its folder, selected there).
        case revealSearch = "reveal-search"
        /// groups-sidebar: the Indexed sidebar with its groups expanded (member counts and the "+" button).
        case groupsSidebar = "groups-sidebar"
        /// group-editor: the group editor sheet with its members ticked (rendered on its own, see GroupEditor.swift).
        case groupEditor = "group-editor"
        /// search-indexed-filters: Indexed search "invoice" with the filter bar set (type PDF, past month, exact words,
        /// globs, newest first) and the facet chips shown. search-indexed-custom-date: the same with the custom range sheet open.
        /// search-mac-filters: the whole-Mac search "invoice" with the bar set (type PDF, this year, globs, a folder excluded).
        case searchIndexedFilters = "search-indexed-filters"
        case searchIndexedCustomDate = "search-indexed-custom-date"
        case searchMacFilters = "search-mac-filters"
        /// search-mac-disk: the main window's Mac mode with "zebra": the file names, then Contents on this Mac (the first page
        /// of the fictional whole-disk hits, with Show more). The whole-disk settings are the snapshot fixture (SnapshotQuickSearch.swift).
        case searchMacDisk = "search-mac-disk"
        /// settings-wholedisk and settings-folders-deep: the Whole-disk Search pane, and the Folders pane with its Deep indexed
        /// heading and the English + German note (SnapshotSettingsExtra.swift).
        case settingsWholeDisk = "settings-wholedisk", settingsFoldersDeep = "settings-folders-deep"
        /// ask-*: the native Ask view (AskViewController.swift), against a stub service: empty, mid-answer (the stub holds
        /// the stream), finished with the steps folded, the folder picker two levels deep, Ask switched off, the model
        /// menu open, and a source open in the side preview.
        case askEmpty = "ask-empty", askStreaming = "ask-streaming", askDone = "ask-done", askPicker = "ask-picker"
        case askOff = "ask-off", askModel = "ask-model", askSource = "ask-source"
        /// onboarding-*: the first-run setup assistant (OnboardingView.swift) with fictional data and no request to any service.
        /// Plain `onboarding` is the Models step with a download half done.
        case onboarding
        case onboardingWelcome = "onboarding-welcome", onboardingFolders = "onboarding-folders", onboardingModels = "onboarding-models"
        case onboardingAccess = "onboarding-access", onboardingDone = "onboarding-done"
        /// search-indexed-docs: the Indexed search "type:invoice amount:>100" over two folders, with the Document row set
        /// (Invoice, From 100): the results carry their type badges, counterparties, amounts and dates.
        /// saved-sidebar: the Indexed search of a saved search, with the Saved section listing the saved searches.
        /// timeline: the Timeline of receipts in the same folders, with the months that have none marked.
        case searchIndexedDocs = "search-indexed-docs", savedSidebar = "saved-sidebar", timeline = "timeline"
        /// search-indexed-chips: Indexed search "invoice" with file filters (PDF, past month, include *.pdf) and document filters
        /// (type:invoice amount:>100) set and More filters closed: every filter shows as a removable chip.
        /// search-indexed-more: the same with More filters open (the method, the document filters and the globs).
        case searchIndexedChips = "search-indexed-chips", searchIndexedMore = "search-indexed-more"
        /// search-empty: the Indexed search of search-indexed-docs with a word nothing contains: no results, so the shared
        /// empty state (EmptyStateView, SearchView.swift) is shown over the results.
        case searchEmpty = "search-empty"
        /// quick-search, quick-search-empty, quick-search-dark: the native Quick Search panel (SnapshotQuickSearch.swift).
        case quickSearch = "quick-search", quickSearchEmpty = "quick-search-empty", quickSearchDark = "quick-search-dark"
        /// quick-search-disk: the same panel, the query "zebra" in Whole Mac scope, with the On this Mac section (SnapshotQuickSearch.swift).
        case quickSearchDisk = "quick-search-disk"
    }

    var output: String
    var width: CGFloat = 1440
    var height: CGFloat = 900
    var state: State = .home
    var dark = false

    /// Nil when --snapshot is absent. A malformed snapshot command line is an error (exit 2), not a normal start.
    static func parse(_ args: [String]) -> SnapshotOptions? {
        guard let i = args.firstIndex(of: "--snapshot") else { return nil }
        guard i + 1 < args.count, !args[i + 1].hasPrefix("--") else { snapshotFail("missing output path", code: 2) }
        var o = SnapshotOptions(output: args[i + 1])
        var j = i + 2
        var fixturePath = ProcessInfo.processInfo.environment["MSE_SNAPSHOT_FIXTURE"].flatMap { $0.isEmpty ? nil : $0 }
        while j < args.count {
            switch args[j] {
            case "--fixture":
                guard j + 1 < args.count, !args[j + 1].hasPrefix("--") else { snapshotFail("--fixture needs a folder", code: 2) }
                fixturePath = args[j + 1]
                j += 2
            case "--size":
                guard j + 1 < args.count else { snapshotFail("--size needs WxH", code: 2) }
                let parts = args[j + 1].split(separator: "x").compactMap { Double($0) }
                guard parts.count == 2, parts[0] >= 200, parts[1] >= 200 else { snapshotFail("bad --size", code: 2) }
                o.width = CGFloat(parts[0]); o.height = CGFloat(parts[1])
                j += 2
            case "--state":
                guard j + 1 < args.count, let s = State(rawValue: args[j + 1]) else { snapshotFail("bad --state", code: 2) }
                o.state = s
                j += 2
            case "--dark":
                o.dark = true
                j += 1
            default:
                snapshotFail("unknown argument \(args[j])", code: 2)
            }
        }
        if let fixturePath { SnapshotFixture.root = SnapshotFixture.adopt(fixturePath) }
        return o
    }
}

/// Fixture mode, for the golden snapshots (scripts/snapshot_check.sh). Every view is rooted at a fictional folder tree
/// made by scripts/fixtures/make_snapshot_tree.sh, the sidebar shows fixed fictional rows, and the service is a stub
/// (scripts/fixtures/stub_service.py). Enabled by --fixture <dir> or MSE_SNAPSHOT_FIXTURE=<dir>, only together with
/// --snapshot. In fixture mode nothing reads the Mac's own recents, pins, sidebar memory or cloud and volume lists,
/// and nothing writes them.
enum SnapshotFixture {
    /// The fixture folder; nil outside fixture mode. Set once by SnapshotOptions.parse, before the app is built.
    nonisolated(unsafe) static var root: URL?
    static var isActive: Bool { root != nil }
    /// The marker make_snapshot_tree.sh writes into every fixture. A folder without it is refused.
    static let markerName = ".mse-snapshot-fixture"

    /// The fictional home folder: <root>/home.
    static var home: URL? { child("home") }
    /// The fictional project the states open: app/Model.swift and docs/ sit inside it.
    static var project: URL? { home?.appendingPathComponent("Documents/Studio/Projects/Alpha", isDirectory: true) }
    /// A folder directly inside the fixture root (the stand-ins for the boot volume, a cloud drive and an external drive).
    static func child(_ name: String) -> URL? { root?.appendingPathComponent(name, isDirectory: true) }

    /// The fixture at `path`, checked: it must carry the marker and a home folder, so no real folder is ever used.
    static func adopt(_ path: String) -> URL {
        let url = URL(fileURLWithPath: path, isDirectory: true)
        let fm = FileManager.default
        guard fm.fileExists(atPath: url.appendingPathComponent(markerName).path),
              fm.fileExists(atPath: url.appendingPathComponent("home").path) else {
            snapshotFail("\(path) is not a snapshot fixture (run scripts/fixtures/make_snapshot_tree.sh first)", code: 2)
        }
        return url
    }
}

/// Writes to stderr and exits. Snapshot mode never shows an alert.
func snapshotFail(_ message: String, code: Int32 = 1) -> Never {
    fputs("MySearch snapshot: \(message)\n", stderr)
    exit(code)
}

@MainActor
private var activeSnapshot: SnapshotRunner?

/// Builds the window in the requested state and starts waiting for it to settle.
@MainActor
func runSnapshot(_ o: SnapshotOptions) {
    if runSettingsExtraSnapshot(o) { return }      // SnapshotSettingsExtra.swift: MSE_SNAPSHOT_SETTINGS (Settings panes, Classic alert, probe)
    if o.state == .groupEditor { runGroupEditorSnapshot(o); return }
    if o.state.isQuickSearch { runQuickSearchSnapshot(o); return }   // SnapshotQuickSearch.swift
    if let step = o.state.onboardingStep { runOnboardingSnapshot(o, step: step); return }   // OnboardingController.swift
    let appearance = NSAppearance(named: o.dark ? .darkAqua : .aqua)
    NSApp.appearance = appearance
    SidebarColors.snapshotRun = true
    SearchFilterBar.snapshotStartsOpen = o.state == .searchIndexedMore      // read when the window's search view is built
    if o.state == .searchMacDisk { WholeDiskStore.shared.apply(WholeDiskSettings.snapshotFixture) }   // the --disk stub's values
    // In fixture mode the home folder and the project are the fictional ones; otherwise the user's home and the service's project.
    let home = SnapshotFixture.home ?? FileManager.default.homeDirectoryForCurrentUser
    let project = SnapshotFixture.project ?? URL(fileURLWithPath: daemonProjectPath(), isDirectory: true)
    let folder: URL
    switch o.state {
    case .documents, .listDocuments: folder = home.appendingPathComponent("Documents")
    case .columnsDeep, .columnsFileThenFolder: folder = project.deletingLastPathComponent()
    case .iconsDocuments, .info, .revealSearch: folder = project
    default: folder = home
    }
    let frame = NSRect(x: -30000, y: -30000, width: o.width, height: o.height)
    let controller = MainWindowController(frame: frame, snapshot: true, startFolder: folder)
    controller.window.appearance = appearance
    let initial: MainWindowController.Mode
    switch o.state {
    case .indexed, .searchIndexed, .searchPreview, .groupsSidebar, .searchIndexedFilters, .searchIndexedCustomDate,
         .searchIndexedDocs, .savedSidebar, .timeline, .searchIndexedChips, .searchIndexedMore, .searchEmpty: initial = .indexed
    case .ask, .askEmpty, .askStreaming, .askDone, .askPicker, .askOff, .askModel, .askSource: initial = .ask
    case .quickSearch, .quickSearchEmpty, .quickSearchDark, .quickSearchDisk: initial = .mac   // returned before this by runQuickSearchSnapshot
    case .home, .documents, .searchMac, .searchCrawling, .listDocuments, .columnsDeep, .columnsFileThenFolder, .iconsDocuments,
         .info, .revealSearch, .groupEditor, .searchMacFilters, .searchMacDisk, .settingsWholeDisk, .settingsFoldersDeep,
         .onboarding, .onboardingWelcome, .onboardingFolders, .onboardingModels, .onboardingAccess, .onboardingDone: initial = .mac
    }
    controller.start(initial: initial)
    // Fixture mode: the path bar gets a fixed width. Its intrinsic width is the whole path, which here is the scratch
    // folder's, and that width sized the window of the column views, so the picture changed from run to run.
    if SnapshotFixture.isActive { fixPathBarWidth(in: controller.window.contentView) }
    switch o.state {
    case .searchMac, .searchCrawling: controller.prepareSearch(query: "invoice", indexedPicks: 0)
    case .searchMacDisk: controller.prepareSearch(query: "zebra", indexedPicks: 0)
    case .searchIndexed: controller.prepareSearch(query: "invoice", indexedPicks: 2)
    case .searchIndexedFilters:
        controller.search.applyFilters(SnapshotFilters.indexed)
        controller.prepareSearch(query: "invoice", indexedPicks: 2)
    case .searchIndexedCustomDate:
        controller.search.applyFilters(SnapshotFilters.indexed)
        controller.prepareSearch(query: "invoice", indexedPicks: 2)
        controller.search.snapshotOpenCustomDate()
    case .searchMacFilters:
        controller.search.applyFilters(SnapshotFilters.mac)
        controller.prepareSearch(query: "invoice", indexedPicks: 0)
    case .searchIndexedDocs:
        controller.prepareSearch(query: "type:invoice amount:>100", indexedPicks: 2)
    case .searchEmpty:
        controller.prepareSearch(query: "type:invoice amount:>100 zebra", indexedPicks: 2)
    case .searchIndexedChips, .searchIndexedMore:
        controller.search.applyFilters(SnapshotFilters.chips)
        controller.prepareSearch(query: "type:invoice amount:>100", indexedPicks: 2)
    case .savedSidebar:
        // a click on the first saved search, once the list has arrived: the window runs it with its folders
        controller.adjustBusy(1)
        Task { @MainActor in
            defer { controller.adjustBusy(-1) }
            for _ in 0..<200 where controller.sidebar.savedCount == 0 { try? await Task.sleep(nanoseconds: 50_000_000) }
            controller.sidebar.selectSavedForSnapshot(0)
        }
    case .timeline:
        controller.search.snapshotShowTimelineOnRun()
        controller.prepareSearch(query: "type:receipt", indexedPicks: 2)
    case .searchPreview:
        controller.snapshotPreview(query: "acme invoice", collection: "company", path: "invoices/2025/2025-03_rechnung_acme.md")
    case .listDocuments:
        controller.listView.setSort(.modified, ascending: false)
        controller.listView.reveal(project.deletingLastPathComponent().deletingLastPathComponent(), expandLast: true)
    case .columnsDeep:
        controller.setViewStyle(.columns)
        controller.columnView.reveal(project.appendingPathComponent("app/Model.swift"))
    case .columnsFileThenFolder:
        controller.setViewStyle(.columns)
        let docs = project.appendingPathComponent("docs")
        controller.columnView.reveal(project.appendingPathComponent("app/Model.swift")) {
            controller.columnView.reveal(docs)
        }
    case .revealSearch:
        controller.prepareSearch(query: "invoice", indexedPicks: 0)
        controller.snapshotReveal(project.appendingPathComponent("app/Navigator.swift"))
    case .iconsDocuments:
        controller.setViewStyle(.icons)
    case .info:
        InfoPanelController.shared.show([FileItem(url: project.appendingPathComponent("app"))], orderFront: false)
    case .askEmpty, .askStreaming, .askDone, .askPicker, .askOff, .askModel, .askSource:
        controller.snapshotAsk(o.state.rawValue)
    default: break
    }
    activeSnapshot = SnapshotRunner(options: o, controller: controller)
    activeSnapshot?.begin()
}

/// Fixture mode only: gives every path control a fixed width (see runSnapshot). The control stays in the window, because
/// the footer's layout depends on it; it is hidden at render time (hideMacOnlyFooter).
@MainActor
private func fixPathBarWidth(in view: NSView?) {
    guard let view else { return }
    for sub in view.subviews {
        if sub is NSPathControl {
            let width = sub.widthAnchor.constraint(equalToConstant: 320)
            width.priority = .defaultHigh
            width.isActive = true
        } else {
            fixPathBarWidth(in: sub)
        }
    }
}

/// Fixture mode only. The footer's path bar draws this Mac's boot volume name and the folder path down to the fixture,
/// and the status line gives the free space of the real volume. A fixture cannot change those, so they are hidden in the
/// picture (the path bar is drawn by ContentViews.swift, which this check does not own).
@MainActor
private func hideMacOnlyFooter(in view: NSView?) {
    guard let view else { return }
    if view is NSPathControl { view.isHidden = true }
    if let field = view as? NSTextField, field.stringValue.contains("available") { field.isHidden = true }
    for sub in view.subviews { hideMacOnlyFooter(in: sub) }
}

@MainActor
final class SnapshotRunner {
    private let options: SnapshotOptions
    private let controller: MainWindowController
    private let started = Date()
    private var settledSince: Date?
    /// The window's frame at the last tick: a window still growing (the column view widens it) is not settled.
    private var lastFrame: NSRect?
    private var timer: Timer?

    init(options: SnapshotOptions, controller: MainWindowController) {
        self.options = options
        self.controller = controller
    }

    func begin() {
        // a hard limit, so a stuck load can never hold the process
        DispatchQueue.main.asyncAfter(deadline: .now() + 90) {
            snapshotFail("timed out after 90 s", code: 2)
        }
        timer = Timer.scheduledTimer(withTimeInterval: 0.2, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.tick() }
        }
    }

    /// Renders once nothing has been loading for a moment. After 60 s it renders whatever is there.
    private func tick() {
        let elapsed = Date().timeIntervalSince(started)
        let frame = controller.window.frame
        let frameChanged = frame != lastFrame
        lastFrame = frame
        if controller.isBusy || frameChanged {
            settledSince = nil
        } else {
            if settledSince == nil { settledSince = Date() }
            // a fixture render waits longer, so that a late layout pass cannot change the picture after it is taken
            let quiet: TimeInterval = SnapshotFixture.isActive ? 1.5 : 0.8
            if let since = settledSince, Date().timeIntervalSince(since) >= quiet { finish() }
        }
        if elapsed > 60 { finish() }
    }

    private func finish() {
        timer?.invalidate()
        timer = nil
        render()
    }

    private func render() {
        let window: NSWindow = options.state == .info ? InfoPanelController.shared.panel : controller.window
        if SnapshotFixture.isActive { hideMacOnlyFooter(in: window.contentView) }
        if options.state == .info {
            window.appearance = NSApp.appearance
            if let content = window.contentView {
                content.layoutSubtreeIfNeeded()
                window.setContentSize(content.fittingSize)
            }
        }
        guard let frameView = window.contentView?.superview else { snapshotFail("the window has no frame view") }
        // The toolbar and titlebar live in the frame view, outside contentView, so the whole frame is rendered.
        window.layoutIfNeeded()
        window.contentView?.layoutSubtreeIfNeeded()
        frameView.layoutSubtreeIfNeeded()
        let bounds = frameView.bounds
        guard let rep = frameView.bitmapImageRepForCachingDisplay(in: bounds) else {
            snapshotFail("could not create a bitmap")
        }
        window.effectiveAppearance.performAsCurrentDrawingAppearance {
            frameView.cacheDisplay(in: bounds, to: rep)
        }
        // A sheet is its own window, so it is not in the main window's picture: its frame view is drawn in at its place.
        if let sheet = controller.search.openSheet, let sheetView = sheet.contentView?.superview,
           let sheetRep = sheetView.bitmapImageRepForCachingDisplay(in: sheetView.bounds) {
            sheetView.layoutSubtreeIfNeeded()
            sheet.appearance = window.appearance
            sheet.effectiveAppearance.performAsCurrentDrawingAppearance {
                sheetView.cacheDisplay(in: sheetView.bounds, to: sheetRep)
            }
            let at = NSRect(x: sheet.frame.minX - window.frame.minX, y: sheet.frame.minY - window.frame.minY,
                            width: sheet.frame.width, height: sheet.frame.height)
            if let ctx = NSGraphicsContext(bitmapImageRep: rep) {
                NSGraphicsContext.saveGraphicsState()
                NSGraphicsContext.current = ctx
                sheetRep.draw(in: at)
                NSGraphicsContext.restoreGraphicsState()
            }
        }
        guard options.state == .searchPreview else { write(rep); return }          // Ask is native now: no web composite
        // WKWebView does not draw through cacheDisplay, so its pixels come from a WebKit snapshot.
        let web = controller.webPane.web
        let rect = web.convert(web.bounds, to: frameView)
        web.takeSnapshot(with: nil) { [weak self] image, _ in
            MainActor.assumeIsolated {
                if let image, let ctx = NSGraphicsContext(bitmapImageRep: rep) {
                    NSGraphicsContext.saveGraphicsState()
                    NSGraphicsContext.current = ctx
                    image.draw(in: rect)
                    NSGraphicsContext.restoreGraphicsState()
                }
                if let self { self.write(rep) } else { snapshotFail("snapshot ended early") }
            }
        }
    }

    private func write(_ rep: NSBitmapImageRep) {
        guard let data = snapshotPNGData(rep) else { snapshotFail("PNG encoding failed") }   // sRGB, no profile, no metadata (SnapshotPNG.swift)
        let url = URL(fileURLWithPath: options.output)
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        do {
            try data.write(to: url)
        } catch {
            snapshotFail("could not write \(options.output)")
        }
        print(url.path)
        exit(0)
    }
}
