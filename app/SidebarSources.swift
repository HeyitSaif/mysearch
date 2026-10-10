// Sidebar sources, kept out of Sidebar.swift: cloud drives and mounted volumes (Locations, with eject), pinned
// folders (Favorites), the Recents section, and the remembered open or closed state of every section.
// Sidebar.swift calls into this file at a few hook points (loadView, buildRoots, viewFor, the drag methods and
// the context menu). Nothing here changes what a selected row means; it only supplies more rows.

import Cocoa

@MainActor
enum SidebarSources {
    /// Posted by a folder view's "Pin to Sidebar" item: the object is the folder's URL.
    static let pinNotification = Notification.Name("MSEPinFolder")

    static let recentLimit = 8
    private static let pinsKey = "MSESidebarPins"            // [Data]: one bookmark per pinned folder
    private static let recentsKey = "MSESidebarRecents"      // [String]: folder paths, newest first
    private static let sectionKeyPrefix = "MSESidebarSectionOpen."

    /// Observers that live as long as the sidebar (the sidebar is never torn down while the app runs).
    static var tokens: [NSObjectProtocol] = []
    /// True while a section is put back to its remembered state, so that is not saved as a new choice.
    private static var restoring = false

    // MARK: helpers

    static func isDirectory(_ url: URL) -> Bool {
        var isDir: ObjCBool = false
        return FileManager.default.fileExists(atPath: url.path, isDirectory: &isDir) && isDir.boolValue
    }

    /// The name a row shows: the volume's own name for "/", the Finder display name otherwise.
    static func name(of url: URL) -> String {
        if url.standardizedFileURL.path == "/" {
            return (try? url.resourceValues(forKeys: [.volumeLocalizedNameKey]))?.volumeLocalizedName ?? "Macintosh HD"
        }
        return FileManager.default.displayName(atPath: url.path)
    }

