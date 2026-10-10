// A small hand-written syntax highlighter: one scanner driven by a per-language spec, plus dedicated scanners for
// JSON, YAML/TOML/INI, Markdown, HTML/XML and CSS. It colours; it does not parse. It works on UTF-16 code units so the
// token ranges can be handed straight to NSLayoutManager. No third-party code.

import Cocoa

enum SyntaxKind {
    case keyword, string, comment, number, type, call, attribute, tag, heading, emphasis, link
}

struct SyntaxToken {
    var location: Int
    var length: Int
    var kind: SyntaxKind
}

enum SyntaxLanguage: CaseIterable {
    case plain, swift, python, javascript, typescript, json, yaml, toml, markdown, shell, html, css, sql, go, rust, java, kotlin, cfamily

    var displayName: String {
        switch self {
        case .plain: return "Plain Text"
        case .swift: return "Swift"
        case .python: return "Python"
        case .javascript: return "JavaScript"
        case .typescript: return "TypeScript"
        case .json: return "JSON"
        case .yaml: return "YAML"
        case .toml: return "TOML / INI"
        case .markdown: return "Markdown"
        case .shell: return "Shell"
        case .html: return "HTML / XML"
        case .css: return "CSS"
        case .sql: return "SQL"
        case .go: return "Go"
        case .rust: return "Rust"
        case .java: return "Java"
        case .kotlin: return "Kotlin"
        case .cfamily: return "C / C++ / Obj-C"
        }
    }

    static func forURL(_ url: URL) -> SyntaxLanguage {
        let ext = url.pathExtension.lowercased()
        switch ext {
        case "swift": return .swift
        case "py", "pyw": return .python
        case "js", "jsx", "mjs", "cjs": return .javascript
        case "ts", "tsx": return .typescript
        case "json", "json5", "jsonc": return .json
        case "yml", "yaml": return .yaml
        case "toml", "ini", "cfg", "conf", "properties", "env", "editorconfig": return .toml
        case "md", "markdown", "mdown", "mkd": return .markdown
        case "sh", "bash", "zsh", "fish": return .shell
        case "html", "htm", "xhtml", "xml", "svg", "plist", "vue", "svelte": return .html
        case "css", "scss", "sass", "less": return .css
        case "sql": return .sql
        case "go": return .go
        case "rs": return .rust
        case "java", "cs": return .java
        case "kt", "kts": return .kotlin
        case "c", "h", "cc", "cpp", "cxx", "hpp", "m", "mm": return .cfamily
        default: break
        }
        switch url.lastPathComponent.lowercased() {
        case "makefile", "dockerfile", ".zshrc", ".bashrc", ".bash_profile", ".profile": return .shell
        case ".gitignore", ".gitattributes", ".npmrc": return .toml
        default: return .plain
        }
    }
}

// MARK: - Generic scanner spec

private struct ScanSpec {
    var lineComments: [String] = []
    var blockComment: (String, String)? = nil
    var quotes: [Character] = ["\"", "'"]
    /// Quote characters whose strings may run over several lines (template literals, Go raw strings).
    var multilineQuotes: [Character] = []
    var tripleQuotes = false
    var keywords: Set<String> = []
    var caseInsensitive = false
    var typeHeuristic = true          // a Capitalised word is a type
    var callHeuristic = true          // a word followed by "(" is a call
    var dashIdent = false             // CSS-like names with "-"
    var atAttributes = false          // @decorator, @interface
    var hashDirective = false         // C preprocessor
    var hashAttribute = false         // Rust #[...]
    var dollarVariables = false       // shell $VAR ${VAR}
    var commentNeedsBoundary = false  // shell "#": only at the start of a word
}

private func words(_ s: String) -> Set<String> { Set(s.split(whereSeparator: { $0 == " " || $0 == "\n" }).map(String.init)) }

