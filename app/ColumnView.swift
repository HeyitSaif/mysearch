// The Finder's column view: an NSBrowser with one column per folder level and, for a selected file, a final
// preview column (Quick Look, then name, kind, size and date). Columns are sorted by name with folders and files
// mixed, as Finder sorts them. Children load lazily through the ContentProvider. Context menu, Quick Look and
// the file operations are shared with the list view (FolderViewCommon, FileOps). Rename is a dialog here (a
// browser cell cannot be edited in place); dropping a row onto a folder moves it, with ⌥ it copies.

import Cocoa
import Quartz
import QuickLookThumbnailing
import CoreServices
import Carbon.HIToolbox

/// A column cell with the Finder's 16 pt icon to the left of the name. NSBrowserCell is a text cell and draws no
/// image beside the title, so the cell draws the icon itself and moves the title right to make room for it.
final class IconBrowserCell: NSBrowserCell {
    var icon: NSImage?
    /// The item's tag dots, drawn at the right end of the row (before a folder's arrow).
    var tags: NSImage?
    private static let iconSide: CGFloat = 16
    private static let gap: CGFloat = 6

    override func drawInterior(withFrame cellFrame: NSRect, in controlView: NSView) {
        guard let icon else {
            super.drawInterior(withFrame: cellFrame, in: controlView)
            return
        }
        let side = IconBrowserCell.iconSide
        let left = cellFrame.minX + 4
        icon.draw(in: NSRect(x: left, y: cellFrame.midY - side / 2, width: side, height: side))
        // the title starts after the icon: the text gets the cell's frame from there to its right edge
        let shift = 4 + side + IconBrowserCell.gap
        var textFrame = cellFrame
        textFrame.origin.x += shift
        var reserved: CGFloat = 0
        if let tags {
            let arrow: CGFloat = isLeaf ? 0 : 16
            let width = tags.size.width
            tags.draw(in: NSRect(x: cellFrame.maxX - arrow - 6 - width, y: cellFrame.midY - 5, width: width, height: 10))
            reserved = width + 8 + arrow
        }
        textFrame.size.width = max(cellFrame.width - shift - reserved, 0)
        super.drawInterior(withFrame: textFrame, in: controlView)
    }

    /// NSCell copies a cell by copying its instance bytes, so the copy's `icon` and `tags` point at the same images
    /// without holding a reference of their own. The copy and the original would then each release them, and the
    /// images would be over-released. The copy takes its own +1 on each image it shares. Do not assign the properties
    /// here: that would release a reference the copy never owned.
    override func copy(with zone: NSZone? = nil) -> Any {
        let copy = super.copy(with: zone)
        guard let cell = copy as? IconBrowserCell else { return copy }
        if let icon, cell.icon === icon { _ = Unmanaged.passUnretained(icon).retain() }
        if let tags, cell.tags === tags { _ = Unmanaged.passUnretained(tags).retain() }
        return cell
    }
}

/// The browser. It hands the keys it wants to its owner first; arrows and type-select stay with NSBrowser.
@MainActor
final class FileBrowser: NSBrowser, FileActionsProviding {
    var onKey: ((NSEvent) -> Bool)?
    weak var fileActions: FileActions?
    /// Called whenever the browser changes size, so the preview column can fill what is left.
    var onResize: (() -> Void)?

    override func setFrameSize(_ newSize: NSSize) {
        super.setFrameSize(newSize)
        onResize?()
    }

    override func keyDown(with event: NSEvent) {
        if onKey?(event) == true { return }
        super.keyDown(with: event)
    }

    // Edit menu: Cut, Copy and Paste reach the folder view through the responder chain.
    @objc func copy(_ sender: Any?) { fileActions?.copySelection() }
    @objc func cut(_ sender: Any?) { fileActions?.cutSelection() }
    @objc func paste(_ sender: Any?) { fileActions?.pasteHere() }
}

/// The root of the preview column. NSBrowser sets only the width of a preview view controller's view and leaves its
/// height at 0, so the view takes its superview's height itself (and follows it when the browser resizes).
@MainActor
private final class PreviewRootView: NSView {
    private var observer: NSObjectProtocol?

    override func viewDidMoveToSuperview() {
        super.viewDidMoveToSuperview()
        if let observer { NotificationCenter.default.removeObserver(observer) }
        observer = nil
        guard let sup = superview else { return }
        sup.postsFrameChangedNotifications = true
        observer = NotificationCenter.default.addObserver(forName: NSView.frameDidChangeNotification, object: sup,
                                                          queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.fit() }
        }
        fit()
    }

    override func layout() {
        super.layout()
        fit()
    }

    private func fit() {
        guard let sup = superview else { return }
        let h = max(sup.bounds.height, 1)
        if abs(frame.height - h) > 0.5 { setFrameSize(NSSize(width: frame.width, height: h)) }
    }
}

