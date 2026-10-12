// Quick Search's content: the search bar (glyph, field, busy spinner, scope), the sectioned list, and the footer with the
// counts and the keys that the selection takes. Laid out by hand, so the panel's height is exact: bar 60 pt, a 1 pt rule,
// the list (as tall as its rows, up to the screen's room), a 1 pt rule and a 28 pt footer.

import Cocoa

@MainActor
final class QuickSearchView: NSView, NSTextFieldDelegate {
    static let barHeight: CGFloat = 60
    static let footerHeight: CGFloat = 28
    /// The bar, the rule under it, the rule over the footer and the footer: everything but the list.
    static let chromeHeight: CGFloat = 60 + 1 + 1 + 28

    let model: QuickSearchModel
    let field = NSTextField()
    private let glyph = NSImageView()
    private let scopeControl = NSSegmentedControl(labels: ["Whole Mac", "Indexed"], trackingMode: .selectOne, target: nil, action: nil)
    private let spinner = NSProgressIndicator()
    private let barRule = NSBox()
    private let footerRule = NSBox()
    private let table = QSTableView()
    private let scroll = NSScrollView()
    private let list = QSListSource()
    private let footerLeft = qsLabel(size: 11)
    private let footerKeys = qsLabel(size: 11)
    private var boundsObserver: NSObjectProtocol?

    init(model: QuickSearchModel) {
        self.model = model
        super.init(frame: NSRect(x: 0, y: 0, width: 720, height: 400))
        build()
    }

    required init?(coder: NSCoder) { fatalError("QuickSearchView is built in code") }

    deinit {
        if let boundsObserver { NotificationCenter.default.removeObserver(boundsObserver) }
    }

    private func build() {
        glyph.image = NSImage(systemSymbolName: "magnifyingglass", accessibilityDescription: nil)
        glyph.contentTintColor = .secondaryLabelColor

        field.font = .systemFont(ofSize: 22, weight: .regular)
        field.placeholderString = "Search files and folders"
        field.isBordered = false
        field.isBezeled = false
        field.drawsBackground = false
        field.focusRingType = .none
        field.usesSingleLineMode = true
        field.lineBreakMode = .byTruncatingTail
        field.cell?.isScrollable = true
        field.delegate = self

        scopeControl.font = .systemFont(ofSize: 11)
        scopeControl.target = self
        scopeControl.action = #selector(scopeChanged(_:))
        scopeControl.selectedSegment = 0

        spinner.style = .spinning
        spinner.controlSize = .small
        spinner.isDisplayedWhenStopped = false

        barRule.boxType = .separator
        footerRule.boxType = .separator

        let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("qs.column"))
        column.resizingMask = .autoresizingMask
        table.addTableColumn(column)
        table.headerView = nil
        table.rowSizeStyle = .custom
        table.intercellSpacing = NSSize(width: 0, height: 0)
        table.backgroundColor = .clear
        table.selectionHighlightStyle = .none
        table.allowsEmptySelection = true
        table.usesAlternatingRowBackgroundColors = false
        table.columnAutoresizingStyle = .lastColumnOnlyAutoresizingStyle
        table.dataSource = list
        table.delegate = list
        table.onClickRow = { [weak self] row, clicks in self?.clicked(row: row, clicks: clicks) }
        list.onRetry = { [weak self] in self?.model.retry() }

