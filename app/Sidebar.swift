// The source-list sidebar: Favorites (home and the standard folders), Indexed (the MySearch collections and
// groups, from GET /api/collections) and Locations (the boot volume). Selecting a row reports a SidebarNode.Kind;
// the main window decides what that means.

import Cocoa

/// One row of the sidebar. Section rows are headers and cannot be selected.
@MainActor
final class SidebarNode: NSObject {
    enum Kind {
        case section(String)
        case place(title: String, url: URL, symbol: String)
        case collection(IndexedCollection)
        case group(IndexedGroup)
        case note(String)
    }

    let kind: Kind
    var children: [SidebarNode] = []
    /// Set on the rows of the Tags section: selecting one lists the files with that colour tag (Tags.swift).
    var tagColor: TagColor?
    /// Set on the rows of the Saved section (SavedSearches.swift). Such a row is a place row whose URL is no folder, so the
    /// folder code never takes it for one; selecting it runs the saved search.
    var savedSearch: SavedSearch?

    init(_ kind: Kind) { self.kind = kind }

    convenience init(saved s: SavedSearch) {
        self.init(.place(title: s.name, url: URL(fileURLWithPath: "/.mysearch-saved/\(s.id)"), symbol: "magnifyingglass"))
        savedSearch = s
    }

    var isSection: Bool { if case .section = kind { return true }; return false }
    var canBeSelected: Bool {
        switch kind {
        case .section, .note: return false
        default: return true
        }
    }

    var title: String {
        switch kind {
        case .section(let t): return t
        case .place(let t, _, _): return t
        case .collection(let c): return c.name
        case .group(let g): return g.name
        case .note(let t): return t
        }
    }

    var symbol: String {
        switch kind {
        case .place(_, _, let s): return s
        case .collection(let c): return c.enabled ? "square.stack.3d.up" : "square.stack.3d.up.slash"
        case .group: return "rectangle.3.group"
        default: return "circle"
        }
    }

    /// The folder a place row opens, if it is one.
    var placeURL: URL? { if case .place(_, let u, _) = kind { return u }; return nil }

    /// A collection or a group in the Indexed section.
    var isIndexedRow: Bool {
        switch kind {
        case .collection, .group: return true
        default: return false
        }
    }
}

/// Colours for the sidebar. In an off-screen snapshot the app is not active, so AppKit resolves the dynamic label and
/// accent colours to their disabled greys; there the fixed equivalents are used, and the live app keeps the dynamic ones.
enum SidebarColors {
    nonisolated(unsafe) static var snapshotRun = false
    private static var dark: Bool { NSApp.appearance?.name == .darkAqua }
    static var label: NSColor {
        guard snapshotRun else { return .labelColor }
        return dark ? NSColor(white: 0.92, alpha: 1) : NSColor(white: 0.1, alpha: 1)
    }
    static var accent: NSColor {
        snapshotRun ? NSColor(srgbRed: 0.04, green: 0.52, blue: 1, alpha: 1) : .controlAccentColor
    }
}

/// The sidebar's outline. Before macOS 26 its content is drawn without vibrancy, so the labels keep their real colour and
/// the accent-tinted icons never wash out to a "disabled" grey (in an inactive or off-screen window too). On macOS 26 and
/// later the system's own source-list material and vibrancy are kept.
final class SidebarOutlineView: NSOutlineView {
    override var allowsVibrancy: Bool {
        if #available(macOS 26, *) {
            return super.allowsVibrancy
        } else {
            return false
        }
    }
}

/// A sidebar cell. Before macOS 26 its icon is tinted with the accent colour (white on a selected row), so the rows never
/// look disabled, whether or not the window is key. On macOS 26 and later the system tints the icon and recolours it on
/// selection, so nothing is set here.
@MainActor
final class SidebarCell: NSTableCellView {
    /// The secondary-coloured parts of the row (a group's member count, the eject button). A selected row (the accent colour)
    /// takes white for them; a custom colour is not swapped for white by AppKit, as the system label colours are.
    var secondaryViews: [NSView] = []

    override var backgroundStyle: NSView.BackgroundStyle {
        didSet {
            applyTint()
            let colour = MSEStyle.secondary(on: backgroundStyle)
            for case let label as NSTextField in secondaryViews { label.textColor = colour }
            for case let button as NSButton in secondaryViews { button.contentTintColor = colour }
        }
    }

    func applyTint() {
        if #available(macOS 26, *) {
            // system colours: the source list tints the symbol itself
        } else {
            let color: NSColor = backgroundStyle == .emphasized ? .alternateSelectedControlTextColor : SidebarColors.accent
            imageView?.contentTintColor = color
            // the size and weight stay MSEStyle.symbolConfig's; only the colour layer is added (an inactive or off-screen
            // window greys a plain tint, a palette colour does not)
            imageView?.symbolConfiguration = MSEStyle.symbolConfig.applying(NSImage.SymbolConfiguration(paletteColors: [color]))
        }
    }
}

