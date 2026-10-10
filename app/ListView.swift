// The Finder's list view: an outline view with Name, Date Modified, Size and Kind columns. Folders expand in place
// and load their children lazily through the ContentProvider (⌥-click expands every folder below). Columns sort
// like Finder. Opening, Quick Look, the context menu and the file operations are shared with the other views
// (FolderViewCommon, FileOps). Return (or a click on the selected name) renames inline; dragging a row onto a folder
// moves it, with ⌥ it copies.

import Cocoa
import Carbon.HIToolbox

/// The outline view. Keys go to its owner first; anything it does not handle (type-select, arrows) goes to AppKit.
@MainActor
final class FileOutlineView: NSOutlineView, FileActionsProviding {
    var onKey: ((NSEvent) -> Bool)?
    weak var fileActions: FileActions?
    /// A click on the name of the one selected row (after the double-click interval): starts the inline rename.
    var onClickRename: ((Int) -> Void)?
    private var pendingRename: Task<Void, Never>?

    override func keyDown(with event: NSEvent) {
        if onKey?(event) == true { return }
        super.keyDown(with: event)
    }

    // Edit menu: Cut, Copy and Paste reach the folder view through the responder chain.
    @objc func copy(_ sender: Any?) { fileActions?.copySelection() }
    @objc func cut(_ sender: Any?) { fileActions?.cutSelection() }
    @objc func paste(_ sender: Any?) { fileActions?.pasteHere() }

    /// Finder's click-to-rename: a single click on the name of a row that was already the only selected one.
    override func mouseDown(with event: NSEvent) {
        pendingRename?.cancel()
        let point = convert(event.locationInWindow, from: nil)
        let row = self.row(at: point)
        var candidate = false
        if event.clickCount == 1, row >= 0, selectedRowIndexes == IndexSet(integer: row),
           event.modifierFlags.intersection([.command, .shift, .option, .control]).isEmpty,
           let nameIndex = tableColumns.firstIndex(where: { $0.identifier.rawValue == "name" }), column(at: point) == nameIndex,
           let cell = view(atColumn: nameIndex, row: row, makeIfNecessary: false) as? NSTableCellView, let label = cell.textField {
            let frame = convert(label.frame, from: cell)
            let textWidth = min(frame.width, label.attributedStringValue.size().width + 4)
            candidate = NSRect(x: frame.minX, y: frame.minY, width: textWidth, height: frame.height).contains(point)
        }
        super.mouseDown(with: event)                      // tracks the click or drag to the end
        guard candidate, let up = window?.currentEvent, up.type == .leftMouseUp,
              abs(up.locationInWindow.x - event.locationInWindow.x) < 3, abs(up.locationInWindow.y - event.locationInWindow.y) < 3,
              selectedRowIndexes == IndexSet(integer: row) else { return }
        let delay = UInt64((NSEvent.doubleClickInterval + 0.15) * 1_000_000_000)
        pendingRename = Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: delay)
            guard !Task.isCancelled, let self, self.selectedRowIndexes == IndexSet(integer: row) else { return }
            self.onClickRename?(row)
        }
    }
}

/// The name cell: icon, name, and the tag dots right after the name.
@MainActor
final class ListNameCell: NSTableCellView {
    let tagView = NSImageView()
    private var tagWidth: NSLayoutConstraint!

    func setTags(_ colors: [TagColor]) {
        let image = TagStore.dotsImage(for: colors)
        tagView.image = image
        tagWidth.constant = image?.size.width ?? 0
    }

    func installTagView(after label: NSTextField) {
        tagView.translatesAutoresizingMaskIntoConstraints = false
        addSubview(tagView)
        tagWidth = tagView.widthAnchor.constraint(equalToConstant: 0)
        let hug = tagView.leadingAnchor.constraint(equalTo: label.trailingAnchor, constant: 4)
        hug.priority = NSLayoutConstraint.Priority(500)
        NSLayoutConstraint.activate([
            tagWidth, hug,
            tagView.centerYAnchor.constraint(equalTo: centerYAnchor),
            tagView.heightAnchor.constraint(equalToConstant: 10),
            tagView.trailingAnchor.constraint(lessThanOrEqualTo: trailingAnchor, constant: -4),
            tagView.leadingAnchor.constraint(greaterThanOrEqualTo: label.trailingAnchor, constant: 4),
        ])
    }
}

