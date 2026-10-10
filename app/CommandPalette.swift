// Command palette (⌘P): a Shuffle-style panel centred over the main window. One search field and one list:
//   Recent            the last picks, when the field is empty
//   Files and Folders the service's name search (GET /api/files/search), debounced 30 ms, stale requests cancelled
//   Commands          existing app actions, matched fuzzily; "Ask MySearch" sends the text to Ask
//   Path              a field that starts with / or ~/ lists that folder live; Tab completes the path
//
// Keys: ↑↓ move · ↵ open or run · ⌘↵ open a file in its default app · ⌥↵ copy its path · Tab complete a path · Esc close.
//
// The Commands section also runs the app's own actions: New Tab and New Window, New Folder, Split Pane, Filter This
// Folder, Show Hidden Files, Open in Terminal / VS Code / Cursor (for the current folder) and Rescan Mac.

import Cocoa

/// One row of the list. Headers and status lines are shown but cannot be selected.
fileprivate enum PaletteEntry {
    case header(String)
    case status(String)
    case file(URL, isDir: Bool, detail: String)
    case command(PaletteCommand)
    case ask(String)

    var selectable: Bool {
        switch self {
        case .header, .status: return false
        default: return true
        }
    }
}

/// A command the palette runs. `run` calls the app's existing entry points.
struct PaletteCommand {
    let id: String
    let title: String
    let keywords: String
    let symbol: String
    let run: @MainActor () -> Void
}

/// The floating panel. It takes key focus so the field can type, and it passes ⌘↵ on to the palette.
@MainActor
fileprivate final class PalettePanel: NSPanel {
    weak var palette: CommandPalette?

    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }

    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        // Return = 36, keypad Enter = 76
        if (event.keyCode == 36 || event.keyCode == 76) && event.modifierFlags.contains(.command) {
            palette?.activateSelection(modifiers: event.modifierFlags)
            return true
        }
        return super.performKeyEquivalent(with: event)
    }
}

@MainActor
final class CommandPalette: NSObject, NSWindowDelegate, NSTextFieldDelegate, NSTableViewDataSource, NSTableViewDelegate {
    static let shared = CommandPalette()

    private static let recentsKey = "MSECommandPaletteRecents"
    private static let hint = "↵ Open    ⌘↵ Open in app    ⌥↵ Copy path    Tab Complete path    Esc Close"
    private let panelSize = NSSize(width: 640, height: 460)

    private var panel: PalettePanel?
    private let field = NSTextField()
    private let table = NSTableView()
    private var entries: [PaletteEntry] = []
    private var fileEntries: [PaletteEntry] = []
    private var fileStatus: String?
    private var filesPending = false
    private var generation = 0
    private var searchTask: Task<Void, Never>?
    private var closing = false

    // MARK: showing and hiding

    /// The menu item's action (View › Command Palette, ⌘P).
    @objc func showFromMenu(_ sender: Any?) {
        if panel?.isVisible == true { close() } else { open() }
    }

    private func open() {
        let p = panel ?? makePanel()
        panel = p
        closing = false
        field.stringValue = ""
        fileEntries = []
        fileStatus = nil
        filesPending = false
        generation += 1
        rebuild()
        position(p)
        NSApp.activate(ignoringOtherApps: true)
        p.makeKeyAndOrderFront(nil)
        p.makeFirstResponder(field)
    }

    func close() {
        guard let p = panel, p.isVisible, !closing else { return }
        closing = true
        searchTask?.cancel()
        p.orderOut(nil)
        closing = false
    }

    /// Centred horizontally over the main window and near its top; centred on the screen when the window is hidden.
    private func position(_ p: PalettePanel) {
        let area = (NSApp.delegate as? AppDelegate)?.mainWindow.window.screen?.visibleFrame
            ?? NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        var x = area.midX - panelSize.width / 2
        var y = area.midY + panelSize.height / 2
        if let host = (NSApp.delegate as? AppDelegate)?.mainWindow.window, host.isVisible {
            x = host.frame.midX - panelSize.width / 2
            y = host.frame.maxY - 150 - panelSize.height
        }
        x = min(max(x, area.minX + 8), area.maxX - panelSize.width - 8)
        y = min(max(y, area.minY + 8), area.maxY - panelSize.height - 8)
        p.setFrameOrigin(NSPoint(x: x, y: y))
    }

