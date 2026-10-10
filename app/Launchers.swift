// "Open in…" launchers: Terminal, iTerm, VS Code, Cursor, Claude Code and Codex, for the selected or current folder.
//
// Where they appear: the toolbar's "Open in…" menu, File › Open in…, and any context menu through
// `Launchers.menuItems(for:)` (public entry point for the folder views and the search view).
// The Finder extension asks the app to do the same through mysearch://terminal?path=… (see handleFolderLink below).
//
// Parts of the detection and launch code are adapted from FinderFlow
// (https://github.com/Gtarafdar/FinderFlow, tag v1.5.2, MIT License, Copyright (c) 2026 Gobinda Tarafdar):
// FinderFlow/QuickActionsToolbar.swift (app and CLI detection, the openIn* helpers) and
// FinderFlow/FolderCreationService.swift (shell and AppleScript quoting).
// The MIT notice is in THIRD_PARTY_NOTICES.md.

import Cocoa
import SwiftUI

// MARK: - quoting (the injection-safe part; keep these two functions the only way a path reaches a script)

enum LauncherQuoting {
    /// Single-quotes a path for a POSIX shell: safe against $, `, !, spaces and quotes.
    static func shellQuoted(_ path: String) -> String {
        "'" + path.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }

    /// Escapes `s` and wraps it in double quotes, so it is one AppleScript string literal: backslashes first, then quotes.
    /// File names may contain " and \, and an unescaped one would let a crafted name inject AppleScript.
    static func appleScriptStringLiteral(_ s: String) -> String {
        "\"" + s.replacingOccurrences(of: "\\", with: "\\\\")
                .replacingOccurrences(of: "\"", with: "\\\"") + "\""
    }

    /// True when the path has no control characters (a newline or NUL has no place in a folder we are asked to open).
    static func isPlain(_ path: String) -> Bool {
        !path.isEmpty && path.utf8.count <= 4096 && !path.unicodeScalars.contains { $0.value < 0x20 || $0.value == 0x7f }
    }
}

// MARK: - the tools

struct LauncherEntry: Codable, Equatable {
    var app: String?        // path of the .app
    var cli: String?        // path of the command-line tool
}

@MainActor
enum Launchers {
    enum Tool: String, CaseIterable, Codable {
        case terminal, iterm, vscode, cursor, claudeCode, codex

        var title: String {
            switch self {
            case .terminal: return "Terminal"
            case .iterm: return "iTerm"
            case .vscode: return "Visual Studio Code"
            case .cursor: return "Cursor"
            case .claudeCode: return "Claude Code"
            case .codex: return "Codex"
            }
        }
        var bundleIDs: [String] {
            switch self {
            case .terminal: return ["com.apple.Terminal"]
            case .iterm: return ["com.googlecode.iterm2"]
            case .vscode: return ["com.microsoft.VSCode"]
            case .cursor: return ["com.todesktop.230313mzl4w4u92", "com.cursor.macos", "com.cursor.cursor"]
            case .claudeCode: return []
            case .codex: return ["com.openai.codex", "com.openai.codex-desktop"]
            }
        }
        var appPaths: [String] {
            switch self {
            case .terminal: return ["/System/Applications/Utilities/Terminal.app", "/Applications/Utilities/Terminal.app"]
            case .iterm: return ["/Applications/iTerm.app"]
            case .vscode: return ["/Applications/Visual Studio Code.app"]
            case .cursor: return ["/Applications/Cursor.app"]
            case .claudeCode: return []
            case .codex: return ["/Applications/Codex.app", "/Applications/OpenAI Codex.app"]
            }
        }
        /// The command name looked up with a login shell's `which`, and the fixed places checked first.
        var cliName: String? {
            switch self {
            case .claudeCode: return "claude"
            case .codex: return "codex"
            default: return nil
            }
        }
        var cliPaths: [String] {
            let home = NSHomeDirectory()
            switch self {
            case .claudeCode:
                return ["/usr/local/bin/claude", "/opt/homebrew/bin/claude", "\(home)/.local/bin/claude",
                        "\(home)/.claude/local/claude", "\(home)/.volta/bin/claude"]
            case .codex:
                return ["/usr/local/bin/codex", "/opt/homebrew/bin/codex", "\(home)/.local/bin/codex"]
            default: return []
            }
        }
        var symbol: String {
            switch self {
            case .terminal, .iterm: return "terminal"
            case .vscode, .cursor: return "chevron.left.forwardslash.chevron.right"
            case .claudeCode: return "brain.head.profile"
            case .codex: return "terminal.fill"
            }
        }
    }

    // MARK: detection (cached for 6 hours)

    static let cacheKey = "launchers.cache.v1"
    static let cacheLifetime: TimeInterval = 6 * 3600

