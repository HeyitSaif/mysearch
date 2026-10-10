// Shared by the list, column and icon views: the node the views wrap a FileItem in, Finder-style formatting of
// sizes and dates, the context menu (Open, Open With, Get Info, Rename, Compress, Duplicate, Make Alias, Extract,
// Quick Look, Copy, Cut, Paste, Tags, Show in Finder, Copy Path, Move to Trash, and for a folder Exclude from /
// Include in whole-Mac search) and the Quick Look panel. FileActions only decides what to do and asks FileOpsService
// (FileOps.swift) to do it; every disk change goes through that service, with undo.

import Cocoa
import Quartz

/// A node in a folder view. A view keeps one node per URL, so expansion, selection and sorting survive reloads.
@MainActor
final class ItemNode: NSObject {
    var item: FileItem
    /// Nil until the folder's children have been loaded.
    var children: [ItemNode]?
    /// How deep below the folder the view was opened at: the root's children are 0.
    var depth = 0
    /// Set by ⌥-click: this folder and every folder below it expand as their children arrive.
    var expandAll = false

    init(_ item: FileItem) {
        self.item = item
    }

    override func isEqual(_ object: Any?) -> Bool {
        guard let other = object as? ItemNode else { return false }
        return other.item.url == item.url
    }

    override var hash: Int { item.url.hashValue }
}

/// Finder's formatting: sizes in file units, dates relative to today ("Today at 14:05"). Folders have no size.
enum FileFormat {
    private static let sizeFormatter: ByteCountFormatter = {
        let f = ByteCountFormatter()
        f.countStyle = .file
        return f
    }()

    private static let dateFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateStyle = .medium
        f.timeStyle = .short
        f.doesRelativeDateFormatting = true
        return f
    }()

    static func size(_ item: FileItem) -> String {
        guard let bytes = item.size else { return "--" }
        return sizeFormatter.string(fromByteCount: bytes)
    }

    private static let longFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateStyle = .long
        f.timeStyle = .short
        return f
    }()

    /// "9 October 2026 at 14:05", for Get Info.
    static func longDate(_ date: Date?) -> String {
        guard let date else { return "--" }
        return longFormatter.string(from: date)
    }

    static func date(_ date: Date?) -> String {
        guard let date else { return "--" }
        return dateFormatter.string(from: date)
    }
}

/// The Quick Look panel for a folder view. Space toggles it for the current selection.
@MainActor
final class QuickLookCoordinator: NSObject, QLPreviewPanelDataSource, QLPreviewPanelDelegate {
    static let shared = QuickLookCoordinator()
    private var urls: [URL] = []

    /// Shows the panel for `urls`, or hides it when it is already showing.
    func toggle(_ urls: [URL]) {
        guard let panel = QLPreviewPanel.shared() else { return }
        if panel.isVisible {
            panel.orderOut(nil)
            return
        }
        guard !urls.isEmpty else { return }
        self.urls = urls
        panel.dataSource = self
        panel.delegate = self
        panel.reloadData()
        panel.makeKeyAndOrderFront(nil)
    }

    func numberOfPreviewItems(in panel: QLPreviewPanel!) -> Int { urls.count }

    func previewPanel(_ panel: QLPreviewPanel!, previewItemAt index: Int) -> QLPreviewItem! {
        urls[index] as NSURL
    }
}

/// The item a context-menu action works on: the URLs, and for Open With the application to use.
private final class OpenRequest: NSObject {
    let urls: [URL]
    let app: URL?

    init(urls: [URL], app: URL?) {
        self.urls = urls
        self.app = app
    }
}

/// What a folder view tells FileActions about itself: its window (for alerts), the folder new items go into, the
/// selection, how to rename inline, and where to print a status message.
@MainActor
protocol FolderHost: AnyObject {
    var hostWindow: NSWindow? { get }
    /// The folder Paste and New Folder work in; nil for a flat list (search results, a tag).
    var hostFolder: URL? { get }
    var hostSelection: [FileItem] { get }
    func hostBeginRename(_ item: FileItem)
    func hostStatus(_ text: String)
}

