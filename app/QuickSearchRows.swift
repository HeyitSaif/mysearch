// The rows of Quick Search's list. One NSTableView (virtualised: cells are recycled) shows the flat list of QSRow values:
// section headers, file names, content matches, recent searches and status lines. The table never takes focus (the field
// keeps the keyboard) and draws the selection itself, in the accent colour, so it is the model's selection that shows.

import Cocoa

/// The table: it reports clicks and never becomes first responder.
final class QSTableView: NSTableView {
    var onClickRow: ((Int, Int) -> Void)?

    override var acceptsFirstResponder: Bool { false }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func mouseDown(with event: NSEvent) {
        let row = self.row(at: convert(event.locationInWindow, from: nil))
        if row >= 0 { onClickRow?(row, event.clickCount) }
    }
}

/// A row. The selected one is drawn as a rounded accent-coloured band (the cells then use the selected text colours).
final class QSRowView: NSTableRowView {
    var highlighted = false {
        didSet { if highlighted != oldValue { needsDisplay = true } }
    }

    override func drawBackground(in dirtyRect: NSRect) {
        guard highlighted else { return }
        NSColor.controlAccentColor.setFill()
        NSBezierPath(roundedRect: bounds.insetBy(dx: 6, dy: 1), xRadius: 8, yRadius: 8).fill()
    }

    override func drawSelection(in dirtyRect: NSRect) {}
}

/// What a file or content row shows, already formatted and coloured for its selection state.
struct QSItemView {
    let path: String
    let isDir: Bool
    let title: NSAttributedString
    let subtitle: NSAttributedString
    let trailTop: String?
    let badge: String?
    let trailBottom: String?
}

/// Text formats for the rows. Used on the main actor only (the formatters are not shared with other threads).
@MainActor
enum QSFormat {
    private static let day: DateFormatter = {
        let f = DateFormatter()
        f.dateStyle = .medium
        f.timeStyle = .none
        return f
    }()
    private static let time: DateFormatter = {
        let f = DateFormatter()
        f.dateStyle = .none
        f.timeStyle = .short
        return f
    }()
    private static let number: NumberFormatter = {
        let f = NumberFormatter()
        f.numberStyle = .decimal
        return f
    }()

    static func date(_ d: Date?) -> String? {
        guard let d else { return nil }
        return Calendar.current.isDateInToday(d) ? time.string(from: d) : day.string(from: d)
    }

    static func size(_ bytes: Int64?) -> String? {
        guard let bytes, bytes > 0 else { return nil }
        return ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file)
    }

    /// "€1,234.50" for a known currency, the plain number otherwise.
    static func amount(_ value: Double?, currency: String?) -> String? {
        guard let value else { return nil }
        guard let currency else { return number.string(from: NSNumber(value: value)) }
        let f = NumberFormatter()
        f.numberStyle = .currency
        f.currencyCode = currency
        return f.string(from: NSNumber(value: value)) ?? "\(value) \(currency)"
    }

    /// The folder of a path, with the home folder shown as ~.
    static func folder(_ path: String) -> String {
        (path as NSString).deletingLastPathComponent.replacingOccurrences(of: NSHomeDirectory(), with: "~")
    }

    /// The last two folders of a path ("Pictures/Safari"), for the narrow column at the right of a row.
    static func shortFolder(_ path: String) -> String {
        let parts = (path as NSString).deletingLastPathComponent.split(separator: "/").map(String.init)
        return parts.suffix(2).joined(separator: "/")
    }

    /// "bank_statement" → "Bank Statement".
    static func docType(_ raw: String) -> String {
        raw.replacingOccurrences(of: "_", with: " ").capitalized
    }

    static func highlighted(_ text: String, marks: [NSRange], font: NSFont, color: NSColor, markColor: NSColor) -> NSAttributedString {
        let s = NSMutableAttributedString(string: text, attributes: [.font: font, .foregroundColor: color])
        let bold = NSFont.systemFont(ofSize: font.pointSize, weight: .semibold)
        let length = s.length
        for r in marks where r.location >= 0 && NSMaxRange(r) <= length {
            s.addAttributes([.font: bold, .foregroundColor: markColor], range: r)
        }
        return s
    }
}