private func spec(for lang: SyntaxLanguage) -> ScanSpec {
    var s = ScanSpec()
    switch lang {
    case .swift:
        s.lineComments = ["//"]; s.blockComment = ("/*", "*/"); s.tripleQuotes = true; s.quotes = ["\""]
        s.atAttributes = true; s.hashDirective = true
        s.keywords = words("""
            associatedtype class deinit enum extension fileprivate func import init inout internal let open operator private \
            precedencegroup protocol public rethrows static struct subscript typealias var break case catch continue default defer do \
            else fallthrough for guard if in repeat return throw switch where while as Any await false is nil self Self super throws \
            true try async actor some any final lazy override mutating nonmutating weak unowned convenience required indirect get set \
            willSet didSet nonisolated isolated consuming borrowing macro
            """)
    case .python:
        s.lineComments = ["#"]; s.tripleQuotes = true; s.atAttributes = true
        s.keywords = words("""
            and as assert async await break class continue def del elif else except finally for from global if import in is lambda \
            nonlocal not or pass raise return try while with yield None True False self cls match case
            """)
    case .javascript, .typescript:
        s.lineComments = ["//"]; s.blockComment = ("/*", "*/"); s.quotes = ["\"", "'", "`"]; s.multilineQuotes = ["`"]
        s.atAttributes = (lang == .typescript)
        var k = words("""
            break case catch class const continue debugger default delete do else export extends finally for function if import in \
            instanceof let new return super switch this throw try typeof var void while with yield async await of static get set null \
            undefined true false from as
            """)
        if lang == .typescript {
            k.formUnion(words("interface type enum implements namespace declare readonly abstract private public protected keyof infer is string number boolean any unknown never"))
        }
        s.keywords = k
    case .go:
        s.lineComments = ["//"]; s.blockComment = ("/*", "*/"); s.quotes = ["\"", "`", "'"]; s.multilineQuotes = ["`"]
        s.keywords = words("break case chan const continue default defer else fallthrough for func go goto if import interface map package range return select struct switch type var nil true false iota")
    case .rust:
        s.lineComments = ["//"]; s.blockComment = ("/*", "*/"); s.quotes = ["\""]; s.hashAttribute = true
        s.keywords = words("as async await break const continue crate dyn else enum extern false fn for if impl in let loop match mod move mut pub ref return self Self static struct super trait true type unsafe use where while")
    case .java, .kotlin:
        s.lineComments = ["//"]; s.blockComment = ("/*", "*/"); s.atAttributes = true; s.tripleQuotes = true
        var k = words("""
            abstract assert boolean break byte case catch char class const continue default do double else enum extends final finally \
            float for goto if implements import instanceof int interface long native new package private protected public return short \
            static super switch synchronized this throw throws transient try void volatile while true false null var record sealed permits yield \
            namespace using string object bool decimal foreach in is as readonly override virtual async await
            """)
        if lang == .kotlin {
            k.formUnion(words("fun val when companion data open internal lateinit by out suspend inline reified typealias init constructor object is as in"))
        }
        s.keywords = k
    case .cfamily:
        s.lineComments = ["//"]; s.blockComment = ("/*", "*/"); s.hashDirective = true; s.atAttributes = true
        s.keywords = words("""
            auto break case char const continue default do double else enum extern float for goto if inline int long register restrict return \
            short signed sizeof static struct switch typedef union unsigned void volatile while bool class namespace template typename public \
            private protected virtual override new delete this nullptr true false NULL using try catch throw operator constexpr noexcept final \
            explicit friend mutable id nil YES NO self super
            """)
    case .sql:
        s.lineComments = ["--"]; s.blockComment = ("/*", "*/"); s.caseInsensitive = true; s.typeHeuristic = false; s.quotes = ["'", "\""]
        s.keywords = words("""
            select from where insert into values update set delete create table alter drop index view join inner left right outer full cross \
            on as and or not null is in between like order by group having limit offset union all distinct case when then else end primary key \
            foreign references default unique check constraint begin commit rollback with returning exists asc desc using pragma if replace \
            trigger function procedure true false int integer bigint smallint varchar char text boolean date timestamp numeric decimal real float
            """)
    case .shell:
        s.lineComments = ["#"]; s.commentNeedsBoundary = true; s.typeHeuristic = false; s.callHeuristic = false; s.dollarVariables = true
        s.dashIdent = true
        s.keywords = words("""
            if then else elif fi for while until do done case esac in function select time return exit break continue local export readonly \
            declare set unset source alias echo cd read shift test true false eval exec trap
            """)
    case .css:
        s.blockComment = ("/*", "*/"); s.dashIdent = true; s.typeHeuristic = false; s.callHeuristic = true
    default:
        break
    }
    return s
}