/// Implemented by the three folder views' AppKit views, so the File menu finds the FileActions of the view that has
/// the keyboard.
@MainActor
protocol FileActionsProviding: AnyObject {
    var fileActions: FileActions? { get }
}

/// A closure kept alive by a menu item (its representedObject) and fired as the item's action.
@MainActor
private final class MenuClosure: NSObject {
    let run: () -> Void
    init(_ run: @escaping () -> Void) { self.run = run }
    @objc func fire() { run() }
}

/// Opening, copying and the context menu of a folder view. The owning view sets `onOpenFolder`, so a folder opens in
/// MySearch's own navigation, and `host`, so file operations know the window, the folder and the selection.
@MainActor
final class FileActions: NSObject {
    /// Opens a folder in MySearch (the user navigates to it).
    var onOpenFolder: ((URL) -> Void)?
    weak var host: FolderHost?

    private var service: FileOpsService { .shared }

    private var context: FileOpContext {
        FileOpContext(window: host?.hostWindow, status: { [weak host] text in host?.hostStatus(text) })
    }

    /// The FileActions of the folder view that has the keyboard, or nil (a text field is being edited, or the focus
    /// is elsewhere in the app).
    static var active: FileActions? {
        var responder = NSApp.keyWindow?.firstResponder
        if responder is NSText { return nil }
        while let current = responder {
            if let provider = current as? FileActionsProviding { return provider.fileActions }
            responder = current.nextResponder
        }
        return nil
    }

    override init() {
        super.init()
        WholeMacExclusions.shared.refresh()          // so the first right-click already knows which folders are excluded
    }

    // MARK: opening

    /// Opens each item: a folder navigates, text and code go to the editor, anything else opens in its default app.
    func open(_ items: [FileItem]) {
        var files: [URL] = []
        for item in items {
            if item.isDir { onOpenFolder?(item.url) } else { files.append(item.url) }
        }
        FileOpener.open(files)
    }

    // MARK: commands (the File menu, the keys and the context menu all end here)

    func run(_ command: FileMenuRouter.Command) {
        let items = host?.hostSelection ?? []
        let urls = items.map(\.url)
        switch command {
        case .newFolder: newFolder()
        case .rename: if items.count == 1 { host?.hostBeginRename(items[0]) }
        case .duplicate: service.duplicate(urls, ctx: context)
        case .alias: service.makeAlias(urls, ctx: context)
        case .compress: service.compress(urls, ctx: context)
        case .extract: service.extract(urls, ctx: context)
        case .trash: service.trash(urls, ctx: context)
        case .deleteNow: service.deleteImmediately(urls, ctx: context)
        case .showInFinder: if !urls.isEmpty { NSWorkspace.shared.activateFileViewerSelecting(urls) }
        case .copyPath: copyPaths(urls)
        }
    }

    func canRun(_ command: FileMenuRouter.Command) -> Bool {
        let items = host?.hostSelection ?? []
        switch command {
        case .newFolder: return host?.hostFolder != nil
        case .rename: return items.count == 1
        case .extract: return items.contains { FileOpsService.archiveKind(of: $0.url) != nil }
        default: return !items.isEmpty
        }
    }

    func newFolder() {
        guard let folder = host?.hostFolder else { return }
        service.newFolder(in: folder, ctx: context)
    }

    func cutSelection() {
        service.cut((host?.hostSelection ?? []).map(\.url), ctx: context)
    }

    func copySelection() {
        copyFiles((host?.hostSelection ?? []).map(\.url))
    }

    func pasteHere() {
        guard let folder = host?.hostFolder else { return }
        service.paste(into: folder, ctx: context)
    }

    func rename(_ url: URL, to name: String) {
        service.rename(url, to: name, ctx: context)
    }