@MainActor
func qsLabel(size: CGFloat, weight: NSFont.Weight = .regular, lines: Int = 1) -> NSTextField {
    let f = NSTextField(labelWithString: "")
    f.font = .systemFont(ofSize: size, weight: weight)
    f.lineBreakMode = .byTruncatingTail
    f.maximumNumberOfLines = lines
    f.isSelectable = false
    f.allowsDefaultTighteningForTruncation = false
    return f
}

/// A file or content row: icon, name with the matched letters in bold, a line under it, and a column on the right.
@MainActor
final class QSItemCell: NSTableCellView {
    private let icon = NSImageView()
    private let title = qsLabel(size: 14)
    private let subtitle = qsLabel(size: 12)
    private let trailTop = qsLabel(size: 11)
    private let trailBottom = qsLabel(size: 11)
    private let badge = qsLabel(size: 10, weight: .semibold)
    private(set) var representedPath = ""

    init(identifier: NSUserInterfaceItemIdentifier) {
        super.init(frame: .zero)
        self.identifier = identifier
        icon.imageScaling = .scaleProportionallyUpOrDown
        title.lineBreakMode = .byTruncatingMiddle
        subtitle.lineBreakMode = .byTruncatingTail
        trailTop.alignment = .right
        trailBottom.alignment = .right
        badge.alignment = .center
        badge.wantsLayer = true
        badge.drawsBackground = true
        badge.layer?.cornerRadius = 4
        for v in [icon, title, subtitle, trailTop, trailBottom, badge] { addSubview(v) }
    }

    required init?(coder: NSCoder) { fatalError("QSItemCell is built in code") }

    func configure(_ v: QSItemView) {
        representedPath = v.path
        icon.image = QuickIcons.image(for: v.path, isDir: v.isDir) { [weak self] path, image in
            if self?.representedPath == path { self?.icon.image = image }
        }
        title.attributedStringValue = v.title
        subtitle.attributedStringValue = v.subtitle
        trailTop.stringValue = v.badge == nil ? (v.trailTop ?? "") : ""
        trailTop.isHidden = v.trailTop == nil || v.badge != nil
        trailBottom.stringValue = v.trailBottom ?? ""
        trailBottom.isHidden = v.trailBottom == nil
        badge.stringValue = v.badge ?? ""
        badge.isHidden = v.badge == nil
        needsLayout = true
    }

    /// Restyles the text for the selection without rebuilding the strings.
    func setSelectedStyle(_ selected: Bool) {
        let primary: NSColor = selected ? .alternateSelectedControlTextColor : .labelColor
        let secondary: NSColor = selected ? NSColor.alternateSelectedControlTextColor.withAlphaComponent(0.8) : .secondaryLabelColor
        title.textColor = primary
        subtitle.textColor = secondary
        trailTop.textColor = secondary
        trailBottom.textColor = secondary
        badge.textColor = primary
        badge.layer?.backgroundColor = (selected ? NSColor.alternateSelectedControlTextColor.withAlphaComponent(0.22) : NSColor.quaternaryLabelColor).cgColor
    }

    override func layout() {
        super.layout()
        let w = bounds.width
        let h = bounds.height
        icon.frame = NSRect(x: 14, y: (h - 28) / 2, width: 28, height: 28)
        let textX: CGFloat = 52
        let trailWidth: CGFloat = 200     // room for "Pictures/Safari · Aug 14, 2025" on a whole-disk row
        let textWidth = max(60, w - textX - trailWidth - 10)
        title.frame = NSRect(x: textX, y: h / 2 + 1, width: textWidth, height: 18)
        subtitle.frame = NSRect(x: textX, y: h / 2 - 17, width: textWidth, height: 16)
        trailBottom.frame = NSRect(x: w - 14 - trailWidth, y: h / 2 - 17, width: trailWidth, height: 16)
        trailTop.frame = NSRect(x: w - 14 - trailWidth, y: h / 2 + 1, width: trailWidth, height: 16)
        let badgeWidth = badge.isHidden ? 0 : ((badge.stringValue as NSString).size(withAttributes: [.font: badge.font as Any]).width + 14)
        badge.frame = NSRect(x: w - 14 - badgeWidth, y: h / 2 + 1, width: badgeWidth, height: 16)
    }
}

