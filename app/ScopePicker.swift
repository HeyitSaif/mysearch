// Where a search runs, inside the search area: the scope button of the filter bar, the popover it opens (Whole Mac,
// All indexed, groups as a single choice, folders as checkboxes), the catalogue of folders, groups and short names
// behind it, and the port of the service's scope shortcut ("work: invoice", "invoice in:work", mse/config.py
// scope_shortcut) so the button follows what is typed in the search field.
//
// The picker holds no state of its own: it shows the choice it is given and reports every change. The search view
// passes the choice on to the main window, which moves the mode and the sidebar selection (MainWindowController.
// applyScopeChoice), so the sidebar, this button and the search always agree.

import Cocoa

/// What a search runs over, as the scope button shows it and the picker chooses it.
enum ScopeChoice: Equatable {
    case wholeMac                    // file names anywhere on the Mac
    case allIndexed                  // every indexed folder marked Include in All
    case group(String)               // a saved group (its id)
    case folders([String])           // one or more indexed folders

    var isWholeMac: Bool { if case .wholeMac = self { return true }; return false }
}

struct ScopeFolder {
    let name: String
    let path: String
    let files: Int
    let enabled: Bool
    let exists: Bool
    var aliases: [String]

    /// A folder that is switched off, or gone from the disk, cannot be searched.
    var usable: Bool { enabled && exists }
}

struct ScopeGroup {
    let name: String                 // the id (a slug)
    let label: String                // the name shown
    let members: [String]
    let aliases: [String]
    let hidden: Bool
}

/// The folders and groups a scope can name, with their short names. Built from the sidebar's library the moment it
/// arrives; the short names come from one more read of /api/collections (the library does not carry them).
struct ScopeCatalog {
    var folders: [ScopeFolder] = []
    var groups: [ScopeGroup] = []

    init() {}

    func group(_ name: String) -> ScopeGroup? { groups.first { $0.name == name } }

    /// The sidebar's library: keeps the short names and labels learnt earlier.
    mutating func update(library lib: IndexedLibrary) {
        let oldFolders = Dictionary(folders.map { ($0.name, $0) }, uniquingKeysWith: { a, _ in a })
        let oldGroups = Dictionary(groups.map { ($0.name, $0) }, uniquingKeysWith: { a, _ in a })
        folders = lib.collections.map { c in
            ScopeFolder(name: c.name, path: c.path, files: c.files, enabled: c.enabled, exists: c.exists,
                        aliases: oldFolders[c.name]?.aliases ?? [])
        }
        groups = lib.groups.map { g in
            let old = oldGroups[g.name]
            return ScopeGroup(name: g.name, label: old?.label ?? g.name, members: g.members,
                              aliases: old?.aliases ?? [], hidden: old?.hidden ?? false)
        }
    }

    /// GET /api/collections, read in full: folders (name, path, enabled, exists, stats.files, aliases) and groups
    /// (name, label, collections, aliases, hidden). Replaces what was here.
    mutating func merge(json obj: [String: Any]) {
        let rows = (obj["collections"] as? [[String: Any]]) ?? []
        let newFolders: [ScopeFolder] = rows.compactMap { r in
            guard let name = r["name"] as? String, let path = r["path"] as? String else { return nil }
            return ScopeFolder(name: name, path: path, files: ((r["stats"] as? [String: Any])?["files"] as? NSNumber)?.intValue ?? 0,
                               enabled: (r["enabled"] as? Bool) ?? true, exists: (r["exists"] as? Bool) ?? true,
                               aliases: ((r["aliases"] as? [Any]) ?? []).compactMap { $0 as? String })
        }
        let newGroups: [ScopeGroup] = ((obj["groups"] as? [[String: Any]]) ?? []).compactMap { g in
            guard let name = g["name"] as? String else { return nil }
            let members = ((g["collections"] as? [Any]) ?? (g["members"] as? [Any]) ?? []).compactMap { $0 as? String }
            return ScopeGroup(name: name, label: (g["label"] as? String) ?? name, members: members,
                              aliases: ((g["aliases"] as? [Any]) ?? []).compactMap { $0 as? String },
                              hidden: (g["hidden"] as? Bool) ?? false)
        }
        if !newFolders.isEmpty || !newGroups.isEmpty { folders = newFolders; groups = newGroups }
    }

