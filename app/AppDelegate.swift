// The app: it owns the main window, the Quick Search panel, the menu-bar item, the hot keys and the Settings
// window, and it handles the mysearch:// links, the menus, and the bridge from the web pages (the `app` message:
// the Quick Search shortcut and the login item).
//
// Shortcuts (Carbon hot keys, no accessibility permission needed):
//   ⌃⌥Space  show or hide the main window
//   ⌥Space   Quick Search (default; Settings › Mac app changes it)
//
// Links: mysearch://search?q=…&c=…&doc=…&page=…  and  mysearch://mini?q=…  (see openURL below).

import Cocoa
import ServiceManagement
import Carbon.HIToolbox

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    /// nil in normal runs; set by --snapshot (see Snapshot.swift). A snapshot run registers nothing global.
    let snapshot: SnapshotOptions?

    /// Every open main window (each a tab in the "MySearchMain" group or a separate window), in creation order.
    /// WindowTabs.swift creates and releases them.
    var mainControllers: [MainWindowController] = []
    /// The main window the app acts on: the key one, else the frontmost, else the last one. If none is open, a new
    /// window opens at the home folder (menu items and links still work after the last window was closed).
    var mainWindow: MainWindowController {
        if let current = currentMainController() { return current }
        return makeMainWindow(startFolder: FileManager.default.homeDirectoryForCurrentUser, viewStyle: .list,
                              primary: mainControllers.isEmpty)
    }
    private var quick: QuickSearchController!
    private var status: StatusItemController!
    private var settings: SettingsWindowController?
    /// The classic (old web UI) window, created on first use and kept until the native app replaces it.
    private var classic: ClassicWindowController?
    private let hotKeys = HotKeyCenter()
    private var quickKey = HotKey.load()
    private var quickMenuItems: [NSMenuItem] = []     // the View menu's Quick Search item (the status item keeps its own)

    init(snapshot: SnapshotOptions?) {
        self.snapshot = snapshot
        super.init()
    }

    func applicationDidFinishLaunching(_ note: Notification) {
        NSWindow.allowsAutomaticWindowTabbing = false
        buildMenu()
        if let snapshot {
            runSnapshot(snapshot)
            return
        }
        EditorRouter.install()   // Markdown reader and code editor (EditorRouter.swift)
        // The main windows first, so the app shows at once; the service may still be starting (the pages retry).
        // They are the tabs saved last time, or one window at the home folder (WindowTabs.swift).
        openRestoredWindows()
        NotificationCenter.default.addObserver(self, selector: #selector(mainTabsDidChange(_:)),
                                               name: .mainTabsChanged, object: nil)

        quick = QuickSearchController()
        quick.onOpenInApp = { [weak self] o in
            guard let self else { return }
            self.mainWindow.openFromQuickSearch(o)
            self.showMain(focus: false)
        }
        quick.start()

        status = StatusItemController()
        status.onQuick = { [weak self] in self?.quick.toggle() }
        status.onOpen = { [weak self] in self?.showMain(focus: false) }
        status.onSync = { [weak self] in self?.syncNow() }
        status.onSettings = { [weak self] in self?.openSettings() }
        status.onClassic = { [weak self] in self?.showClassic() }
        status.install(quickKey: quickKey)

        hotKeys.onMain = { [weak self] in self?.toggleMain() }
        hotKeys.onQuick = { [weak self] in self?.quick.toggle() }
        hotKeys.install()
        if !hotKeys.registerAll(quick: quickKey) {
            NSLog("MySearch: Quick Search shortcut \(quickKey.label) is taken by another app")
        }

        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationWillTerminate(_ note: Notification) {
        saveOpenTabs()
    }


    // MARK: showing the main window

    func showMain(focus: Bool) {
        NSApp.activate(ignoringOtherApps: true)
        mainWindow.show()
        if focus { mainWindow.focusSearch() }
    }

    /// ⌃⌥Space: hide the app when its main window is the key window, otherwise show and focus the search.
    func toggleMain() {
        if NSApp.isActive && (currentMainController()?.window.isKeyWindow ?? false) { NSApp.hide(nil) } else { showMain(focus: true) }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showMain(focus: false)
        return true
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    // MARK: links (mysearch://)

    func application(_ application: NSApplication, open urls: [URL]) {
        for url in urls { openURL(url) }
    }

    /// mysearch://search?q=…&c=…&doc=…&page=…   the main window at that query, scope and document
    /// mysearch://mini?q=…                      Quick Search with that query
    /// Only q, c, doc and page are read; the page validates them again and checks c against the known folders.
    func openURL(_ url: URL) {
        guard url.scheme?.lowercased() == "mysearch",
              let parts = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return }
        let target = (parts.host ?? parts.path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))).lowercased()
        if target == "classic" {
            showClassic()
            return
        }
        // mysearch://search|ask|open|terminal?path=… (the Finder extension; Launchers.swift)
        if handleFolderLink(target: target, items: parts.queryItems ?? []) { return }
        var keep = URLComponents()
        var items: [URLQueryItem] = []
        for item in parts.queryItems ?? [] {
            guard let v = item.value else { continue }
            switch item.name {
            case "q" where v.count <= 500: items.append(URLQueryItem(name: "q", value: v))
            case "c" where v.range(of: #"^[\w\-,]{1,400}$"#, options: .regularExpression) != nil: items.append(URLQueryItem(name: "c", value: v))
            case "doc" where v.range(of: #"^#?[0-9A-Fa-f]{6,16}$"#, options: .regularExpression) != nil: items.append(URLQueryItem(name: "doc", value: v))
            case "page" where v.range(of: #"^[0-9]{1,6}$"#, options: .regularExpression) != nil: items.append(URLQueryItem(name: "page", value: v))
            default: break
            }
        }
        keep.queryItems = items
        if target == "mini" {
            quick.show(query: items.first { $0.name == "q" }?.value ?? "")
            return
        }
        guard target == "search" || target.isEmpty else { return }
        // URLComponents leaves "+" unescaped, and the page's URLSearchParams would read it as a space ("C++" → "C  ").
        let query = keep.percentEncodedQuery.map { "?" + $0.replacingOccurrences(of: "+", with: "%2B") } ?? ""
        showMain(focus: false)
        mainWindow.openWebSearch(query: query)
    }

    // MARK: the web bridge (`app` messages, from the main pane and the Settings window)

    /// Settings › Mac app: the Quick Search shortcut and the login item.
    func appMessage(_ d: [String: Any], from pane: WebPane) {
        switch d["type"] as? String {
        case "pauseHotkeys":
            hotKeys.unregisterAll()
        case "resumeHotkeys":
            hotKeys.registerAll(quick: quickKey)
        case "setQuickKey":
            setQuickKey(d, from: pane)
        case "setLogin":
            let on = (d["enabled"] as? Bool) ?? false
            do {
                if on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
                sendAppInfo(to: pane)
            } catch {
                sendAppInfo(to: pane, error: "macOS refused: \(error.localizedDescription)")
            }
        default: break
        }
    }

    func setQuickKey(_ d: [String: Any], from pane: WebPane) {
        let reset = (d["reset"] as? Bool) == true
        var key = HotKey.quickDefault
        if !reset {
            guard let code = (d["code"] as? String).flatMap({ HotKey.codes[$0] }) else {
                hotKeys.registerAll(quick: quickKey)
                return sendAppInfo(to: pane, error: "That key cannot be used for a shortcut.")
            }
            var mods: UInt32 = 0
            if (d["cmd"] as? Bool) == true { mods |= UInt32(cmdKey) }
            if (d["ctrl"] as? Bool) == true { mods |= UInt32(controlKey) }
            if (d["alt"] as? Bool) == true { mods |= UInt32(optionKey) }
            if (d["shift"] as? Bool) == true { mods |= UInt32(shiftKey) }
            key = HotKey(code: UInt32(code), mods: mods)
            let refusal: String?
            if mods & UInt32(cmdKey | controlKey | optionKey) == 0 && !key.isFunctionKey {
                refusal = "Add ⌘, ⌃ or ⌥, so the shortcut does not take over normal typing."
            } else if key == HotKey.main {
                refusal = "\(key.label) already shows the main window."
            } else if HotKey.reserved.contains(key) {
                refusal = "\(key.label) belongs to macOS or another app you use."
            } else {
                refusal = nil
            }
            if let refusal {
                hotKeys.registerAll(quick: quickKey)
                return sendAppInfo(to: pane, error: refusal)
            }
        }
        let old = quickKey
        quickKey = key
        hotKeys.unregisterAll()
        if hotKeys.registerAll(quick: key) {
            if reset {
                UserDefaults.standard.removeObject(forKey: "quickKeyCode")
                UserDefaults.standard.removeObject(forKey: "quickKeyMods")
            } else {
                key.save()
            }
            status.setQuickKey(key, extraItems: quickMenuItems)
            sendAppInfo(to: pane, message: "Saved. Press \(key.label) anywhere.")
        } else {
            quickKey = old
            hotKeys.registerAll(quick: old)
            sendAppInfo(to: pane, error: "\(key.label) is taken by another app. Try another.")
        }
    }

    func loginState() -> String {
        switch SMAppService.mainApp.status {
        case .enabled: return "on"
        case .requiresApproval: return "approval"
        case .notFound: return "unavailable"
        default: return "off"
        }
    }

    /// Tells a page the current shortcuts and login state (and the outcome of the last action).
    func sendAppInfo(to pane: WebPane, message: String? = nil, error: String? = nil) {
        var d: [String: Any] = ["quickKey": quickKey.label, "mainKey": HotKey.main.label, "login": loginState()]
        if let message { d["message"] = message }
        if let error { d["error"] = error }
        pane.run("window.appInfo && appInfo(\(js(d)))")
    }

    /// Runs the service's sync (the web UI's syncAll). The status line says so if the service does not answer.
    func syncNow() {
        mainWindow.webPane.web.evaluateJavaScript("window.syncAll ? syncAll() : 'not loaded'") { [weak self] r, _ in
            if (r as? String) != "ok" { self?.status.showSyncFailed() }
        }
    }

    func openSettings() {
        if settings == nil { settings = SettingsWindowController(appDelegate: self) }
        settings?.show()
    }

    /// Opens the classic window (the full old web UI) or brings it forward. Independent of the main window.
    func showClassic() {
        if classic == nil { classic = ClassicWindowController() }
        classic?.show()
    }

    @objc func openClassicWindow(_ sender: Any?) { showClassic() }
    @objc private func openClassicAction() { showClassic() }
    /// The File menu's "New Group…" (⌘⌥N). Another agent observes this notification; nothing here implements groups.
    @objc private func newGroupAction() {
        NotificationCenter.default.post(name: Notification.Name("MSENewGroup"), object: nil)
    }

    // MARK: menus (the Edit menu is required for copy/paste inside WKWebView)

    func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem(); main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About MySearch", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Folders & Settings…", action: #selector(openSettingsAction), keyEquivalent: ",").target = self
        appMenu.addItem(.separator())
        appMenu.addItem(classicMenuItem(keyed: false))          // ⌘⇧0 lives in the View menu only (one key, one item)
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide MySearch", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "Quit MySearch", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu

        let fileItem = NSMenuItem(); main.addItem(fileItem)
        let file = NSMenu(title: "File")
        file.addItem(menuItem("New Tab", #selector(newTabAction), "t"))
        file.addItem(menuItem("New Window", #selector(newWindowAction), "n"))
        file.addItem(.separator())
        file.addItem(menuItem("Get Info", #selector(getInfoAction), "i"))
        file.addItem(menuItem("Quick Look", #selector(quickLookAction), "y"))
        file.addItem(Launchers.makeFileMenuItem())          // Open in… (Terminal, VS Code, Cursor, Claude Code, Codex)
        file.addItem(.separator())
        let newGroup = menuItem("New Group…", #selector(newGroupAction), "n")
        newGroup.keyEquivalentModifierMask = [.command, .option]
        file.addItem(newGroup)
        FileMenuRouter.shared.addItems(to: file)            // New Folder, Duplicate, Move to Trash, ... (FileOps.swift)
        fileItem.submenu = file

        let editItem = NSMenuItem(); main.addItem(editItem)
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit

        let viewItem = NSMenuItem(); main.addItem(viewItem)
        let view = NSMenu(title: "View")
        view.addItem(menuItem("Icons", #selector(viewIcons), "1"))
        view.addItem(menuItem("List", #selector(viewList), "2"))
        view.addItem(menuItem("Columns", #selector(viewColumns), "3"))
        let splitItem = NSMenuItem(title: "Split Pane", action: #selector(splitPaneAction), keyEquivalent: "\\")   // SplitPanes.swift
        splitItem.keyEquivalentModifierMask = [.command, .option]
        splitItem.target = self
        view.addItem(splitItem)
        view.addItem(.separator())
        view.addItem(menuItem("Find", #selector(searchAction), "f"))
        view.addItem(menuItem("Search", #selector(searchAction), "k"))
        let quickItem = NSMenuItem(title: "Quick Search", action: #selector(quickAction), keyEquivalent: "")
        quickItem.target = self
        quickMenuItems.append(quickItem)
        view.addItem(quickItem)
        let paletteItem = NSMenuItem(title: "Command Palette", action: #selector(CommandPalette.showFromMenu(_:)), keyEquivalent: "p")
        paletteItem.target = CommandPalette.shared      // app/CommandPalette.swift
        view.addItem(paletteItem)
        view.addItem(menuItem("Reload", #selector(reloadAction), "r"))
        view.addItem(.separator())
        view.addItem(classicMenuItem())
        viewItem.submenu = view

        let goItem = NSMenuItem(); main.addItem(goItem)
        let go = NSMenu(title: "Go")
        go.addItem(menuItem("Back", #selector(backAction), "["))
        go.addItem(menuItem("Forward", #selector(forwardAction), "]"))
        let up = String(Character(UnicodeScalar(NSUpArrowFunctionKey)!))
        let enclosing = NSMenuItem(title: "Enclosing Folder", action: #selector(enclosingAction), keyEquivalent: up)
        enclosing.target = self
        go.addItem(enclosing)
        go.addItem(.separator())
        go.addItem(menuItem("Home", #selector(homeAction), "H", shift: true))
        goItem.submenu = go

        let winItem = NSMenuItem(); main.addItem(winItem)
        let win = NSMenu(title: "Window")
        win.addItem(withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        win.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        win.addItem(withTitle: "Zoom", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")
        win.addItem(.separator())
        win.addItem(withTitle: "Show Tab Bar", action: #selector(NSWindow.toggleTabBar(_:)), keyEquivalent: "")
        let allTabs = NSMenuItem(title: "Show All Tabs", action: #selector(NSWindow.toggleTabOverview(_:)), keyEquivalent: "\\")
        allTabs.keyEquivalentModifierMask = [.command, .control]
        win.addItem(allTabs)
        winItem.submenu = win
        NSApp.windowsMenu = win

        let helpItem = NSMenuItem(); main.addItem(helpItem)
        let help = NSMenu(title: "Help")
        help.addItem(menuItem("Keyboard Shortcuts", #selector(showShortcuts), "/"))
        helpItem.submenu = help
        NSApp.helpMenu = help
        NSApp.mainMenu = main
    }

    private func menuItem(_ title: String, _ action: Selector, _ key: String, shift: Bool = false) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
        item.target = self
        if shift { item.keyEquivalentModifierMask = [.command, .shift] }
        return item
    }

    @objc private func openSettingsAction() { openSettings() }
    @objc private func viewIcons() { mainWindow.setViewStyle(.icons); showMain(focus: false) }
    @objc private func viewList() { mainWindow.setViewStyle(.list); showMain(focus: false) }
    @objc private func viewColumns() { mainWindow.setViewStyle(.columns); showMain(focus: false) }
    @objc private func splitPaneAction() { mainWindow.toggleSplitPane() }
    @objc private func getInfoAction() { mainWindow.getInfo() }
    @objc private func quickLookAction() { mainWindow.quickLookSelection() }
    @objc private func searchAction() { showMain(focus: true) }
    @objc private func quickAction() { quick.toggle() }
    /// ⌘R reloads the classic window when it is the key window, otherwise the main window.
    @objc private func reloadAction() {
        if let classic, classic.isKey { classic.reload() } else { mainWindow.reload() }
    }

    /// "Open Classic MySearch" (⌘⇧0): the one item shared by the App menu and the View menu.
    private func classicMenuItem(keyed: Bool = true) -> NSMenuItem {
        let item = NSMenuItem(title: "Open Classic MySearch", action: #selector(openClassicAction), keyEquivalent: keyed ? "0" : "")
        if keyed { item.keyEquivalentModifierMask = [.command, .shift] }
        item.target = self
        return item
    }
    @objc private func backAction() { mainWindow.goBack() }
    @objc private func forwardAction() { mainWindow.goForward() }
    @objc private func enclosingAction() { mainWindow.goEnclosingFolder() }
    @objc private func homeAction() { mainWindow.goHome() }
    @objc private func showShortcuts() {
        showMain(focus: false)
        mainWindow.webPane.run("window.showHelp && showHelp()")
    }
}