    /// A menu item whose action is a closure. The item keeps its action alive through representedObject.
    static func menuItem(_ title: String, _ run: @escaping () -> Void) -> NSMenuItem {
        let action = SidebarMenuAction(run)
        let item = NSMenuItem(title: title, action: #selector(SidebarMenuAction.fire), keyEquivalent: "")
        item.target = action
        item.representedObject = action
        return item
    }

    /// The folders in a drag from Finder (file URLs that are directories).
    static func folderURLs(from info: NSDraggingInfo) -> [URL] {
        let options: [NSPasteboard.ReadingOptionKey: Any] = [.urlReadingFileURLsOnly: true]
        let objects = info.draggingPasteboard.readObjects(forClasses: [NSURL.self], options: options) ?? []
        return objects.compactMap { $0 as? URL }.filter { isDirectory($0) }
    }

    // MARK: cloud drives (Locations)

    /// iCloud Drive, and every folder in ~/Library/CloudStorage (Dropbox, Google Drive, OneDrive, ...). Each one only if it exists.
    static func cloudDriveNodes() -> [SidebarNode] {
        let home = FileManager.default.homeDirectoryForCurrentUser
        var nodes: [SidebarNode] = []
        let icloud = home.appendingPathComponent("Library/Mobile Documents/com~apple~CloudDocs", isDirectory: true)
        if isDirectory(icloud) {
            nodes.append(SidebarNode(.place(title: "iCloud Drive", url: icloud, symbol: "icloud")))
        }
        let storage = home.appendingPathComponent("Library/CloudStorage", isDirectory: true)
        let entries = (try? FileManager.default.contentsOfDirectory(at: storage, includingPropertiesForKeys: nil,
                                                                  options: [.skipsHiddenFiles])) ?? []
        let services = entries
            .filter { isDirectory($0) }
            .sorted { $0.lastPathComponent.localizedCaseInsensitiveCompare($1.lastPathComponent) == .orderedAscending }
            .map { url -> (title: String, account: String, url: URL) in
                let parts = cleanCloudName(url.lastPathComponent)
                return (title: parts.title, account: parts.account, url: url)
            }
        var counts: [String: Int] = [:]
        for s in services { counts[s.title, default: 0] += 1 }
        for s in services {
            // two accounts of one service keep their account name, so the rows can be told apart
            let title = (counts[s.title, default: 0] > 1 && !s.account.isEmpty) ? "\(s.title) (\(s.account))" : s.title
            nodes.append(SidebarNode(.place(title: title, url: s.url, symbol: "cloud")))
        }
        return nodes
    }

    /// "GoogleDrive-name@example.com" becomes ("Google Drive", "name@example.com"); "OneDrive-Personal" becomes ("OneDrive", "Personal").
    static func cleanCloudName(_ folder: String) -> (title: String, account: String) {
        var service = folder
        var account = ""
        if let dash = folder.firstIndex(of: "-") {
            service = String(folder[..<dash])
            account = String(folder[folder.index(after: dash)...])
        }
        let known = ["GoogleDrive": "Google Drive", "OneDrive": "OneDrive", "Dropbox": "Dropbox", "Box": "Box",
                     "ProtonDrive": "Proton Drive", "iCloudDrive": "iCloud Drive"]
        if let title = known[service] { return (title, account) }
        return (spaced(service), account)
    }

    /// "ProtonDrive" style names get a space before each capital that follows a lowercase letter.
    private static func spaced(_ text: String) -> String {
        var out = ""
        var previous: Character?
        for ch in text {
            if let p = previous, ch.isUppercase, p.isLowercase { out.append(" ") }
            out.append(ch)
            previous = ch
        }
        return out
    }

    // MARK: mounted volumes (Locations)

    private static let volumeKeys: [URLResourceKey] = [.volumeLocalizedNameKey, .volumeIsBrowsableKey, .volumeIsLocalKey,
                                                       .volumeIsEjectableKey, .volumeIsRemovableKey, .isHiddenKey]

    /// Every mounted volume except the boot volume (already listed under Locations). Hidden and unbrowsable volumes are skipped.
    static func volumeNodes() -> [SidebarNode] {
        let urls = FileManager.default.mountedVolumeURLs(includingResourceValuesForKeys: volumeKeys,
                                                         options: [.skipHiddenVolumes]) ?? []
        var nodes: [SidebarNode] = []
        for url in urls where url.standardizedFileURL.path != "/" {
            let v = try? url.resourceValues(forKeys: Set(volumeKeys))
            if v?.volumeIsBrowsable == false || v?.isHidden == true { continue }
            let title = v?.volumeLocalizedName ?? name(of: url)
            let network = v?.volumeIsLocal == false
            nodes.append(SidebarNode(.place(title: title, url: url, symbol: network ? "network" : "externaldrive")))
        }
        return nodes
    }

    /// A removable, ejectable or network volume.
    static func canEject(_ url: URL) -> Bool {
        guard let v = try? url.resourceValues(forKeys: [.volumeIsEjectableKey, .volumeIsRemovableKey, .volumeIsLocalKey]) else {
            return false
        }
        return v.volumeIsEjectable == true || v.volumeIsRemovable == true || v.volumeIsLocal == false
    }

    /// True only for a mounted volume's own root. A folder inside a network share is never ejectable, so the share is never unmounted by mistake.
    static func isVolumeRoot(_ url: URL) -> Bool {
        let path = url.standardizedFileURL.path
        let mounted = FileManager.default.mountedVolumeURLs(includingResourceValuesForKeys: nil, options: [.skipHiddenVolumes]) ?? []
        return mounted.contains { $0.standardizedFileURL.path == path }
    }

    // MARK: pinned folders (Favorites)

    private static var pinData: [Data] {
        get { (UserDefaults.standard.array(forKey: pinsKey) as? [Data]) ?? [] }
        set { UserDefaults.standard.set(newValue, forKey: pinsKey) }
    }

    private static func resolve(_ data: Data) -> URL? {
        var stale = false
        return (try? URL(resolvingBookmarkData: data, options: [], relativeTo: nil, bookmarkDataIsStale: &stale))?.standardizedFileURL
    }

    /// The pinned folders that resolve now. A bookmark that does not resolve (an unplugged drive, say) is kept and simply not shown.
    static func pinnedURLs() -> [URL] {
        pinData.compactMap { resolve($0) }
    }

    static func isPinned(_ url: URL) -> Bool {
        let path = url.standardizedFileURL.path
        return pinnedURLs().contains { $0.path == path }
    }

    /// Pins a folder to Favorites. Only a bookmark is stored; the folder itself is never touched.
    static func pin(_ url: URL) {
        let target = url.standardizedFileURL
        guard isDirectory(target), !isPinned(target), let data = try? target.bookmarkData() else { return }
        pinData = pinData + [data]
    }

    /// Removes the pin only. The folder stays exactly where it is.
    static func unpin(_ url: URL) {
        let path = url.standardizedFileURL.path
        pinData = pinData.filter { resolve($0)?.path != path }
    }

    static func pinnedNodes() -> [SidebarNode] {
        pinnedURLs().filter { isDirectory($0) }.map { SidebarNode(.place(title: name(of: $0), url: $0, symbol: "folder")) }
    }

    /// Favorites: the standard folders, then the pinned ones.
    static func favoriteNodes(home: URL) -> [SidebarNode] {
        [
            SidebarNode(.place(title: "Home", url: home, symbol: "house")),
            SidebarNode(.place(title: "Desktop", url: home.appendingPathComponent("Desktop"), symbol: "menubar.dock.rectangle")),
            SidebarNode(.place(title: "Documents", url: home.appendingPathComponent("Documents"), symbol: "doc")),
            SidebarNode(.place(title: "Downloads", url: home.appendingPathComponent("Downloads"), symbol: "arrow.down.circle")),
            SidebarNode(.place(title: "Applications", url: URL(fileURLWithPath: "/Applications"), symbol: "square.grid.2x2")),
        ] + pinnedNodes()
    }

    // MARK: recents

    /// Remembers a folder the Navigator visited: newest first, no repeats, the last `recentLimit` kept.
    static func recordRecent(_ url: URL) {
        let path = url.standardizedFileURL.path
        var paths = UserDefaults.standard.stringArray(forKey: recentsKey) ?? []
        paths.removeAll { $0 == path }
        paths.insert(path, at: 0)
        UserDefaults.standard.set(Array(paths.prefix(recentLimit)), forKey: recentsKey)
    }

    /// The Recents rows: the remembered folders that still exist, or a note when there are none.
    static func recentChildren() -> [SidebarNode] {
        let paths = UserDefaults.standard.stringArray(forKey: recentsKey) ?? []
        let urls = paths.prefix(recentLimit).map { URL(fileURLWithPath: $0, isDirectory: true) }.filter { isDirectory($0) }
        guard !urls.isEmpty else { return [SidebarNode(.note("No recent folders"))] }
        return urls.map { SidebarNode(.place(title: name(of: $0), url: $0, symbol: "clock")) }
    }

    // MARK: section memory (every section can be collapsed, and the state is kept)

    static func isOpen(_ title: String) -> Bool {
        (UserDefaults.standard.object(forKey: sectionKeyPrefix + title) as? Bool) ?? true
    }

    /// Runs a reload or restore that the outline itself reports as expand, collapse or selection changes, so none of
    /// it is saved as a user choice and none of it reports a selection change.
    static func quietly(_ body: () -> Void) {
        let was = restoring
        restoring = true
        defer { restoring = was }
        body()
    }

    /// Puts a section back to its remembered state. An open section also opens its children (the Indexed groups).
    static func applySectionState(_ outline: NSOutlineView, _ node: SidebarNode) {
        quietly {
            applyState(outline, node)
        }
    }

    private static func applyState(_ outline: NSOutlineView, _ node: SidebarNode) {
        if isOpen(node.title) {
            outline.expandItem(node, expandChildren: true)
        } else {
            outline.collapseItem(node)
        }
    }

    /// Saves a section the user opened or closed. Called from the outline's expand and collapse notifications.
    static func sectionChanged(_ note: Notification, open: Bool) {
        guard !restoring, let node = note.userInfo?["NSObject"] as? SidebarNode, node.isSection else { return }
        UserDefaults.standard.set(open, forKey: sectionKeyPrefix + node.title)
    }
}

/// A menu item's action, kept alive by the item's representedObject.
@MainActor
final class SidebarMenuAction: NSObject {
    let run: () -> Void
    init(_ run: @escaping () -> Void) { self.run = run }
    @objc func fire() { run() }
}

/// The eject button at the end of a mounted volume's row. It is its own target.
@MainActor
final class SidebarEjectButton: NSButton {
    var onPress: (() -> Void)?

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        target = self
        action = #selector(pressed)
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
    }

