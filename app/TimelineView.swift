// The timeline of documents (Indexed mode): one row per month of the scope's folders. For one type it draws that type's
// bar and its amounts per currency, and a month with no document of that type is a gap ("No receipts"). For all types it
// stacks one bar per type, with a legend, and shows no amounts (they would mix kinds of document). A click on a month runs
// the search for it: the server's own query for that bucket (type:receipt docdate:2025-03), as /api/docs/timeline gives it.
// Counts come from GET /api/docs/facets (the types the scope has) and GET /api/docs/timeline (one call per type).
// Everything is drawn with system colours, so the view follows light and dark mode.

import Cocoa

extension DocType {
    /// The plural a gap names: "No receipts", "No invoices".
    var plural: String {
        switch self {
        case .invoice: return "invoices"
        case .receipt: return "receipts"
        case .bankStatement: return "statements"
        case .contract: return "contracts"
        case .letter: return "letters"
        case .quote: return "quotes"
        case .reminder: return "reminders"
        case .payslip: return "payslips"
        case .taxDocument: return "tax documents"
        case .other: return "documents"
        }
    }

    /// The colour of this type's bar: a system colour per type, in DocType order.
    var barColor: NSColor {
        let palette: [NSColor] = [.systemBlue, .systemTeal, .systemOrange, .systemPurple, .systemGreen,
                                  .systemPink, .systemIndigo, .systemYellow, .systemBrown, .systemGray]
        let i = DocType.allCases.firstIndex(of: self) ?? 0
        return palette[i % palette.count]
    }
}

/// One month of the timeline: its count per type, and for the chosen type its amounts per currency.
struct TimelineMonth {
    let month: String                     // "2025-03"
    var counts: [DocType: Int] = [:]
    var amounts: [String: Double] = [:]
    /// The search for this month: the server's query for the bucket ("type:receipt docdate:2025-03").
    let query: String
    var total: Int { counts.values.reduce(0, +) }
}

@MainActor
final class TimelineView: NSView {
    /// A month was clicked: its search text and the chosen type (nil for all types).
    var onRun: ((String, DocType?) -> Void)?
    /// True while the counts are on their way (the snapshot waits on it).
    private(set) var isLoading = false

    private let typePop = NSPopUpButton(frame: .zero, pullsDown: false)
    private let summary = NSTextField(labelWithString: "")
    private let legend = NSTextField(labelWithString: "")
    private let note = NSTextField(wrappingLabelWithString: "")
    private let table = NSTableView()
    private let scroll = NSScrollView()

    private var months: [TimelineMonth] = []
    private var present: [DocType] = []           // the types the scope has, in DocType order
    private var chosen: DocType?                  // nil = all types
    private var scope: String?
    private var generation = 0
    private var task: Task<Void, Never>?

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        build()
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    private func build() {
        typePop.controlSize = .regular
        typePop.setAccessibilityLabel("Document type for the timeline")
        typePop.action = #selector(typeChanged(_:))
        typePop.target = self
        typePop.addItem(withTitle: "All types")
        typePop.lastItem?.tag = 0

        summary.font = .systemFont(ofSize: 12)
        summary.textColor = .secondaryLabelColor
        summary.lineBreakMode = .byTruncatingTail
        summary.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        legend.font = .systemFont(ofSize: 11)
        legend.lineBreakMode = .byTruncatingTail

        note.font = .systemFont(ofSize: 11)
        note.textColor = .secondaryLabelColor
        note.isHidden = true

        let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("month"))
        column.resizingMask = .autoresizingMask
        table.addTableColumn(column)
        table.headerView = nil
        table.rowHeight = 26
        table.intercellSpacing = NSSize(width: 0, height: 2)
        table.style = .fullWidth
        table.allowsEmptySelection = true
        table.allowsMultipleSelection = false
        table.usesAlternatingRowBackgroundColors = false
        table.columnAutoresizingStyle = .uniformColumnAutoresizingStyle
        table.dataSource = self
        table.delegate = self
        table.target = self
        table.action = #selector(rowClicked(_:))
        table.setAccessibilityLabel("Documents by month")