/// The last column of a file, as in Finder: a large Quick Look preview (or the file's thumbnail icon), then the
/// name, kind and size, and a table of dates (created, modified, last opened) with a More… button. Created and
/// Last opened come from the file system and Spotlight, which can block, so they are read off the main thread
/// and shown as "--" until they arrive.
@MainActor
private final class FilePreviewController: NSViewController {
    private let item: FileItem
    var onMore: ((FileItem) -> Void)?
    /// True while this controller is still the browser's preview. Results that arrive after it was replaced are dropped.
    var isCurrent: () -> Bool = { true }
    private let createdValue = FilePreviewController.valueField("--")
    private let lastOpenedValue = FilePreviewController.valueField("--")

    init(item: FileItem) {
        self.item = item
        super.init(nibName: nil, bundle: nil)
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override func loadView() {
        let root = PreviewRootView(frame: NSRect(x: 0, y: 0, width: 240, height: 400))

        let thumb = NSImageView()
        thumb.imageScaling = .scaleProportionallyUpOrDown
        thumb.image = item.icon
        thumb.translatesAutoresizingMaskIntoConstraints = false
        thumb.setContentHuggingPriority(.defaultLow, for: .vertical)
        thumb.setContentCompressionResistancePriority(.defaultLow, for: .vertical)
        // Large Quick Look thumbnail, drawn inside a fixed-height frame so it can never be clipped by the stack.
        let wanted = NSSize(width: 480, height: 480)
        QLThumbnailGenerator.shared.generateBestRepresentation(
            for: QLThumbnailGenerator.Request(fileAt: item.url, size: wanted,
                                              scale: NSScreen.main?.backingScaleFactor ?? 2,
                                              representationTypes: .thumbnail)) { [weak self] rep, _ in
            guard let image = rep?.nsImage else { return }
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    guard let self, self.isCurrent() else { return }
                    thumb.image = image
                }
            }
        }

        let name = NSTextField(wrappingLabelWithString: item.name)
        name.font = .systemFont(ofSize: 15, weight: .semibold)
        name.alignment = .center
        name.maximumNumberOfLines = 3
        name.isSelectable = false
        let sub = NSTextField(labelWithString: item.isDir ? item.kind : "\(item.kind) - \(FileFormat.size(item))")
        sub.font = .systemFont(ofSize: 12)
        sub.textColor = .secondaryLabelColor
        sub.alignment = .center
        sub.lineBreakMode = .byTruncatingTail
        let header = NSStackView(views: [name, sub])
        header.orientation = .vertical
        header.alignment = .centerX
        header.spacing = 2

        let divider = NSBox()
        divider.boxType = .separator

        // One row per date: the label hugs its text and never gives way; the value takes the rest and truncates last.
        let rows = [
            FilePreviewController.infoRow(FilePreviewController.labelField("Created"), createdValue),
            FilePreviewController.infoRow(FilePreviewController.labelField("Modified"),
                                          FilePreviewController.valueField(FileFormat.date(item.mtime))),
            FilePreviewController.infoRow(FilePreviewController.labelField("Last opened"), lastOpenedValue),
        ]
        let info = NSStackView(views: rows)
        info.orientation = .vertical
        info.alignment = .leading
        info.spacing = 4
        info.distribution = .fill

