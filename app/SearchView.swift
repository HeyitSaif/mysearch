// The native search results view, Everything-style: a name search over the whole Mac (scope=mac) or over the
// Indexed folders and groups the user picked in the sidebar (scope=indexed). Results arrive as the user types
// (the first keystroke searches at once, the next ones are debounced 40 ms, a re: query waits 250 ms; every older request
// is cancelled). The names come in two pages (50, then up to 150 more). Indexed mode adds a second section, "Content
// matches" (meaning and keywords from /api/search), which loads after the names and never blocks typing.
//
// The main window owns the toolbar search field and calls run(_:) with its text; this controller owns the table,
// the scope bar, the banner and the keys. Endpoints: GET /api/files/search, /api/files/status, /api/search (see
// MSEClient.get in API.swift). A 404 from /api/files/* means the whole-Mac index is not installed: the view then
// shows a banner and the hybrid search over the indexed folders instead.

import Cocoa
import Quartz
import SwiftUI

// MARK: - Design tokens

/// The values the native views share, defined once. AskColors (AskModel.swift) and SidebarColors (Sidebar.swift) stay what
/// they are: palettes of one view each. Later steps refine these values; use the token, not a literal.
enum MSEStyle {
    /// A colour of two fixed values, one for light and one for dark mode, chosen each time it is drawn. It is built from
    /// opaque values on purpose: a colour made with withAlphaComponent is fixed in the appearance that was current when it
    /// was made, so a token built that way would stay light or dark. bestMatch (not a name check) also sends the vibrant and
    /// high-contrast variants to the right side.
    private static func dynamic(light: UInt32, dark: UInt32) -> NSColor {
        NSColor(name: nil) { appearance in
            let rgb = appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua ? dark : light
            return NSColor(srgbRed: CGFloat((rgb >> 16) & 0xFF) / 255, green: CGFloat((rgb >> 8) & 0xFF) / 255,
                           blue: CGFloat(rgb & 0xFF) / 255, alpha: 1)
        }
    }
    /// The one secondary text colour (captions, counts, hints, snippets, the Folder column, section headers). At least 4.5:1
    /// on the white row (#FFFFFF / #1E1E1E), the window and footer, the sidebar (#E4E4E4 / #3A3A3A) and a selected row of an
    /// inactive window (#DCDCDC / #464646), light and dark. The system secondary label colour is about 3.7 to 3.95:1 in light.
    static let secondary: NSColor = dynamic(light: 0x595959, dark: 0xB8B8B8)
    /// The text of the document badge, on the quiet pill it sits on (its fill is darker than the row in light, lighter in
    /// dark, so the badge needs a stronger value than MSEStyle.secondary to keep 4.5:1, selected row of an inactive window included).
    static let badgeText: NSColor = dynamic(light: 0x484848, dark: 0xE0E0E0)
    /// Secondary text in a cell, for the cell's background style: a selected row (the accent colour, .emphasized) takes white,
    /// because only the system label colours are swapped for white there by AppKit; a custom colour is not.
    static func secondary(on style: NSView.BackgroundStyle) -> NSColor {
        style == .emphasized ? .alternateSelectedControlTextColor : secondary
    }
    /// The header of a section ("File names", "Content matches").
    static let sectionHeaderFont: NSFont = .systemFont(ofSize: 12, weight: .semibold)
    /// The ONE SF Symbol configuration: every symbol in a view is made with it.
    static let symbolConfig: NSImage.SymbolConfiguration = .init(pointSize: 13, weight: .regular)
    /// The side inset of a content view.
    static let contentInset: CGFloat = 24
    /// The width of a line of the Ask answer, in characters.
    static let askMeasure: Int = 70
    /// The height of a result row.
    static let rowHeight: CGFloat = 60
    /// The side of a file icon in a result row.
    static let iconSize: CGFloat = 32
}

enum SearchScope { case mac, indexed }

// BEGIN SearchDebounce (Foundation only: scratch checks compile this block on its own)
/// When the request for a keystroke goes out. Leading edge: a keystroke that comes after a quiet spell (at least `quiet`
/// since the call before it) searches at once; a keystroke inside a burst waits `quiet`, so a burst sends its first
/// keystroke and then one request after its last. A `re:` query is trailing only and waits `regexQuiet`: a regular
/// expression that is half typed can be invalid, so it is never sent on the leading edge. Older requests are cancelled
/// by the caller, as before; this only answers "how long to wait".
struct SearchDebounce {
    static let quiet: UInt64 = 40_000_000           // ns
    static let regexQuiet: UInt64 = 250_000_000     // ns
    private var lastCall: UInt64?

    /// A word of the text that starts with re: (after an optional ! or -), in any case.
    static func isRegexQuery(_ text: String) -> Bool {
        text.split(whereSeparator: { $0.isWhitespace }).contains { token in
            var t = Substring(token)
            if let f = t.first, f == "!" || f == "-" { t = t.dropFirst() }
            return t.lowercased().hasPrefix("re:")
        }
    }

    /// The wait before the request for `text`, in ns; nil = send it at once. `now` is a monotonic clock in ns. Every call
    /// counts as a keystroke, so call it once per run of the search.
    mutating func delay(for text: String, now: UInt64) -> UInt64? {
        defer { lastCall = now }
        if SearchDebounce.isRegexQuery(text) { return SearchDebounce.regexQuiet }
        if let last = lastCall, now >= last, now - last < SearchDebounce.quiet { return SearchDebounce.quiet }
        return nil
    }
}
// END SearchDebounce

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

/// What a document is, as /api/search says it (the `meta` of a result, or nil): the type, its date, amount and currency,
/// the counterparty and the reference (mse/docmeta.py).
struct DocMeta {
    let doctype: DocType?
    let docDate: String?          // "2025-03-14", "2025-03" or "2025"
    let amount: Double?
    let currency: String?
    let counterparty: String?
    let reference: String?

    init?(_ d: Any?) {
        guard let obj = d as? [String: Any] else { return nil }
        let type = (obj["doctype"] as? String).flatMap { DocType(rawValue: $0) }
        let raw = obj["doctype"] as? String
        // a result with a document type that this build does not know is still shown, without a badge name it lacks
        guard type != nil || raw != nil else { return nil }
        doctype = type
        docDate = obj["doc_date"] as? String
        amount = (obj["amount"] as? NSNumber)?.doubleValue
        currency = (obj["currency"] as? String).flatMap { $0.isEmpty ? nil : $0 }
        counterparty = (obj["counterparty"] as? String).flatMap { $0.isEmpty ? nil : $0 }
        reference = (obj["reference"] as? String).flatMap { $0.isEmpty ? nil : $0 }
    }

    /// The amount with its currency, locale-formatted ("€1,234.50"); the plain number when the currency is unknown.
    var amountText: String? {
        guard let amount else { return nil }
        guard let currency else { return DocMeta.number.string(from: NSNumber(value: amount)) }
        let f = NumberFormatter()
        f.numberStyle = .currency
        f.currencyCode = currency
        f.locale = Locale.current
        return f.string(from: NSNumber(value: amount)) ?? "\(amount) \(currency)"
    }

