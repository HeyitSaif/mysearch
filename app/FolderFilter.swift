// The in-place filter of the folder views (list, icon and column). Press / (or ⌘⌥F) while a folder view has the
// keyboard: a field slides in above the view and the names that do not match leave. Matching ignores case and accents
// ("ü" finds "u"); every word typed must match; a word of five letters or more forgives one typo (one edit, or two
// neighbouring letters swapped) in any word of the name; the matched characters come out bold. Esc clears and closes
// the filter, ↓ or Return moves the keyboard into the list. The filter belongs to one view and one folder: it clears
// when the view opens another folder.

import Cocoa

/// One typed query and its matching rules.
struct FolderFilter {
    /// The query as typed, trimmed.
    let query: String
    /// The words of the query, folded for comparison.
    private let words: [[Character]]

    /// Nil when the text has no words: then there is no filter at all.
    init?(text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let folded = String(FolderFilter.fold(trimmed).map { $0.0 })
        let words = folded.split(whereSeparator: { $0.isWhitespace }).map { Array($0) }
        guard !words.isEmpty else { return nil }
        query = trimmed
        self.words = words
    }

    /// "12 of 340 items match 'inv'", the status line while the filter is on.
    func statusText(matching: Int, of total: Int) -> String {
        "\(matching) of \(total) items match '\(query)'"
    }

    /// Whether `name` matches every word of the query. The fast path: the whole name is folded in one call.
    func matches(_ name: String) -> Bool {
        let text = Array(name.folding(options: [.caseInsensitive, .diacriticInsensitive], locale: nil))
        return FolderFilter.spans(in: text, words: words) != nil
    }

    /// Whether `name` matches every word, and which characters of `name` (by Character index) to show in bold.
    func match(_ name: String) -> (matches: Bool, bold: IndexSet) {
        let folded = FolderFilter.fold(name)
        guard let spans = FolderFilter.spans(in: folded.map { $0.0 }, words: words) else { return (false, IndexSet()) }
        var bold = IndexSet()
        for span in spans {
            for position in span { bold.insert(folded[position].1) }
        }
        return (true, bold)
    }

    /// The folded spans that the words hit, or nil when one word does not match anywhere.
    private static func spans(in text: [Character], words queryWords: [[Character]]) -> [Range<Int>]? {
        let tokens = tokens(of: text)
        var spans: [Range<Int>] = []
        for word in queryWords {
            var hits = occurrences(of: word, in: text).map { $0..<($0 + word.count) }
            if hits.isEmpty, word.count >= 5,
               let token = tokens.first(where: { isOneTypo(Array(text[$0]), word) }) {
                hits = [token]
            }
            guard !hits.isEmpty else { return nil }
            spans += hits
        }
        return spans
    }

    /// The name as an attributed string, its matched characters in the bold face of `font`.
    func highlighted(_ name: String, font: NSFont?, alignment: NSTextAlignment = .left) -> NSAttributedString {
        let base = font ?? NSFont.systemFont(ofSize: NSFont.systemFontSize)
        let bold = NSFontManager.shared.convert(base, toHaveTrait: .boldFontMask)
        let paragraph = NSMutableParagraphStyle()
        paragraph.alignment = alignment
        paragraph.lineBreakMode = .byTruncatingMiddle
        let text = NSMutableAttributedString(string: name, attributes: [.font: base, .paragraphStyle: paragraph])
        let boldIndexes = match(name).bold
        var location = 0
        for (index, character) in name.enumerated() {
            let length = String(character).utf16.count
            if boldIndexes.contains(index) {
                text.addAttribute(.font, value: bold, range: NSRange(location: location, length: length))
            }
            location += length
        }
        return text
    }

    // MARK: matching helpers

    /// Each Character of `text` folded for comparison, with the index of the original Character it came from. Folding
    /// can grow a character (ß becomes ss), so one original index can stand for several folded characters.
    static func fold(_ text: String) -> [(Character, Int)] {
        var out: [(Character, Int)] = []
        for (index, character) in text.enumerated() {
            let foldedText = String(character).folding(options: [.caseInsensitive, .diacriticInsensitive], locale: nil)
            for folded in foldedText { out.append((folded, index)) }
        }
        return out
    }

    private static func isWordCharacter(_ character: Character) -> Bool {
        character.isLetter || character.isNumber
    }

    /// The runs of letters and digits, as ranges of folded positions.
    private static func tokens(of text: [Character]) -> [Range<Int>] {
        var out: [Range<Int>] = []
        var i = 0
        while i < text.count {
            guard isWordCharacter(text[i]) else {
                i += 1
                continue
            }
            let start = i
            while i < text.count && isWordCharacter(text[i]) { i += 1 }
            out.append(start..<i)
        }
        return out
    }

    /// The start positions of every occurrence of `word` in `text`.
    private static func occurrences(of word: [Character], in text: [Character]) -> [Int] {
        guard !word.isEmpty, word.count <= text.count else { return [] }
        var found: [Int] = []
        for start in 0...(text.count - word.count) {
            var k = 0
            while k < word.count && text[start + k] == word[k] { k += 1 }
            if k == word.count { found.append(start) }
        }
        return found
    }