    @objc private func pressed() { onPress?() }
}

// MARK: - Hooks used by Sidebar.swift

extension SidebarViewController {
    /// Connects the sidebar sources: section memory, mounts and unmounts, the Navigator's visits, and pins sent by the folder views.
    /// Called once from loadView.
    func installSidebarSources() {
        let center = NotificationCenter.default
        SidebarSources.tokens.append(center.addObserver(forName: NSOutlineView.itemDidExpandNotification, object: outline, queue: .main) { note in
            MainActor.assumeIsolated { SidebarSources.sectionChanged(note, open: true) }
        })
        SidebarSources.tokens.append(center.addObserver(forName: NSOutlineView.itemDidCollapseNotification, object: outline, queue: .main) { note in
            MainActor.assumeIsolated { SidebarSources.sectionChanged(note, open: false) }
        })
        let workspace = NSWorkspace.shared.notificationCenter
        for name in [NSWorkspace.didMountNotification, NSWorkspace.didUnmountNotification, NSWorkspace.didRenameVolumeNotification] {
            SidebarSources.tokens.append(workspace.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated { self?.reloadLocations() }
            })
        }
        SidebarSources.tokens.append(center.addObserver(forName: .navigatorDidChange, object: nil, queue: .main) { [weak self] note in
            MainActor.assumeIsolated {
                if let url = note.userInfo?["url"] as? URL { SidebarSources.recordRecent(url) }
                self?.reloadRecents()
            }
        })
        SidebarSources.tokens.append(center.addObserver(forName: SidebarSources.pinNotification, object: nil, queue: .main) { [weak self] note in
            MainActor.assumeIsolated {
                guard let url = note.object as? URL else { return }
                SidebarSources.pin(url)
                self?.reloadFavorites()
            }
        })
    }

    func sectionNode(_ title: String) -> SidebarNode? {
        roots.first { $0.isSection && $0.title == title }
    }

    func reloadFavorites() {
        refreshSection("Favorites", SidebarSources.favoriteNodes(home: FileManager.default.homeDirectoryForCurrentUser))
    }

    func reloadRecents() {
        refreshSection("Recents", SidebarSources.recentChildren())
    }

    /// Rebuilds Locations: the boot volume, then cloud drives, then mounted volumes.
    func reloadLocations() {
        guard let node = sectionNode("Locations") else { return }
        let boot = node.children.filter { $0.placeURL?.path == "/" }
        refreshSection("Locations", boot + SidebarSources.cloudDriveNodes() + SidebarSources.volumeNodes())
    }

    /// Replaces one section's rows. The selection callback is silenced: a row that is rebuilt must not reset the search scope.
    private func refreshSection(_ title: String, _ children: [SidebarNode]) {
        guard let node = sectionNode(title) else { return }
        let was = suppressCallback
        suppressCallback = true
        defer { suppressCallback = was }
        SidebarSources.quietly {
            node.children = children
            outline.reloadItem(node, reloadChildren: true)
            SidebarSources.applySectionState(outline, node)
        }
    }

    /// The drop operation for a Finder folder dragged onto Favorites. nil means this is not that drag, so the sidebar's own rules apply.
    func pinDropOperation(_ outlineView: NSOutlineView, _ info: NSDraggingInfo, _ item: Any?) -> NSDragOperation? {
        guard isFavoritesTarget(outlineView, item), let favorites = sectionNode("Favorites"),
              !SidebarSources.folderURLs(from: info).isEmpty else { return nil }
        outlineView.setDropItem(favorites, dropChildIndex: NSOutlineViewDropOnItemIndex)
        return .copy
    }

    /// Pins the folders of a Finder drop onto Favorites. Returns false when the drop is not for Favorites.
    func acceptPinDrop(_ info: NSDraggingInfo, _ item: Any?) -> Bool {
        guard let node = item as? SidebarNode, node.isSection, node.title == "Favorites" else { return false }
        let urls = SidebarSources.folderURLs(from: info)
        guard !urls.isEmpty else { return false }
        for url in urls { SidebarSources.pin(url) }
        reloadFavorites()
        return true
    }

    private func isFavoritesTarget(_ outlineView: NSOutlineView, _ item: Any?) -> Bool {
        guard let node = item as? SidebarNode else { return false }
        if node.isSection { return node.title == "Favorites" }
        return (outlineView.parent(forItem: node) as? SidebarNode)?.title == "Favorites"
    }

    /// Context-menu items for a folder row: Remove from Sidebar for a pin, Pin to Sidebar for any other folder outside Favorites.
    func addPlaceItems(_ node: SidebarNode, to menu: NSMenu) {
        guard let url = node.placeURL else { return }
        let inFavorites = (outline.parent(forItem: node) as? SidebarNode)?.title == "Favorites"
        if SidebarSources.isPinned(url) {
            menu.addItem(SidebarSources.menuItem("Remove from Sidebar") { [weak self] in
                SidebarSources.unpin(url)
                self?.reloadFavorites()
            })
        } else if !inFavorites, SidebarSources.isDirectory(url) {
            menu.addItem(SidebarSources.menuItem("Pin to Sidebar") { [weak self] in
                SidebarSources.pin(url)
                self?.reloadFavorites()
            })
        }
    }

    /// Adds the eject button to a mounted volume's row in Locations (only for a removable or network volume root).
    func decoratePlaceCell(_ cell: NSView, text: NSTextField, node: SidebarNode) {
        guard let url = node.placeURL, (outline.parent(forItem: node) as? SidebarNode)?.title == "Locations",
              SidebarSources.isVolumeRoot(url), SidebarSources.canEject(url) else { return }
        let button = SidebarEjectButton(frame: .zero)
        button.image = NSImage(systemSymbolName: "eject.fill", accessibilityDescription: "Eject")
        button.isBordered = false
        button.imagePosition = .imageOnly
        button.contentTintColor = .secondaryLabelColor
        button.toolTip = "Eject “\(node.title)”"
        button.translatesAutoresizingMaskIntoConstraints = false
        button.onPress = { [weak self] in self?.eject(url, name: node.title) }
        cell.addSubview(button)
        NSLayoutConstraint.activate([
            button.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -4),
            button.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
            button.widthAnchor.constraint(equalToConstant: 16),
            text.trailingAnchor.constraint(lessThanOrEqualTo: button.leadingAnchor, constant: -6),
        ])
    }

    private func eject(_ url: URL, name: String) {
        do {
            try NSWorkspace.shared.unmountAndEjectDevice(at: url)
        } catch {
            let alert = NSAlert()
            alert.alertStyle = .warning
            alert.messageText = "“\(name)” was not ejected"
            alert.informativeText = error.localizedDescription
            alert.addButton(withTitle: "OK")
            if let window = view.window { alert.beginSheetModal(for: window) { _ in } } else { alert.runModal() }
        }
    }
}