    /// The folders a choice searches; empty for Whole Mac and All indexed (the service reads empty as "every folder").
    func names(for c: ScopeChoice) -> [String] {
        switch c {
        case .wholeMac, .allIndexed: return []
        case .group(let g): return group(g)?.members ?? []
        case .folders(let n): return n
        }
    }

    /// Folder names in the order the sidebar lists the folders.
    func ordered(_ names: [String]) -> [String] {
        let index = Dictionary(folders.enumerated().map { ($0.element.name, $0.offset) }, uniquingKeysWith: { a, _ in a })
        return names.sorted { (index[$0] ?? Int.max, $0) < (index[$1] ?? Int.max, $1) }
    }

    /// The choice that is the sidebar's selection: nothing = every indexed folder; exactly a group's members = that group.
    func choice(forNames names: [String]) -> ScopeChoice {
        if names.isEmpty { return .allIndexed }
        let set = Set(names)
        if let g = groups.first(where: { !$0.members.isEmpty && Set($0.members) == set }) { return .group(g.name) }
        return .folders(ordered(names))
    }

    /// The choice a scope shortcut names (its tokens are folders, groups or "all").
    func choice(forTokens tokens: [String]) -> ScopeChoice {
        if tokens.isEmpty || tokens.contains("all") { return .allIndexed }
        if tokens.count == 1, group(tokens[0]) != nil { return .group(tokens[0]) }
        var names: [String] = []
        for t in tokens {
            if let g = group(t) {
                for m in g.members where !names.contains(m) { names.append(m) }
            } else if !names.contains(t) {
                names.append(t)
            }
        }
        return choice(forNames: names)
    }

    static func short(_ s: String) -> String { s.count > 22 ? String(s.prefix(21)) + "…" : s }

    func label(for c: ScopeChoice) -> String {
        switch c {
        case .wholeMac: return "Whole Mac"
        case .allIndexed: return "All indexed"
        case .group(let g): return Self.short(group(g)?.label ?? g)
        case .folders(let n):
            let s = n.map(Self.short)
            if s.count <= 2 { return s.joined(separator: " + ") }
            return "\(s[0]) + \(s.count - 1) more"
        }
    }

    /// The scope in words, for the tooltip and the status line.
    func description(for c: ScopeChoice) -> String {
        switch c {
        case .wholeMac: return "the file names of the whole Mac"
        case .allIndexed: return "every indexed folder"
        case .group(let g):
            let m = group(g)?.members ?? []
            return "the group \(group(g)?.label ?? g) (\(m.count) \(m.count == 1 ? "folder" : "folders"))"
        case .folders(let n): return n.count == 1 ? "the folder \(n[0])" : "the folders " + n.joined(separator: ", ")
        }
    }

    func symbol(for c: ScopeChoice) -> String {
        switch c {
        case .wholeMac: return "desktopcomputer"
        case .allIndexed: return "square.stack.3d.up"
        case .group: return "rectangle.3.group"
        case .folders: return "folder"
        }
    }

    /// Every name the scope shortcut accepts (lower case) -> the folder, group or "all" it means. The filter keys
    /// never count (mse/config.py shortcut_names): "type:" is a filter, never a folder.
    var shortcutNames: [String: String] {
        var out: [String: String] = ["all": "all"]
        for f in folders where out[f.name.lowercased()] == nil { out[f.name.lowercased()] = f.name }
        for g in groups where out[g.name.lowercased()] == nil { out[g.name.lowercased()] = g.name }
        for (name, aliases) in folders.map({ ($0.name, $0.aliases) }) + groups.map({ ($0.name, $0.aliases) }) {
            for a in aliases where out[a.lowercased()] == nil { out[a.lowercased()] = name }
        }
        return out.filter { $0.key == "all" || !ScopeShortcut.reserved.contains($0.key) }
    }
}

/// The scope shortcut found in a query: "work: invoice" at the start, or "in:work" anywhere (not inside a quoted
/// phrase, and not a folder path such as in:~/Documents). Names count only when they are known, so "Note: the
/// meeting" stays an ordinary query. The service applies the same rule; this copy only tells the button.
struct ScopeShortcut {
    /// The folders, groups or "all" it names, once each; empty when the query has no shortcut.
    let tokens: [String]
    /// The query without the shortcut.
    let rest: String
    /// Names in in: tokens that match nothing (the service answers 400 for them).
    let unknown: [String]

