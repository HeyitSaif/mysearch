// The filter bar above the native results, in the style of Finder's search bar. Row 1 is where to search (scope, File
// names, Contents, Results | Timeline, Save Search); row 2 is the everyday filters (type, Modified, size or sort), the
// "More filters (n)" button and Clear. The button opens the search method, the document filters and the include and
// exclude globs. Under row 2, only when a filter is set, a strip holds one chip per filter, from every group, so a filter
// that is behind the button is never out of sight. Also the facet chips above the content matches (the web's .facets row) and the custom date range sheet.
// The bar only reports; SearchViewController owns the state and re-runs the search. Every chip and every control is a view
// of that state: a chip's x goes the way the control goes (onChange for a file filter, onDocChange for a document filter,
// which the controller writes into the search text).

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
    /// The document types of the matches (facets.doctype), most first: the Document pop-up shows their counts.
    let doctypes: [(type: DocType, count: Int)]

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
        doctypes = counts("doctype").compactMap { (key, n) -> (type: DocType, count: Int)? in
            guard let t = DocType(rawValue: key) else { return nil }
            return (type: t, count: n)
        }.sorted { $0.count != $1.count ? $0.count > $1.count : $0.type.rawValue < $1.type.rawValue }
        if kinds.isEmpty && years.isEmpty && collections.isEmpty && doctypes.isEmpty { return nil }
    }
}

/// One change of a document filter (Indexed mode). SearchViewController turns it into operators in the search text.
enum DocChange {
    case types([DocType])
    case amount(AmountChoice)
    case currency(String?)
    case party(String)
    case docdate(DatePreset)
}

/// The filter popups and fields. Mac scope shows type, date, size and the globs; Indexed scope shows type, date,
/// the method, sort and the globs, as the web does (the web has no size and no sort), and a row of document filters
/// (document type, amount, currency, party, document date) with the Results / Timeline switch and Save Search.
@MainActor
final class SearchFilterBar: NSStackView, NSTextFieldDelegate {
    enum Mode { case mac, indexed }

    /// A document filter changed (Indexed mode). The controller writes it into the search text and runs the search.
    var onDocChange: ((DocChange) -> Void)?
    /// Results (false) or Timeline (true) was chosen.
    var onViewChoice: ((Bool) -> Void)?
    /// Save Search… was pressed.
    var onSaveSearch: (() -> Void)?
    /// The document filters as the pop-ups show them (set by setDocs; the search text is the source).
    private(set) var docs = DocFilters()

