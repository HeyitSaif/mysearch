// The Finder's icon view: a grid of items with Quick Look thumbnails (QLThumbnailGenerator) that fall back to the
// file icon while a thumbnail is being made. Opening, Quick Look, the context menu and the file operations are
// shared with the list and column views (FolderViewCommon, FileOps). Return (or a click on the selected name)
// renames in place; dragging an item onto a folder moves it, with ⌥ it copies; dragging out to other apps copies.

import Cocoa
import Carbon.HIToolbox
import QuickLookThumbnailing

@MainActor
final class IconCollectionView: NSCollectionView, FileActionsProviding {
    var onKey: ((NSEvent) -> Bool)?
    var onContextClick: ((NSEvent) -> Void)?
    weak var fileActions: FileActions?

    // Edit menu: Cut, Copy and Paste reach the folder view through the responder chain.
    @objc func copy(_ sender: Any?) { fileActions?.copySelection() }
    @objc func cut(_ sender: Any?) { fileActions?.cutSelection() }
    @objc func paste(_ sender: Any?) { fileActions?.pasteHere() }

    override func keyDown(with event: NSEvent) {
        if onKey?(event) == true { return }
        super.keyDown(with: event)
    }

    override func menu(for event: NSEvent) -> NSMenu? {
        onContextClick?(event)
        return super.menu(for: event)
    }
}

@MainActor
final class IconItemView: NSView {
    let imageView = NSImageView()
    let label = NSTextField(wrappingLabelWithString: "")
    /// The item's tag dots, at the bottom right of the icon.
    let tagView = NSImageView()
    var onDoubleClick: (() -> Void)?
    /// A click on the name of the one selected item (after the double-click interval): starts the rename.
    var onLabelClick: (() -> Void)?
    var selected = false { didSet { needsDisplay = true } }
    private var pendingRename: Task<Void, Never>?

    override init(frame: NSRect) {
        super.init(frame: frame)
        imageView.imageScaling = .scaleProportionallyUpOrDown
        imageView.translatesAutoresizingMaskIntoConstraints = false
        label.font = .systemFont(ofSize: 11)
        label.alignment = .center
        label.maximumNumberOfLines = 2
        label.lineBreakMode = .byTruncatingMiddle
        label.cell?.truncatesLastVisibleLine = true
        label.isSelectable = false
        label.translatesAutoresizingMaskIntoConstraints = false
        addSubview(imageView)
        addSubview(label)
        tagView.translatesAutoresizingMaskIntoConstraints = false
        tagView.imageScaling = .scaleNone
        addSubview(tagView)
        NSLayoutConstraint.activate([
            tagView.trailingAnchor.constraint(equalTo: imageView.trailingAnchor, constant: 6),
            tagView.bottomAnchor.constraint(equalTo: imageView.bottomAnchor, constant: 2),
            imageView.topAnchor.constraint(equalTo: topAnchor, constant: 6),
            imageView.centerXAnchor.constraint(equalTo: centerXAnchor),
            imageView.widthAnchor.constraint(equalToConstant: 64),
            imageView.heightAnchor.constraint(equalToConstant: 64),
            label.topAnchor.constraint(equalTo: imageView.bottomAnchor, constant: 4),
            label.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 4),
            label.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -4),
        ])
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override func mouseDown(with event: NSEvent) {
        pendingRename?.cancel()
        if event.clickCount >= 2 {
            onDoubleClick?()
            return
        }
        let wasSelected = selected
        let onName = label.frame.contains(convert(event.locationInWindow, from: nil))
        let plain = event.modifierFlags.intersection([.command, .shift, .option, .control]).isEmpty
        super.mouseDown(with: event)                      // selects, or tracks a drag to its end
        guard wasSelected, onName, plain, event.clickCount == 1, let up = window?.currentEvent, up.type == .leftMouseUp,
              abs(up.locationInWindow.x - event.locationInWindow.x) < 3, abs(up.locationInWindow.y - event.locationInWindow.y) < 3
        else { return }
        let delay = UInt64((NSEvent.doubleClickInterval + 0.15) * 1_000_000_000)
        pendingRename = Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: delay)
            guard !Task.isCancelled, let self, self.selected else { return }
            self.onLabelClick?()
        }
    }

    override func draw(_ dirtyRect: NSRect) {
        guard selected else { return }
        NSColor.unemphasizedSelectedContentBackgroundColor.setFill()
        NSBezierPath(roundedRect: imageView.frame.insetBy(dx: -4, dy: -3), xRadius: 6, yRadius: 6).fill()
        NSColor.selectedContentBackgroundColor.setFill()
        let labelBox = label.frame.insetBy(dx: -2, dy: 0)
        NSBezierPath(roundedRect: labelBox, xRadius: 4, yRadius: 4).fill()
    }
}

