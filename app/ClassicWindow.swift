// The classic window: the full old web UI (search, Ask, Settings, everything) in its own window, beside the native
// main window. It is kept until the native app has taken over every part of the web UI. Its frame is remembered
// (frame autosave), and ⌘R reloads it. Opened from the App and View menus, the menu-bar item, the toolbar's
// overflow menu and mysearch://classic.

import Cocoa

@MainActor
final class ClassicWindowController: NSObject, NSWindowDelegate {
    let pane = WebPane(messages: [])
    let window: NSWindow

    override init() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1100, height: 760),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        super.init()
        window.title = "Classic MySearch"
        window.isReleasedWhenClosed = false
        window.minSize = NSSize(width: 640, height: 420)
        window.delegate = self
        window.setFrameAutosaveName("MySearchClassicWindow")
        pane.waitingHTML = waitingPageHTML
        pane.retryAfter = 2
        window.contentView = pane.web
        pane.web.autoresizingMask = [.width, .height]
    }

    /// Shows the window, loading the old UI the first time (and again after a reload).
    func show() {
        if pane.retryURL == nil { reload() }
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }

    /// Loads the old web UI from the local service with the current token.
    func reload() {
        pane.load(MSEClient.shared.pageURL())
    }

    var isKey: Bool { window.isKeyWindow }
}