    private let docTypePop = NSPopUpButton(frame: .zero, pullsDown: false)
    private let amountPop = NSPopUpButton(frame: .zero, pullsDown: false)
    private let currencyPop = NSPopUpButton(frame: .zero, pullsDown: false)
    private let docDatePop = NSPopUpButton(frame: .zero, pullsDown: false)
    private let partyField = NSTextField()
    private let viewSeg = NSSegmentedControl(labels: ["Results", "Timeline"], trackingMode: .selectOne, target: nil, action: nil)
    private let saveButton = NSButton(title: "Save Search…", target: nil, action: nil)
    private var partyDebounce: Task<Void, Never>?
    /// The sheet of a custom amount or date range, while it is open (the snapshot composites it).
    private var docSheet: AnyObject?
    /// The currencies the pop-up offers, in order; any other code shows as its own item.
    static let currencyCodes = ["EUR", "USD", "GBP", "CHF", "JPY", "CAD", "AUD", "SEK", "NOK", "DKK", "PLN", "CZK", "CNY", "INR", "BRL", "ZAR"]

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
    private let clearButton = NSButton(title: "Clear", target: nil, action: nil)
    private let scopeButton = NSButton(title: "Whole Mac", target: nil, action: nil)
    private let namesCheck = NSButton(checkboxWithTitle: "File names", target: nil, action: nil)
    private let contentsCheck = NSButton(checkboxWithTitle: "Contents", target: nil, action: nil)
    private let includeGroup = NSStackView()
    private let excludeGroup = NSStackView()
    /// "Modified" with the file's date pop-up, and "Document date" with the document's: the two dates are told apart by name.
    private let modifiedGroup = NSStackView()
    private let docDateGroup = NSStackView()
    /// "More filters (n)", at the end of row 2: opens the method, the document filters and the globs. n counts the filters set
    /// inside it.
    private let moreButton = NSButton(title: "More filters", target: nil, action: nil)
    /// Snapshot hook: the next bar starts with More filters open (Snapshot.swift sets it before the window is built).
    static var snapshotStartsOpen = false
    private var moreOpen = SearchFilterBar.snapshotStartsOpen
    /// The chips of the filters that are set, and what they were built from (a chip is rebuilt only when its text changes).
    private var chips: [FilterChipView] = []
    private var chipKeys: [String] = []
    private var chipsInMore = 0
    /// The lines of the bar: one persistent stack per line (a line that is wider than the bar wraps onto more stacks), and
    /// what each holds now. A stack whose controls did not change is left alone, so a field that is being typed in keeps
    /// the keyboard while a chip appears next to it.
    private var rowStacks: [String: NSStackView] = [:]
    private var appliedRows: [String: [ObjectIdentifier]] = [:]
    private var appliedOrder: [String] = []
    /// The width the lines fit before the bar has a host (narrow on purpose: a wide first arrangement would ask the window
    /// for that width, and nothing gives it back).
    private static let seedWidth: CGFloat = 480
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
            pop.bezelStyle = .rounded                          // NSButton.BezelStyle has no .roundedBezel; .rounded is the pop-up's pill
            pop.target = self
        }
        typePop.action = #selector(typeChanged(_:))
        typePop.setAccessibilityLabel("File type")
        for k in KindFilter.allCases {
            typePop.addItem(withTitle: k.title)
            typePop.lastItem?.tag = k.rawValue
        }
        datePop.action = #selector(dateChanged(_:))
        datePop.setAccessibilityLabel("Modified")
        datePop.toolTip = "When the file was last changed"
        modifiedGroup.orientation = .horizontal
        modifiedGroup.alignment = .centerY
        modifiedGroup.spacing = 6
        modifiedGroup.setViews([caption("Modified"), datePop], in: .leading)
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
            (include, "Include", includeGroup, "*.pdf, Invoices/", "Only these files: comma-separated, e.g. .pdf, *.pdf or Invoices/"),
            (exclude, "Exclude", excludeGroup, "*.md, *.eml, backups/", "Skip these files: comma-separated, e.g. .md, *.eml or backups/"),
        ] {
            field.placeholderString = placeholder
            field.toolTip = help
            field.controlSize = .regular
            field.isBezeled = true
            field.bezelStyle = .roundedBezel
            field.delegate = self
            field.target = self
            field.action = #selector(globCommitted(_:))
            field.setAccessibilityLabel(label)
            field.translatesAutoresizingMaskIntoConstraints = false
            field.widthAnchor.constraint(equalToConstant: 190).isActive = true
            group.orientation = .horizontal
            group.alignment = .centerY
            group.spacing = 6
            group.setViews([caption(label), field], in: .leading)
        }

        clearButton.bezelStyle = .rounded
        clearButton.controlSize = .regular
        clearButton.target = self
        clearButton.action = #selector(clearPressed(_:))
        clearButton.toolTip = "Clear every filter (the search text stays)"
        clearButton.setAccessibilityLabel("Clear filters")

        scopeButton.bezelStyle = .rounded
        scopeButton.controlSize = .regular
        scopeButton.imagePosition = .imageLeading
        scopeButton.lineBreakMode = .byTruncatingTail
        scopeButton.target = self
        scopeButton.action = #selector(scopePressed(_:))
        scopeButton.setAccessibilityLabel("Search scope")
        scopeButton.translatesAutoresizingMaskIntoConstraints = false
        scopeButton.widthAnchor.constraint(lessThanOrEqualToConstant: 300).isActive = true
        scopeButton.widthAnchor.constraint(greaterThanOrEqualToConstant: 96).isActive = true
        scopeButton.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        for box in [namesCheck, contentsCheck] {
            box.controlSize = .regular
            box.target = self
            box.action = #selector(sourcesClicked(_:))
        }
        namesCheck.setAccessibilityLabel("Search file names")
        contentsCheck.setAccessibilityLabel("Search contents")

        buildDocControls()
        viewSeg.segmentStyle = .rounded
        viewSeg.controlSize = .regular
        viewSeg.target = self
        viewSeg.action = #selector(viewChanged(_:))
        viewSeg.setAccessibilityLabel("Results or timeline")
        viewSeg.toolTip = "Show the matching documents as a list, or as a timeline of months (per type, with amounts)"
        for i in 0..<2 { viewSeg.setWidth(0, forSegment: i) }
        saveButton.bezelStyle = .rounded
        saveButton.controlSize = .regular
        saveButton.target = self
        saveButton.action = #selector(savePressed(_:))
        saveButton.toolTip = "Save this search, its filters and folders under a name. Your files are untouched."
        saveButton.setAccessibilityLabel("Save search")
        // A stack view hugs at a low priority by default, so a row that is wider than its controls (the bar is as wide as its
        // widest row) handed the spare width to the first stack in it and spread its controls apart. The groups hold their width.
        for group in [modifiedGroup, docDateGroup, includeGroup, excludeGroup] {
            group.setHuggingPriority(.required, for: .horizontal)
        }
        moreButton.bezelStyle = .rounded
        moreButton.controlSize = .regular
        moreButton.imagePosition = .imageLeading
        moreButton.target = self
        moreButton.action = #selector(moreToggled(_:))
        moreButton.toolTip = "The search method, the document filters (type, amount, currency, party, document date) and the file globs"
        moreButton.setAccessibilityLabel("More filters")
        updateChips()
        applyLayout(width: SearchFilterBar.seedWidth)
    }

    /// A caption in front of a control: the one secondary colour.
    private func caption(_ text: String) -> NSTextField {
        let c = NSTextField(labelWithString: text)
        c.textColor = MSEStyle.secondary
        return c
    }

    /// The document filters: the document type, the amount, the currency, the party and the document date.
    private func buildDocControls() {
        for pop in [docTypePop, amountPop, currencyPop, docDatePop] {
            pop.controlSize = .regular
            pop.bezelStyle = .rounded
            pop.target = self
        }
        docTypePop.action = #selector(docTypeChanged(_:))
        docTypePop.setAccessibilityLabel("Document type")
        docTypePop.toolTip = "The type of document (type:invoice, doctype:other …), counted from the classified documents"
        docTypePop.addItem(withTitle: "Any document")
        docTypePop.lastItem?.tag = 0
        for (i, t) in DocType.allCases.enumerated() {
            docTypePop.addItem(withTitle: t.title)
            docTypePop.lastItem?.tag = i + 1
        }

        amountPop.action = #selector(amountChanged(_:))
        amountPop.setAccessibilityLabel("Amount")
        amountPop.toolTip = "The document's amount (an invoice's gross total, a receipt's total paid)"
        for (tag, title) in [(0, "Any amount"), (1, "Under 100"), (2, "100 to 500"), (3, "Over 500"), (4, "Custom…")] {
            amountPop.addItem(withTitle: title)
            amountPop.lastItem?.tag = tag
        }

        currencyPop.action = #selector(currencyChanged(_:))
        currencyPop.setAccessibilityLabel("Currency")
        currencyPop.toolTip = "The currency of the amount (currency:EUR)"
        currencyPop.addItem(withTitle: "Any currency")
        currencyPop.lastItem?.tag = 0
        for (i, code) in SearchFilterBar.currencyCodes.enumerated() {
            currencyPop.addItem(withTitle: code)
            currencyPop.lastItem?.tag = i + 1
        }

        partyField.placeholderString = "Party"
        partyField.toolTip = "The counterparty contains this text, any case or accent (party:acme). Commas mean any of them."
        partyField.controlSize = .regular
        partyField.isBezeled = true
        partyField.bezelStyle = .roundedBezel
        partyField.delegate = self
        partyField.target = self
        partyField.action = #selector(partyCommitted(_:))
        partyField.setAccessibilityLabel("Party")
        partyField.translatesAutoresizingMaskIntoConstraints = false
        partyField.widthAnchor.constraint(equalToConstant: 170).isActive = true

        docDatePop.action = #selector(docDateChanged(_:))
        docDatePop.setAccessibilityLabel("Document date")
        docDatePop.toolTip = "The date in the document, not the file's (docdate:2025-03)"
        for (tag, preset) in dateTags {
            docDatePop.addItem(withTitle: preset.title)
            docDatePop.lastItem?.tag = tag
        }
        docDatePop.menu?.addItem(.separator())
        docDatePop.addItem(withTitle: "Custom range…")
        docDatePop.lastItem?.tag = customTag

        docDateGroup.orientation = .horizontal
        docDateGroup.alignment = .centerY
        docDateGroup.spacing = 6
        docDateGroup.setViews([caption("Document date"), docDatePop], in: .leading)
    }

    /// The counts of the document types in the matches: each type's item says how many, as "Invoice  4". The "Any document"
    /// item stays plain. Called after each content search, so the counts are the answer's own.
    func setDocCounts(_ counts: [DocType: Int]) {
        for (i, t) in DocType.allCases.enumerated() {
            guard let item = docTypePop.itemArray.first(where: { $0.tag == i + 1 }) else { continue }
            item.title = counts[t].map { "\(t.title)  \($0)" } ?? t.title
        }
    }

    /// Shows the document filters in their pop-ups and field without sending a change: the search text is the source
    /// (the controller calls this after each search, from the text or from the server's answer).
    func setDocs(_ d: DocFilters) {
        docs = d
        // document type: one item per type, or "Several types" when the text names more than one
        setDynamic(docTypePop, tag: 90, title: d.types.count > 1 ? "Several types" : nil)
        if d.types.count > 1 {
            docTypePop.selectItem(withTag: 90)
        } else if let t = d.types.first, let i = DocType.allCases.firstIndex(of: t) {
            docTypePop.selectItem(withTag: i + 1)
        } else {
            docTypePop.selectItem(withTag: 0)
        }
        // amount: a preset, or the custom range as its own item
        var amountTag = 0
        switch d.amount {
        case .any: amountTag = 0
        case .under100: amountTag = 1
        case .between100and500: amountTag = 2
        case .over500: amountTag = 3
        case .custom: amountTag = 5
        }
        var customAmountTitle: String?
        if case .custom = d.amount { customAmountTitle = d.amount.title }
        setDynamic(amountPop, tag: 5, title: customAmountTitle)
        amountPop.selectItem(withTag: amountTag)
        // currency: a listed code, or the code the text names as its own item
        if let c = d.currency {
            if let i = SearchFilterBar.currencyCodes.firstIndex(of: c) {
                setDynamic(currencyPop, tag: 90, title: nil)
                currencyPop.selectItem(withTag: i + 1)
            } else {
                setDynamic(currencyPop, tag: 90, title: c)
                currencyPop.selectItem(withTag: 90)
            }
        } else {
            setDynamic(currencyPop, tag: 90, title: nil)
            currencyPop.selectItem(withTag: 0)
        }
        // party
        if partyField.stringValue != d.party { partyField.stringValue = d.party }
        // document date: a preset, or the custom range as its own item (the same look as the file date)
        if case .custom = d.docdate {
            setDynamic(docDatePop, tag: customActiveTag, title: d.docdate.title)
            docDatePop.selectItem(withTag: customActiveTag)
        } else {
            setDynamic(docDatePop, tag: customActiveTag, title: nil)
            docDatePop.selectItem(withTag: dateTags.first { $0.preset == d.docdate }?.tag ?? 0)
        }
        refresh()
    }

    /// Adds or removes the item with `tag` at the end of a pop-up (the custom and the unlisted choices).
    private func setDynamic(_ pop: NSPopUpButton, tag: Int, title: String?) {
        if let old = pop.itemArray.first(where: { $0.tag == tag }) { pop.menu?.removeItem(old) }
        if let title {
            pop.addItem(withTitle: title)
            pop.lastItem?.tag = tag
        }
    }

    /// Results or Timeline, as the controller shows it.
    func setView(timeline: Bool) {
        viewSeg.selectedSegment = timeline ? 1 : 0
    }

    /// Save Search… is for a search with words or a filter in it.
    func setCanSave(_ on: Bool) {
        saveButton.isEnabled = on
    }

    @objc private func viewChanged(_ s: NSSegmentedControl) {
        onViewChoice?(s.selectedSegment == 1)
    }

    @objc private func savePressed(_ sender: Any?) {
        onSaveSearch?()
    }

    @objc private func docTypeChanged(_ s: NSPopUpButton) {
        let all = DocType.allCases
        let tag = s.selectedTag()
        guard tag >= 1 && tag <= all.count else {
            onDocChange?(.types([]))
            return
        }
        onDocChange?(.types([all[tag - 1]]))
    }

    @objc private func amountChanged(_ s: NSPopUpButton) {
        switch s.selectedTag() {
        case 1: onDocChange?(.amount(.under100))
        case 2: onDocChange?(.amount(.between100and500))
        case 3: onDocChange?(.amount(.over500))
        case 4:
            setDocs(docs)                           // the menu goes back to the current choice until the sheet answers
            presentAmountSheet()
        default: onDocChange?(.amount(.any))
        }
    }

    @objc private func currencyChanged(_ s: NSPopUpButton) {
        let tag = s.selectedTag()
        guard tag != 90 else { return }
        let codes = SearchFilterBar.currencyCodes
        onDocChange?(.currency(tag >= 1 && tag <= codes.count ? codes[tag - 1] : nil))
    }

    @objc private func docDateChanged(_ s: NSPopUpButton) {
        let tag = s.selectedTag()
        if tag == customTag {
            setDocs(docs)
            presentDocDateSheet()
            return
        }
        guard tag != customActiveTag else { return }
        onDocChange?(.docdate(dateTags.first { $0.tag == tag }?.preset ?? .any))
    }

    @objc private func partyCommitted(_ sender: NSTextField) {
        partyDebounce?.cancel()
        onDocChange?(.party(sender.stringValue))
    }

    /// The custom amount sheet: a minimum and a maximum, either may be empty.
    private func presentAmountSheet() {
        guard let win = window else { return }
        let sheet = AmountRangeSheet(lo: docs.amount.customBounds.0, hi: docs.amount.customBounds.1) { [weak self] lo, hi in
            self?.docSheet = nil
            let choice: AmountChoice = (lo == nil && hi == nil) ? .any : .custom(lo, hi)
            self?.onDocChange?(.amount(choice))
        } cancel: { [weak self] in
            self?.docSheet = nil
        }
        docSheet = sheet
        sheet.present(on: win)
    }

    /// The custom document date sheet: the same From and To as the file's custom range.
    private func presentDocDateSheet() {
        guard let win = window else { return }
        var from = Calendar.current.date(byAdding: .day, value: -30, to: Date()) ?? Date()
        var to = Date()
        if case .custom(let a, let b) = docs.docdate { from = a; to = b }
        let sheet = DateRangeSheet(from: from, to: to, apply: { [weak self] a, b in
            self?.docSheet = nil
            self?.onDocChange?(.docdate(.custom(a, b)))
        }, cancel: { [weak self] in
            self?.docSheet = nil
        })
        docSheet = sheet
        sheet.present(on: win)
    }

    /// The sheet that is open over this bar (a custom amount or date), for the snapshot.
    var openDocSheet: NSWindow? {
        if let s = docSheet as? DateRangeSheet { return s.sheetPanel }
        if let s = docSheet as? AmountRangeSheet { return s.sheetPanel }
        return nil
    }

    /// Clear shows when any filter is set, the file ones or the document ones.
    private var clearVisible: Bool { filters.isActive || docs.isActive }

    // MARK: chips

    /// One chip: what it says, what it names in full, whether the filter lives behind More filters, and how it goes away.
    private struct ChipSpec {
        let key: String
        let text: String
        let tip: String
        let inMore: Bool
        let remove: () -> Void
    }

    /// One chip per filter that is set, from every group: the file filters, then the document filters. A chip is a view of
    /// the same state the controls show; its x does what the control does (see removeFileFilter and onDocChange).
    private func chipSpecs() -> [ChipSpec] {
        var out: [ChipSpec] = []
        let indexed = mode == .indexed
        func file(_ key: String, _ text: String, _ tip: String, inMore: Bool, _ clear: @escaping (inout SearchFilters) -> Void) {
            out.append(ChipSpec(key: key, text: text, tip: tip, inMore: inMore, remove: { [weak self] in self?.removeFileFilter(clear) }))
        }
        func doc(_ key: String, _ text: String, _ tip: String, _ change: @escaping @MainActor (SearchFilterBar) -> DocChange) {
            out.append(ChipSpec(key: key, text: text, tip: tip, inMore: indexed, remove: { [weak self] in
                guard let self else { return }
                self.onDocChange?(change(self))
            }))
        }
        if filters.kind != .any {
            file("kind", filters.kind.title, "File type: \(filters.kind.title)", inMore: false) { $0.kind = .any }
        }
        if filters.date != .any {
            file("date", "Modified: \(filters.date.title)", "Modified: \(filters.date.title)", inMore: false) { $0.date = .any }
        }
        if !indexed && filters.size != .any {
            file("size", filters.size.title, "Size: \(filters.size.title)", inMore: false) { $0.size = .any }
        }
        if indexed && filters.method != .hybrid {
            file("method", filters.method.title, "Search method: \(filters.method.title)", inMore: true) { $0.method = .hybrid }
        }
        if !filters.includeTerms.isEmpty {
            let t = filters.includeTerms.joined(separator: ", ")
            file("include", "Include: \(t)", "Include: \(t)", inMore: true) { $0.include = "" }
        }
        if !filters.excludeTerms.isEmpty {
            let t = filters.excludeTerms.joined(separator: ", ")
            file("exclude", "Exclude: \(t)", "Exclude: \(t)", inMore: true) { $0.exclude = "" }
        }
        for t in docs.types {
            doc("type:\(t.key)", t.title, "Document type: \(t.title)") { .types($0.docs.types.filter { $0 != t }) }
        }
        if docs.amount != .any {
            let text: String
            switch docs.amount {
            case .under100: text = "< 100"
            case .over500: text = "> 500"
            case .custom(let lo, let hi):
                switch (lo, hi) {
                case let (l?, nil): text = "\u{2265} \(AmountChoice.text(l))"
                case let (nil, h?): text = "\u{2264} \(AmountChoice.text(h))"
                default: text = docs.amount.title
                }
            default: text = docs.amount.title
            }
            doc("amount", text, "Amount: \(docs.amount.title)") { _ in .amount(.any) }
        }
        if let c = docs.currency {
            doc("currency", c, "Currency: \(c)") { _ in .currency(nil) }
        }
        if !docs.party.isEmpty {
            doc("party", "Party: \(docs.party)", "Party: \(docs.party)") { _ in .party("") }
        }
        if docs.docdate != .any {
            doc("docdate", "Document date: \(docs.docdate.title)", "Document date: \(docs.docdate.title)") { _ in .docdate(.any) }
        }
        return out
    }

    /// A chip of a file filter was removed: the same path as its pop-up. The controls show the new filters, then the
    /// controller hears of the change (onChange) and runs the search again.
    private func removeFileFilter(_ clear: (inout SearchFilters) -> Void) {
        clear(&filters)
        syncControls()
        emit()
    }

    /// Rebuilds the chips when what they say has changed; a chip that still says the same is kept.
    private func updateChips() {
        let specs = chipSpecs()
        chipsInMore = specs.filter { $0.inMore }.count
        let keys = specs.map { "\($0.key)|\($0.text)" }
        guard keys != chipKeys else { return }
        chipKeys = keys
        chips = specs.map { FilterChipView(text: $0.text, tip: $0.tip, remove: $0.remove) }
    }

    /// "More filters (n)": n is the number of filters set inside the group. The chevron shows whether the group is open.
    private func updateMoreButton() {
        moreButton.title = chipsInMore > 0 ? "More filters (\(chipsInMore))" : "More filters"
        let symbol = moreOpen ? "chevron.down" : "chevron.right"
        moreButton.image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil)?.withSymbolConfiguration(MSEStyle.symbolConfig)
        moreButton.setAccessibilityExpanded(moreOpen)
    }

    @objc private func moreToggled(_ sender: Any?) {
        moreOpen.toggle()
        updateMoreButton()
        reflow()
    }

    /// Everything that depends on the filters: the chips, the button, Clear and the lines.
    private func refresh() {
        updateChips()
        updateMoreButton()
        reflow()
    }

    // MARK: lines

    /// One logical line of controls. A line that is wider than the bar wraps onto further rows of its own.
    private struct Line {
        let key: String
        let items: [NSView]
        let spacing: CGFloat
    }

    /// The lines in order. Membership, not isHidden, decides what is on screen: a control that is not listed is not in the bar.
    private func lines() -> [Line] {
        var out: [Line] = []
        var scopeRow: [NSView] = [scopeButton, namesCheck, contentsCheck]
        if mode == .indexed { scopeRow += [viewSeg, saveButton] }
        out.append(Line(key: "a", items: scopeRow, spacing: 10))
        // More filters sits before Clear, so it keeps its place when Clear appears with the first filter
        var filterRow: [NSView] = [typePop, modifiedGroup, mode == .mac ? sizePop : sortPop, moreButton]
        if clearVisible { filterRow.append(clearButton) }
        out.append(Line(key: "b", items: filterRow, spacing: 10))
        if !chips.isEmpty { out.append(Line(key: "c", items: chips, spacing: 8)) }
        if moreOpen {
            if mode == .indexed {
                out.append(Line(key: "m", items: [methodSeg], spacing: 10))
                out.append(Line(key: "d", items: [docTypePop, amountPop, currencyPop, partyField, docDateGroup], spacing: 10))
            }
            out.append(Line(key: "g", items: [includeGroup, excludeGroup], spacing: 10))
        }
        return out
    }

    /// Greedy line breaking: items fill a row until the next one no longer fits. A row has at least one item.
    private func wrap(_ items: [NSView], width: CGFloat, spacing: CGFloat) -> [[NSView]] {
        var rows: [[NSView]] = [[]]
        var used: CGFloat = 0
        for v in items {
            // the stack lays controls out by their alignment rects: a pop-up's frame is wider than the pill it draws, so the
            // frame's width (fittingSize) would count the shadow margins and wrap a row that fits
            let size = v.fittingSize
            let w = v.alignmentRect(forFrame: NSRect(origin: .zero, size: size)).width
            if rows[rows.count - 1].isEmpty {
                rows[rows.count - 1].append(v)
                used = w
            } else if used + spacing + w <= width {
                rows[rows.count - 1].append(v)
                used += spacing + w
            } else {
                rows.append([v])
                used = w
            }
        }
        return rows
    }

    private func rowStack(_ key: String, spacing: CGFloat) -> NSStackView {
        if let r = rowStacks[key] { return r }
        let r = NSStackView()
        r.orientation = .horizontal
        r.alignment = .centerY
        r.spacing = spacing
        // a row takes the width of its controls: it is not stretched to the bar's width, where the spare width spread
        // its controls differently from one render to the next
        r.setHuggingPriority(.required, for: .horizontal)
        rowStacks[key] = r
        return r
    }

    /// Lays the lines out in `width`. Only a row whose controls changed gets setViews, and the rows themselves stay in the bar
    /// (a missing one is inserted), so a field that is being typed in is not detached while a chip appears next to it.
    private func applyLayout(width: CGFloat) {
        var rows: [(key: String, items: [NSView], spacing: CGFloat)] = []
        for line in lines() {
            for (i, items) in wrap(line.items, width: width, spacing: line.spacing).enumerated() {
                rows.append((key: "\(line.key)\(i)", items: items, spacing: line.spacing))
            }
        }
        let order = rows.map { $0.key }
        for r in rows {
            let stack = rowStack(r.key, spacing: r.spacing)
            let ids = r.items.map { ObjectIdentifier($0) }
            if appliedRows[r.key] != ids {
                stack.setViews(r.items, in: .leading)
                appliedRows[r.key] = ids
            }
        }
        for key in appliedOrder where !order.contains(key) {
            guard let stack = rowStacks[key] else { continue }
            stack.setViews([], in: .leading)
            appliedRows[key] = nil
            removeArrangedSubview(stack)
            stack.removeFromSuperview()
        }
        for (i, key) in order.enumerated() {
            guard let stack = rowStacks[key], !arrangedSubviews.contains(stack) else { continue }
            insertArrangedSubview(stack, at: min(i, arrangedSubviews.count))
        }
        appliedOrder = order
    }

    /// Lays the lines out in the width the owning header offers.
    private func reflow() {
        var width = SearchFilterBar.seedWidth
        if let host = superview?.superview, host.bounds.width > 200 { width = host.bounds.width - 24 }
        applyLayout(width: width)
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

    /// The layout follows the width the owning header gives the bar at every layout pass. The header is not in the
    /// window yet when the bar is first added, so the frame observer above can be missed, and a reflow that ran too
    /// early left the controls on two rows. Deciding in layout makes the arrangement follow the final width.
    override func layout() {
        super.layout()
        reflow()
    }

    /// The scope button: its title and symbol, what it stands for, and whether the scope comes from a shortcut typed in
    /// the search field ("work: ..."), which shows in the accent colour.
    func setScope(title: String, symbol: String, tooltip: String, fromQuery: Bool) {
        scopeButton.title = title + (fromQuery ? " (typed)" : "") + "  \u{25BE}"
        scopeButton.image = NSImage(systemSymbolName: symbol, accessibilityDescription: title)?.withSymbolConfiguration(MSEStyle.symbolConfig)
        scopeButton.contentTintColor = fromQuery ? .controlAccentColor : nil
        scopeButton.toolTip = tooltip + (fromQuery
            ? "\nSet by the shortcut in the search text. Choosing here replaces it."
            : "\nWhere to search (\u{2318}\u{2325}F). Typing work: before your words does the same.")
    }

    /// Shows the two boxes. In the Whole Mac scope file names are always searched and contents are not available.
    func setSources(names: Bool, contents: Bool, macScope: Bool, namesOff: Bool = false) {
        namesCheck.state = (macScope || names) && !namesOff ? .on : .off
        contentsCheck.state = (!macScope && contents) ? .on : .off
        namesCheck.isEnabled = !macScope && !namesOff
        contentsCheck.isEnabled = !macScope
        namesCheck.toolTip = macScope
            ? "The whole Mac is searched by file name."
            : namesOff
                ? "Not searched with document filters: a file name has no type, amount or date. Clear the document filters to search names again."
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

    /// Mac scope shows type, Modified, size and, behind More filters, the globs; Indexed scope shows type, Modified, sort and,
    /// behind More filters, the method, the document filters and the globs.
    func setMode(_ m: Mode) {
        mode = m
        syncControls()
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
        refresh()
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
        refresh()
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
        if let field = obj.object as? NSTextField, field === partyField {
            partyDebounce?.cancel()
            let text = partyField.stringValue
            partyDebounce = Task { @MainActor [weak self] in
                try? await Task.sleep(nanoseconds: 350_000_000)
                guard let self, !Task.isCancelled else { return }
                self.onDocChange?(.party(text))
            }
            return
        }
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

/// The chip of one filter that is set: its text and an x. Only the x removes the filter (the remove closure the bar gave it).
/// The pill is drawn, not layered, so its colours follow the appearance (light, dark) every time it is drawn.
@MainActor
final class FilterChipView: NSStackView {
    private let onRemove: () -> Void
    private let label: NSTextField

    init(text: String, tip: String, remove: @escaping () -> Void) {
        onRemove = remove
        label = NSTextField(labelWithString: text)
        super.init(frame: .zero)
        orientation = .horizontal
        alignment = .centerY
        spacing = 2
        edgeInsets = NSEdgeInsets(top: 0, left: 10, bottom: 0, right: 3)
        translatesAutoresizingMaskIntoConstraints = false
        setHuggingPriority(.required, for: .horizontal)
        toolTip = tip
        label.font = .systemFont(ofSize: 12)
        label.textColor = .labelColor
        label.lineBreakMode = .byTruncatingTail
        label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        let x = NSButton(title: "", target: self, action: #selector(removePressed))
        x.isBordered = false
        x.image = NSImage(systemSymbolName: "xmark", accessibilityDescription: nil)?.withSymbolConfiguration(MSEStyle.symbolConfig)
        x.imagePosition = .imageOnly
        x.contentTintColor = MSEStyle.secondary
        x.toolTip = "Remove this filter"
        x.setAccessibilityLabel("Remove filter \(text)")
        x.translatesAutoresizingMaskIntoConstraints = false
        x.widthAnchor.constraint(equalToConstant: 22).isActive = true
        setViews([label, x], in: .leading)
        heightAnchor.constraint(equalToConstant: 22).isActive = true
        widthAnchor.constraint(lessThanOrEqualToConstant: 260).isActive = true
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override func draw(_ dirtyRect: NSRect) {
        let path = NSBezierPath(roundedRect: bounds.insetBy(dx: 0.5, dy: 0.5), xRadius: bounds.height / 2, yRadius: bounds.height / 2)
        NSColor.controlAccentColor.withAlphaComponent(0.14).setFill()
        path.fill()
        NSColor.controlAccentColor.withAlphaComponent(0.5).setStroke()
        path.lineWidth = 1
        path.stroke()
        super.draw(dirtyRect)
    }

    @objc private func removePressed() {
        withExtendedLifetime(self) { onRemove() }
    }
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

/// The custom amount sheet: a minimum and a maximum, either may be left empty. Apply sends (min, max), nil for an empty
/// field; Cancel sends nothing. A value that is not a number is read as empty.
@MainActor
final class AmountRangeSheet: NSObject {
    private let panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 340, height: 150),
                                styleMask: [.titled], backing: .buffered, defer: false)
    private let minField = NSTextField()
    private let maxField = NSTextField()
    private let onApply: (Double?, Double?) -> Void
    private let onCancel: () -> Void

    init(lo: Double?, hi: Double?, apply: @escaping (Double?, Double?) -> Void, cancel: @escaping () -> Void) {
        onApply = apply
        onCancel = cancel
        super.init()
        for (field, value, placeholder) in [(minField, lo, "No minimum"), (maxField, hi, "No maximum")] {
            field.placeholderString = placeholder
            field.stringValue = value.map { AmountChoice.text($0) } ?? ""
            field.controlSize = .regular
            field.translatesAutoresizingMaskIntoConstraints = false
            field.widthAnchor.constraint(equalToConstant: 160).isActive = true
            field.setAccessibilityLabel(placeholder == "No minimum" ? "Minimum amount" : "Maximum amount")
        }
        let title = NSTextField(labelWithString: "Custom amount")
        title.font = .systemFont(ofSize: 13, weight: .semibold)
        let minRow = NSStackView(views: [NSTextField(labelWithString: "Minimum"), minField])
        let maxRow = NSStackView(views: [NSTextField(labelWithString: "Maximum"), maxField])
        for r in [minRow, maxRow] {
            r.orientation = .horizontal
            r.alignment = .centerY
            r.spacing = 12
            r.arrangedSubviews.first?.widthAnchor.constraint(equalToConstant: 70).isActive = true
        }
        let cancelButton = NSButton(title: "Cancel", target: self, action: #selector(cancelPressed))
        cancelButton.keyEquivalent = "\u{1b}"
        let applyButton = NSButton(title: "Apply", target: self, action: #selector(applyPressed))
        applyButton.keyEquivalent = "\r"
        applyButton.bezelStyle = .rounded
        let buttons = NSStackView(views: [cancelButton, applyButton])
        buttons.orientation = .horizontal
        buttons.spacing = 10
        let stack = NSStackView(views: [title, minRow, maxRow, buttons])
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

    /// The panel, for the snapshot (composited into the main window's picture).
    var sheetPanel: NSPanel { panel }

    private func number(_ field: NSTextField) -> Double? {
        let v = field.stringValue.trimmingCharacters(in: .whitespaces).replacingOccurrences(of: ",", with: "")
        return v.isEmpty ? nil : Double(v)
    }

    @objc private func applyPressed() {
        var lo = number(minField), hi = number(maxField)
        if let a = lo, let b = hi, a > b { swap(&lo, &hi) }
        panel.sheetParent?.endSheet(panel)
        onApply(lo, hi)
    }

    @objc private func cancelPressed() {
        panel.sheetParent?.endSheet(panel)
        onCancel()
    }
}
