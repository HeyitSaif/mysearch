// The native search results view, Everything-style: a name search over the whole Mac (scope=mac) or over the
// Indexed folders and groups the user picked in the sidebar (scope=indexed). Results arrive as the user types
// (40 ms debounce, every older request cancelled). Indexed mode adds a second section, "Content matches" (meaning
// and keywords from /api/search), which loads after the names and never blocks typing.
//
// The main window owns the toolbar search field and calls run(_:) with its text; this controller owns the table,
// the scope bar, the banner and the keys. Endpoints: GET /api/files/search, /api/files/status, /api/search (see
// MSEClient.get in API.swift). A 404 from /api/files/* means the whole-Mac index is not installed: the view then
// shows a banner and the hybrid search over the indexed folders instead.

import Cocoa
import Quartz

enum SearchScope { case mac, indexed }

/// One name match, from /api/files/search (or a hybrid hit in the fallback). `match` holds character ranges in name.
struct SearchItem {
    let path: String
    let name: String
    let parent: String
    let isDir: Bool
    let kind: String
    let size: Int64?
    let mtime: Date?
    let score: Double
    let match: [(Int, Int)]
    let collection: String?
    var dateText: String? = nil      // a document date that is only a year or a month, shown as written

    var url: URL { URL(fileURLWithPath: path) }

    static func parse(_ d: [String: Any]) -> SearchItem? {
        guard let path = d["path"] as? String else { return nil }
        let name = (d["name"] as? String) ?? URL(fileURLWithPath: path).lastPathComponent
        let parent = (d["parent"] as? String) ?? URL(fileURLWithPath: path).deletingLastPathComponent().path
        let ranges = ((d["match"] as? [[Any]]) ?? []).compactMap { pair -> (Int, Int)? in
            guard pair.count == 2, let s = (pair[0] as? NSNumber)?.intValue, let e = (pair[1] as? NSNumber)?.intValue else { return nil }
            return (s, e)
        }
        return SearchItem(path: path, name: name, parent: parent, isDir: (d["is_dir"] as? Bool) ?? false,
                          kind: (d["kind"] as? String) ?? "", size: (d["size"] as? NSNumber)?.int64Value,
                          mtime: SearchDates.parse(d["mtime"]), score: (d["score"] as? NSNumber)?.doubleValue ?? 0,
                          match: ranges, collection: d["collection"] as? String)
    }

    /// A hybrid hit (/api/search) shown as a name row in the fallback.
    static func fromHybrid(_ d: [String: Any]) -> SearchItem? {
        guard let path = (d["abs_path"] as? String) ?? (d["path"] as? String) else { return nil }
        let url = URL(fileURLWithPath: path)
        return SearchItem(path: path, name: (d["name"] as? String) ?? url.lastPathComponent,
                          parent: url.deletingLastPathComponent().path, isDir: false,
                          kind: (d["kind"] as? String) ?? "", size: (d["size"] as? NSNumber)?.int64Value,
                          mtime: SearchDates.parse(d["date"]), score: (d["score"] as? NSNumber)?.doubleValue ?? 0,
                          match: [], collection: d["collection"] as? String, dateText: SearchDates.display(d["date"]))
    }
}

/// One content match (/api/search): the first matching passage of a file, with its <mark> highlights.
struct ContentHit {
    let path: String
    let name: String
    let title: String
    let kind: String
    let date: Date?
    let size: Int64?
    let snippet: NSAttributedString
    let collection: String?          // the indexed folder it belongs to
    let relPath: String?             // its path inside that folder (what the web preview selects by)
    let dateText: String?            // the document date as the service wrote it ("2025-03")
    let why: [String]                // how it matched: keyword, meaning, translated, visual, audio
    let relevance: Int?              // 0 to 100, against the best match

    var url: URL { URL(fileURLWithPath: path) }

    /// `terms` are the query's words: they are highlighted in a passage the service did not mark itself (a match by meaning).
    static func parse(_ d: [String: Any], terms: [String] = []) -> ContentHit? {
        guard let path = (d["abs_path"] as? String) ?? (d["path"] as? String) else { return nil }
        let url = URL(fileURLWithPath: path)
        let hit = ((d["hits"] as? [[String: Any]]) ?? []).first
        let snippet = SnippetText.attributed((hit?["snippet"] as? String) ?? "", terms: terms)
        return ContentHit(path: path, name: (d["name"] as? String) ?? url.lastPathComponent,
                          title: (d["title"] as? String) ?? (d["name"] as? String) ?? url.lastPathComponent,
                          kind: (d["kind"] as? String) ?? "", date: SearchDates.parse(d["date"]),
                          size: (d["size"] as? NSNumber)?.int64Value, snippet: snippet,
                          collection: d["collection"] as? String, relPath: d["path"] as? String,
                          dateText: SearchDates.display(d["date"]),
                          why: ((d["why"] as? [Any]) ?? []).compactMap { $0 as? String },
                          relevance: (d["relevance"] as? NSNumber)?.intValue)
    }

    /// Why this file is in the list, in the words of the web UI's channels ("name" is the keyword channel there too):
    /// "Words + meaning", "Name + words", "Meaning".
    func whyText(alsoByName: Bool) -> String {
        var parts: [String] = []
        if alsoByName { parts.append("name") }
        for w in why {
            let label: String
            switch w {
            case "keyword", "name": label = "words"
            case "meaning", "translated", "visual", "audio": label = w
            default: label = w
            }
            if !parts.contains(label) { parts.append(label) }
        }
        guard let first = parts.first else { return "Content" }
        return (first.prefix(1).uppercased() + first.dropFirst() + parts.dropFirst().map { " + " + $0 }.joined())
    }
}

/// A row of the Indexed picker: an indexed folder, or a group (its members are collection names).
struct PickItem {
    let name: String
    let path: String?
    let files: Int
    let members: [String]
    let isGroup: Bool
}

enum SearchDates {
    private static let fractional: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter(); f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]; return f
    }()
    private static let plain: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter(); f.formatOptions = [.withInternetDateTime]; return f
    }()

    private static let day: DateFormatter = {
        let f = DateFormatter(); f.locale = Locale(identifier: "en_US_POSIX"); f.timeZone = TimeZone.current; return f
    }()

    /// Epoch seconds (the file-name index: mtime, ctime), or text: the document date of /api/search is "YYYY",
    /// "YYYY-MM" or "YYYY-MM-DD"; a full ISO 8601 date-time is read too. nil when none of them.
    static func parse(_ v: Any?) -> Date? {
        if let n = v as? NSNumber { return Date(timeIntervalSince1970: n.doubleValue) }
        guard let s = v as? String, !s.isEmpty else { return nil }
        if let d = fractional.date(from: s) ?? plain.date(from: s) { return d }
        let formats = ["yyyy-MM-dd": 10, "yyyy-MM": 7, "yyyy": 4]
        for (fmt, len) in formats where s.count == len {
            day.dateFormat = fmt
            if let d = day.date(from: s) { return d }
        }
        return nil
    }

    /// How a document date of /api/search is shown: a year or a month as written ("2025-03"), a day as a date
    /// without a time of day; nil for a full date-time (the caller formats it) or no date.
    static func display(_ v: Any?) -> String? {
        guard let s = v as? String else { return nil }
        switch s.count {
        case 4, 7: return s
        case 10:
            guard let d = parse(s) else { return nil }
            let f = DateFormatter(); f.dateStyle = .medium; f.timeStyle = .none
            return f.string(from: d)
        default: return nil
        }
    }
}

/// Turns the service's snippet (plain text with <mark>…</mark> around the matched terms) into highlighted runs: the
/// matched words are semibold on a soft yellow. A passage the service did not mark (a match by meaning) gets the
/// query's own words highlighted, so the eye finds what to read.
enum SnippetText {
    private static let base: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 11)]
    private static var marked: [NSAttributedString.Key: Any] {
        [.font: NSFont.systemFont(ofSize: 11, weight: .semibold),
         .backgroundColor: NSColor.systemYellow.withAlphaComponent(0.28)]
    }

    static func attributed(_ raw: String, terms: [String] = []) -> NSAttributedString {
        let out = NSMutableAttributedString()
        let parts = raw.components(separatedBy: "<mark>")
        var sawMark = false
        for (i, part) in parts.enumerated() {
            if i == 0 { out.append(NSAttributedString(string: unescape(part), attributes: base)); continue }
            sawMark = true
            let sub = part.components(separatedBy: "</mark>")
            out.append(NSAttributedString(string: unescape(sub[0]), attributes: marked))
            if sub.count > 1 {
                out.append(NSAttributedString(string: unescape(sub.dropFirst().joined(separator: "</mark>")), attributes: base))
            }
        }
        if !sawMark && !terms.isEmpty {
            let text = out.string as NSString
            for t in terms where t.count >= 3 {
                var from = 0
                while from < text.length {
                    let r = text.range(of: t, options: [.caseInsensitive, .diacriticInsensitive],
                                       range: NSRange(location: from, length: text.length - from))
                    if r.location == NSNotFound { break }
                    out.addAttributes(marked, range: r)
                    from = NSMaxRange(r)
                }
            }
        }
        return out
    }

    /// The words of a query worth highlighting: no operators (word:value), no negations, no quotes, three letters or more.
    static func terms(from query: String) -> [String] {
        var seen = Set<String>(), out: [String] = []
        for raw in query.split(whereSeparator: { $0.isWhitespace }) {
            let w = raw.trimmingCharacters(in: CharacterSet(charactersIn: "\"'“”„‘’,.;()[]"))
            if w.contains(":") || w.hasPrefix("!") || w.hasPrefix("-") || w.count < 3 { continue }
            if seen.insert(w.lowercased()).inserted { out.append(w) }
        }
        return out
    }

    static func unescape(_ s: String) -> String {
        s.replacingOccurrences(of: "&lt;", with: "<").replacingOccurrences(of: "&gt;", with: ">")
            .replacingOccurrences(of: "&quot;", with: "\"").replacingOccurrences(of: "&#39;", with: "'")
            .replacingOccurrences(of: "&#x27;", with: "'").replacingOccurrences(of: "&amp;", with: "&")
    }
}

