// The native text editor used by both the code editor and the Markdown editor: an NSTextView (TextKit 1) in a
// scroll view, with an optional line-number gutter, a monospaced font, syntax colours (SyntaxTokenizer, applied as
// temporary attributes so they never touch the text or the undo stack), the system find bar (NSTextFinder, so find
// and replace) and auto-indent. No web view, no third-party code.

import Cocoa

// MARK: - Text view

final class CodeTextView: NSTextView {
    /// What Tab inserts: a tab character, or spaces to match the file.
    var indentUnit = "    "
    var usesTabIndent = false

    private func finder(_ action: NSTextFinder.Action) {
        let item = NSMenuItem()
        item.tag = action.rawValue
        performTextFinderAction(item)
    }

    /// ⌘F and friends are claimed here, before the app's main menu (where ⌘F focuses the main window's search).
    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        guard event.type == .keyDown, window?.firstResponder === self else { return super.performKeyEquivalent(with: event) }
        let flags = event.modifierFlags.intersection([.command, .shift, .option, .control])
        let key = event.charactersIgnoringModifiers?.lowercased() ?? ""
        if flags == .command {
            switch key {
            case "f": finder(.showFindInterface); return true
            case "g": finder(.nextMatch); return true
            case "e": finder(.setSearchString); return true
            default: break
            }
        } else if flags == [.command, .shift], key == "g" {
            finder(.previousMatch); return true
        } else if flags == [.command, .option], key == "f" {
            finder(.showReplaceInterface); return true
        }
        return super.performKeyEquivalent(with: event)
    }

    override func insertTab(_ sender: Any?) {
        if usesTabIndent { super.insertTab(sender); return }
        insertText(indentUnit, replacementRange: selectedRange())
    }

    /// New line keeps the indentation of the line above, and goes one level deeper after an opening bracket.
    override func insertNewline(_ sender: Any?) {
        let ns = string as NSString
        let sel = selectedRange()
        let lineRange = ns.lineRange(for: NSRange(location: sel.location, length: 0))
        var indent = ""
        var i = lineRange.location
        while i < sel.location, i < ns.length {
            let c = ns.character(at: i)
            if c == 32 || c == 9 { indent.append(Character(UnicodeScalar(c)!)) } else { break }
            i += 1
        }
        var extra = ""
        if sel.location > 0 {
            let prev = ns.character(at: sel.location - 1)
            if prev == 123 || prev == 91 || prev == 40 { extra = usesTabIndent ? "\t" : indentUnit }   // { [ (
        }
        super.insertNewline(sender)
        if !indent.isEmpty || !extra.isEmpty { insertText(indent + extra, replacementRange: selectedRange()) }
    }
}

// MARK: - Line numbers

final class LineNumberRuler: NSRulerView {
    weak var textView: NSTextView?
    private var lineStarts: [Int] = [0]
    private var dirty = true

    init(scrollView: NSScrollView, textView: NSTextView) {
        self.textView = textView
        super.init(scrollView: scrollView, orientation: .verticalRuler)
        clientView = textView
        ruleThickness = 44
        scrollView.contentView.postsBoundsChangedNotifications = true
        NotificationCenter.default.addObserver(self, selector: #selector(redraw), name: NSView.boundsDidChangeNotification,
                                               object: scrollView.contentView)
    }

    required init(coder: NSCoder) { fatalError("init(coder:) is not used") }

    deinit { NotificationCenter.default.removeObserver(self) }

    @objc private func redraw() { needsDisplay = true }

    /// Call after the text changed.
    func invalidate() {
        dirty = true
        needsDisplay = true
    }

    private func rebuildIfNeeded() {
        guard dirty, let tv = textView else { return }
        dirty = false
        var starts = [0]
        var idx = 0
        for c in tv.string.utf16 {
            idx += 1
            if c == 10 { starts.append(idx) }
        }
        lineStarts = starts
        let digits = String(starts.count).count
        let thickness = max(40, CGFloat(digits) * 8 + 24)
        if abs(thickness - ruleThickness) > 0.5 { ruleThickness = thickness }
    }

    /// Zero-based line index of a character offset.
    func lineIndex(forCharacter loc: Int) -> Int {
        rebuildIfNeeded()
        var lo = 0, hi = lineStarts.count - 1
        while lo < hi {
            let mid = (lo + hi + 1) / 2
            if lineStarts[mid] <= loc { lo = mid } else { hi = mid - 1 }
        }
        return lo
    }