// MARK: - Entry point

enum SyntaxTokenizer {
    /// Tokens for `text`. An empty array for plain text.
    static func tokens(for text: String, language: SyntaxLanguage) -> [SyntaxToken] {
        let u = Array(text.utf16)
        switch language {
        case .plain: return []
        case .json: return scanJSON(u)
        case .yaml: return scanKeyValue(u, separator: ch(":"), yaml: true)
        case .toml: return scanKeyValue(u, separator: ch("="), yaml: false)
        case .markdown: return scanMarkdown(u)
        case .html: return scanMarkup(u)
        case .css: return scanGeneric(u, spec(for: .css), css: true)
        default: return scanGeneric(u, spec(for: language), css: false)
        }
    }
}

// MARK: - Character helpers

private func ch(_ s: Unicode.Scalar) -> UInt16 { UInt16(s.value) }
private let NL = ch("\n")
private let CR = ch("\r")

private func isIdentStart(_ c: UInt16) -> Bool {
    (c >= 65 && c <= 90) || (c >= 97 && c <= 122) || c == 95 || c == 36 || c > 127
}
private func isDigit(_ c: UInt16) -> Bool { c >= 48 && c <= 57 }
private func isIdentPart(_ c: UInt16) -> Bool { isIdentStart(c) || isDigit(c) }
private func isSpace(_ c: UInt16) -> Bool { c == 32 || c == 9 }
private func isHex(_ c: UInt16) -> Bool { isDigit(c) || (c >= 65 && c <= 70) || (c >= 97 && c <= 102) }

private func matches(_ u: [UInt16], _ i: Int, _ pat: [UInt16]) -> Bool {
    if pat.isEmpty || i + pat.count > u.count { return false }
    for k in 0..<pat.count where u[i + k] != pat[k] { return false }
    return true
}

private func find(_ u: [UInt16], from: Int, _ pat: [UInt16]) -> Int? {
    if pat.isEmpty { return nil }
    var i = from
    let last = u.count - pat.count
    while i <= last {
        if u[i] == pat[0] && matches(u, i, pat) { return i }
        i += 1
    }
    return nil
}

private func lineEnd(_ u: [UInt16], _ i: Int) -> Int {
    var j = i
    while j < u.count && u[j] != NL && u[j] != CR { j += 1 }
    return j
}

private func asString(_ u: [UInt16], _ a: Int, _ b: Int) -> String {
    String(decoding: u[a..<b], as: UTF16.self)
}

/// Scans a quoted string starting at `i` (the opening quote). Returns the index after the closing quote.
private func scanQuoted(_ u: [UInt16], _ i: Int, multiline: Bool, escapes: Bool = true) -> Int {
    let q = u[i]
    var j = i + 1
    while j < u.count {
        let c = u[j]
        if c == 92 && escapes { j += 2; continue }        // backslash
        if c == q { return j + 1 }
        if (c == NL) && !multiline { return j }
        j += 1
    }
    return u.count
}

// MARK: - Generic scanner