/// The table: keys go to the controller first; anything it does not take goes to NSTableView. A click on a row the
/// controller claims (the Content matches header) is the controller's too.
final class SearchTable: NSTableView {
    var onKey: ((NSEvent) -> Bool)?
    var onRowClick: ((Int) -> Bool)?

    override func keyDown(with event: NSEvent) {
        if onKey?(event) == true { return }
        super.keyDown(with: event)
    }

    override func mouseDown(with event: NSEvent) {
        let row = self.row(at: convert(event.locationInWindow, from: nil))
        if row >= 0, onRowClick?(row) == true {
            window?.makeFirstResponder(self)
            selectRowIndexes(IndexSet(integer: row), byExtendingSelection: false)
            return
        }
        super.mouseDown(with: event)
    }
}

/// A scope token above the table ("Whole Mac", or one indexed folder name).
final class ScopeToken: NSView {
    init(_ text: String) {
        super.init(frame: .zero)
        wantsLayer = true
        layer?.cornerRadius = 9
        let label = NSTextField(labelWithString: text)
        label.font = .systemFont(ofSize: 11, weight: .medium)
        label.translatesAutoresizingMaskIntoConstraints = false
        addSubview(label)
        NSLayoutConstraint.activate([
            label.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 8),
            label.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -8),
            label.topAnchor.constraint(equalTo: topAnchor, constant: 2),
            label.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -2),
        ])
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override var wantsUpdateLayer: Bool { true }

    override func updateLayer() {
        layer?.backgroundColor = NSColor.controlAccentColor.withAlphaComponent(0.15).cgColor
    }
}

/// A name cell: icon, title and an optional second line (the snippet of a content match).
final class SearchCell: NSTableCellView {
    let title = NSTextField(labelWithString: "")
    let detail = NSTextField(labelWithString: "")
    let tagDots = NSImageView()           // the file's colour tags, after the name (TagStore.dotsImage)
    private(set) var tagDotsWidth: NSLayoutConstraint!      // the dots' width: 0 when there are none

    init(identifier: String) {
        super.init(frame: .zero)
        self.identifier = NSUserInterfaceItemIdentifier(identifier)
        let icon = NSImageView()
        icon.translatesAutoresizingMaskIntoConstraints = false
        icon.imageScaling = .scaleProportionallyDown
        title.lineBreakMode = .byTruncatingTail
        detail.lineBreakMode = .byTruncatingTail
        detail.textColor = .secondaryLabelColor
        let text = NSStackView(views: [title, detail])
        text.orientation = .vertical
        text.alignment = .leading
        text.spacing = 1
        text.translatesAutoresizingMaskIntoConstraints = false
        tagDots.translatesAutoresizingMaskIntoConstraints = false
        tagDots.imageScaling = .scaleNone
        tagDots.setContentHuggingPriority(.required, for: .horizontal)
        tagDotsWidth = tagDots.widthAnchor.constraint(equalToConstant: 0)
        title.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        addSubview(icon)
        addSubview(text)
        addSubview(tagDots)
        imageView = icon
        textField = title
        NSLayoutConstraint.activate([
            icon.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 4),
            icon.centerYAnchor.constraint(equalTo: centerYAnchor),
            icon.widthAnchor.constraint(equalToConstant: 16),
            icon.heightAnchor.constraint(equalToConstant: 16),
            text.leadingAnchor.constraint(equalTo: icon.trailingAnchor, constant: 6),
            text.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -4),
            text.centerYAnchor.constraint(equalTo: centerYAnchor),
            tagDots.leadingAnchor.constraint(equalTo: title.trailingAnchor, constant: 6),
            tagDots.centerYAnchor.constraint(equalTo: title.centerYAnchor),
            tagDots.trailingAnchor.constraint(lessThanOrEqualTo: trailingAnchor, constant: -4),
            tagDotsWidth,
        ])
    }

    required init?(coder: NSCoder) { fatalError("not used") }
}

/// The message shown over an empty result list: what happened, and the buttons that get out of it.
@MainActor
final class SearchEmptyState: NSStackView {
    private let titleLabel = NSTextField(wrappingLabelWithString: "")
    private let detailLabel = NSTextField(wrappingLabelWithString: "")
    private let buttons = NSStackView()
    private var actions: [() -> Void] = []