        scroll.documentView = table
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.scrollerStyle = .overlay
        scroll.drawsBackground = false
        scroll.borderType = .noBorder
        scroll.contentView.postsBoundsChangedNotifications = true
        boundsObserver = NotificationCenter.default.addObserver(forName: NSView.boundsDidChangeNotification,
                                                                object: scroll.contentView, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.checkPaging() }
        }

        footerLeft.textColor = .secondaryLabelColor
        footerKeys.textColor = .secondaryLabelColor
        footerKeys.alignment = .right

        for v in [glyph, field, scopeControl, spinner, barRule, footerRule, scroll, footerLeft, footerKeys] as [NSView] {
            addSubview(v)
        }
    }

    // MARK: layout

    override func layout() {
        super.layout()
        let w = bounds.width
        let h = bounds.height
        let top = h - Self.barHeight
        glyph.frame = NSRect(x: 18, y: top + (Self.barHeight - 20) / 2, width: 20, height: 20)
        scopeControl.frame = NSRect(x: w - 186 - 14, y: top + (Self.barHeight - 24) / 2, width: 186, height: 24)
        spinner.frame = NSRect(x: scopeControl.frame.minX - 24, y: top + (Self.barHeight - 16) / 2, width: 16, height: 16)
        field.frame = NSRect(x: 50, y: top + (Self.barHeight - 30) / 2, width: max(60, spinner.frame.minX - 58), height: 30)
        barRule.frame = NSRect(x: 0, y: top - 1, width: w, height: 1)
        footerRule.frame = NSRect(x: 0, y: Self.footerHeight, width: w, height: 1)
        footerLeft.frame = NSRect(x: 14, y: 7, width: w * 0.38, height: 14)
        footerKeys.frame = NSRect(x: w * 0.38 + 8, y: 7, width: w * 0.62 - 22, height: 14)
        scroll.frame = NSRect(x: 0, y: Self.footerHeight + 1, width: w, height: max(0, top - 1 - Self.footerHeight - 1))
        table.setFrameSize(NSSize(width: scroll.contentSize.width, height: table.frame.height))
    }

    /// The height the panel needs for its rows, never more than `maxHeight`: the chrome plus the list.
    func desiredHeight(maxHeight: CGFloat) -> CGFloat {
        let rows = QSListSource.totalHeight(model.rows) + 8
        let room = max(120, maxHeight - Self.chromeHeight)
        return Self.chromeHeight + min(rows, room)
    }

    /// Builds the list and the bar from the model. Called by the window after each change.
    func update() {
        if field.stringValue != model.query { field.stringValue = model.query }
        scopeControl.selectedSegment = model.scope == .mac ? 0 : 1
        if model.isBusy { spinner.startAnimation(nil) } else { spinner.stopAnimation(nil) }
        footerLeft.stringValue = model.footerLine
        footerKeys.stringValue = keyHints()
        list.rows = model.rows
        list.selectedID = model.selectedID
        table.reloadData()
        if model.scrollRequested, let row = list.index(ofID: model.selectedID ?? "") {
            table.scrollRowToVisible(row)
        }
        model.consumeScroll()
        layoutSubtreeIfNeeded()
        table.displayIfNeeded()
        model.didPaint()
    }

    private func keyHints() -> String {
        switch model.selectedRow {
        case .file(let f, _)?:
            return f.isDir ? "↵ Open   ⌘↵ Reveal   ⌘O MySearch" : "↵ Open   ⌘↵ Reveal   ␣ Quick Look   ⌘C Copy path   ⌘O MySearch"
        case .content?, .disk?:
            return "↵ Open   ⌘↵ Reveal   ␣ Quick Look   ⌘C Copy path   ⌘O MySearch"
        case .recentFile?:
            return "↵ Open   ⌘↵ Reveal   ␣ Quick Look"
        case .recentQuery?:
            return "↵ Search again   ⌘O MySearch"
        case .status(.offline)?, .status(.unauthorized)?, .status(.failed(_))?:
            return "↵ or ⌘R Retry"
        default:
            return "⇥ Sections   ⌃1 ⌃2 ⌃3 Jump   ⌘1 Whole Mac   ⌘2 Indexed"
        }
    }

    // MARK: the field

    /// Sets the field's text without the change counting as typing (the model is told by the caller).
    func setFieldText(_ text: String) {
        field.stringValue = text
        (field.currentEditor() as? NSTextView)?.selectedRange = NSRange(location: text.utf16.count, length: 0)
    }

    var fieldHasMarkedText: Bool { (field.currentEditor() as? NSTextView)?.hasMarkedText() ?? false }

    var fieldHasSelectedText: Bool { ((field.currentEditor() as? NSTextView)?.selectedRange().length ?? 0) > 0 }

    func controlTextDidChange(_ obj: Notification) {
        model.setQuery(field.stringValue, typed: true)
    }

    // MARK: actions

    @objc private func scopeChanged(_ sender: NSSegmentedControl) {
        model.setScope(sender.selectedSegment == 1 ? .indexed : .mac)
    }

    private func clicked(row: Int, clicks: Int) {
        guard let id = list.id(at: row) else { return }
        model.select(id: id)
        if clicks >= 2 { model.activateSelected(reveal: false) }
    }

    /// The list was scrolled: when its last visible row is near the end, the next page of names is asked for.
    private func checkPaging() {
        guard table.numberOfRows > 0 else { return }
        let visible = table.rows(in: table.visibleRect)
        model.loadMoreIfNeeded(lastVisibleRow: visible.location + visible.length - 1)
    }
}