        scroll.documentView = table
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false
        scroll.translatesAutoresizingMaskIntoConstraints = false

        let header = NSStackView(views: [typePop, summary])
        header.orientation = .horizontal
        header.alignment = .centerY
        header.spacing = 10
        let stack = NSStackView(views: [header, legend, note, scroll])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 6
        stack.translatesAutoresizingMaskIntoConstraints = false
        addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 12),
            stack.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -12),
            stack.topAnchor.constraint(equalTo: topAnchor, constant: 4),
            stack.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -8),
            header.trailingAnchor.constraint(equalTo: stack.trailingAnchor),
            legend.trailingAnchor.constraint(equalTo: stack.trailingAnchor),
            scroll.leadingAnchor.constraint(equalTo: stack.leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: stack.trailingAnchor),
        ])
        scroll.setContentHuggingPriority(.defaultLow, for: .vertical)
    }

    /// Loads the timeline of the scope (the search's `c` value; nil = every indexed folder). `type` is the type the text
    /// names, which the pop-up starts on.
    func load(scope newScope: String?, type: DocType?) {
        scope = newScope
        chosen = type
        reload()
    }

    @objc private func typeChanged(_ s: NSPopUpButton) {
        let tag = s.selectedTag()
        let all = DocType.allCases
        chosen = tag >= 1 && tag <= all.count ? all[tag - 1] : nil
        reload()
    }

    @objc private func rowClicked(_ sender: Any?) {
        let row = table.clickedRow
        guard row >= 0, row < months.count else { return }
        onRun?(months[row].query, chosen)
    }

    private func reload() {
        generation += 1
        let gen = generation
        task?.cancel()
        isLoading = true
        summary.stringValue = "Loading…"
        legend.stringValue = ""
        note.isHidden = true
        let scopeItems: [URLQueryItem] = (scope?.isEmpty == false) ? [URLQueryItem(name: "collection", value: scope)] : []
        task = Task { [weak self] in
            guard let self else { return }
            defer { if gen == self.generation { self.isLoading = false } }
            do {
                let facets = (try await MSEClient.shared.get("/api/docs/facets", query: scopeItems) as? [String: Any]) ?? [:]
                guard gen == self.generation else { return }
                let counts = (facets["doctypes"] as? [[String: Any]]) ?? []
                self.present = DocType.allCases.filter { t in counts.contains { ($0["doctype"] as? String) == t.key } }
                self.rebuildTypeMenu()
                let types = self.chosen.map { [$0] } ?? self.present
                var perType: [(DocType, [[String: Any]])] = []
                for t in types {
                    let query = scopeItems + [URLQueryItem(name: "type", value: t.key)]
                    let obj = (try await MSEClient.shared.get("/api/docs/timeline", query: query) as? [String: Any]) ?? [:]
                    guard gen == self.generation else { return }
                    perType.append((t, (obj["buckets"] as? [[String: Any]]) ?? []))
                }
                self.months = TimelineView.merge(perType, chosen: self.chosen)
                self.table.reloadData()
                self.updateSummary()
            } catch {
                guard gen == self.generation else { return }
                self.months = []
                self.table.reloadData()
                self.summary.stringValue = "The search service did not answer."
            }
        }
    }

    /// The pop-up lists "All types" and the types the scope has; the chosen one is selected.
    private func rebuildTypeMenu() {
        typePop.removeAllItems()
        typePop.addItem(withTitle: "All types")
        typePop.lastItem?.tag = 0
        for t in present {
            typePop.addItem(withTitle: t.title)
            typePop.lastItem?.tag = (DocType.allCases.firstIndex(of: t) ?? 0) + 1
        }
        typePop.selectItem(withTag: chosen.flatMap { DocType.allCases.firstIndex(of: $0) }.map { $0 + 1 } ?? 0)
    }

    /// The months of the buckets, oldest first. For one type the buckets are that type's (gaps included); for all types
    /// the months of every type, each with its count per type.
    static func merge(_ perType: [(DocType, [[String: Any]])], chosen: DocType?) -> [TimelineMonth] {
        if let chosen {
            let buckets = perType.first(where: { $0.0 == chosen })?.1 ?? []
            return buckets.compactMap { b -> TimelineMonth? in
                guard let month = b["month"] as? String else { return nil }
                var m = TimelineMonth(month: month, query: (b["query"] as? String) ?? "docdate:\(month)")
                m.counts[chosen] = (b["count"] as? NSNumber)?.intValue ?? 0
                for (cur, v) in (b["amounts"] as? [String: Any]) ?? [:] {
                    if let n = (v as? NSNumber)?.doubleValue { m.amounts[cur] = n }
                }
                return m
            }
        }
        var byMonth: [String: TimelineMonth] = [:]
        for (t, buckets) in perType {
            for b in buckets {
                guard let month = b["month"] as? String else { continue }
                var m = byMonth[month] ?? TimelineMonth(month: month, query: "docdate:\(month)")
                m.counts[t] = (b["count"] as? NSNumber)?.intValue ?? 0
                byMonth[month] = m
            }
        }
        return byMonth.values.sorted { $0.month < $1.month }
    }

    private func updateSummary() {
        let shown = months
        let total = shown.reduce(0) { $0 + $1.total }
        guard !shown.isEmpty else {
            summary.stringValue = "No classified documents in this scope yet."
            legend.stringValue = ""
            note.stringValue = "Documents get their type, date and amount when they are classified. Nothing is shown until then."
            note.isHidden = false
            return
        }
        if let chosen {
            let gaps = shown.filter { $0.total == 0 }.count
            var parts = ["\(shown.count) months", "\(total) \(total == 1 ? "document" : "documents")"]
            if gaps > 0 { parts.append("\(gaps) with no \(chosen.plural)") }
            var sums: [String: Double] = [:]
            for m in shown { for (cur, v) in m.amounts { sums[cur, default: 0] += v } }
            let amounts = sums.keys.sorted().map { TimelineView.money(sums[$0] ?? 0, currency: $0) }
            summary.stringValue = parts.joined(separator: " · ") + (amounts.isEmpty ? "" : " · " + amounts.joined(separator: " · "))
            legend.attributedStringValue = TimelineView.legendText([chosen])
            note.isHidden = true
        } else {
            summary.stringValue = "\(shown.count) months · \(total) documents"
            legend.attributedStringValue = TimelineView.legendText(present)
            note.stringValue = "Pick a type to see its amounts per currency and the months with none."
            note.isHidden = false
        }
    }

    /// An amount in a currency, locale-formatted; the plain number when the code is not a currency.
    static func money(_ v: Double, currency: String) -> String {
        let f = NumberFormatter()
        f.numberStyle = .currency
        f.currencyCode = currency
        f.locale = Locale.current
        return f.string(from: NSNumber(value: v)) ?? "\(v) \(currency)"
    }

    /// "● Invoice  ● Receipt": a coloured dot in each type's colour, then its name.
    static func legendText(_ types: [DocType]) -> NSAttributedString {
        let out = NSMutableAttributedString()
        for t in types {
            out.append(NSAttributedString(string: "●", attributes: [.foregroundColor: t.barColor, .font: NSFont.systemFont(ofSize: 11)]))
            out.append(NSAttributedString(string: " \(t.title)   ", attributes: [.foregroundColor: NSColor.secondaryLabelColor,
                                                                              .font: NSFont.systemFont(ofSize: 11)]))
        }
        return out
    }
}

