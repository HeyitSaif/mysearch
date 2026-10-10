// The filter bar above the native results, in the style of Finder's search bar: popups for type, date, size and
// sort, a segmented control for the search method, and two text fields for the files to include and exclude. Also
// the facet chips above the content matches (the web's .facets row) and the custom date range sheet.
// The bar only reports; SearchViewController owns the state and re-runs the search.

import Cocoa

/// What a facet chip picks: a type, a year (its whole year as a date range) or an indexed folder.
enum FacetPick {
    case kind(KindFilter)
    case year(String)
    case collection(String)
}

/// The facets of an /api/search response, as the web shows them (kind, year, collection counts).
struct SearchFacets {
    let kinds: [(key: String, count: Int)]
    let years: [(year: String, count: Int)]
    let collections: [(name: String, count: Int)]

    /// JSON objects lose their order, so the counts are sorted here: most first for kinds and collections, newest
    /// year first (the web shows the six newest years).
    init?(_ d: [String: Any]) {
        func counts(_ key: String) -> [(String, Int)] {
            guard let obj = d[key] as? [String: Any] else { return [] }
            return obj.compactMap { k, v in (v as? NSNumber).map { (k, $0.intValue) } }
        }
        kinds = counts("kind").sorted { $0.1 != $1.1 ? $0.1 > $1.1 : $0.0 < $1.0 }.map { (key: $0.0, count: $0.1) }
        years = counts("year").sorted { $0.0 > $1.0 }.prefix(6).map { (year: $0.0, count: $0.1) }
        collections = counts("collection").sorted { $0.1 != $1.1 ? $0.1 > $1.1 : $0.0 < $1.0 }.map { (name: $0.0, count: $0.1) }
        if kinds.isEmpty && years.isEmpty && collections.isEmpty { return nil }
    }
}

/// The filter popups and fields. Mac scope shows type, date, size and the globs; Indexed scope shows type, date,
/// the method, sort and the globs, as the web does (the web has no size and no sort).
@MainActor
final class SearchFilterBar: NSStackView, NSTextFieldDelegate {
    enum Mode { case mac, indexed }

    /// A filter changed: the whole filter set. The user runs the search again.
    var onChange: ((SearchFilters) -> Void)?
    /// "Custom range…" was chosen: the user opens the date sheet (the menu goes back to its last choice).
    var onCustomDate: (() -> Void)?
    /// Clear filters was pressed.
    var onClear: (() -> Void)?
    /// Esc in one of the fields: the user clears the search, as Esc does in the search field.
    var onEscape: (() -> Void)?
    /// The scope button was pressed: the user opens the scope picker.
    var onScopeButton: (() -> Void)?
    /// File names or Contents was ticked or unticked: (file names, contents, the file names box was the one clicked).
    /// The user keeps at least one on and answers with setSources.
    var onSourcesChanged: ((Bool, Bool, Bool) -> Void)?

    private(set) var filters = SearchFilters()
    private(set) var mode: Mode = .mac

    private let typePop = NSPopUpButton(frame: .zero, pullsDown: false)
    private let datePop = NSPopUpButton(frame: .zero, pullsDown: false)
    private let sizePop = NSPopUpButton(frame: .zero, pullsDown: false)
    private let sortPop = NSPopUpButton(frame: .zero, pullsDown: false)
    private let methodSeg = NSSegmentedControl(labels: [], trackingMode: .selectOne, target: nil, action: nil)
    private let include = NSTextField()
    private let exclude = NSTextField()
    private let clearButton = NSButton(title: "Clear filters", target: nil, action: nil)
    private let scopeButton = NSButton(title: "Whole Mac", target: nil, action: nil)
    private let namesCheck = NSButton(checkboxWithTitle: "File names", target: nil, action: nil)
    private let contentsCheck = NSButton(checkboxWithTitle: "Contents", target: nil, action: nil)
    private let rowZero = NSStackView()
    private let rowOne = NSStackView()
    private let rowTwo = NSStackView()
    private let includeGroup = NSStackView()
    private let excludeGroup = NSStackView()
    /// Whether every control sits on a single row (the window is wide enough) or the globs wrap onto a second row.
    private var singleRow = false
    private var observed: NSView?
    private var lastDate: DatePreset = .any
    private var quiet = false
    private var debounce: Task<Void, Never>?