    func lineAndColumn(forCharacter loc: Int) -> (line: Int, column: Int) {
        let l = lineIndex(forCharacter: loc)
        return (l + 1, loc - lineStarts[l] + 1)
    }

    var lineCount: Int { rebuildIfNeeded(); return lineStarts.count }

    override func drawHashMarksAndLabels(in rect: NSRect) {
        guard let tv = textView, let lm = tv.layoutManager, let tc = tv.textContainer else { return }
        rebuildIfNeeded()
        NSColor.controlBackgroundColor.setFill()
        bounds.fill()
        NSColor.separatorColor.setFill()
        NSRect(x: bounds.maxX - 1, y: bounds.minY, width: 1, height: bounds.height).fill()

        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.monospacedDigitSystemFont(ofSize: 10.5, weight: .regular),
            .foregroundColor: NSColor.secondaryLabelColor,
        ]
        let ns = tv.string as NSString
        let origin = tv.textContainerOrigin
        func draw(_ number: Int, lineRect: NSRect) {
            let r = convert(NSRect(x: 0, y: lineRect.minY + origin.y, width: 1, height: lineRect.height), from: tv)
            let s = NSAttributedString(string: String(number), attributes: attrs)
            let size = s.size()
            s.draw(at: NSPoint(x: bounds.width - 8 - size.width, y: r.minY + (r.height - size.height) / 2))
        }

        if ns.length > 0 {
            let glyphRange = lm.glyphRange(forBoundingRect: tv.visibleRect, in: tc)
            lm.enumerateLineFragments(forGlyphRange: glyphRange) { [self] fragRect, _, _, fragGlyphs, _ in
                let loc = lm.characterIndexForGlyph(at: fragGlyphs.location)
                if loc == 0 || ns.character(at: loc - 1) == 10 {
                    draw(lineIndex(forCharacter: loc) + 1, lineRect: fragRect)
                }
            }
        }
        let extra = lm.extraLineFragmentRect
        if !extra.isEmpty && (ns.length == 0 || ns.character(at: ns.length - 1) == 10) {
            draw(lineStarts.count, lineRect: extra)
        }
    }
}

// MARK: - Pane

@MainActor
final class CodeEditorPane: NSObject, NSTextViewDelegate {
    let scrollView = NSScrollView()
    let textView: CodeTextView
    private(set) var ruler: LineNumberRuler?
    var language: SyntaxLanguage { didSet { rehighlight() } }
    var onTextChange: (() -> Void)?
    var onSelectionChange: (() -> Void)?

    /// Highlighting is skipped above this many UTF-16 units; the editor stays responsive on big files.
    static let highlightLimit = 500_000

    private var loading = false
    private var highlightWork: DispatchWorkItem?
    static let font = NSFont.monospacedSystemFont(ofSize: 12.5, weight: .regular)

    init(language: SyntaxLanguage, wrap: Bool, lineNumbers: Bool) {
        self.language = language
        textView = CodeTextView(usingTextLayoutManager: false)
        super.init()

        textView.frame = NSRect(x: 0, y: 0, width: 600, height: 400)
        textView.minSize = NSSize(width: 0, height: 0)
        textView.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        textView.isVerticallyResizable = true
        textView.isHorizontallyResizable = !wrap
        textView.autoresizingMask = wrap ? [.width] : []
        if let tc = textView.textContainer {
            tc.widthTracksTextView = wrap
            tc.containerSize = NSSize(width: wrap ? 600 : CGFloat.greatestFiniteMagnitude,
                                      height: CGFloat.greatestFiniteMagnitude)
        }
        textView.delegate = self
        textView.isEditable = true
        textView.isSelectable = true
        textView.isRichText = false
        textView.importsGraphics = false
        textView.allowsUndo = true
        textView.isAutomaticQuoteSubstitutionEnabled = false
        textView.isAutomaticDashSubstitutionEnabled = false
        textView.isAutomaticSpellingCorrectionEnabled = false
        textView.isAutomaticTextReplacementEnabled = false
        textView.isAutomaticLinkDetectionEnabled = false
        textView.isAutomaticDataDetectionEnabled = false
        textView.isContinuousSpellCheckingEnabled = false
        textView.isGrammarCheckingEnabled = false
        textView.smartInsertDeleteEnabled = false
        textView.usesFindBar = true
        textView.isIncrementalSearchingEnabled = true
        textView.usesFontPanel = false
        textView.font = Self.font
        textView.textColor = .textColor
        textView.backgroundColor = .textBackgroundColor
        textView.insertionPointColor = .controlAccentColor
        textView.textContainerInset = NSSize(width: 6, height: 8)
        let para = NSMutableParagraphStyle()
        let space = " ".size(withAttributes: [.font: Self.font]).width
        para.defaultTabInterval = space * 4
        para.tabStops = []
        textView.defaultParagraphStyle = para
        textView.typingAttributes = [.font: Self.font, .foregroundColor: NSColor.textColor, .paragraphStyle: para]

        scrollView.documentView = textView
        scrollView.hasVerticalScroller = true
        scrollView.hasHorizontalScroller = !wrap
        scrollView.autohidesScrollers = true
        scrollView.borderType = .noBorder
        scrollView.drawsBackground = true
        scrollView.backgroundColor = .textBackgroundColor
        scrollView.findBarPosition = .aboveContent
        scrollView.postsFrameChangedNotifications = true
        NotificationCenter.default.addObserver(self, selector: #selector(scrollResized), name: NSView.frameDidChangeNotification,
                                               object: scrollView)
        if lineNumbers {
            let r = LineNumberRuler(scrollView: scrollView, textView: textView)
            scrollView.verticalRulerView = r
            scrollView.hasVerticalRuler = true
            scrollView.rulersVisible = true
            ruler = r
        }
    }