private func scanGeneric(_ u: [UInt16], _ s: ScanSpec, css: Bool) -> [SyntaxToken] {
    var out: [SyntaxToken] = []
    let lineComments = s.lineComments.map { Array($0.utf16) }
    let blockOpen = s.blockComment.map { Array($0.0.utf16) } ?? []
    let blockClose = s.blockComment.map { Array($0.1.utf16) } ?? []
    let quoteSet = Set(s.quotes.map { $0.utf16.first! })
    let multiSet = Set(s.multilineQuotes.map { $0.utf16.first! })
    var braceDepth = 0
    var i = 0
    let n = u.count
    func add(_ a: Int, _ b: Int, _ k: SyntaxKind) { if b > a { out.append(SyntaxToken(location: a, length: b - a, kind: k)) } }

    while i < n {
        let c = u[i]
        // block comment
        if !blockOpen.isEmpty && matches(u, i, blockOpen) {
            let e = find(u, from: i + blockOpen.count, blockClose).map { $0 + blockClose.count } ?? n
            add(i, e, .comment); i = e; continue
        }
        // line comment
        var isLineComment = false
        for p in lineComments where matches(u, i, p) { isLineComment = true; break }
        if isLineComment && s.commentNeedsBoundary && i > 0 {
            let prev = u[i - 1]
            if !(isSpace(prev) || prev == NL || prev == 59 || prev == 40 || prev == 124 || prev == 38) { isLineComment = false }
            if prev == 36 { isLineComment = false }       // $#
        }
        if isLineComment { let e = lineEnd(u, i); add(i, e, .comment); i = e; continue }
        // triple-quoted strings
        if s.tripleQuotes && (c == 34 || c == 39) && i + 2 < n && u[i + 1] == c && u[i + 2] == c {
            let e = find(u, from: i + 3, [c, c, c]).map { $0 + 3 } ?? n
            add(i, e, .string); i = e; continue
        }
        // strings
        if quoteSet.contains(c) {
            // Rust/Swift-like lifetimes and chars: a lone ' in rust is not in quoteSet, so nothing to special-case here.
            let e = scanQuoted(u, i, multiline: multiSet.contains(c), escapes: !(s.dollarVariables && c == 39))
            add(i, e, .string); i = e; continue
        }
        // numbers
        if isDigit(c) || (c == 46 && i + 1 < n && isDigit(u[i + 1]) && (i == 0 || !isIdentPart(u[i - 1]))) {
            var j = i + 1
            while j < n && (isIdentPart(u[j]) || (u[j] == 46 && j + 1 < n && isDigit(u[j + 1]))) { j += 1 }
            if css { while j < n && (u[j] == 37) { j += 1 } }
            add(i, j, .number); i = j; continue
        }
        // CSS hex colours and selectors
        if css && c == 35 {
            var j = i + 1
            while j < n && (isIdentPart(u[j]) || u[j] == 45) { j += 1 }
            let allHex = j > i + 1 && (i + 1..<j).allSatisfy { isHex(u[$0]) } && (j - i - 1 == 3 || j - i - 1 == 6 || j - i - 1 == 8)
            add(i, j, allHex ? .number : .type); i = max(j, i + 1); continue
        }
        if css && c == 46 && i + 1 < n && isIdentStart(u[i + 1]) && braceDepth == 0 {
            var j = i + 1
            while j < n && (isIdentPart(u[j]) || u[j] == 45) { j += 1 }
            add(i, j, .type); i = j; continue
        }
        if css && c == 123 { braceDepth += 1 }
        if css && c == 125 { braceDepth = max(0, braceDepth - 1) }
        // C preprocessor / Rust attributes
        if s.hashDirective && c == 35 {
            var k = i - 1
            while k >= 0 && isSpace(u[k]) { k -= 1 }
            if k < 0 || u[k] == NL || u[k] == CR {
                var j = i + 1
                while j < n && isSpace(u[j]) { j += 1 }
                let ws = j
                while j < n && isIdentPart(u[j]) { j += 1 }
                add(i, j, .attribute)
                let word = asString(u, ws, j)
                if word == "include" || word == "import" {
                    var m = j
                    while m < n && isSpace(u[m]) { m += 1 }
                    if m < n && u[m] == 60 { // <
                        let e = min(lineEnd(u, m), (find(u, from: m, [62]).map { $0 + 1 } ?? n))
                        add(m, e, .string); j = e
                    }
                }
                i = max(j, i + 1); continue
            }
        }
        if s.hashAttribute && c == 35 && i + 1 < n && (u[i + 1] == 91 || (u[i + 1] == 33 && i + 2 < n && u[i + 2] == 91)) {
            let e = (find(u, from: i, [93]).map { $0 + 1 }) ?? lineEnd(u, i)
            add(i, e, .attribute); i = e; continue
        }
        // shell variables
        if s.dollarVariables && c == 36 && i + 1 < n {
            var j = i + 1
            if u[j] == 123 { // ${...}
                let e = (find(u, from: j, [125]).map { $0 + 1 }) ?? lineEnd(u, j)
                add(i, e, .attribute); i = e; continue
            }
            if isIdentPart(u[j]) || u[j] == 63 || u[j] == 36 || u[j] == 33 || u[j] == 64 || u[j] == 42 || u[j] == 35 {
                if isIdentPart(u[j]) { while j < n && isIdentPart(u[j]) { j += 1 } } else { j += 1 }
                add(i, j, .attribute); i = j; continue
            }
        }
        // @attributes / decorators / at-rules
        if c == 64 && (s.atAttributes || css) && i + 1 < n && isIdentStart(u[i + 1]) {
            var j = i + 1
            while j < n && (isIdentPart(u[j]) || u[j] == 46 || (css && u[j] == 45)) { j += 1 }
            add(i, j, css ? .keyword : .attribute); i = j; continue
        }
        // words
        if isIdentStart(c) && c != 36 || (c == 36 && !s.dollarVariables && isIdentStart(c)) {
            var j = i + 1
            while j < n && (isIdentPart(u[j]) || (s.dashIdent && u[j] == 45 && j + 1 < n && isIdentStart(u[j + 1]))) { j += 1 }
            let w = asString(u, i, j)
            let key = s.caseInsensitive ? w.lowercased() : w
            if s.keywords.contains(key) {
                add(i, j, .keyword)
            } else if css {
                var m = j
                while m < n && isSpace(u[m]) { m += 1 }
                if braceDepth > 0 && m < n && u[m] == 58 { add(i, j, .attribute) }
                else if m < n && u[m] == 40 { add(i, j, .call) }
            } else {
                var m = j
                while m < n && isSpace(u[m]) { m += 1 }
                if s.callHeuristic && m < n && u[m] == 40 && !(s.typeHeuristic && u[i] >= 65 && u[i] <= 90) {
                    add(i, j, .call)
                } else if s.typeHeuristic && u[i] >= 65 && u[i] <= 90 {
                    add(i, j, .type)
                }
            }
            i = j; continue
        }
        i += 1
    }
    return out
}