extension TimelineView: NSTableViewDataSource, NSTableViewDelegate {
    func numberOfRows(in tableView: NSTableView) -> Int { months.count }

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        let cell = (tableView.makeView(withIdentifier: NSUserInterfaceItemIdentifier("timeline.row"), owner: self) as? TimelineRowCell)
            ?? TimelineRowCell(identifier: "timeline.row")
        let maxTotal = months.map { $0.total }.max() ?? 1
        cell.configure(months[row], chosen: chosen, maxTotal: max(1, maxTotal))
        return cell
    }

    func tableView(_ tableView: NSTableView, shouldSelectRow row: Int) -> Bool { false }
}

/// One month row: the month, its bar (stacked per type, or the chosen type alone), its count, and its amounts. A gap of the
/// chosen type is drawn as a dashed outline with "No receipts" in the warning colour. Everything is drawn in draw(_:), so
/// the colours resolve in the view's appearance (light or dark) at each draw.
final class TimelineRowCell: NSTableCellView {
    private var month: TimelineMonth?
    private var chosen: DocType?
    private var maxTotal = 1

    init(identifier: String) {
        super.init(frame: .zero)
        self.identifier = NSUserInterfaceItemIdentifier(identifier)
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override var isFlipped: Bool { true }

    func configure(_ m: TimelineMonth, chosen: DocType?, maxTotal: Int) {
        month = m
        self.chosen = chosen
        self.maxTotal = maxTotal
        needsDisplay = true
    }

    override func draw(_ dirtyRect: NSRect) {
        guard let m = month else { return }
        let h = bounds.height
        let w = bounds.width
        let mid = h / 2
        let monthAttrs: [NSAttributedString.Key: Any] = [.font: NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .medium),
                                                         .foregroundColor: NSColor.labelColor]
        NSAttributedString(string: m.month, attributes: monthAttrs).draw(at: NSPoint(x: 12, y: mid - 8))

        let barX: CGFloat = 96
        let amountsWidth: CGFloat = chosen == nil ? 0 : min(220, w * 0.3)
        let barMax = max(40, w - barX - amountsWidth - 80)
        let barH: CGFloat = 12
        let barY = mid - barH / 2
        let countAttrs: [NSAttributedString.Key: Any] = [.font: NSFont.monospacedDigitSystemFont(ofSize: 11, weight: .regular),
                                                         .foregroundColor: NSColor.secondaryLabelColor]

        if let chosen, m.total == 0 {
            // a gap: a dashed outline the length of a typical month, and the name of what is missing
            let outline = NSBezierPath(roundedRect: NSRect(x: barX, y: barY, width: barMax * 0.4, height: barH), xRadius: 3, yRadius: 3)
            outline.setLineDash([3, 3], count: 2, phase: 0)
            outline.lineWidth = 1
            NSColor.systemOrange.setStroke()
            outline.stroke()
            NSAttributedString(string: "No \(chosen.plural)",
                               attributes: [.font: NSFont.systemFont(ofSize: 11, weight: .semibold),
                                            .foregroundColor: NSColor.systemOrange]).draw(at: NSPoint(x: barX + barMax * 0.4 + 8, y: mid - 7))
            return
        }

        var x = barX
        let total = CGFloat(m.total)
        for t in DocType.allCases {
            guard let n = m.counts[t], n > 0 else { continue }
            let width = max(2, CGFloat(n) / CGFloat(maxTotal) * barMax)
            t.barColor.setFill()
            NSBezierPath(roundedRect: NSRect(x: x, y: barY, width: width, height: barH), xRadius: 2, yRadius: 2).fill()
            x += width
        }
        if total > 0 {
            NSAttributedString(string: "\(m.total)", attributes: countAttrs).draw(at: NSPoint(x: x + 6, y: mid - 7))
        }

        if chosen != nil, !m.amounts.isEmpty {
            let text = m.amounts.keys.sorted().map { TimelineView.money(m.amounts[$0] ?? 0, currency: $0) }.joined(separator: " · ")
            let attrs: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 11), .foregroundColor: NSColor.labelColor]
            let str = NSAttributedString(string: text, attributes: attrs)
            let size = str.size()
            str.draw(at: NSPoint(x: w - size.width - 12, y: mid - size.height / 2))
        }
    }
}