/// A section header: its title, its count, and the ⌃ number that jumps to it.
@MainActor
final class QSHeaderCell: NSTableCellView {
    private let title = qsLabel(size: 11, weight: .semibold)
    private let count = qsLabel(size: 11)
    private let key = qsLabel(size: 10, weight: .medium)

    init(identifier: NSUserInterfaceItemIdentifier) {
        super.init(frame: .zero)
        self.identifier = identifier
        count.textColor = .tertiaryLabelColor
        title.textColor = .secondaryLabelColor
        key.textColor = .tertiaryLabelColor
        key.alignment = .center
        key.wantsLayer = true
        key.drawsBackground = true
        key.backgroundColor = .clear
        key.layer?.cornerRadius = 4
        key.layer?.backgroundColor = NSColor.quaternaryLabelColor.cgColor
        for v in [title, count, key] { addSubview(v) }
    }

    required init?(coder: NSCoder) { fatalError("QSHeaderCell is built in code") }

    func configure(title t: String, count c: String, shortcut: Int?) {
        title.stringValue = t
        count.stringValue = c
        key.stringValue = shortcut.map { "⌃\($0)" } ?? ""
        key.isHidden = shortcut == nil
        needsLayout = true
    }

    override func layout() {
        super.layout()
        let w = bounds.width
        let h = bounds.height
        title.sizeToFit()
        title.frame = NSRect(x: 14, y: (h - 15) / 2, width: title.frame.width, height: 15)
        count.sizeToFit()
        count.frame = NSRect(x: title.frame.maxX + 6, y: (h - 14) / 2, width: count.frame.width, height: 14)
        let keyWidth: CGFloat = key.isHidden ? 0 : 26
        key.frame = NSRect(x: w - 14 - keyWidth, y: (h - 15) / 2, width: keyWidth, height: 15)
    }
}

/// A recent search (the empty query): a clock, and the query.
@MainActor
final class QSRecentCell: NSTableCellView {
    private let icon = NSImageView()
    private let text = qsLabel(size: 14)

    init(identifier: NSUserInterfaceItemIdentifier) {
        super.init(frame: .zero)
        self.identifier = identifier
        icon.image = NSImage(systemSymbolName: "clock", accessibilityDescription: nil)
        icon.contentTintColor = .secondaryLabelColor
        addSubview(icon)
        addSubview(text)
    }

    required init?(coder: NSCoder) { fatalError("QSRecentCell is built in code") }

    func configure(_ query: String, selected: Bool) {
        text.stringValue = query
        icon.contentTintColor = selected ? .alternateSelectedControlTextColor : .secondaryLabelColor
        text.textColor = selected ? .alternateSelectedControlTextColor : .labelColor
    }

    override func layout() {
        super.layout()
        let h = bounds.height
        icon.frame = NSRect(x: 18, y: (h - 16) / 2, width: 16, height: 16)
        text.frame = NSRect(x: 52, y: (h - 18) / 2, width: bounds.width - 66, height: 18)
    }
}

/// A line that is not a result: a hint, a search in progress, "No results", or an error with its Retry button.
@MainActor
final class QSStatusCell: NSTableCellView {
    private let text = qsLabel(size: 13)
    private let retry = NSButton(title: "Retry", target: nil, action: nil)
    var onRetry: (() -> Void)?

    init(identifier: NSUserInterfaceItemIdentifier) {
        super.init(frame: .zero)
        self.identifier = identifier
        retry.bezelStyle = .rounded
        retry.controlSize = .small
        retry.target = self
        retry.action = #selector(retryTapped)
        addSubview(text)
        addSubview(retry)
    }

    required init?(coder: NSCoder) { fatalError("QSStatusCell is built in code") }

