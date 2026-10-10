// Dual panes, Shuffle-style: the folder area of a main window can split into two folder panes side by side. Each pane
// has its own Navigator (history, back and forward), its own view style (icons, list or columns), its own folder views,
// and its own path bar and status line. The focused pane drives the window title, the toolbar's back and forward, the
// view switcher and the sidebar highlight. Search, Ask and Indexed stay single-pane.
//
// The split is an NSSplitViewController whose second item is collapsed while the window has one pane, so the folder
// area always shows the same controller and toggling only changes the collapse. F5 and F6 (and the "Copy to / Move to
// Other Pane" context-menu items) copy or move the selection to the other pane's folder through FileOpsService.
// Dragging between panes already works: each folder view accepts drops into its own folder.

import Cocoa

/// The accent outline of the focused pane. It only draws: it never takes a click, a drag or a key.
final class PaneOutlineView: NSView {
    var isOn = false { didSet { apply() } }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        apply()
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override func hitTest(_ point: NSPoint) -> NSView? { nil }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        apply()
    }

    private func apply() {
        layer?.borderWidth = isOn ? 2 : 0
        layer?.borderColor = NSColor.controlAccentColor.withAlphaComponent(0.6).cgColor
    }
}

/// One folder pane. Its folder views are created on first use and call back into the owning window with the pane's index.
@MainActor
final class FolderPane {
    let index: Int
    let navigator: Navigator
    var viewStyle: MainWindowController.ViewStyle
    /// The pane's content area: the folder view, with the pane's own path bar and status line under it.
    let container = ContentContainerViewController()
    /// The window that owns the pane. MainWindowController sets it right after creating the pane.
    weak var owner: MainWindowController?

    private let provider: ContentProvider
    private let snapshot: Bool
    private var shownFolder: URL?
    private var shownStyle: MainWindowController.ViewStyle?
    private lazy var outline: PaneOutlineView = {
        let view = PaneOutlineView()
        view.translatesAutoresizingMaskIntoConstraints = false
        container.view.addSubview(view)
        NSLayoutConstraint.activate([
            view.topAnchor.constraint(equalTo: container.view.topAnchor),
            view.bottomAnchor.constraint(equalTo: container.view.bottomAnchor),
            view.leadingAnchor.constraint(equalTo: container.view.leadingAnchor),
            view.trailingAnchor.constraint(equalTo: container.view.trailingAnchor),
        ])
        return view
    }()

    init(index: Int, start: URL, viewStyle: MainWindowController.ViewStyle, provider: ContentProvider, snapshot: Bool) {
        self.index = index
        self.navigator = Navigator(start: start)
        self.viewStyle = viewStyle
        self.provider = provider
        self.snapshot = snapshot
        container.onPathClick = { [weak self] url in
            guard let self else { return }
            self.owner?.paneNavigate(self.index, url)
        }
    }

    private(set) lazy var listView: ListViewController = {
        let view = ListViewController(provider: provider, snapshot: snapshot)
        view.onNavigate = { [weak self] url in if let self { self.owner?.paneNavigate(self.index, url) } }
        view.onEnclosingFolder = { [weak self] in if let self { self.owner?.paneEnclosingFolder(self.index) } }
        view.onStatus = { [weak self] text in if let self { self.owner?.paneStatus(self.index, text) } }
        view.onBusy = { [weak self] on in if let self { self.owner?.paneBusy(on) } }
        return view
    }()

    private(set) lazy var iconView: IconViewController = {
        let view = IconViewController(provider: provider, snapshot: snapshot)
        view.onNavigate = { [weak self] url in if let self { self.owner?.paneNavigate(self.index, url) } }
        view.onEnclosingFolder = { [weak self] in if let self { self.owner?.paneEnclosingFolder(self.index) } }
        view.onStatus = { [weak self] text in if let self { self.owner?.paneStatus(self.index, text) } }
        view.onBusy = { [weak self] on in if let self { self.owner?.paneBusy(on) } }
        return view
    }()