    var found: Bool { !tokens.isEmpty }

    static let reserved: Set<String> = ["type", "kind", "ext", "in", "after", "before", "path", "has", "include", "exclude"]

    private static let prefixRegex = try! NSRegularExpression(
        pattern: #"^\s*(?<names>[\w-]+(?:\s*,\s*[\w-]+)*):(?=\s|["'“„]|$)"#)
    private static let inRegex = try! NSRegularExpression(
        pattern: #"(?<!\S)in:(?:"(?<quoted>[^"]*)"|(?<plain>[\w-]+(?:,[\w-]+)*))(?=\s|$)"#, options: [.caseInsensitive])
    private static let quotedRegex = try! NSRegularExpression(pattern: #""[^"]*"|“[^”]*”|„[^“”]*[“”]"#)

    static func parse(_ q: String, catalog: ScopeCatalog) -> ScopeShortcut {
        let known = catalog.shortcutNames
        let ns = q as NSString
        let whole = NSRange(location: 0, length: ns.length)
        var tokens: [String] = [], unknown: [String] = [], cuts: [NSRange] = []

        func take(_ names: [String]) {
            for n in names {
                if let t = known[n], !tokens.contains(t) { tokens.append(t) }
            }
        }
        func split(_ raw: String) -> [String] {
            raw.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces).lowercased() }.filter { !$0.isEmpty }
        }

        if let m = prefixRegex.firstMatch(in: q, options: [], range: whole) {
            let found = split(ns.substring(with: m.range(withName: "names")))
            if !found.isEmpty, found.allSatisfy({ known[$0] != nil }) {
                take(found)
                cuts.append(m.range)
            }
        }
        let quoted = quotedRegex.matches(in: q, options: [], range: whole).map { $0.range }
        for x in inRegex.matches(in: q, options: [], range: whole) {
            let start = x.range.location
            if (quoted + cuts).contains(where: { $0.location <= start && start < NSMaxRange($0) }) { continue }
            let qr = x.range(withName: "quoted")
            let raw = ns.substring(with: qr.location != NSNotFound ? qr : x.range(withName: "plain"))
            if raw.drop(while: { $0 == " " }).hasPrefix("~") || raw.drop(while: { $0 == " " }).hasPrefix("/") { continue }
            let found = split(raw)
            if found.isEmpty { continue }
            if found.allSatisfy({ known[$0] != nil }) {
                take(found)
                cuts.append(x.range)
            } else {
                for n in found where known[n] == nil && !unknown.contains(n) { unknown.append(n) }
            }
        }
        guard !cuts.isEmpty else { return ScopeShortcut(tokens: [], rest: q, unknown: unknown) }
        var pieces: [String] = [], at = 0
        for c in cuts.sorted(by: { $0.location < $1.location }) {
            pieces.append(ns.substring(with: NSRange(location: at, length: max(0, c.location - at))))
            at = NSMaxRange(c)
        }
        pieces.append(ns.substring(from: min(at, ns.length)))
        let rest = pieces.joined(separator: " ").split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
        return ScopeShortcut(tokens: tokens, rest: rest, unknown: unknown)
    }
}

// MARK: - the popover

/// One row of the picker: a radio or checkbox mark, the name and a small detail on the right.
@MainActor
private final class PickerCell: NSTableCellView {
    let mark = NSImageView()
    let title = NSTextField(labelWithString: "")
    let detail = NSTextField(labelWithString: "")
    private var checkbox = false
    private var on = false
    private var dimmed = false

    override var backgroundStyle: NSView.BackgroundStyle {
        didSet { applyTint() }
    }