    func configure(_ status: QSStatus, selected: Bool) {
        let primary: NSColor = selected ? .alternateSelectedControlTextColor : .secondaryLabelColor
        let quiet: NSColor = selected ? NSColor.alternateSelectedControlTextColor.withAlphaComponent(0.8) : .tertiaryLabelColor
        switch status {
        case .hint(let t):
            text.stringValue = t
            text.textColor = quiet
        case .searching:
            text.stringValue = "Searching…"
            text.textColor = quiet
        case .noResults(let q):
            text.stringValue = "No results for “\(q)”"
            text.textColor = primary
        case .offline:
            text.stringValue = "MySearch's search service is not running. Start it with mse service install."
            text.textColor = primary
        case .unauthorized:
            text.stringValue = "The search service refused this app's token. Restart MySearch, then retry."
            text.textColor = primary
        case .failed(let message):
            text.stringValue = message
            text.textColor = primary
        }
        retry.isHidden = !(status == .offline || status == .unauthorized || { if case .failed = status { return true }; return false }())
        retry.contentTintColor = selected ? .alternateSelectedControlTextColor : nil
        needsLayout = true
    }

    @objc private func retryTapped() { onRetry?() }

    override func layout() {
        super.layout()
        let w = bounds.width
        let h = bounds.height
        let buttonWidth: CGFloat = retry.isHidden ? 0 : 64
        retry.frame = NSRect(x: w - 14 - buttonWidth, y: (h - 22) / 2, width: buttonWidth, height: 22)
        text.frame = NSRect(x: 52, y: (h - 18) / 2, width: w - 52 - buttonWidth - 24, height: 18)
    }
}

/// The data source and delegate of the list: builds each row's cell from its QSRow and reports each row's height.
@MainActor
final class QSListSource: NSObject, NSTableViewDataSource, NSTableViewDelegate {
    var rows: [QSRow] = []
    var selectedID: String?
    var onRetry: (() -> Void)?

    private let headerID = NSUserInterfaceItemIdentifier("qs.header")
    private let itemID = NSUserInterfaceItemIdentifier("qs.item")
    private let recentID = NSUserInterfaceItemIdentifier("qs.recent")
    private let statusID = NSUserInterfaceItemIdentifier("qs.status")
    private let rowID = NSUserInterfaceItemIdentifier("qs.row")

    static func height(_ row: QSRow) -> CGFloat {
        switch row {
        case .header: return 26
        case .file: return 46
        case .content: return 56
        case .disk: return 56
        case .recentQuery: return 34
        case .recentFile: return 46
        case .status: return 40
        }
    }

    /// The total height of the rows, for the panel's size.
    static func totalHeight(_ rows: [QSRow]) -> CGFloat {
        rows.reduce(0) { $0 + height($1) }
    }

    func id(at row: Int) -> String? { rows.indices.contains(row) ? rows[row].id : nil }

    func index(ofID id: String) -> Int? { rows.firstIndex { $0.id == id } }

    func numberOfRows(in tableView: NSTableView) -> Int { rows.count }

    func tableView(_ tableView: NSTableView, heightOfRow row: Int) -> CGFloat {
        QSListSource.height(rows[row])
    }

