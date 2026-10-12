// The Settings window (⌘,): native SwiftUI (SettingsWindowViews.swift and the Settings*.swift files beside it).
// The old web settings page stays reachable from the sidebar's "Classic Settings…" until nothing is left only there
// (today: recording a new Quick Search shortcut); it keeps the `app` message bridge to the app delegate.

import Cocoa
import SwiftUI

@MainActor
final class SettingsWindowController: NSObject, NSWindowDelegate {
    private let window: NSWindow
    private let model = SWModel()
    private let classic: ClassicSettingsWindowController

    init(appDelegate: AppDelegate) {
        classic = ClassicSettingsWindowController(appDelegate: appDelegate)
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1040, height: 720),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        super.init()
        window.title = "Settings"
        window.isReleasedWhenClosed = false
        window.tabbingMode = .disallowed
        window.minSize = NSSize(width: 880, height: 560)
        window.delegate = self
        let host = NSHostingController(rootView: SWRootView(m: model))
        host.sizingOptions = []
        window.contentViewController = host
        window.setContentSize(NSSize(width: 1040, height: 720))
        window.setFrameAutosaveName("MySearchNativeSettings")
        if window.frame.origin == .zero { window.center() }
        let bar = NSToolbar(identifier: "MySearchSettingsToolbar")
        bar.delegate = self
        bar.displayMode = .iconAndLabel
        window.toolbar = bar
        window.toolbarStyle = .unified
        model.window = window
        model.openClassic = { [weak self] section in self?.classic.show(section: section) }
        NotificationCenter.default.addObserver(forName: ClassicUI.didChange, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in self?.syncClassicToolbarItem() }
        }
    }

    /// Opens the window (⌘,) and starts the live refresh. `section` opens that pane; `highlight` opens the pane of that
    /// row and flashes it (the Classic alert's "Open Settings" uses both).
    func show(section: SWSection? = nil, highlight: String? = nil) {
        if let highlight, let item = SWIndex.items.first(where: { $0.id == highlight }) { model.reveal(item) }
        else if let section { model.setSection(section) }
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
        model.start()
    }

    /// Adds or removes the toolbar's Classic Settings item to match the Classic setting (ClassicUI.swift).
    func syncClassicToolbarItem() {
        ClassicUI.syncToolbar(window.toolbar, itemID: ClassicUI.settingsToolbarItemID)
    }

    @objc private func classicAction(_ sender: Any?) { classic.show() }

    func windowWillClose(_ notification: Notification) { model.stop() }
    func windowDidMiniaturize(_ notification: Notification) { model.stop() }
    func windowDidDeminiaturize(_ notification: Notification) { model.start() }
}

/// The toolbar's one item: "Classic Settings…" opens the old web settings window.
extension SettingsWindowController: NSToolbarDelegate {
    private static let classicID = NSToolbarItem.Identifier("classicSettings")

    func toolbarAllowedItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] { [.flexibleSpace, Self.classicID] }
    func toolbarDefaultItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] {
        [.flexibleSpace] + (ClassicUI.isEnabled ? [Self.classicID] : [])      // off: no Classic Settings item
    }

    func toolbar(_ toolbar: NSToolbar, itemForItemIdentifier itemIdentifier: NSToolbarItem.Identifier,
                 willBeInsertedIntoToolbar flag: Bool) -> NSToolbarItem? {
        guard itemIdentifier == Self.classicID else { return nil }
        let item = NSToolbarItem(itemIdentifier: itemIdentifier)
        item.label = "Classic Settings…"
        item.paletteLabel = "Classic Settings"
        item.toolTip = "Open the old web settings (until this window is complete)"
        item.image = NSImage(systemSymbolName: "rectangle.on.rectangle", accessibilityDescription: "Classic Settings")
        item.target = self
        item.action = #selector(classicAction(_:))
        return item
    }
}

/// The web settings page in its own window (what ⌘, used to open).
@MainActor
final class ClassicSettingsWindowController: NSObject, NSWindowDelegate {
    private let pane = WebPane(messages: ["app", "layout"])
    private let window: NSWindow

    init(appDelegate: AppDelegate) {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 980, height: 700),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        super.init()
        pane.waitingHTML = waitingPageHTML
        pane.retryAfter = 2
        pane.onMessage = { [weak appDelegate, weak pane] name, d in
            guard name == "app", let pane else { return }
            appDelegate?.appMessage(d, from: pane)
        }
        pane.onLoaded = { [weak appDelegate, weak pane] in
            guard let pane else { return }
            appDelegate?.sendAppInfo(to: pane)
        }
        window.title = "Classic Settings"
        window.isReleasedWhenClosed = false
        window.tabbingMode = .disallowed
        window.contentView = pane.web
        window.setFrameAutosaveName("MySearchSettings")
        if window.frame.origin == .zero { window.center() }
    }

    /// Opens the window on the web settings page (loading it the first time), at `section` when given.
    func show(section: String? = nil) {
        if pane.web.url == nil || pane.authFailed { pane.load(MSEClient.shared.pageURL()) }
        let arg = section.map { js($0) } ?? "undefined"
        pane.run("window.showSettings && showSettings(\(arg))")
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }
}
