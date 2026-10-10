// Native window tabbing for the main window, like the Finder: every main window is a member of the "MySearchMain"
// tab group (tabbingMode .preferred), so ⌘T and the tab bar's "+" open a tab at the current folder, dragging a tab
// out makes a window and dragging it back joins the group. ⌘N opens a separate window. The open tabs (folder and
// view) are saved in UserDefaults and reopened at launch. The shared panels (Quick Search, status item, hot keys,
// Classic and Settings windows) stay single and live in AppDelegate.swift.

import Cocoa

/// The class of every main window (not the snapshot window). Its "+" button and File › New Tab both send
/// newWindowForTab:, which creates a tab in this window's group.
@MainActor
final class MainTabWindow: NSWindow {
    override func newWindowForTab(_ sender: Any?) {
        (NSApp.delegate as? AppDelegate)?.newTab(from: self)
    }
}

extension Notification.Name {
    /// Posted by a main window when its folder, view or mode changes, so the open tabs can be saved.
    static let mainTabsChanged = Notification.Name("MySearchMainTabsChanged")
}

/// The open tabs, kept in UserDefaults as an array of { "path": folder, "view": 0 icons | 1 list | 2 columns }.
enum TabStore {
    private static let key = "MSEOpenTabs"

    struct Tab {
        let folder: URL
        let view: MainWindowController.ViewStyle
        // Split Pane (SplitPanes.swift): the second pane. Saved tabs from before the split have none of these.
        var split = false
        var rightFolder: URL? = nil
        var rightView: MainWindowController.ViewStyle = .list
        var focusRight = false
    }

    /// The saved tabs whose folders still exist, in the order they were saved.
    static func load() -> [Tab] {
        guard let items = UserDefaults.standard.array(forKey: key) as? [[String: Any]] else { return [] }
        var tabs: [Tab] = []
        for item in items {
            guard let path = item["path"] as? String else { continue }
            var isDir: ObjCBool = false
            guard FileManager.default.fileExists(atPath: path, isDirectory: &isDir), isDir.boolValue else { continue }
            let view = MainWindowController.ViewStyle(rawValue: (item["view"] as? Int) ?? 1) ?? .list
            tabs.append(tab(from: item, path: path, view: view))
        }
        return tabs
    }

    static func save(_ tabs: [Tab]) {
        let items: [[String: Any]] = tabs.map(dictionary(for:))
        UserDefaults.standard.set(items, forKey: key)
    }

    /// A saved item as a tab. The split keys are optional; a right folder that no longer exists falls back to the left one.
    private static func tab(from item: [String: Any], path: String, view: MainWindowController.ViewStyle) -> Tab {
        var tab = Tab(folder: URL(fileURLWithPath: path), view: view)
        guard (item["split"] as? Bool) == true else { return tab }
        tab.split = true
        var isDir: ObjCBool = false
        if let right = item["rightPath"] as? String, FileManager.default.fileExists(atPath: right, isDirectory: &isDir), isDir.boolValue {
            tab.rightFolder = URL(fileURLWithPath: right)
        }
        tab.rightView = MainWindowController.ViewStyle(rawValue: (item["rightView"] as? Int) ?? 1) ?? .list
        tab.focusRight = (item["focus"] as? Int) == 1
        return tab
    }

    private static func dictionary(for tab: Tab) -> [String: Any] {
        var item: [String: Any] = ["path": tab.folder.path, "view": tab.view.rawValue]
        guard tab.split else { return item }
        item["split"] = true
        item["rightPath"] = (tab.rightFolder ?? tab.folder).path
        item["rightView"] = tab.rightView.rawValue
        item["focus"] = tab.focusRight ? 1 : 0
        return item
    }
}

// MARK: - the app's main windows (AppDelegate keeps the list in `mainControllers`)

extension AppDelegate {
    /// The key main window's controller, else the frontmost one, else the last one opened. nil only when none is open.
    func currentMainController() -> MainWindowController? {
        if let key = mainControllers.first(where: { $0.window.isKeyWindow }) { return key }
        for w in NSApp.orderedWindows {
            if let c = mainControllers.first(where: { $0.window === w }) { return c }
        }
        return mainControllers.last
    }

    /// The main window controller that owns `window`, if it is one of ours.
    func mainController(for window: NSWindow?) -> MainWindowController? {
        guard let window else { return nil }
        return mainControllers.first { $0.window === window }
    }