    /// A rename dialog, for the views without an inline rename (the column view).
    func promptRename(_ item: FileItem) {
        service.promptName(title: "Rename", message: "Enter a new name for “\(item.url.lastPathComponent)”.",
                           current: item.url.lastPathComponent, button: "Rename", in: host?.hostWindow) { [weak self] name in
            self?.rename(item.url, to: name)
        }
    }

    /// A drop of `urls` (from inside the app) into `dir`.
    func drop(_ urls: [URL], into dir: URL, operation: NSDragOperation) {
        service.transfer(urls, into: dir, copy: operation == .copy, ctx: context)
    }

    // MARK: the context menu

    /// Fills `menu` with the actions for a set of items (the right-click selection). With no items it is the menu of
    /// the empty background: New Folder and Paste.
    func fillContextMenu(_ menu: NSMenu, for items: [FileItem]) {
        guard !items.isEmpty else {
            fillBackgroundMenu(menu)
            return
        }
        let urls = items.map(\.url)

        add(menu, "Open") { [weak self] in self?.open(items) }

        if items.allSatisfy({ !$0.isDir }) {
            let withItem = NSMenuItem(title: "Open With", action: nil, keyEquivalent: "")
            withItem.submenu = openWithMenu(for: urls)
            menu.addItem(withItem)
        }

        menu.addItem(.separator())
        add(menu, "Get Info", key: "i") { InfoPanelController.shared.show(items) }
        add(menu, "Rename", enabled: items.count == 1) { [weak self] in self?.host?.hostBeginRename(items[0]) }
        add(menu, items.count == 1 ? "Compress “\(items[0].name)”" : "Compress \(items.count) Items") { [weak self] in
            guard let self else { return }
            self.service.compress(urls, ctx: self.context)
        }
        add(menu, "Duplicate", key: "d") { [weak self] in
            guard let self else { return }
            self.service.duplicate(urls, ctx: self.context)
        }
        add(menu, "Make Alias") { [weak self] in
            guard let self else { return }
            self.service.makeAlias(urls, ctx: self.context)
        }
        if items.contains(where: { FileOpsService.archiveKind(of: $0.url) != nil }) {
            add(menu, "Extract") { [weak self] in
                guard let self else { return }
                self.service.extract(urls, ctx: self.context)
            }
        }
        add(menu, "Quick Look", key: " ") { QuickLookCoordinator.shared.toggle(urls) }

        menu.addItem(.separator())
        add(menu, "Copy", key: "c") { [weak self] in self?.copyFiles(urls) }
        add(menu, "Cut", key: "x") { [weak self] in
            guard let self else { return }
            self.service.cut(urls, ctx: self.context)
        }
        if host?.hostFolder != nil {
            add(menu, "Paste", key: "v", enabled: service.hasPasteableFiles) { [weak self] in self?.pasteHere() }
        }
        // Split Pane (SplitPanes.swift): the selection to or from the other folder pane of this window.
        if let folderHost = host, let main = folderHost.hostWindow?.delegate as? MainWindowController, main.canTransferBetweenPanes {
            let source = folderHost as AnyObject
            add(menu, "Copy to Other Pane") { main.transferFromPane(source, urls: urls, copy: true) }
            add(menu, "Move to Other Pane") { main.transferFromPane(source, urls: urls, copy: false) }
        }

        addTagItems(to: menu, for: items)

        menu.addItem(.separator())
        add(menu, "Show in Finder") { NSWorkspace.shared.activateFileViewerSelecting(urls) }
        add(menu, "Copy Path", key: "c", mask: [.command, .option]) { [weak self] in self?.copyPaths(urls) }
        for item in Launchers.menuItems(for: urls) { menu.addItem(item) }         // Open in… (Launchers.swift)
        if items.count == 1, items[0].isDir {
            let folder = items[0].url
            add(menu, "Pin to Sidebar") {
                NotificationCenter.default.post(name: Notification.Name("MSEPinFolder"), object: folder)
            }
        }

        menu.addItem(.separator())
        add(menu, "Move to Trash", key: "\u{8}") { [weak self] in
            guard let self else { return }
            self.service.trash(urls, ctx: self.context)
        }

        // Folders only: take them out of the whole-Mac name search or put them back (WholeMacExclusions.swift).
        addWholeMacItems(to: menu, for: items)
    }