    /// The method order of the segmented control, and the order of its labels.
    private let methods: [SearchMethod] = [.hybrid, .keyword, .semantic]
    private let dateTags: [(tag: Int, preset: DatePreset)] = [(0, .any), (1, .week), (2, .month), (3, .quarter), (4, .year)]
    private let customTag = 5
    private let customActiveTag = 6

    init() {
        super.init(frame: .zero)
        orientation = .vertical
        alignment = .leading
        spacing = 8
        translatesAutoresizingMaskIntoConstraints = false
        build()
        setMode(.mac)
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    private func build() {
        for pop in [typePop, datePop, sizePop, sortPop] {
            pop.controlSize = .regular
            pop.target = self
        }
        typePop.action = #selector(typeChanged(_:))
        typePop.setAccessibilityLabel("File type")
        for k in KindFilter.allCases {
            typePop.addItem(withTitle: k.title)
            typePop.lastItem?.tag = k.rawValue
        }
        datePop.action = #selector(dateChanged(_:))
        datePop.setAccessibilityLabel("Date")
        for (tag, preset) in dateTags {
            datePop.addItem(withTitle: preset.title)
            datePop.lastItem?.tag = tag
        }
        datePop.menu?.addItem(.separator())
        datePop.addItem(withTitle: "Custom range…")
        datePop.lastItem?.tag = customTag

        sizePop.action = #selector(sizeChanged(_:))
        sizePop.setAccessibilityLabel("Size")
        for (i, s) in [SizeFilter.any, .over1MB, .over10MB, .over100MB, .under1MB].enumerated() {
            sizePop.addItem(withTitle: s.title)
            sizePop.lastItem?.tag = i
        }

        sortPop.action = #selector(sortChanged(_:))
        sortPop.setAccessibilityLabel("Sort")
        for (i, s) in [ResultSort.best, .newest, .oldest, .name].enumerated() {
            sortPop.addItem(withTitle: s.title)
            sortPop.lastItem?.tag = i
        }

        methodSeg.segmentStyle = .rounded
        methodSeg.controlSize = .regular
        methodSeg.segmentCount = methods.count
        for (i, m) in methods.enumerated() {
            methodSeg.setLabel(m.title, forSegment: i)
            methodSeg.setWidth(0, forSegment: i)               // automatic width
        }
        methodSeg.target = self
        methodSeg.action = #selector(methodChanged(_:))
        methodSeg.setAccessibilityLabel("Search method")

        for (field, label, group, placeholder, help) in [
            (include, "Include:", includeGroup, "*.pdf, Invoices/", "Only these files: comma-separated, e.g. .pdf, *.pdf or Invoices/"),
            (exclude, "Exclude:", excludeGroup, "*.md, *.eml, backups/", "Skip these files: comma-separated, e.g. .md, *.eml or backups/"),
        ] {
            field.placeholderString = placeholder
            field.toolTip = help
            field.controlSize = .regular
            field.delegate = self
            field.target = self
            field.action = #selector(globCommitted(_:))
            field.setAccessibilityLabel(String(label.dropLast()))
            field.translatesAutoresizingMaskIntoConstraints = false
            field.widthAnchor.constraint(equalToConstant: 190).isActive = true
            let caption = NSTextField(labelWithString: label)
            caption.textColor = .secondaryLabelColor
            group.orientation = .horizontal
            group.alignment = .centerY
            group.spacing = 6
            group.setViews([caption, field], in: .leading)
        }

        clearButton.bezelStyle = .rounded
        clearButton.controlSize = .regular
        clearButton.target = self
        clearButton.action = #selector(clearPressed(_:))
        clearButton.toolTip = "Clear every filter (the search text stays)"
        clearButton.isHidden = true

        scopeButton.bezelStyle = .rounded
        scopeButton.controlSize = .regular
        scopeButton.imagePosition = .imageLeading
        scopeButton.lineBreakMode = .byTruncatingTail
        scopeButton.target = self
        scopeButton.action = #selector(scopePressed(_:))
        scopeButton.setAccessibilityLabel("Search scope")
        scopeButton.translatesAutoresizingMaskIntoConstraints = false
        scopeButton.widthAnchor.constraint(lessThanOrEqualToConstant: 300).isActive = true
        scopeButton.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        for box in [namesCheck, contentsCheck] {
            box.controlSize = .regular
            box.target = self
            box.action = #selector(sourcesClicked(_:))
        }
        namesCheck.setAccessibilityLabel("Search file names")
        contentsCheck.setAccessibilityLabel("Search contents")

        for row in [rowZero, rowOne, rowTwo] {
            row.orientation = .horizontal
            row.alignment = .centerY
            row.spacing = 10
        }
        rowZero.setViews([scopeButton, namesCheck, contentsCheck], in: .leading)
        addArrangedSubview(rowZero)
        addArrangedSubview(rowOne)
        addArrangedSubview(rowTwo)
        arrange()
    }

    private var rowOneViews: [NSView] { [typePop, datePop, sizePop, methodSeg, sortPop] }
    private var rowTwoViews: [NSView] { [includeGroup, excludeGroup, clearButton] }

    /// Puts the controls on one row or two. Hidden controls take no room (NSStackView detaches them).
    private func arrange() {
        if singleRow {
            rowOne.setViews(rowOneViews + rowTwoViews, in: .leading)
            rowTwo.setViews([], in: .leading)
            rowTwo.isHidden = true
        } else {
            rowOne.setViews(rowOneViews, in: .leading)
            rowTwo.setViews(rowTwoViews, in: .leading)
            rowTwo.isHidden = false
        }
    }

    /// The width the controls need on a single row, and the width the owning header offers.
    private func reflow() {
        guard let host = superview?.superview else { return }
        var need: CGFloat = 0
        let views = rowOneViews + rowTwoViews
        let visible = views.filter { !$0.isHidden }
        for v in visible { need += v.fittingSize.width }
        need += CGFloat(max(0, visible.count - 1)) * rowOne.spacing
        let want = need <= host.bounds.width - 24
        if want != singleRow {
            singleRow = want
            arrange()
        }
    }

    override func viewDidMoveToSuperview() {
        super.viewDidMoveToSuperview()
        if let o = observed { NotificationCenter.default.removeObserver(self, name: NSView.frameDidChangeNotification, object: o) }
        observed = superview?.superview
        if let host = observed {
            host.postsFrameChangedNotifications = true
            NotificationCenter.default.addObserver(self, selector: #selector(hostResized(_:)), name: NSView.frameDidChangeNotification, object: host)
        }
        DispatchQueue.main.async { [weak self] in self?.reflow() }
    }

    @objc private func hostResized(_ n: Notification) { reflow() }

    /// The scope button: its title and symbol, what it stands for, and whether the scope comes from a shortcut typed in
    /// the search field ("work: ..."), which shows in the accent colour.
    func setScope(title: String, symbol: String, tooltip: String, fromQuery: Bool) {
        scopeButton.title = title + (fromQuery ? " (typed)" : "") + "  \u{25BE}"
        scopeButton.image = NSImage(systemSymbolName: symbol, accessibilityDescription: title)
        scopeButton.contentTintColor = fromQuery ? .controlAccentColor : nil
        scopeButton.toolTip = tooltip + (fromQuery
            ? "\nSet by the shortcut in the search text. Choosing here replaces it."
            : "\nWhere to search (\u{2318}\u{2325}F). Typing work: before your words does the same.")
    }

    /// Shows the two boxes. In the Whole Mac scope file names are always searched and contents are not available.
    func setSources(names: Bool, contents: Bool, macScope: Bool) {
        namesCheck.state = (macScope || names) ? .on : .off
        contentsCheck.state = (!macScope && contents) ? .on : .off
        namesCheck.isEnabled = !macScope
        contentsCheck.isEnabled = !macScope
        namesCheck.toolTip = macScope
            ? "The whole Mac is searched by file name."
            : "File names: the instant search by name. Turn it off to search contents only."
        contentsCheck.toolTip = macScope
            ? "Contents are searched in indexed folders only. Pick a folder or group with the scope button (\u{2318}\u{2325}F)."
            : "Contents: the deep search of words and meaning inside the indexed files. Turn it off for the instant search by name only (no embedding, no re-ranking)."
        methodSeg.isEnabled = macScope || contents
        methodSeg.toolTip = (macScope || contents) ? nil : "The method applies to contents. Turn on Contents to use it."
    }

    /// The anchor of the scope popover.
    var scopeAnchor: NSView { scopeButton }

    /// Command-3: the keyboard moves to the first filter.
    func focusFirstFilter() {
        window?.makeFirstResponder(typePop)
    }

    @objc private func scopePressed(_ sender: Any?) { onScopeButton?() }

    @objc private func sourcesClicked(_ sender: NSButton) {
        onSourcesChanged?(namesCheck.state == .on, contentsCheck.state == .on, sender === namesCheck)
    }

    /// Mac scope shows type, date, size and the globs; Indexed scope shows type, date, method, sort and the globs.
    func setMode(_ m: Mode) {
        mode = m
        sizePop.isHidden = m == .indexed
        methodSeg.isHidden = m == .mac
        sortPop.isHidden = m == .mac
        syncControls()
        reflow()
    }

    /// Shows `f` in the controls without sending a change.
    func show(_ f: SearchFilters) {
        filters = f
        syncControls()
    }

    private func syncControls() {
        quiet = true
        defer { quiet = false }
        typePop.selectItem(withTag: filters.kind.rawValue)
        if case .custom = filters.date {
            rebuildDateMenu(customTitle: filters.date.title)
            datePop.selectItem(withTag: customActiveTag)
        } else {
            rebuildDateMenu(customTitle: nil)
            datePop.selectItem(withTag: dateTags.first { $0.preset == filters.date }?.tag ?? 0)
        }
        lastDate = filters.date
        sizePop.selectItem(withTag: [SizeFilter.any, .over1MB, .over10MB, .over100MB, .under1MB].firstIndex(of: filters.size) ?? 0)
        sortPop.selectItem(withTag: [ResultSort.best, .newest, .oldest, .name].firstIndex(of: filters.sort) ?? 0)
        methodSeg.selectedSegment = methods.firstIndex(of: filters.method) ?? 0
        if include.stringValue != filters.include { include.stringValue = filters.include }
        if exclude.stringValue != filters.exclude { exclude.stringValue = filters.exclude }
        clearButton.isHidden = !filters.isActive
        reflow()
    }

    /// The custom range shows as its own item, selected, after the separator.
    private func rebuildDateMenu(customTitle: String?) {
        if let old = datePop.itemArray.first(where: { $0.tag == customActiveTag }) {
            datePop.menu?.removeItem(old)
        }
        if let t = customTitle {
            datePop.addItem(withTitle: t)
            datePop.lastItem?.tag = customActiveTag
        }
    }

    private func emit() {
        clearButton.isHidden = !filters.isActive
        reflow()
        guard !quiet else { return }
        onChange?(filters)
    }

    @objc private func typeChanged(_ s: NSPopUpButton) {
        filters.kind = KindFilter(rawValue: s.selectedTag()) ?? .any
        emit()
    }

    @objc private func dateChanged(_ s: NSPopUpButton) {
        let tag = s.selectedTag()
        if tag == customTag {
            // The menu goes back to the last choice; the sheet sets the range when it is applied.
            syncControls()
            onCustomDate?()
            return
        }
        guard tag != customActiveTag else { return }
        filters.date = dateTags.first { $0.tag == tag }?.preset ?? .any
        emit()
    }

    @objc private func sizeChanged(_ s: NSPopUpButton) {
        let all: [SizeFilter] = [.any, .over1MB, .over10MB, .over100MB, .under1MB]
        filters.size = all[max(0, min(all.count - 1, s.selectedTag()))]
        emit()
    }

    @objc private func sortChanged(_ s: NSPopUpButton) {
        let all: [ResultSort] = [.best, .newest, .oldest, .name]
        filters.sort = all[max(0, min(all.count - 1, s.selectedTag()))]
        emit()
    }

    @objc private func methodChanged(_ s: NSSegmentedControl) {
        filters.method = methods[max(0, min(methods.count - 1, s.selectedSegment))]
        emit()
    }

    /// Return in a glob field sends it at once.
    @objc private func globCommitted(_ sender: NSTextField) {
        debounce?.cancel()
        readGlobs()
        emit()
    }

    /// Typing in a glob field waits for a short pause, so each keystroke does not start a search.
    func controlTextDidChange(_ obj: Notification) {
        readGlobs()
        debounce?.cancel()
        debounce = Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 350_000_000)
            guard let self, !Task.isCancelled else { return }
            self.emit()
        }
    }