    private(set) lazy var columnView: ColumnViewController = {
        let view = ColumnViewController(provider: provider, snapshot: snapshot)
        view.onNavigate = { [weak self] url in if let self { self.owner?.paneNavigate(self.index, url) } }
        view.onEnclosingFolder = { [weak self] in if let self { self.owner?.paneEnclosingFolder(self.index) } }
        view.onFocusFolder = { [weak self] url in if let self { self.owner?.paneFocusedFolder(self.index, url) } }
        view.onStatus = { [weak self] text in if let self { self.owner?.paneStatus(self.index, text) } }
        view.onBusy = { [weak self] on in if let self { self.owner?.paneBusy(on) } }
        return view
    }()

    private func folderView(_ style: MainWindowController.ViewStyle) -> NSViewController {
        switch style {
        case .icons: return iconView
        case .list: return listView
        case .columns: return columnView
        }
    }

    /// Shows the pane's folder in its view and its path in the pane's path bar. The view loads the folder when the
    /// folder or the view style has changed since it last loaded, or always when `force` is set.
    func show(force: Bool) {
        let folder = navigator.current
        let style = viewStyle
        let changed = folder != shownFolder || style != shownStyle
        container.show(folderView(style))
        container.setPath(folder)
        guard force || changed else { return }
        shownFolder = folder
        shownStyle = style
        switch style {
        case .icons: iconView.load(folder: folder)
        case .list: listView.load(folder: folder)
        case .columns: columnView.load(folder: folder)
        }
    }

    /// Selects and scrolls to `url` in the view that is showing.
    func reveal(_ url: URL) {
        switch viewStyle {
        case .icons: iconView.reveal(url)
        case .list: listView.reveal(url, expandLast: false)
        case .columns: columnView.reveal(url)
        }
    }

    /// The selection of the view that is showing.
    var selectedItems: [FileItem] {
        switch viewStyle {
        case .icons: return iconView.selectedItems
        case .list: return listView.selectedItems
        case .columns: return columnView.selectedItems
        }
    }

    /// The keyboard focus of the pane: its table, grid or browser.
    var focusView: NSView? { findFolderView(in: container.view) }

    /// Whether `host` is one of this pane's three folder views (the context-menu sender tells which pane it came from).
    func owns(_ host: AnyObject) -> Bool {
        host === listView || host === iconView || host === columnView
    }

    func setFocusOutline(_ on: Bool) {
        outline.isOn = on
    }

    private func findFolderView(in view: NSView) -> NSView? {
        if view is FileOutlineView || view is IconCollectionView || view is FileBrowser { return view }
        for sub in view.subviews {
            if let found = findFolderView(in: sub) { return found }
        }
        return nil
    }
}

/// The two folder panes side by side. The second item is collapsed while the window has one pane.
@MainActor
final class FolderPanesController: NSSplitViewController {
    let leftItem: NSSplitViewItem
    let rightItem: NSSplitViewItem
    /// The wanted state. It is applied to the right item once the view has loaded (viewDidLoad), so a window that
    /// starts with one pane never shows the second one, whatever the order of the calls.
    private var wantsSplit = false

    init(left: NSViewController, right: NSViewController) {
        leftItem = NSSplitViewItem(viewController: left)
        rightItem = NSSplitViewItem(viewController: right)
        super.init(nibName: nil, bundle: nil)
        leftItem.minimumThickness = 200
        leftItem.canCollapse = false
        rightItem.minimumThickness = 200
        rightItem.canCollapse = true
        addSplitViewItem(leftItem)
        addSplitViewItem(rightItem)
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override func viewDidLoad() {
        super.viewDidLoad()
        applySplit()
    }

    /// Two panes are showing.
    var isSplit: Bool { wantsSplit }

    func setSplit(_ on: Bool) {
        guard on != wantsSplit else { return }
        wantsSplit = on
        applySplit()
        if on {
            // The divider goes to the middle once the right pane has a width; before that the call does nothing.
            DispatchQueue.main.async { [weak self] in
                guard let self, self.splitView.bounds.width > 0 else { return }
                self.splitView.setPosition(self.splitView.bounds.width / 2, ofDividerAt: 0)
            }
        }
    }

    private func applySplit() {
        guard isViewLoaded else { return }
        rightItem.isCollapsed = !wantsSplit
    }
}

// MARK: - the window's panes

extension MainWindowController {
    /// Two panes are showing.
    var isSplitPane: Bool { panesController.isSplit }