    init(title: String, detail: String, checkbox: Bool, on: Bool, enabled: Bool) {
        super.init(frame: .zero)
        self.checkbox = checkbox
        self.on = on
        self.dimmed = !enabled
        self.title.stringValue = title
        self.title.lineBreakMode = .byTruncatingMiddle
        self.title.font = .systemFont(ofSize: 13)
        self.detail.stringValue = detail
        self.detail.font = .systemFont(ofSize: 11)
        self.detail.textColor = .secondaryLabelColor
        self.detail.alignment = .right
        self.detail.setContentHuggingPriority(.required, for: .horizontal)
        self.detail.setContentCompressionResistancePriority(.defaultHigh, for: .horizontal)
        mark.translatesAutoresizingMaskIntoConstraints = false
        self.title.translatesAutoresizingMaskIntoConstraints = false
        self.detail.translatesAutoresizingMaskIntoConstraints = false
        addSubview(mark)
        addSubview(self.title)
        addSubview(self.detail)
        imageView = mark
        textField = self.title
        NSLayoutConstraint.activate([
            mark.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 8),
            mark.centerYAnchor.constraint(equalTo: centerYAnchor),
            mark.widthAnchor.constraint(equalToConstant: 16),
            mark.heightAnchor.constraint(equalToConstant: 16),
            self.title.leadingAnchor.constraint(equalTo: mark.trailingAnchor, constant: 8),
            self.title.centerYAnchor.constraint(equalTo: centerYAnchor),
            self.detail.leadingAnchor.constraint(greaterThanOrEqualTo: self.title.trailingAnchor, constant: 8),
            self.detail.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -8),
            self.detail.centerYAnchor.constraint(equalTo: centerYAnchor),
        ])
        let name: String
        if checkbox { name = on ? "checkmark.square.fill" : "square" } else { name = on ? "largecircle.fill.circle" : "circle" }
        mark.image = NSImage(systemSymbolName: name, accessibilityDescription: on ? "selected" : "not selected")
        if dimmed { self.title.textColor = .tertiaryLabelColor }
        applyTint()
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    private func applyTint() {
        let color: NSColor
        if backgroundStyle == .emphasized { color = .alternateSelectedControlTextColor }
        else if on { color = .controlAccentColor }
        else { color = .tertiaryLabelColor }
        mark.contentTintColor = color
    }
}

/// The list: keys go to the picker first.
final class PickerTable: NSTableView {
    var onKey: ((NSEvent) -> Bool)?

    override func keyDown(with event: NSEvent) {
        if onKey?(event) == true { return }
        super.keyDown(with: event)
    }
}

@MainActor
final class ScopePickerViewController: NSViewController, NSTableViewDataSource, NSTableViewDelegate, NSSearchFieldDelegate {
    private enum PRow {
        case header(String)
        case mac
        case all
        case group(ScopeGroup)
        case folder(ScopeFolder)
        case note(String)
    }

    /// The choice changed (a click, space or return).
    var onChoose: ((ScopeChoice) -> Void)?
    /// Return on a row, Esc in the list: the user closes the popover.
    var onClose: (() -> Void)?

    private(set) var catalog: ScopeCatalog
    private(set) var choice: ScopeChoice
    private let field = NSSearchField()
    private let table = PickerTable()
    private let hint = NSTextField(labelWithString: "↑↓ move · space tick · ↵ apply · esc close")
    private var rows: [PRow] = []
    private let counts: NumberFormatter = { let f = NumberFormatter(); f.numberStyle = .decimal; return f }()