    private func readGlobs() {
        filters.include = include.stringValue
        filters.exclude = exclude.stringValue
    }

    func control(_ control: NSControl, textView: NSTextView, doCommandBy commandSelector: Selector) -> Bool {
        if commandSelector == #selector(NSResponder.cancelOperation(_:)) {
            onEscape?()
            return true
        }
        return false
    }

    @objc private func clearPressed(_ sender: Any?) {
        onClear?()
    }

    /// The date sheet was cancelled: the menu shows the last range again (already restored by syncControls).
    func setDate(_ d: DatePreset) {
        filters.date = d
        lastDate = d
        emit()
    }
}

/// The chips above the content matches: kinds, years and folders with their counts. Clicking a chip filters; a
/// chip that is on is clicked again to turn its filter off, as in the web UI.
@MainActor
final class FacetRow: NSStackView {
    var onPick: ((FacetPick, Bool) -> Void)?

    init() {
        super.init(frame: .zero)
        orientation = .horizontal
        alignment = .centerY
        spacing = 6
        translatesAutoresizingMaskIntoConstraints = false
        isHidden = true
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    /// Rebuilds the chips from the facets of the last content search. Groups appear only when they say something:
    /// a kind or year group needs more than one value (or an active filter), a folder group more than one folder.
    func update(_ facets: SearchFacets?, filters: SearchFilters) {
        arrangedSubviews.forEach { $0.removeFromSuperview() }
        guard let f = facets else { isHidden = true; return }
        var groups: [[NSView]] = []
        let kindOn = filters.kind
        let kinds = f.kinds.filter { KindFilter.fromFacet($0.key) != nil || $0.key == "other" }
        if kinds.count > 1 || kindOn != .any {
            groups.append(kinds.map { k -> NSView in
                let label = KindFilter.fromFacet(k.key)?.title ?? "Other"
                let pick: FacetPick? = KindFilter.fromFacet(k.key).map { .kind($0) }
                let on = KindFilter.fromFacet(k.key) == kindOn
                return chip(label, count: k.count, on: on, pick: pick)
            })
        }
        var activeYear: String?
        if case .custom(let a, let b) = filters.date, Day.string(a).hasSuffix("-01-01"),
           Calendar.current.component(.year, from: a) == Calendar.current.component(.year, from: b),
           Day.string(b).hasSuffix("-12-31") {
            activeYear = String(Day.string(a).prefix(4))
        }
        if f.years.count > 1 || activeYear != nil {
            groups.append(f.years.map { y in
                chip(y.year, count: y.count, on: y.year == activeYear, pick: .year(y.year))
            })
        }
        if f.collections.count > 1 {
            groups.append(f.collections.map { c in chip(c.name, count: c.count, on: false, pick: .collection(c.name)) })
        }
        for (i, g) in groups.enumerated() {
            if i > 0 {
                let sep = NSTextField(labelWithString: "|")
                sep.textColor = .tertiaryLabelColor
                addArrangedSubview(sep)
            }
            g.forEach { addArrangedSubview($0) }
        }
        isHidden = groups.isEmpty
    }

    private func chip(_ label: String, count: Int, on: Bool, pick: FacetPick?) -> NSView {
        let b = FacetChip(title: "\(label)  \(count)", on: on) { [weak self] in
            guard let pick else { return }
            self?.onPick?(pick, on)
        }
        b.isEnabled = pick != nil
        return b
    }
}

/// A facet chip: a push-on, push-off button.
@MainActor
final class FacetChip: NSButton {
    private let onPress: () -> Void