// MARK: - JSON

private func scanJSON(_ u: [UInt16]) -> [SyntaxToken] {
    var out: [SyntaxToken] = []
    let n = u.count
    var i = 0
    while i < n {
        let c = u[i]
        if c == 47 && i + 1 < n && u[i + 1] == 47 {                    // // (JSONC)
            let e = lineEnd(u, i); out.append(SyntaxToken(location: i, length: e - i, kind: .comment)); i = e; continue
        }
        if c == 47 && i + 1 < n && u[i + 1] == 42 {                    // /* */
            let e = find(u, from: i + 2, [42, 47]).map { $0 + 2 } ?? n
            out.append(SyntaxToken(location: i, length: e - i, kind: .comment)); i = e; continue
        }
        if c == 34 {
            let e = scanQuoted(u, i, multiline: false)
            var m = e
            while m < n && (isSpace(u[m]) || u[m] == NL || u[m] == CR) { m += 1 }
            let isKey = m < n && u[m] == 58
            out.append(SyntaxToken(location: i, length: e - i, kind: isKey ? .attribute : .string)); i = e; continue
        }
        if isDigit(c) || (c == 45 && i + 1 < n && isDigit(u[i + 1])) {
            var j = i + 1
            while j < n && (isDigit(u[j]) || u[j] == 46 || u[j] == 101 || u[j] == 69 || u[j] == 43 || u[j] == 45) { j += 1 }
            out.append(SyntaxToken(location: i, length: j - i, kind: .number)); i = j; continue
        }
        if isIdentStart(c) {
            var j = i + 1
            while j < n && isIdentPart(u[j]) { j += 1 }
            let w = asString(u, i, j)
            if w == "true" || w == "false" || w == "null" { out.append(SyntaxToken(location: i, length: j - i, kind: .keyword)) }
            i = j; continue
        }
        i += 1
    }
    return out
}

// MARK: - YAML, TOML, INI (line based)