@MainActor
final class SidebarViewController: NSViewController, NSOutlineViewDataSource, NSOutlineViewDelegate {
    var onSelect: ((SidebarNode.Kind) -> Void)?
    /// True while the collections are being fetched, then false.
    var onLoading: ((Bool) -> Void)?
    /// The collections and groups, once fetched.
    var onLibrary: ((IndexedLibrary) -> Void)?
    /// The Indexed selection, as collection names (⌘-click adds, an empty list means every indexed folder).
    var onIndexedSelection: (([String]) -> Void)?

    let outline = SidebarOutlineView()                  // internal: SidebarSources.swift refreshes sections
    var roots: [SidebarNode] = []
    private var indexedRoot: SidebarNode!
    /// The Saved section: the saved searches (SavedSearches.swift). It is in `roots` only while there are some.
    private var savedRoot: SidebarNode!
    var suppressCallback = false                        // internal: SidebarSources.swift refreshes sections silently
    /// The groups by id, with the label, aliases and tab flag the sidebar and its menus need (see GroupEditor.swift).
    private var details: [String: GroupDetail] = [:]
    private var observers: [NSObjectProtocol] = []
    /// A folder row dragged in the sidebar: "<folder>\u{1F}<group it is dragged out of, or empty>".
    static let folderDragType = NSPasteboard.PasteboardType("com.mysearch.sidebar.folder")

