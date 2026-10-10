// MySearch's Finder Sync extension: a "MySearch" submenu in Finder's right-click menu.
//   Search in MySearch here · Ask MySearch about this folder · Open in MySearch · Copy Path · Open in Terminal
//
// Adapted from FinderFlow (https://github.com/Gtarafdar/FinderFlow, tag v1.5.2, MIT License,
// Copyright (c) 2026 Gobinda Tarafdar): FinderFlowExtension/FinderSyncExtension.swift. The MIT notice is in
// THIRD_PARTY_NOTICES.md.
//
// The extension is sandboxed. It runs no scripts: every action except Copy Path is handed to the main app as a
// mysearch://…?path=… link (the app is not sandboxed and validates the path), so there is no AppleScript here.

import Cocoa
import FinderSync

@objc(MySearchFinderSync)
final class FinderSyncExtension: FIFinderSync {

    /// The user's real home folder (inside the sandbox, NSHomeDirectory() is the extension's container).
    private static var realHome: String {
        if let pw = getpwuid(getuid()), let dir = pw.pointee.pw_dir { return String(cString: dir) }
        return NSHomeDirectory()
    }

    /// (name, path) of the collections in the service's config.json. Read-only; empty when the sandbox cannot read it.
    private static func collections() -> [(name: String, path: String)] {
        let url = URL(fileURLWithPath: realHome).appendingPathComponent("Library/Application Support/MySearch/config.json")
        guard let data = try? Data(contentsOf: url),
              let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let list = root["collections"] as? [[String: Any]] else { return [] }
        return list.compactMap { c in
            guard let n = c["name"] as? String, let p = c["path"] as? String, (c["enabled"] as? Bool) != false else { return nil }
            return (n, p.hasPrefix("~") ? realHome + p.dropFirst() : p)
        }
    }

    private let known = FinderSyncExtension.collections()

    override init() {
        super.init()
        let home = URL(fileURLWithPath: FinderSyncExtension.realHome, isDirectory: true)
        var dirs: Set<URL> = [home]
        for sub in ["Desktop", "Documents", "Downloads"] { dirs.insert(home.appendingPathComponent(sub, isDirectory: true)) }
        for c in known { dirs.insert(URL(fileURLWithPath: c.path, isDirectory: true)) }
        FIFinderSyncController.default().directoryURLs = dirs      // the home folder alone when config.json is unreadable
    }

    // MARK: context menu

    override func menu(for menuKind: FIMenuKind) -> NSMenu {
        let menu = NSMenu(title: "")
        switch menuKind {
        case .contextualMenuForItems, .contextualMenuForContainer, .toolbarItemMenu:
            break
        default:
            return menu
        }
        let sub = NSMenu(title: "MySearch")
        sub.addItem(withTitle: "Search in MySearch here", action: #selector(searchHere(_:)), keyEquivalent: "")
        sub.addItem(withTitle: "Ask MySearch about this folder", action: #selector(askAbout(_:)), keyEquivalent: "")
        sub.addItem(withTitle: "Open in MySearch", action: #selector(openInMySearch(_:)), keyEquivalent: "")
        sub.addItem(.separator())
        sub.addItem(withTitle: "Copy Path", action: #selector(copyPath(_:)), keyEquivalent: "")
        sub.addItem(withTitle: "Open in Terminal", action: #selector(openInTerminal(_:)), keyEquivalent: "")
        for item in sub.items where item.action != nil { item.target = self }
        let top = NSMenuItem(title: "MySearch", action: nil, keyEquivalent: "")
        top.image = NSImage(systemSymbolName: "magnifyingglass", accessibilityDescription: "MySearch")
        top.submenu = sub
        menu.addItem(top)
        return menu
    }

    // MARK: what the menu acts on

    /// The first selected item, else the folder Finder is showing.
    private func targetURL() -> URL? {
        let c = FIFinderSyncController.default()
        return c.selectedItemURLs()?.first ?? c.targetedURL()
    }

    /// The collection whose folder contains `url` (deepest match), or nil.
    private func collection(containing url: URL) -> String? {
        let p = url.standardizedFileURL.path
        var best: (name: String, len: Int)?
        for c in known {
            let root = URL(fileURLWithPath: c.path).standardizedFileURL.path
            guard p == root || p.hasPrefix(root.hasSuffix("/") ? root : root + "/") else { continue }
            if best == nil || root.count > best!.len { best = (c.name, root.count) }
        }
        return best?.name
    }

    /// mysearch://<route>?[c=…&]path=… with every character outside A-Z a-z 0-9 - . _ ~ / percent-encoded.
    private func send(_ route: String, path: String, collection: String? = nil) {
        let allowed = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~/")
        func enc(_ s: String) -> String { s.addingPercentEncoding(withAllowedCharacters: allowed) ?? "" }
        var q = ""
        if let collection, collection.range(of: #"^[\w\-]{1,100}$"#, options: .regularExpression) != nil { q += "c=\(enc(collection))&" }
        q += "path=\(enc(path))"
        guard let url = URL(string: "mysearch://\(route)?\(q)") else { return }
        NSWorkspace.shared.open(url)
    }

    // MARK: actions

    @objc private func searchHere(_ sender: Any?) {
        guard let t = targetURL() else { return }
        send("search", path: t.path, collection: collection(containing: t))
    }

    @objc private func askAbout(_ sender: Any?) {
        guard let t = targetURL() else { return }
        send("ask", path: t.path, collection: collection(containing: t))
    }

    @objc private func openInMySearch(_ sender: Any?) {
        guard let t = targetURL() else { return }
        send("open", path: t.path)
    }

    @objc private func copyPath(_ sender: Any?) {
        let c = FIFinderSyncController.default()
        let urls = c.selectedItemURLs() ?? []
        let text = urls.isEmpty ? (c.targetedURL()?.path ?? "") : urls.map { $0.path }.joined(separator: "\n")
        guard !text.isEmpty else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }

    /// The sandboxed extension runs no AppleScript: the unsandboxed app opens Terminal (mysearch://terminal?path=…).
    @objc private func openInTerminal(_ sender: Any?) {
        guard let t = targetURL() else { return }
        send("terminal", path: t.path)
    }
}