    init(title: String, on: Bool, action: @escaping () -> Void) {
        onPress = action
        super.init(frame: .zero)
        self.title = title
        setButtonType(.pushOnPushOff)
        bezelStyle = .recessed
        controlSize = .small
        font = .systemFont(ofSize: 11)
        state = on ? .on : .off
        target = self
        self.action = #selector(pressed)
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    @objc private func pressed() { onPress() }
}

/// The custom date range sheet: two day pickers, From and To, then Cancel or Apply.
@MainActor
final class DateRangeSheet: NSObject {
    private let panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 340, height: 150),
                                styleMask: [.titled], backing: .buffered, defer: false)
    private let fromPicker = NSDatePicker()
    private let toPicker = NSDatePicker()
    private let onApply: (Date, Date) -> Void
    private let onCancel: () -> Void

    init(from: Date, to: Date, apply: @escaping (Date, Date) -> Void, cancel: @escaping () -> Void) {
        onApply = apply
        onCancel = cancel
        super.init()
        for p in [fromPicker, toPicker] {
            p.datePickerStyle = .textFieldAndStepper
            p.datePickerElements = [.yearMonthDay]
            p.datePickerMode = .single
            p.controlSize = .small
            p.translatesAutoresizingMaskIntoConstraints = false
        }
        fromPicker.dateValue = from
        toPicker.dateValue = to

        let title = NSTextField(labelWithString: "Custom date range")
        title.font = .systemFont(ofSize: 13, weight: .semibold)
        let fromRow = NSStackView(views: [NSTextField(labelWithString: "From"), fromPicker])
        let toRow = NSStackView(views: [NSTextField(labelWithString: "To"), toPicker])
        for r in [fromRow, toRow] {
            r.orientation = .horizontal
            r.alignment = .centerY
            r.spacing = 12
            r.arrangedSubviews.first?.widthAnchor.constraint(equalToConstant: 40).isActive = true
        }
        let cancelButton = NSButton(title: "Cancel", target: self, action: #selector(cancelPressed))
        cancelButton.keyEquivalent = "\u{1b}"
        let applyButton = NSButton(title: "Apply", target: self, action: #selector(applyPressed))
        applyButton.keyEquivalent = "\r"
        applyButton.bezelStyle = .rounded
        let buttons = NSStackView(views: [cancelButton, applyButton])
        buttons.orientation = .horizontal
        buttons.spacing = 10

        let stack = NSStackView(views: [title, fromRow, toRow, buttons])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 12
        stack.translatesAutoresizingMaskIntoConstraints = false
        let container = NSView()
        container.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: container.leadingAnchor, constant: 20),
            stack.trailingAnchor.constraint(lessThanOrEqualTo: container.trailingAnchor, constant: -20),
            stack.topAnchor.constraint(equalTo: container.topAnchor, constant: 18),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: container.bottomAnchor, constant: -18),
        ])
        panel.contentView = container
    }

    /// Shows the sheet on `window`; Apply and Cancel end it.
    func present(on window: NSWindow) {
        window.beginSheet(panel) { _ in }
    }

    /// The panel, for the snapshot (the sheet is a separate window, composited into the main window's picture).
    var sheetPanel: NSPanel { panel }

    @objc private func applyPressed() {
        var a = fromPicker.dateValue, b = toPicker.dateValue
        if a > b { swap(&a, &b) }
        panel.sheetParent?.endSheet(panel)
        onApply(a, b)
    }

    @objc private func cancelPressed() {
        panel.sheetParent?.endSheet(panel)
        onCancel()
    }
}
