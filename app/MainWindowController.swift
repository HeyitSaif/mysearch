// The main window, in the Finder's shape: a unified toolbar (back and forward, the Mac | Indexed | Ask mode
// switch, the Icons | List | Columns view switch, search), a source-list sidebar, and a content area with a path
// bar and a status line. The content area shows the folder placeholder (Mac), the collection placeholder
// (Indexed) or the web UI's Ask page (Ask). Other agents replace the content by changing showFolder(),
// showIndexed() or showAsk().

import Cocoa

/// The window of an off-screen snapshot: it is never ordered in, so AppKit would draw it as an inactive window
/// (dimmed sidebar and toolbar). It reports itself key and main so the render shows the real, active look.
final class SnapshotWindow: NSWindow {
    override var isKeyWindow: Bool { true }
    override var isMainWindow: Bool { true }
}

@MainActor
final class MainWindowController: NSObject, NSWindowDelegate, NSToolbarDelegate {
    enum Mode: Int { case mac = 0, indexed = 1, ask = 2 }
    enum ViewStyle: Int { case icons = 0, list = 1, columns = 2 }

    private enum Item {
        static let nav = NSToolbarItem.Identifier("MySearch.nav")
        static let mode = NSToolbarItem.Identifier("MySearch.mode")
        static let view = NSToolbarItem.Identifier("MySearch.view")
        static let split = NSToolbarItem.Identifier("MySearch.split")
        static let search = NSToolbarItem.Identifier("MySearch.search")
        static let classic = NSToolbarItem.Identifier("MySearch.classic")
        static let openIn = NSToolbarItem.Identifier("MySearch.openIn")
    }

    let window: NSWindow
    let sidebar = SidebarViewController()
    let content = ContentContainerViewController()
    let provider: ContentProvider = FileSystemProvider()
    /// The web UI (the Ask view). It stays loaded while another mode is showing, so calls into it always land.
    let webPane = WebPane(messages: ["app", "layout"])

    private let split = NSSplitViewController()
    private let placeholder = PlaceholderViewController()
    private lazy var webHost = ViewHostViewController(webPane.web)
    /// The native Ask view (AskViewController.swift). The web page above is still used for the Indexed Search preview.
    lazy var ask = AskViewController(snapshot: snapshot)
    let snapshot: Bool

    /// The folder panes, left first (SplitPanes.swift). Each has its own navigator, view style and folder views.
    private(set) var panes: [FolderPane] = []
    /// The two panes side by side; the right one is collapsed while the window has a single pane.
    private(set) var panesController: FolderPanesController!
    /// The focused pane's index. It drives the title, the toolbar's back and forward, the view switcher and the status.
    var focusedIndex = 0
    var paneMouseMonitor: Any?

    private(set) var mode: Mode = .mac
    /// In Indexed mode: the collection or group the content shows.
    private(set) var indexedScope: SidebarNode.Kind?
    private(set) var library: IndexedLibrary?
    /// The Indexed folders picked in the sidebar (collection names; empty = every indexed folder).
    private(set) var indexedSelection: [String] = []
    /// The native search view (SearchView.swift), shown while the search field has a query or in Indexed mode.
    let search = SearchViewController()
    private(set) var searchShown = false
    /// Indexed mode showing the web UI's Search view (rich preview, favourites) instead of the native results.
    private(set) var webSearchShown = false
    private weak var searchField: NSSearchField?

    private var busy = 0                 // loads in flight (folder listing, sidebar)
    private var keyMonitor: Any?         // ⌘⌥F and ⌃1–⌃3 while the search results are showing

    private weak var backForward: NSSegmentedControl?
    private weak var modeControl: NSSegmentedControl?
    private weak var viewControl: NSSegmentedControl?
    private weak var searchToolbarItem: NSSearchToolbarItem?

    /// True while anything the window shows is still loading (snapshot mode waits on this).
    var isBusy: Bool { busy > 0 || (webSearchShown && webPane.web.isLoading) || (mode == .ask && ask.isBusy) || (searchShown && search.isLoading)
        || (!searchShown && mode == .mac && viewStyle == .icons && iconView.isLoadingThumbnails)
        || InfoPanelController.shared.isCalculating }