    init() {
        super.init(frame: .zero)
        orientation = .vertical
        alignment = .centerX
        spacing = 8
        titleLabel.font = .systemFont(ofSize: 14, weight: .medium)
        titleLabel.textColor = .labelColor
        titleLabel.alignment = .center
        detailLabel.font = .systemFont(ofSize: 12)
        detailLabel.textColor = .secondaryLabelColor
        detailLabel.alignment = .center
        buttons.orientation = .horizontal
        buttons.spacing = 8
        addArrangedSubview(titleLabel)
        addArrangedSubview(detailLabel)
        addArrangedSubview(buttons)
        isHidden = true
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    func show(title: String, detail: String?, actions list: [(String, () -> Void)]) {
        titleLabel.stringValue = title
        detailLabel.stringValue = detail ?? ""
        detailLabel.isHidden = detail == nil
        buttons.arrangedSubviews.forEach { $0.removeFromSuperview() }
        actions = list.map { $0.1 }
        for (i, item) in list.enumerated() {
            let b = NSButton(title: item.0, target: self, action: #selector(pressed(_:)))
            b.bezelStyle = .rounded
            b.tag = i
            buttons.addArrangedSubview(b)
        }
        buttons.isHidden = list.isEmpty
        isHidden = false
    }

    @objc private func pressed(_ sender: NSButton) {
        guard sender.tag >= 0, sender.tag < actions.count else { return }
        actions[sender.tag]()
    }
}

/// The header row of the Content matches section: a chevron, the title and what state the section is in. A click
/// on the row, Return, or the arrow keys open and close it.
@MainActor
private final class SectionHeaderCell: NSTableCellView {
    let title = NSTextField(labelWithString: "")
    let tail = NSTextField(labelWithString: "")
    let chevron = NSImageView()

    override var backgroundStyle: NSView.BackgroundStyle {
        didSet { recolor() }
    }

    init() {
        super.init(frame: .zero)
        title.font = .systemFont(ofSize: 10, weight: .semibold)
        tail.font = .systemFont(ofSize: 11)
        tail.lineBreakMode = .byTruncatingTail
        chevron.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 9, weight: .bold)
        for v in [chevron, title, tail] as [NSView] {
            v.translatesAutoresizingMaskIntoConstraints = false
            addSubview(v)
        }
        textField = title
        NSLayoutConstraint.activate([
            chevron.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 4),
            chevron.centerYAnchor.constraint(equalTo: centerYAnchor),
            chevron.widthAnchor.constraint(equalToConstant: 10),
            title.leadingAnchor.constraint(equalTo: chevron.trailingAnchor, constant: 6),
            title.centerYAnchor.constraint(equalTo: centerYAnchor),
            tail.leadingAnchor.constraint(equalTo: title.trailingAnchor, constant: 8),
            tail.trailingAnchor.constraint(lessThanOrEqualTo: trailingAnchor, constant: -4),
            tail.centerYAnchor.constraint(equalTo: centerYAnchor),
        ])
        title.setContentCompressionResistancePriority(.required, for: .horizontal)
        recolor()
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    private func recolor() {
        let emphasized = backgroundStyle == .emphasized
        title.textColor = emphasized ? .alternateSelectedControlTextColor : .secondaryLabelColor
        tail.textColor = emphasized ? .alternateSelectedControlTextColor : .tertiaryLabelColor
        chevron.contentTintColor = title.textColor
    }
}

@MainActor
final class SearchViewController: NSViewController, NSTableViewDataSource, NSTableViewDelegate,
                                  NSMenuDelegate, QLPreviewPanelDataSource {
    private enum Row {
        case header(String)
        case contentHeader
        case file(SearchItem)
        case content(ContentHit)
        case pick(PickItem)
    }

    /// Where the content matches stand: not asked for, on their way, in, or failed.
    private enum ContentState { case idle, loading, loaded, failed }

    // Callbacks to the main window.
    /// The status line (under the content): counts, time, scope, indexing progress.
    var onStatus: ((String) -> Void)?
    /// Open a folder in Mac mode (a folder row, Return or double-click).
    var onOpenFolder: ((URL) -> Void)?
    /// Show the item in its folder, selected there (⌘Return, Show in Enclosing Folder). The URL is the item itself.
    var onRevealFolder: ((URL) -> Void)?
    /// Replace the search text (Search only here).
    var onRequestQuery: ((String) -> Void)?
    /// Pick Indexed folders (Return or double-click on a picker row); the names are collections.
    var onPick: (([String]) -> Void)?
    /// Esc: clear the search.
    var onEscape: (() -> Void)?
    /// Open a content match (or an indexed file) in the web UI's Search view: the rich preview with the favourite
    /// star, Not relevant, Find similar and Copy for AI. (query, scope names, collection, path inside the collection)
    var onOpenPreview: ((String, [String], String, String) -> Void)?
    /// A scope was chosen with the scope button: (the choice, its folders, the search text without a typed scope
    /// shortcut, or nil when the text stays). The main window moves the mode and the sidebar selection.
    var onScopeChosen: ((ScopeChoice, [String], String?) -> Void)?
    /// Searches the user acted on are remembered for the search field's recent-searches menu (off in snapshots).
    var recordsRecents = true

    private(set) var scope: SearchScope = .mac
    private(set) var indexedNames: [String] = []     // empty = every indexed folder
    private(set) var query = ""
    private(set) var library: IndexedLibrary?

    private let banner = NSTextField(wrappingLabelWithString: "")
    private let emptyState = SearchEmptyState()
    private let table = SearchTable()
    private let scroll = NSScrollView()
    private let bytes: ByteCountFormatter = { let f = ByteCountFormatter(); f.countStyle = .file; return f }()
    private let dates: DateFormatter = {
        let f = DateFormatter(); f.dateStyle = .medium; f.timeStyle = .short; return f
    }()
    private let counts: NumberFormatter = { let f = NumberFormatter(); f.numberStyle = .decimal; return f }()

    private var fileItems: [SearchItem] = []         // name matches, in relevance order
    private var contentHits: [ContentHit] = []
    private var namePaths = Set<String>()            // the paths of fileItems (a content match found by name too says so)
    private var picker: [PickItem] = []
    private var rows: [Row] = []
    private var showingPicker = false
    private var fallback = false

    private var generation = 0
    private var nameLoading = false
    private var contentState: ContentState = .idle
    private var contentLoadedGen = -1                // the generation the content matches on screen belong to
    private var resultCount: Int?
    private var resultMs: Int?
    private var crawling = false
    private var progress: Double = 0
    private var phase = ""
    /// What /api/files/status said last: the whole-Mac index can answer only when it is searchable.
    private var macSearchable = true
    private var macEnabled = true
    /// Why the name rows come from the indexed folders instead of the Mac index: nil = they do not.
    private enum Fallback { case notInstalled, indexing, off }
    private var fallbackReason: Fallback?
    /// The name index cannot answer (not installed, still being built, switched off) and no name rows stand in for it.
    private var namesUnavailable = false
    /// Why there is nothing to list: the service did not answer (retry helps) or the text is not a complete query.
    private var errorText: String?
    private var errorRetry = false

    private var nameTask: Task<Void, Never>?
    private var contentTask: Task<Void, Never>?
    private var statusTask: Task<Void, Never>?
    private var recentTask: Task<Void, Never>?
    private var aliasTask: Task<Void, Never>?
    private var previewURLs: [URL] = []

    // Where to search, and what to search for, per window.
    private var catalog = ScopeCatalog()
    /// A scope shortcut typed in the search field ("work: invoice"); it wins over the sidebar's choice for that search.
    private var shortcut: ScopeShortcut?
    private var shortcutUnknown: [String] = []
    private var scopePopover: ScopePopover?
    /// File names and Contents. Kept per window, outside the filters (Clear filters leaves them alone). Both on is the
    /// full search; File names alone is the fast one. Never both off.
    private(set) var namesOn = true
    private(set) var contentsOn = true
    /// Whether the Content matches section is open. Closed, it asks the service for nothing.
    private(set) var contentsExpanded = true

    /// The filters of this window (kept per window: each main window has its own search view).
    private(set) var filters = SearchFilters()
    private let filterBar = SearchFilterBar()
    private let facetRow = FacetRow()
    private let alsoLabel = NSTextField(labelWithString: "")
    /// The facets and bridge terms of the last content search, and whether the re-ranked list is on screen.
    private var facets: SearchFacets?
    private var bridgeTerms: [String] = []
    private var reranked = false
    /// The precise (re-ranked) answer is on its way: the web's second request after the instant results.
    private var refining = false
    private var dateSheet: DateRangeSheet?

    /// True while a name or content request is in flight (the snapshot waits on this).
    var isLoading: Bool { nameLoading || contentState == .loading || refining }

    override func loadView() {
        view = NSView()
        // Name and Folder are flexible (they share any extra width); Size, Date Modified and Kind keep their width.
        let column: (String, String, CGFloat, CGFloat, Bool) -> NSTableColumn = { id, title, width, min, sortable in
            let c = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(id))
            c.title = title
            c.width = width
            c.minWidth = min
            c.resizingMask = (id == "name" || id == "folder") ? [.userResizingMask, .autoresizingMask] : .userResizingMask
            if sortable { c.sortDescriptorPrototype = NSSortDescriptor(key: id, ascending: true) }
            return c
        }
        table.addTableColumn(column("name", "Name", 340, 160, true))
        table.addTableColumn(column("folder", "Folder", 300, 120, true))
        table.addTableColumn(column("size", "Size", 84, 60, true))
        table.addTableColumn(column("date", "Date Modified", 180, 120, true))
        table.addTableColumn(column("kind", "Kind", 120, 80, true))
        table.style = .fullWidth
        table.rowHeight = 20
        table.intercellSpacing = NSSize(width: 8, height: 0)
        table.usesAlternatingRowBackgroundColors = false
        table.allowsMultipleSelection = true
        table.allowsEmptySelection = true
        table.columnAutoresizingStyle = .uniformColumnAutoresizingStyle
        table.dataSource = self
        table.delegate = self
        table.target = self
        table.doubleAction = #selector(doubleClicked(_:))
        table.onKey = { [weak self] event in self?.handleKey(event) ?? false }
        table.onRowClick = { [weak self] row in self?.headerClicked(row) ?? false }
        table.setDraggingSourceOperationMask(.copy, forLocal: false)
        let menu = NSMenu()
        menu.delegate = self
        table.menu = menu

        scroll.documentView = table
        scroll.hasVerticalScroller = true
        scroll.hasHorizontalScroller = false
        scroll.drawsBackground = false
        scroll.translatesAutoresizingMaskIntoConstraints = false

        banner.font = .systemFont(ofSize: 11)
        banner.textColor = .secondaryLabelColor
        banner.isHidden = true
        banner.translatesAutoresizingMaskIntoConstraints = false
        emptyState.translatesAutoresizingMaskIntoConstraints = false

        alsoLabel.font = .systemFont(ofSize: 11)
        alsoLabel.textColor = .secondaryLabelColor
        alsoLabel.lineBreakMode = .byTruncatingTail
        alsoLabel.isHidden = true
        alsoLabel.translatesAutoresizingMaskIntoConstraints = false
        filterBar.onChange = { [weak self] f in self?.filtersChanged(f) }
        filterBar.onCustomDate = { [weak self] in self?.showDateSheet() }
        filterBar.onClear = { [weak self] in self?.clearFilters() }
        filterBar.onEscape = { [weak self] in self?.onEscape?() }
        filterBar.onScopeButton = { [weak self] in self?.toggleScopePicker() }
        filterBar.onSourcesChanged = { [weak self] names, contents, fromNames in
            self?.sourcesChanged(names: names, contents: contents, fromNames: fromNames)
        }
        facetRow.onPick = { [weak self] pick, on in self?.facetPicked(pick, on: on) }

        let header = NSStackView(views: [filterBar, banner, facetRow, alsoLabel])
        header.orientation = .vertical
        header.alignment = .leading
        header.spacing = 4
        header.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(header)
        view.addSubview(scroll)
        view.addSubview(emptyState)
        NSLayoutConstraint.activate([
            header.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor, constant: 8),
            header.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 12),
            header.trailingAnchor.constraint(lessThanOrEqualTo: view.trailingAnchor, constant: -12),
            scroll.topAnchor.constraint(equalTo: header.bottomAnchor, constant: 6),
            scroll.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            scroll.bottomAnchor.constraint(equalTo: view.bottomAnchor),
            emptyState.centerXAnchor.constraint(equalTo: view.centerXAnchor),
            emptyState.centerYAnchor.constraint(equalTo: scroll.centerYAnchor),
            emptyState.widthAnchor.constraint(lessThanOrEqualTo: view.widthAnchor, constant: -48),
        ])
        refreshScopeUI()
    }

    // MARK: API from the main window

    /// Sets the scope. Names are the Indexed collections picked in the sidebar (empty = every indexed folder).
    func configure(scope newScope: SearchScope, names: [String]) {
        let changed = newScope != scope || names != indexedNames
        scope = newScope
        indexedNames = names
        if changed {
            facets = nil
            facetRow.update(nil, filters: filters)
            refreshScopeUI()
            updateAlso()
        }
    }

    /// Snapshot and test hook: sets the filters as if chosen in the bar (the search runs again when there is a query).
    func applyFilters(_ f: SearchFilters) {
        filterBar.show(f)
        filtersChanged(f)
    }

    /// Snapshot hook: the custom range sheet, open with the default range.
    func snapshotOpenCustomDate() { showDateSheet() }

    /// The custom range sheet, for the snapshot (a separate window, composited into the main window's picture).
    var openSheet: NSWindow? { dateSheet?.sheetPanel }

    /// The library (collections and groups), for the Indexed picker and the scope button.
    func setLibrary(_ lib: IndexedLibrary) {
        library = lib
        catalog.update(library: lib)
        picker = lib.collections.map { PickItem(name: $0.name, path: $0.path, files: $0.files, members: [$0.name], isGroup: false) }
            + lib.groups.map { PickItem(name: $0.name, path: nil, files: $0.members.count, members: $0.members, isGroup: true) }
        if showingPicker { rebuild() }
        refreshScopeUI()
        loadShortcutNames()
    }

    /// The short names ("sp") and group labels come with one more read of /api/collections; the library does not carry them.
    private func loadShortcutNames() {
        aliasTask?.cancel()
        aliasTask = Task { [weak self] in
            guard let obj = try? await MSEClient.shared.get("/api/collections") as? [String: Any] else { return }
            guard let self, !Task.isCancelled else { return }
            self.catalog.merge(json: obj)
            self.refreshScopeUI()
            // A shortcut typed before its short name was known: ask again with the scope it names.
            if !self.query.isEmpty, ScopeShortcut.parse(self.query, catalog: self.catalog).tokens != (self.shortcut?.tokens ?? []) {
                self.run(self.query)
            }
        }
    }

    /// Runs `text` as the query. Empty text cancels the search; in Indexed mode it shows the picker.
    func run(_ text: String) {
        query = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let parsed = ScopeShortcut.parse(query, catalog: catalog)
        shortcut = parsed.found ? parsed : nil
        shortcutUnknown = parsed.unknown
        generation += 1
        let gen = generation
        nameTask?.cancel()
        contentTask?.cancel()
        recentTask?.cancel()
        nameLoading = false
        contentState = .idle
        refining = false
        reranked = false
        facets = nil
        bridgeTerms = []
        errorText = nil
        errorRetry = false
        namesUnavailable = false
        facetRow.update(nil, filters: filters)
        refreshScopeUI()
        updateAlso()
        guard !query.isEmpty else {
            fileItems = []
            contentHits = []
            resultCount = nil
            resultMs = nil
            fallback = false
            setBanner(nil)
            if scope == .indexed {
                showingPicker = true
                rebuild()
            } else {
                showingPicker = false
                rows = []
                table.reloadData()
                updateEmptyNote()
            }
            return
        }
        showingPicker = false
        emptyState.isHidden = true
        // A shortcut with no words after it ("work:") names where to look but not what for: wait for the words.
        if let sc = shortcut, sc.rest.isEmpty {
            fileItems = []
            contentHits = []
            resultCount = nil
            resultMs = nil
            fallback = false
            setBanner(nil)
            errorText = "Searching \(catalog.description(for: shownChoice)). Now type what to look for."
            errorRetry = false
            rebuild()
            return
        }
        let q = query
        if !contentsOpen { contentHits = [] }
        if wantNames {
            nameLoading = true
            // The query is sent after 40 ms of quiet, so typing fast sends one request per pause.
            nameTask = Task { [weak self] in
                try? await Task.sleep(nanoseconds: 40_000_000)
                guard let self, !Task.isCancelled, gen == self.generation else { return }
                await self.fetchNames(q, gen)
            }
        } else {
            // Contents only: no name request at all.
            fileItems = []
            resultCount = nil
            resultMs = nil
            fallback = false
            setBanner(nil)
            startContent(q, gen, delay: 120)
        }
    }

    /// Focuses the results table (the search field keeps the query).
    func focusTable() {
        view.window?.makeFirstResponder(table)
    }

    // MARK: scope and sources

    /// The scope this search really has: a typed shortcut means the indexed folders it names.
    private var activeScope: SearchScope { shortcut != nil ? .indexed : scope }

    /// What the scope button shows.
    private var shownChoice: ScopeChoice {
        if let s = shortcut { return catalog.choice(forTokens: s.tokens) }
        return scope == .mac ? .wholeMac : catalog.choice(forNames: indexedNames)
    }

    /// The `c` parameter of the searches: a typed shortcut's names, else the sidebar's folders; nil = every indexed folder.
    private var scopeParameter: String? {
        if let s = shortcut { return s.tokens.joined(separator: ",") }
        return indexedNames.isEmpty ? nil : indexedNames.joined(separator: ",")
    }

    /// File names are searched in the Whole Mac scope always, in the indexed scope when the box is ticked.
    private var wantNames: Bool { activeScope == .mac || namesOn }

    /// Contents are searched in the indexed scope when the box is ticked.
    private var wantContents: Bool { activeScope == .indexed && contentsOn }

    /// The Content matches section is on screen (it is not while the name rows stand in from the hybrid search).
    private var contentSectionVisible: Bool { wantContents && !fallback }

    /// The section can be closed: only while file names are listed above it.
    private var contentsCollapsible: Bool { contentSectionVisible && wantNames && !namesUnavailable }

    /// The section is open (its matches are listed, and fetched).
    private var contentsOpen: Bool { contentSectionVisible && (!contentsCollapsible || contentsExpanded) }

    /// Shows the scope, the two boxes and the filters that fit, in the filter bar. The picker, when open, follows too.
    private func refreshScopeUI() {
        let want: SearchFilterBar.Mode = activeScope == .mac ? .mac : .indexed
        if filterBar.mode != want { filterBar.setMode(want) }
        let c = shownChoice
        filterBar.setScope(title: catalog.label(for: c), symbol: catalog.symbol(for: c),
                           tooltip: "Searching " + catalog.description(for: c) + ".", fromQuery: shortcut != nil)
        filterBar.setSources(names: namesOn, contents: contentsOn, macScope: activeScope == .mac)
        if scopePopover?.isShown == true { scopePopover?.picker.update(catalog: catalog, choice: c) }
    }

    /// ⌘⌥F and the scope button: opens the picker, or closes it.
    func toggleScopePicker() {
        if let p = scopePopover, p.isShown { p.close(); return }
        guard view.window != nil else { return }
        let p = ScopePopover(catalog: catalog, choice: shownChoice)
        p.picker.onChoose = { [weak self] c in self?.scopePicked(c) }
        p.onClosed = { [weak self] in self?.scopePopover = nil }
        scopePopover = p
        p.show(relativeTo: filterBar.scopeAnchor)
    }

    var isScopePickerShown: Bool { scopePopover?.isShown == true }

    /// A choice in the picker or an empty-state button. A shortcut typed in the search field would override it, so the
    /// main window takes the shortcut out of the text.
    private func scopePicked(_ c: ScopeChoice) {
        onScopeChosen?(c, catalog.names(for: c), shortcut?.rest)
        if view.window == nil { scopePopover?.close() }
    }

    /// File names or Contents was ticked: re-ask only what the change needs. Never both off.
    private func sourcesChanged(names: Bool, contents: Bool, fromNames: Bool) {
        guard activeScope == .indexed else { refreshScopeUI(); return }
        var n = names, c = contents
        if !n && !c { if fromNames { c = true } else { n = true } }
        let namesWereOff = !namesOn
        namesOn = n
        contentsOn = c
        refreshScopeUI()
        guard !query.isEmpty else { return }
        if n && namesWereOff { run(query); return }                   // the names were never asked for
        if !n { nameTask?.cancel(); nameLoading = false }              // contents only from here on
        if !c {
            contentTask?.cancel()
            contentState = .idle
            contentHits = []
            refining = false
            reranked = false
            facets = nil
            bridgeTerms = []
            facetRow.update(nil, filters: filters)
            updateAlso()
            rebuild()
            return
        }
        if contentState != .loaded || contentLoadedGen != generation, contentsOpen, !nameLoading {
            startContent(query, generation, delay: 0)
        } else {
            rebuild()
        }
    }

    /// Opens or closes the Content matches section. Opening asks for the matches if they are not in yet; closing
    /// stops a request on its way.
    func setContentsExpanded(_ open: Bool) {
        guard contentsCollapsible else { return }
        contentsExpanded = open
        if !open {
            if contentState == .loading { contentTask?.cancel(); contentState = .idle; refining = false }
            rebuild()
            return
        }
        if contentState != .loaded || contentLoadedGen != generation, !query.isEmpty, !nameLoading {
            startContent(query, generation, delay: 0)
        } else {
            rebuild()
        }
    }

    /// The header row: open and close, or retry after a failure.
    private func activateContentHeader() {
        if contentState == .failed, !query.isEmpty {
            startContent(query, generation, delay: 0)
            return
        }
        setContentsExpanded(!contentsExpanded)
    }

    /// A click on the Content matches header row (the table asks before it selects).
    private func headerClicked(_ row: Int) -> Bool {
        guard row >= 0, row < rows.count, case .contentHeader = rows[row] else { return false }
        if (NSApp.currentEvent?.clickCount ?? 1) <= 1 { activateContentHeader() }
        return true
    }

    // MARK: jumps (⌃1, ⌃2, ⌃3)

    /// ⌃1: the file name rows.
    func jumpToNames() {
        guard let i = rows.firstIndex(where: { if case .file = $0 { return true }; return false }) ?? rows.firstIndex(where: { isSelectable($0) }) else { return }
        select(row: i)
    }

    /// ⌃2: the Content matches section; it opens if it was closed.
    func jumpToContents() {
        guard contentSectionVisible else { return }
        if contentsCollapsible && !contentsExpanded { setContentsExpanded(true) }
        let first = rows.firstIndex(where: { if case .content = $0 { return true }; return false })
        guard let i = first ?? rows.firstIndex(where: { if case .contentHeader = $0 { return true }; return false }) else { return }
        select(row: i)
    }

    /// ⌃3: the filters.
    func jumpToFilters() {
        filterBar.focusFirstFilter()
    }

    private func select(row i: Int) {
        view.window?.makeFirstResponder(table)
        table.selectRowIndexes(IndexSet(integer: i), byExtendingSelection: false)
        table.scrollRowToVisible(i)
    }

    // MARK: fetching

    private func fetchNames(_ q: String, _ gen: Int) async {
        let started = Date()
        var items = [URLQueryItem(name: "q", value: nameQuery(q)), URLQueryItem(name: "limit", value: "200"),
                     URLQueryItem(name: "scope", value: activeScope == .mac ? "mac" : "indexed")]
        if activeScope == .indexed, let c = scopeParameter {
            items.append(URLQueryItem(name: "c", value: c))
        }
        do {
            let obj = try await MSEClient.shared.get("/api/files/search", query: items)
            guard gen == generation, !Task.isCancelled else { return }
            let dict = (obj as? [String: Any]) ?? [:]
            // The first crawl builds the name index in bulk: until it ends the service says searchable: false (an
            // empty list that is not "no matches"). Show the progress and answer from the indexed folders meanwhile.
            if (dict["searchable"] as? Bool) == false {
                macSearchable = false
                refreshStatus()
                await namesNotReady(q, gen, started, reason: macEnabled ? .indexing : .off)
                return
            }
            macSearchable = true
            fallbackReason = nil
            namesUnavailable = false
            let parsed = ((dict["items"] as? [[String: Any]]) ?? []).compactMap(SearchItem.parse)
            fileItems = parsed.filter { filters.keeps(path: $0.path) }     // the globs the operators cannot say
            resultCount = fileItems.count == parsed.count ? ((dict["total"] as? NSNumber)?.intValue ?? fileItems.count) : fileItems.count
            resultMs = (dict["ms"] as? NSNumber)?.intValue
            fallback = false
            setBanner(nil)
            nameLoading = false
            if !contentsOpen { contentHits = []; contentState = .idle }
            rebuild()
            // Contents follow the names, after a short pause: typing on does not start a deep search per keystroke.
            if contentsOpen { startContent(q, gen, delay: 220) }
            refreshStatus()
            scheduleRecent(gen)
        } catch let APIError.http(code) where code == 404 {
            guard gen == generation, !Task.isCancelled else { return }
            await namesNotReady(q, gen, started, reason: .notInstalled)
        } catch let APIError.http(code) where code == 400 {
            // The service refuses a query it cannot read: an unfinished operator, or a name after in: it does not know.
            guard gen == generation, !Task.isCancelled else { return }
            fail(queryProblem(), retry: false)
        } catch {
            guard gen == generation, !Task.isCancelled else { return }
            fail("The search service did not answer.", retry: true)
        }
    }

    /// Nothing can be listed: say why (empty state), keep the section state honest.
    private func fail(_ message: String, retry: Bool) {
        nameLoading = false
        fileItems = []
        contentHits = []
        contentState = .idle
        resultCount = nil
        resultMs = nil
        errorText = message
        errorRetry = retry
        setBanner(nil)
        rebuild()
    }

    private func queryProblem() -> String {
        if !shortcutUnknown.isEmpty {
            let known = catalog.folders.map { $0.name } + catalog.groups.map { $0.name }
            let names = shortcutUnknown.joined(separator: ", ")
            return "No folder or group called “\(names)” after in:" + (known.isEmpty ? "." : ". Known: " + known.joined(separator: ", ") + ".")
        }
        return "The search text is not a complete query yet (an operator, or a name after in:, is unfinished)."
    }

    /// The name index cannot answer now (not installed, still being built, switched off).
    /// - Whole Mac: name rows from the indexed folders' own search stand in.
    /// - Indexed with Contents on: the Content matches carry the answer.
    /// - Indexed with Contents off: nothing deeper is asked; the empty state offers to turn Contents on.
    private func namesNotReady(_ q: String, _ gen: Int, _ started: Date, reason: Fallback) async {
        fallbackReason = reason
        setBanner(bannerText(for: reason))
        if activeScope == .indexed {
            fallback = false
            fileItems = []
            resultCount = nil
            resultMs = nil
            nameLoading = false
            namesUnavailable = true
            contentHits = []
            contentState = .idle
            rebuild()
            if wantContents { startContent(q, gen, delay: 0) }
            return
        }
        await fetchHybridRows(q, gen, started)
    }

    private func bannerText(for reason: Fallback) -> String {
        let standIn: String
        if activeScope == .mac {
            standIn = "Showing matches from your indexed folders until the file names are ready."
        } else if contentsOn {
            standIn = "The content matches answer meanwhile."
        } else {
            standIn = "Turn on Contents to search your indexed folders now."
        }
        switch reason {
        case .indexing:
            let pct = indexingPercent.map { " \($0)%" } ?? ""
            let what = phase.isEmpty ? "" : " (\(phase))"
            let lead = pct.isEmpty && what.isEmpty ? "Indexing your Mac… " : "Indexing your Mac…\(pct)\(what). "
            return lead + standIn
        case .off:
            return "Searching file names across the Mac is switched off. " + (activeScope == .mac ? "Showing matches from your indexed folders." : standIn)
        case .notInstalled:
            return "Whole-Mac index not installed yet. " + (activeScope == .mac ? "Showing matches from the indexed folders." : standIn)
        }
    }

    /// The indexed folders' hybrid search shown as name rows (the Whole-Mac index cannot answer yet). Only the Whole Mac
    /// scope gets here: with Contents off, the indexed scope never asks the content search for anything.
    private func fetchHybridRows(_ q: String, _ gen: Int, _ started: Date) async {
        fallback = true
        let c = scopeParameter ?? "all"
        do {
            let items = [URLQueryItem(name: "q", value: q), URLQueryItem(name: "c", value: c),
                         URLQueryItem(name: "limit", value: "20")] + filters.contentParameters()
            let obj = try await MSEClient.shared.get("/api/search", query: items)
            guard gen == generation, !Task.isCancelled else { return }
            let results = ((obj as? [String: Any])?["results"] as? [[String: Any]]) ?? []
            var seen = Set<String>()
            fileItems = results.compactMap(SearchItem.fromHybrid).filter { seen.insert($0.path).inserted }
            contentHits = []
            contentState = .idle
            resultCount = fileItems.count
            resultMs = Int(Date().timeIntervalSince(started) * 1000)
            nameLoading = false
            rebuild()
            scheduleRecent(gen)
        } catch {
            guard gen == generation, !Task.isCancelled else { return }
            nameLoading = false
            fileItems = []
            resultCount = 0
            setBanner("The file-name index is not ready, and the indexed folders did not answer.")
            rebuild()
        }
    }

    private func updateIndexingBanner() {
        guard !query.isEmpty, let reason = fallbackReason, reason != .notInstalled else { return }
        setBanner(bannerText(for: reason))
    }

    /// The Content matches section's request. Starts after the names (or at once when they are not asked for) and
    /// replaces the section when it lands. Never runs unless Contents is on.
    private func startContent(_ q: String, _ gen: Int, delay: UInt64) {
        guard wantContents, gen == generation else { return }
        contentTask?.cancel()
        contentState = .loading
        refining = false
        rebuild()
        // The file-name operators (mtime:, size:, ext:, path:, re:, limit:, !word) mean nothing to a content search.
        let cq = q.replacingOccurrences(of: #"(^|\s)(!?(mtime|size|ext|kind|path|re|limit):\S*|![^\s:]+)"#, with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cq.isEmpty else {
            contentHits = []
            contentState = .loaded
            contentLoadedGen = gen
            rebuild()
            return
        }
        let terms = SnippetText.terms(from: cq)
        // The request is the web's: q, c, limit, then the filter parameters (mode, type, after, before, include, exclude).
        let items = [URLQueryItem(name: "q", value: cq), URLQueryItem(name: "c", value: scopeParameter ?? "all"),
                     URLQueryItem(name: "limit", value: "20")] + filters.contentParameters()
        contentTask = Task { [weak self] in
            if delay > 0 { try? await Task.sleep(nanoseconds: delay * 1_000_000) }
            guard let self, !Task.isCancelled, gen == self.generation else { return }
            do {
                let obj = try await MSEClient.shared.get("/api/search", query: items)
                guard gen == self.generation, !Task.isCancelled else { return }
                let dict = (obj as? [String: Any]) ?? [:]
                self.contentHits = ((dict["results"] as? [[String: Any]]) ?? []).compactMap { ContentHit.parse($0, terms: terms) }
                self.facets = (dict["facets"] as? [String: Any]).flatMap(SearchFacets.init)
                self.bridgeTerms = SearchViewController.bridgeList(dict["bridge"])
                self.contentState = .loaded
                self.contentLoadedGen = gen
                self.facetRow.update(self.facets, filters: self.filters)
                self.updateAlso()
                self.rebuild()
                self.scheduleRecent(gen)
                // The web's second request: the re-ranker re-checks the list (precise=1) and replaces it when the order changes.
                if self.contentHits.count >= 2 {
                    await self.refine(items, gen, before: self.contentHits.map { $0.path }, terms: terms)
                }
            } catch {
                guard gen == self.generation, !Task.isCancelled else { return }
                self.contentHits = []
                self.contentState = .failed
                self.rebuild()
            }
        }
    }

    /// The precise (re-ranked) answer. It replaces the content matches only when their order changes, as the web does.
    private func refine(_ items: [URLQueryItem], _ gen: Int, before: [String], terms: [String]) async {
        guard wantContents else { return }
        refining = true
        defer { refining = false }
        rebuild()
        do {
            let obj = try await MSEClient.shared.get("/api/search", query: items + [URLQueryItem(name: "precise", value: "1")])
            guard gen == generation, !Task.isCancelled else { return }
            let hits = (((obj as? [String: Any])?["results"] as? [[String: Any]]) ?? []).compactMap { ContentHit.parse($0, terms: terms) }
            guard hits.map({ $0.path }) != before else { return }
            contentHits = hits
            reranked = true
            rebuild()
        } catch {
            // superseded by a newer search, or the re-ranker is not answering: the instant results stay
        }
    }

    /// Remembers the search for the recent-searches menu once it has stood for two seconds with something listed.
    private func scheduleRecent(_ gen: Int) {
        guard recordsRecents else { return }
        recentTask?.cancel()
        let q = query
        recentTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            guard let self, !Task.isCancelled, gen == self.generation, !(self.fileItems.isEmpty && self.contentHits.isEmpty) else { return }
            RecentSearches.record(q)
        }
    }

    /// The bridge terms of a response ({collection: [terms]}), once each, in the order they came.
    static func bridgeList(_ v: Any?) -> [String] {
        guard let obj = v as? [String: Any] else { return [] }
        var seen = Set<String>(), out: [String] = []
        for key in obj.keys.sorted() {
            for term in (obj[key] as? [String]) ?? [] where seen.insert(term).inserted { out.append(term) }
        }
        return out
    }

    /// "Also searched" under the filter bar: the translation bridge's words, as the web shows them.
    private func updateAlso() {
        guard contentSectionVisible, !showingPicker, !bridgeTerms.isEmpty else { alsoLabel.isHidden = true; return }
        let s = NSMutableAttributedString(string: "Also searched: ",
                                          attributes: [.font: NSFont.systemFont(ofSize: 11), .foregroundColor: NSColor.secondaryLabelColor])
        s.append(NSAttributedString(string: bridgeTerms.joined(separator: " · "),
                                    attributes: [.font: NSFont.systemFont(ofSize: 11, weight: .medium), .foregroundColor: NSColor.controlAccentColor]))
        alsoLabel.attributedStringValue = s
        alsoLabel.isHidden = false
    }

    /// The name query sent to /api/files/search: the typed text plus the Mac operators of the filters.
    private func nameQuery(_ q: String) -> String {
        (([q] + filters.macOperators()).joined(separator: " ")).trimmingCharacters(in: .whitespaces)
    }

    // MARK: filter actions

    /// Any filter change: the sort order re-orders the rows already here; every other change runs the search again.
    private func filtersChanged(_ f: SearchFilters) {
        let old = filters
        filters = f
        guard !query.isEmpty else { return }
        var sameExceptSort = old
        sameExceptSort.sort = f.sort
        if sameExceptSort == f { rebuild() } else { run(query) }
    }

    private func clearFilters() {
        let empty = SearchFilters()
        filterBar.show(empty)
        filtersChanged(empty)
    }

    /// A facet chip: a kind toggles its type, a year sets its whole year, a folder picks that folder (as the web does).
    private func facetPicked(_ pick: FacetPick, on: Bool) {
        var f = filters
        switch pick {
        case .kind(let k): f.kind = on ? .any : k
        case .year(let y):
            if on {
                f.date = .any
            } else if let n = Int(y), let a = Calendar.current.date(from: DateComponents(year: n, month: 1, day: 1)),
                      let b = Calendar.current.date(from: DateComponents(year: n, month: 12, day: 31)) {
                f.date = .custom(a, b)
            }
        case .collection(let c):
            onPick?([c])
            return
        }
        filterBar.show(f)
        filtersChanged(f)
    }

    /// The custom range sheet: From and To. Apply sets the range; Cancel leaves the filters as they were.
    private func showDateSheet() {
        guard let win = view.window else { return }
        var from = Calendar.current.date(byAdding: .day, value: -30, to: Date()) ?? Date()
        var to = Date()
        if case .custom(let a, let b) = filters.date { from = a; to = b }
        let sheet = DateRangeSheet(from: from, to: to, apply: { [weak self] a, b in
            guard let self else { return }
            self.dateSheet = nil
            var f = self.filters
            f.date = .custom(a, b)
            self.filterBar.show(f)
            self.filtersChanged(f)
        }, cancel: { [weak self] in
            self?.dateSheet = nil
        })
        dateSheet = sheet
        sheet.present(on: win)
    }

    /// Percent of the first crawl, from status.progress: {phase, units_done, units_total, entries, fraction, current, elapsed_s}.
    private var indexingPercent: Int? {
        guard crawling || !macSearchable else { return nil }
        return progress > 0 ? Int((progress * 100).rounded()) : nil
    }

    /// Reads /api/files/status now and, while the Mac is being indexed (or the name index is not searchable yet),
    /// every two seconds. When the index becomes searchable the query in the field runs again by itself.
    private func refreshStatus() {
        statusTask?.cancel()
        statusTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                guard let obj = try? await MSEClient.shared.get("/api/files/status") as? [String: Any] else {
                    self.crawling = false
                    self.onStatus?(self.statusText())
                    return
                }
                self.crawling = (obj["crawling"] as? Bool) ?? false
                self.macEnabled = (obj["enabled"] as? Bool) ?? true
                let wasSearchable = self.macSearchable
                self.macSearchable = (obj["searchable"] as? Bool) ?? true
                let prog = obj["progress"] as? [String: Any]
                if let f = (prog?["fraction"] as? NSNumber)?.doubleValue {
                    self.progress = f
                } else if let done = (prog?["units_done"] as? NSNumber)?.doubleValue,
                          let total = (prog?["units_total"] as? NSNumber)?.doubleValue, total > 0 {
                    self.progress = done / total
                } else if !self.crawling {
                    self.progress = 0
                }
                self.phase = (prog?["phase"] as? String).map { $0 == "crawling" ? "" : $0 } ?? ""
                if self.fallbackReason == .indexing { self.updateIndexingBanner() }
                self.onStatus?(self.statusText())
                if !wasSearchable && self.macSearchable && self.fallbackReason != nil && self.fallbackReason != .notInstalled {
                    self.fallbackReason = nil
                    self.fallback = false
                    self.setBanner(nil)
                    if !self.query.isEmpty { self.run(self.query) }     // the names are ready: ask again
                }
                if !self.crawling && self.macSearchable { return }
                if !self.macEnabled { return }
                try? await Task.sleep(nanoseconds: 2_000_000_000)
            }
        }
    }

    // MARK: rows

    /// Rebuilds the rows, keeps the selection of every path that is still listed, and reports the status.
    private func rebuild() {
        let kept = selectedKeys()
        namePaths = Set(fileItems.map { $0.path })
        rows = buildRows()
        table.reloadData()
        var sel = IndexSet()
        for (i, r) in rows.enumerated() {
            if let k = key(of: r), kept.contains(k) { sel.insert(i) }
        }
        if sel.isEmpty, let first = rows.firstIndex(where: { isSelectable($0) }) { sel.insert(first) }
        if !sel.isEmpty { table.selectRowIndexes(sel, byExtendingSelection: false) }
        updateEmptyNote()
        onStatus?(statusText())
    }

    private func buildRows() -> [Row] {
        if showingPicker {
            var out: [Row] = []
            let folders = picker.filter { !$0.isGroup }
            let groups = picker.filter { $0.isGroup }
            if !folders.isEmpty {
                out.append(.header("Indexed folders"))
                out += folders.map { Row.pick($0) }
            }
            if !groups.isEmpty {
                out.append(.header("Groups"))
                out += groups.map { Row.pick($0) }
            }
            return out
        }
        if errorText != nil || noResults { return [] }
        var out: [Row] = []
        if wantNames && !namesUnavailable {
            // With both sections on screen the names get a header too: it carries their count and the ⌃1 target.
            if contentSectionVisible { out.append(.header(namesHeaderText)) }
            out += sortedFiles().map { Row.file($0) }
        }
        if contentSectionVisible {
            out.append(.contentHeader)
            if contentsOpen { out += sortedHits().map { Row.content($0) } }
        }
        return out
    }

    /// "File names · 12", or "File names · 200 of 1,234" when the list is cut at 200.
    private var namesHeaderText: String {
        if nameLoading { return "File names · searching…" }
        let shown = fileItems.count
        if let total = resultCount, total > shown {
            return "File names · \(counts.string(from: NSNumber(value: shown)) ?? "\(shown)") of \(counts.string(from: NSNumber(value: total)) ?? "\(total)")"
        }
        return "File names · \(counts.string(from: NSNumber(value: shown)) ?? "\(shown)")"
    }

    /// The Content matches header: its title and the state the section is in.
    private func contentHeaderParts() -> (String, String) {
        let n = contentHits.count
        let collapsed = contentsCollapsible && !contentsExpanded
        let loadedHere = contentState == .loaded && contentLoadedGen == generation
        let tail: String
        switch contentState {
        case .loading:
            tail = "searching…"
        case .failed:
            tail = "could not load · click to try again"
        case .loaded where loadedHere:
            let count = "\(counts.string(from: NSNumber(value: n)) ?? "\(n)") \(n == 1 ? "match" : "matches")"
            if collapsed { tail = count + " · click to show" }
            else if refining { tail = count + " · refining the order…" }
            else if reranked { tail = count + " · re-ranked" }
            else { tail = count }
        default:
            tail = collapsed ? "press ↓ or click to load" : "…"
        }
        return ("Content matches", tail)
    }

    /// Nothing at all to list for this query (and nothing still on its way): the empty state speaks instead of headers.
    private var noResults: Bool {
        guard !query.isEmpty, !showingPicker, !nameLoading, errorText == nil else { return false }
        if namesUnavailable && !wantContents { return true }
        if wantNames && !namesUnavailable && !fileItems.isEmpty { return false }
        if contentSectionVisible {
            switch contentState {
            case .loading, .failed: return false
            case .idle: return false
            case .loaded: return contentLoadedGen == generation && contentHits.isEmpty
            }
        }
        return true
    }

    /// The order the rows come in: the column the user clicked, else the Sort menu (native only), else relevance.
    /// Relevance is the tie-break, so equal keys keep the order the service gave.
    private func sortedFiles() -> [SearchItem] {
        guard let d = table.sortDescriptors.first ?? menuSortDescriptor, let key = d.key else { return fileItems }
        let indexed = Array(fileItems.enumerated())
        let sorted = indexed.sorted { a, b in
            let order = Self.compare(a.element, b.element, by: key)
            if order == .orderedSame { return a.offset < b.offset }
            return d.ascending ? order == .orderedAscending : order == .orderedDescending
        }
        return sorted.map { $0.element }
    }

    /// The Sort menu as a sort descriptor (Newest first is the date column, descending); nil for Best match.
    private var menuSortDescriptor: NSSortDescriptor? {
        switch filters.sort {
        case .best: return nil
        case .newest: return NSSortDescriptor(key: "date", ascending: false)
        case .oldest: return NSSortDescriptor(key: "date", ascending: true)
        case .name: return NSSortDescriptor(key: "name", ascending: true)
        }
    }

    /// Content matches in the Sort menu's order; the service's relevance order for Best match.
    private func sortedHits() -> [ContentHit] {
        guard let d = menuSortDescriptor, let key = d.key else { return contentHits }
        return contentHits.enumerated().sorted { a, b in
            let x = a.element, y = b.element
            let order: ComparisonResult
            if key == "name" {
                order = x.title.localizedStandardCompare(y.title)
            } else {
                let p = x.date ?? .distantPast, q = y.date ?? .distantPast
                order = p < q ? .orderedAscending : (p > q ? .orderedDescending : .orderedSame)
            }
            if order == .orderedSame { return a.offset < b.offset }
            return d.ascending ? order == .orderedAscending : order == .orderedDescending
        }.map { $0.element }
    }

    private static func compare(_ a: SearchItem, _ b: SearchItem, by key: String) -> ComparisonResult {
        switch key {
        case "name": return a.name.localizedStandardCompare(b.name)
        case "folder": return a.parent.localizedStandardCompare(b.parent)
        case "size":
            let x = a.size ?? -1, y = b.size ?? -1
            return x < y ? .orderedAscending : (x > y ? .orderedDescending : .orderedSame)
        case "date":
            let x = a.mtime ?? .distantPast, y = b.mtime ?? .distantPast
            return x < y ? .orderedAscending : (x > y ? .orderedDescending : .orderedSame)
        case "kind": return a.kind.localizedStandardCompare(b.kind)
        default: return .orderedSame
        }
    }

    private func key(of row: Row) -> String? {
        switch row {
        case .header: return nil
        case .contentHeader: return "h:content"
        case .file(let i): return "f:" + i.path
        case .content(let c): return "c:" + c.path
        case .pick(let p): return "p:" + p.name
        }
    }

    private func isSelectable(_ row: Row) -> Bool {
        if case .header = row { return false }
        return true
    }

    private func selectedKeys() -> Set<String> {
        var out = Set<String>()
        for i in table.selectedRowIndexes where i < rows.count {
            if let k = key(of: rows[i]) { out.insert(k) }
        }
        return out
    }

    /// The empty state: a service error, a query the service cannot read, or no matches, each with the way out.
    private func updateEmptyNote() {
        if showingPicker || query.isEmpty { emptyState.isHidden = true; return }
        if let e = errorText {
            emptyState.show(title: e, detail: nil,
                            actions: errorRetry ? [("Try again", { [weak self] in
                                guard let self else { return }
                                self.run(self.query)
                            })] : [])
            return
        }
        guard noResults else { emptyState.isHidden = true; return }
        let label = catalog.label(for: shownChoice)
        var actions: [(String, () -> Void)] = []
        let clear: (String, () -> Void) = ("Clear filters", { [weak self] in self?.clearFilters() })
        let title: String, detail: String?
        if namesUnavailable && !wantContents {
            title = "File names cannot be searched yet"
            detail = "The Mac’s name index is not ready. Contents of your indexed folders can answer now."
            actions.append(("Turn on Contents", { [weak self] in
                guard let self else { return }
                self.sourcesChanged(names: self.namesOn, contents: true, fromNames: false)
            }))
        } else if activeScope == .mac {
            title = "No file names match “\(query)”"
            detail = "The whole Mac is searched by name. Narrow it with kind:pdf, ext:docx or in:~/Documents, or search the contents of your indexed folders."
            actions.append(("Search indexed folders", { [weak self] in self?.scopePicked(.allIndexed) }))
        } else if !wantContents {
            title = "No file names match “\(query)” in \(label)"
            detail = "Fast mode searches names only. Contents looks inside the files."
            actions.append(("Search contents too", { [weak self] in
                guard let self else { return }
                self.sourcesChanged(names: self.namesOn, contents: true, fromNames: false)
            }))
            actions.append(("Whole Mac", { [weak self] in self?.scopePicked(.wholeMac) }))
        } else {
            title = "No matches for “\(query)” in \(label)"
            var hint = filters.method == .hybrid ? "Try other words, or widen the scope." : "Try “Words + meaning” in the method menu, or widen the scope."
            if shortcut != nil { hint = "The shortcut in the search text sets this scope. " + hint }
            detail = hint
            actions.append(("Whole Mac", { [weak self] in self?.scopePicked(.wholeMac) }))
            if shownChoice != .allIndexed { actions.append(("All indexed", { [weak self] in self?.scopePicked(.allIndexed) })) }
        }
        if filters.isActive { actions.append(clear) }
        emptyState.show(title: title, detail: detail, actions: actions)
    }

    private func setBanner(_ text: String?) {
        banner.stringValue = text ?? ""
        banner.isHidden = text == nil
    }

    // MARK: status

    private var scopeLabel: String {
        switch shownChoice {
        case .wholeMac: return fallback ? "Indexed: all folders" : "whole Mac"
        case .allIndexed: return "Indexed: all folders"
        default: return "Indexed: " + catalog.label(for: shownChoice)
        }
    }

    /// "12 names · 20 content matches · 87 ms · Indexed: Work"; Whole Mac: "1,234 results · 3 ms · whole Mac".
    /// While the Mac is being indexed, "indexing your Mac… 42%" comes first.
    func statusText() -> String {
        if showingPicker {
            let folders = library?.collections.count ?? 0
            let groups = library?.groups.count ?? 0
            return "\(folders) indexed folders · \(groups) groups"
        }
        func fmt(_ n: Int) -> String { counts.string(from: NSNumber(value: n)) ?? "\(n)" }
        var parts: [String] = []
        if crawling || !macSearchable, macEnabled {
            parts.append(progress > 0 ? "indexing your Mac… \(Int((progress * 100).rounded()))%" : "indexing your Mac…")
        }
        let sectioned = contentSectionVisible && wantNames && !namesUnavailable
        if wantNames && !namesUnavailable, let n = resultCount {
            parts.append("\(fmt(n)) " + (sectioned ? (n == 1 ? "name" : "names") : (n == 1 ? "result" : "results")))
        }
        if contentSectionVisible && !query.isEmpty {
            switch contentState {
            case .loaded where contentLoadedGen == generation:
                let m = contentHits.count
                parts.append("\(fmt(m)) content \(m == 1 ? "match" : "matches")")
            case .loading: parts.append("searching contents…")
            case .failed: parts.append("contents did not load")
            default: if sectioned && !contentsOpen { parts.append("contents not loaded") }
            }
        }
        if wantNames, !namesUnavailable, let ms = resultMs { parts.append("\(ms) ms") }
        if reranked && contentSectionVisible { parts.append("re-ranked") }
        if activeScope == .indexed && !wantContents && !fallback && !query.isEmpty { parts.append("names only") }
        parts.append(scopeLabel)
        return parts.joined(separator: " · ")
    }

    // MARK: table data

    func numberOfRows(in tableView: NSTableView) -> Int { rows.count }

    func tableView(_ tableView: NSTableView, shouldSelectRow row: Int) -> Bool { isSelectable(rows[row]) }

    func tableView(_ tableView: NSTableView, heightOfRow row: Int) -> CGFloat {
        switch rows[row] {
        case .header: return 22
        case .contentHeader: return 26
        case .content: return 56
        default: return 20
        }
    }

    func tableView(_ tableView: NSTableView, sortDescriptorsDidChange oldDescriptors: [NSSortDescriptor]) {
        rebuild()
    }

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        let id = tableColumn?.identifier.rawValue ?? ""
        switch rows[row] {
        case .header(let text):
            guard id == "name" else { return nil }
            let label = NSTextField(labelWithString: text.uppercased())
            label.font = .systemFont(ofSize: 10, weight: .semibold)
            label.textColor = .secondaryLabelColor
            label.lineBreakMode = .byTruncatingTail
            let c = NSTableCellView()
            c.addSubview(label)
            c.textField = label
            label.translatesAutoresizingMaskIntoConstraints = false
            NSLayoutConstraint.activate([
                label.leadingAnchor.constraint(equalTo: c.leadingAnchor, constant: 4),
                label.centerYAnchor.constraint(equalTo: c.centerYAnchor),
            ])
            return c
        case .contentHeader:
            guard id == "name" else { return nil }
            return contentHeaderCell()
        case .file(let item):
            return cellFor(id: id, name: item.name, match: item.match, detail: nil, icon: NSWorkspace.shared.icon(forFile: item.path),
                           folder: item.parent, size: item.isDir ? nil : item.size, date: item.mtime, dateText: item.dateText,
                           kind: item.isDir ? "Folder" : Self.kindName(item.kind), isDir: item.isDir,
                           tags: id == "name" ? FileItem(url: URL(fileURLWithPath: item.path)).tagColors : [], in: tableView)
        case .content(let hit):
            // Why it is here: the channels that found it ("Words + meaning"), and "Name" when its name matched too.
            let why = hit.whyText(alsoByName: namePaths.contains(hit.path))
            var tip = "Matched by " + why.lowercased()
            if let r = hit.relevance { tip += " · relevance \(r)%" }
            if !hit.kind.isEmpty { tip = Self.kindName(hit.kind) + " · " + tip }
            return cellFor(id: id, name: hit.title, match: [], detail: hit.snippet, icon: NSWorkspace.shared.icon(forFile: hit.path),
                           folder: URL(fileURLWithPath: hit.path).deletingLastPathComponent().path,
                           size: hit.size, date: hit.date, dateText: hit.dateText, kind: why, isDir: false, tip: tip,
                           tags: id == "name" ? FileItem(url: URL(fileURLWithPath: hit.path)).tagColors : [], in: tableView)
        case .pick(let p):
            let symbol = p.isGroup ? "rectangle.3.group" : "square.stack.3d.up"
            let icon = NSImage(systemSymbolName: symbol, accessibilityDescription: p.name) ?? NSImage()
            let kind = p.isGroup ? "Group · \(p.files) folders" : "Indexed folder · \(counts.string(from: NSNumber(value: p.files)) ?? "\(p.files)") files"
            return cellFor(id: id, name: p.name, match: [], detail: nil, icon: icon, folder: p.path.map { ($0 as NSString).abbreviatingWithTildeInPath } ?? "",
                           size: nil, date: nil, kind: kind, isDir: true, in: tableView)
        }
    }

    /// The cell for one column of a row: the name column carries the icon and title, the others one text line.
    private func cellFor(id: String, name: String, match: [(Int, Int)], detail: NSAttributedString?, icon: NSImage,
                         folder: String, size: Int64? = nil, date: Date?, dateText: String? = nil, kind: String, isDir: Bool,
                         tip: String? = nil, tags: [TagColor] = [], in tableView: NSTableView) -> NSView {
        if id == "name" {
            let cell = (tableView.makeView(withIdentifier: NSUserInterfaceItemIdentifier("name"), owner: self) as? SearchCell)
                ?? SearchCell(identifier: "name")
            icon.size = NSSize(width: 16, height: 16)
            cell.imageView?.image = icon
            cell.title.attributedStringValue = Self.highlighted(name, match)
            cell.title.font = .systemFont(ofSize: 13)
            if let detail {
                cell.detail.attributedStringValue = detail
                cell.detail.isHidden = false
            } else {
                cell.detail.stringValue = ""
                cell.detail.isHidden = true
            }
            let dots = TagStore.dotsImage(for: tags)                // nil clears the dots on a reused cell
            cell.tagDots.image = dots
            cell.tagDotsWidth.constant = dots?.size.width ?? 0
            cell.toolTip = tip
            return cell
        }
        let text: String
        let rightAligned = id == "size"
        switch id {
        case "folder": text = (folder as NSString).abbreviatingWithTildeInPath
        case "size": text = size.map { bytes.string(fromByteCount: $0) } ?? ""
        case "date": text = dateText ?? date.map { dates.string(from: $0) } ?? ""
        case "kind": text = kind
        default: text = ""
        }
        let cell = (tableView.makeView(withIdentifier: NSUserInterfaceItemIdentifier("text." + id), owner: self) as? NSTableCellView)
            ?? Self.textCell(id: "text." + id)
        cell.textField?.stringValue = text
        cell.textField?.alignment = rightAligned ? .right : .left
        cell.textField?.lineBreakMode = id == "folder" ? .byTruncatingMiddle : .byTruncatingTail
        cell.textField?.textColor = id == "folder" ? .secondaryLabelColor : nil
        cell.toolTip = tip
        return cell
    }

    /// The Content matches header row.
    private func contentHeaderCell() -> NSView {
        let cell = SectionHeaderCell()
        let (title, tail) = contentHeaderParts()
        cell.title.stringValue = title.uppercased()
        cell.tail.stringValue = tail
        let collapsed = contentsCollapsible && !contentsExpanded
        if contentsCollapsible || contentState == .failed {
            let name = contentState == .failed ? "arrow.clockwise" : (collapsed ? "chevron.right" : "chevron.down")
            cell.chevron.image = NSImage(systemSymbolName: name, accessibilityDescription: collapsed ? "Collapsed" : "Expanded")
        }
        cell.toolTip = contentsCollapsible
            ? "Click, or press Return, to show or hide the content matches (\u{2318}2 jumps here). Hidden, they are not searched."
            : nil
        return cell
    }

    private static func textCell(id: String) -> NSTableCellView {
        let c = NSTableCellView()
        c.identifier = NSUserInterfaceItemIdentifier(id)
        let t = NSTextField(labelWithString: "")
        t.font = .systemFont(ofSize: 12)
        t.translatesAutoresizingMaskIntoConstraints = false
        c.addSubview(t)
        c.textField = t
        NSLayoutConstraint.activate([
            t.leadingAnchor.constraint(equalTo: c.leadingAnchor, constant: 4),
            t.trailingAnchor.constraint(equalTo: c.trailingAnchor, constant: -4),
            t.centerYAnchor.constraint(equalTo: c.centerYAnchor),
        ])
        return c
    }

    /// The name with the matched character ranges in bold.
    static func highlighted(_ name: String, _ ranges: [(Int, Int)]) -> NSAttributedString {
        let out = NSMutableAttributedString(string: name, attributes: [.font: NSFont.systemFont(ofSize: 13)])
        let chars = Array(name)
        for (s, e) in ranges {
            let start = max(0, min(s, chars.count)), end = max(start, min(e, chars.count))
            guard end > start else { continue }
            let loc = String(chars[0..<start]).utf16.count
            let len = String(chars[start..<end]).utf16.count
            out.addAttribute(.font, value: NSFont.systemFont(ofSize: 13, weight: .bold), range: NSRange(location: loc, length: len))
        }
        return out
    }

    static func kindName(_ kind: String) -> String {
        guard let first = kind.first else { return "" }
        return first.uppercased() + kind.dropFirst()
    }

    // MARK: drag (copies only)

    func tableView(_ tableView: NSTableView, pasteboardWriterForRow row: Int) -> NSPasteboardWriting? {
        url(forRow: row).map { $0 as NSURL }
    }

    // MARK: keys and actions

    private func handleKey(_ e: NSEvent) -> Bool {
        let cmd = e.modifierFlags.contains(.command)
        let opt = e.modifierFlags.contains(.option)
        let sel = table.selectedRow
        let onHeader: Bool = { if sel >= 0, sel < rows.count, case .contentHeader = rows[sel] { return true }; return false }()
        switch e.keyCode {
        case 36, 76:                                   // Return, Enter
            if onHeader && !cmd { activateContentHeader(); return true }
            if cmd { revealSelected() } else { openSelected() }
            return true
        case 49 where !cmd:                            // Space
            if onHeader { activateContentHeader(); return true }
            toggleQuickLook()
            return true
        case 124 where onHeader && !cmd && !opt:       // → opens the section
            if contentsCollapsible && !contentsExpanded { setContentsExpanded(true) }
            return true
        case 123 where onHeader && !cmd && !opt:       // ← closes it
            if contentsCollapsible && contentsExpanded { setContentsExpanded(false) }
            return true
        case 125 where onHeader && !cmd && !opt:       // ↓ on a closed header loads and opens it
            if contentsCollapsible && !contentsExpanded { setContentsExpanded(true); return true }
            return false
        case 53:                                       // Esc
            onEscape?()
            return true
        default:
            break
        }
        if cmd, e.charactersIgnoringModifiers == "o" { openFileAction(); return true }
        if cmd, e.charactersIgnoringModifiers == "c" {
            if opt { copyPaths() } else { copyFiles() }
            return true
        }
        return false
    }

    private func selectedRowsInOrder() -> [Int] { table.selectedRowIndexes.filter { $0 < rows.count }.sorted() }

    private func url(forRow row: Int) -> URL? {
        guard row >= 0, row < rows.count else { return nil }
        switch rows[row] {
        case .header, .contentHeader: return nil
        case .file(let i): return i.url
        case .content(let c): return c.url
        case .pick(let p): return p.path.map { URL(fileURLWithPath: $0) }
        }
    }

    /// The files selected in the results (Get Info, Quick Look).
    var selectedFileURLs: [URL] { selectedURLs() }

    private func selectedURLs() -> [URL] { selectedRowsInOrder().compactMap { url(forRow: $0) } }

    @objc private func doubleClicked(_ sender: Any?) { openSelected() }

    /// Return: a picker row picks its folder or group; a folder opens in Mac mode; a file opens in its app.
    private func openSelected() {
        let rows = selectedRowsInOrder()
        if recordsRecents, rows.contains(where: { r in
            switch self.rows[r] { case .file, .content: return true; default: return false }
        }) { RecentSearches.record(query) }
        for row in rows { open(row: row) }
    }

    private func open(row: Int) {
        guard row < rows.count else { return }
        switch rows[row] {
        case .header, .contentHeader: return
        case .pick(let p): onPick?(p.members)
        case .file(let i):
            if i.isDir { onOpenFolder?(i.url) } else { NSWorkspace.shared.open(i.url) }
        case .content(let c): openPreview(c)
        }
    }

    /// A content match opens in the web UI's Search view, so the rich preview (spreadsheet tables, mail headers, page
    /// jumps, highlighted passages) and its ★ favourite, Not relevant, Find similar and Copy for AI are one Return away.
    private func openPreview(_ c: ContentHit) {
        guard let coll = c.collection, let rel = c.relPath else { NSWorkspace.shared.open(c.url); return }
        onOpenPreview?(query, indexedNames, coll, rel)
    }

    /// The indexed folder a file lies in and its path there, from the library (name rows carry `collection`).
    private func previewTarget(forRow row: Int) -> (String, String)? {
        guard row >= 0, row < rows.count else { return nil }
        switch rows[row] {
        case .content(let c):
            guard let coll = c.collection, let rel = c.relPath else { return nil }
            return (coll, rel)
        case .file(let i):
            guard !i.isDir, let coll = i.collection,
                  let root = library?.collections.first(where: { $0.name == coll })?.path else { return nil }
            let r = root.hasSuffix("/") ? root : root + "/"
            return i.path.hasPrefix(r) ? (coll, String(i.path.dropFirst(r.count))) : nil
        default: return nil
        }
    }

    private func revealSelected() {
        guard let row = selectedRowsInOrder().first, let u = url(forRow: row) else { return }
        onRevealFolder?(u)
    }

    private func copyFiles() {
        let urls = selectedURLs()
        guard !urls.isEmpty else { return }
        let pb = NSPasteboard.general
        pb.clearContents()
        pb.writeObjects(urls.map { $0 as NSURL })
    }

    private func copyPaths() {
        let paths = selectedURLs().map { $0.path }
        guard !paths.isEmpty else { return }
        let pb = NSPasteboard.general
        pb.clearContents()
        pb.setString(paths.joined(separator: "\n"), forType: .string)
    }

    private func toggleQuickLook() {
        guard let panel = QLPreviewPanel.shared() else { return }
        if QLPreviewPanel.sharedPreviewPanelExists() && panel.isVisible {
            panel.orderOut(nil)
            return
        }
        previewURLs = selectedURLs()
        guard !previewURLs.isEmpty else { return }
        panel.dataSource = self
        panel.reloadData()
        panel.makeKeyAndOrderFront(nil)
    }

    func numberOfPreviewItems(in panel: QLPreviewPanel!) -> Int { previewURLs.count }

    func previewPanel(_ panel: QLPreviewPanel!, previewItemAt index: Int) -> QLPreviewItem! {
        previewURLs[index] as NSURL
    }

    // MARK: context menu

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        let row = table.clickedRow
        guard row >= 0, row < rows.count, let target = url(forRow: row) else { return }
        if !table.selectedRowIndexes.contains(row) {
            table.selectRowIndexes(IndexSet(integer: row), byExtendingSelection: false)
        }
        let isPick: Bool = { if case .pick = rows[row] { return true }; return false }()
        func add(_ title: String, _ action: Selector) {
            let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
            item.target = self
            menu.addItem(item)
        }
        if isPick {
            add("Open", #selector(openAction))
            return
        }
        if previewTarget(forRow: row) != nil { add("Open in MySearch Preview (★ Favourite…)", #selector(previewAction)) }
        add(isContentRow(row) ? "Open File" : "Open", #selector(openFileAction))
        let openWith = NSMenuItem(title: "Open With", action: nil, keyEquivalent: "")
        let sub = NSMenu(title: "Open With")
        for app in NSWorkspace.shared.urlsForApplications(toOpen: target).prefix(15) {
            let item = NSMenuItem(title: FileManager.default.displayName(atPath: app.path), action: #selector(openWithAction(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = [target, app] as [Any]
            sub.addItem(item)
        }
        openWith.submenu = sub
        menu.addItem(openWith)
        add("Quick Look", #selector(quickLookAction))
        add("Show in Enclosing Folder", #selector(revealAction))
        menu.addItem(.separator())
        add("Copy Path", #selector(copyPathAction))
        add("Search only here", #selector(searchHereAction))
        let launchers = Launchers.menuItems(for: selectedURLs())
        if !launchers.isEmpty {
            menu.addItem(.separator())
            menu.items += launchers
        }
    }

    @objc private func openAction() { openSelected() }

    private func isContentRow(_ row: Int) -> Bool { if case .content = rows[row] { return true }; return false }

    /// Open File: the file in its own app (a content row's Return opens the preview instead).
    @objc private func openFileAction() {
        for row in selectedRowsInOrder() {
            guard row < rows.count else { continue }
            switch rows[row] {
            case .content(let c): NSWorkspace.shared.open(c.url)
            default: open(row: row)
            }
        }
    }

    @objc private func previewAction() {
        guard let row = selectedRowsInOrder().first, let (coll, rel) = previewTarget(forRow: row) else { return }
        onOpenPreview?(query, indexedNames, coll, rel)
    }

    @objc private func openWithAction(_ sender: NSMenuItem) {
        guard let pair = sender.representedObject as? [Any], pair.count == 2,
              let file = pair[0] as? URL, let app = pair[1] as? URL else { return }
        NSWorkspace.shared.open([file], withApplicationAt: app, configuration: NSWorkspace.OpenConfiguration())
    }

    @objc private func quickLookAction() { toggleQuickLook() }

    @objc private func revealAction() { revealSelected() }

    @objc private func copyPathAction() { copyPaths() }

    /// Narrows the search to the folder of the row (a folder row's own folder, a file's enclosing folder).
    @objc private func searchHereAction() {
        guard let row = selectedRowsInOrder().first, let u = url(forRow: row) else { return }
        let isDir: Bool = { if case .file(let i) = rows[row] { return i.isDir }; return false }()
        let folder = isDir ? u : u.deletingLastPathComponent()
        let path = (folder.path as NSString).abbreviatingWithTildeInPath
        let rest = query.replacingOccurrences(of: #"in:\S+"#, with: "", options: .regularExpression)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        onRequestQuery?(("in:\(path) " + rest).trimmingCharacters(in: .whitespaces))
    }
}