    /// Right-click on the empty background of a folder.
    private func fillBackgroundMenu(_ menu: NSMenu) {
        guard host?.hostFolder != nil else { return }
        add(menu, "New Folder", key: "n", mask: [.command, .shift]) { [weak self] in self?.newFolder() }
        add(menu, "Paste", key: "v", enabled: service.hasPasteableFiles) { [weak self] in self?.pasteHere() }
        if let folder = host?.hostFolder {
            for item in Launchers.menuItems(for: [folder]) { menu.addItem(item) }
        }
    }

    @discardableResult
    private func add(_ menu: NSMenu, _ title: String, key: String = "", mask: NSEvent.ModifierFlags = [.command],
                     enabled: Bool = true, _ run: @escaping () -> Void) -> NSMenuItem {
        let closure = MenuClosure(run)
        let item = NSMenuItem(title: title, action: #selector(MenuClosure.fire), keyEquivalent: key)
        if !key.isEmpty { item.keyEquivalentModifierMask = mask }
        item.target = closure
        item.representedObject = closure                    // the item does not retain its target
        item.isEnabled = enabled
        menu.addItem(item)
        return item
    }

    /// The "Tags" row of the colour dots, and Clear Tags.
    private func addTagItems(to menu: NSMenu, for items: [FileItem]) {
        let urls = items.map(\.url)
        menu.addItem(.separator())
        let header = NSMenuItem(title: "Tags", action: nil, keyEquivalent: "")
        header.isEnabled = false
        menu.addItem(header)
        var state: [TagColor: Bool] = [:]
        for color in TagColor.allCases { state[color] = items.allSatisfy { $0.tagColors.contains(color) } }
        let row = NSMenuItem()
        row.view = TagMenuRowView(state: state) { [weak self] color in
            guard let self else { return }
            self.service.toggleTag(color, on: urls, ctx: self.context)
        }
        menu.addItem(row)
        add(menu, "Clear Tags", enabled: items.contains { !$0.tagNames.isEmpty }) { [weak self] in
            guard let self else { return }
            self.service.clearTags(on: urls, ctx: self.context)
        }
    }

    /// The applications that can open the first file, the default one first.
    private func openWithMenu(for urls: [URL]) -> NSMenu {
        let menu = NSMenu(title: "Open With")
        guard let first = urls.first else { return menu }
        let workspace = NSWorkspace.shared
        let fallback = workspace.urlForApplication(toOpen: first)
        var apps = workspace.urlsForApplications(toOpen: first)
        if let fallback, let index = apps.firstIndex(of: fallback) {
            apps.remove(at: index)
            apps.insert(fallback, at: 0)
        }
        for app in apps {
            let title = FileManager.default.displayName(atPath: app.path)
            let entry = add(menu, title) {
                NSWorkspace.shared.open(urls, withApplicationAt: app, configuration: NSWorkspace.OpenConfiguration())
            }
            entry.image = workspace.icon(forFile: app.path)
            entry.image?.size = NSSize(width: 16, height: 16)
        }
        if apps.isEmpty {
            menu.addItem(withTitle: "No Applications Found", action: nil, keyEquivalent: "")
        }
        return menu
    }

    // MARK: pasteboard

    /// Puts file URLs on the general pasteboard, as Finder's Copy does, so they paste into Finder and other apps.
    func copyFiles(_ urls: [URL]) {
        service.copy(urls)
    }

    /// Puts the POSIX paths on the pasteboard as text, one per line.
    func copyPaths(_ urls: [URL]) {
        guard !urls.isEmpty else { return }
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        pasteboard.setString(urls.map(\.path).joined(separator: "\n"), forType: .string)
    }
}