    /// True when `token` and `word` are equal, or one edit apart (an insertion, a deletion or a substitution), or two
    /// neighbouring letters are swapped.
    static func isOneTypo(_ token: [Character], _ word: [Character]) -> Bool {
        let shorter = token.count <= word.count ? token : word
        let longer = token.count <= word.count ? word : token
        guard longer.count - shorter.count <= 1 else { return false }
        if token.count == word.count {
            var diffs: [Int] = []
            for i in 0..<token.count where token[i] != word[i] {
                diffs.append(i)
                if diffs.count > 2 { return false }
            }
            if diffs.count <= 1 { return true }
            return diffs.count == 2 && diffs[1] == diffs[0] + 1
                && token[diffs[0]] == word[diffs[1]] && token[diffs[1]] == word[diffs[0]]
        }
        // one character is extra in the longer word: skip it once
        var i = 0
        var j = 0
        var skipped = false
        while i < shorter.count && j < longer.count {
            if shorter[i] == longer[j] {
                i += 1
                j += 1
            } else {
                if skipped { return false }
                skipped = true
                j += 1
            }
        }
        return true
    }
}

/// The field that slides in above a folder view. It is the field's own delegate, so the view controllers never see its
/// keys: typing reports through `onChange`, Esc through `onClose`, ↓ and Return through `onMoveIntoList`.
@MainActor
final class FolderFilterBar: NSView, NSTextFieldDelegate {
    static let height: CGFloat = 30
    let field = NSTextField()
    /// Every change of the text.
    var onChange: ((String) -> Void)?
    /// Esc: the filter is cleared and the bar closes.
    var onClose: (() -> Void)?
    /// ↓ or Return: the keyboard moves into the list.
    var onMoveIntoList: (() -> Void)?
    private(set) var isOpen = false
    private var heightConstraint: NSLayoutConstraint!

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        layer?.masksToBounds = true
        field.translatesAutoresizingMaskIntoConstraints = false
        field.placeholderString = "Filter this folder"
        field.font = .systemFont(ofSize: 13)
        field.bezelStyle = .roundedBezel
        field.delegate = self
        let rule = NSBox()
        rule.boxType = .separator
        rule.translatesAutoresizingMaskIntoConstraints = false
        addSubview(field)
        addSubview(rule)
        heightConstraint = heightAnchor.constraint(equalToConstant: 0)
        NSLayoutConstraint.activate([
            heightConstraint,
            field.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 12),
            field.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -12),
            field.centerYAnchor.constraint(equalTo: centerYAnchor, constant: -1),
            rule.leadingAnchor.constraint(equalTo: leadingAnchor),
            rule.trailingAnchor.constraint(equalTo: trailingAnchor),
            rule.bottomAnchor.constraint(equalTo: bottomAnchor),
        ])
        isHidden = true
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    /// Slides the bar in and puts the caret in the field.
    func open() {
        isHidden = false
        if !isOpen {
            isOpen = true
            NSAnimationContext.runAnimationGroup { context in
                context.duration = 0.12
                self.heightConstraint.animator().constant = FolderFilterBar.height
            }
        }
        window?.makeFirstResponder(field)
    }

    /// Empties the field and slides the bar away. The caller reloads the view and moves the focus.
    func close() {
        if field.currentEditor() != nil { window?.makeFirstResponder(nil) }   // a hidden field must not keep the keyboard
        isOpen = false
        field.stringValue = ""
        heightConstraint.constant = 0
        isHidden = true
    }

    func controlTextDidChange(_ obj: Notification) {
        guard isOpen else { return }
        onChange?(field.stringValue)
    }

    func control(_ control: NSControl, textView: NSTextView, doCommandBy commandSelector: Selector) -> Bool {
        if commandSelector == #selector(NSResponder.cancelOperation(_:)) {
            onClose?()
            return true
        }
        if commandSelector == #selector(NSResponder.moveDown(_:)) || commandSelector == #selector(NSResponder.insertNewline(_:)) {
            onMoveIntoList?()
            return true
        }
        return false
    }
}

/// A folder view with the filter bar above it. The view fills the rest.
@MainActor
final class FolderFilterContainer: NSView {
    init(content: NSView, bar: FolderFilterBar) {
        super.init(frame: .zero)
        content.translatesAutoresizingMaskIntoConstraints = false
        bar.translatesAutoresizingMaskIntoConstraints = false
        addSubview(content)
        addSubview(bar)
        NSLayoutConstraint.activate([
            bar.topAnchor.constraint(equalTo: topAnchor),
            bar.leadingAnchor.constraint(equalTo: leadingAnchor),
            bar.trailingAnchor.constraint(equalTo: trailingAnchor),
            content.topAnchor.constraint(equalTo: bar.bottomAnchor),
            content.leadingAnchor.constraint(equalTo: leadingAnchor),
            content.trailingAnchor.constraint(equalTo: trailingAnchor),
            content.bottomAnchor.constraint(equalTo: bottomAnchor),
        ])
    }

    required init?(coder: NSCoder) { fatalError("not used") }
}
