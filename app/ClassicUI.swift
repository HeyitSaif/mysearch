// The Classic interface (the old web UI in its own window, and the web Settings page) is off by default. It is one
// app setting, classicUIEnabled in UserDefaults. Everything that offers Classic reads it here, so the menus, the
// status item, the Command Palette, the toolbars and mysearch://classic agree. A change goes through setEnabled, which
// posts didChange; the AppKit parts (AppDelegate, StatusItem, Settings.swift) listen for it.

import Cocoa

enum ClassicUI {
    static let key = "classicUIEnabled"
    static let didChange = Notification.Name("MSEClassicUIDidChange")

    /// The main window's Classic toolbar item. MainWindowController.Item.classic is private there; the raw value must match.
    static let toolbarItemID = NSToolbarItem.Identifier("MySearch.classic")
    /// The Settings toolbar's Classic Settings item (Settings.swift).
    static let settingsToolbarItemID = NSToolbarItem.Identifier("classicSettings")

    /// Snapshot renders only (SnapshotSettingsExtra.swift): sets the value for one process without writing the defaults.
    nonisolated(unsafe) static var snapshotOverride: Bool?

    static var isEnabled: Bool {
        if let snapshotOverride { return snapshotOverride }
        return UserDefaults.standard.bool(forKey: key)
    }

    /// The one setter. Posts didChange only when the value really changes.
    static func setEnabled(_ on: Bool) {
        guard on != isEnabled else { return }
        UserDefaults.standard.set(on, forKey: key)
        NotificationCenter.default.post(name: didChange, object: nil)
    }

    /// The toolbar identifiers the main window's default toolbar has for Classic: one item when on, none when off.
    static var toolbarItems: [NSToolbarItem.Identifier] { isEnabled ? [toolbarItemID] : [] }

    /// Keeps one toolbar's Classic item in step with the setting: inserted at the end when on, removed when off.
    @MainActor static func syncToolbar(_ bar: NSToolbar?, itemID: NSToolbarItem.Identifier) {
        guard let bar else { return }
        let at = bar.items.firstIndex { $0.itemIdentifier == itemID }
        if isEnabled, at == nil { bar.insertItem(withItemIdentifier: itemID, at: bar.items.count) }
        if !isEnabled, let at { bar.removeItem(at: at) }
    }

    /// The alert shown wherever Classic would open while it is off. Its first button goes to the toggle in Settings.
    @MainActor static func makeOffAlert() -> NSAlert {
        let alert = NSAlert()
        alert.alertStyle = .informational
        alert.messageText = "The Classic interface is turned off."
        alert.informativeText = "Turn it on in Settings › Advanced."
        alert.addButton(withTitle: "Open Settings")
        alert.addButton(withTitle: "Cancel")
        return alert
    }

    /// Shows the off alert. "Open Settings" opens Settings › Advanced with the toggle highlighted.
    @MainActor static func presentOffAlert() {
        let alert = makeOffAlert()
        NSApp.activate(ignoringOtherApps: true)
        if alert.runModal() == .alertFirstButtonReturn {
            (NSApp.delegate as? AppDelegate)?.openSettings(section: .advanced, highlight: "classic.toggle")
        }
    }
}