    /// `viewStyle`: the Mac view it opens in. `primary`: the app's first main window, which keeps the saved frame;
    /// the others (tabs and new windows, see WindowTabs.swift) are placed beside the key window.
    init(frame: NSRect, snapshot: Bool, startFolder: URL, viewStyle: ViewStyle = .list, primary: Bool = true) {
        self.snapshot = snapshot
        let style: NSWindow.StyleMask = [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView]
        window = snapshot ? SnapshotWindow(contentRect: frame, styleMask: style, backing: .buffered, defer: false)
                          : MainTabWindow(contentRect: frame, styleMask: style, backing: .buffered, defer: false)
        super.init()
        panes = [FolderPane(index: 0, start: startFolder, viewStyle: viewStyle, provider: provider, snapshot: snapshot),
                 FolderPane(index: 1, start: startFolder, viewStyle: viewStyle, provider: provider, snapshot: snapshot)]
        for pane in panes { pane.owner = self }
        panesController = FolderPanesController(left: panes[0].container, right: panes[1].container)
        configureWindow(frame: frame, primary: primary)
        NotificationCenter.default.addObserver(self, selector: #selector(navigatorChanged(_:)),
                                               name: .navigatorDidChange, object: nil)
        sidebar.onSelect = { [weak self] kind in self?.sidebarSelected(kind) }
        sidebar.onLoading = { [weak self] on in self?.adjustBusy(on ? 1 : -1) }
        sidebar.onLibrary = { [weak self] lib in self?.libraryArrived(lib) }
        content.onPathClick = { [weak self] url in self?.openFolder(url) }
        sidebar.onIndexedSelection = { [weak self] names in self?.indexedSelectionChanged(names) }
        search.onStatus = { [weak self] text in
            self?.content.setStatus(text)
            self?.refreshSubtitle()                 // the count in the subtitle follows the results
        }
        search.onOpenFolder = { [weak self] url in self?.openFolder(url) }
        search.onRevealFolder = { [weak self] url in self?.revealInFolderView(url) }
        search.onRequestQuery = { [weak self] text in self?.setSearchText(text) }
        search.onPick = { [weak self] names in self?.pickIndexed(names) }
        search.onEscape = { [weak self] in self?.clearSearchFromKeyboard() }
        search.onOpenPreview = { [weak self] q, names, coll, rel in self?.openContentPreview(query: q, scope: names, collection: coll, path: rel) }
        search.onScopeChosen = { [weak self] choice, names, text in self?.applyScopeChoice(choice, names: names, query: text) }
        search.recordsRecents = !snapshot
        NotificationCenter.default.addObserver(self, selector: #selector(recentsChanged(_:)), name: RecentSearches.didChange, object: nil)
        installKeyMonitor()
        installPaneMonitors()
    }

    /// The window's folder state, read from the focused pane: its navigator and its view style.
    var navigator: Navigator { focusedPane.navigator }
    var viewStyle: ViewStyle { focusedPane.viewStyle }

    /// The folder views of the focused pane.
    var listView: ListViewController { focusedPane.listView }
    var iconView: IconViewController { focusedPane.iconView }
    var columnView: ColumnViewController { focusedPane.columnView }

    /// Mac folder mode, with no search on top: the folder panes are the content.
    var showingFolders: Bool { mode == .mac && !searchShown && !tagListShowing }

    private func configureWindow(frame: NSRect, primary: Bool) {
        window.isReleasedWhenClosed = false
        // Native tabs: every main window joins the "MySearchMain" group (the snapshot window never does).
        window.tabbingMode = snapshot ? .disallowed : .preferred
        window.tabbingIdentifier = "MySearchMain"
        window.isRestorable = false
        window.minSize = NSSize(width: 720, height: 480)
        window.delegate = self
        window.titleVisibility = .visible
        window.toolbarStyle = .unified

        let sideItem = NSSplitViewItem(sidebarWithViewController: sidebar)
        sideItem.minimumThickness = 210
        sideItem.maximumThickness = 320
        let contentItem = NSSplitViewItem(viewController: content)
        contentItem.minimumThickness = 420
        split.addSplitViewItem(sideItem)
        split.addSplitViewItem(contentItem)
        if !snapshot { split.splitView.autosaveName = "MySearchSplit" }
        window.contentViewController = split
        if snapshot { keepVibrancyActive(window.contentView) }
        window.setFrame(frame, display: false)       // contentViewController resizes the window to its content
        if !primary && !snapshot, let key = NSApp.keyWindow, key !== window {
            window.setFrameTopLeftPoint(NSPoint(x: key.frame.minX + 28, y: key.frame.maxY - 28))
        }

        let toolbar = NSToolbar(identifier: "MySearchMain")
        toolbar.delegate = self
        toolbar.displayMode = .iconOnly
        toolbar.allowsUserCustomization = false
        window.toolbar = toolbar
        if !snapshot && primary {
            window.setFrameAutosaveName("MySearchMain")      // restores the saved frame, if there is one
            if window.frame.origin == .zero { window.center() }
        }
    }

    /// An off-screen snapshot window is never key, so its vibrant sidebar would fade as if disabled; keep it active.
    private func keepVibrancyActive(_ view: NSView?) {
        guard let view else { return }
        if let effect = view as? NSVisualEffectView { effect.state = .active }
        for sub in view.subviews { keepVibrancyActive(sub) }
    }

    /// Brings the window forward.
    func show() {
        window.makeKeyAndOrderFront(nil)
    }

    /// Starts the window's content: the web UI loads in the background and the first mode is shown.
    func start(initial: Mode = .mac) {
        webPane.load(MSEClient.shared.pageURL())
        sidebar.reloadIndexed()
        setMode(initial)
    }

    // MARK: navigation

    func goBack() { navigator.back() }
    func goForward() { navigator.forward() }
    func goEnclosingFolder() { navigator.enclosingFolder() }
    func goHome() { openFolder(FileManager.default.homeDirectoryForCurrentUser) }

    /// Opens a folder in Mac mode and records it in the history.
    func openFolder(_ url: URL) {
        clearSearchText()                    // leaving a search: the folder view comes back
        navigator.go(to: url)
        if mode != .mac || tagListShowing { setMode(.mac) }     // a tag list or another mode gives way to the folder
    }

    /// View > Show Hidden Files, for the command palette: every pane's folder views show or hide dot-files together
    /// (the ⌘⇧. key in the icon and column views does the same for one view).
    func toggleHiddenFiles() {
        let shown: Bool      // the state of the view that is showing, whichever style it is
        switch viewStyle {
        case .icons: shown = focusedPane.iconView.showHidden
        case .list: shown = focusedPane.listView.showHidden
        case .columns: shown = focusedPane.columnView.showHidden
        }
        let on = !shown
        for pane in panes {
            pane.listView.setShowHidden(on)
            pane.iconView.setShowHidden(on)
            pane.columnView.setShowHidden(on)
        }
    }

    /// Filter This Folder, for the command palette: opens the filter of the focused pane's folder view.
    func filterThisFolder() {
        guard mode == .mac, !searchShown, !tagListShowing else { return }
        switch viewStyle {
        case .icons: focusedPane.iconView.openFilter()
        case .list: focusedPane.listView.openFilter()
        case .columns: focusedPane.columnView.openFilter()
        }
    }

    /// The sidebar's Tags list takes this window's content: any search leaves the state, so the search keys, the title
    /// and the pane keys stop acting on the views that are now hidden.
    func tagListWillShow() {
        searchShown = false
        webSearchShown = false
    }

    /// True while the sidebar's tag list (TagResultsController) is this window's content.
    var tagListShowing: Bool { TagResultsController.shared.isShowing(in: window) }

    @objc private func navigatorChanged(_ n: Notification) {
        guard let changed = n.object as? Navigator, let pane = panes.first(where: { $0.navigator === changed }) else { return }
        clearSearchText()
        if pane === focusedPane {
            sidebar.highlight(folder: navigator.current)
            if mode == .mac { showFolder() } else { setMode(.mac) }
        } else if mode == .mac {
            showFolder()                       // the other pane moved: its folder shows, the focus stays where it was
        }
    }

    private func sidebarSelected(_ kind: SidebarNode.Kind) {
        switch kind {
        case .place(_, let url, _):
            openFolder(url)
        case .collection, .group:
            indexedScope = kind
            setMode(.indexed)
        case .section, .note:
            break
        }
    }

    private func libraryArrived(_ lib: IndexedLibrary) {
        library = lib
        search.setLibrary(lib)              // the Indexed picker lists the collections and groups
    }

    /// The sidebar's Indexed selection changed: the scope follows it. Outside Indexed mode, picking a folder opens it.
    private func indexedSelectionChanged(_ names: [String]) {
        indexedSelection = names
        if mode == .indexed {
            showSearch()
        } else if !names.isEmpty {
            setMode(.indexed)
        }
    }

    /// Picks Indexed folders from the search view's picker (and mirrors the choice in the sidebar).
    private func pickIndexed(_ names: [String]) {
        sidebar.selectIndexed(collections: names)
        indexedSelectionChanged(names)
    }

    /// The scope button in the search area chose where to search (Whole Mac, All indexed, a group or folders). The mode
    /// and the sidebar follow, and the words in the search field stay: they are searched in the new scope. A scope
    /// shortcut typed in the field would override the choice, so the search view hands over the text without it.
    private func applyScopeChoice(_ choice: ScopeChoice, names: [String], query newText: String?) {
        if let t = newText { searchField?.stringValue = t }
        switch choice {
        case .wholeMac:
            indexedSelection = []
            sidebar.selectIndexed(names: [])
            sidebar.highlight(folder: navigator.current)
            setMode(.mac)
        default:
            indexedSelection = names
            sidebar.selectIndexed(names: names)
            setMode(.indexed)
        }
    }

    // MARK: search keys

    /// ⌘⌥F opens the scope picker; ⌃1, ⌃2, ⌃3 jump to the file names, the content matches and the filters. A local
    /// monitor sees the keys before the menu does. The jumps use ⌃ because ⌘1–⌘3 are View > Icons, List, Columns.
    private func installKeyMonitor() {
        guard !snapshot, keyMonitor == nil else { return }
        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let self else { return event }
            let handled = MainActor.assumeIsolated { self.searchShortcut(event) || self.paneShortcut(event) }
            return handled ? nil : event
        }
    }