@MainActor
final class ListViewController: NSViewController, NSOutlineViewDataSource, NSOutlineViewDelegate, NSMenuDelegate,
                                NSTextFieldDelegate, FolderHost {
    /// Folders deeper than this do not open with ⌥-click, so a symbolic-link loop cannot run away.
    private static let maxExpandDepth = 12
    private static let sortKeyDefault = "MySearchListSortKey"
    private static let sortAscendingDefault = "MySearchListSortAscending"

    /// A folder opened in MySearch (double-click, ⌘↓, Open).
    var onNavigate: ((URL) -> Void)?
    /// The enclosing folder (⌘↑).
    var onEnclosingFolder: (() -> Void)?
    /// The status line: "N items", "N of M selected", or a message.
    var onStatus: ((String) -> Void)?
    /// True while anything is loading, false once it has all arrived.
    var onBusy: ((Bool) -> Void)?

    let provider: ContentProvider
    private let snapshot: Bool
    private let outline = FileOutlineView()
    private let actions = FileActions()

    /// The folder shown, or nil for a flat list (see `show(items:)`).
    private(set) var folder: URL?
    private(set) var showHidden = false
    private(set) var sortKey: SortKey = .name
    private(set) var sortAscending = true

    private var rootNodes: [ItemNode]?
    /// Bumped whenever the folder, the hidden-file setting or the list changes. Results from an older generation are dropped.
    private var generation = 0
    private var pending = 0 {
        didSet {
            let busy = pending > 0
            if busy != lastBusy {
                lastBusy = busy
                onBusy?(busy)
            }
        }
    }
    private var lastBusy = false
    private var watcher: DispatchSourceFileSystemObject?
    private var refreshTask: Task<Void, Never>?
    /// The item whose name is being edited in place. Refreshes wait until it ends, so they cannot cancel the edit.
    private var renamingURL: URL?
    private var renameCancelled = false
    private var observers: [NSObjectProtocol] = []
    /// The in-place filter (FolderFilter.swift). Nil when no word is typed.
    private var filter: FolderFilter?
    private let filterBar = FolderFilterBar()
    /// The children each folder shows while a filter is on (keyed by the folder's node, the root by the controller).
    /// Cleared whenever the loaded data or the filter changes.
    private var filterMemo: [ObjectIdentifier: [ItemNode]] = [:]
    /// How many top-level items match, while the filter is on (cleared with the memo).
    private var rootMatchCount: Int?
    /// True while the filter opens the folders that hold a match, so ⌥ held down at that moment does not expand everything.
    private var revealingMatches = false

    init(provider: ContentProvider, snapshot: Bool) {
        self.provider = provider
        self.snapshot = snapshot
        super.init(nibName: nil, bundle: nil)
        actions.onOpenFolder = { [weak self] url in self?.onNavigate?(url) }
        actions.host = self
        if !snapshot { restoreSort() }
        observers.append(NotificationCenter.default.addObserver(forName: FileOpsService.changed, object: nil, queue: .main) { [weak self] n in
            MainActor.assumeIsolated { self?.filesChanged(n) }
        })
    }

    deinit {
        for o in observers { NotificationCenter.default.removeObserver(o) }
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override func loadView() {
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.documentView = outline

        outline.rowHeight = 20
        outline.indentationPerLevel = 16
        outline.allowsMultipleSelection = true
        outline.allowsTypeSelect = true
        outline.usesAlternatingRowBackgroundColors = false
        outline.columnAutoresizingStyle = .firstColumnOnlyAutoresizingStyle   // Name takes whatever width is left over
        outline.dataSource = self
        outline.delegate = self
        outline.target = self
        outline.doubleAction = #selector(doubleClicked(_:))
        outline.setDraggingSourceOperationMask([.copy, .move], forLocal: true)     // inside the app: move, or copy with ⌥
        outline.setDraggingSourceOperationMask(.copy, forLocal: false)
        outline.registerForDraggedTypes([.fileURL])
        outline.fileActions = actions
        outline.onKey = { [weak self] event in self?.handleKey(event) ?? false }
        outline.onClickRename = { [weak self] row in self?.beginRename(row: row) }

        let menu = NSMenu()
        menu.delegate = self
        outline.menu = menu

        let name = addColumn("name", title: "Name", width: 400, ascending: true, flexible: true)
        outline.outlineTableColumn = name
        name.minWidth = 160
        _ = addColumn("modified", title: "Date Modified", width: Self.fixedWidths["modified"]!, ascending: false)
        _ = addColumn("size", title: "Size", width: Self.fixedWidths["size"]!, ascending: false)
        _ = addColumn("kind", title: "Kind", width: Self.fixedWidths["kind"]!, ascending: true)
        outline.sortDescriptors = [NSSortDescriptor(key: sortKey.rawValue, ascending: sortAscending)]
        if !snapshot {
            outline.autosaveName = "MySearchListView"
            outline.autosaveTableColumns = true
        }
        filterBar.onChange = { [weak self] text in self?.setFilterText(text) }
        filterBar.onClose = { [weak self] in self?.closeFilter() }
        filterBar.onMoveIntoList = { [weak self] in self?.moveIntoList() }
        view = FolderFilterContainer(content: scroll, bar: filterBar)
    }

    /// Finder's widths for the fixed columns; Name is flexible and gets the rest of the window.
    private static let fixedWidths: [String: CGFloat] = ["modified": 180, "size": 90, "kind": 160]
    private var widthsChecked = false

    private func addColumn(_ id: String, title: String, width: CGFloat, ascending: Bool, flexible: Bool = false) -> NSTableColumn {
        let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(id))
        column.title = title
        column.width = width
        column.minWidth = id == "size" ? 60 : 80
        column.resizingMask = flexible ? [.autoresizingMask, .userResizingMask] : .userResizingMask
        column.sortDescriptorPrototype = NSSortDescriptor(key: id, ascending: ascending)
        outline.addTableColumn(column)
        return column
    }

    /// Once the view has its real width: columns restored from the autosave are kept only when they are sane (Name
    /// at least 160 pt and about 35 % of the width or more); otherwise Finder's defaults apply, Name taking the rest.
    override func viewDidLayout() {
        super.viewDidLayout()
        guard !widthsChecked, view.bounds.width > 300 else { return }
        widthsChecked = true
        let total = view.bounds.width
        let cols = outline.tableColumns
        guard let name = cols.first(where: { $0.identifier.rawValue == "name" }) else { return }
        let fixedSum = cols.filter { $0 !== name }.reduce(CGFloat(0)) { $0 + $1.width }
        let sane = !snapshot && name.width >= 160 && name.width >= total * 0.30
            && fixedSum + name.width <= total + 40
            && cols.filter({ $0 !== name }).allSatisfy { $0.width >= 50 && $0.width <= 400 }
        if !sane {
            for c in cols where c !== name { c.width = Self.fixedWidths[c.identifier.rawValue] ?? c.width }
            let spacing = outline.intercellSpacing.width * CGFloat(cols.count)
            name.width = max(160, total - Self.fixedWidths.values.reduce(0, +) - spacing - 4)
        }
    }

    // MARK: public API

    /// The items the user has selected, in row order.
    var selectedItems: [FileItem] {
        outline.selectedRowIndexes.compactMap { (outline.item(atRow: $0) as? ItemNode)?.item }
    }

    /// Shows `url` as the top level of the list, with its children loaded lazily.
    func load(folder url: URL) {
        let target = url.standardizedFileURL
        if target != folder { dropFilter() }        // the filter belongs to one folder
        folder = target
        generation += 1
        rootNodes = nil
        invalidateFilterMemo()
        outline.reloadData()
        watch(target)
        loadRoot()
    }

    /// Shows a flat list of items (search results, for example). Folders in it still expand.
    func show(items: [FileItem]) {
        folder = nil
        generation += 1
        stopWatching()
        rootNodes = sorted(makeNodes(items, existing: nil, depth: 0))
        invalidateFilterMemo()
        outline.reloadData()
        updateStatus()
    }

    /// Reloads the top level and every expanded folder, keeping expansion and selection.
    func refresh() {
        guard folder != nil, renamingURL == nil else { return }
        loadRoot()
        for node in expandedNodes(rootNodes) { loadChildren(of: node) }
    }

    /// Shows or hides hidden files (⌘⇧.).
    /// Filter This Folder (the command palette): opens the filter bar, as the "/" key does.
    func openFilter() { filterBar.open() }

    func setShowHidden(_ on: Bool) {
        guard on != showHidden else { return }
        showHidden = on
        generation += 1
        refresh()
    }

    /// Sorts every level by `key`, as clicking a column header does.
    func setSort(_ key: SortKey, ascending: Bool) {
        outline.sortDescriptors = [NSSortDescriptor(key: key.rawValue, ascending: ascending)]
        applySort(key, ascending: ascending)
    }

    /// Opens every folder on the way to `target` (inside the current folder), selects it and, with `expandLast`,
    /// expands it too. The work is asynchronous; the busy callback covers it.
    func reveal(_ target: URL, expandLast: Bool) {
        guard let root = folder else { return }
        let base = root.pathComponents
        let parts = Array(target.standardizedFileURL.pathComponents.dropFirst(base.count))
        let token = generation
        Task {
            if rootNodes == nil {
                guard await fillRoot(token: token) else { return }
            }
            var level = rootNodes ?? []
            var found: ItemNode?
            for (index, part) in parts.enumerated() {
                guard let node = level.first(where: { $0.item.url.lastPathComponent == part }) else { break }
                found = node
                let isLast = index == parts.count - 1
                guard node.item.isDir, !isLast || expandLast else { break }
                if node.children == nil {
                    guard await fill(node, token: token) else { return }
                }
                outline.expandItem(node)
                level = node.children ?? []
            }
            if let found, !isStale(token) {
                let row = outline.row(forItem: found)
                if row >= 0 {
                    outline.selectRowIndexes(IndexSet(integer: row), byExtendingSelection: false)
                    outline.scrollRowToVisible(row)
                }
            }
        }
    }

    // MARK: loading

    private func isStale(_ token: Int) -> Bool { token != generation }

    /// Reads the children of `url` through the provider. Nil means the folder could not be read.
    private func readChildren(of url: URL, existing: [ItemNode]?, depth: Int) async -> [ItemNode]? {
        pending += 1
        defer { pending -= 1 }
        do {
            let items = try await provider.items(in: url, recursive: false, includeHidden: showHidden)
            return sorted(makeNodes(items, existing: existing, depth: depth))
        } catch {
            return nil
        }
    }

    /// Loads the top level. Returns false when a newer load has replaced this one.
    private func fillRoot(token: Int) async -> Bool {
        guard let url = folder else { return false }
        let nodes = await readChildren(of: url, existing: rootNodes, depth: 0)
        guard !isStale(token) else { return false }
        rootNodes = nodes ?? []
        invalidateFilterMemo()
        outline.reloadItem(nil, reloadChildren: true)
        if nodes == nil { onStatus?("This folder cannot be read.") } else { updateStatus() }
        return true
    }

    private func loadRoot() {
        let token = generation
        Task { _ = await fillRoot(token: token) }
    }

    /// Loads the children of one folder node. Returns false when a newer load has replaced this one.
    private func fill(_ node: ItemNode, token: Int) async -> Bool {
        let nodes = await readChildren(of: node.item.url, existing: node.children, depth: node.depth + 1)
        guard !isStale(token) else { return false }
        node.children = nodes ?? []
        invalidateFilterMemo()
        outline.reloadItem(node, reloadChildren: true)
        return true
    }

    private func loadChildren(of node: ItemNode) {
        let token = generation
        Task {
            guard await fill(node, token: token) else { return }
            if node.expandAll { expandSubfolders(of: node) }
            updateStatus()
        }
    }

    /// ⌥-click: expands each folder under `node` (their own children load as they arrive).
    private func expandSubfolders(of node: ItemNode) {
        for child in node.children ?? [] where child.item.isDir && child.depth < Self.maxExpandDepth {
            child.expandAll = true
            outline.expandItem(child)
        }
    }

    private func expandedNodes(_ nodes: [ItemNode]?) -> [ItemNode] {
        var out: [ItemNode] = []
        for node in nodes ?? [] where outline.isItemExpanded(node) {
            out.append(node)
            out += expandedNodes(node.children)
        }
        return out
    }

    // MARK: nodes and sorting

    /// One node per URL: nodes that were already on screen are reused, so their expansion survives a reload.
    private func makeNodes(_ items: [FileItem], existing: [ItemNode]?, depth: Int) -> [ItemNode] {
        var byURL: [URL: ItemNode] = [:]
        for node in existing ?? [] { byURL[node.item.url] = node }
        return items.map { item in
            if let node = byURL[item.url] {
                node.item = item
                return node
            }
            let node = ItemNode(item)
            node.depth = depth
            return node
        }
    }

    /// Finder order: the key, then the name. Folders are mixed in with files, as Finder's list view does.
    private func sorted(_ nodes: [ItemNode]) -> [ItemNode] {
        let order = sortedItems(nodes.map(\.item), by: sortKey, ascending: sortAscending, foldersFirst: false)
        let byURL = Dictionary(nodes.map { ($0.item.url, $0) }, uniquingKeysWith: { first, _ in first })
        return order.compactMap { byURL[$0.url] }
    }

    private func applySort(_ key: SortKey, ascending: Bool) {
        sortKey = key
        sortAscending = ascending
        saveSort()
        rootNodes = rootNodes.map(sorted)
        resortLoaded(rootNodes)
        invalidateFilterMemo()
        outline.reloadItem(nil, reloadChildren: true)
    }

    private func resortLoaded(_ nodes: [ItemNode]?) {
        for node in nodes ?? [] {
            if let children = node.children {
                node.children = sorted(children)
                resortLoaded(node.children)
            }
        }
    }

    private func restoreSort() {
        let defaults = UserDefaults.standard
        if let raw = defaults.string(forKey: Self.sortKeyDefault), let key = SortKey(rawValue: raw) {
            sortKey = key
            sortAscending = defaults.object(forKey: Self.sortAscendingDefault) as? Bool ?? true
        }
    }

    private func saveSort() {
        guard !snapshot else { return }
        UserDefaults.standard.set(sortKey.rawValue, forKey: Self.sortKeyDefault)
        UserDefaults.standard.set(sortAscending, forKey: Self.sortAscendingDefault)
    }

    func outlineView(_ outlineView: NSOutlineView, sortDescriptorsDidChange oldDescriptors: [NSSortDescriptor]) {
        guard let descriptor = outlineView.sortDescriptors.first,
              let key = SortKey(rawValue: descriptor.key ?? "") else { return }
        applySort(key, ascending: descriptor.ascending)
    }

    // MARK: data source

    func outlineView(_ outlineView: NSOutlineView, numberOfChildrenOfItem item: Any?) -> Int {
        shownChildren(of: item as? ItemNode).count
    }

    func outlineView(_ outlineView: NSOutlineView, child index: Int, ofItem item: Any?) -> Any {
        // numberOfChildrenOfItem reports the same shown children, so the index is always in range
        shownChildren(of: item as? ItemNode)[index]
    }

    func outlineView(_ outlineView: NSOutlineView, isItemExpandable item: Any) -> Bool {
        (item as? ItemNode)?.item.isDir ?? false
    }

    func outlineViewItemWillExpand(_ notification: Notification) {
        guard let node = notification.userInfo?["NSObject"] as? ItemNode else { return }
        if NSEvent.modifierFlags.contains(.option), !revealingMatches { node.expandAll = true }
        if node.children == nil {
            loadChildren(of: node)
        } else if node.expandAll {
            Task { expandSubfolders(of: node) }
        }
    }

    // MARK: delegate

    func outlineView(_ outlineView: NSOutlineView, viewFor tableColumn: NSTableColumn?, item: Any) -> NSView? {
        guard let node = item as? ItemNode, let column = tableColumn else { return nil }
        switch column.identifier.rawValue {
        case "name":
            let nameCell = cell("name", text: node.item.name, icon: node.item.icon, alignment: .left, tags: node.item.tagColors)
            if let filter {
                nameCell.textField?.attributedStringValue = filter.highlighted(node.item.name, font: nameCell.textField?.font)
            }
            return nameCell
        case "modified":
            return cell("modified", text: FileFormat.date(node.item.mtime), icon: nil, alignment: .left)
        case "size":
            return cell("size", text: FileFormat.size(node.item), icon: nil, alignment: .right)
        default:
            return cell("kind", text: node.item.kind, icon: nil, alignment: .left)
        }
    }

    /// A reusable cell: an optional 16 px icon, then the text. The name column has an icon; the others do not.
    private func cell(_ id: String, text: String, icon: NSImage?, alignment: NSTextAlignment,
                      tags: [TagColor] = []) -> NSTableCellView {
        let identifier = NSUserInterfaceItemIdentifier("cell.\(id)")
        let cell: NSTableCellView
        if let reused = outline.makeView(withIdentifier: identifier, owner: self) as? NSTableCellView {
            cell = reused
        } else {
            cell = id == "name" ? ListNameCell() : NSTableCellView()
            cell.identifier = identifier
            let label = NSTextField(labelWithString: "")
            label.lineBreakMode = .byTruncatingMiddle
            label.translatesAutoresizingMaskIntoConstraints = false
            cell.addSubview(label)
            cell.textField = label
            var constraints = [label.centerYAnchor.constraint(equalTo: cell.centerYAnchor)]
            if id != "name" { constraints.append(label.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -4)) }
            if id == "name" {
                let image = NSImageView()
                image.translatesAutoresizingMaskIntoConstraints = false
                cell.addSubview(image)
                cell.imageView = image
                constraints += [image.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 2),
                                image.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
                                image.widthAnchor.constraint(equalToConstant: 16),
                                image.heightAnchor.constraint(equalToConstant: 16),
                                label.leadingAnchor.constraint(equalTo: image.trailingAnchor, constant: 6)]
            } else {
                constraints.append(label.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 4))
            }
            NSLayoutConstraint.activate(constraints)
            (cell as? ListNameCell)?.installTagView(after: label)
        }
        (cell as? ListNameCell)?.setTags(tags)
        cell.textField?.stringValue = text
        cell.textField?.alignment = alignment
        if let icon {
            icon.size = NSSize(width: 16, height: 16)
            cell.imageView?.image = icon
        }
        return cell
    }

    func outlineViewSelectionDidChange(_ notification: Notification) {
        updateStatus()
    }

    func outlineView(_ outlineView: NSOutlineView, pasteboardWriterForItem item: Any) -> NSPasteboardWriting? {
        (item as? ItemNode).map { $0.item.url as NSURL }
    }

    // MARK: actions

    @objc private func doubleClicked(_ sender: Any?) {
        guard outline.clickedRow >= 0 else { return }
        actions.open(selectedItems)
    }

    /// The keys of the Finder's list view. Returns true when the key was handled.
    private func handleKey(_ event: NSEvent) -> Bool {
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        let command = flags.contains(.command)
        let option = flags.contains(.option)
        let shift = flags.contains(.shift)
        // "/" is ⇧7 on a German keyboard, so shift is allowed; ⌘⌥F opens the filter too
        if flags.subtracting([.shift, .capsLock, .numericPad]).isEmpty, event.characters == "/" {
            filterBar.open()
            return true
        }
        switch Int(event.keyCode) {
        case kVK_ANSI_F where command && option:
            filterBar.open()
        case kVK_Escape where filterBar.isOpen:
            closeFilter()
        case kVK_Space where flags.isEmpty:
            QuickLookCoordinator.shared.toggle(selectedItems.map(\.url))
        case kVK_DownArrow where command:
            actions.open(selectedItems)
        case kVK_UpArrow where command:
            onEnclosingFolder?()
        case kVK_RightArrow where flags.isEmpty:
            guard let node = selectedNode, node.item.isDir else { return false }
            if outline.isItemExpanded(node) {
                if let first = shownChildren(of: node).first { selectOnly(first) }
            } else {
                outline.expandItem(node)
            }
        case kVK_LeftArrow where flags.isEmpty:
            guard let node = selectedNode else { return false }
            if outline.isItemExpanded(node) {
                outline.collapseItem(node)
            } else if let parent = outline.parent(forItem: node) as? ItemNode {
                selectOnly(parent)
            }
        case kVK_ANSI_C where command:
            if option { actions.copyPaths(selectedItems.map(\.url)) } else { actions.copyFiles(selectedItems.map(\.url)) }
        case kVK_ANSI_X where command && !option:
            actions.cutSelection()
        case kVK_ANSI_V where command && !option:
            actions.pasteHere()
        case kVK_Return where flags.isEmpty, kVK_ANSI_KeypadEnter where flags.isEmpty:
            guard outline.selectedRowIndexes.count == 1 else { return false }
            beginRename(row: outline.selectedRow)
        case kVK_ANSI_Period where command && shift:
            setShowHidden(!showHidden)
        default:
            return false
        }
        return true
    }

    private var selectedNode: ItemNode? {
        outline.selectedRow >= 0 ? outline.item(atRow: outline.selectedRow) as? ItemNode : nil
    }

    private func selectOnly(_ node: ItemNode) {
        let row = outline.row(forItem: node)
        guard row >= 0 else { return }
        outline.selectRowIndexes(IndexSet(integer: row), byExtendingSelection: false)
        outline.scrollRowToVisible(row)
    }

    /// Right-click: the clicked row becomes the selection if it was not selected, then the menu is filled for it.
    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        let row = outline.clickedRow
        if row >= 0, !outline.selectedRowIndexes.contains(row) {
            outline.selectRowIndexes(IndexSet(integer: row), byExtendingSelection: false)
        } else if row < 0 {
            outline.deselectAll(nil)                         // a right-click on the background: the folder's own menu
        }
        actions.fillContextMenu(menu, for: selectedItems)
    }

    private func updateStatus() {
        if let filter {
            let roots = rootNodes ?? []
            let matching = rootMatchCount ?? roots.filter { filter.matches($0.item.name) }.count
            rootMatchCount = matching
            onStatus?(filter.statusText(matching: matching, of: roots.count))
            return
        }
        onStatus?(FolderStatus.text(total: rootNodes?.count ?? 0, selected: selectedItems, folder: folder))
    }

    // MARK: the in-place filter (FolderFilter.swift)

    /// The children of `parent` (nil: the top level) that the view shows. Without a filter, all loaded children. With
    /// one, a child is shown when its own name matches or a loaded descendant does, so a folder stays when a match is
    /// inside it. Folders whose children are not loaded can only match by name.
    private func shownChildren(of parent: ItemNode?) -> [ItemNode] {
        let all: [ItemNode] = parent.map { $0.children ?? [] } ?? (rootNodes ?? [])
        guard let filter else { return all }
        let key = parent.map { ObjectIdentifier($0) } ?? ObjectIdentifier(self)
        if let memo = filterMemo[key] { return memo }
        let kept = all.filter { keeps($0, filter) }
        filterMemo[key] = kept
        return kept
    }

    private func keeps(_ node: ItemNode, _ filter: FolderFilter) -> Bool {
        if filter.matches(node.item.name) { return true }
        guard node.item.isDir else { return false }
        return (node.children ?? []).contains { keeps($0, filter) }
    }

    private func setFilterText(_ text: String) {
        filter = FolderFilter(text: text)
        invalidateFilterMemo()
        outline.reloadItem(nil, reloadChildren: true)
        revealMatches(in: nil)
        updateStatus()
    }

    /// Expands the folders that are shown only because something below them matches, so the match is in view.
    private func revealMatches(in parent: ItemNode?) {
        revealingMatches = true
        expandMatchingFolders(in: parent)
        revealingMatches = false
    }

    private func expandMatchingFolders(in parent: ItemNode?) {
        guard let filter else { return }
        for node in shownChildren(of: parent) where node.item.isDir && !filter.matches(node.item.name) {
            guard node.children != nil else { continue }
            outline.expandItem(node)
            expandMatchingFolders(in: node)
        }
    }

    /// The loaded data or the filter changed: the filtered children and the match count are worked out again.
    private func invalidateFilterMemo() {
        filterMemo.removeAll()
        rootMatchCount = nil
    }

    /// Clears the filter without reloading (the caller reloads the rows).
    private func dropFilter() {
        filterBar.close()
        filter = nil
        invalidateFilterMemo()
    }

    private func closeFilter() {
        guard filterBar.isOpen || filter != nil else { return }
        dropFilter()
        outline.reloadItem(nil, reloadChildren: true)
        outline.window?.makeFirstResponder(outline)
        updateStatus()
    }

    /// ↓ or Return in the filter field: the keyboard goes to the list, and the first row is selected if none is.
    private func moveIntoList() {
        outline.window?.makeFirstResponder(outline)
        if outline.selectedRow < 0, outline.numberOfRows > 0 {
            outline.selectRowIndexes(IndexSet(integer: 0), byExtendingSelection: false)
        }
    }

    // MARK: file operations: folder host, refresh after a change, inline rename, drag and drop

    var hostWindow: NSWindow? { isViewLoaded ? view.window : nil }
    var hostFolder: URL? { folder }
    var hostSelection: [FileItem] { selectedItems }
    func hostBeginRename(_ item: FileItem) {
        if let node = node(for: item.url) { beginRename(node) }
    }
    func hostStatus(_ text: String) { onStatus?(text) }

    private func node(for url: URL) -> ItemNode? {
        let wanted = url.standardizedFileURL.path
        func search(_ nodes: [ItemNode]?) -> ItemNode? {
            for node in nodes ?? [] {
                if node.item.url.standardizedFileURL.path == wanted { return node }
                if let found = search(node.children) { return found }
            }
            return nil
        }
        return search(rootNodes)
    }

    /// A file operation finished (anywhere in the app): reload what is on screen, select the new items and, for a
    /// new folder, start renaming it.
    private func filesChanged(_ n: Notification) {
        guard isViewLoaded, view.window != nil, renamingURL == nil else { return }
        let select = (n.userInfo?["select"] as? [URL]) ?? []
        let rename = (n.userInfo?["rename"] as? Bool) ?? false
        guard folder != nil else {
            refreshFlat()
            selectURLs(select)
            return
        }
        let token = generation
        Task { @MainActor in
            guard await fillRoot(token: token) else { return }
            for node in expandedNodes(rootNodes) { _ = await fill(node, token: token) }
            selectURLs(select)
            if rename, select.count == 1, let node = node(for: select[0]) { beginRename(node) }
        }
    }

    /// A flat list (search results, a tag): drop the items that are gone and read the others again.
    private func refreshFlat() {
        guard folder == nil, let nodes = rootNodes else { return }
        let items = nodes.map(\.item.url).filter { FileWork.exists($0) }.map { FileItem(url: $0) }
        show(items: items)
    }

    private func selectURLs(_ urls: [URL]) {
        guard !urls.isEmpty else { return }
        var rows = IndexSet()
        for url in urls {
            if let node = node(for: url) {
                let row = outline.row(forItem: node)
                if row >= 0 { rows.insert(row) }
            }
        }
        guard !rows.isEmpty else { return }
        outline.selectRowIndexes(rows, byExtendingSelection: false)
        if let first = rows.first { outline.scrollRowToVisible(first) }
    }

    // inline rename

    private func beginRename(row: Int) {
        guard row >= 0, let node = outline.item(atRow: row) as? ItemNode else { return }
        beginRename(node)
    }

    private func beginRename(_ node: ItemNode) {
        guard renamingURL == nil,
              let column = outline.tableColumns.firstIndex(where: { $0.identifier.rawValue == "name" }) else { return }
        let row = outline.row(forItem: node)
        guard row >= 0 else { return }
        outline.selectRowIndexes(IndexSet(integer: row), byExtendingSelection: false)
        outline.scrollRowToVisible(row)
        guard let cell = outline.view(atColumn: column, row: row, makeIfNecessary: true) as? NSTableCellView,
              let field = cell.textField else { return }
        renamingURL = node.item.url
        renameCancelled = false
        let name = node.item.url.lastPathComponent
        field.stringValue = name
        field.delegate = self
        field.isEditable = true
        field.isSelectable = true
        field.drawsBackground = true
        field.backgroundColor = .textBackgroundColor
        field.lineBreakMode = .byClipping
        outline.window?.makeFirstResponder(field)
        let baseLength = node.item.isDir ? (name as NSString).length : ((name as NSString).deletingPathExtension as NSString).length
        field.currentEditor()?.selectedRange = NSRange(location: 0, length: baseLength)
    }

    func control(_ control: NSControl, textView: NSTextView, doCommandBy commandSelector: Selector) -> Bool {
        if commandSelector == #selector(NSResponder.cancelOperation(_:)) { renameCancelled = true }
        return false
    }

    func controlTextDidEndEditing(_ obj: Notification) {
        guard let field = obj.object as? NSTextField, let url = renamingURL else { return }
        let newName = field.stringValue
        let cancelled = renameCancelled || (obj.userInfo?["NSTextMovement"] as? Int) == NSTextMovement.cancel.rawValue
        renamingURL = nil
        field.delegate = nil
        field.isEditable = false
        field.isSelectable = false
        field.drawsBackground = false
        field.lineBreakMode = .byTruncatingMiddle
        outline.window?.makeFirstResponder(outline)
        if cancelled {
            refresh()
            refreshFlat()
            return
        }
        actions.rename(url, to: newName)                 // reports a refusal, then reloads and selects the item
    }

    // drag and drop: a row onto a folder moves it, with ⌥ it copies

    private func dropDestination(for item: Any?) -> (node: ItemNode?, url: URL)? {
        if let node = item as? ItemNode {
            if node.item.isDir { return (node, node.item.url) }
            if let parent = outline.parent(forItem: node) as? ItemNode { return (parent, parent.item.url) }
        }
        guard let folder else { return nil }
        return (nil, folder)
    }

    func outlineView(_ outlineView: NSOutlineView, validateDrop info: NSDraggingInfo,
                     proposedItem item: Any?, proposedChildIndex index: Int) -> NSDragOperation {
        guard let destination = dropDestination(for: item) else { return [] }
        let operation = DropPlan.operation(for: DropPlan.urls(info), into: destination.url)
        guard !operation.isEmpty else { return [] }
        outlineView.setDropItem(destination.node, dropChildIndex: NSOutlineViewDropOnItemIndex)
        return operation
    }

    func outlineView(_ outlineView: NSOutlineView, acceptDrop info: NSDraggingInfo, item: Any?, childIndex index: Int) -> Bool {
        guard let destination = dropDestination(for: item) else { return false }
        let urls = DropPlan.urls(info)
        let operation = DropPlan.operation(for: urls, into: destination.url)
        guard !operation.isEmpty else { return false }
        actions.drop(urls, into: destination.url, operation: operation)
        return true
    }

    // MARK: live updates

    /// Watches the shown folder and refreshes it, debounced, when its entries change.
    private func watch(_ url: URL) {
        stopWatching()
        guard !snapshot else { return }
        let fd = open(url.path, O_EVTONLY)
        guard fd >= 0 else { return }
        let source = DispatchSource.makeFileSystemObjectSource(fileDescriptor: fd,
                                                               eventMask: [.write, .rename, .delete, .extend],
                                                               queue: .main)
        source.setEventHandler { [weak self] in
            Task { @MainActor in self?.scheduleRefresh() }
        }
        source.setCancelHandler { close(fd) }
        source.resume()
        watcher = source
    }

    private func stopWatching() {
        watcher?.cancel()
        watcher = nil
    }

    private func scheduleRefresh() {
        refreshTask?.cancel()
        refreshTask = Task {
            try? await Task.sleep(nanoseconds: 300_000_000)
            guard !Task.isCancelled else { return }
            refresh()
        }
    }
}