    private static let number: NumberFormatter = { let f = NumberFormatter(); f.numberStyle = .decimal; return f }()
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
    let meta: DocMeta?               // what the document is, when it is classified (nil for a note or an unclassified file)

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
                          relevance: (d["relevance"] as? NSNumber)?.intValue,
                          meta: DocMeta(d["meta"]))
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
    /// The snippet is one line, 12 pt. The paragraph style carries the line break: a cell given an attributed string
    /// takes its truncation from the string, so without it a long snippet is clipped at the column edge, mid-word,
    /// with no ellipsis. The colour is left to the field (MSEStyle.secondary), so a selected row can recolour it.
    private static var oneLine: NSParagraphStyle {
        let p = NSMutableParagraphStyle()
        p.lineBreakMode = .byTruncatingTail
        return p
    }
    private static var base: [NSAttributedString.Key: Any] {
        [.font: NSFont.systemFont(ofSize: 12), .paragraphStyle: oneLine]
    }
    private static var marked: [NSAttributedString.Key: Any] {
        [.font: NSFont.systemFont(ofSize: 12, weight: .semibold), .paragraphStyle: oneLine,
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

    /// `s` cut to fit `width` points on one line, ending in a plain "…". AppKit's own tail truncation gives the ellipsis the
    /// attributes of the character where the cut falls, so a cut inside a highlighted word left a yellow, semibold "…".
    /// Here the visible text keeps its runs (the highlight stays on the words that can be seen) and the ellipsis is plain.
    /// A string that fits is returned as it is.
    static func fitted(_ s: NSAttributedString, width: CGFloat) -> NSAttributedString {
        guard s.length > 0, s.size().width > width else { return s }
        let ellipsis = NSAttributedString(string: "\u{2026}", attributes: base)
        func candidate(_ end: Int) -> NSAttributedString {
            guard end > 0 else { return ellipsis }
            let cut = min((s.string as NSString).rangeOfComposedCharacterSequence(at: end - 1).upperBound, s.length)
            var head = s.attributedSubstring(from: NSRange(location: 0, length: cut))
            // no space or newline before the ellipsis
            var trimmed = head.length
            let ns = head.string as NSString
            while trimmed > 0, let u = Unicode.Scalar(ns.character(at: trimmed - 1)), CharacterSet.whitespacesAndNewlines.contains(u) { trimmed -= 1 }
            if trimmed != head.length { head = head.attributedSubstring(from: NSRange(location: 0, length: trimmed)) }
            let out = NSMutableAttributedString(attributedString: head)
            out.append(ellipsis)
            return out
        }
        var lo = 0, hi = s.length                       // the longest prefix (in UTF-16 units) whose cut string fits
        while lo < hi {
            let mid = (lo + hi + 1) / 2
            if candidate(mid).size().width <= width { lo = mid } else { hi = mid - 1 }
        }
        return candidate(lo)
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

/// The document type of a result as a small neutral pill: the label on a quiet fill, readable in light and dark mode.
final class DocBadge: NSView {
    let label = NSTextField(labelWithString: "")

    init() {
        super.init(frame: .zero)
        wantsLayer = true
        layer?.cornerRadius = 4
        label.font = .systemFont(ofSize: 10, weight: .medium)
        label.textColor = MSEStyle.badgeText
        label.translatesAutoresizingMaskIntoConstraints = false
        addSubview(label)
        NSLayoutConstraint.activate([
            label.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 5),
            label.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -5),
            label.topAnchor.constraint(equalTo: topAnchor, constant: 1),
            label.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -1),
        ])
        setContentHuggingPriority(.required, for: .horizontal)
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override var wantsUpdateLayer: Bool { true }

    override func updateLayer() {
        layer?.backgroundColor = NSColor.secondaryLabelColor.withAlphaComponent(0.14).cgColor
    }

    /// On a selected row (the accent colour) the label is white; otherwise the badge's own colour.
    func setEmphasized(_ on: Bool) {
        label.textColor = on ? .alternateSelectedControlTextColor : MSEStyle.badgeText
    }
}

/// A name cell: icon and title; a content match adds the snippet and then the document line (NetNewsWire's order:
/// title, one line of text, then the facts). A file-name row stays one compact line with a 16 pt icon.
final class SearchCell: NSTableCellView {
    let title = NSTextField(labelWithString: "")
    let detail = NSTextField(labelWithString: "")
    let tagDots = NSImageView()           // the file's colour tags, after the name (TagStore.dotsImage)
    private(set) var tagDotsWidth: NSLayoutConstraint!      // the dots' width: 0 when there are none
    /// The path whose colour tags this cell shows or waits for. Cells are reused, so a tag answer that comes late for
    /// another path is dropped (SearchViewController.showTags).
    var tagPath: String?
    /// The document line of a classified result: type badge, then amount · date · counterparty. Hidden when there is none.
    let badge = DocBadge()
    let metaText = NSTextField(labelWithString: "")
    private(set) var metaRow = NSStackView()
    private var iconSide: [NSLayoutConstraint] = []         // the icon's width and height: 16, or MSEStyle.iconSize in a rich row
    private var textGap: NSLayoutConstraint!                // icon to text
    /// The title, snippet and document line stacked; its width is the room the snippet has.
    private var textStack: NSStackView?
    /// The whole snippet. What the snippet field shows is this cut to the room (layout), with a plain ellipsis.
    private var fullSnippet: NSAttributedString?
    /// What the snippet field was last given (compared with the next fit, so an unchanged fit assigns nothing and the
    /// layout settles; the field's own attributedStringValue is not equal to what it was given).
    private var shownSnippet: NSAttributedString?

    init(identifier: String) {
        super.init(frame: .zero)
        self.identifier = NSUserInterfaceItemIdentifier(identifier)
        let icon = NSImageView()
        icon.translatesAutoresizingMaskIntoConstraints = false
        icon.imageScaling = .scaleProportionallyDown
        title.lineBreakMode = .byTruncatingTail
        detail.lineBreakMode = .byTruncatingTail
        detail.maximumNumberOfLines = 1           // a snippet is one line: a longer one ends in an ellipsis, never wraps
        detail.cell?.usesSingleLineMode = true
        detail.cell?.truncatesLastVisibleLine = true
        detail.font = .systemFont(ofSize: 12)
        detail.textColor = MSEStyle.secondary
        metaText.font = .systemFont(ofSize: 11)
        metaText.textColor = MSEStyle.secondary
        metaText.lineBreakMode = .byTruncatingTail
        metaText.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        // The snippet must give way to the column edge, as the title and the document line do: at the default
        // resistance it kept its full width and the cell clipped it mid-word.
        detail.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        metaRow = NSStackView(views: [badge, metaText])
        metaRow.orientation = .horizontal
        metaRow.alignment = .centerY
        metaRow.spacing = 6
        metaRow.isHidden = true
        let text = NSStackView(views: [title, detail, metaRow])
        text.orientation = .vertical
        text.alignment = .leading
        text.spacing = 2
        text.translatesAutoresizingMaskIntoConstraints = false
        textStack = text
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
        iconSide = [icon.widthAnchor.constraint(equalToConstant: 16), icon.heightAnchor.constraint(equalToConstant: 16)]
        textGap = text.leadingAnchor.constraint(equalTo: icon.trailingAnchor, constant: 6)
        NSLayoutConstraint.activate(iconSide + [
            icon.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 4),
            icon.centerYAnchor.constraint(equalTo: centerYAnchor),
            textGap,
            text.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -4),
            text.centerYAnchor.constraint(equalTo: centerYAnchor),
            // every line stops at the text column's edge, so a long snippet or document line truncates there
            detail.trailingAnchor.constraint(lessThanOrEqualTo: text.trailingAnchor),
            metaRow.trailingAnchor.constraint(lessThanOrEqualTo: text.trailingAnchor),
            tagDots.leadingAnchor.constraint(equalTo: title.trailingAnchor, constant: 6),
            tagDots.centerYAnchor.constraint(equalTo: title.centerYAnchor),
            tagDots.trailingAnchor.constraint(lessThanOrEqualTo: trailingAnchor, constant: -4),
            tagDotsWidth,
        ])
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    /// The snippet of a content match; nil hides the line (and clears a reused cell). The field shows the snippet cut to
    /// the room: AppKit's own tail truncation puts the highlight and the semibold weight of a cut word on the ellipsis.
    func setSnippet(_ snippet: NSAttributedString?) {
        fullSnippet = snippet
        shownSnippet = snippet
        if let snippet {
            detail.attributedStringValue = snippet
            detail.isHidden = false
        } else {
            detail.stringValue = ""
            detail.isHidden = true
        }
        needsLayout = true
    }

    override func layout() {
        super.layout()
        guard let full = fullSnippet, !detail.isHidden, let stack = textStack else { return }
        // Measured against the text column, not the snippet field: the field is as wide as the string it holds, so its own
        // width would only ever shrink. The column's edge less the field's 2 pt padding on each side and a little to spare.
        let room = stack.frame.width - 8
        guard room > 40 else { return }
        let fitted = SnippetText.fitted(full, width: room)
        guard shownSnippet.map({ !fitted.isEqual(to: $0) }) ?? true else { return }
        shownSnippet = fitted
        detail.attributedStringValue = fitted
        recolor()                                       // a new string must not lose the row's text colour (a selected row's)
    }

    /// The snippet, the document line and the badge are secondary text: a selected row (the accent colour) takes white,
    /// every other row the token. (A custom colour is not swapped for white by AppKit, as the system label colours are.)
    override var backgroundStyle: NSView.BackgroundStyle {
        didSet { recolor() }
    }

    private func recolor() {
        let colour = MSEStyle.secondary(on: backgroundStyle)
        detail.textColor = colour
        metaText.textColor = colour
        badge.setEmphasized(backgroundStyle == .emphasized)
    }

    /// The colour dots after the name; an empty list clears the dots of a reused cell.
    func setTags(_ colors: [TagColor]) {
        let dots = TagStore.dotsImage(for: colors)
        tagDots.image = dots
        tagDotsWidth.constant = dots?.size.width ?? 0
    }

    /// A rich row (a content match: title, snippet, document line) has the large icon, MSEStyle.iconSize, in a row of
    /// MSEStyle.rowHeight; a file-name row keeps the compact 16 pt icon.
    func setRich(_ rich: Bool) {
        let side: CGFloat = rich ? MSEStyle.iconSize : 16
        for c in iconSide { c.constant = side }
        textGap.constant = rich ? 10 : 6
    }

    /// The document line: the type badge and the text (amount · date · counterparty); nil hides the line.
    func setMeta(_ meta: DocMeta?, line: String?) {
        guard let meta else {
            metaRow.isHidden = true
            return
        }
        badge.label.stringValue = meta.doctype?.title ?? "Document"
        metaText.stringValue = line ?? ""
        metaRow.isHidden = false
    }
}

