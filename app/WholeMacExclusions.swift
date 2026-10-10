// Taking a folder out of the whole-Mac name search (and putting it back) from the folder views' context menu.
//
// The list lives in the service (mac_index.exclude). This file reads GET /api/files/status (what each entry means
// and the built-in privacy rules) and GET /api/settings (the raw list), and writes in one way only: POST
// /api/settings {"mac_index": {"exclude": [...]}} with the whole list. The service validates it (a refusal comes back
// as its own sentence) and the built-in rules cannot be changed from here: an exclusion can only add to them.
// Nothing on the disk is touched.
//
// The menu is built synchronously, so the last answer is kept here and refreshed in the background whenever a menu
// is opened. Without an answer (the service is down or too old) the menu simply has no such item.

import Cocoa

@MainActor
final class WholeMacExclusions {
    static let shared = WholeMacExclusions()

    /// One configured exclusion as the service reports it.
    struct Entry {
        let raw: String            // exactly as stored in the settings
        let path: String?          // the folder with ~ expanded; nil for a name or pattern
        let valid: Bool            // false for an old entry the service would refuse today
    }

    enum State: Equatable {
        case included
        case excluded                  // an entry of its own: Include removes it
        case covered(by: String)       // inside an excluded folder, or under a built-in rule: nothing to switch here
    }

    private(set) var entries: [Entry] = []
    private(set) var loaded = false
    private var builtInFolders: [String] = []       // absolute, lower-case, no trailing slash
    private var builtInNames: Set<String> = []      // folder names that are never indexed anywhere (lower-case)
    private var refreshedAt = Date.distantPast
    private var inFlight = false

    // MARK: reading

    /// Asks the service again unless it was asked a moment ago. Failures keep the last answer.
    func refresh(force: Bool = false) {
        guard !inFlight, force || Date().timeIntervalSince(refreshedAt) > 20 else { return }
        inFlight = true
        Task { @MainActor [self] in
            defer { inFlight = false }
            guard let obj = try? await MSEClient.shared.get("/api/files/status") as? [String: Any],
                  let rows = obj["exclude_entries"] as? [[String: Any]] else { return }     // no service, or an older one
            refreshedAt = Date()
            apply(rows: rows, hard: (obj["hard_excluded"] as? [[String: Any]]) ?? [])
        }
    }

    private func apply(rows: [[String: Any]], hard: [[String: Any]]) {
        entries = rows.compactMap { r in
            guard let raw = r["entry"] as? String else { return nil }
            return Entry(raw: raw, path: r["path"] as? String, valid: r["invalid"] == nil)
        }
        var folders: [String] = [], names: Set<String> = []
        let home = NSHomeDirectory()
        for group in hard {
            for item in (group["items"] as? [String]) ?? [] where !item.contains(where: { "*?[".contains($0) }) {
                let bare = item.hasSuffix("/") ? String(item.dropLast()) : item
                if bare.hasPrefix("~/") { folders.append((home + bare.dropFirst()).lowercased()) }
                else if bare.hasPrefix("/") { folders.append(bare.lowercased()) }
                else if item.hasSuffix("/"), !bare.contains("/") { names.insert(bare.lowercased()) }
            }
        }
        builtInFolders = folders
        builtInNames = names
        loaded = true
    }

    /// Where a folder stands: switchable here (included, or an exclusion of its own) or already covered.
    func state(for url: URL) -> State {
        let p = url.path.lowercased()
        let own = entries.filter { $0.valid && $0.path != nil }
        if own.contains(where: { $0.path!.lowercased() == p }) { return .excluded }
        if let parent = own.first(where: { p.hasPrefix($0.path!.lowercased() + "/") }) { return .covered(by: parent.path!) }
        if builtInFolders.contains(where: { p == $0 || p.hasPrefix($0 + "/") })
            || p.split(separator: "/").contains(where: { builtInNames.contains(String($0)) }) {
            return .covered(by: "")
        }
        return .included
    }

    // MARK: writing