    func windowDidResignKey(_ notification: Notification) { close() }

    // MARK: building the panel

    private func makePanel() -> PalettePanel {
        let p = PalettePanel(contentRect: NSRect(origin: .zero, size: panelSize),
                             styleMask: [.borderless, .fullSizeContentView], backing: .buffered, defer: false)
        p.palette = self
        p.isFloatingPanel = true
        p.level = .floating
        p.isOpaque = false
        p.backgroundColor = .clear
        p.hasShadow = true
        p.isReleasedWhenClosed = false
        p.hidesOnDeactivate = false
        p.delegate = self
        buildContent(in: p)
        return p
    }

    private func buildContent(in p: PalettePanel) {
        let root = NSVisualEffectView()
        root.material = .popover
        root.blendingMode = .behindWindow
        root.state = .active
        root.wantsLayer = true
        root.layer?.cornerRadius = 14
        root.layer?.masksToBounds = true
        root.layer?.borderWidth = 0.5
        root.layer?.borderColor = NSColor.separatorColor.cgColor
        p.contentView = root

        let icon = NSImageView(image: NSImage(systemSymbolName: "magnifyingglass", accessibilityDescription: nil) ?? NSImage())
        icon.contentTintColor = .secondaryLabelColor
        field.placeholderString = "Search files, run a command, or type / to browse"
        field.font = .systemFont(ofSize: 20, weight: .regular)
        field.isBordered = false
        field.drawsBackground = false
        field.focusRingType = .none
        field.delegate = self

        let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("palette.main"))
        column.width = panelSize.width - 16
        table.addTableColumn(column)
        table.headerView = nil
        table.style = .plain
        table.backgroundColor = .clear
        table.rowSizeStyle = .custom
        table.selectionHighlightStyle = .regular
        table.allowsEmptySelection = true
        table.columnAutoresizingStyle = .uniformColumnAutoresizingStyle
        table.dataSource = self
        table.delegate = self
        table.target = self
        table.doubleAction = #selector(rowDoubleClicked(_:))
        let scroll = NSScrollView()
        scroll.documentView = table
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.drawsBackground = false
        scroll.borderType = .noBorder

        let sep1 = NSBox()
        sep1.boxType = .separator
        let sep2 = NSBox()
        sep2.boxType = .separator
        let footer = NSTextField(labelWithString: Self.hint)
        footer.font = .systemFont(ofSize: 11)
        footer.textColor = .secondaryLabelColor