    /// The text view always fills the visible area, so a click anywhere below the last line puts the cursor there.
    @objc private func scrollResized() {
        let size = scrollView.contentSize
        guard size.width > 0, size.height > 0 else { return }
        textView.minSize = NSSize(width: textView.isHorizontallyResizable ? size.width : 0, height: size.height)
    }

    var text: String { textView.string }

    /// Puts a file's text into the editor without counting it as an edit.
    func setText(_ s: String) {
        loading = true
        textView.string = s
        textView.undoManager?.removeAllActions()
        loading = false
        // indentation style of the file
        var tabs = 0, spaces = 0
        var narrow = 0
        s.enumerateSubstrings(in: s.startIndex..., options: [.byLines, .substringNotRequired]) { _, range, _, stop in
            let line = s[range]
            if line.hasPrefix("\t") { tabs += 1 } else if line.hasPrefix("  ") {
                spaces += 1
                if line.hasPrefix("  ") && !line.hasPrefix("   ") { narrow += 1 }
            }
            if tabs + spaces > 400 { stop = true }
        }
        textView.usesTabIndent = tabs > spaces
        textView.indentUnit = (narrow > 0 && narrow * 2 >= spaces) ? "  " : "    "
        ruler?.invalidate()
        rehighlight()
        textView.setSelectedRange(NSRange(location: 0, length: 0))
        textView.scrollRangeToVisible(NSRange(location: 0, length: 0))
    }

    // MARK: delegate

    func textDidChange(_ notification: Notification) {
        guard !loading else { return }
        ruler?.invalidate()
        scheduleHighlight()
        onTextChange?()
    }

    func textViewDidChangeSelection(_ notification: Notification) { onSelectionChange?() }

    // MARK: highlighting

    private func scheduleHighlight() {
        highlightWork?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.rehighlight() }
        highlightWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.12, execute: work)
    }

    func rehighlight() {
        highlightWork?.cancel()
        guard let lm = textView.layoutManager else { return }
        let full = NSRange(location: 0, length: (textView.string as NSString).length)
        lm.removeTemporaryAttribute(.foregroundColor, forCharacterRange: full)
        guard language != .plain, full.length > 0, full.length <= Self.highlightLimit else { return }
        let tokens = SyntaxTokenizer.tokens(for: textView.string, language: language)
        for t in tokens where t.location + t.length <= full.length {
            lm.addTemporaryAttribute(.foregroundColor, value: SyntaxTheme.color(t.kind),
                                     forCharacterRange: NSRange(location: t.location, length: t.length))
        }
    }

    /// "Ln 12, Col 5" for the selection start.
    var cursorDescription: String {
        let loc = textView.selectedRange().location
        if let r = ruler {
            let lc = r.lineAndColumn(forCharacter: loc)
            return "Ln \(lc.line), Col \(lc.column)"
        }
        let ns = textView.string as NSString
        let upTo = ns.substring(to: min(loc, ns.length))
        let line = upTo.reduce(1) { $1 == "\n" ? $0 + 1 : $0 }
        return "Ln \(line)"
    }
}