    /// Looks for the apps and CLIs. `deep` adds the login-shell `which` lookups (slow: run it off the main thread).
    nonisolated static func detect(deep: Bool) -> [Tool: LauncherEntry] {
        var out: [Tool: LauncherEntry] = [:]
        let fm = FileManager.default
        for tool in Tool.allCases {
            var e = LauncherEntry()
            for id in tool.bundleIDs {
                if let u = NSWorkspace.shared.urlForApplication(withBundleIdentifier: id) { e.app = u.path; break }
            }
            if e.app == nil { e.app = tool.appPaths.first { fm.fileExists(atPath: $0) } }
            if let fixed = tool.cliPaths.first(where: { fm.isExecutableFile(atPath: $0) }) {
                e.cli = fixed
            } else if deep, let name = tool.cliName {
                e.cli = whichCLI(name)
            }
            out[tool] = e
        }
        return out
    }

    /// `which <name>` in a login shell (handles nvm, Homebrew, npm prefixes); gives up after 4 seconds.
    nonisolated private static func whichCLI(_ name: String) -> String? {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/zsh")
        p.arguments = ["-l", "-c", "which \(name) 2>/dev/null"]       // `name` is one of our own constants
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = FileHandle.nullDevice
        p.standardInput = FileHandle.nullDevice
        let done = DispatchSemaphore(value: 0)
        p.terminationHandler = { _ in done.signal() }
        guard (try? p.run()) != nil else { return nil }
        if done.wait(timeout: .now() + 4) == .timedOut { p.terminate(); return nil }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        let line = String(data: data, encoding: .utf8)?
            .split(whereSeparator: \.isNewline).last.map(String.init)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        guard line.hasPrefix("/"), FileManager.default.isExecutableFile(atPath: line) else { return nil }
        return line
    }

    /// Tools that can be launched, in menu order.
    static var available: [Tool] {
        Tool.allCases.filter { isAvailable($0) }
    }

    static func isAvailable(_ tool: Tool) -> Bool {
        guard let e = LauncherStore.shared.entries[tool] else { return false }
        switch tool {
        case .claudeCode: return e.cli != nil                     // a Terminal command only: no CLI, no entry
        case .codex: return e.app != nil || e.cli != nil
        default: return e.app != nil
        }
    }

    static func icon(_ tool: Tool) -> NSImage {
        let e = LauncherStore.shared.entries[tool]
        var img: NSImage?
        if let app = e?.app { img = NSWorkspace.shared.icon(forFile: app) }
        if img == nil, tool == .claudeCode {
            for id in ["com.anthropic.claudefordesktop", "com.anthropic.claude-mac", "com.anthropic.claude"] {
                if let u = NSWorkspace.shared.urlForApplication(withBundleIdentifier: id) { img = NSWorkspace.shared.icon(forFile: u.path); break }
            }
        }
        let base = img ?? NSImage(systemSymbolName: tool.symbol, accessibilityDescription: tool.title) ?? NSImage()
        let small = (base.copy() as? NSImage) ?? base
        small.size = NSSize(width: 16, height: 16)
        return small
    }

    // MARK: folders

    /// The folder an item stands for: a folder itself, a file's parent.
    static func folder(for url: URL) -> URL {
        let u = url.standardizedFileURL
        let isDir = (try? u.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) ?? u.hasDirectoryPath
        return isDir ? u : u.deletingLastPathComponent()
    }

    static func folders(for urls: [URL]) -> [URL] {
        var seen = Set<String>(), out: [URL] = []
        for u in urls {
            let f = folder(for: u)
            if seen.insert(f.path).inserted { out.append(f) }
        }
        return out
    }

    // MARK: opening

    /// Opens `tool` on the folders (a file counts as its folder). Terminals and CLI tools use the first folder only.
    static func open(_ tool: Tool, urls: [URL]) {
        let list = folders(for: urls)
        guard let first = list.first, LauncherQuoting.isPlain(first.path),
              let e = LauncherStore.shared.entries[tool] else { return }
        switch tool {
        case .terminal, .iterm:
            if let app = e.app { openWithApp(app, [first]) }
        case .vscode, .cursor:
            if let app = e.app { openWithApp(app, list) }
        case .claudeCode:
            if let cli = e.cli { runInTerminal(folder: first, command: LauncherQuoting.shellQuoted(cli)) }
        case .codex:
            if let app = e.app { openWithApp(app, [first]) }
            else if let cli = e.cli { runInTerminal(folder: first, command: LauncherQuoting.shellQuoted(cli)) }
        }
    }

    /// Plain Terminal at a folder, with no AppleScript (the mysearch://terminal route and the Finder extension use this).
    static func openTerminal(at url: URL) {
        open(.terminal, urls: [url])
    }