private func scanKeyValue(_ u: [UInt16], separator: UInt16, yaml: Bool) -> [SyntaxToken] {
    var out: [SyntaxToken] = []
    let n = u.count
    var ls = 0
    func add(_ a: Int, _ b: Int, _ k: SyntaxKind) { if b > a { out.append(SyntaxToken(location: a, length: b - a, kind: k)) } }
    while ls < n {
        let le = lineEnd(u, ls)
        var i = ls
        while i < le && isSpace(u[i]) { i += 1 }
        if i < le {
            let c = u[i]
            if c == 35 || (!yaml && c == 59) {                                   // comment line
                add(i, le, .comment)
            } else if !yaml && c == 91 {                                          // [section]
                add(i, le, .type)
            } else if yaml && (matches(u, i, Array("---".utf16)) || matches(u, i, Array("...".utf16))) {
                add(i, le, .comment)
            } else {
                if yaml {                                                         // list dashes
                    while i < le && u[i] == 45 && i + 1 < le && isSpace(u[i + 1]) {
                        add(i, i + 1, .keyword); i += 1
                        while i < le && isSpace(u[i]) { i += 1 }
                    }
                }
                // key
                var k = i
                var keyEnd: Int? = nil
                if k < le && (u[k] == 34 || u[k] == 39) {
                    let e = min(scanQuoted(u, k, multiline: false), le)
                    var m = e
                    while m < le && isSpace(u[m]) { m += 1 }
                    if m < le && u[m] == separator { keyEnd = e; k = m }
                } else {
                    while k < le {
                        if u[k] == separator && (!yaml || k + 1 >= le || isSpace(u[k + 1])) { keyEnd = k; break }
                        if u[k] == 35 && yaml && k > i && isSpace(u[k - 1]) { break }
                        k += 1
                    }
                    if yaml, let ke = keyEnd, ke == i { keyEnd = nil }
                }
                var vs = i
                if let ke = keyEnd {
                    var kEnd = ke
                    while kEnd > i && isSpace(u[kEnd - 1]) { kEnd -= 1 }
                    add(i, kEnd, .attribute)
                    var m = ke
                    while m < le && u[m] != separator { m += 1 }
                    vs = min(m + 1, le)
                }
                // value
                var j = vs
                while j < le {
                    let d = u[j]
                    if d == 34 || d == 39 {
                        let e = min(scanQuoted(u, j, multiline: false, escapes: d == 34), le)
                        add(j, e, .string); j = max(e, j + 1); continue
                    }
                    if (d == 35 && (j == vs || isSpace(u[j - 1]))) || (!yaml && d == 59 && (j == vs || isSpace(u[j - 1]))) {
                        add(j, le, .comment); j = le; continue
                    }
                    if isDigit(d) && (j == vs || !isIdentPart(u[j - 1])) {
                        var m = j + 1
                        while m < le && (isIdentPart(u[m]) || u[m] == 46) { m += 1 }
                        add(j, m, .number); j = m; continue
                    }
                    if isIdentStart(d) && (j == vs || !isIdentPart(u[j - 1])) {
                        var m = j + 1
                        while m < le && isIdentPart(u[m]) { m += 1 }
                        let w = asString(u, j, m).lowercased()
                        if ["true", "false", "null", "yes", "no", "on", "off", "~"].contains(w) { add(j, m, .keyword) }
                        j = m; continue
                    }
                    if yaml && (d == 38 || d == 42) && j + 1 < le && isIdentStart(u[j + 1]) {       // &anchor *alias
                        var m = j + 1
                        while m < le && isIdentPart(u[m]) { m += 1 }
                        add(j, m, .type); j = m; continue
                    }
                    j += 1
                }
            }
        }
        ls = le
        while ls < n && (u[ls] == NL || u[ls] == CR) { ls += 1 }
    }
    return out
}

// MARK: - Markdown