    /// Creates a main window with its own web UI, navigator and views, registers it and starts it. It is not shown.
    /// Only the primary window keeps the saved frame; the others are placed beside the key window.
    @discardableResult
    func makeMainWindow(startFolder: URL, viewStyle: MainWindowController.ViewStyle, primary: Bool) -> MainWindowController {
        let mc = MainWindowController(frame: NSRect(x: 0, y: 0, width: 1240, height: 820), snapshot: false,
                                      startFolder: startFolder, viewStyle: viewStyle, primary: primary)
        configureMainWindow(mc)
        mainControllers.append(mc)
        NotificationCenter.default.addObserver(self, selector: #selector(mainWindowWillClose(_:)),
                                               name: NSWindow.willCloseNotification, object: mc.window)
        mc.start()
        return mc
    }

    /// The per-window hooks that used to be set once for the single main window. Each window's own web pane
    /// reports to the app, so a message from one window's Ask or Search page is answered in that window.
    func configureMainWindow(_ mc: MainWindowController) {
        mc.webPane.onMessage = { [weak self, weak mc] name, d in
            guard let self, let mc, name == "app" else { return }
            self.appMessage(d, from: mc.webPane)
        }
        mc.webPane.onLoaded = { [weak self, weak mc] in
            guard let self, let mc else { return }
            self.sendAppInfo(to: mc.webPane)
        }
        mc.webPane.waitingHTML = waitingPageHTML
        mc.webPane.retryAfter = 2
        mc.content.onPathClick = { [weak mc] url in mc?.openFolder(url) }
    }

    /// Launch: reopens the tabs saved last time (folder and view), or a single window at the home folder.
    /// The first saved tab is the window; the others are added to its tab group, in order.
    func openRestoredWindows() {
        let saved = TabStore.load()
        let first = makeMainWindow(startFolder: saved.first?.folder ?? FileManager.default.homeDirectoryForCurrentUser,
                                   viewStyle: saved.first?.view ?? .list, primary: true)
        first.restorePanes(from: saved.first)
        first.show()
        var anchor = first.window
        for tab in saved.dropFirst() {
            let mc = makeMainWindow(startFolder: tab.folder, viewStyle: tab.view, primary: false)
            mc.restorePanes(from: tab)
            anchor.addTabbedWindow(mc.window, ordered: .above)
            anchor = mc.window
        }
        first.show()
        saveOpenTabs()
    }

    /// ⌘T, the tab bar's "+" and File › New Tab: a new tab in the group of `anchor` (or of the current main window),
    /// starting at that window's folder and in its view.
    func newTab(from anchor: NSWindow?) {
        guard let source = mainController(for: anchor) ?? currentMainController() else { return }
        let mc = makeMainWindow(startFolder: source.navigator.current, viewStyle: source.viewStyle, primary: false)
        source.window.addTabbedWindow(mc.window, ordered: .above)
        mc.show()
        saveOpenTabs()
    }

    /// ⌘N: a separate main window (not a tab), starting at the current window's folder.
    @objc func newWindowAction() {
        let source = currentMainController()
        let mc = makeMainWindow(startFolder: source?.navigator.current ?? FileManager.default.homeDirectoryForCurrentUser,
                                viewStyle: source?.viewStyle ?? .list, primary: false)
        mc.show()
        saveOpenTabs()
    }

    /// ⌘T from the menu: a tab next to the key main window, or next to the current one.
    @objc func newTabAction() {
        newTab(from: NSApp.keyWindow)
    }

    /// Saves every open main window in tab order (each group in its own order, groups in creation order).
    /// `closing` is left out, for a window that is being closed.
    func saveOpenTabs(excluding closing: NSWindow? = nil) {
        var ordered: [MainWindowController] = []
        for c in mainControllers {
            for w in c.window.tabbedWindows ?? [c.window] {
                if let m = mainController(for: w), !ordered.contains(where: { $0 === m }) { ordered.append(m) }
            }
            if !ordered.contains(where: { $0 === c }) { ordered.append(c) }
        }
        let tabs = ordered.filter { $0.window !== closing }
            .map { $0.savedTab }
        TabStore.save(tabs)
    }

    /// A main window closed: its tab is no longer saved, and its controller is released once AppKit is done with it.
    @objc func mainWindowWillClose(_ note: Notification) {
        guard let window = note.object as? NSWindow else { return }
        saveOpenTabs(excluding: window)
        NotificationCenter.default.removeObserver(self, name: NSWindow.willCloseNotification, object: window)
        DispatchQueue.main.async { [weak self] in
            self?.mainControllers.removeAll { $0.window === window }
        }
    }

    /// A main window's folder, view or mode changed.
    @objc func mainTabsDidChange(_ note: Notification) {
        saveOpenTabs()
    }
}