    override func loadView() {
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false
        scroll.automaticallyAdjustsContentInsets = true
        scroll.documentView = outline

        let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("sidebar"))
        column.resizingMask = .autoresizingMask
        column.minWidth = 100
        outline.addTableColumn(column)
        outline.outlineTableColumn = column
        outline.headerView = nil
        outline.style = .sourceList
        outline.floatsGroupRows = false
        outline.indentationPerLevel = 12
        outline.autoresizesOutlineColumn = true
        outline.columnAutoresizingStyle = .lastColumnOnlyAutoresizingStyle
        outline.focusRingType = .none
        outline.dataSource = self
        outline.delegate = self
        outline.allowsEmptySelection = true
        outline.allowsMultipleSelection = true              // ⌘-click picks several Indexed folders
        outline.registerForDraggedTypes([Self.folderDragType, .fileURL])   // .fileURL: folders dropped from Finder (pins)
        outline.setDraggingSourceOperationMask([.copy, .move], forLocal: true)
        let contextMenu = NSMenu()
        contextMenu.delegate = self
        outline.menu = contextMenu
        view = scroll
        buildRoots()
        observeGroupNotes()
        installSidebarSources()                         // SidebarSources.swift: cloud, volumes, pins, recents, section memory
    }

    /// The one column always spans the sidebar's width, so a long name (Macintosh HD) is never cut short.
    override func viewDidLayout() {
        super.viewDidLayout()
        outline.sizeLastColumnToFit()
    }

    private func buildRoots() {
        let home = SidebarSources.home                       // the fixture's home in snapshot fixture mode
        let favorites = SidebarNode(.section("Favorites"))
        favorites.children = SidebarSources.favoriteNodes(home: home)      // the standard folders, then pinned ones
        let recents = SidebarNode(.section("Recents"))
        recents.children = SidebarSources.recentChildren()
        indexedRoot = SidebarNode(.section("Indexed"))
        indexedRoot.children = [SidebarNode(.note("Loading…"))]
        savedRoot = SidebarNode(.section("Saved"))
        let locations = SidebarNode(.section("Locations"))
        locations.children = SidebarSources.locationNodes()  // the boot volume, cloud drives, mounted volumes
        let tags = SidebarNode(.section("Tags"))
        tags.children = TagColor.menuOrder.map { color in
            // A place whose URL is no real folder, so highlight(folder:) and Pin to Sidebar never match it.
            let row = SidebarNode(.place(title: color.name, url: URL(fileURLWithPath: "/.mysearch-tags/\(color.name)"), symbol: "circle.fill"))
            row.tagColor = color
            return row
        }
        roots = [favorites, recents, indexedRoot, locations, tags]
        outline.reloadData()
        for r in roots { SidebarSources.applySectionState(outline, r) }
        observeSaved()
        reloadSaved()
    }

    /// Fetches the saved searches and lists them under Saved. Counts as loading, as the Indexed list does; a failed fetch
    /// keeps what was listed.
    func reloadSaved() {
        onLoading?(true)
        Task {
            defer { onLoading?(false) }
            guard let items = try? await SavedSearchStore.load() else { return }
            showSaved(items)
        }
    }

    /// The Saved section lists these searches, and is shown only while there are some. The rest of the sidebar keeps its
    /// selection: a list that changes in place is reloaded as one item.
    func showSaved(_ items: [SavedSearch]) {
        guard let savedRoot else { return }
        // the selected rows are remembered and selected again, so a change to the list keeps the folder highlight
        let picked = outline.selectedRowIndexes.compactMap { outline.item(atRow: $0) as? SidebarNode }
        savedRoot.children = items.map { SidebarNode(saved: $0) }
        let shown = roots.contains { $0 === savedRoot }
        if !items.isEmpty && !shown {
            guard let at = roots.firstIndex(where: { $0 === indexedRoot }) else { return }
            roots.insert(savedRoot, at: at + 1)
            reloadKeeping(picked)
        } else if items.isEmpty && shown {
            roots.removeAll { $0 === savedRoot }
            reloadKeeping(picked)
        } else if shown {
            outline.reloadItem(savedRoot, reloadChildren: true)
            SidebarSources.applySectionState(outline, savedRoot)
            outline.sizeLastColumnToFit()
            reselect(picked)
        }
    }

    /// Reloads the outline, restores the sections' open state and the selection (quietly: nothing is re-run), and refits
    /// the one column (a reload alone leaves a row cut one width short).
    private func reloadKeeping(_ picked: [SidebarNode]) {
        outline.reloadData()
        for r in roots { SidebarSources.applySectionState(outline, r) }
        reselect(picked)
        outline.sizeLastColumnToFit()
    }

    private func reselect(_ picked: [SidebarNode]) {
        let rows = IndexSet(picked.compactMap { node -> Int? in
            let row = outline.row(forItem: node)
            return row >= 0 ? row : nil
        })
        suppressCallback = true
        if rows.isEmpty { outline.deselectAll(nil) } else { outline.selectRowIndexes(rows, byExtendingSelection: false) }
        suppressCallback = false
        outline.sizeLastColumnToFit()
    }

    /// Snapshot and test hook: how many saved searches the Saved section lists.
    var savedCount: Int { savedRoot?.children.count ?? 0 }

    /// Snapshot hook: a click on the saved search at `index`, as the user makes it (the selection callback runs, so the
    /// search runs in the window with its folders).
    func selectSavedForSnapshot(_ index: Int) {
        guard let savedRoot, savedRoot.children.indices.contains(index) else { return }
        let node = savedRoot.children[index]
        outline.expandItem(savedRoot)
        let row = outline.row(forItem: node)
        guard row >= 0 else { return }
        outline.selectRowIndexes(IndexSet(integer: row), byExtendingSelection: false)
    }

    /// The saved searches change (saved, renamed, edited, deleted): the section lists them again.
    private func observeSaved() {
        observers.append(NotificationCenter.default.addObserver(forName: SavedSearchStore.didChange, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.reloadSaved() }
        })
    }

    /// Fetches the collections and groups from the service and shows them under Indexed.
    func reloadIndexed() {
        onLoading?(true)
        Task {
            defer { onLoading?(false) }
            do {
                let (lib, groups) = try await MSEClient.shared.libraryWithGroups()
                details = Dictionary(groups.map { ($0.name, $0) }, uniquingKeysWith: { first, _ in first })
                showIndexed(lib)
                onLibrary?(lib)
            } catch {
                indexedRoot.children = [SidebarNode(.note("Search service not running"))]
                outline.reloadItem(indexedRoot, reloadChildren: true)
                SidebarSources.applySectionState(outline, indexedRoot)
                outline.sizeLastColumnToFit()     // as showIndexed: the reload must not leave the column stale
            }
        }
    }

    private func showIndexed(_ lib: IndexedLibrary) {
        var grouped = Set<String>()
        var nodes: [SidebarNode] = []
        for g in lib.groups {
            let node = SidebarNode(.group(g))
            node.children = lib.collections.filter { g.members.contains($0.name) }.map { SidebarNode(.collection($0)) }
            grouped.formUnion(g.members)
            nodes.append(node)
        }
        nodes += lib.collections.filter { !grouped.contains($0.name) }.map { SidebarNode(.collection($0)) }
        indexedRoot.children = nodes.isEmpty ? [SidebarNode(.note("No collections yet"))] : nodes
        outline.reloadItem(indexedRoot, reloadChildren: true)
        SidebarSources.applySectionState(outline, indexedRoot)
        // Refit after the reload: the new rows can widen the outline past the sidebar, and only a refit brings the column back
        // to the clip view (reloadKeeping does the same). Without it the selection runs past the sidebar edge, depending on
        // whether Saved or Indexed loaded last.
        outline.sizeLastColumnToFit()
    }

    /// Selects the favorite or location that is this folder, without reporting it back as a user choice.
    func highlight(folder url: URL?) {
        guard let url else { suppressCallback = true; outline.deselectAll(nil); suppressCallback = false; return }
        let target = url.standardizedFileURL
        for root in roots where root.title != "Recents" {     // Recents repeats folders that have a real row elsewhere
            for node in root.children where node.placeURL?.standardizedFileURL == target {
                let row = outline.row(forItem: node)
                guard row >= 0, outline.selectedRow != row else { return }
                suppressCallback = true
                outline.selectRowIndexes(IndexSet(integer: row), byExtendingSelection: false)
                suppressCallback = false
                return
            }
        }
        suppressCallback = true
        outline.deselectAll(nil)
        suppressCallback = false
    }

    // MARK: NSOutlineViewDataSource

    func outlineView(_ outlineView: NSOutlineView, numberOfChildrenOfItem item: Any?) -> Int {
        (item as? SidebarNode)?.children.count ?? roots.count
    }

    func outlineView(_ outlineView: NSOutlineView, child index: Int, ofItem item: Any?) -> Any {
        (item as? SidebarNode)?.children[index] ?? roots[index]
    }

    func outlineView(_ outlineView: NSOutlineView, isItemExpandable item: Any) -> Bool {
        guard let node = item as? SidebarNode else { return false }
        if node.isSection { return true }
        if case .group = node.kind { return true }
        return false
    }

    // MARK: NSOutlineViewDelegate

    func outlineView(_ outlineView: NSOutlineView, isGroupItem item: Any) -> Bool {
        (item as? SidebarNode)?.isSection ?? false
    }

    func outlineView(_ outlineView: NSOutlineView, shouldSelectItem item: Any) -> Bool {
        (item as? SidebarNode)?.canBeSelected ?? false
    }

    func outlineView(_ outlineView: NSOutlineView, viewFor tableColumn: NSTableColumn?, item: Any) -> NSView? {
        guard let node = item as? SidebarNode else { return nil }
        if node.isSection {
            // The header is the section's own title ("Favorites", "Recents", "Indexed", "Saved", "Locations", "Tags"), as written.
            // The Indexed section is shown as "Deep indexed"; its key stays "Indexed" (isOpen, applySectionState).
            let label = NSTextField(labelWithString: node === indexedRoot ? "Deep indexed" : node.title)
            label.font = MSEStyle.sectionHeaderFont
            label.textColor = MSEStyle.secondary
            // The label sits in a view of its own, not as the row itself: AppKit restyles a bare label that is the whole row
            // of a source-list group (it draws it in a pale system grey, 1.96:1 on the sidebar in light, whatever its
            // textColor is) and leaves a label inside another view as it was set.
            let header = NSView()
            guard node === indexedRoot else {
                label.translatesAutoresizingMaskIntoConstraints = false
                header.addSubview(label)
                NSLayoutConstraint.activate([
                    label.leadingAnchor.constraint(equalTo: header.leadingAnchor, constant: 2),
                    label.centerYAnchor.constraint(equalTo: header.centerYAnchor),
                ])
                return header
            }
            // Indexed: the "+" in the header offers New Group… and Deep-index a Folder… (indexedPlusTapped).
            let plusImage = NSImage(systemSymbolName: "plus", accessibilityDescription: "New Group")?
                .withSymbolConfiguration(MSEStyle.symbolConfig)
            let plus = NSButton(image: plusImage ?? NSImage(), target: self, action: #selector(indexedPlusTapped(_:)))
            plus.isBordered = false
            plus.toolTip = "New Group or Deep-index a Folder"
            plus.contentTintColor = MSEStyle.secondary
            label.translatesAutoresizingMaskIntoConstraints = false
            plus.translatesAutoresizingMaskIntoConstraints = false
            header.addSubview(label)
            header.addSubview(plus)
            NSLayoutConstraint.activate([
                label.leadingAnchor.constraint(equalTo: header.leadingAnchor, constant: 2),
                label.centerYAnchor.constraint(equalTo: header.centerYAnchor),
                plus.trailingAnchor.constraint(equalTo: header.trailingAnchor, constant: -6),
                plus.centerYAnchor.constraint(equalTo: header.centerYAnchor),
                plus.widthAnchor.constraint(equalToConstant: 18),
            ])
            return header
        }
        let cell = SidebarCell()
        let image = NSImageView()
        image.image = NSImage(systemSymbolName: node.symbol, accessibilityDescription: node.title)?
            .withSymbolConfiguration(MSEStyle.symbolConfig)
        if let color = node.tagColor { image.image = TagStore.swatch(color, diameter: 12) }      // a coloured dot, not tinted
        image.translatesAutoresizingMaskIntoConstraints = false
        let text = NSTextField(labelWithString: node.title)
        text.lineBreakMode = .byTruncatingTail
        text.translatesAutoresizingMaskIntoConstraints = false
        if #available(macOS 26, *) {
            // the system label colour and vibrancy
        } else {
            text.textColor = SidebarColors.label
        }
        if case .note = node.kind { text.textColor = MSEStyle.secondary; text.font = .systemFont(ofSize: 12) }
        cell.addSubview(image)
        cell.addSubview(text)
        cell.imageView = image
        cell.textField = text
        if case .group(let g) = node.kind { addMemberCount(to: cell, text: text, group: g) }
        decoratePlaceCell(cell, text: text, node: node)          // eject button on mounted volumes (SidebarSources.swift)
        cell.applyTint()
        NSLayoutConstraint.activate([
            image.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 2),
            image.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
            image.widthAnchor.constraint(equalToConstant: 16),
            text.leadingAnchor.constraint(equalTo: image.trailingAnchor, constant: 6),
            text.trailingAnchor.constraint(lessThanOrEqualTo: cell.trailingAnchor, constant: -4),
            text.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
        ])
        return cell
    }

    /// Selects the Indexed rows with these collection names (a snapshot or test hook); reports nothing.
    func selectIndexed(collections names: [String]) { selectIndexed(names: names) }

    /// Makes the sidebar show a search scope chosen elsewhere (the scope button in the search area), without reporting
    /// it back. A group whose members are exactly `names` gets its own row selected; otherwise every folder row with
    /// one of the names is, also those inside a group (a folder that sits in two groups is selected once). No names
    /// clears the Indexed selection.
    func selectIndexed(names: [String]) {
        guard let indexedRoot else { return }
        var rows = IndexSet()
        let wanted = Set(names)
        if !wanted.isEmpty {
            let groupNode = indexedRoot.children.first { node in
                if case .group(let g) = node.kind { return !g.members.isEmpty && Set(g.members) == wanted }
                return false
            }
            if let groupNode {
                let r = outline.row(forItem: groupNode)
                if r >= 0 { rows.insert(r) }
            } else {
                var done = Set<String>()
                func walk(_ parent: SidebarNode) {
                    for child in parent.children {
                        if case .collection(let c) = child.kind, wanted.contains(c.name), !done.contains(c.name) {
                            let r = outline.row(forItem: child)
                            if r >= 0 { rows.insert(r); done.insert(c.name) }
                        }
                        if !child.children.isEmpty { walk(child) }
                    }
                }
                walk(indexedRoot)
            }
        }
        suppressCallback = true
        if rows.isEmpty { outline.deselectAll(nil) } else { outline.selectRowIndexes(rows, byExtendingSelection: false) }
        suppressCallback = false
    }

    /// Selection: a folder row goes to onSelect (as before). Collection and group rows, ⌘-clicked or not, go to
    /// onIndexedSelection as one list of collection names (a group contributes its members); nothing selected = [].
    func outlineViewSelectionDidChange(_ notification: Notification) {
        guard !suppressCallback else { return }
        let selected = outline.selectedRowIndexes.compactMap { outline.item(atRow: $0) as? SidebarNode }
        if let color = selected.first(where: { $0.tagColor != nil })?.tagColor {          // Tags: files with this colour
            TagResultsController.shared.show(color, in: view.window)
            return
        }
        if let s = selected.first(where: { $0.savedSearch != nil })?.savedSearch {   // a saved search runs in this window (SearchView)
            NotificationCenter.default.post(name: SavedSearchStore.picked, object: view.window, userInfo: ["saved": s])
            return
        }
        if selected.isEmpty { onIndexedSelection?([]); return }        // nothing picked: every indexed folder
        let indexed = selected.filter { $0.isIndexedRow }
        if !indexed.isEmpty {
            var names: [String] = []
            for node in indexed {
                switch node.kind {
                case .collection(let c): if !names.contains(c.name) { names.append(c.name) }
                case .group(let g): for m in g.members where !names.contains(m) { names.append(m) }
                default: break
                }
            }
            onIndexedSelection?(names)
            return
        }
        if let place = selected.first(where: { $0.placeURL != nil }) { onSelect?(place.kind) }
    }
}

