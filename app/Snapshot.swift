// Off-screen snapshot mode, for headless checks:
//   MySearch --snapshot <out.png> --size <W>x<H> [--state home|documents|indexed|ask] [--dark]
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
        /// ask-*: the native Ask view (AskViewController.swift), against a stub service: empty, mid-answer (the stub holds
        /// the stream), finished with the steps folded, the folder picker two levels deep, Ask switched off, the model
        /// menu open, and a source open in the side preview.
        case askEmpty = "ask-empty", askStreaming = "ask-streaming", askDone = "ask-done", askPicker = "ask-picker"
        case askOff = "ask-off", askModel = "ask-model", askSource = "ask-source"
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
        while j < args.count {
            switch args[j] {
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
        return o
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
    if o.state == .groupEditor { runGroupEditorSnapshot(o); return }
    let appearance = NSAppearance(named: o.dark ? .darkAqua : .aqua)
    NSApp.appearance = appearance
    SidebarColors.snapshotRun = true
    let home = FileManager.default.homeDirectoryForCurrentUser
    let project = URL(fileURLWithPath: daemonProjectPath(), isDirectory: true)
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
    case .indexed, .searchIndexed, .searchPreview, .groupsSidebar, .searchIndexedFilters, .searchIndexedCustomDate: initial = .indexed
    case .ask, .askEmpty, .askStreaming, .askDone, .askPicker, .askOff, .askModel, .askSource: initial = .ask
    case .home, .documents, .searchMac, .searchCrawling, .listDocuments, .columnsDeep, .columnsFileThenFolder, .iconsDocuments,
         .info, .revealSearch, .groupEditor, .searchMacFilters: initial = .mac
    }
    controller.start(initial: initial)
    switch o.state {
    case .searchMac, .searchCrawling: controller.prepareSearch(query: "invoice", indexedPicks: 0)
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

@MainActor
final class SnapshotRunner {
    private let options: SnapshotOptions
    private let controller: MainWindowController
    private let started = Date()
    private var settledSince: Date?
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
        if controller.isBusy {
            settledSince = nil
        } else {
            if settledSince == nil { settledSince = Date() }
            if let since = settledSince, Date().timeIntervalSince(since) >= 0.8 { finish() }
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
        guard let data = rep.representation(using: .png, properties: [:]) else { snapshotFail("PNG encoding failed") }
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