/// One button of an empty state: what it says and what it does.
struct EmptyStateAction: Identifiable {
    let id: Int
    let title: String
    let run: () -> Void
}

/// The SwiftUI side of the one shared empty state: an icon, a title, one line, and the buttons that get out of it.
/// ContentUnavailableView is the system view for exactly this (macOS 14 and later, the app's floor). The icon is
/// drawn with MSEStyle.symbolConfig and the colour is MSEStyle.secondary; the side inset is MSEStyle.contentInset.
private struct EmptyStateContent: View {
    var symbol = "magnifyingglass"
    var title = ""
    var detail: String?
    var actions: [EmptyStateAction] = []

    var body: some View {
        ContentUnavailableView {
            Label {
                Text(title)
            } icon: {
                AskSymbol(name: symbol).foregroundStyle(Color(nsColor: MSEStyle.secondary))
            }
        } description: {
            if let detail { Text(detail).foregroundStyle(Color(nsColor: MSEStyle.secondary)) }
        } actions: {
            ForEach(actions) { action in
                Button(action.title) { action.run() }.buttonStyle(.bordered)
            }
        }
        .padding(.horizontal, MSEStyle.contentInset)
    }
}

/// The one empty state of the native views, for AppKit code: put it over the area that would be empty, pinned to its
/// four edges (it centres its own content), and call show(...) with an icon, a short title, one line and the buttons.
/// Hidden until shown; set isHidden = true to take it away. Used by the search results (SearchView) for "No matches",
/// "Name index not ready" and the search errors.
@MainActor
final class EmptyStateView: NSView {
    private let host = NSHostingView(rootView: EmptyStateContent())

    override init(frame: NSRect) {
        super.init(frame: frame)
        // The view is pinned to its parent's edges, so the SwiftUI content must not ask the window for a size.
        host.sizingOptions = []
        host.translatesAutoresizingMaskIntoConstraints = false
        addSubview(host)
        NSLayoutConstraint.activate([
            host.topAnchor.constraint(equalTo: topAnchor),
            host.bottomAnchor.constraint(equalTo: bottomAnchor),
            host.leadingAnchor.constraint(equalTo: leadingAnchor),
            host.trailingAnchor.constraint(equalTo: trailingAnchor),
        ])
        isHidden = true
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    /// Shows the state. `symbol` is an SF Symbol name; `detail` is one line (nil for none); each action is a button.
    func show(symbol: String, title: String, detail: String?, actions list: [(String, () -> Void)]) {
        host.rootView = EmptyStateContent(
            symbol: symbol, title: title, detail: detail,
            actions: list.enumerated().map { EmptyStateAction(id: $0.offset, title: $0.element.0, run: $0.element.1) })
        isHidden = false
    }
}

/// A text column cell of the results table (Folder, Size, Date, Match). The Folder column is secondary text: the token,
/// and white on a selected row (a custom colour is not swapped for white by AppKit, as the system label colours are).
@MainActor
private final class TextColumnCell: NSTableCellView {
    var isSecondary = false { didSet { recolor() } }

    override var backgroundStyle: NSView.BackgroundStyle {
        didSet { recolor() }
    }

    private func recolor() {
        textField?.textColor = isSecondary ? MSEStyle.secondary(on: backgroundStyle) : nil
    }
}

/// The header row of the Content matches section: a chevron (when the section can be closed) and one label in the
/// same style as the File names header ("Content matches · 4", plus what state the section is in). A click on the
/// row, Return, or the arrow keys open and close it. No band: only a selected header is highlighted.
@MainActor
private final class SectionHeaderCell: NSTableCellView {
    let title = NSTextField(labelWithString: "")
    let chevron = NSImageView()

    override var backgroundStyle: NSView.BackgroundStyle {
        didSet { recolor() }
    }