private func scanMarkdown(_ u: [UInt16]) -> [SyntaxToken] {
    var out: [SyntaxToken] = []
    let n = u.count
    func add(_ a: Int, _ b: Int, _ k: SyntaxKind) { if b > a { out.append(SyntaxToken(location: a, length: b - a, kind: k)) } }
    var inFence = false
    var fenceChar: UInt16 = 0
    var ls = 0
    var firstLine = true
    var inFrontMatter = false
    while ls < n {
        let le = lineEnd(u, ls)
        var i = ls
        var spaces = 0
        while i < le && u[i] == 32 && spaces < 3 { i += 1; spaces += 1 }
        let c: UInt16 = i < le ? u[i] : 0
        let isDashes = matches(u, i, Array("---".utf16)) && (ls..<le).allSatisfy { u[$0] == 45 || isSpace(u[$0]) }
        if firstLine && isDashes { inFrontMatter = true; add(ls, le, .comment); firstLine = false; ls = nextLine(u, le); continue }
        firstLine = false
        if inFrontMatter {
            add(ls, le, .comment)
            if isDashes || matches(u, i, Array("...".utf16)) { inFrontMatter = false }
            ls = nextLine(u, le); continue
        }
        if (c == 96 || c == 126) && i + 2 < le + 0 && u[i + 1] == c && u[i + 2] == c && (!inFence || c == fenceChar) {
            inFence.toggle(); fenceChar = c
            add(ls, le, .string); ls = nextLine(u, le); continue
        }
        if inFence { add(ls, le, .string); ls = nextLine(u, le); continue }
        if c == 35 {                                                          // heading
            var h = i
            while h < le && u[h] == 35 { h += 1 }
            if h - i <= 6 && (h == le || isSpace(u[h])) { add(i, le, .heading); ls = nextLine(u, le); continue }
        }
        if c == 62 { add(i, i + 1, .keyword) }                                // quote marker
        else if (c == 45 || c == 42 || c == 43) && i + 1 < le && isSpace(u[i + 1]) { add(i, i + 1, .keyword) }
        else if isDigit(c) {
            var d = i
            while d < le && isDigit(u[d]) { d += 1 }
            if d < le && (u[d] == 46 || u[d] == 41) && d + 1 < le && isSpace(u[d + 1]) { add(i, d + 1, .keyword) }
        } else if isDashes || ((c == 42 || c == 95) && (i..<le).allSatisfy { u[$0] == c || isSpace(u[$0]) } && le - i >= 3) {
            add(i, le, .comment); ls = nextLine(u, le); continue
        }
        // inline
        var j = i
        while j < le {
            let d = u[j]
            if d == 92 { j += 2; continue }                                    // backslash escape
            if d == 96 {                                                       // `code`
                var ticks = 0
                while j + ticks < le && u[j + ticks] == 96 { ticks += 1 }
                if let e = find(u, from: j + ticks, [UInt16](repeating: 96, count: ticks)), e < le {
                    add(j, e + ticks, .string); j = e + ticks; continue
                }
                j += ticks; continue
            }
            if d == 60 && matches(u, j, Array("<!--".utf16)) {
                let e = find(u, from: j + 4, Array("-->".utf16)).map { $0 + 3 } ?? le
                add(j, min(e, le), .comment); j = min(e, le); continue
            }
            if d == 33 || d == 91 {                                            // ![alt](url) [text](url)
                var b = j
                if d == 33 { b += 1 }
                if b < le && u[b] == 91, let close = find(u, from: b, [93]), close < le, close + 1 < le, u[close + 1] == 40,
                   let paren = find(u, from: close + 1, [41]), paren < le {
                    add(j, paren + 1, .link); j = paren + 1; continue
                }
            }
            if d == 42 || d == 95 {                                            // **bold** *em* __bold__ _em_
                var run = 0
                while j + run < le && u[j + run] == d { run += 1 }
                if run <= 3 && j + run < le && !isSpace(u[j + run]) && (d == 42 || j == 0 || !isIdentPart(u[j - 1])) {
                    let closer = [UInt16](repeating: d, count: run)
                    var from = j + run
                    var matched = false
                    while let e = find(u, from: from, closer), e < le {
                        if e > j + run && !isSpace(u[e - 1]) && (d == 42 || e + run >= le || !isIdentPart(u[e + run])) {
                            add(j, e + run, .emphasis); j = e + run; matched = true; break
                        }
                        from = e + 1
                    }
                    if matched { continue }
                }
                j += max(run, 1); continue
            }
            j += 1
        }
        ls = nextLine(u, le)
    }
    return out
}