    init(catalog: ScopeCatalog, choice: ScopeChoice) {
        self.catalog = catalog
        self.choice = choice
        super.init(nibName: nil, bundle: nil)
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override func loadView() {
        view = NSView(frame: NSRect(x: 0, y: 0, width: 340, height: 420))
        field.placeholderString = "Find a folder or group"
        field.delegate = self
        field.sendsSearchStringImmediately = true
        field.setAccessibilityLabel("Find a folder or group")
        field.translatesAutoresizingMaskIntoConstraints = false

        let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("pick"))
        column.resizingMask = .autoresizingMask
        table.addTableColumn(column)
        table.headerView = nil
        table.style = .plain
        table.rowHeight = 24
        table.intercellSpacing = NSSize(width: 0, height: 0)
        table.allowsEmptySelection = true
        table.allowsMultipleSelection = false
        table.dataSource = self
        table.delegate = self
        table.target = self
        table.action = #selector(clicked(_:))
        table.onKey = { [weak self] e in self?.handleKey(e) ?? false }
        table.setAccessibilityLabel("Where to search")

        let scroll = NSScrollView()
        scroll.documentView = table
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false
        scroll.translatesAutoresizingMaskIntoConstraints = false

        hint.font = .systemFont(ofSize: 10)
        hint.textColor = .tertiaryLabelColor
        hint.translatesAutoresizingMaskIntoConstraints = false

        view.addSubview(field)
        view.addSubview(scroll)
        view.addSubview(hint)
        NSLayoutConstraint.activate([
            field.topAnchor.constraint(equalTo: view.topAnchor, constant: 10),
            field.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 10),
            field.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -10),
            scroll.topAnchor.constraint(equalTo: field.bottomAnchor, constant: 8),
            scroll.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            scroll.bottomAnchor.constraint(equalTo: hint.topAnchor, constant: -6),
            hint.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 12),
            hint.trailingAnchor.constraint(lessThanOrEqualTo: view.trailingAnchor, constant: -12),
            hint.bottomAnchor.constraint(equalTo: view.bottomAnchor, constant: -8),
        ])
        rebuild(selecting: nil)
    }

    /// The user's state changed (the sidebar picked something, the library arrived): show it.
    func update(catalog newCatalog: ScopeCatalog, choice newChoice: ScopeChoice) {
        catalog = newCatalog
        choice = newChoice
        guard isViewLoaded else { return }
        let keep = table.selectedRow >= 0 && table.selectedRow < rows.count ? key(of: rows[table.selectedRow]) : nil
        rebuild(selecting: keep)
    }

    /// The list takes the keyboard, with the current choice selected.
    func focusList() {
        view.window?.makeFirstResponder(table)
    }

    // MARK: rows

    private func key(of r: PRow) -> String? {
        switch r {
        case .mac: return "mac"
        case .all: return "all"
        case .group(let g): return "g:" + g.name
        case .folder(let f): return "f:" + f.name
        default: return nil
        }
    }

    private var currentKey: String? {
        switch choice {
        case .wholeMac: return "mac"
        case .allIndexed: return "all"
        case .group(let g): return "g:" + g
        case .folders(let n): return n.first.map { "f:" + $0 }
        }
    }

    private func rebuild(selecting wanted: String?) {
        let f = field.stringValue.trimmingCharacters(in: .whitespaces).lowercased()
        func matches(_ strings: [String]) -> Bool { f.isEmpty || strings.contains { $0.lowercased().contains(f) } }
        var out: [PRow] = []
        if matches(["whole mac", "mac", "this mac", "everything", "file names"]) { out.append(.mac) }
        if matches(["all indexed", "all", "every indexed folder", "everything indexed"]) { out.append(.all) }
        let groups = catalog.groups.filter { matches([$0.name, $0.label] + $0.aliases) }
        if !groups.isEmpty {
            out.append(.header("Groups · pick one"))
            out += groups.map { PRow.group($0) }
        }
        let folders = catalog.folders.filter { matches([$0.name] + $0.aliases) }
        if !folders.isEmpty {
            out.append(.header("Folders · tick several"))
            out += folders.map { PRow.folder($0) }
        }
        if out.isEmpty { out.append(.note("No folder or group matches “\(field.stringValue)”.")) }
        rows = out
        table.reloadData()
        let target = wanted ?? currentKey
        if let t = target, let i = rows.firstIndex(where: { key(of: $0) == t }) {
            table.selectRowIndexes(IndexSet(integer: i), byExtendingSelection: false)
            table.scrollRowToVisible(i)
        } else if let i = rows.firstIndex(where: { selectable($0) }) {
            table.selectRowIndexes(IndexSet(integer: i), byExtendingSelection: false)
        }
    }

    private func selectable(_ r: PRow) -> Bool {
        switch r {
        case .header, .note: return false
        case .folder(let f): return f.usable
        default: return true
        }
    }

    private func isOn(_ r: PRow) -> Bool {
        switch r {
        case .mac: if case .wholeMac = choice { return true }; return false
        case .all: if case .allIndexed = choice { return true }; return false
        case .group(let g): if case .group(let c) = choice { return c == g.name }; return false
        case .folder(let f): return catalog.names(for: choice).contains(f.name)
        default: return false
        }
    }

    func numberOfRows(in tableView: NSTableView) -> Int { rows.count }

    func tableView(_ tableView: NSTableView, shouldSelectRow row: Int) -> Bool { selectable(rows[row]) }

    func tableView(_ tableView: NSTableView, heightOfRow row: Int) -> CGFloat {
        if case .header = rows[row] { return 24 }
        if case .note = rows[row] { return 40 }
        return 24
    }

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        func n(_ v: Int) -> String { counts.string(from: NSNumber(value: v)) ?? "\(v)" }
        switch rows[row] {
        case .header(let text):
            let label = NSTextField(labelWithString: text.uppercased())
            label.font = .systemFont(ofSize: 10, weight: .semibold)
            label.textColor = .secondaryLabelColor
            let c = NSTableCellView()
            c.addSubview(label)
            c.textField = label
            label.translatesAutoresizingMaskIntoConstraints = false
            NSLayoutConstraint.activate([
                label.leadingAnchor.constraint(equalTo: c.leadingAnchor, constant: 10),
                label.bottomAnchor.constraint(equalTo: c.bottomAnchor, constant: -3),
            ])
            return c
        case .note(let text):
            let label = NSTextField(wrappingLabelWithString: text)
            label.font = .systemFont(ofSize: 12)
            label.textColor = .secondaryLabelColor
            let c = NSTableCellView()
            c.addSubview(label)
            label.translatesAutoresizingMaskIntoConstraints = false
            NSLayoutConstraint.activate([
                label.leadingAnchor.constraint(equalTo: c.leadingAnchor, constant: 12),
                label.trailingAnchor.constraint(equalTo: c.trailingAnchor, constant: -12),
                label.centerYAnchor.constraint(equalTo: c.centerYAnchor),
            ])
            return c
        case .mac:
            let c = PickerCell(title: "Whole Mac", detail: "file names only", checkbox: false, on: isOn(rows[row]), enabled: true)
            c.toolTip = "Search the names of every file on this Mac. Contents are searched in indexed folders only."
            return c
        case .all:
            let usable = catalog.folders.filter { $0.usable }.count
            let c = PickerCell(title: "All indexed folders", detail: usable > 0 ? "\(usable) \(usable == 1 ? "folder" : "folders")" : "",
                               checkbox: false, on: isOn(rows[row]), enabled: true)
            c.toolTip = "Every indexed folder marked Include in All. Type all: before your words to do the same from the keyboard."
            return c
        case .group(let g):
            let k = g.members.count
            let c = PickerCell(title: g.label, detail: "\(k) \(k == 1 ? "folder" : "folders")", checkbox: false,
                               on: isOn(rows[row]), enabled: true)
            let short = ([g.name] + g.aliases).map { $0 + ":" }.joined(separator: "  ")
            c.toolTip = "\(g.label): \(g.members.joined(separator: ", ")). Type \(short) before your words to search it."
            return c
        case .folder(let f):
            let detail = f.usable ? n(f.files) + " files" : (f.enabled ? "missing" : "switched off")
            let c = PickerCell(title: f.name, detail: detail, checkbox: true, on: isOn(rows[row]), enabled: f.usable)
            let short = ([f.name] + f.aliases).map { $0 + ":" }.joined(separator: "  ")
            c.toolTip = "\(f.path)\nType \(short) before your words to search only this folder."
            return c
        }
    }

    // MARK: choosing

    private func choose(_ c: ScopeChoice) {
        choice = c
        onChoose?(c)
        let keep = table.selectedRow >= 0 && table.selectedRow < rows.count ? key(of: rows[table.selectedRow]) : nil
        rebuild(selecting: keep)
    }

    /// The folders ticked now, with `name` toggled.
    private func toggled(_ name: String) -> ScopeChoice {
        var names = catalog.names(for: choice)
        if let i = names.firstIndex(of: name) { names.remove(at: i) } else { names.append(name) }
        return catalog.choice(forNames: catalog.ordered(names))
    }

    /// A click or space: Whole Mac, All indexed and a group are chosen (one answer); a folder is ticked or unticked.
    private func activate(_ row: Int, closing: Bool) {
        guard row >= 0, row < rows.count else { return }
        switch rows[row] {
        case .mac: choose(.wholeMac); if closing { onClose?() }
        case .all: choose(.allIndexed); if closing { onClose?() }
        case .group(let g): choose(.group(g.name)); if closing { onClose?() }
        case .folder(let f): if f.usable { choose(toggled(f.name)) }
        default: break
        }
    }

    @objc private func clicked(_ sender: Any?) {
        let row = table.clickedRow
        guard row >= 0, row < rows.count else { return }
        switch rows[row] {
        case .folder: activate(row, closing: false)
        default: activate(row, closing: true)
        }
    }

    /// Return: choose the row and close. On a folder with nothing ticked that picks the folder alone; with folders
    /// ticked it just applies them.
    private func returnPressed() {
        let row = table.selectedRow
        guard row >= 0, row < rows.count else { onClose?(); return }
        if case .folder(let f) = rows[row] {
            if catalog.names(for: choice).isEmpty && f.usable { choose(.folders([f.name])) }
            onClose?()
            return
        }
        activate(row, closing: true)
    }

    private func move(by delta: Int) {
        var i = table.selectedRow
        while true {
            i += delta
            if i < 0 || i >= rows.count { return }
            if selectable(rows[i]) { break }
        }
        table.selectRowIndexes(IndexSet(integer: i), byExtendingSelection: false)
        table.scrollRowToVisible(i)
    }

    // MARK: keys and the filter field

    private func handleKey(_ e: NSEvent) -> Bool {
        let flags = e.modifierFlags.intersection([.command, .control, .option])
        switch e.keyCode {
        case 36, 76: returnPressed(); return true
        case 49 where flags.isEmpty: activate(table.selectedRow, closing: false); return true
        case 53: onClose?(); return true
        default: break
        }
        // A letter or digit starts the filter (space stays the tick key while the list has the keyboard).
        if flags.isEmpty, let chars = e.characters, let u = chars.unicodeScalars.first, u.value > 0x20, u.value != 0x7f,
           !(0xF700...0xF8FF).contains(u.value) {
            view.window?.makeFirstResponder(field)
            if let editor = field.currentEditor() as? NSTextView {
                editor.selectedRange = NSRange(location: editor.string.utf16.count, length: 0)
                editor.insertText(chars, replacementRange: NSRange(location: NSNotFound, length: 0))
            }
            return true
        }
        return false
    }

    func controlTextDidChange(_ obj: Notification) {
        rebuild(selecting: nil)
    }

    func control(_ control: NSControl, textView: NSTextView, doCommandBy commandSelector: Selector) -> Bool {
        switch commandSelector {
        case #selector(NSResponder.moveDown(_:)), #selector(NSResponder.moveUp(_:)):
            // The list takes over; its selected row is the first one (or the current choice).
            view.window?.makeFirstResponder(table)
            if table.selectedRow < 0 { move(by: 1) }
            return true
        case #selector(NSResponder.insertTab(_:)):
            view.window?.makeFirstResponder(table)
            return true
        case #selector(NSResponder.insertNewline(_:)):
            returnPressed()
            return true
        case #selector(NSResponder.cancelOperation(_:)):
            if field.stringValue.isEmpty { onClose?() } else { field.stringValue = ""; rebuild(selecting: nil) }
            return true
        default:
            return false
        }
    }
}

/// The popover around the picker.
@MainActor
final class ScopePopover: NSObject, NSPopoverDelegate {
    private let popover = NSPopover()
    let picker: ScopePickerViewController
    var onClosed: (() -> Void)?

    var isShown: Bool { popover.isShown }

    init(catalog: ScopeCatalog, choice: ScopeChoice) {
        picker = ScopePickerViewController(catalog: catalog, choice: choice)
        super.init()
        popover.contentViewController = picker
        popover.contentSize = NSSize(width: 340, height: 420)
        popover.behavior = .transient
        popover.delegate = self
        picker.onClose = { [weak self] in self?.close() }
    }

    func show(relativeTo anchor: NSView) {
        popover.show(relativeTo: anchor.bounds, of: anchor, preferredEdge: .maxY)
    }

    func close() {
        popover.performClose(nil)
    }

    func popoverDidShow(_ notification: Notification) {
        picker.focusList()
    }

    func popoverDidClose(_ notification: Notification) {
        onClosed?()
    }
}