    /// The other pane can take copies and moves now: the window is split and shows folders.
    var canTransferBetweenPanes: Bool { isSplitPane && showingFolders }

    /// The focused pane's navigator is the window's; in Search, Ask or Indexed mode the focused pane still answers.
    var focusedPane: FolderPane { panes[focusedIndex] }

    /// View > Split Pane (⌘⌥\) and the toolbar button.
    @objc func toggleSplitPaneAction(_ sender: Any?) { toggleSplitPane() }

    func toggleSplitPane() { setSplitPane(!isSplitPane) }

    func setSplitPane(_ on: Bool) {
        guard on != isSplitPane else { return }
        panesController.setSplit(on)
        if on {
            // The new pane shows its own folder, which may not have loaded yet.
            if showingFolders { panes[1].show(force: false) }
        } else {
            focusPane(0)
        }
        refreshFolderTitle()
        updateChrome()
    }

    /// Makes `index` the focused pane: the title, the toolbar's back and forward, the view switcher, the sidebar
    /// highlight and the outline follow it. `keyboard` also moves the keyboard into that pane's folder view.
    func focusPane(_ index: Int, keyboard: Bool = false) {
        guard panes.indices.contains(index) else { return }
        if index != focusedIndex {
            focusedIndex = index
            sidebar.highlight(folder: navigator.current)
            refreshFolderTitle()
            updateChrome()                     // toolbar, outline, status line and the saved tabs follow the focus
        }
        if keyboard, showingFolders, let view = focusedPane.focusView {
            window.makeFirstResponder(view)
        }
    }

    /// The window title is the focused pane's folder name, in Mac folder mode.
    func refreshFolderTitle() {
        guard showingFolders else { return }
        let folder = navigator.current
        window.title = folder.lastPathComponent.isEmpty ? "/" : folder.lastPathComponent
    }

    /// The outline sits on the focused pane, and only while two panes show folders. In a split window the status line
    /// is the focused pane's: the others keep their text and show it again when they get the focus.
    func refreshPaneOutlines() {
        for (i, pane) in panes.enumerated() {
            pane.setFocusOutline(isSplitPane && showingFolders && i == focusedIndex)
            pane.container.showsStatus = !isSplitPane || i == focusedIndex
        }
    }

    // MARK: callbacks from the panes' folder views

    /// A folder view navigated (a double-click, Return, a column): its pane takes the focus and opens the folder.
    func paneNavigate(_ index: Int, _ url: URL) {
        focusPane(index)
        openFolder(url)
    }

    func paneEnclosingFolder(_ index: Int) {
        focusPane(index)
        goEnclosingFolder()
    }

    /// Columns: the folder selected in a column moves that pane's path bar and, when it is focused, the title.
    func paneFocusedFolder(_ index: Int, _ url: URL) {
        panes[index].container.setPath(url)
        if index == focusedIndex {
            window.title = url.lastPathComponent.isEmpty ? "/" : url.lastPathComponent
        }
    }

    func paneStatus(_ index: Int, _ text: String) {
        panes[index].container.setStatus(text)
    }

    func paneBusy(_ on: Bool) {
        adjustBusy(on ? 1 : -1)
    }

    // MARK: copy and move between panes