    private func searchShortcut(_ e: NSEvent) -> Bool {
        guard searchShown, !webSearchShown, mode != .ask else { return false }
        guard NSApp.keyWindow === window || search.isScopePickerShown else { return false }
        let flags = e.modifierFlags.intersection([.command, .option, .control, .shift])
        if flags == [.command, .option], e.keyCode == 3 {          // F
            search.toggleScopePicker()
            return true
        }
        if flags == [.control], !search.isScopePickerShown {        // ⌃1–⌃3: ⌘1–⌘3 are View > Icons, List, Columns
            switch e.keyCode {
            case 18: search.jumpToNames(); return true              // ⌃1
            case 19: search.jumpToContents(); return true           // ⌃2
            case 20: search.jumpToFilters(); return true            // ⌃3
            default: break
            }
        }
        return false
    }

    // MARK: recent searches

    /// The search field's magnifier menu: the searches acted on lately, newest first, and Clear.
    private func refreshRecentsMenu() {
        guard let cell = searchField?.cell as? NSSearchFieldCell else { return }
        let menu = NSMenu(title: "Recent Searches")
        let recents = RecentSearches.all
        let head = NSMenuItem(title: recents.isEmpty ? "No Recent Searches" : "Recent Searches", action: nil, keyEquivalent: "")
        head.isEnabled = false
        menu.addItem(head)
        for q in recents {
            let item = NSMenuItem(title: q, action: #selector(recentPicked(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = q
            menu.addItem(item)
        }
        if !recents.isEmpty {
            menu.addItem(.separator())
            let clear = NSMenuItem(title: "Clear Recent Searches", action: #selector(clearRecents(_:)), keyEquivalent: "")
            clear.target = self
            menu.addItem(clear)
        }
        cell.searchMenuTemplate = menu
    }

    @objc private func recentsChanged(_ n: Notification) { refreshRecentsMenu() }

    @objc private func recentPicked(_ sender: NSMenuItem) {
        guard let q = sender.representedObject as? String else { return }
        setSearchText(q)
    }

    @objc private func clearRecents(_ sender: Any?) { RecentSearches.clear() }

    // MARK: modes

    func setMode(_ m: Mode) {
        mode = m
        searchShown = false
        webSearchShown = false
        modeControl?.selectedSegment = m.rawValue
        switch m {
        case .mac: if searchQuery.isEmpty { showFolder() } else { showSearch() }
        case .indexed: showIndexed()
        case .ask: showAsk()
        }
        updateChrome()
    }

    /// Switches the Mac-mode view. The folder stays, and the selected item stays selected in the new view.
    func setViewStyle(_ style: ViewStyle) {
        let carried = selectedURL
        focusedPane.viewStyle = style
        viewControl?.selectedSegment = style.rawValue
        guard mode == .mac && !searchShown && !tagListShowing else { return }
        showFolder()
        if let carried { revealInCurrentView(carried) }
    }

    /// The Mac-mode view that is showing: the icon grid, the list or the columns.
    private var showingColumns: Bool { viewStyle == .columns }

    /// The item selected in the focused pane's view, if any.
    private var selectedURL: URL? { focusedPane.selectedItems.first?.url }

    private func revealInCurrentView(_ url: URL) {
        focusedPane.reveal(url)
    }

    /// The URL that ⌘↵ in the search view should show in the folder view, once the folder is on screen.
    private var pendingReveal: URL?

    /// ⌘↵ in the search view: leaves the search, opens the item's folder in the current folder view and selects it there.
    func revealInFolderView(_ item: URL) {
        let parent = item.standardizedFileURL.deletingLastPathComponent()
        pendingReveal = item.standardizedFileURL
        clearSearchText()
        if parent.standardizedFileURL == navigator.current {
            if mode == .mac { showFolder() } else { setMode(.mac) }
        } else {
            openFolder(parent)
        }
    }

    /// Snapshot hook: after the search results have landed, does what ⌘↵ does for `url`.
    func snapshotReveal(_ url: URL) {
        busy += 1
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 2_500_000_000)
            revealInFolderView(url)
            try? await Task.sleep(nanoseconds: 300_000_000)
            busy -= 1
        }
    }