        let views: [NSView] = [icon, field, sep1, scroll, sep2, footer]
        for v in views {
            v.translatesAutoresizingMaskIntoConstraints = false
            root.addSubview(v)
        }
        NSLayoutConstraint.activate([
            icon.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 16),
            icon.centerYAnchor.constraint(equalTo: field.centerYAnchor),
            icon.widthAnchor.constraint(equalToConstant: 18),
            icon.heightAnchor.constraint(equalToConstant: 18),
            field.leadingAnchor.constraint(equalTo: icon.trailingAnchor, constant: 10),
            field.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -16),
            field.topAnchor.constraint(equalTo: root.topAnchor, constant: 14),
            sep1.topAnchor.constraint(equalTo: field.bottomAnchor, constant: 12),
            sep1.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            sep1.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            sep1.heightAnchor.constraint(equalToConstant: 1),
            scroll.topAnchor.constraint(equalTo: sep1.bottomAnchor, constant: 4),
            scroll.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 8),
            scroll.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -8),
            sep2.topAnchor.constraint(equalTo: scroll.bottomAnchor, constant: 4),
            sep2.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            sep2.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            sep2.heightAnchor.constraint(equalToConstant: 1),
            footer.topAnchor.constraint(equalTo: sep2.bottomAnchor, constant: 8),
            footer.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 16),
            footer.trailingAnchor.constraint(lessThanOrEqualTo: root.trailingAnchor, constant: -12),
            footer.bottomAnchor.constraint(equalTo: root.bottomAnchor, constant: -8),
        ])
    }

    // MARK: typing and searching

    func controlTextDidChange(_ obj: Notification) { scheduleUpdate() }

    /// Every keystroke: the commands and any path list show at once; the file search follows after 30 ms.
    /// A newer keystroke cancels the older search, and an answer for an older query is dropped.
    private func scheduleUpdate() {
        searchTask?.cancel()
        generation += 1
        let gen = generation
        let text = field.stringValue
        let trimmed = text.trimmingCharacters(in: .whitespaces)
        fileEntries = []
        fileStatus = nil
        filesPending = !CommandPalette.isPath(text) && !trimmed.isEmpty
        rebuild()
        guard filesPending else { return }
        searchTask = Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 30_000_000)
            guard let self, !Task.isCancelled, gen == self.generation else { return }
            do {
                let rows = try await CommandPalette.searchFiles(trimmed)
                guard !Task.isCancelled, gen == self.generation else { return }
                self.fileEntries = rows
            } catch {
                guard !Task.isCancelled, gen == self.generation else { return }
                self.fileStatus = "The search service did not answer."
            }
            self.filesPending = false
            self.rebuild()
        }
    }

    /// GET /api/files/search: the name search of the service (fuzzy, typo tolerant), the Mac scope.
    private static func searchFiles(_ query: String) async throws -> [PaletteEntry] {
        let obj = try await MSEClient.shared.get("/api/files/search", query: [
            URLQueryItem(name: "q", value: query),
            URLQueryItem(name: "limit", value: "30"),
            URLQueryItem(name: "scope", value: "mac"),
        ])
        let items = ((obj as? [String: Any])?["items"] as? [[String: Any]]) ?? []
        return items.compactMap { item -> PaletteEntry? in
            guard let path = item["path"] as? String else { return nil }
            let url = URL(fileURLWithPath: path)
            let isDir = (item["is_dir"] as? Bool) ?? false
            return PaletteEntry.file(url, isDir: isDir, detail: displayPath(url.deletingLastPathComponent().path))
        }
    }

    /// Rebuilds the list for the current text. Cheap: the commands are a fixed array and paths are one folder read.
    private func rebuild() {
        let text = field.stringValue
        let trimmed = text.trimmingCharacters(in: .whitespaces)
        var rows: [PaletteEntry] = []
        if CommandPalette.isPath(text) {
            rows = pathEntries(for: text)
            if rows.isEmpty { rows = [PaletteEntry.status("No folders or files here")] }
        } else if trimmed.isEmpty {
            let recent = recentEntries()
            if !recent.isEmpty { rows += [PaletteEntry.header("Recent")] + recent }
            let all = CommandPalette.builtIn().map { PaletteEntry.command($0) }
            rows += [PaletteEntry.header("Commands")] + all
        } else {
            if !fileEntries.isEmpty {
                rows += [PaletteEntry.header("Files and Folders")] + fileEntries
            } else if filesPending {
                rows.append(PaletteEntry.status("Searching files…"))
            } else if let message = fileStatus {
                rows.append(PaletteEntry.status(message))
            } else {
                rows.append(PaletteEntry.status("No matching files or folders"))
            }
            let matched: [PaletteEntry] = CommandPalette.builtIn()
                .compactMap { c -> (PaletteCommand, Int)? in
                    CommandPalette.fuzzy(trimmed, in: c.title + " " + c.keywords).map { (c, $0) }
                }
                .sorted { $0.1 > $1.1 }
                .map { PaletteEntry.command($0.0) }
            rows += [PaletteEntry.header("Commands")] + matched + [PaletteEntry.ask(trimmed)]
        }
        entries = rows
        table.reloadData()
        if let first = entries.firstIndex(where: { $0.selectable }) { select(first) } else { table.deselectAll(nil) }
    }

    /// Subsequence match, case-insensitive. Scores consecutive characters and word starts higher.
    private static func fuzzy(_ query: String, in text: String) -> Int? {
        let q = Array(query.lowercased().filter { !$0.isWhitespace })
        if q.isEmpty { return 0 }
        let t = Array(text.lowercased())
        let separators: Set<Character> = [" ", "/", "-", "."]
        var score = 0
        var ti = 0
        var last = -2
        for qc in q {
            var found = false
            while ti < t.count {
                if t[ti] == qc {
                    var bonus = 1
                    if ti == last + 1 { bonus += 4 }
                    if ti == 0 || separators.contains(t[ti - 1]) { bonus += 3 }
                    score += bonus
                    last = ti
                    ti += 1
                    found = true
                    break
                }
                ti += 1
            }
            if !found { return nil }
        }
        return score
    }

    // MARK: path mode

    private static func isPath(_ text: String) -> Bool {
        text.hasPrefix("/") || text == "~" || text.hasPrefix("~/")
    }

    /// The folder a path text names (~ expanded, the folder ends with a slash) and the text after its last slash.
    private static func splitPath(_ raw: String) -> (dir: URL, partial: String) {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        var text = raw
        if text == "~" { text = home + "/" } else if text.hasPrefix("~/") { text = home + String(text.dropFirst(1)) }
        guard let slash = text.lastIndex(of: "/") else {
            return (FileManager.default.homeDirectoryForCurrentUser, "")
        }
        let dir = String(text[...slash])
        let partial = String(text[text.index(after: slash)...])
        return (URL(fileURLWithPath: dir, isDirectory: true), partial)
    }

    /// The entries of the folder a path text names whose names start with what follows its last slash.
    /// Folders first, then by name. Hidden items show only when the typed part starts with a dot.
    private func pathEntries(for raw: String) -> [PaletteEntry] {
        let (dir, partial) = CommandPalette.splitPath(raw)
        let options: FileManager.DirectoryEnumerationOptions = partial.hasPrefix(".") ? [] : [.skipsHiddenFiles]
        guard let urls = try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: [.isDirectoryKey],
                                                                      options: options) else { return [] }
        let key = partial.lowercased()
        let items: [(url: URL, isDir: Bool)] = urls
            .filter { $0.lastPathComponent.lowercased().hasPrefix(key) }
            .map { url in (url: url, isDir: (try? url.resourceValues(forKeys: [.isDirectoryKey]))?.isDirectory ?? false) }
        let sorted = items.sorted { a, b in
            if a.isDir != b.isDir { return a.isDir }
            return a.url.lastPathComponent.localizedStandardCompare(b.url.lastPathComponent) == .orderedAscending
        }
        return sorted.prefix(200).map { item in
            PaletteEntry.file(item.url, isDir: item.isDir,
                              detail: CommandPalette.displayPath(item.url.deletingLastPathComponent().path))
        }
    }

    /// Tab in path mode: completes a single match (a folder gets its slash) or the common prefix of several.
    private func completePath() {
        let raw = field.stringValue
        guard CommandPalette.isPath(raw) else { return }
        if raw == "~" {
            field.stringValue = "~/"
            scheduleUpdate()
            return
        }
        let (_, partial) = CommandPalette.splitPath(raw)
        let typedDir = String(raw.dropLast(partial.count))      // as typed, so "~/" stays "~/"
        let candidates: [(name: String, isDir: Bool)] = pathEntries(for: raw).compactMap { entry in
            if case .file(let url, let isDir, _) = entry { return (url.lastPathComponent, isDir) }
            return nil
        }
        guard let only = candidates.first else { return }
        if candidates.count == 1 {
            field.stringValue = typedDir + only.name + (only.isDir ? "/" : "")
        } else {
            let common = CommandPalette.commonPrefix(candidates.map { $0.name })
            guard common.count > partial.count else { return }
            field.stringValue = typedDir + common
        }
        scheduleUpdate()
    }

    private static func commonPrefix(_ names: [String]) -> String {
        guard var prefix = names.first else { return "" }
        for name in names.dropFirst() {
            while !name.lowercased().hasPrefix(prefix.lowercased()) { prefix.removeLast() }
        }
        return prefix
    }

    // MARK: keys

    func control(_ control: NSControl, textView: NSTextView, doCommandBy commandSelector: Selector) -> Bool {
        switch commandSelector {
        case #selector(NSResponder.moveUp(_:)): moveSelection(-1)
        case #selector(NSResponder.moveDown(_:)): moveSelection(1)
        case #selector(NSResponder.insertNewline(_:)): activateSelection(modifiers: NSApp.currentEvent?.modifierFlags ?? [])
        case #selector(NSResponder.cancelOperation(_:)): close()
        case #selector(NSResponder.insertTab(_:)): completePath()
        default: return false
        }
        return true
    }

    private func moveSelection(_ step: Int) {
        var next = table.selectedRow < 0 ? (step > 0 ? 0 : entries.count - 1) : table.selectedRow + step
        while entries.indices.contains(next) && !entries[next].selectable { next += step }
        if entries.indices.contains(next) { select(next) }
    }

    private func select(_ row: Int) {
        table.selectRowIndexes(IndexSet(integer: row), byExtendingSelection: false)
        table.scrollRowToVisible(row)
    }

    @objc private func rowDoubleClicked(_ sender: Any?) { activateSelection(modifiers: []) }

    /// ↵ opens or runs the selected row. ⌘↵ opens a file in its default app; ⌥↵ copies its path.
    func activateSelection(modifiers: NSEvent.ModifierFlags) {
        let row = table.selectedRow
        guard entries.indices.contains(row) else { return }
        switch entries[row] {
        case .file(let url, let isDir, _):
            remember((isDir ? "d:" : "f:") + url.path)
            close()
            if modifiers.contains(.command) {
                NSWorkspace.shared.open(url)
            } else if modifiers.contains(.option) {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(url.path, forType: .string)
            } else if let app = NSApp.delegate as? AppDelegate {
                if isDir { app.mainWindow.openFolder(url) } else { app.mainWindow.revealInFolderView(url) }
                app.showMain(focus: false)
            }
        case .command(let c):
            remember("c:" + c.id)
            close()
            c.run()
        case .ask(let question):
            close()
            guard let app = NSApp.delegate as? AppDelegate else { return }
            app.showMain(focus: false)
            app.mainWindow.setMode(.ask)
            app.mainWindow.ask.ask(question: question)
        case .header, .status:
            break
        }
    }

    // MARK: recents

    private func remember(_ token: String) {
        var list = UserDefaults.standard.stringArray(forKey: Self.recentsKey) ?? []
        list.removeAll { $0 == token }
        list.insert(token, at: 0)
        UserDefaults.standard.set(Array(list.prefix(12)), forKey: Self.recentsKey)
    }

    /// The recent picks that still exist. Tokens: "c:<command id>", "f:<file path>", "d:<folder path>".
    private func recentEntries() -> [PaletteEntry] {
        let tokens = UserDefaults.standard.stringArray(forKey: Self.recentsKey) ?? []
        let commands = CommandPalette.builtIn()
        var out: [PaletteEntry] = []
        for token in tokens.prefix(8) {
            if token.hasPrefix("c:") {
                let id = String(token.dropFirst(2))
                if let c = commands.first(where: { $0.id == id }) { out.append(PaletteEntry.command(c)) }
            } else if token.hasPrefix("f:") || token.hasPrefix("d:") {
                let path = String(token.dropFirst(2))
                guard FileManager.default.fileExists(atPath: path) else { continue }
                let url = URL(fileURLWithPath: path)
                out.append(PaletteEntry.file(url, isDir: token.hasPrefix("d:"),
                                             detail: CommandPalette.displayPath(url.deletingLastPathComponent().path)))
            }
        }
        return out
    }

    // MARK: table

    func numberOfRows(in tableView: NSTableView) -> Int { entries.count }

    func tableView(_ tableView: NSTableView, shouldSelectRow row: Int) -> Bool { entries[row].selectable }

    func tableView(_ tableView: NSTableView, heightOfRow row: Int) -> CGFloat {
        switch entries[row] {
        case .header: return 26
        case .status: return 30
        default: return 44
        }
    }

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        switch entries[row] {
        case .header(let title):
            return makeLabel(title.uppercased(), size: 11, weight: .semibold, color: .secondaryLabelColor)
        case .status(let text):
            return makeLabel(text, size: 12, weight: .regular, color: .secondaryLabelColor)
        case .file(let url, _, let detail):
            return makeRow(icon: NSWorkspace.shared.icon(forFile: url.path), title: url.lastPathComponent, subtitle: detail)
        case .command(let c):
            return makeRow(icon: NSImage(systemSymbolName: c.symbol, accessibilityDescription: nil), title: c.title, subtitle: "")
        case .ask(let question):
            return makeRow(icon: NSImage(systemSymbolName: "sparkles", accessibilityDescription: nil),
                           title: "Ask MySearch “\(question)”", subtitle: "Opens Ask in the main window")
        }
    }

    private func makeLabel(_ text: String, size: CGFloat, weight: NSFont.Weight, color: NSColor) -> NSView {
        let label = NSTextField(labelWithString: text)
        label.font = .systemFont(ofSize: size, weight: weight)
        label.textColor = color
        label.lineBreakMode = .byTruncatingTail
        label.translatesAutoresizingMaskIntoConstraints = false
        let host = NSView()
        host.addSubview(label)
        NSLayoutConstraint.activate([
            label.leadingAnchor.constraint(equalTo: host.leadingAnchor, constant: 14),
            label.trailingAnchor.constraint(lessThanOrEqualTo: host.trailingAnchor, constant: -12),
            label.centerYAnchor.constraint(equalTo: host.centerYAnchor),
        ])
        return host
    }

    private func makeRow(icon: NSImage?, title: String, subtitle: String) -> NSView {
        let image = NSImageView(image: icon ?? NSImage())
        image.imageScaling = .scaleProportionallyUpOrDown
        image.translatesAutoresizingMaskIntoConstraints = false
        let name = NSTextField(labelWithString: title)
        name.font = .systemFont(ofSize: 13)
        name.lineBreakMode = .byTruncatingMiddle
        let detail = NSTextField(labelWithString: subtitle)
        detail.font = .systemFont(ofSize: 11)
        detail.textColor = .secondaryLabelColor
        detail.lineBreakMode = .byTruncatingMiddle
        let texts = NSStackView(views: [name, detail])
        texts.orientation = .vertical
        texts.alignment = .leading
        texts.spacing = 1
        texts.translatesAutoresizingMaskIntoConstraints = false
        let host = NSView()
        host.addSubview(image)
        host.addSubview(texts)
        NSLayoutConstraint.activate([
            image.leadingAnchor.constraint(equalTo: host.leadingAnchor, constant: 14),
            image.centerYAnchor.constraint(equalTo: host.centerYAnchor),
            image.widthAnchor.constraint(equalToConstant: 26),
            image.heightAnchor.constraint(equalToConstant: 26),
            texts.leadingAnchor.constraint(equalTo: image.trailingAnchor, constant: 10),
            texts.trailingAnchor.constraint(equalTo: host.trailingAnchor, constant: -14),
            texts.centerYAnchor.constraint(equalTo: host.centerYAnchor),
        ])
        return host
    }

    // MARK: helpers

    private static func displayPath(_ path: String) -> String {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        if path == home { return "~" }
        if path.hasPrefix(home + "/") { return "~" + String(path.dropFirst(home.count)) }
        return path
    }

    /// Runs `body` on the app and brings the main window forward afterwards.
    private static func inMain(_ body: (AppDelegate) -> Void) {
        guard let app = NSApp.delegate as? AppDelegate else { return }
        body(app)
        app.showMain(focus: false)
    }

    /// For commands that need the keyboard or first responder: runs once the palette has closed and the main window is key.
    private static func afterClose(_ body: @escaping (AppDelegate) -> Void) {
        DispatchQueue.main.async { inMain(body) }
    }

    /// The commands. Each one calls an entry point the app already has; nothing here adds behaviour.
    static func builtIn() -> [PaletteCommand] {
        let home = FileManager.default.homeDirectoryForCurrentUser
        return [
            PaletteCommand(id: "go.home", title: "Go to Home", keywords: "folder start", symbol: "house") {
                inMain { $0.mainWindow.goHome() }
            },
            PaletteCommand(id: "go.desktop", title: "Go to Desktop", keywords: "folder", symbol: "menubar.dock.rectangle") {
                inMain { $0.mainWindow.openFolder(home.appendingPathComponent("Desktop")) }
            },
            PaletteCommand(id: "go.documents", title: "Go to Documents", keywords: "folder", symbol: "doc.text") {
                inMain { $0.mainWindow.openFolder(home.appendingPathComponent("Documents")) }
            },
            PaletteCommand(id: "go.downloads", title: "Go to Downloads", keywords: "folder", symbol: "arrow.down.circle") {
                inMain { $0.mainWindow.openFolder(home.appendingPathComponent("Downloads")) }
            },
            PaletteCommand(id: "view.list", title: "List View", keywords: "show style", symbol: "list.bullet") {
                inMain { $0.mainWindow.setViewStyle(.list) }
            },
            PaletteCommand(id: "view.columns", title: "Column View", keywords: "show style", symbol: "rectangle.split.3x1") {
                inMain { $0.mainWindow.setViewStyle(.columns) }
            },
            PaletteCommand(id: "view.icons", title: "Icon View", keywords: "show style grid", symbol: "square.grid.2x2") {
                inMain { $0.mainWindow.setViewStyle(.icons) }
            },
            PaletteCommand(id: "mode.mac", title: "Switch to Mac Mode", keywords: "files folders", symbol: "folder") {
                inMain { $0.mainWindow.setMode(.mac) }
            },
            PaletteCommand(id: "mode.indexed", title: "Switch to Indexed Mode", keywords: "collections index", symbol: "books.vertical") {
                inMain { $0.mainWindow.setMode(.indexed) }
            },
            PaletteCommand(id: "mode.ask", title: "Switch to Ask Mode", keywords: "chat question", symbol: "sparkles") {
                inMain { $0.mainWindow.setMode(.ask) }
            },
            PaletteCommand(id: "settings", title: "Settings…", keywords: "preferences folders", symbol: "gearshape") {
                (NSApp.delegate as? AppDelegate)?.openSettings()
            },
            PaletteCommand(id: "classic", title: "Open Classic MySearch", keywords: "old web ui", symbol: "rectangle.on.rectangle") {
                (NSApp.delegate as? AppDelegate)?.showClassic()
            },
            PaletteCommand(id: "sync", title: "Sync Now", keywords: "update refresh index", symbol: "arrow.triangle.2.circlepath") {
                (NSApp.delegate as? AppDelegate)?.syncNow()
            },
            PaletteCommand(id: "tab.new", title: "New Tab", keywords: "open folder window", symbol: "plus.rectangle.on.rectangle") {
                inMain { app in app.newTab(from: app.mainWindow.window) }
            },
            PaletteCommand(id: "window.new", title: "New Window", keywords: "open folder", symbol: "macwindow.badge.plus") {
                inMain { app in app.newWindowAction() }
            },
            PaletteCommand(id: "file.newFolder", title: "New Folder", keywords: "create make directory", symbol: "folder.badge.plus") {
                afterClose { _ in FileActions.active?.run(.newFolder) }      // needs the folder view to have the keyboard
            },
            PaletteCommand(id: "view.split", title: "Split Pane", keywords: "two panes side by side", symbol: "rectangle.split.2x1") {
                inMain { app in app.mainWindow.toggleSplitPane() }
            },
            PaletteCommand(id: "folder.filter", title: "Filter This Folder", keywords: "narrow find search within", symbol: "line.3.horizontal.decrease.circle") {
                afterClose { app in app.mainWindow.filterThisFolder() }
            },
            PaletteCommand(id: "view.hidden", title: "Show or Hide Hidden Files", keywords: "dot files invisible", symbol: "eye") {
                inMain { app in app.mainWindow.toggleHiddenFiles() }
            },
            PaletteCommand(id: "open.terminal", title: "Open in Terminal", keywords: "shell console folder", symbol: "terminal") {
                inMain { app in Launchers.open(.terminal, urls: [app.mainWindow.navigator.current]) }
            },
            PaletteCommand(id: "open.vscode", title: "Open in VS Code", keywords: "code editor folder", symbol: "chevron.left.forwardslash.chevron.right") {
                inMain { app in Launchers.open(.vscode, urls: [app.mainWindow.navigator.current]) }
            },
            PaletteCommand(id: "open.cursor", title: "Open in Cursor", keywords: "code editor folder", symbol: "cursorarrow") {
                inMain { app in Launchers.open(.cursor, urls: [app.mainWindow.navigator.current]) }
            },
            PaletteCommand(id: "rescan", title: "Rescan Mac", keywords: "index crawl update files", symbol: "arrow.clockwise") {
                Task {
                    do { _ = try await MSEClient.shared.postJSON("/api/files/crawl", body: ["force": true]) }
                    catch { NSLog("MySearch: Rescan Mac failed: \(error.localizedDescription)") }
                }
            },
        ]
    }
}