    init() {
        super.init(frame: .zero)
        title.font = MSEStyle.sectionHeaderFont
        title.lineBreakMode = .byTruncatingTail
        chevron.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 9, weight: .bold)
        for v in [chevron, title] as [NSView] {
            v.translatesAutoresizingMaskIntoConstraints = false
            addSubview(v)
        }
        textField = title
        NSLayoutConstraint.activate([
            chevron.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 4),
            chevron.centerYAnchor.constraint(equalTo: centerYAnchor),
            chevron.widthAnchor.constraint(equalToConstant: 10),
            title.leadingAnchor.constraint(equalTo: chevron.trailingAnchor, constant: 6),
            title.trailingAnchor.constraint(lessThanOrEqualTo: trailingAnchor, constant: -4),
            title.centerYAnchor.constraint(equalTo: centerYAnchor),
        ])
        title.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        recolor()
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    private func recolor() {
        let emphasized = backgroundStyle == .emphasized
        title.textColor = emphasized ? .alternateSelectedControlTextColor : MSEStyle.secondary
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
        /// Mac mode's On this Mac section: its header, one whole-disk hit, and the "Show more" row after the last one listed.
        case diskHeader
        case disk(DiskHit)
        case diskMore
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
    private let emptyState = EmptyStateView()
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
    /// The second page of names is on its way (the first is on screen).
    private var moreLoading = false
    /// A name was dropped by the globs the operators cannot say, so the total from the service no longer counts what is listed.
    private var namesFilteredOut = false
    private var debounce = SearchDebounce()
    /// The first page of names, and the most names one search lists (the first page plus one more request).
    private static let firstPageSize = 50
    private static let namesCap = 200
    /// Colour tags read so far, by path. Cleared with every new search; it keeps the dots from blinking when the table reloads.
    private var tagCache: [String: [TagColor]] = [:]
    /// Tag reads that are still running off the main thread (the snapshot waits on them, see isLoading).
    private var tagLoadsPending = 0
    private var contentState: ContentState = .idle
    private var contentLoadedGen = -1                // the generation the content matches on screen belong to
    /// The names the service counted for this search (the total, not only the page on screen). nil = not asked or unknown.
    private var nameTotal: Int?
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
    private var errorTitle = ""          // the short title that goes with errorText in the empty state
    private var errorRetry = false

    private var nameTask: Task<Void, Never>?
    private var contentTask: Task<Void, Never>?
    private var statusTask: Task<Void, Never>?
    private var recentTask: Task<Void, Never>?
    private var aliasTask: Task<Void, Never>?
    private var diskTask: Task<Void, Never>?
    /// The On this Mac section (Mac mode): the whole-disk hits of the query and their paging. diskUnavailable is set when the
    /// service has no whole-disk search; the section is then not shown, and no error is.
    private var diskHits: [DiskHit] = []
    private var diskState: ContentState = .idle
    private var diskNext: Int?
    private var diskHasMore = false
    private var diskMoreLoading = false
    private var diskUnavailable = false
    private(set) var diskExpanded = true
    private var diskObserver: NSObjectProtocol?
    /// The hits per page of the On this Mac section ("Show more" loads the next page).
    static let diskPageSize = 5
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
    /// The Timeline view of the Indexed scope (TimelineView.swift), shown in place of the results with the Timeline switch.
    private let timeline = TimelineView()
    private(set) var showingTimeline = false
    /// Snapshot hook: the timeline is shown as soon as the next search text lands (it comes in asynchronously).
    private var showTimelineOnRun = false
    private let facetRow = FacetRow()
    private let alsoLabel = NSTextField(labelWithString: "")
    /// The facets and bridge terms of the last content search, and whether the re-ranked list is on screen.
    private var facets: SearchFacets?
    private var bridgeTerms: [String] = []
    private var reranked = false
    /// The precise (re-ranked) answer is on its way: the web's second request after the instant results.
    private var refining = false
    private var dateSheet: DateRangeSheet?

    override func viewDidLayout() {
        super.viewDidLayout()
        fitColumns()
    }

    /// Each column's preferred width: the width it takes when there is room. fitColumns starts every layout pass from these,
    /// so a window that was narrowed and is then widened gives the columns their widths back. A column the user drags to a
    /// new width takes that width as its preference (columnResized).
    private var preferredWidths: [String: CGFloat] = [:]
    /// fitColumns is setting widths itself (its own changes are not the user's).
    private var fittingColumns = false

    /// Fits the columns to the room, on every layout pass, from their preferred widths. With room to spare, Name takes
    /// the rest. When the columns do not fit (in a narrow window they used to run past the right edge, Date Modified and Kind
    /// cut off, no scroller), take the width back in this order: Folder down to its minimum, Name down to 220 pt (room for
    /// a title and a snippet), then Date Modified, Kind and Size to their minimums, and Name last.
    private func fitColumns() {
        let available = scroll.contentSize.width
        guard available > 200 else { return }
        let cols = table.tableColumns
        let spacing = table.intercellSpacing.width * CGFloat(cols.count)
        var width: [String: CGFloat] = [:]
        for c in cols {
            let id = c.identifier.rawValue
            width[id] = max(c.minWidth, preferredWidths[id] ?? c.width)
        }
        var over = width.values.reduce(spacing, +) - available
        if over < 0 {
            width["name", default: 0] -= over             // room to spare: Name takes it
        } else {
            let steps: [(String, CGFloat?)] = [("folder", nil), ("name", 220), ("date", nil), ("kind", nil), ("size", nil), ("name", nil)]
            for (id, floor) in steps where over > 0 {
                guard let c = table.tableColumn(withIdentifier: NSUserInterfaceItemIdentifier(id)), let w = width[id] else { continue }
                let cut = min(over, max(0, w - max(c.minWidth, floor ?? 0)))
                if cut > 0 { width[id] = w - cut; over -= cut }
            }
        }
        fittingColumns = true
        defer { fittingColumns = false }
        for c in cols {
            if let w = width[c.identifier.rawValue], abs(c.width - w) > 0.5 { c.width = w }
        }
    }

    /// The user dragged a column's edge: that width is the column's preference from now on. AppKit's own width changes (the
    /// table autoresizing Name, fitColumns) are not recorded: only the column whose edge is being dragged, and not our own.
    private func columnResized(_ n: Notification) {
        guard !fittingColumns, let header = table.headerView, header.resizedColumn >= 0,
              header.resizedColumn < table.tableColumns.count,
              let col = n.userInfo?["NSTableColumn"] as? NSTableColumn,
              table.tableColumns[header.resizedColumn] === col else { return }
        preferredWidths[col.identifier.rawValue] = col.width
    }

    /// True while a name or content request is in flight, or a colour tag is still being read (the snapshot waits on this,
    /// so a render never catches the dots half way).
    var isLoading: Bool { nameLoading || moreLoading || tagLoadsPending > 0 || contentState == .loading || refining || timeline.isLoading || diskState == .loading || diskMoreLoading }

    override func loadView() {
        view = NSView()
        // Name takes any extra width, so a title and its snippet get the room; Folder, Size, Date Modified and Kind keep
        // theirs (fitColumns takes width back from them, Folder first, when the window is narrow, and gives it back).
        let column: (String, String, CGFloat, CGFloat, Bool) -> NSTableColumn = { id, title, width, min, sortable in
            let c = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(id))
            c.title = title
            c.width = width
            self.preferredWidths[id] = width
            c.minWidth = min
            c.resizingMask = id == "name" ? [.userResizingMask, .autoresizingMask] : .userResizingMask
            if sortable { c.sortDescriptorPrototype = NSSortDescriptor(key: id, ascending: true) }
            return c
        }
        table.addTableColumn(column("name", "Name", 380, 160, true))
        table.addTableColumn(column("folder", "Folder", 200, 100, true))
        table.addTableColumn(column("size", "Size", 76, 60, true))
        table.addTableColumn(column("date", "Date Modified", 160, 120, true))
        table.addTableColumn(column("kind", "Kind", 100, 80, true))
        table.style = .fullWidth
        table.rowHeight = 20
        table.intercellSpacing = NSSize(width: 8, height: 0)
        table.usesAlternatingRowBackgroundColors = false
        table.allowsMultipleSelection = true
        table.allowsEmptySelection = true
        table.columnAutoresizingStyle = .uniformColumnAutoresizingStyle
        table.dataSource = self
        table.delegate = self
        NotificationCenter.default.addObserver(forName: NSTableView.columnDidResizeNotification, object: table, queue: .main) { [weak self] n in
            MainActor.assumeIsolated { self?.columnResized(n) }
        }
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
        banner.textColor = MSEStyle.secondary
        banner.isHidden = true
        banner.translatesAutoresizingMaskIntoConstraints = false
        emptyState.translatesAutoresizingMaskIntoConstraints = false

        alsoLabel.font = .systemFont(ofSize: 11)
        alsoLabel.textColor = MSEStyle.secondary
        alsoLabel.lineBreakMode = .byTruncatingTail
        alsoLabel.isHidden = true
        alsoLabel.translatesAutoresizingMaskIntoConstraints = false
        filterBar.onChange = { [weak self] f in self?.filtersChanged(f) }
        filterBar.onCustomDate = { [weak self] in self?.showDateSheet() }
        filterBar.onClear = { [weak self] in self?.clearFilters() }
        filterBar.onEscape = { [weak self] in self?.onEscape?() }
        filterBar.onScopeButton = { [weak self] in self?.toggleScopePicker() }
        filterBar.onDocChange = { [weak self] change in self?.docChanged(change) }
        filterBar.onViewChoice = { [weak self] on in self?.setTimelineShown(on) }
        filterBar.onSaveSearch = { [weak self] in self?.saveSearchPressed() }
        timeline.translatesAutoresizingMaskIntoConstraints = false
        timeline.isHidden = true
        timeline.onRun = { [weak self] text, type in self?.timelineRan(text, type: type) }
        NotificationCenter.default.addObserver(forName: SavedSearchStore.picked, object: nil, queue: .main) { [weak self] n in
            MainActor.assumeIsolated { self?.savedPicked(n) }
        }
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
        view.addSubview(timeline)
        view.addSubview(emptyState)
        NSLayoutConstraint.activate([
            header.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor, constant: 8),
            header.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 12),
            header.trailingAnchor.constraint(lessThanOrEqualTo: view.trailingAnchor, constant: -12),
            scroll.topAnchor.constraint(equalTo: header.bottomAnchor, constant: 6),
            scroll.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            scroll.bottomAnchor.constraint(equalTo: view.bottomAnchor),
            timeline.topAnchor.constraint(equalTo: header.bottomAnchor, constant: 6),
            timeline.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            timeline.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            timeline.bottomAnchor.constraint(equalTo: view.bottomAnchor),
            emptyState.topAnchor.constraint(equalTo: scroll.topAnchor),
            emptyState.bottomAnchor.constraint(equalTo: scroll.bottomAnchor),
            emptyState.leadingAnchor.constraint(equalTo: scroll.leadingAnchor),
            emptyState.trailingAnchor.constraint(equalTo: scroll.trailingAnchor),
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
    var openSheet: NSWindow? { filterBar.openDocSheet ?? dateSheet?.sheetPanel }

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
        if diskObserver == nil {
            // The whole-disk settings were read or saved: the section follows them (WholeDiskStore, SettingsWholeDisk.swift).
            diskObserver = NotificationCenter.default.addObserver(forName: .wholeDiskChanged, object: nil, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated { self?.wholeDiskChanged() }
            }
        }
        // the document filters of the text, in their pop-ups (the answer's own filters replace them when it lands)
        filterBar.setDocs(DocQuery.parse(query))
        filterBar.setCanSave(!query.isEmpty)
        // a new search that is typed while the timeline is on shows the results
        if showingTimeline && !query.isEmpty { setTimelineShown(false) }
        if showTimelineOnRun && !query.isEmpty { showTimelineOnRun = false; setTimelineShown(true) }
        let parsed = ScopeShortcut.parse(query, catalog: catalog)
        shortcut = parsed.found ? parsed : nil
        shortcutUnknown = parsed.unknown
        generation += 1
        let gen = generation
        // Leading edge: nil = this keystroke searches at once (see SearchDebounce). Every call is stamped, early returns included.
        let wait = debounce.delay(for: query, now: DispatchTime.now().uptimeNanoseconds)
        nameTask?.cancel()
        contentTask?.cancel()
        recentTask?.cancel()
        nameLoading = false
        moreLoading = false
        namesFilteredOut = false
        tagCache.removeAll()
        diskTask?.cancel()
        diskState = .idle
        diskHits = []
        diskNext = nil
        diskHasMore = false
        diskMoreLoading = false
        contentState = .idle
        refining = false
        reranked = false
        facets = nil
        filterBar.setDocCounts([:])
        bridgeTerms = []
        errorText = nil
        errorTitle = ""
        errorRetry = false
        namesUnavailable = false
        facetRow.update(nil, filters: filters)
        refreshScopeUI()
        updateAlso()
        guard !query.isEmpty else {
            fileItems = []
            contentHits = []
            nameTotal = nil
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
            nameTotal = nil
            resultMs = nil
            fallback = false
            setBanner(nil)
            errorText = "Searching \(catalog.description(for: shownChoice)). Now type what to look for."
            errorTitle = "Ready to search"
            errorRetry = false
            rebuild()
            return
        }
        let q = query
        startDisk(q, gen)
        if !contentsOpen { contentHits = [] }
        if wantNames {
            nameLoading = true
            // The first keystroke after a pause is sent at once; the keystrokes of a burst wait 40 ms for the next one, so
            // typing fast sends the first and then one request after the last. A re: query only waits (250 ms).
            nameTask = Task { [weak self] in
                if let wait { try? await Task.sleep(nanoseconds: wait) }
                guard let self, !Task.isCancelled, gen == self.generation else { return }
                await self.fetchNames(q, gen)
            }
        } else {
            // Contents only: no name request at all.
            fileItems = []
            nameTotal = nil
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
    private var wantNames: Bool { activeScope == .mac || (namesOn && !DocQuery.parse(query).isActive) }

    /// The On this Mac section is asked for in Mac mode, while the settings turn whole-disk search on and the service has it.
    private var wantDisk: Bool { activeScope == .mac && WholeDiskStore.shared.enabled && !diskUnavailable }

    /// The hits not already listed as file names above: a file found both ways is listed once, as a name.
    private var visibleDiskHits: [DiskHit] { diskHits.filter { !namePaths.contains($0.path) } }

    /// The section is on screen: asked for, on its way, failed, or with hits to list.
    private var diskSectionVisible: Bool {
        wantDisk && !query.isEmpty && (diskState == .loading || diskState == .failed || !visibleDiskHits.isEmpty)
    }

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
        filterBar.setSources(names: namesOn, contents: contentsOn, macScope: activeScope == .mac,
                             namesOff: activeScope == .indexed && DocQuery.parse(query).isActive)
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
        if !n { nameTask?.cancel(); nameLoading = false; moreLoading = false }   // contents only from here on
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

    // MARK: On this Mac (Mac mode: the whole-disk hits)

    /// Asks the whole-disk service for the first page of the query of generation `gen`. Mac mode only, and only while the
    /// settings turn it on. A newer query cancels it. A 404 means the service has no whole-disk search: the section is not
    /// shown, and that is not an error.
    private func startDisk(_ q: String, _ gen: Int) {
        diskTask?.cancel()
        diskTask = nil
        diskMoreLoading = false
        WholeDiskStore.shared.ensureFresh()
        guard wantDisk, !q.isEmpty else { diskState = .idle; diskHits = []; return }
        diskState = .loading
        diskTask = Task { [weak self] in
            guard let self else { return }
            do {
                let page = try await MSEClient.shared.diskSearch(q, limit: SearchViewController.diskPageSize, offset: 0)
                guard gen == self.generation, !Task.isCancelled else { return }
                self.diskHits = page.items
                self.diskHasMore = page.hasMore
                self.diskNext = page.nextOffset
                self.diskState = .loaded
                self.diskTask = nil
                self.rebuild()
            } catch {
                guard gen == self.generation, !Task.isCancelled else { return }
                self.diskTask = nil
                if let api = error as? APIError, case .http(404) = api {
                    self.diskUnavailable = true
                    self.diskState = .idle
                } else {
                    self.diskState = .failed
                }
                self.rebuild()
            }
        }
        rebuild()
    }

    /// "Show more" in the On this Mac section: the next page, appended without the names already listed.
    private func loadMoreDisk() {
        guard diskHasMore, !diskMoreLoading, diskTask == nil, let next = diskNext else { return }
        let q = query
        let gen = generation
        diskMoreLoading = true
        rebuild()
        diskTask = Task { [weak self] in
            guard let self else { return }
            let fetched = try? await MSEClient.shared.diskSearch(q, limit: SearchViewController.diskPageSize, offset: next)
            guard gen == self.generation, !Task.isCancelled else { return }
            if let page = fetched {
                let seen = Set(self.diskHits.map { $0.path })
                self.diskHits += page.items.filter { !seen.contains($0.path) }
                self.diskHasMore = page.hasMore
                self.diskNext = page.nextOffset
            }
            self.diskMoreLoading = false
            self.diskTask = nil
            self.rebuild()
        }
    }

    /// The settings were read or saved: the section is asked again for the query on the screen.
    private func wholeDiskChanged() {
        diskUnavailable = false
        guard !query.isEmpty else { return }
        startDisk(query, generation)
    }

    /// The header of the section: a click or Return opens and closes it, and retries after a failure.
    private func activateDiskHeader() {
        if diskState == .failed, !query.isEmpty { startDisk(query, generation); return }
        diskExpanded.toggle()
        rebuild()
    }

    private var diskHeaderText: String {
        let n = visibleDiskHits.count
        let count = counts.string(from: NSNumber(value: n)) ?? "\(n)"
        let tail: String
        switch diskState {
        case .loading: tail = "searching…"
        case .failed: tail = "could not load · click to try again"
        default: tail = diskExpanded ? count : (n > 0 ? count + " · click to show" : "click to show")
        }
        return "Contents on this Mac · " + tail
    }

    private func diskHeaderCell() -> NSView {
        let cell = SectionHeaderCell()
        cell.title.stringValue = diskHeaderText
        let symbol = diskState == .failed ? "arrow.clockwise" : (diskExpanded ? "chevron.down" : "chevron.right")
        cell.chevron.image = NSImage(systemSymbolName: symbol, accessibilityDescription: diskExpanded ? "Expanded" : "Collapsed")
        cell.toolTip = "Click, or press Return, to show or hide the matches on this Mac. Hidden, they are not listed."
        return cell
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
        guard row >= 0, row < rows.count else { return false }
        switch rows[row] {
        case .contentHeader:
            if (NSApp.currentEvent?.clickCount ?? 1) <= 1 { activateContentHeader() }
            return true
        case .diskHeader:
            if (NSApp.currentEvent?.clickCount ?? 1) <= 1 { activateDiskHeader() }
            return true
        case .diskMore:
            loadMoreDisk()
            return true
        default:
            return false
        }
    }

    // MARK: jumps (⌃1, ⌃2, ⌃3)

    /// ⌃1: the file name rows.
    func jumpToNames() {
        guard let i = rows.firstIndex(where: { if case .file = $0 { return true }; return false }) ?? rows.firstIndex(where: { isSelectable($0) }) else { return }
        select(row: i)
    }

    /// ⌃2: the Content matches section; it opens if it was closed.
    func jumpToContents() {
        if diskSectionVisible {
            if !diskExpanded { activateDiskHeader() }
            if let i = rows.firstIndex(where: { if case .diskHeader = $0 { return true }; return false }) { select(row: i) }
            return
        }
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
        var items = [URLQueryItem(name: "q", value: nameQuery(q)), URLQueryItem(name: "limit", value: String(Self.firstPageSize)),
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
            namesFilteredOut = fileItems.count != parsed.count
            nameTotal = namesFilteredOut ? fileItems.count : ((dict["total"] as? NSNumber)?.intValue ?? fileItems.count)
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
            // The rest of the list, in the same task: a newer search cancels both requests.
            if let more = Self.nextNamesPage(dict) { await fetchMoreNames(items, more, gen) }
        } catch let APIError.http(code) where code == 404 {
            guard gen == generation, !Task.isCancelled else { return }
            await namesNotReady(q, gen, started, reason: .notInstalled)
        } catch let APIError.http(code) where code == 400 {
            // The service refuses a query it cannot read: an unfinished operator, or a name after in: it does not know.
            guard gen == generation, !Task.isCancelled else { return }
            let problem = queryProblem()
            fail(title: problem.title, message: problem.message, retry: false)
        } catch {
            guard gen == generation, !Task.isCancelled else { return }
            fail(title: "Search unavailable", message: "The search service did not answer.", retry: true)
        }
    }

    /// Whether the answer of the first page says there is more to fetch, and the page to ask for: the service's own
    /// next_offset, and the rest of the 200 names as the limit. Nil when the list is complete, when it is already as long
    /// as the 200, or when the first page was not the one asked for (a limit: operator in the text sets its own length).
    private static func nextNamesPage(_ dict: [String: Any]) -> (offset: Int, limit: Int)? {
        guard (dict["has_more"] as? Bool) == true,
              (dict["limit"] as? NSNumber)?.intValue == firstPageSize,
              let next = (dict["next_offset"] as? NSNumber)?.intValue, next > 0, next < namesCap else { return nil }
        return (next, namesCap - next)
    }

    /// The second request: the names after the first page, appended to the list. The first page stays on screen whatever
    /// happens here: a failed or cancelled request adds nothing and says nothing.
    private func fetchMoreNames(_ first: [URLQueryItem], _ page: (offset: Int, limit: Int), _ gen: Int) async {
        moreLoading = true
        defer { if gen == generation { moreLoading = false } }
        let items = first.filter { $0.name != "limit" }
            + [URLQueryItem(name: "offset", value: String(page.offset)), URLQueryItem(name: "limit", value: String(page.limit))]
        guard let obj = try? await MSEClient.shared.get("/api/files/search", query: items),
              gen == generation, !Task.isCancelled,
              let dict = obj as? [String: Any], (dict["searchable"] as? Bool) != false else { return }
        let parsed = ((dict["items"] as? [[String: Any]]) ?? []).compactMap(SearchItem.parse)
        let kept = parsed.filter { filters.keeps(path: $0.path) }
        if kept.count != parsed.count { namesFilteredOut = true }
        var seen = Set(fileItems.map { $0.path })                      // a path is listed once, whatever the service sends
        let fresh = kept.filter { seen.insert($0.path).inserted }
        fileItems += fresh                                             // may be empty: the count below still has to follow the filters
        nameTotal = namesFilteredOut ? fileItems.count : ((dict["total"] as? NSNumber)?.intValue ?? fileItems.count)
        moreLoading = false
        rebuild()
    }

    /// Nothing can be listed: say why (empty state), keep the section state honest.
    private func fail(title: String, message: String, retry: Bool) {
        nameLoading = false
        moreLoading = false
        fileItems = []
        contentHits = []
        contentState = .idle
        nameTotal = nil
        resultMs = nil
        errorText = message
        errorTitle = title
        errorRetry = retry
        setBanner(nil)
        rebuild()
    }

    private func queryProblem() -> (title: String, message: String) {
        if !shortcutUnknown.isEmpty {
            let known = catalog.folders.map { $0.name } + catalog.groups.map { $0.name }
            let names = shortcutUnknown.joined(separator: ", ")
            return ("Unknown folder",
                    "No folder or group called “\(names)” after in:" + (known.isEmpty ? "." : ". Known: " + known.joined(separator: ", ") + "."))
        }
        return ("Incomplete query", "An operator, or a name after in:, is not finished yet.")
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
            nameTotal = nil
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
            nameTotal = fileItems.count
            resultMs = Int(Date().timeIntervalSince(started) * 1000)
            nameLoading = false
            rebuild()
            scheduleRecent(gen)
        } catch {
            guard gen == generation, !Task.isCancelled else { return }
            nameLoading = false
            fileItems = []
            nameTotal = 0
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
        let items = [URLQueryItem(name: "q", value: DocQuery.forServer(cq)), URLQueryItem(name: "c", value: scopeParameter ?? "all"),
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
                if let applied = dict["filters"] as? [String: Any] { self.filterBar.setDocs(DocFilters(serverFilters: applied)) }
                self.filterBar.setDocCounts(Dictionary((self.facets?.doctypes ?? []).map { ($0.type, $0.count) }, uniquingKeysWith: { a, _ in a }))
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
        defer { refining = false; rebuild() }
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
                                          attributes: [.font: NSFont.systemFont(ofSize: 11), .foregroundColor: MSEStyle.secondary])
        s.append(NSAttributedString(string: bridgeTerms.joined(separator: " · "),
                                    attributes: [.font: NSFont.systemFont(ofSize: 11, weight: .medium), .foregroundColor: NSColor.controlAccentColor]))
        alsoLabel.attributedStringValue = s
        alsoLabel.isHidden = false
    }

    /// The name query sent to /api/files/search: the typed text plus the Mac operators of the filters.
    private func nameQuery(_ q: String) -> String {
        (([q] + filters.macOperators()).joined(separator: " ")).trimmingCharacters(in: .whitespaces)
    }

    // MARK: document filters (the search text is their source)

    /// Sets the search text, as if typed in the toolbar field (the field runs the search).
    private func setQueryText(_ text: String) {
        onRequestQuery?(text)
    }

    /// A document pop-up or the party field changed: its operators are rewritten in the text, and the search runs again.
    private func docChanged(_ change: DocChange) {
        let group: DocQuery.Group
        let new: [String]
        switch change {
        case .types(let t): group = .type; new = DocQuery.typeOperators(t)
        case .amount(let a): group = .amount; new = a.operators
        case .currency(let c): group = .currency; new = DocQuery.currencyOperators(c)
        case .party(let p): group = .party; new = DocQuery.partyOperators(p)
        case .docdate(let d): group = .docdate; new = DocQuery.docdateOperators(d)
        }
        let text = DocQuery.replacing(query, group: group, with: new)
        if text == query { filterBar.setDocs(DocQuery.parse(query)); return }
        setQueryText(text)
    }

    /// Snapshot hook: the Timeline is shown once the next search text is set (see the timeline state, Snapshot.swift).
    func snapshotShowTimelineOnRun() { showTimelineOnRun = true }

    /// Results (false) or Timeline (true). The timeline is for the Indexed scope's folders, and takes the document type
    /// the text names (the results keep the search text).
    private func setTimelineShown(_ on: Bool) {
        showingTimeline = on
        filterBar.setView(timeline: on)
        scroll.isHidden = on
        timeline.isHidden = !on
        if on {
            emptyState.isHidden = true
            timeline.load(scope: scopeParameter, type: DocQuery.parse(query).types.first)
        } else {
            updateEmptyNote()
        }
        onStatus?(statusText())
    }

    /// A month of the timeline was clicked: the search for that month and type (its own text), with the kind reset, because
    /// a file kind in the URL would narrow the months.
    private func timelineRan(_ text: String, type: DocType?) {
        setTimelineShown(false)
        var f = filters
        f.kind = .any
        filters = f
        filterBar.show(f)
        setQueryText(text)
    }

    /// A saved search was clicked in the sidebar (SavedSearches.swift): its text and folders run in Indexed mode, and its
    /// file filters become this window's, as they were saved. Notifications from other windows are ignored.
    private func savedPicked(_ n: Notification) {
        guard (n.object as? NSWindow) === view.window, let saved = n.userInfo?["saved"] as? SavedSearch else { return }
        let split = SavedQuery.split(saved.query)
        filters = split.filters
        filterBar.show(split.filters)
        setTimelineShown(false)
        let names = saved.collectionNames
        onScopeChosen?(names.isEmpty ? .allIndexed : .folders(names), names, split.text)
    }

    /// Save Search…: the current text, its file filters as operators, and the folders, saved under a name the user gives.
    private func saveSearchPressed() {
        guard let win = view.window, !query.isEmpty else { return }
        let text = SavedQuery.compose(query, filters: filters)
        SavedSearchStore.askName(title: "Save Search", value: "", button: "Save", in: win) { name in
            guard let name else { return }
            Task { @MainActor in
                do {
                    _ = try await SavedSearchStore.add(name: name, query: text, collection: self.scopeParameter)
                } catch {
                    SavedSearchStore.report(error, in: win)
                }
            }
        }
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
        var text = query
        for group in DocQuery.Group.allCases { text = DocQuery.replacing(text, group: group, with: []) }
        if text != query { setQueryText(text) }
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
        // The first thing to select is a result, not a section header: a selected header is a heavy highlighted band, and
        // a header selected only because it was the one row on screen while the content section loaded would stay
        // selected when the results arrive. So the header is the fallback only when the section is closed or failed:
        // those are final, and one Return or arrow key then opens it or tries again, as before.
        let firstResult = rows.firstIndex(where: { r in
            switch r { case .file, .content, .disk, .pick: return true; case .header, .contentHeader, .diskHeader, .diskMore: return false }
        })
        let headerIsFinal = (contentsCollapsible && !contentsExpanded) || contentState == .failed
        let headerRow = headerIsFinal ? rows.firstIndex(where: { if case .contentHeader = $0 { return true }; return false }) : nil
        if sel.isEmpty, let first = firstResult ?? headerRow { sel.insert(first) }
        if !sel.isEmpty { table.selectRowIndexes(sel, byExtendingSelection: false) }
        updateKindColumnTitle()
        updateEmptyNote()
        onStatus?(statusText())
    }

    /// The last column shows the file's kind on a name row and the channel that found it ("Words", "Words + meaning")
    /// on a content match. It is called "Match" only while every row in it is a content match, "Kind / Match" while
    /// both are listed, and "Kind" otherwise.
    private func updateKindColumnTitle() {
        var files = false, hits = false
        for r in rows {
            switch r { case .file, .pick: files = true; case .content, .disk: hits = true; case .header, .contentHeader, .diskHeader, .diskMore: break }
        }
        let title = hits ? (files ? "Kind / Match" : "Match") : "Kind"
        if let c = table.tableColumn(withIdentifier: NSUserInterfaceItemIdentifier("kind")), c.title != title { c.title = title }
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
        // A section with nothing in it is not listed: no "File names · 0", no "Content matches · 0".
        let showContent = contentSectionVisible && !contentSectionEmpty
        if wantNames && !namesUnavailable {
            let files = sortedFiles()
            // With both sections on screen the names get a header too: it carries their count and the ⌃1 target.
            if showContent && !files.isEmpty { out.append(.header(namesHeaderText)) }
            out += files.map { Row.file($0) }
        }
        if diskSectionVisible {
            out.append(.diskHeader)
            if diskExpanded {
                out += visibleDiskHits.map { Row.disk($0) }
                if diskHasMore || diskMoreLoading { out.append(.diskMore) }
            }
        }
        if showContent {
            out.append(.contentHeader)
            if contentsOpen { out += sortedHits().map { Row.content($0) } }
        }
        return out
    }

    /// How many results the current search lists, for the window subtitle (MainWindowController.refreshSubtitle). Files, not rows:
    /// the names (the service's total) plus the content matches that are not also a listed name, so a file found both ways
    /// counts once. 0 = nothing matched ("No matches"). nil = nothing to count: no text, the picker, the timeline, an error,
    /// a search still on its way (names or contents), or a Content matches section that is closed and so not searched.
    var resultCount: Int? {
        guard !query.isEmpty, !showingPicker, !showingTimeline, errorText == nil, !nameLoading, !moreLoading else { return nil }
        if noResults { return 0 }
        let namesListed = wantNames && !namesUnavailable
        var total = namesListed ? (nameTotal ?? fileItems.count) : 0
        if contentsOpen {
            guard contentState == .failed || (contentState == .loaded && contentLoadedGen == generation) else { return nil }
            let names = namesListed ? Set(fileItems.map { $0.path }) : []
            total += contentHits.filter { !names.contains($0.path) }.count
        }
        return total > 0 ? total : nil
    }

    /// "File names · 12", or "File names · 200 of 1,234" when the list is cut at 200.
    private var namesHeaderText: String {
        if nameLoading { return "File names · searching…" }
        let shown = fileItems.count
        if let total = nameTotal, total > shown {
            return "File names · \(counts.string(from: NSNumber(value: shown)) ?? "\(shown)") of \(counts.string(from: NSNumber(value: total)) ?? "\(total)")"
        }
        return "File names · \(counts.string(from: NSNumber(value: shown)) ?? "\(shown)")"
    }

    /// The Content matches header, one line in the style of the File names header: "Content matches · 4", then the state
    /// the section is in when it is not just the count.
    private var contentHeaderText: String {
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
            let count = counts.string(from: NSNumber(value: n)) ?? "\(n)"
            if collapsed { tail = n > 0 ? count + " · click to show" : "click to show" }
            else if refining { tail = count + " · refining the order…" }
            else if reranked { tail = count + " · re-ranked" }
            else { tail = count }
        default:
            tail = collapsed ? "press ↓ or click to load" : "…"
        }
        return "Content matches · " + tail
    }

    /// An open Content matches section that came back with nothing is not listed at all: no "Content matches · 0".
    /// A closed one keeps its header (it is the way to open it again), and so do loading and failed.
    private var contentSectionEmpty: Bool {
        contentsOpen && contentState == .loaded && contentLoadedGen == generation && contentHits.isEmpty
    }

    /// Nothing at all to list for this query (and nothing still on its way): the empty state speaks instead of headers.
    private var noResults: Bool {
        guard !query.isEmpty, !showingPicker, !nameLoading, !moreLoading, errorText == nil else { return false }
        if diskSectionVisible { return false }     // a whole-disk hit is a result too
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
        case .diskHeader: return "h:disk"
        case .disk(let h): return "d:" + h.path
        case .diskMore: return "m:disk"
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
    /// One shared view (EmptyStateView): an icon, a short title, one line and the buttons.
    private func updateEmptyNote() {
        if showingPicker || query.isEmpty || showingTimeline { emptyState.isHidden = true; return }
        if let e = errorText {
            let symbol = errorRetry ? "exclamationmark.triangle" : "magnifyingglass"
            emptyState.show(symbol: symbol, title: errorTitle, detail: e,
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
        let symbol: String, title: String, detail: String
        if namesUnavailable && !wantContents {
            symbol = "clock"
            title = "Name index not ready"
            detail = "File names cannot be searched yet. Contents of your indexed folders can answer now."
            actions.append(("Turn on Contents", { [weak self] in
                guard let self else { return }
                self.sourcesChanged(names: self.namesOn, contents: true, fromNames: false)
            }))
        } else if activeScope == .mac {
            symbol = "magnifyingglass"
            title = "No matches"
            detail = "No file name on this Mac matches “\(query)”."
            actions.append(("Search indexed folders", { [weak self] in self?.scopePicked(.allIndexed) }))
        } else if !wantContents {
            symbol = "magnifyingglass"
            title = "No matches"
            detail = "No file name in \(label) matches “\(query)”. Fast mode does not look inside files."
            actions.append(("Search contents too", { [weak self] in
                guard let self else { return }
                self.sourcesChanged(names: self.namesOn, contents: true, fromNames: false)
            }))
            actions.append(("Whole Mac", { [weak self] in self?.scopePicked(.wholeMac) }))
        } else {
            symbol = "magnifyingglass"
            title = "No matches"
            let hint = shortcut != nil ? "The shortcut in your search sets this scope."
                : (filters.method == .hybrid ? "Try other words, or widen the scope." : "Try “Words + meaning” in the method menu.")
            detail = "Nothing in \(label) matches “\(query)”. " + hint
            actions.append(("Whole Mac", { [weak self] in self?.scopePicked(.wholeMac) }))
            if shownChoice != .allIndexed { actions.append(("All indexed", { [weak self] in self?.scopePicked(.allIndexed) })) }
        }
        // Any filter: the file ones, and the document ones typed in the text (type:, amount:, party:). The same two sets
        // that make the filter bar's Clear button and its chips show, so the button here and the chips always agree.
        if filters.isActive || filterBar.docs.isActive { actions.append(clear) }
        emptyState.show(symbol: symbol, title: title, detail: detail, actions: actions)
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
        if wantNames && !namesUnavailable, let n = nameTotal {
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
        if wantDisk, !query.isEmpty, diskState == .loaded, !visibleDiskHits.isEmpty {
            parts.append("\(fmt(visibleDiskHits.count)) on this Mac")
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
        case .header, .contentHeader, .diskHeader, .diskMore: return 26
        case .content, .disk: return MSEStyle.rowHeight
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
            let label = NSTextField(labelWithString: text)
            label.font = MSEStyle.sectionHeaderFont
            label.textColor = MSEStyle.secondary
            label.lineBreakMode = .byTruncatingTail
            let c = NSTableCellView()
            c.addSubview(label)
            c.textField = label
            label.translatesAutoresizingMaskIntoConstraints = false
            NSLayoutConstraint.activate([
                label.leadingAnchor.constraint(equalTo: c.leadingAnchor, constant: 20),     // where the Content matches label starts (after its chevron)
                label.trailingAnchor.constraint(lessThanOrEqualTo: c.trailingAnchor, constant: -4),
                label.centerYAnchor.constraint(equalTo: c.centerYAnchor),
            ])
            return c
        case .contentHeader:
            guard id == "name" else { return nil }
            return contentHeaderCell()
        case .file(let item):
            return cellFor(id: id, name: item.name, match: item.match, detail: nil, icon: { NSWorkspace.shared.icon(forFile: item.path) },
                           folder: item.parent, size: item.isDir ? nil : item.size, date: item.mtime, dateText: item.dateText,
                           kind: item.isDir ? "Folder" : Self.kindName(item.kind), isDir: item.isDir,
                           tagPath: item.path, in: tableView)
        case .content(let hit):
            // Why it is here: the channels that found it ("Words + meaning"), and "Name" when its name matched too.
            let why = hit.whyText(alsoByName: namePaths.contains(hit.path))
            var tip = "Matched by " + why.lowercased()
            if let r = hit.relevance { tip += " · relevance \(r)%" }
            if !hit.kind.isEmpty { tip = Self.kindName(hit.kind) + " · " + tip }
            return cellFor(id: id, name: hit.title, match: [], detail: hit.snippet, icon: { NSWorkspace.shared.icon(forFile: hit.path) },
                           folder: URL(fileURLWithPath: hit.path).deletingLastPathComponent().path,
                           size: hit.size, date: hit.date, dateText: hit.dateText, kind: why, isDir: false, tip: tip,
                           tagPath: hit.path,
                           meta: hit.meta, metaLine: hit.meta.map { Self.metaLine($0, dateText: hit.dateText) }, in: tableView)
        case .diskHeader:
            guard id == "name" else { return nil }
            return diskHeaderCell()
        case .diskMore:
            guard id == "name" else { return nil }
            let more = Self.textCell(id: "disk.more")
            more.textField?.stringValue = diskMoreLoading ? "Loading more…" : "Show more on this Mac"
            more.textField?.textColor = .controlAccentColor
            return more
        case .disk(let hit):
            // The snippet with its matched words, else the labels the image was given; the channel that found it is the kind.
            var detail: NSAttributedString?
            if !hit.snippet.isEmpty {
                detail = SnippetText.attributed(hit.snippet, terms: [])
            } else if !hit.labels.isEmpty {
                detail = NSAttributedString(string: hit.labels.joined(separator: " · "),
                                            attributes: [.font: NSFont.systemFont(ofSize: 12), .foregroundColor: NSColor.secondaryLabelColor])
            }
            let why = hit.why == "label" ? "Label" : hit.why == "text" ? "Text" : hit.why == "vector" ? "Meaning" : "Name"
            return cellFor(id: id, name: hit.name, match: [], detail: detail, icon: { NSWorkspace.shared.icon(forFile: hit.path) },
                           folder: URL(fileURLWithPath: hit.path).deletingLastPathComponent().path,
                           size: hit.size, date: hit.mtime, kind: why, isDir: false, tip: "On this Mac · matched by " + why.lowercased(),
                           tagPath: hit.path, in: tableView)
        case .pick(let p):
            let symbol = p.isGroup ? "rectangle.3.group" : "square.stack.3d.up"
            let icon = { NSImage(systemSymbolName: symbol, accessibilityDescription: p.name) ?? NSImage() }
            let kind = p.isGroup ? "Group · \(p.files) folders" : "Indexed folder · \(counts.string(from: NSNumber(value: p.files)) ?? "\(p.files)") files"
            return cellFor(id: id, name: p.name, match: [], detail: nil, icon: icon, folder: p.path.map { ($0 as NSString).abbreviatingWithTildeInPath } ?? "",
                           size: nil, date: nil, kind: kind, isDir: true, in: tableView)
        }
    }

    /// The cell for one column of a row: the name column carries the icon and title, the others one text line.
    /// `icon` is asked for only by the name column, and `tagPath` (the file whose colour tags go after the name) is read
    /// only there too, off the main thread: the other columns look up neither.
    private func cellFor(id: String, name: String, match: [(Int, Int)], detail: NSAttributedString?, icon: () -> NSImage,
                         folder: String, size: Int64? = nil, date: Date?, dateText: String? = nil, kind: String, isDir: Bool,
                         tip: String? = nil, tagPath: String? = nil, meta: DocMeta? = nil, metaLine: String? = nil,
                         in tableView: NSTableView) -> NSView {
        if id == "name" {
            let cell = (tableView.makeView(withIdentifier: NSUserInterfaceItemIdentifier("name"), owner: self) as? SearchCell)
                ?? SearchCell(identifier: "name")
            let rich = detail != nil                                // a content match: title, snippet, document line
            let side = rich ? MSEStyle.iconSize : 16
            let image = icon()
            image.size = NSSize(width: side, height: side)
            cell.imageView?.image = image
            cell.setRich(rich)
            cell.title.attributedStringValue = Self.highlighted(name, match, weight: rich ? .medium : .regular)
            cell.setMeta(meta, line: metaLine)                      // hidden for a row without a document line
            cell.setSnippet(detail)
            showTags(in: cell, path: tagPath)
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
        (cell as? TextColumnCell)?.isSecondary = id == "folder"
        cell.toolTip = tip
        return cell
    }

    /// The colour dots of a name cell. A path read before is shown at once. Otherwise the dots are cleared (the cell may be
    /// a reused one), the tags are read off the main thread, and the answer is applied only if the cell still belongs to
    /// the same path: a reused cell is left alone. The read counts in isLoading until it ends, answer used or not.
    private func showTags(in cell: SearchCell, path: String?) {
        cell.tagPath = path
        guard let path else { cell.setTags([]); return }
        if let known = tagCache[path] { cell.setTags(known); return }
        cell.setTags([])
        tagLoadsPending += 1
        Task { @MainActor [weak self] in
            let colors = await Task.detached(priority: .userInitiated) { Self.tagColors(atPath: path) }.value
            guard let self else { return }
            self.tagLoadsPending -= 1
            self.tagCache[path] = colors
            if cell.tagPath == path { cell.setTags(colors) }
        }
    }

    /// The colours among the file's Finder tags (the one resource value FileItem reads for them, and nothing else).
    nonisolated private static func tagColors(atPath path: String) -> [TagColor] {
        let names = (try? URL(fileURLWithPath: path).resourceValues(forKeys: [.tagNamesKey]))?.tagNames ?? []
        return TagStore.colors(of: names)
    }

    /// The document line of a classified result: amount · date · counterparty (the badge before it carries the type).
    static func metaLine(_ meta: DocMeta, dateText: String?) -> String {
        var parts: [String] = []
        if let amount = meta.amountText { parts.append(amount) }
        let date = dateText ?? meta.docDate.flatMap { SearchDates.display($0) } ?? ""
        if !date.isEmpty { parts.append(date) }
        if let cp = meta.counterparty { parts.append(cp) }
        return parts.joined(separator: " · ")
    }

    /// The Content matches header row.
    private func contentHeaderCell() -> NSView {
        let cell = SectionHeaderCell()
        cell.title.stringValue = contentHeaderText
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
        let c = TextColumnCell()
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
    static func highlighted(_ name: String, _ ranges: [(Int, Int)], weight: NSFont.Weight = .regular) -> NSAttributedString {
        let out = NSMutableAttributedString(string: name, attributes: [.font: NSFont.systemFont(ofSize: 13, weight: weight)])
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
        let onDiskHeader: Bool = { if sel >= 0, sel < rows.count, case .diskHeader = rows[sel] { return true }; return false }()
        switch e.keyCode {
        case 36, 76:                                   // Return, Enter
            if onDiskHeader && !cmd { activateDiskHeader(); return true }
            if onHeader && !cmd { activateContentHeader(); return true }
            if cmd { revealSelected() } else { openSelected() }
            return true
        case 49 where !cmd:                            // Space
            if onDiskHeader { activateDiskHeader(); return true }
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
        case .header, .contentHeader, .diskHeader, .diskMore: return nil
        case .file(let i): return i.url
        case .disk(let h): return URL(fileURLWithPath: h.path)
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
        case .header, .contentHeader, .diskHeader: return
        case .diskMore: loadMoreDisk()
        case .disk(let h):
            recordOpened(h.path)
            NSWorkspace.shared.open(URL(fileURLWithPath: h.path))
        case .pick(let p): onPick?(p.members)
        case .file(let i):
            recordOpened(i.path)
            if i.isDir { onOpenFolder?(i.url) } else { NSWorkspace.shared.open(i.url) }
        case .content(let c):
            recordOpened(c.path)
            openPreview(c)
        }
    }

    /// Tells the service that the user opened a result (POST /api/files/opened {path, query}: the file ranks a little
    /// higher afterwards, first for exactly this search text). Fire and forget: it runs off the main thread and its answer
    /// and its errors are ignored, so it never blocks or changes anything on screen.
    private func recordOpened(_ path: String) {
        let text = query
        Task.detached(priority: .utility) {
            _ = try? await MSEClient.shared.postJSON("/api/files/opened", body: ["path": path, "query": text])
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
        if case .file(let i) = rows[row], i.isDir, !(library?.collections ?? []).contains(where: { $0.path == i.path }) {
            add("Deep-index this Folder", #selector(deepIndexFolderAction))
        }
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

    /// Deep-index the folder under the pointer: the same POST /api/collections the Folders pane's add flow sends.
    @objc private func deepIndexFolderAction() {
        let row = table.clickedRow
        guard row >= 0, row < rows.count, case .file(let i) = rows[row], i.isDir else { return }
        let path = i.path
        Task { @MainActor in
            do {
                try await MSEClient.shared.deepIndex(path: path)
                NotificationCenter.default.post(name: .deepIndexChanged, object: nil)
            } catch {
                let alert = NSAlert()
                alert.alertStyle = .warning
                alert.messageText = "The folder was not added for deep indexing"
                alert.informativeText = SW.clean(error)
                alert.addButton(withTitle: "OK")
                alert.runModal()
            }
        }
    }

    private func isContentRow(_ row: Int) -> Bool { if case .content = rows[row] { return true }; return false }

    /// Open File: the file in its own app (a content row's Return opens the preview instead).
    @objc private func openFileAction() {
        for row in selectedRowsInOrder() {
            guard row < rows.count else { continue }
            switch rows[row] {
            case .content(let c):
                recordOpened(c.path)
                NSWorkspace.shared.open(c.url)
            default: open(row: row)
            }
        }
    }

    @objc private func previewAction() {
        guard let row = selectedRowsInOrder().first, let (coll, rel) = previewTarget(forRow: row) else { return }
        if let target = url(forRow: row) { recordOpened(target.path) }
        onOpenPreview?(query, indexedNames, coll, rel)
    }

    @objc private func openWithAction(_ sender: NSMenuItem) {
        guard let pair = sender.representedObject as? [Any], pair.count == 2,
              let file = pair[0] as? URL, let app = pair[1] as? URL else { return }
        recordOpened(file.path)
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