@MainActor
final class IconItem: NSCollectionViewItem {
    var node: ItemNode?
    var thumbnailRequest: QLThumbnailGenerator.Request?

    override func loadView() { view = IconItemView(frame: NSRect(x: 0, y: 0, width: 104, height: 104)) }

    var itemView: IconItemView { view as! IconItemView }

    override var isSelected: Bool {
        didSet {
            itemView.selected = isSelected
            itemView.label.textColor = isSelected ? .alternateSelectedControlTextColor : .labelColor
        }
    }
}

@MainActor
final class IconViewController: NSViewController, NSCollectionViewDataSource, NSCollectionViewDelegate,
                                NSCollectionViewDelegateFlowLayout, NSMenuDelegate, NSTextFieldDelegate, FolderHost {
    var onNavigate: ((URL) -> Void)?
    var onEnclosingFolder: (() -> Void)?
    var onStatus: ((String) -> Void)?
    var onBusy: ((Bool) -> Void)?

    let provider: ContentProvider
    private let snapshot: Bool
    private let scroll = NSScrollView()
    private let collection = IconCollectionView()
    private let actions = FileActions()
    private(set) var folder: URL?
    private(set) var showHidden = false
    /// Every loaded item. `nodes` is the part of it the view shows: all of them, or the matches of the filter.
    private var allNodes: [ItemNode] = [] {
        didSet { nodes = visibleNodes() }
    }
    private var nodes: [ItemNode] = []
    /// The in-place filter (FolderFilter.swift). Nil when no word is typed.
    private var filter: FolderFilter?
    private let filterBar = FolderFilterBar()
    private var generation = 0
    private var pending = 0 { didSet { let b = pending > 0; if b != lastBusy { lastBusy = b; onBusy?(b) } } }
    private var lastBusy = false
    private var watcher: DispatchSourceFileSystemObject?
    private var refreshTask: Task<Void, Never>?
    private var thumbnails: [URL: NSImage] = [:]
    private var thumbnailsInFlight = 0
    /// True while thumbnails are still being generated (the snapshot waits on this).
    var isLoadingThumbnails: Bool { thumbnailsInFlight > 0 }
    private static let itemIdentifier = NSUserInterfaceItemIdentifier("icon")
    /// The item whose name is being edited in place. Reloads wait until it ends, so they cannot cancel the edit.
    private var renamingURL: URL?
    private var renameCancelled = false
    private var observers: [NSObjectProtocol] = []

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
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override func loadView() {
        let layout = NSCollectionViewFlowLayout()
        layout.itemSize = NSSize(width: 104, height: 104)
        layout.minimumInteritemSpacing = 6
        layout.minimumLineSpacing = 10
        layout.sectionInset = NSEdgeInsets(top: 12, left: 14, bottom: 12, right: 14)
        collection.collectionViewLayout = layout
        collection.dataSource = self
        collection.delegate = self
        collection.isSelectable = true
        collection.allowsMultipleSelection = true
        collection.backgroundColors = [.clear]
        collection.register(IconItem.self, forItemWithIdentifier: Self.itemIdentifier)
        collection.setDraggingSourceOperationMask([.copy, .move], forLocal: true)     // inside the app: move, or copy with ⌥
        collection.setDraggingSourceOperationMask(.copy, forLocal: false)
        collection.registerForDraggedTypes([.fileURL])
        collection.fileActions = actions
        collection.onKey = { [weak self] event in self?.handleKey(event) ?? false }
        let menu = NSMenu()
        menu.delegate = self
        collection.menu = menu
        collection.onContextClick = { [weak self] event in self?.contextClick(event) }
        scroll.documentView = collection
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false
        filterBar.onChange = { [weak self] text in self?.setFilterText(text) }
        filterBar.onClose = { [weak self] in self?.closeFilter() }
        filterBar.onMoveIntoList = { [weak self] in self?.moveIntoList() }
        view = FolderFilterContainer(content: scroll, bar: filterBar)
    }

    // MARK: public API

    var selectedItems: [FileItem] {
        collection.selectionIndexPaths.sorted().compactMap { $0.item < nodes.count ? nodes[$0.item].item : nil }
    }

    func load(folder url: URL) {
        let target = url.standardizedFileURL
        if target != folder { dropFilter() }        // the filter belongs to one folder
        folder = target
        generation += 1
        allNodes = []
        collection.reloadData()
        watch(target)
        fill(keepSelection: false)
    }

    func refresh() {
        guard renamingURL == nil else { return }
        fill(keepSelection: true)
    }

    /// Filter This Folder (the command palette): opens the filter bar, as the "/" key does.
    func openFilter() { filterBar.open() }

    func setShowHidden(_ on: Bool) {
        guard on != showHidden else { return }
        showHidden = on
        fill(keepSelection: true)
    }

    /// Selects `target` (a direct child of the folder) and scrolls it into view.
    func reveal(_ target: URL) {
        let token = generation
        Task {
            for _ in 0..<100 where nodes.isEmpty && token == generation { try? await Task.sleep(nanoseconds: 30_000_000) }
            guard token == generation,
                  let index = nodes.firstIndex(where: { $0.item.url == target.standardizedFileURL }) else { return }
            let path = IndexPath(item: index, section: 0)
            collection.selectionIndexPaths = [path]
            collection.scrollToItems(at: [path], scrollPosition: .centeredVertically)
            updateStatus()
            view.window?.makeFirstResponder(collection)
        }
    }

    // MARK: loading

    private func fill(keepSelection: Bool) {
        guard let url = folder else { return }
        let token = generation
        let kept = keepSelection ? Set(selectedItems.map(\.url)) : []
        pending += 1
        Task {
            defer { pending -= 1 }
            let items = (try? await provider.items(in: url, recursive: false, includeHidden: showHidden)) ?? []
            guard token == generation else { return }
            allNodes = sortedItems(items, by: .name, ascending: true, foldersFirst: false).map(ItemNode.init)
            collection.reloadData()
            let paths = nodes.enumerated().filter { kept.contains($0.element.item.url) }.map { IndexPath(item: $0.offset, section: 0) }
            collection.selectionIndexPaths = Set(paths)
            updateStatus()
        }
    }

    private func updateStatus() {
        if let filter {
            // every shown item is a match here: the icon view has no folders kept for their contents
            onStatus?(filter.statusText(matching: nodes.count, of: allNodes.count))
            return
        }
        onStatus?(FolderStatus.text(total: nodes.count, selected: selectedItems, folder: folder))
    }

    // MARK: the in-place filter (FolderFilter.swift)

    private func visibleNodes() -> [ItemNode] {
        guard let filter else { return allNodes }
        return allNodes.filter { filter.matches($0.item.name) }
    }

    private func setFilterText(_ text: String) {
        filter = FolderFilter(text: text)
        applyFilter()
    }

    /// Re-shows the items with the filter applied, keeping the selection of every item that is still shown.
    private func applyFilter() {
        let kept = Set(selectedItems.map(\.url))
        nodes = visibleNodes()
        collection.reloadData()
        let paths = nodes.enumerated().filter { kept.contains($0.element.item.url) }.map { IndexPath(item: $0.offset, section: 0) }
        collection.selectionIndexPaths = Set(paths)
        updateStatus()
    }

    /// Clears the filter without reloading (the caller reloads the items).
    private func dropFilter() {
        filterBar.close()
        filter = nil
    }

    private func closeFilter() {
        guard filterBar.isOpen || filter != nil else { return }
        dropFilter()
        applyFilter()
        view.window?.makeFirstResponder(collection)
    }

    /// ↓ or Return in the filter field: the keyboard goes to the items, and the first one is selected if none is.
    private func moveIntoList() {
        view.window?.makeFirstResponder(collection)
        if collection.selectionIndexPaths.isEmpty, !nodes.isEmpty {
            collection.selectionIndexPaths = [IndexPath(item: 0, section: 0)]
        }
    }

    // MARK: data source

    func collectionView(_ collectionView: NSCollectionView, numberOfItemsInSection section: Int) -> Int { nodes.count }

    func collectionView(_ collectionView: NSCollectionView, itemForRepresentedObjectAt indexPath: IndexPath) -> NSCollectionViewItem {
        let item = collectionView.makeItem(withIdentifier: Self.itemIdentifier, for: indexPath) as! IconItem
        let node = nodes[indexPath.item]
        item.node = node
        if let filter {
            item.itemView.label.attributedStringValue = filter.highlighted(node.item.name, font: item.itemView.label.font, alignment: .center)
        } else {
            item.itemView.label.stringValue = node.item.name
        }
        item.itemView.onDoubleClick = { [weak self] in self?.actions.open(self?.selectedItems ?? []) }
        item.itemView.onLabelClick = { [weak self] in
            guard let self, self.selectedItems.count == 1 else { return }
            self.beginRename(node.item.url)
        }
        item.itemView.tagView.image = TagStore.dotsImage(for: node.item.tagColors)
        item.itemView.selected = item.isSelected
        let url = node.item.url
        if let cached = thumbnails[url] {
            item.itemView.imageView.image = cached
        } else {
            item.itemView.imageView.image = node.item.icon
            requestThumbnail(for: url, into: item)
        }
        return item
    }

    private func requestThumbnail(for url: URL, into item: IconItem) {
        let request = QLThumbnailGenerator.Request(fileAt: url, size: NSSize(width: 128, height: 128),
                                                   scale: NSScreen.main?.backingScaleFactor ?? 2,
                                                   representationTypes: .thumbnail)
        item.thumbnailRequest = request
        thumbnailsInFlight += 1
        QLThumbnailGenerator.shared.generateBestRepresentation(for: request) { [weak self, weak item] rep, _ in
            let image = rep?.nsImage
            DispatchQueue.main.async {
                guard let self else { return }
                self.thumbnailsInFlight -= 1
                guard let image else { return }
                self.thumbnails[url] = image
                if let item, item.node?.item.url == url { item.itemView.imageView.image = image }
            }
        }
    }

    func collectionView(_ collectionView: NSCollectionView, didSelectItemsAt indexPaths: Set<IndexPath>) { updateStatus() }
    func collectionView(_ collectionView: NSCollectionView, didDeselectItemsAt indexPaths: Set<IndexPath>) { updateStatus() }

    func collectionView(_ collectionView: NSCollectionView, pasteboardWriterForItemAt indexPath: IndexPath) -> NSPasteboardWriting? {
        nodes[indexPath.item].item.url as NSURL
    }

    func collectionView(_ collectionView: NSCollectionView, canDragItemsAt indexPaths: Set<IndexPath>, with event: NSEvent) -> Bool { true }

    // MARK: keys and menu

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
        case kVK_ANSI_F where command && option: filterBar.open()
        case kVK_Escape where filterBar.isOpen: closeFilter()
        case kVK_Space where flags.isEmpty: QuickLookCoordinator.shared.toggle(selectedItems.map(\.url))
        case kVK_DownArrow where command: actions.open(selectedItems)
        case kVK_UpArrow where command: onEnclosingFolder?()
        case kVK_ANSI_C where command:
            if option { actions.copyPaths(selectedItems.map(\.url)) } else { actions.copyFiles(selectedItems.map(\.url)) }
        case kVK_ANSI_Period where command && shift: setShowHidden(!showHidden)
        case kVK_ANSI_X where command && !option: actions.cutSelection()
        case kVK_ANSI_V where command && !option: actions.pasteHere()
        case kVK_Return where flags.isEmpty, kVK_ANSI_KeypadEnter where flags.isEmpty:
            guard let only = selectedItems.first, selectedItems.count == 1 else { return false }
            beginRename(only.url)
        default: return false
        }
        return true
    }

    private func contextClick(_ event: NSEvent) {
        let point = collection.convert(event.locationInWindow, from: nil)
        if let path = collection.indexPathForItem(at: point) {
            if !collection.selectionIndexPaths.contains(path) { collection.selectionIndexPaths = [path] }
        } else {
            collection.selectionIndexPaths = []              // a right-click on the background: the folder's own menu
        }
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        actions.fillContextMenu(menu, for: selectedItems)
    }

    // MARK: file operations: folder host, refresh after a change, inline rename, drag and drop

    var hostWindow: NSWindow? { isViewLoaded ? view.window : nil }
    var hostFolder: URL? { folder }
    var hostSelection: [FileItem] { selectedItems }
    func hostBeginRename(_ item: FileItem) { beginRename(item.url) }
    func hostStatus(_ text: String) { onStatus?(text) }

    private func index(of url: URL) -> Int? {
        let wanted = url.standardizedFileURL.path
        return nodes.firstIndex { $0.item.url.standardizedFileURL.path == wanted }
    }

    /// A file operation finished (anywhere in the app): reload, select the new items and, for a new folder, rename it.
    private func filesChanged(_ n: Notification) {
        guard isViewLoaded, view.window != nil, let url = folder, renamingURL == nil else { return }
        let select = (n.userInfo?["select"] as? [URL]) ?? []
        let rename = (n.userInfo?["rename"] as? Bool) ?? false
        let token = generation
        pending += 1
        Task { @MainActor in
            defer { pending -= 1 }
            let items = (try? await provider.items(in: url, recursive: false, includeHidden: showHidden)) ?? []
            guard token == generation else { return }
            allNodes = sortedItems(items, by: .name, ascending: true, foldersFirst: false).map(ItemNode.init)
            collection.reloadData()
            let paths = select.compactMap { index(of: $0) }.map { IndexPath(item: $0, section: 0) }
            collection.selectionIndexPaths = Set(paths)
            if let first = paths.first { collection.scrollToItems(at: [first], scrollPosition: .nearestVerticalEdge) }
            updateStatus()
            if rename, select.count == 1 { beginRename(select[0]) }
        }
    }

    private func beginRename(_ url: URL) {
        guard renamingURL == nil, let index = index(of: url) else { return }
        let path = IndexPath(item: index, section: 0)
        collection.selectionIndexPaths = [path]
        collection.scrollToItems(at: [path], scrollPosition: .nearestVerticalEdge)
        collection.layoutSubtreeIfNeeded()
        guard let item = collection.item(at: path) as? IconItem else { return }
        let label = item.itemView.label
        renamingURL = nodes[index].item.url
        renameCancelled = false
        let name = nodes[index].item.url.lastPathComponent
        label.stringValue = name
        label.delegate = self
        label.isEditable = true
        label.isSelectable = true
        label.drawsBackground = true
        label.backgroundColor = .textBackgroundColor
        label.textColor = .labelColor
        view.window?.makeFirstResponder(label)
        let isDir = nodes[index].item.isDir
        let baseLength = isDir ? (name as NSString).length : ((name as NSString).deletingPathExtension as NSString).length
        label.currentEditor()?.selectedRange = NSRange(location: 0, length: baseLength)
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
        view.window?.makeFirstResponder(collection)
        if cancelled {
            fill(keepSelection: true)
            return
        }
        actions.rename(url, to: newName)                 // reports a refusal, then reloads and selects the item
    }

    // drag and drop: an item onto a folder moves it, with ⌥ it copies

    private func dropDestination(_ path: IndexPath, onItem: Bool) -> URL? {
        if onItem, path.item < nodes.count, nodes[path.item].item.isDir { return nodes[path.item].item.url }
        return folder
    }

    func collectionView(_ collectionView: NSCollectionView, validateDrop draggingInfo: NSDraggingInfo,
                        proposedIndexPath proposedDropIndexPath: AutoreleasingUnsafeMutablePointer<NSIndexPath>,
                        dropOperation proposedDropOperation: UnsafeMutablePointer<NSCollectionView.DropOperation>) -> NSDragOperation {
        let path = proposedDropIndexPath.pointee as IndexPath
        guard let destination = dropDestination(path, onItem: proposedDropOperation.pointee == .on) else { return [] }
        let operation = DropPlan.operation(for: DropPlan.urls(draggingInfo), into: destination)
        guard !operation.isEmpty else { return [] }
        if destination == folder, proposedDropOperation.pointee == .on {
            // the pointer is on a file, not a folder: the drop goes to the whole folder
            proposedDropIndexPath.pointee = NSIndexPath(forItem: nodes.count, inSection: 0)
            proposedDropOperation.pointee = .before
        }
        return operation
    }

    func collectionView(_ collectionView: NSCollectionView, acceptDrop draggingInfo: NSDraggingInfo,
                        indexPath: IndexPath, dropOperation: NSCollectionView.DropOperation) -> Bool {
        guard let destination = dropDestination(indexPath, onItem: dropOperation == .on) else { return false }
        let urls = DropPlan.urls(draggingInfo)
        let operation = DropPlan.operation(for: urls, into: destination)
        guard !operation.isEmpty else { return false }
        actions.drop(urls, into: destination, operation: operation)
        return true
    }

    // MARK: live updates

    private func watch(_ url: URL) {
        watcher?.cancel(); watcher = nil
        guard !snapshot else { return }
        let fd = open(url.path, O_EVTONLY)
        guard fd >= 0 else { return }
        let source = DispatchSource.makeFileSystemObjectSource(fileDescriptor: fd, eventMask: [.write, .rename, .delete, .extend], queue: .main)
        source.setEventHandler { [weak self] in
            Task { @MainActor in
                guard let self else { return }
                self.refreshTask?.cancel()
                self.refreshTask = Task {
                    try? await Task.sleep(nanoseconds: 300_000_000)
                    guard !Task.isCancelled else { return }
                    self.refresh()
                }
            }
        }
        source.setCancelHandler { close(fd) }
        source.resume()
        watcher = source
    }
}