private func nextLine(_ u: [UInt16], _ le: Int) -> Int {
    var j = le
    if j < u.count && u[j] == CR { j += 1 }
    if j < u.count && u[j] == NL { j += 1 }
    return j > le ? j : u.count
}

// MARK: - HTML / XML

private func scanMarkup(_ u: [UInt16]) -> [SyntaxToken] {
    var out: [SyntaxToken] = []
    let n = u.count
    var i = 0
    func add(_ a: Int, _ b: Int, _ k: SyntaxKind) { if b > a { out.append(SyntaxToken(location: a, length: b - a, kind: k)) } }
    let commentOpen = Array("<!--".utf16), commentClose = Array("-->".utf16)
    while i < n {
        guard let lt = find(u, from: i, [60]) else { break }
        if matches(u, lt, commentOpen) {
            let e = find(u, from: lt + 4, commentClose).map { $0 + 3 } ?? n
            add(lt, e, .comment); i = e; continue
        }
        var j = lt + 1
        if j < n && (u[j] == 33 || u[j] == 63) {                                // <!DOCTYPE …> <?xml …?>
            let e = find(u, from: j, [62]).map { $0 + 1 } ?? n
            add(lt, e, .attribute); i = e; continue
        }
        if j < n && u[j] == 47 { j += 1 }
        guard j < n, isIdentStart(u[j]) else { i = lt + 1; continue }
        let nameStart = j
        while j < n && (isIdentPart(u[j]) || u[j] == 45 || u[j] == 58 || u[j] == 46) { j += 1 }
        add(lt, j, .tag); _ = nameStart
        // attributes
        while j < n && u[j] != 62 {
            let c = u[j]
            if c == 34 || c == 39 {
                let e = scanQuoted(u, j, multiline: true, escapes: false)
                add(j, e, .string); j = e; continue
            }
            if isIdentStart(c) {
                var m = j + 1
                while m < n && (isIdentPart(u[m]) || u[m] == 45 || u[m] == 58 || u[m] == 46) { m += 1 }
                add(j, m, .attribute); j = m; continue
            }
            j += 1
        }
        if j < n { add(j - (u[j - 1] == 47 ? 1 : 0), j + 1, .tag); j += 1 }
        i = j
    }
    return out
}

// MARK: - Colours

enum SyntaxTheme {
    private static func dyn(_ light: Int, _ dark: Int) -> NSColor {
        NSColor(name: nil) { appearance in
            let isDark = appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
            let v = isDark ? dark : light
            return NSColor(srgbRed: CGFloat((v >> 16) & 0xFF) / 255, green: CGFloat((v >> 8) & 0xFF) / 255,
                           blue: CGFloat(v & 0xFF) / 255, alpha: 1)
        }
    }

    static let keyword = dyn(0xAD3DA4, 0xFC5FA3)
    static let string = dyn(0xD12F1B, 0xFC6A5D)
    static let comment = dyn(0x707F8C, 0x7F8C98)
    static let number = dyn(0x272AD8, 0xD0BF69)
    static let type = dyn(0x3E8087, 0x5DD8FF)
    static let call = dyn(0x326D74, 0x67B7A4)
    static let attribute = dyn(0x815F03, 0xBF8555)
    static let tag = dyn(0xAD3DA4, 0xFC5FA3)
    static let heading = dyn(0x0A60FF, 0x6AB0FF)
    static let emphasis = dyn(0x6C36A9, 0xA167E6)
    static let link = dyn(0x0E6EE0, 0x6CA6FF)

    static func color(_ k: SyntaxKind) -> NSColor {
        switch k {
        case .keyword: return keyword
        case .string: return string
        case .comment: return comment
        case .number: return number
        case .type: return type
        case .call: return call
        case .attribute: return attribute
        case .tag: return tag
        case .heading: return heading
        case .emphasis: return emphasis
        case .link: return link
        }
    }
}