        let more = NSButton(title: "More…", target: self, action: #selector(moreClicked(_:)))
        more.bezelStyle = .rounded
        more.controlSize = .small

        let stack = NSStackView(views: [thumb, header, divider, info, more])
        stack.orientation = .vertical
        stack.alignment = .centerX
        stack.spacing = 10
        stack.translatesAutoresizingMaskIntoConstraints = false
        root.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: root.topAnchor, constant: 34),
            stack.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 14),
            stack.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -14),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: root.bottomAnchor, constant: -14),
            thumb.widthAnchor.constraint(equalTo: stack.widthAnchor),
            thumb.heightAnchor.constraint(lessThanOrEqualToConstant: 320),
            thumb.heightAnchor.constraint(greaterThanOrEqualToConstant: 96),
            header.widthAnchor.constraint(equalTo: stack.widthAnchor),
            divider.widthAnchor.constraint(equalTo: stack.widthAnchor),
            info.widthAnchor.constraint(equalTo: stack.widthAnchor),
        ])
        NSLayoutConstraint.activate(rows.map { $0.widthAnchor.constraint(equalTo: info.widthAnchor) })
        view = root
        loadDates()
    }

    /// A row label: it keeps its full width, so "Modified" and "Last opened" are never cut.
    private static func labelField(_ text: String) -> NSTextField {
        let l = NSTextField(labelWithString: text)
        l.font = .systemFont(ofSize: 11)
        l.textColor = .secondaryLabelColor
        l.lineBreakMode = .byClipping
        l.setContentHuggingPriority(.required, for: .horizontal)
        l.setContentCompressionResistancePriority(.required, for: .horizontal)
        return l
    }

    /// One date row: the label, then the value, which takes the rest of the width.
    private static func infoRow(_ label: NSTextField, _ value: NSTextField) -> NSStackView {
        let row = NSStackView(views: [label, value])
        row.orientation = .horizontal
        row.alignment = .firstBaseline
        row.distribution = .fill
        row.spacing = 8
        return row
    }

    /// A row value: it gives way first, and truncates at the tail only when it must.
    private static func valueField(_ text: String) -> NSTextField {
        let v = NSTextField(labelWithString: text)
        v.font = .systemFont(ofSize: 11)
        v.lineBreakMode = .byTruncatingTail
        v.setContentHuggingPriority(.defaultLow, for: .horizontal)
        v.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        return v
    }

    /// Reads the creation date and the Spotlight "last opened" date off the main thread, then fills the two values.
    /// The fill is dropped if the browser has replaced this preview meanwhile.
    private func loadDates() {
        let url = item.url
        Task { [weak self] in
            let dates = await Task.detached(priority: .utility) { () -> (created: Date?, opened: Date?) in
                let created = (try? url.resourceValues(forKeys: [.creationDateKey]))?.creationDate
                var opened: Date?
                if let md = MDItemCreateWithURL(nil, url as CFURL) {
                    opened = MDItemCopyAttribute(md, kMDItemLastUsedDate) as? Date
                }
                return (created, opened)
            }.value
            guard let self, self.isCurrent() else { return }
            self.createdValue.stringValue = FileFormat.date(dates.created)
            self.lastOpenedValue.stringValue = FileFormat.date(dates.opened)
        }
    }

    @objc private func moreClicked(_ sender: Any?) { onMore?(item) }
}

@MainActor
final class ColumnViewController: NSViewController, NSBrowserDelegate, FolderHost {
    /// A folder opened in MySearch (double-click, ⌘↓, Open).
    var onNavigate: ((URL) -> Void)?
    /// The enclosing folder (⌘↑).
    var onEnclosingFolder: (() -> Void)?
    /// The folder whose contents the last column shows; the window's title and path bar follow it.
    var onFocusFolder: ((URL) -> Void)?
    /// The status line: "N items", "N of M selected".
    var onStatus: ((String) -> Void)?
    /// True while anything is loading, false once it has all arrived.
    var onBusy: ((Bool) -> Void)?

    let provider: ContentProvider
    private let snapshot: Bool
    private let browser = FileBrowser()
    private let actions = FileActions()

    /// The folder at the root of the browser, or nil before `load(folder:)`.
    private(set) var folder: URL?
    private(set) var showHidden = false
    private var rootNode: ItemNode?
    /// Bumped when the root folder or the hidden-file setting changes; older loads are dropped.
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
    private var observers: [NSObjectProtocol] = []
    /// The in-place filter (FolderFilter.swift). It narrows one column: the children of `filterNode`, which the column
    /// at `filterColumn` shows. `filterMemo` holds those children that match, until they are worked out again.
    private var filter: FolderFilter?
    private let filterBar = FolderFilterBar()
    private var filterNode: ItemNode?
    private var filterColumn = 0
    private var filterMemo: [ItemNode]?
    /// The preview column's controller, kept while the column shows it. NSBrowser's header does not say whether the
    /// browser keeps the controller from browser(_:previewViewControllerForLeafItem:), and NSControl's target is weak,
    /// so the More… button would lose its target if nothing else held the controller.
    private var previewController: NSViewController?
    /// The narrowest the preview column may get. Below that the browser scrolls instead, as Finder does.
    private static let minPreviewWidth: CGFloat = 260
    /// The normal width of each column the preview has widened. It comes back when that column stops being the
    /// preview, so a folder column later at the same depth is not left at the preview's width.
    private var normalWidths: [Int: CGFloat] = [:]
    private var previewWidthPending = false

    init(provider: ContentProvider, snapshot: Bool) {
        self.provider = provider
        self.snapshot = snapshot
        super.init(nibName: nil, bundle: nil)
        actions.onOpenFolder = { [weak self] url in self?.onNavigate?(url) }
        actions.host = self
        observers.append(NotificationCenter.default.addObserver(forName: FileOpsService.changed, object: nil, queue: .main) { [weak self] n in
            MainActor.assumeIsolated { self?.filesChanged(n) }
        })
    }