    /// ⌘I: Get Info for the selection of whatever view is showing.
    func getInfo() {
        if tagListShowing {
            let tagged = TagResultsController.shared.selectedItems
            if !tagged.isEmpty { InfoPanelController.shared.show(tagged) }
            return
        }
        let items: [FileItem]
        if searchShown { items = search.selectedFileURLs.map { FileItem(url: $0) } }
        else {
            switch viewStyle {
            case .columns: items = columnView.selectedItems
            case .icons: items = iconView.selectedItems
            case .list: items = listView.selectedItems
            }
        }
        if items.isEmpty, mode == .mac, !searchShown { InfoPanelController.shared.show([FileItem(url: navigator.current)]) }
        else { InfoPanelController.shared.show(items) }
    }

    /// Space (from the menu): Quick Look for the current selection.
    func quickLookSelection() {
        let urls: [URL]
        if tagListShowing { urls = TagResultsController.shared.selectedItems.map { $0.url } }
        else { urls = searchShown ? search.selectedFileURLs : [selectedURL].compactMap { $0 } }
        QuickLookCoordinator.shared.toggle(urls)
    }

    /// Mac mode: the folder panes. The focused pane reloads its folder (its view keeps its state); the other pane
    /// reloads only when its folder or view style changed. Each pane's folder views live in SplitPanes.swift.
    private func showFolder() {
        content.collapsesEmptyFooter = true    // the panes have their own footers: the outer one gives up its space
        content.show(panesController)          // one pane, or two side by side (the second is collapsed when one)
        content.setPath(nil)
        content.setStatus("")
        for pane in panes where pane === focusedPane || isSplitPane { pane.show(force: pane === focusedPane) }
        if let target = pendingReveal {
            pendingReveal = nil
            revealInCurrentView(target)
        }
        refreshFolderTitle()
        updateChrome()
    }

