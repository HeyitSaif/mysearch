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

    init(_ kind: Kind) { self.kind = kind }

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

/// The sidebar's outline. Its content is drawn without vibrancy, so the labels keep their real colour and the
/// accent-tinted icons never wash out to a "disabled" grey (in an inactive or off-screen window too).
final class SidebarOutlineView: NSOutlineView {
    override var allowsVibrancy: Bool { false }
}

/// A sidebar cell whose icon is tinted with the accent colour (white on a selected row), so the rows never look
/// disabled, whether or not the window is key.
@MainActor
final class SidebarCell: NSTableCellView {
    override var backgroundStyle: NSView.BackgroundStyle {
        didSet { applyTint() }
    }

    func applyTint() {
        let color: NSColor = backgroundStyle == .emphasized ? .alternateSelectedControlTextColor : SidebarColors.accent
        imageView?.contentTintColor = color
        imageView?.symbolConfiguration = NSImage.SymbolConfiguration(paletteColors: [color])
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
        let fm = FileManager.default
        let home = fm.homeDirectoryForCurrentUser
        let favorites = SidebarNode(.section("Favorites"))
        favorites.children = SidebarSources.favoriteNodes(home: home)      // the standard folders, then pinned ones
        let recents = SidebarNode(.section("Recents"))
        recents.children = SidebarSources.recentChildren()
        indexedRoot = SidebarNode(.section("Indexed"))
        indexedRoot.children = [SidebarNode(.note("Loading…"))]
        let volume = (try? URL(fileURLWithPath: "/").resourceValues(forKeys: [.volumeLocalizedNameKey]))?.volumeLocalizedName
        let locations = SidebarNode(.section("Locations"))
        let boot = SidebarNode(.place(title: volume ?? "Macintosh HD", url: URL(fileURLWithPath: "/"), symbol: "internaldrive"))
        locations.children = [boot] + SidebarSources.cloudDriveNodes() + SidebarSources.volumeNodes()
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
            let label = NSTextField(labelWithString: node.title.uppercased())
            label.font = .systemFont(ofSize: 11, weight: .semibold)
            label.textColor = .secondaryLabelColor
            guard node === indexedRoot else { return label }
            // Indexed: the "+" in the header starts a new group.
            let header = NSView()
            let plus = NSButton(image: NSImage(systemSymbolName: "plus", accessibilityDescription: "New Group") ?? NSImage(),
                                target: self, action: #selector(newGroupTapped))
            plus.isBordered = false
            plus.toolTip = "New Group…"
            plus.contentTintColor = .secondaryLabelColor
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
        image.image = NSImage(systemSymbolName: node.symbol, accessibilityDescription: node.title)
        if let color = node.tagColor { image.image = TagStore.swatch(color, diameter: 12) }      // a coloured dot, not tinted
        image.translatesAutoresizingMaskIntoConstraints = false
        let text = NSTextField(labelWithString: node.title)
        text.lineBreakMode = .byTruncatingTail
        text.translatesAutoresizingMaskIntoConstraints = false
        text.textColor = SidebarColors.label
        if case .note = node.kind { text.textColor = .secondaryLabelColor; text.font = .systemFont(ofSize: 12) }
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
        observers.append(center.addObserver(forName: GroupNotes.new, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.newGroupTapped() }
        })
    }

    @objc func newGroupTapped() {
        GroupEditorSheet.present(on: view.window ?? NSApp.mainWindow)
    }

    /// The member count under a group's name ("3", or "3 · hidden" when it has no tab of its own).
    func addMemberCount(to cell: NSView, text: NSTextField, group g: IndexedGroup) {
        let n = g.members.count
        let hidden = details[g.name]?.hidden ?? false
        let count = NSTextField(labelWithString: hidden ? "\(n) · hidden" : "\(n)")
        count.font = .systemFont(ofSize: 11)
        count.textColor = .secondaryLabelColor
        count.alignment = .right
        count.translatesAutoresizingMaskIntoConstraints = false
        count.setContentHuggingPriority(.required, for: .horizontal)
        cell.addSubview(count)
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
            addPlaceItems(node, to: menu)                    // Pin to Sidebar / Remove from Sidebar (SidebarSources.swift)
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