    deinit {
        for o in observers { NotificationCenter.default.removeObserver(o) }
        watcher?.cancel()           // its cancel handler closes the watched folder's file descriptor
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override func loadView() {
        browser.delegate = self
        browser.allowsMultipleSelection = true
        browser.allowsEmptySelection = true
        browser.hasHorizontalScroller = true
        browser.separatesColumns = true
        browser.columnResizingType = .userColumnResizing
        browser.setCellClass(IconBrowserCell.self)
        browser.setDefaultColumnWidth(200)
        browser.minColumnWidth = 140
        browser.takesTitleFromPreviousColumn = false
        browser.setDraggingSourceOperationMask([.copy, .move], forLocal: true)     // inside the app: move, or copy with ⌥
        browser.setDraggingSourceOperationMask(.copy, forLocal: false)
        browser.registerForDraggedTypes([.fileURL])
        browser.fileActions = actions
        browser.onResize = { [weak self] in self?.schedulePreviewWidth() }
        browser.target = self
        browser.action = #selector(selectionChanged(_:))
        browser.doubleAction = #selector(doubleClicked(_:))
        browser.onKey = { [weak self] event in self?.handleKey(event) ?? false }
        if !snapshot {
            browser.columnsAutosaveName = "MySearchColumnView"
        }
        let menu = NSMenu()
        menu.delegate = self
        browser.menu = menu
        filterBar.onChange = { [weak self] text in self?.setFilterText(text) }
        filterBar.onClose = { [weak self] in self?.closeFilter() }
        filterBar.onMoveIntoList = { [weak self] in self?.moveIntoList() }
        view = FolderFilterContainer(content: browser, bar: filterBar)
    }

    // MARK: public API

    /// The items selected in the browser, in column and row order.
    var selectedItems: [FileItem] {
        browser.selectionIndexPaths.compactMap { (browser.item(at: $0) as? ItemNode)?.item }
    }

    /// The first selected URL, if any. Used to keep the selection when the view changes.
    var selectedURL: URL? { selectedItems.first?.url }

    /// Shows `url` as the root of the browser. Columns are rebuilt, so this is also how a new folder opens.
    func load(folder url: URL) {
        let target = url.standardizedFileURL
        dropFilter()                                 // the columns are rebuilt, so the filter goes with them
        folder = target
        generation += 1
        rootNode = ItemNode(FileItem(url: target))
        watcher?.cancel(); watcher = nil; watchedNode = nil
        browser.loadColumnZero()
    }

    /// Shows or hides hidden files (⌘⇧.): the browser is rebuilt for the same folder.
    func setShowHidden(_ on: Bool) {
        guard on != showHidden else { return }
        showHidden = on
        if let folder { load(folder: folder) }
    }

    /// Opens every column on the way to `target` (inside the root folder) and selects it.
    func reveal(_ target: URL, completion: (() -> Void)? = nil) {
        guard let root = rootNode, let base = folder?.pathComponents else { return }
        let parts = Array(target.standardizedFileURL.pathComponents.dropFirst(base.count))
        let token = generation
        Task {
            var parent: ItemNode = root
            var path: [Int] = []
            for part in parts {
                if parent.children == nil {
                    guard await fill(parent, token: token) else { return }
                }
                let visible = shownChildren(of: parent)      // the browser's rows, which a filter may have cut down
                guard let index = visible.firstIndex(where: { $0.item.url.lastPathComponent == part }) else { break }
                let child = visible[index]
                path.append(index)
                parent = child
            }
            guard !path.isEmpty, token == generation else { return }
            browser.selectionIndexPaths = [IndexPath(indexes: path)]
            browser.scrollColumnToVisible(browser.selectedColumn)
            notifyFocus()
            updateStatus()
            completion?()
        }
    }

    // MARK: loading

    /// Reads the children of a folder node and redraws the column that shows them. Nodes that already exist are
    /// reused, so the browser's selection keeps pointing at live objects. Returns false when a newer load has
    /// replaced this one.
    private func fill(_ node: ItemNode, token: Int) async -> Bool {
        pending += 1
        defer { pending -= 1 }
        let items: [FileItem]
        do {
            items = try await provider.items(in: node.item.url, recursive: false, includeHidden: showHidden)
        } catch {
            items = []
        }
        guard token == generation else { return false }
        var byURL: [URL: ItemNode] = [:]
        for child in node.children ?? [] { byURL[child.item.url] = child }
        let children = items.map { item -> ItemNode in
            if let existing = byURL[item.url] {
                existing.item = item
                return existing
            }
            let child = ItemNode(item)
            child.depth = node.depth + 1
            return child
        }
        node.children = sortedByName(children)
        filterMemo = nil
        reloadColumns(showing: node)
        return true
    }

    /// Finder's column order: by name, with folders and files mixed.
    private func sortedByName(_ nodes: [ItemNode]) -> [ItemNode] {
        let order = sortedItems(nodes.map(\.item), by: .name, ascending: true, foldersFirst: false)
        let byURL = Dictionary(nodes.map { ($0.item.url, $0) }, uniquingKeysWith: { first, _ in first })
        return order.compactMap { byURL[$0.url] }
    }

    /// Loads a folder's children in the background, when the browser asks for them.
    private func loadChildren(of node: ItemNode) {
        let token = generation
        Task { _ = await fill(node, token: token) }
    }

    private func reloadColumns(showing node: ItemNode) {
        for column in 0...max(browser.lastColumn, 0) where (browser.parentForItems(inColumn: column) as? ItemNode) === node {
            browser.reloadColumn(column)
        }
        schedulePreviewWidth()
        updateStatus()
    }

    // MARK: NSBrowserDelegate (item based)

    func rootItem(for sender: NSBrowser) -> Any? {
        rootNode
    }

    func browser(_ sender: NSBrowser, numberOfChildrenOfItem item: Any?) -> Int {
        guard let node = item as? ItemNode, node.item.isDir else { return 0 }
        if node.children == nil {
            loadChildren(of: node)
            return 0
        }
        return shownChildren(of: node).count
    }

    func browser(_ sender: NSBrowser, child index: Int, ofItem item: Any?) -> Any {
        // numberOfChildrenOfItem reports the same shown children, so the index is in range
        shownChildren(of: item as! ItemNode)[index]
    }

    func browser(_ sender: NSBrowser, isLeafItem item: Any?) -> Bool {
        !((item as? ItemNode)?.item.isDir ?? false)
    }

    func browser(_ sender: NSBrowser, objectValueForItem item: Any?) -> Any? {
        (item as? ItemNode)?.item.name
    }

    func browser(_ sender: NSBrowser, willDisplayCell cell: Any, atRow row: Int, column: Int) {
        guard let cell = cell as? IconBrowserCell, let node = sender.item(atRow: row, inColumn: column) as? ItemNode else { return }
        // The icon is drawn by IconBrowserCell (NSBrowserCell draws no image next to the name).
        let icon = node.item.icon
        icon.size = NSSize(width: 16, height: 16)
        cell.icon = icon
        cell.tags = TagStore.dotsImage(for: node.item.tagColors)
        cell.isLeaf = !node.item.isDir
        cell.lineBreakMode = .byTruncatingMiddle
        if let filter, column == filterColumn {
            cell.attributedStringValue = filter.highlighted(node.item.name, font: cell.font)
        }
    }

    /// The preview column for a selected file.
    func browser(_ sender: NSBrowser, previewViewControllerForLeafItem item: Any) -> NSViewController? {
        guard let node = item as? ItemNode else { return nil }
        let controller = FilePreviewController(item: node.item)
        controller.onMore = { item in InfoPanelController.shared.show([item]) }
        controller.isCurrent = { [weak self, weak controller] in
            guard let self, let controller else { return false }
            return self.previewController === controller
        }
        previewController = controller
        return controller
    }

    /// The preview column takes the width the browser leaves it (see updatePreviewWidth), so a drag on its edge does
    /// nothing. A column that a folder now occupies, after the preview had widened it, gets its normal width back at
    /// once: NSBrowser's column configuration (autosaved per depth) may hold the preview's width for that depth.
    /// Every other column keeps the width NSBrowser suggests.
    func browser(_ sender: NSBrowser, shouldSizeColumn column: Int, forUserResize: Bool, toWidth suggestedWidth: CGFloat) -> CGFloat {
        guard column == previewColumn() else {
            if !forUserResize, let normal = normalWidths.removeValue(forKey: column) { return normal }
            return suggestedWidth
        }
        if normalWidths[column] == nil { normalWidths[column] = suggestedWidth }
        return previewFillWidth(column)
    }

    /// The last column changes when a file is selected or a folder is opened: the preview comes or goes.
    func browser(_ sender: NSBrowser, didChangeLastColumn oldLastColumn: Int, toColumn column: Int) {
        schedulePreviewWidth()
    }

    /// The user resized a column: the preview column gets what is left.
    func browserColumnConfigurationDidChange(_ notification: Notification) {
        schedulePreviewWidth()
    }

    // Drag out of a column (copy only: the file stays where it is).
    func browser(_ browser: NSBrowser, canDragRowsWith rowIndexes: IndexSet, inColumn column: Int, with event: NSEvent) -> Bool {
        true
    }

    func browser(_ browser: NSBrowser, pasteboardWriterForRow row: Int, inColumn column: Int) -> NSPasteboardWriting? {
        (browser.item(atRow: row, inColumn: column) as? ItemNode).map { $0.item.url as NSURL }
    }

    /// Where a drop lands: the folder row it is on, otherwise the folder the column shows.
    private func dropDestination(row: Int, column: Int, on: Bool) -> URL? {
        if on, row >= 0, let node = browser.item(atRow: row, inColumn: column) as? ItemNode, node.item.isDir {
            return node.item.url
        }
        return (browser.parentForItems(inColumn: column) as? ItemNode)?.item.url
    }

    func browser(_ browser: NSBrowser, validateDrop info: NSDraggingInfo, proposedRow row: UnsafeMutablePointer<Int>,
                 column: UnsafeMutablePointer<Int>, dropOperation: UnsafeMutablePointer<NSBrowser.DropOperation>) -> NSDragOperation {
        guard let destination = dropDestination(row: row.pointee, column: column.pointee, on: dropOperation.pointee == .on) else { return [] }
        return DropPlan.operation(for: DropPlan.urls(info), into: destination)
    }

    func browser(_ browser: NSBrowser, acceptDrop info: NSDraggingInfo, atRow row: Int, column: Int,
                 dropOperation: NSBrowser.DropOperation) -> Bool {
        guard let destination = dropDestination(row: row, column: column, on: dropOperation == .on) else { return false }
        let urls = DropPlan.urls(info)
        let operation = DropPlan.operation(for: urls, into: destination)
        guard !operation.isEmpty else { return false }
        actions.drop(urls, into: destination, operation: operation)
        return true
    }

    // MARK: actions

    @objc private func selectionChanged(_ sender: Any?) {
        notifyFocus()
        updateStatus()
    }

    /// The folder the deepest column shows is the folder the user is in: the title, path bar and live refresh follow it.
    private func notifyFocus() {
        // a selection in a column before the filtered one has moved to another folder: the filter ends there
        if filter != nil, browser.selectedColumn < filterColumn {
            dropFilter()
            if filterColumn <= browser.lastColumn { browser.reloadColumn(filterColumn) }
        }
        schedulePreviewWidth()
        guard let parent = focusedFolderNode else { return }
        onFocusFolder?(parent.item.url)
        watch(parent)
    }

    // MARK: the preview column's width

    /// The preview column while a file is selected: NSBrowser's parent of the last column is then the file itself.
    /// Nil while a folder is shown.
    private func previewColumn() -> Int? {
        let last = browser.lastColumn
        guard last >= 0, let node = browser.parentForItems(inColumn: last) as? ItemNode, !node.item.isDir else { return nil }
        return last
    }

    /// The folder the deepest column shows. With a file selected the deepest column is the preview, so the folder is
    /// the one that holds the file.
    private var focusedFolderNode: ItemNode? {
        let last = browser.lastColumn
        guard let node = browser.parentForItems(inColumn: last) as? ItemNode else { return nil }
        if node.item.isDir { return node }
        guard last > 0 else { return nil }
        return browser.parentForItems(inColumn: last - 1) as? ItemNode
    }

    /// The width the preview takes: what is left of the visible width after the columns before it, and never less
    /// than minPreviewWidth. Columns scrolled off the left edge do not count. The frames are used, not the widths,
    /// because NSBrowser puts a separator between columns (two points) that the widths leave out.
    private func previewFillWidth(_ column: Int) -> CGFloat {
        let first = min(max(browser.firstVisibleColumn, 0), column)
        let start = browser.frame(ofColumn: column).minX - browser.frame(ofColumn: first).minX
        return max(ColumnViewController.minPreviewWidth, browser.bounds.width - start)
    }

    /// Defers updatePreviewWidth to the next turn of the run loop, after NSBrowser has laid out the columns. Calls made
    /// in one turn are merged into one update.
    private func schedulePreviewWidth() {
        guard !previewWidthPending else { return }
        previewWidthPending = true
        DispatchQueue.main.async { [weak self] in
            MainActor.assumeIsolated {
                guard let self else { return }
                self.previewWidthPending = false
                self.updatePreviewWidth()
            }
        }
    }

    /// Makes the preview column fill the visible width, so NSBrowser draws no empty columns after it. A column that the
    /// preview has left gets its normal width back. Folder columns are not touched.
    private func updatePreviewWidth() {
        guard isViewLoaded, folder != nil else { return }
        let preview = previewColumn()
        for column in normalWidths.keys.sorted() where column != preview {
            if let width = normalWidths.removeValue(forKey: column) { browser.setWidth(width, ofColumn: column) }
        }
        guard let preview else {
            previewController = nil
            return
        }
        if normalWidths[preview] == nil {
            normalWidths[preview] = browser.width(ofColumn: preview)
            browser.scrollColumnToVisible(preview)      // a newly shown preview is on screen
        }
        let fill = previewFillWidth(preview)
        if abs(browser.width(ofColumn: preview) - fill) > 0.5 { browser.setWidth(fill, ofColumn: preview) }
    }

    // MARK: live updates

    private var watcher: DispatchSourceFileSystemObject?
    private var watchedNode: ItemNode?
    private var refreshTask: Task<Void, Never>?

    /// Watches the folder the deepest column shows; when its entries change the column reloads (debounced).
    private func watch(_ node: ItemNode) {
        guard !snapshot, watchedNode !== node else { return }
        watcher?.cancel()
        watcher = nil
        watchedNode = node
        let fd = open(node.item.url.path, O_EVTONLY)
        guard fd >= 0 else { return }
        let source = DispatchSource.makeFileSystemObjectSource(fileDescriptor: fd,
                                                               eventMask: [.write, .rename, .delete, .extend], queue: .main)
        source.setEventHandler { [weak self, weak node] in
            Task { @MainActor in
                guard let self, let node else { return }
                self.scheduleRefresh(node)
            }
        }
        source.setCancelHandler { close(fd) }
        source.resume()
        watcher = source
    }

    private func scheduleRefresh(_ node: ItemNode) {
        refreshTask?.cancel()
        let token = generation
        refreshTask = Task {
            try? await Task.sleep(nanoseconds: 300_000_000)
            guard !Task.isCancelled else { return }
            _ = await fill(node, token: token)
        }
    }

    @objc private func doubleClicked(_ sender: Any?) {
        actions.open(selectedItems)
    }

    private func updateStatus() {
        if let filter, let node = filterNode {
            onStatus?(filter.statusText(matching: shownChildren(of: node).count, of: (node.children ?? []).count))
            return
        }
        let column = browser.lastColumn
        let parent = browser.parentForItems(inColumn: column) as? ItemNode
        let total = parent?.children?.count ?? 0
        let paths = browser.selectionIndexPaths.filter { $0.count > 0 }
        let inColumn = paths.filter { $0.count == column + 1 }.compactMap { (browser.item(at: $0) as? ItemNode)?.item }
        if !inColumn.isEmpty {
            onStatus?(FolderStatus.text(total: total, selected: inColumn, folder: parent?.item.url))
        } else if !paths.isEmpty {
            onStatus?(FolderStatus.text(total: nil, selected: selectedItems, folder: parent?.item.url))
        } else {
            onStatus?(FolderStatus.text(total: total, selected: [], folder: parent?.item.url))
        }
    }

    // MARK: the in-place filter (FolderFilter.swift)

    /// The children of `node` that the browser shows: all of them, or with a filter and `node` the filtered folder, the
    /// ones whose names match.
    private func shownChildren(of node: ItemNode) -> [ItemNode] {
        let all = node.children ?? []
        guard let filter, node === filterNode else { return all }
        if let memo = filterMemo { return memo }
        let kept = all.filter { filter.matches($0.item.name) }
        filterMemo = kept
        return kept
    }

    /// Filter This Folder (the command palette): the same filter as the "/" key.
    func openFilter() { beginFilter() }

    /// "/" or ⌘⌥F: the filter narrows the column with the selection (or the first column) and its folder.
    private func beginFilter() {
        guard folder != nil else { return }
        if filterBar.isOpen {
            filterBar.open()
            return
        }
        let column = max(browser.selectedColumn, 0)
        guard let node = browser.parentForItems(inColumn: column) as? ItemNode else { return }
        filterColumn = column
        filterNode = node
        filterMemo = nil
        filterBar.open()
    }

    private func setFilterText(_ text: String) {
        filter = FolderFilter(text: text)
        filterMemo = nil
        refilterColumn()
    }

    /// Re-shows the filtered column. The selection in it and in every column after it is dropped, because the rows moved.
    /// `index` (an index among all the children, not the shown ones) selects that row again.
    private func refilterColumn(selecting index: Int? = nil) {
        let prefix = browser.selectionIndexPaths.first.map { Array($0.prefix(filterColumn)) } ?? []
        let path = index.map { prefix + [$0] } ?? prefix
        browser.selectionIndexPaths = path.isEmpty ? [] : [IndexPath(indexes: path)]
        if filterColumn <= browser.lastColumn { browser.reloadColumn(filterColumn) }
        schedulePreviewWidth()
        updateStatus()
    }

    /// Clears the filter state without reloading (the caller reloads the column).
    private func dropFilter() {
        filterBar.close()
        filter = nil
        filterNode = nil
        filterMemo = nil
    }

    /// Esc: the filter ends and the row that was chosen in the filtered column stays chosen.
    private func closeFilter() {
        guard filterBar.isOpen || filter != nil else { return }
        let parent = filterNode
        let row = browser.selectedRow(inColumn: filterColumn)
        let chosen = row >= 0 ? browser.item(atRow: row, inColumn: filterColumn) as? ItemNode : nil
        dropFilter()
        let index = chosen.flatMap { node in parent?.children?.firstIndex(where: { $0 === node }) }
        refilterColumn(selecting: index)
        view.window?.makeFirstResponder(browser)
    }

    /// ↓ or Return in the filter field: the keyboard goes to the browser, and the first row of the filtered column is
    /// selected if that column has no selection of its own.
    private func moveIntoList() {
        view.window?.makeFirstResponder(browser)
        let rows = (browser.parentForItems(inColumn: filterColumn) as? ItemNode).map { shownChildren(of: $0).count } ?? 0
        if browser.selectedRow(inColumn: filterColumn) < 0, filterColumn <= browser.lastColumn, rows > 0 {
            browser.selectRow(0, inColumn: filterColumn)
        }
    }

    // MARK: file operations

    var hostWindow: NSWindow? { isViewLoaded ? view.window : nil }
    /// New items go into the folder the last column shows (the selected folder, or the folder of the selected file).
    var hostFolder: URL? { focusedFolderNode?.item.url }
    var hostSelection: [FileItem] { selectedItems }
    func hostBeginRename(_ item: FileItem) { actions.promptRename(item) }
    func hostStatus(_ text: String) { onStatus?(text) }

    /// A file operation finished (anywhere in the app): reload every column, select the new item and, for a new
    /// folder, ask for its name.
    private func filesChanged(_ n: Notification) {
        guard isViewLoaded, view.window != nil, folder != nil else { return }
        let select = (n.userInfo?["select"] as? [URL]) ?? []
        let rename = (n.userInfo?["rename"] as? Bool) ?? false
        let token = generation
        let parents = (0...max(browser.lastColumn, 0)).compactMap { browser.parentForItems(inColumn: $0) as? ItemNode }
        Task { @MainActor in
            for parent in parents { _ = await fill(parent, token: token) }
            guard let first = select.first else { return }
            reveal(first) { [weak self] in
                if rename, select.count == 1 { self?.actions.promptRename(FileItem(url: first)) }
            }
        }
    }

    /// The keys of the Finder's column view that the browser does not already handle.
    private func handleKey(_ event: NSEvent) -> Bool {
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        let command = flags.contains(.command)
        let option = flags.contains(.option)
        let shift = flags.contains(.shift)
        // "/" is ⇧7 on a German keyboard, so shift is allowed; ⌘⌥F opens the filter too
        if flags.subtracting([.shift, .capsLock, .numericPad]).isEmpty, event.characters == "/" {
            beginFilter()
            return true
        }
        switch Int(event.keyCode) {
        case kVK_ANSI_F where command && option:
            beginFilter()
        case kVK_Escape where filterBar.isOpen:
            closeFilter()
        case kVK_Space where flags.isEmpty:
            QuickLookCoordinator.shared.toggle(selectedItems.map(\.url))
        case kVK_DownArrow where command:
            actions.open(selectedItems)
        case kVK_UpArrow where command:
            onEnclosingFolder?()
        case kVK_ANSI_C where command:
            if option { actions.copyPaths(selectedItems.map(\.url)) } else { actions.copyFiles(selectedItems.map(\.url)) }
        case kVK_ANSI_X where command && !option:
            actions.cutSelection()
        case kVK_ANSI_V where command && !option:
            actions.pasteHere()
        case kVK_Return where flags.isEmpty, kVK_ANSI_KeypadEnter where flags.isEmpty:
            let items = selectedItems
            guard items.count == 1 else { return false }
            actions.promptRename(items[0])
        case kVK_ANSI_Period where command && shift:
            setShowHidden(!showHidden)
        default:
            return false
        }
        return true
    }
}

extension ColumnViewController: NSMenuDelegate {
    /// Right-click: the clicked cell joins the selection if it was not selected, then the menu is filled.
    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        let column = browser.clickedColumn
        let row = browser.clickedRow
        guard column >= 0, row >= 0 else {
            actions.fillContextMenu(menu, for: [])           // the background of a column: New Folder, Paste
            return
        }
        let path = IndexPath(indexes: (0...column).map { c in c == column ? row : browser.selectedRow(inColumn: c) })
        if !browser.selectionIndexPaths.contains(path) {
            browser.selectionIndexPaths = [path]
        }
        actions.fillContextMenu(menu, for: selectedItems)
    }
}