    /// Indexed mode: the search view, which with an empty query lists the indexed folders and groups to pick.
    /// The scope is the sidebar's selection (every indexed folder when nothing is selected).
    private func showIndexed() {
        showSearch()            // a search text stays: switching to Indexed searches the same words there
    }

    /// Mac or Indexed search: the native results view in the content area (its scope from the mode and selection).
    private func showSearch() {
        searchShown = true
        content.collapsesEmptyFooter = false
        content.show(search)
        content.setPath(nil)
        // The window title is the scope, short and fixed ("Mac", "Indexed", "Ask", "Preview": 15 characters at most). It never
        // carries the query or the collection names; those are the subtitle (refreshSubtitle).
        if mode == .indexed {
            search.configure(scope: .indexed, names: indexedSelection)
            window.title = "Indexed"
        } else {
            search.configure(scope: .mac, names: [])
            window.title = "Mac"
        }
        refreshSubtitle()
        search.run(searchQuery)
    }

    /// The toolbar search field's text.
    var searchQuery: String { searchField?.stringValue ?? "" }

    /// Sets the toolbar search field and runs it, as if typed.
    private func setSearchText(_ text: String) {
        searchField?.stringValue = text
        searchFieldChanged(searchField)
    }

    /// Empties the search field without changing the view; the caller shows what comes next.
    private func clearSearchText() {
        guard searchShown || !searchQuery.isEmpty else { return }
        searchField?.stringValue = ""
        searchShown = false
        search.run("")
    }

    /// Esc: clears the search and returns to the folder view (Mac) or the Indexed picker.
    private func clearSearchFromKeyboard() {
        searchField?.stringValue = ""
        if mode == .mac {
            searchShown = false
            showFolder()
        } else {
            search.run("")
        }
    }

    /// Every keystroke in the toolbar field lands here: a query shows the search view, an empty one leaves it.
    @objc private func searchFieldChanged(_ sender: NSSearchField?) {
        defer { refreshSubtitle() }             // the subtitle follows the typing
        let text = sender?.stringValue ?? ""
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if mode == .ask && !trimmed.isEmpty { setMode(.mac); return }
        if webSearchShown && !trimmed.isEmpty { webSearchShown = false; showSearch(); return }
        if mode == .mac && trimmed.isEmpty {
            if searchShown {
                searchShown = false
                search.run("")
                showFolder()
            }
            return
        }
        if searchShown { search.run(text) } else { showSearch() }
    }

    /// Snapshot and test hook: optional Indexed picks (the first N collections), then a query, as if typed.
    func prepareSearch(query: String, indexedPicks: Int) {
        busy += 1
        Task { @MainActor in
            defer { busy -= 1 }
            if indexedPicks > 0 {
                for _ in 0..<200 where library == nil {               // up to 10 s for the sidebar
                    try? await Task.sleep(nanoseconds: 50_000_000)
                }
                let names = (library?.collections ?? []).prefix(indexedPicks).map { $0.name }
                pickIndexed(names)
            }
            setSearchText(query)
        }
    }

