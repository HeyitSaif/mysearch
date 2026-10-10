// The menu-bar item: the edge-notched index card, with the same menu as before. Its status line asks the
// service (/api/status) when the menu opens.

import Cocoa

/// The menu-bar icon: the edge-notched index card, as a template image.
func statusIcon() -> NSImage {
    let img = NSImage(size: NSSize(width: 18, height: 18), flipped: true) { _ in
        NSColor.black.set()
        let card = NSBezierPath(roundedRect: NSRect(x: 3.5, y: 3.6, width: 12.5, height: 10.8), xRadius: 2, yRadius: 2)
        card.lineWidth = 1.3
        card.stroke()
        for y in [6.9, 11.1] {        // two notches cut open on the left edge
            NSBezierPath(roundedRect: NSRect(x: 2.2, y: y - 1.15, width: 3.6, height: 2.3), xRadius: 1.15, yRadius: 1.15).fill()
        }
        let lines = NSBezierPath()
        lines.move(to: NSPoint(x: 8, y: 7.4)); lines.line(to: NSPoint(x: 13.5, y: 7.4))
        lines.move(to: NSPoint(x: 8, y: 10.6)); lines.line(to: NSPoint(x: 11.8, y: 10.6))
        lines.lineWidth = 1.3
        lines.lineCapStyle = .round
        lines.stroke()
        return true
    }
    img.isTemplate = true
    img.accessibilityDescription = "MySearch"
    return img
}

/// Shows the Quick Search shortcut in a menu item's key equivalent, or in its title when the key has no symbol.
func updateQuickMenuItems(_ items: [NSMenuItem], key: HotKey) {
    for item in items {
        if let (k, mods) = key.menuKey { item.keyEquivalent = k; item.keyEquivalentModifierMask = mods }
        else { item.keyEquivalent = ""; item.title = "Quick Search (\(key.label))" }
    }
}

@MainActor
final class StatusItemController: NSObject, NSMenuDelegate {
    private var item: NSStatusItem!
    private(set) var quickMenuItems: [NSMenuItem] = []
    private var statusLine: NSMenuItem!
    private var indexWatch: Task<Void, Never>?

    var onQuick: (() -> Void)?
    var onOpen: (() -> Void)?
    var onSync: (() -> Void)?
    var onSettings: (() -> Void)?
    var onClassic: (() -> Void)?

    func install(quickKey: HotKey) {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        item.button?.image = statusIcon()
        item.button?.toolTip = "MySearch"
        let menu = NSMenu()
        menu.autoenablesItems = false
        menu.delegate = self
        let quick = NSMenuItem(title: "Quick Search", action: #selector(quickAction), keyEquivalent: "")
        quickMenuItems.append(quick)
        menu.addItem(quick)
        menu.addItem(withTitle: "Open MySearch", action: #selector(openAction), keyEquivalent: "")
        menu.addItem(withTitle: "Sync Now", action: #selector(syncAction), keyEquivalent: "")
        menu.addItem(withTitle: "Open Classic MySearch", action: #selector(classicAction), keyEquivalent: "")
        menu.addItem(.separator())
        statusLine = NSMenuItem(title: "Checking the search service…", action: nil, keyEquivalent: "")
        statusLine.isEnabled = false
        menu.addItem(statusLine)
        menu.addItem(.separator())
        menu.addItem(withTitle: "Settings…", action: #selector(settingsAction), keyEquivalent: "")
        menu.addItem(withTitle: "Quit MySearch", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "")
        for m in menu.items where m.action != #selector(NSApplication.terminate(_:)) { m.target = self }
        item.menu = menu
        updateQuickMenuItems(quickMenuItems, key: quickKey)
        startIndexWatch()
    }

    /// While a folder is being indexed the menu-bar item reads "Indexing <folder> 42%"; otherwise it is the icon alone.
    /// It asks the service every 1.5 seconds while something is indexing and every 6 seconds while idle.
    private func startIndexWatch() {
        indexWatch = Task { @MainActor [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                let title = await IndexProgressAPI.menuBarTitle()
                self.showIndexTitle(title)
                try? await Task.sleep(nanoseconds: title == nil ? 6_000_000_000 : 1_500_000_000)
            }
        }
    }

    private func showIndexTitle(_ title: String?) {
        guard let item, let button = item.button else { return }
        if let title {
            if item.length != NSStatusItem.variableLength { item.length = NSStatusItem.variableLength }
            button.title = " " + title
            button.imagePosition = .imageLeading
        } else if item.length != NSStatusItem.squareLength || !button.title.isEmpty {
            button.title = ""
            button.imagePosition = .imageOnly
            item.length = NSStatusItem.squareLength
        }
    }

    /// Re-labels the Quick Search items after the shortcut changes.
    func setQuickKey(_ key: HotKey, extraItems: [NSMenuItem] = []) {
        updateQuickMenuItems(quickMenuItems + extraItems, key: key)
    }

    @objc private func quickAction() { onQuick?() }
    @objc private func openAction() { onOpen?() }
    @objc private func syncAction() { onSync?() }
    @objc private func settingsAction() { onSettings?() }
    @objc private func classicAction() { onClassic?() }

    func menuWillOpen(_ menu: NSMenu) {
        Task {
            do {
                let s = try await MSEClient.shared.status()
                statusLine.title = "Search service running · \(s.version)"
            } catch {
                statusLine.title = "Search service not running"
            }
        }
    }

    /// Shown when the Sync action cannot start the service's sync.
    func showSyncFailed() { statusLine.title = "Sync could not start: the service is not answering" }
}