    /// F5 (copy) and F6 (move): the focused pane's selection goes to the other pane's folder.
    func transferSelection(copy: Bool) {
        guard canTransferBetweenPanes else { return }
        transfer(focusedPane.selectedItems.map(\.url), from: focusedIndex, copy: copy)
    }

    /// The context-menu items "Copy to Other Pane" and "Move to Other Pane": `host` is the folder view that was clicked,
    /// which may belong to a pane that does not have the focus.
    func transferFromPane(_ host: AnyObject, urls: [URL], copy: Bool) {
        guard canTransferBetweenPanes, let source = panes.firstIndex(where: { $0.owns(host) }) else { return }
        transfer(urls, from: source, copy: copy)
    }

    private func transfer(_ urls: [URL], from source: Int, copy: Bool) {
        let target = 1 - source
        let from = panes[source]
        let destination = panes[target].navigator.current
        guard !urls.isEmpty else {
            from.container.setStatus(copy ? "Select items to copy to the other pane." : "Select items to move to the other pane.")
            return
        }
        guard from.navigator.current.standardizedFileURL != destination.standardizedFileURL else {
            from.container.setStatus("The other pane shows this folder already.")
            return
        }
        // FileOpsService does the work: it never overwrites (a name taken gets a number) and it keeps the undo record.
        let context = FileOpContext(window: window, status: { [weak from] text in from?.container.setStatus(text) })
        FileOpsService.shared.transfer(urls, into: destination, copy: copy, ctx: context)
    }

    // MARK: keys and mouse

    /// Installed once per window: a click inside a pane focuses it.
    func installPaneMonitors() {
        guard !snapshot, paneMouseMonitor == nil else { return }
        paneMouseMonitor = NSEvent.addLocalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown]) { [weak self] event in
            guard let self else { return event }
            MainActor.assumeIsolated { self.paneClicked(event) }
            return event
        }
    }

    private func paneClicked(_ event: NSEvent) {
        guard event.window === window, isSplitPane else { return }
        for (i, pane) in panes.enumerated() {
            let view = pane.container.view
            guard view.window === window else { continue }
            if view.convert(view.bounds, to: nil).contains(event.locationInWindow) {
                focusPane(i)
                return
            }
        }
    }

    /// Tab moves the focus to the other pane; F5 and F6 copy and move. Only for this window's key focus in a split
    /// window that shows folders, and never while a text field is being edited.
    func paneShortcut(_ event: NSEvent) -> Bool {
        guard isSplitPane, showingFolders, NSApp.keyWindow === window else { return false }
        if window.firstResponder is NSText { return false }
        let flags = event.modifierFlags.intersection([.command, .option, .control, .shift])
        guard flags.isEmpty else { return false }
        switch event.keyCode {
        case 48:                                       // Tab
            focusPane(1 - focusedIndex, keyboard: true)
            return true
        case 96:                                       // F5
            transferSelection(copy: true)
            return true
        case 97:                                       // F6
            transferSelection(copy: false)
            return true
        default:
            return false
        }
    }

    // MARK: saved tabs (WindowTabs.swift)

    /// This window as a saved tab: the left pane is the tab's folder and view; the right pane and the focus follow.
    var savedTab: TabStore.Tab {
        let left = panes[0], right = panes[1]
        var tab = TabStore.Tab(folder: left.navigator.current, view: left.viewStyle)
        tab.split = isSplitPane
        tab.rightFolder = right.navigator.current
        tab.rightView = right.viewStyle
        tab.focusRight = focusedIndex == 1
        return tab
    }

    /// Applies a saved tab's split state after the window has started. Older saved tabs have none, so nothing changes.
    func restorePanes(from tab: TabStore.Tab?) {
        guard let tab, tab.split else { return }
        panes[1].viewStyle = tab.rightView
        if let folder = tab.rightFolder, folder != panes[1].navigator.current {
            panes[1].navigator.go(to: folder)
        }
        setSplitPane(true)
        if tab.focusRight { focusPane(1) }
    }
}