    /// Ask mode: the web UI, in the content area.
    private func showAsk() {
        content.collapsesEmptyFooter = false
        content.show(ask)
        content.setPath(nil)
        content.setStatus("")
        window.title = "Ask"
        refreshSubtitle()
        ask.activate()
    }

    /// Snapshot hook: the native Ask view in one of its states (ask-empty, ask-streaming, ask-done, ask-picker, ask-off ...).
    func snapshotAsk(_ name: String) { ask.snapshotState(name) }

    /// Indexed mode with the web UI's Search view in the content area (the rich preview with ★ Favourite, Not
    /// relevant, Find similar, Copy for AI). Pick Indexed in the mode switch to go back to the native results.
    private func showWebSearch() {
        mode = .indexed
        searchShown = false
        webSearchShown = true
        modeControl?.selectedSegment = Mode.indexed.rawValue
        content.collapsesEmptyFooter = false
        content.show(webHost)
        content.setPath(nil)
        content.setStatus("")
        window.title = "Preview"
        updateChrome()
    }

    /// Opens a search from a mysearch:// link in the web UI (its openLink function): the Search view with that
    /// query, folders and document at its page.
    func openWebSearch(query: String) {
        showWebSearch()
        webPane.run("window.openLink && openLink(\(js(query)))")
    }

    /// Opens an item sent from the Quick Search panel in the web UI (openFromMini): a search, or an Ask chat.
    func openFromQuickSearch(_ o: [String: Any]) {
        if (o["ask"] as? Bool) == true || (o["ask"] as? NSNumber)?.boolValue == true {
            setMode(.ask)                                   // the native Ask view asks it in a new chat
            ask.ask(question: (o["q"] as? String) ?? "")
            return
        }
        showWebSearch()
        webPane.run("window.openFromMini && openFromMini(\(js(o)))")
    }

    /// A content match (or an indexed file) of the native Indexed results: the web Search view for the same query,
    /// with that file selected and previewed.
    func openContentPreview(query: String, scope: [String], collection: String, path: String) {
        showWebSearch()
        let o: [String: Any] = ["q": query, "scope": scope.isEmpty ? "all" : scope.joined(separator: ","),
                                "collection": collection, "path": path]
        webPane.run("window.openFromMini && openFromMini(\(js(o)))")
    }