    func tableView(_ tableView: NSTableView, rowViewForRow row: Int) -> NSTableRowView? {
        let view = (tableView.makeView(withIdentifier: rowID, owner: nil) as? QSRowView) ?? {
            let v = QSRowView()
            v.identifier = rowID
            return v
        }()
        let id = rows[row].id
        view.highlighted = id != nil && id == selectedID
        return view
    }

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        let selected = rows[row].id != nil && rows[row].id == selectedID
        switch rows[row] {
        case .header(let title, let count, let shortcut):
            let cell = (tableView.makeView(withIdentifier: headerID, owner: nil) as? QSHeaderCell) ?? QSHeaderCell(identifier: headerID)
            cell.configure(title: title, count: count, shortcut: shortcut)
            return cell
        case .file, .content, .recentFile, .disk:
            let cell = (tableView.makeView(withIdentifier: itemID, owner: nil) as? QSItemCell) ?? QSItemCell(identifier: itemID)
            cell.configure(itemView(for: rows[row], selected: selected))
            cell.setSelectedStyle(selected)
            return cell
        case .recentQuery(let query):
            let cell = (tableView.makeView(withIdentifier: recentID, owner: nil) as? QSRecentCell) ?? QSRecentCell(identifier: recentID)
            cell.configure(query, selected: selected)
            return cell
        case .status(let status):
            let cell = (tableView.makeView(withIdentifier: statusID, owner: nil) as? QSStatusCell) ?? QSStatusCell(identifier: statusID)
            cell.onRetry = { [weak self] in self?.onRetry?() }
            cell.configure(status, selected: selected)
            return cell
        }
    }

    /// The text of a file, content or recent-file row, in the colours of its selection state.
    private func itemView(for row: QSRow, selected: Bool) -> QSItemView {
        let primary: NSColor = selected ? .alternateSelectedControlTextColor : .labelColor
        let mark: NSColor = selected ? .alternateSelectedControlTextColor : .controlAccentColor
        let secondary: NSColor = selected ? NSColor.alternateSelectedControlTextColor.withAlphaComponent(0.8) : .secondaryLabelColor
        let titleFont = NSFont.systemFont(ofSize: 14)
        let subFont = NSFont.systemFont(ofSize: 12)

        switch row {
        case .file(let f, _):
            let title = QSFormat.highlighted(f.name, marks: f.match, font: titleFont, color: primary, markColor: mark)
            let subtitle = NSAttributedString(string: QSFormat.folder(f.path), attributes: [.font: NSFont.systemFont(ofSize: 11), .foregroundColor: secondary])
            return QSItemView(path: f.path, isDir: f.isDir, title: title, subtitle: subtitle,
                              trailTop: QSFormat.size(f.size), badge: nil, trailBottom: QSFormat.date(f.mtime))
        case .content(let c):
            let title = NSAttributedString(string: c.name, attributes: [.font: titleFont, .foregroundColor: primary])
            let snippet = QSFormat.highlighted(c.snippet, marks: c.marks, font: subFont, color: secondary, markColor: mark)
            let type = c.meta.map { QSFormat.docType($0.docType).uppercased() }
            let amount = QSFormat.amount(c.meta?.amount, currency: c.meta?.currency)
            let when = c.meta?.docDate ?? c.dateText ?? QSFormat.date(c.date)
            let bottom = [amount, when].compactMap { $0 }.joined(separator: " · ")
            return QSItemView(path: c.path, isDir: false, title: title, subtitle: snippet,
                              trailTop: nil, badge: type, trailBottom: bottom.isEmpty ? nil : bottom)
        case .disk(let d):
            let title = NSAttributedString(string: d.name, attributes: [.font: titleFont, .foregroundColor: primary])
            // the line under the name: the snippet with its matched words, else what the image was labelled, else the path
            let line: NSAttributedString
            var bottom: [String] = []
            if !d.snippet.isEmpty {
                line = QSFormat.highlighted(d.snippet, marks: d.marks, font: subFont, color: secondary, markColor: mark)
                bottom.append(QSFormat.shortFolder(d.path))
            } else if !d.labels.isEmpty {
                line = NSAttributedString(string: d.labels.joined(separator: " · "), attributes: [.font: subFont, .foregroundColor: secondary])
                bottom.append(QSFormat.shortFolder(d.path))
            } else {
                line = NSAttributedString(string: QSFormat.folder(d.path), attributes: [.font: NSFont.systemFont(ofSize: 11), .foregroundColor: secondary])
            }
            if let when = QSFormat.date(d.mtime) { bottom.append(when) }
            return QSItemView(path: d.path, isDir: false, title: title, subtitle: line, trailTop: nil, badge: d.badge,
                              trailBottom: bottom.isEmpty ? nil : bottom.joined(separator: " · "))
        case .recentFile(let r):
            let title = NSAttributedString(string: r.name, attributes: [.font: titleFont, .foregroundColor: primary])
            let subtitle = NSAttributedString(string: QSFormat.folder(r.path), attributes: [.font: NSFont.systemFont(ofSize: 11), .foregroundColor: secondary])
            return QSItemView(path: r.path, isDir: false, title: title, subtitle: subtitle, trailTop: nil, badge: nil, trailBottom: nil)
        default:
            return QSItemView(path: "", isDir: false, title: NSAttributedString(), subtitle: NSAttributedString(), trailTop: nil, badge: nil, trailBottom: nil)
        }
    }
}