// MARK: - Groups: the + button, context menus, drag and drop, and the group notifications
// Every change goes through GroupStore (GroupEditor.swift); a success posts GroupNotes.changed, which reloads this list.

/// What a folder row carries when it is dragged (see SidebarViewController.folderDragType).
private struct FolderDrag {
    let folder: String
    let from: String           // the group it is dragged out of, or ""
}

/// A menu item's action, kept alive by the item's representedObject.
@MainActor
private final class MenuAction: NSObject {
    let run: () -> Void
    init(_ run: @escaping () -> Void) { self.run = run }
    @objc func fire() { run() }
}

extension SidebarViewController: NSMenuDelegate {
    /// Reloads the Indexed list on every group change, and opens an empty editor for File > New Group….
    func observeGroupNotes() {
        let center = NotificationCenter.default
        observers.append(center.addObserver(forName: GroupNotes.changed, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.reloadIndexed() }
        })
        observers.append(center.addObserver(forName: .deepIndexChanged, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.reloadIndexed() }
        })
        observers.append(center.addObserver(forName: GroupNotes.new, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.newGroupTapped() }
        })
    }

    @objc func newGroupTapped() {
        GroupEditorSheet.present(on: view.window ?? NSApp.mainWindow)
    }

    /// The "+" in the Indexed header: New Group… (as before) and Deep-index a Folder….
    @objc func indexedPlusTapped(_ sender: NSButton) {
        let menu = NSMenu()
        menu.addItem(menuItem("New Group…") { [weak self] in self?.newGroupTapped() })
        menu.addItem(menuItem("Deep-index a Folder…") { [weak self] in self?.chooseDeepFolder() })
        menu.popUp(positioning: nil, at: NSPoint(x: 0, y: sender.bounds.height + 2), in: sender)
    }

    /// Deep-index a folder chosen in an Open panel: the same POST /api/collections the Folders pane's add flow sends.
    func chooseDeepFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.prompt = "Deep-index"
        panel.message = "Choose a folder to deep-index: full search of all its text, scans, audio and English↔German matching."
        guard panel.runModal() == .OK, let url = panel.url else { return }
        deepIndex(url.path)
    }

    /// Deep-indexes a folder from the app (the "+" menu, a folder's context menu) and reloads the Indexed list.
    func deepIndex(_ path: String) {
        Task { @MainActor [weak self] in
            do {
                try await MSEClient.shared.deepIndex(path: path)
                NotificationCenter.default.post(name: .deepIndexChanged, object: nil)
                self?.reloadIndexed()
            } catch {
                let alert = NSAlert()
                alert.alertStyle = .warning
                alert.messageText = "The folder was not added for deep indexing"
                alert.informativeText = SW.clean(error)
                alert.addButton(withTitle: "OK")
                alert.runModal()
            }
        }
    }

    /// The member count under a group's name ("3", or "3 · hidden" when it has no tab of its own).
    func addMemberCount(to cell: NSView, text: NSTextField, group g: IndexedGroup) {
        let n = g.members.count
        let hidden = details[g.name]?.hidden ?? false
        let count = NSTextField(labelWithString: hidden ? "\(n) · hidden" : "\(n)")
        count.font = .systemFont(ofSize: 11)
        count.textColor = MSEStyle.secondary
        count.alignment = .right
        count.translatesAutoresizingMaskIntoConstraints = false
        count.setContentHuggingPriority(.required, for: .horizontal)
        cell.addSubview(count)
        (cell as? SidebarCell)?.secondaryViews.append(count)
        NSLayoutConstraint.activate([
            count.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -6),
            count.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
            text.trailingAnchor.constraint(lessThanOrEqualTo: count.leadingAnchor, constant: -6),
        ])
    }

    // MARK: menus

    fileprivate func menuItem(_ title: String, enabled: Bool = true, state: NSControl.StateValue = .off,
                              _ run: @escaping () -> Void) -> NSMenuItem {
        let action = MenuAction(run)
        let item = NSMenuItem(title: title, action: #selector(MenuAction.fire), keyEquivalent: "")
        item.target = action
        item.representedObject = action
        item.isEnabled = enabled
        item.state = state
        return item
    }

    /// The menu for the row under the pointer (NSOutlineView sets clickedRow before this runs).
    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        let row = outline.clickedRow
        guard row >= 0, let node = outline.item(atRow: row) as? SidebarNode else { return }
        if let s = node.savedSearch {
            menu.addItem(menuItem("Rename…") { [weak self] in self?.renameSaved(s) })
            menu.addItem(menuItem("Edit Query…") { [weak self] in self?.editSavedQuery(s) })
            menu.addItem(.separator())
            menu.addItem(menuItem("Delete Saved Search…") { [weak self] in self?.deleteSaved(s) })
            return
        }
        switch node.kind {
        case .group(let g):
            guard let d = details[g.name] else { return }
            menu.addItem(menuItem("Rename…") { [weak self] in self?.rename(d) })
            menu.addItem(menuItem("Edit Members…") { [weak self] in self?.editGroup(d) })
            menu.addItem(menuItem("Aliases…") { [weak self] in self?.editGroup(d) })
            menu.addItem(menuItem(d.hidden ? "Show as Tab" : "Hide as Tab") { [weak self] in
                self?.attempt { _ = try await GroupStore.update(d, hidden: !d.hidden) }
            })
            menu.addItem(.separator())
            menu.addItem(menuItem("Delete Group…") { [weak self] in self?.confirmDelete(d) })
        case .collection(let c):
            let names = selectedFolderNames()
            if names.count >= 2, names.contains(c.name) {
                menu.addItem(menuItem("New Group from Selection…") { [weak self] in self?.newGroup(with: names) })
                menu.addItem(.separator())
            }
            let groups = details.values.sorted { $0.label.localizedCaseInsensitiveCompare($1.label) == .orderedAscending }
            let sub = NSMenu()
            for d in groups {
                let member = d.members.contains(c.name)
                sub.addItem(menuItem(d.label, enabled: !member, state: member ? .on : .off) { [weak self] in
                    self?.attempt { _ = try await GroupStore.update(d, members: d.members + [c.name]) }
                })
            }
            if !groups.isEmpty { sub.addItem(.separator()) }
            sub.addItem(menuItem("New Group with This Folder…") { [weak self] in self?.newGroup(with: [c.name]) })
            let addTo = NSMenuItem(title: "Add to Group", action: nil, keyEquivalent: "")
            addTo.submenu = sub
            menu.addItem(addTo)
            for d in groups where d.members.contains(c.name) {
                menu.addItem(menuItem("Remove from “\(d.label)”…") { [weak self] in self?.removeFromGroup(c.name, d) })
            }
        default:
            if node === indexedRoot {
                menu.addItem(menuItem("Deep-index a Folder…") { [weak self] in self?.chooseDeepFolder() })
                return
            }
            if let folder = placeFolder(node), !indexedFolderPaths().contains(folder.path) {
                menu.addItem(menuItem("Deep-index this Folder") { [weak self] in self?.deepIndex(folder.path) })
                menu.addItem(.separator())
            }
            addPlaceItems(node, to: menu)                    // Pin to Sidebar / Remove from Sidebar (SidebarSources.swift)
        }
    }

    /// The folder a sidebar place row stands for, when it is a real folder (not a saved search, not a volume's note).
    private func placeFolder(_ node: SidebarNode) -> URL? {
        guard node.savedSearch == nil, case .place(_, let url, _) = node.kind, SidebarSources.isDirectory(url) else { return nil }
        return url
    }

    /// The paths of the deep-indexed folders the Indexed list shows.
    private func indexedFolderPaths() -> Set<String> {
        Set((indexedRoot?.children ?? []).compactMap { node -> String? in
            if case .collection(let c) = node.kind { return c.path }
            return nil
        })
    }

    // Saved searches: each change goes to the service, and the list is read again on its notification (SavedSearches.swift).
    private func renameSaved(_ s: SavedSearch) {
        guard let win = view.window else { return }
        SavedSearchStore.askName(title: "Rename Saved Search", value: s.name, button: "Rename", in: win) { [weak self] name in
            guard let name else { return }
            self?.attempt { try await SavedSearchStore.rename(s, to: name) }
        }
    }

    private func editSavedQuery(_ s: SavedSearch) {
        guard let win = view.window else { return }
        SavedSearchStore.askName(title: "Edit Query", value: s.query, button: "Save", in: win) { [weak self] query in
            guard let query else { return }
            self?.attempt { try await SavedSearchStore.editQuery(s, to: query) }
        }
    }

    private func deleteSaved(_ s: SavedSearch) {
        guard let win = view.window else { return }
        SavedSearchStore.confirmDelete(s, in: win) { [weak self] yes in
            guard yes else { return }
            self?.attempt { try await SavedSearchStore.remove(s) }
        }
    }

    /// The folder names among the selected Indexed rows (⌘-click).
    private func selectedFolderNames() -> [String] {
        outline.selectedRowIndexes.compactMap { outline.item(atRow: $0) as? SidebarNode }.compactMap { node in
            if case .collection(let c) = node.kind { return c.name }
            return nil
        }
    }

    private func newGroup(with folders: [String]) {
        GroupEditorSheet.present(on: view.window ?? NSApp.mainWindow, existing: nil, preselect: folders)
    }

    private func editGroup(_ d: GroupDetail) {
        GroupEditorSheet.present(on: view.window ?? NSApp.mainWindow, existing: d)
    }

    private func rename(_ d: GroupDetail) {
        let field = NSTextField(string: d.label)
        field.frame = NSRect(x: 0, y: 0, width: 260, height: 24)
        askSheet(message: "Rename “\(d.label)”",
                 info: "The group’s ID (\(d.name)) stays the same. Only the name shown changes.",
                 accessory: field, button: "Rename") { [weak self] in
            let label = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !label.isEmpty else { self?.showError(MSEServerMessage(message: "A group needs a name.")); return }
            self?.attempt { _ = try await GroupStore.update(d, label: label) }
        }
    }

    private func confirmDelete(_ d: GroupDetail) {
        askSheet(message: "Delete “\(d.label)”?",
                 info: "The folders stay where they are and stay indexed. Only the group is deleted.",
                 button: "Delete", style: .warning) { [weak self] in
            self?.attempt { try await GroupStore.delete(d.name) }
        }
    }

    /// Removes one folder from a group after a confirmation. A group's last folder is not removed: offer Delete instead.
    private func removeFromGroup(_ folder: String, _ d: GroupDetail) {
        if d.members.count <= 1 {
            askSheet(message: "“\(d.label)” would have no folders",
                     info: "A group needs at least one folder. Delete the group instead?",
                     button: "Delete Group", style: .warning) { [weak self] in self?.confirmDelete(d) }
            return
        }
        askSheet(message: "Remove “\(folder)” from “\(d.label)”?",
                 info: "The folder is not deleted or un-indexed. It only leaves this group.",
                 button: "Remove") { [weak self] in
            self?.attempt { _ = try await GroupStore.update(d, members: d.members.filter { $0 != folder }) }
        }
    }

    /// A sheet on the sidebar's window with an optional accessory view; `ok` runs when the first button is pressed.
    private func askSheet(message: String, info: String, accessory: NSView? = nil, button: String,
                          style: NSAlert.Style = .informational, ok: @escaping () -> Void) {
        let alert = NSAlert()
        alert.alertStyle = style
        alert.messageText = message
        alert.informativeText = info
        alert.accessoryView = accessory
        alert.addButton(withTitle: button)
        alert.addButton(withTitle: "Cancel")
        guard let window = view.window else { return }
        alert.beginSheetModal(for: window) { response in
            if response == .alertFirstButtonReturn { ok() }
        }
    }

    /// Runs one group change; a refusal from the service is shown as it is.
    private func attempt(_ op: @escaping () async throws -> Void) {
        Task { @MainActor [weak self] in
            do { try await op() } catch { self?.showError(error) }
        }
    }

    private func showError(_ error: Error) {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "The group was not changed"
        alert.informativeText = GroupEditorModel.message(for: error)
        alert.addButton(withTitle: "OK")
        if let window = view.window { alert.beginSheetModal(for: window) { _ in } } else { alert.runModal() }
    }

    // MARK: drag and drop

    private func dragPayload(_ info: NSDraggingInfo) -> FolderDrag? {
        guard let text = info.draggingPasteboard.string(forType: Self.folderDragType) else { return nil }
        let parts = text.components(separatedBy: "\u{1F}")
        guard let folder = parts.first, !folder.isEmpty else { return nil }
        return FolderDrag(folder: folder, from: parts.count > 1 ? parts[1] : "")
    }

    private func groupName(above node: SidebarNode) -> String {
        guard let parent = outline.parent(forItem: node) as? SidebarNode,
              case .group(let g) = parent.kind else { return "" }
        return g.name
    }

    func outlineView(_ outlineView: NSOutlineView, pasteboardWriterForItem item: Any) -> NSPasteboardWriting? {
        guard let node = item as? SidebarNode, case .collection(let c) = node.kind else { return nil }
        let pasteboardItem = NSPasteboardItem()
        pasteboardItem.setString("\(c.name)\u{1F}\(groupName(above: node))", forType: Self.folderDragType)
        return pasteboardItem
    }

    func outlineView(_ outlineView: NSOutlineView, validateDrop info: NSDraggingInfo,
                     proposedItem item: Any?, proposedChildIndex index: Int) -> NSDragOperation {
        if let op = pinDropOperation(outlineView, info, item) { return op }    // Finder folders onto Favorites
        guard let p = dragPayload(info) else { return [] }
        if let node = item as? SidebarNode {
            // a folder goes onto a group (not between rows), and only if it is not in that group already
            guard case .group(let g) = node.kind, index == NSOutlineViewDropOnItemIndex, !g.members.contains(p.folder) else { return [] }
            return .copy
        }
        // the empty space: a folder dragged out of a group leaves it (confirmed in acceptDrop)
        guard item == nil, !p.from.isEmpty else { return [] }
        outlineView.setDropItem(nil, dropChildIndex: NSOutlineViewDropOnItemIndex)
        return .move
    }

    func outlineView(_ outlineView: NSOutlineView, acceptDrop info: NSDraggingInfo,
                     item: Any?, childIndex index: Int) -> Bool {
        if acceptPinDrop(info, item) { return true }                           // Finder folders onto Favorites
        guard let p = dragPayload(info) else { return false }
        if let node = item as? SidebarNode, case .group(let g) = node.kind, let d = details[g.name] {
            attempt { _ = try await GroupStore.update(d, members: d.members + [p.folder]) }
            return true
        }
        guard item == nil, !p.from.isEmpty, let d = details[p.from] else { return false }
        removeFromGroup(p.folder, d)
        return true
    }
}