    /// Snapshot hook: the preview of one indexed file, as after Return on its content match.
    func snapshotPreview(query: String, collection: String, path: String) {
        busy += 1
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            openContentPreview(query: query, scope: [collection], collection: collection, path: path)
            try? await Task.sleep(nanoseconds: 2_500_000_000)
            busy -= 1
        }
    }

    /// Reloads the current view and the sidebar's collections (⌘R).
    func reload() {
        sidebar.reloadIndexed()
        if mode == .ask { ask.reload() } else if webSearchShown { webPane.load(MSEClient.shared.pageURL()) } else { setMode(mode) }
    }

    /// ⌘K: moves the keyboard to the toolbar's search field.
    func focusSearch() {
        window.makeFirstResponder(searchToolbarItem?.searchField)
    }

    func adjustBusy(_ delta: Int) {
        busy += delta
    }


    /// Brings the toolbar, the title and the pane outlines in line with the focused pane and the mode, then saves the tabs.
    func updateChrome() {
        backForward?.setEnabled(navigator.canGoBack, forSegment: 0)
        backForward?.setEnabled(navigator.canGoForward, forSegment: 1)
        viewControl?.selectedSegment = viewStyle.rawValue
        modeControl?.selectedSegment = tagListShowing ? -1 : mode.rawValue     // no mode is selected while Tags shows
        refreshSubtitle()
        refreshPaneOutlines()
        if !snapshot { NotificationCenter.default.post(name: .mainTabsChanged, object: self) }   // the open tabs are saved
    }

    /// The window subtitle (NSWindow.subtitle) is the context of the scope in the title: what the search found and WHERE it looks,
    /// never the query (the search field has that). "12 matches · work + notes" in Indexed mode, "1,234 results · on this Mac"
    /// in Mac mode, "No matches · <place>" when nothing matched; while a search is still on its way, or its count is not known,
    /// the place alone. The place is, for Mac, "on this Mac"; for Indexed, the folders picked in the sidebar, "work + notes"
    /// (three at most, then "+ 2 more"; a picked group counts as its members), or "all indexed folders" when none is picked.
    /// It shows only while the native results show with a search text. Ask, folders, tags, the Indexed picker (no text, nothing
    /// searched yet) and the Preview page have none: the Preview page searches the scope its own link or Quick Search sent,
    /// which can differ from the sidebar selection, so a place there could be false. It is recomputed from state each time,
    /// so it is never left over from the last view, and it follows the search view's status (search.onStatus), which is
    /// called each time the results or their count change.
    private func refreshSubtitle() {
        let q = searchQuery.trimmingCharacters(in: .whitespacesAndNewlines)
        guard searchShown && !q.isEmpty else { window.subtitle = ""; return }
        let indexed = mode == .indexed
        let place = indexed ? Self.indexedPlace(indexedSelection) : "on this Mac"
        window.subtitle = Self.countText(search.resultCount, indexed: indexed).map { $0 + " · " + place } ?? place
    }

    private static let subtitleCounts: NumberFormatter = { let f = NumberFormatter(); f.numberStyle = .decimal; return f }()

    /// "12 matches" (Indexed), "1 result" (Mac), "No matches" for zero; nil when the count is not known.
    private static func countText(_ count: Int?, indexed: Bool) -> String? {
        guard let count else { return nil }
        if count == 0 { return "No matches" }
        let n = subtitleCounts.string(from: NSNumber(value: count)) ?? "\(count)"
        return n + " " + (indexed ? (count == 1 ? "match" : "matches") : (count == 1 ? "result" : "results"))
    }

    /// The Indexed place for the subtitle: the picked folders joined with " + ", at most three.
    private static func indexedPlace(_ names: [String]) -> String {
        let named = names.filter { !$0.isEmpty }
        guard !named.isEmpty else { return "all indexed folders" }
        let shown = named.prefix(3).joined(separator: " + ")
        return named.count > 3 ? "\(shown) + \(named.count - 3) more" : shown
    }

    // MARK: launchers (Launchers.swift)

    /// What the "Open in…" menus act on: the selection of the view that shows, else the current folder.
    var launcherTargets: [URL] {
        if tagListShowing {
            let tagged = TagResultsController.shared.selectedItems.map { $0.url }
            return tagged.isEmpty ? [navigator.current] : tagged
        }
        let picked: [URL] = searchShown ? search.selectedFileURLs : [selectedURL].compactMap { $0 }
        return picked.isEmpty ? [navigator.current] : picked
    }

    /// Searches in a folder (mysearch://search?path=…): Indexed mode scoped to its collection when it is in one,
    /// otherwise Mac mode with `in:"<folder>"`.
    func searchHere(folder: URL) {
        if let c = LauncherCollections.containing(folder) {
            pickIndexed([c.name])
        } else if !folder.path.contains("\"") {
            setMode(.mac)
            setSearchText("in:\"\(folder.path)\" ")
        } else {
            setMode(.mac)
        }
        focusSearch()
        if let editor = window.firstResponder as? NSText { editor.selectedRange = NSRange(location: editor.string.utf16.count, length: 0) }
    }

    // MARK: toolbar

    func toolbarAllowedItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] {
        [.toggleSidebar, Item.nav, .flexibleSpace, .sidebarTrackingSeparator, Item.mode, Item.view, Item.split, Item.openIn, Item.search, Item.classic]
    }

    func toolbarDefaultItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] {
        [.toggleSidebar, Item.nav, .sidebarTrackingSeparator, .flexibleSpace, Item.mode, Item.view, Item.split, Item.openIn, Item.search, Item.classic]
    }

    func toolbar(_ toolbar: NSToolbar, itemForItemIdentifier itemIdentifier: NSToolbarItem.Identifier,
                 willBeInsertedIntoToolbar flag: Bool) -> NSToolbarItem? {
        switch itemIdentifier {
        case Item.nav:
            let seg = NSSegmentedControl(images: [symbol("chevron.backward", "Back"), symbol("chevron.forward", "Forward")],
                                         trackingMode: .momentary, target: self, action: #selector(backForwardClicked(_:)))
            seg.segmentStyle = .separated
            backForward = seg
            let item = NSToolbarItem(itemIdentifier: itemIdentifier)
            item.label = "Back and Forward"
            item.paletteLabel = "Back and Forward"
            item.isNavigational = true          // Finder's back and forward: kept beside the title, never in the overflow
            item.visibilityPriority = .high
            item.view = seg
            return item
        case Item.mode:
            let seg = NSSegmentedControl(labels: ["Mac", "Indexed", "Ask"], trackingMode: .selectOne,
                                         target: self, action: #selector(modeClicked(_:)))
            seg.selectedSegment = mode.rawValue
            modeControl = seg
            let item = NSToolbarItem(itemIdentifier: itemIdentifier)
            item.label = "Mode"
            item.paletteLabel = "Mode"
            item.toolTip = "Mac files, the Indexed collections, or Ask"
            item.view = seg
            return item
        case Item.view:
            let seg = NSSegmentedControl(images: [symbol("square.grid.2x2", "Icons"), symbol("list.bullet", "List"),
                                                  symbol("rectangle.split.3x1", "Columns")],
                                         trackingMode: .selectOne, target: self, action: #selector(viewClicked(_:)))
            seg.selectedSegment = viewStyle.rawValue
            for (i, label) in ["Icons", "List", "Columns"].enumerated() { seg.setToolTip(label, forSegment: i) }
            viewControl = seg
            let item = NSToolbarItem(itemIdentifier: itemIdentifier)
            item.label = "View"
            item.paletteLabel = "View"
            item.view = seg
            return item
        case Item.split:
            // Split Pane (SplitPanes.swift): one folder pane, or two side by side.
            let item = NSToolbarItem(itemIdentifier: itemIdentifier)
            item.label = "Split Pane"
            item.paletteLabel = "Split Pane"
            item.toolTip = "Split the window into two folder panes (⌘⌥\\)"
            item.image = symbol("rectangle.split.2x1", "Split Pane")
            item.target = self
            item.action = #selector(toggleSplitPaneAction(_:))
            return item
        case Item.search:
            let item = NSSearchToolbarItem(itemIdentifier: itemIdentifier)
            item.searchField.placeholderString = "Search"
            item.preferredWidthForSearchField = 240
            item.searchField.sendsSearchStringImmediately = true     // one search per keystroke (debounced in the view)
            item.searchField.target = self
            item.searchField.action = #selector(searchFieldChanged(_:))
            item.searchField.delegate = self                          // ↓ moves into the results
            searchField = item.searchField
            searchToolbarItem = item
            refreshRecentsMenu()
            return item
        case Item.openIn:
            // "Open in…": a menu of the installed tools (Launchers.swift) acting on the selection or the current folder.
            let item = NSMenuToolbarItem(itemIdentifier: itemIdentifier)
            item.label = "Open in…"
            item.paletteLabel = "Open in…"
            item.toolTip = "Open the selected or current folder in Terminal, VS Code, Cursor, Claude Code or Codex"
            item.image = symbol("arrow.up.forward.app", "Open in…")
            item.menu = Launchers.makeDynamicMenu()
            item.showsIndicator = true
            item.visibilityPriority = .low
            return item
        case Item.classic:
            guard ClassicUI.isEnabled else { return nil }      // off: no Classic item (ClassicUI.swift; AppDelegate keeps open windows in step)
            // Only the overflow menu matters here: the item is hidden first when the toolbar is narrow, and its menu
            // form is the same "Open Classic MySearch" entry. The action goes up the responder chain to AppDelegate.
            let item = NSToolbarItem(itemIdentifier: itemIdentifier)
            item.label = "Classic"
            item.paletteLabel = "Open Classic MySearch"
            item.toolTip = "Open Classic MySearch (the old web UI)"
            item.image = symbol("rectangle.on.rectangle", "Open Classic MySearch")
            item.action = #selector(AppDelegate.openClassicWindow(_:))
            item.visibilityPriority = .low
            let menuForm = NSMenuItem(title: "Open Classic MySearch", action: #selector(AppDelegate.openClassicWindow(_:)), keyEquivalent: "")
            item.menuFormRepresentation = menuForm
            return item
        default:
            return nil          // the system items (toggle sidebar, flexible space, tracking separator)
        }
    }

    private func symbol(_ name: String, _ description: String) -> NSImage {
        NSImage(systemSymbolName: name, accessibilityDescription: description) ?? NSImage()
    }

    @objc private func backForwardClicked(_ sender: NSSegmentedControl) {
        if sender.selectedSegment == 0 { goBack() } else { goForward() }
    }

    @objc private func modeClicked(_ sender: NSSegmentedControl) {
        if let m = Mode(rawValue: sender.selectedSegment) { setMode(m) }
    }

    @objc private func viewClicked(_ sender: NSSegmentedControl) {
        if let v = ViewStyle(rawValue: sender.selectedSegment) { setViewStyle(v) }
    }
}

extension MainWindowController: NSSearchFieldDelegate {
    /// ↓ in the search field moves the keyboard into the results (the first row is already selected).
    func control(_ control: NSControl, textView: NSTextView, doCommandBy commandSelector: Selector) -> Bool {
        if commandSelector == #selector(NSResponder.moveDown(_:)), searchShown, !webSearchShown {
            search.focusTable()
            return true
        }
        return false
    }
}