    /// Adds the folders to the list. The service normalises them (~, trailing slash, duplicates) and may refuse.
    func exclude(_ urls: [URL]) async throws {
        var list = try await rawList()
        list += urls.map { $0.path }
        try await save(list)
    }

    /// Takes the folders' own entries out of the list; the other entries go back unchanged.
    func include(_ urls: [URL]) async throws {
        let gone = Set(urls.map { $0.path.lowercased() })
        let drop = Set(entries.filter { $0.path != nil && gone.contains($0.path!.lowercased()) }.map(\.raw))
        try await save(try await rawList().filter { !drop.contains($0) })
    }

    private func rawList() async throws -> [String] {
        let settings = try await MSEClient.shared.get("/api/settings") as? [String: Any]
        return ((settings?["mac_index"] as? [String: Any])?["exclude"] as? [String]) ?? []
    }

    private func save(_ list: [String]) async throws {
        _ = try await MSEClient.shared.postJSON("/api/settings", body: ["mac_index": ["exclude": list]])
        refresh(force: true)
    }

    /// The service's own sentence, without the Python exception name in front of it.
    static func message(for error: Error) -> String {
        guard let m = (error as? MSEServerMessage)?.message else { return error.localizedDescription }
        return m.hasPrefix("ValueError: ") ? String(m.dropFirst("ValueError: ".count)) : m
    }
}

// MARK: - the context-menu items

extension FileActions {
    /// "Exclude from whole-Mac search" / "Include in whole-Mac search" for a selection of folders (never for files).
    /// A folder already covered by a parent exclusion or a built-in privacy rule gets a disabled note instead.
    func addWholeMacItems(to menu: NSMenu, for items: [FileItem]) {
        let store = WholeMacExclusions.shared
        store.refresh()
        guard store.loaded, !items.isEmpty, items.allSatisfy(\.isDir) else { return }
        var toExclude: [URL] = [], toInclude: [URL] = []
        var covered: String?
        for item in items {
            switch store.state(for: item.url) {
            case .included: toExclude.append(item.url)
            case .excluded: toInclude.append(item.url)
            case .covered(let by): covered = by
            }
        }
        guard !toExclude.isEmpty || !toInclude.isEmpty || covered != nil else { return }
        menu.addItem(.separator())
        if !toExclude.isEmpty {
            let entry = menu.addItem(withTitle: "Exclude from whole-Mac search", action: #selector(excludeFromWholeMacAction(_:)), keyEquivalent: "")
            entry.target = self
            entry.representedObject = toExclude
        }
        if !toInclude.isEmpty {
            let entry = menu.addItem(withTitle: "Include in whole-Mac search", action: #selector(includeInWholeMacAction(_:)), keyEquivalent: "")
            entry.target = self
            entry.representedObject = toInclude
        }
        if toExclude.isEmpty && toInclude.isEmpty, let by = covered {
            let title = by.isEmpty ? "Always excluded from whole-Mac search"
                                   : "Excluded from whole-Mac search by \((by as NSString).abbreviatingWithTildeInPath)"
            menu.addItem(withTitle: title, action: nil, keyEquivalent: "").isEnabled = false
        }
    }

    @objc func excludeFromWholeMacAction(_ sender: NSMenuItem) {
        guard let urls = sender.representedObject as? [URL] else { return }
        Task { @MainActor in
            do { try await WholeMacExclusions.shared.exclude(urls) } catch { Self.showWholeMacError(error) }
        }
    }

    @objc func includeInWholeMacAction(_ sender: NSMenuItem) {
        guard let urls = sender.representedObject as? [URL] else { return }
        Task { @MainActor in
            do { try await WholeMacExclusions.shared.include(urls) } catch { Self.showWholeMacError(error) }
        }
    }

    private static func showWholeMacError(_ error: Error) {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "The whole-Mac search was not changed"
        alert.informativeText = WholeMacExclusions.message(for: error)
            + "\n\nSettings (\u{2318},) \u{203A} Whole-Mac search lists everything that is skipped."
        alert.addButton(withTitle: "OK")
        alert.runModal()
    }
}