    private static func openWithApp(_ app: String, _ urls: [URL]) {
        NSWorkspace.shared.open(urls, withApplicationAt: URL(fileURLWithPath: app), configuration: NSWorkspace.OpenConfiguration()) { _, error in
            if let error { NSLog("MySearch: could not open \(app): \(error.localizedDescription)") }
        }
    }

    /// New Terminal window: `cd '<folder>' && <command>`. The folder is shell-quoted, the whole line is one AppleScript literal.
    /// `command` must already be shell-safe (LauncherQuoting.shellQuoted of a path, or one of our constants).
    private static func runInTerminal(folder: URL, command: String) {
        let line = "cd \(LauncherQuoting.shellQuoted(folder.path)) && \(command)"
        let source = """
        tell application "Terminal"
            activate
            do script \(LauncherQuoting.appleScriptStringLiteral(line))
        end tell
        """
        var err: NSDictionary?
        NSAppleScript(source: source)?.executeAndReturnError(&err)
        if let err { NSLog("MySearch: Terminal script failed: \(err)") }
    }

    // MARK: menus

    /// The "Open in…" entry for a context menu: one item with a submenu of the installed tools, acting on `urls`
    /// (a file counts as its folder). Empty when no supported tool is installed or `urls` is empty.
    /// Other views call this from their context menus: `menu.items += Launchers.menuItems(for: urls)`.
    static func menuItems(for urls: [URL]) -> [NSMenuItem] {
        guard !urls.isEmpty, !available.isEmpty else { return [] }
        let item = NSMenuItem(title: "Open in…", action: nil, keyEquivalent: "")
        let sub = NSMenu(title: "Open in…")
        for tool in available { sub.addItem(toolItem(tool, urls: urls)) }
        item.submenu = sub
        return [item]
    }

    /// The File menu's "Open in…" item: its submenu is rebuilt each time it opens and acts on the current selection or folder.
    static func makeFileMenuItem() -> NSMenuItem {
        let item = NSMenuItem(title: "Open in…", action: nil, keyEquivalent: "")
        item.submenu = makeDynamicMenu()
        return item
    }

    /// A menu that fills itself with the installed tools when it opens (toolbar item and File menu).
    static func makeDynamicMenu() -> NSMenu {
        let menu = NSMenu(title: "Open in…")
        menu.delegate = LauncherMenuTarget.shared
        menu.autoenablesItems = false
        return menu
    }

    fileprivate static func toolItem(_ tool: Tool, urls: [URL]?) -> NSMenuItem {
        let it = NSMenuItem(title: tool.title, action: #selector(LauncherMenuTarget.run(_:)), keyEquivalent: "")
        it.target = LauncherMenuTarget.shared
        it.representedObject = LauncherRequest(tool: tool, urls: urls)
        it.image = icon(tool)
        return it
    }

    /// The selection of the main window (or its current folder), when an "Open in…" item has no urls of its own.
    static func currentTargets() -> [URL] {
        (NSApp.delegate as? AppDelegate)?.mainWindow.launcherTargets
            ?? [FileManager.default.homeDirectoryForCurrentUser]
    }
}

/// What a menu item carries: the tool, and the urls it was built for (nil = the main window's selection at click time).
final class LauncherRequest: NSObject {
    let tool: Launchers.Tool
    let urls: [URL]?
    init(tool: Launchers.Tool, urls: [URL]?) { self.tool = tool; self.urls = urls }
}

@MainActor
final class LauncherMenuTarget: NSObject, NSMenuDelegate {
    static let shared = LauncherMenuTarget()

    @objc func run(_ sender: NSMenuItem) {
        guard let r = sender.representedObject as? LauncherRequest else { return }
        Launchers.open(r.tool, urls: r.urls ?? Launchers.currentTargets())
    }

    @objc func refreshAction(_ sender: Any?) { LauncherStore.shared.refresh() }

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        let tools = Launchers.available
        if tools.isEmpty {
            let none = NSMenuItem(title: LauncherStore.shared.refreshing ? "Looking for apps…" : "No supported apps found", action: nil, keyEquivalent: "")
            none.isEnabled = false
            menu.addItem(none)
        }
        for tool in tools { menu.addItem(Launchers.toolItem(tool, urls: nil)) }
    }
}

/// The detected tools, observable for the Settings screen. Loads the cache, fills gaps at once with the fast checks,
/// and refreshes in the background when the cache is older than 6 hours.
@MainActor
final class LauncherStore: ObservableObject {
    static let shared = LauncherStore()

    @Published private(set) var entries: [Launchers.Tool: LauncherEntry] = [:]
    @Published private(set) var refreshing = false
    @Published private(set) var checkedAt: Date?

    private struct Cache: Codable { var at: Date; var entries: [String: LauncherEntry] }

    private init() {
        var stale = true
        if let data = UserDefaults.standard.data(forKey: Launchers.cacheKey),
           let c = try? JSONDecoder().decode(Cache.self, from: data) {
            for (k, v) in c.entries { if let t = Launchers.Tool(rawValue: k) { entries[t] = v } }
            checkedAt = c.at
            stale = Date().timeIntervalSince(c.at) > Launchers.cacheLifetime
        }
        if entries.isEmpty { entries = Launchers.detect(deep: false) }      // fast pass, so the first menu is not empty
        if stale { refresh() }
    }

    /// Detects again (including the login-shell lookups) off the main thread.
    func refresh() {
        guard !refreshing else { return }
        refreshing = true
        DispatchQueue.global(qos: .utility).async {
            let found = Launchers.detect(deep: true)
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    let store = LauncherStore.shared
                    store.entries = found
                    store.checkedAt = Date()
                    store.refreshing = false
                    let c = Cache(at: Date(), entries: Dictionary(uniqueKeysWithValues: found.map { ($0.key.rawValue, $0.value) }))
                    if let data = try? JSONEncoder().encode(c) { UserDefaults.standard.set(data, forKey: Launchers.cacheKey) }
                }
            }
        }
    }
}

// MARK: - folders that are indexed collections (read from the service's config.json)

enum LauncherCollections {
    /// (name, absolute path) of the enabled collections.
    static func all() -> [(name: String, path: String)] {
        let url = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/MySearch/config.json")
        guard let data = try? Data(contentsOf: url),
              let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let list = root["collections"] as? [[String: Any]] else { return [] }
        return list.compactMap { c in
            guard let n = c["name"] as? String, let p = c["path"] as? String, (c["enabled"] as? Bool) != false else { return nil }
            return (n, (p as NSString).expandingTildeInPath)
        }
    }

    /// The collection whose folder contains `folder` (the deepest one), with the path inside it ("" for its root).
    static func containing(_ folder: URL) -> (name: String, relative: String)? {
        let target = folder.standardizedFileURL.path
        var best: (name: String, root: String)?
        for c in all() {
            let root = URL(fileURLWithPath: c.path).standardizedFileURL.path
            guard target == root || target.hasPrefix(root.hasSuffix("/") ? root : root + "/") else { continue }
            if best == nil || root.count > best!.root.count { best = (c.name, root) }
        }
        guard let b = best else { return nil }
        let rel = target == b.root ? "" : String(target.dropFirst(b.root.count)).trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        return (b.name, rel)
    }
}

// MARK: - mysearch:// folder routes (Finder extension and others)

extension AppDelegate {
    /// mysearch://search?c=…&path=…   search here (a collection folder: Indexed scoped to it; otherwise file names in that folder)
    /// mysearch://ask?path=…          Ask about the folder (scoped to its collection when it is in one)
    /// mysearch://open?path=…         the main window shows that folder (a file: its folder, with the file selected)
    /// mysearch://terminal?path=…     Terminal at that folder
    /// Returns true when the link was one of these (search without a `path` is left to the existing route).
    /// The path must be absolute, exist, and contain no control characters; nothing else in the link is used to run anything.
    func handleFolderLink(target: String, items: [URLQueryItem]) -> Bool {
        guard ["search", "ask", "open", "terminal"].contains(target),
              let raw = items.first(where: { $0.name == "path" })?.value else { return false }
        guard raw.hasPrefix("/"), LauncherQuoting.isPlain(raw) else { return true }
        let item = URL(fileURLWithPath: raw).standardizedFileURL
        guard FileManager.default.fileExists(atPath: item.path) else { return true }
        let folder = Launchers.folder(for: item)
        switch target {
        case "terminal":
            Launchers.openTerminal(at: folder)
        case "open":
            showMain(focus: false)
            if folder == item { mainWindow.openFolder(item) } else { mainWindow.revealInFolderView(item) }
        case "ask":
            showMain(focus: false)
            mainWindow.askAbout(folder: folder)
        default:
            showMain(focus: false)
            mainWindow.searchHere(folder: folder)
        }
        return true
    }
}

extension MainWindowController {
    // searchHere(folder:) and launcherTargets live in MainWindowController.swift (they use its private state).

    /// A new Ask chat about a folder: scoped to its collection and subfolder when it is indexed, otherwise a draft that names it.
    func askAbout(folder: URL) {
        setMode(.ask)
        ask.ask(question: "")                               // a fresh chat with an empty draft
        if let c = LauncherCollections.containing(folder) {
            ask.model.setWhere(scope: c.name, path: c.relative)
        } else {
            ask.model.draft = "About the folder \"\(folder.lastPathComponent)\" (\(folder.path)): "
        }
    }
}
