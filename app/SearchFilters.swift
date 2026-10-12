// The filter model of the native search, with the same choices as the web UI's filter bar (mse/ui/index.html and
// app.js, runSearch / searchFilters / dateFilter). Indexed mode sends the web's own parameters to /api/search
// (type, after, before, include, exclude, mode). Whole-Mac file-name search (/api/files/search) takes only q, limit,
// scope and c, so the same choices become its query operators (kind:, mtime:, size:, ext:, path:, !ext:, !path:).
// Globs the operators cannot express are matched here, with the backend's own gitignore-style rules (mse/ignore.py).

import Foundation

enum KindFilter: Int, CaseIterable {
    case any, pdf, office, text, email, image, audio, video

    /// The web's labels (index.html #f-type).
    var title: String {
        switch self {
        case .any: return "Any type"
        case .pdf: return "PDF"
        case .office: return "Word, Excel, slides"
        case .text: return "Notes & text"
        case .email: return "E-mail"
        case .image: return "Images"
        case .audio: return "Audio"
        case .video: return "Video"
        }
    }

    /// The `type` value /api/search takes; nil for Any type.
    var apiValue: String? {
        switch self {
        case .any: return nil
        case .pdf: return "pdf"
        case .office: return "office"
        case .text: return "text"
        case .email: return "email"
        case .image: return "image"
        case .audio: return "audio"
        case .video: return "video"
        }
    }

    /// The `kind:` operator of the file-name search; nil for Any type. Its Word kind covers .doc/.docx/.txt/.md, so
    /// Notes & text and Word overlap there (a difference from the content search, which keeps them apart).
    var macKind: String? {
        switch self {
        case .any: return nil
        case .pdf: return "pdf"
        case .office: return "doc,sheet,slides"
        case .text: return "doc"
        case .email: return "email"
        case .image: return "image"
        case .audio: return "audio"
        case .video: return "video"
        }
    }

    /// The type of a /api/search facet key ("pdf", "office", …); nil for "other" (no filter can express it).
    static func fromFacet(_ key: String) -> KindFilter? {
        switch key {
        case "pdf": return .pdf
        case "office": return .office
        case "text": return .text
        case "email": return .email
        case "image": return .image
        case "audio": return .audio
        case "video": return .video
        default: return nil
        }
    }
}

enum DatePreset: Equatable {
    case any, week, month, quarter, year
    /// An inclusive day range picked in the custom range sheet.
    case custom(Date, Date)

    var title: String {
        switch self {
        case .any: return "Any date"
        case .week: return "Past week"
        case .month: return "Past month"
        case .quarter: return "Past 3 months"
        case .year: return "This year"
        case .custom(let a, let b): return "\(Day.string(a)) to \(Day.string(b))"
        }
    }
}

enum SearchMethod: Equatable {
    case hybrid, keyword, semantic

    /// The web's labels (index.html #f-mode).
    var title: String {
        switch self {
        case .hybrid: return "Words + meaning"
        case .keyword: return "Exact words only"
        case .semantic: return "Meaning only"
        }
    }

    /// The `mode` /api/search takes.
    var apiValue: String {
        switch self {
        case .hybrid: return "hybrid"
        case .keyword: return "keyword"
        case .semantic: return "semantic"
        }
    }
}

/// Native only: the web UI lists results by relevance and has no sort control.
enum ResultSort: Equatable {
    case best, newest, oldest, name

    var title: String {
        switch self {
        case .best: return "Best match"
        case .newest: return "Newest first"
        case .oldest: return "Oldest first"
        case .name: return "Name"
        }
    }
}

/// Native only: a size filter for the whole-Mac file-name search (size: operator).
enum SizeFilter: Equatable {
    case any, over1MB, over10MB, over100MB, under1MB

    var title: String {
        switch self {
        case .any: return "Any size"
        case .over1MB: return "Over 1 MB"
        case .over10MB: return "Over 10 MB"
        case .over100MB: return "Over 100 MB"
        case .under1MB: return "Under 1 MB"
        }
    }

    var macOperator: String? {
        switch self {
        case .any: return nil
        case .over1MB: return "size:>1mb"
        case .over10MB: return "size:>10mb"
        case .over100MB: return "size:>100mb"
        case .under1MB: return "size:<1mb"
        }
    }
}

struct SearchFilters: Equatable {
    var kind: KindFilter = .any
    var date: DatePreset = .any
    var method: SearchMethod = .hybrid
    var size: SizeFilter = .any
    var sort: ResultSort = .best
    /// Comma-separated globs, sent as typed (trimmed) to /api/search.
    var include = ""
    var exclude = ""

    /// Any filter other than the sort order is set.
    var isActive: Bool {
        kind != .any || date != .any || method != .hybrid || size != .any || !includeTerms.isEmpty || !excludeTerms.isEmpty
    }

    var includeTerms: [String] { SearchFilters.terms(include) }
    var excludeTerms: [String] { SearchFilters.terms(exclude) }

    static func terms(_ s: String) -> [String] {
        s.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
    }

    /// after (on or after) and before (exclusive) as /api/search takes them. The web computes "Past N days" as the
    /// local date N days back, written as a UTC date (its toISOString), and "This year" from the local year.
    func dateBounds(now: Date = Date()) -> (after: String?, before: String?) {
        let cal = Calendar.current
        switch date {
        case .any:
            return (nil, nil)
        case .week, .month, .quarter:
            let days = date == .week ? 7 : (date == .month ? 31 : 92)
            let back = cal.date(byAdding: .day, value: -days, to: now) ?? now
            return (Day.string(back, utc: true), nil)
        case .year:
            return ("\(cal.component(.year, from: now))-01-01", nil)
        case .custom(let a, let b):
            let end = cal.date(byAdding: .day, value: 1, to: b) ?? b
            return (Day.string(a), Day.string(end))
        }
    }

    /// The parameters of the content search, named and valued as the web sends them.
    func contentParameters(now: Date = Date()) -> [URLQueryItem] {
        var items = [URLQueryItem(name: "mode", value: method.apiValue)]
        if let t = kind.apiValue { items.append(URLQueryItem(name: "type", value: t)) }
        let bounds = dateBounds(now: now)
        if let a = bounds.after { items.append(URLQueryItem(name: "after", value: a)) }
        if let b = bounds.before { items.append(URLQueryItem(name: "before", value: b)) }
        let inc = include.trimmingCharacters(in: .whitespaces)
        if !inc.isEmpty { items.append(URLQueryItem(name: "include", value: inc)) }
        let exc = exclude.trimmingCharacters(in: .whitespaces)
        if !exc.isEmpty { items.append(URLQueryItem(name: "exclude", value: exc)) }
        return items
    }

    /// Operators for the whole-Mac file-name query. Include and exclude globs become operators only where the
    /// operator means the same thing (ext: for extension globs, path: for a single folder glob, !ext:, !path:);
    /// the rest is applied by `keeps(_:)`.
    func macOperators(now: Date = Date()) -> [String] {
        var ops: [String] = []
        if let k = kind.macKind { ops.append("kind:\(k)") }
        if let s = size.macOperator { ops.append(s) }
        switch date {
        case .any: break
        case .custom(let a, let b): ops.append("mtime:\(Day.string(a))..\(Day.string(b))")
        default:
            if let a = dateBounds(now: now).after { ops.append("mtime:>\(a)") }
        }
        let inc = includeTerms.map(SearchFilters.webGlob)
        let incExt = inc.compactMap(SearchFilters.extensionOf)
        if !inc.isEmpty && incExt.count == inc.count {
            ops.append("ext:" + incExt.joined(separator: ","))          // all includes are extensions: one OR list
        } else if inc.count == 1, inc[0].contains("/") {
            ops.append("path:\"\(inc[0])\"")
        }
        for g in excludeTerms.map(SearchFilters.webGlob) {
            if let e = SearchFilters.extensionOf(g) { ops.append("!ext:\(e)") }
            else if g.contains("/") { ops.append("!path:\"\(g)\"") }
        }
        return ops
    }

    /// The client-side part of the include and exclude globs, for names that came back from /api/files/search.
    /// A name is kept when it matches an include glob (if any are set) and no exclude glob.
    func keeps(path: String) -> Bool {
        let inc = includeTerms.map(SearchFilters.webGlob)
        let exc = excludeTerms.map(SearchFilters.webGlob)
        if !inc.isEmpty && !GlobMatcher(inc).matches(path) { return false }
        if !exc.isEmpty && GlobMatcher(exc).matches(path) { return false }
        return true
    }

    /// The labels of the active filters, for the scope tokens above the results.
    var tokens: [String] {
        var out: [String] = []
        if kind != .any { out.append(kind.title) }
        if date != .any { out.append(date.title) }
        if method != .hybrid { out.append(method.title) }
        if size != .any { out.append(size.title) }
        if !includeTerms.isEmpty { out.append("Include " + includeTerms.joined(separator: ", ")) }
        if !excludeTerms.isEmpty { out.append("Exclude " + excludeTerms.joined(separator: ", ")) }
        return out
    }

    /// Web glob rules (mse/search.py _glob): ".md" and "md" (a known extension) mean "*.md".
    static let extWords: Set<String> = [
        "md", "txt", "pdf", "json", "csv", "eml", "html", "xlsx", "docx", "pptx", "jpg", "jpeg", "png", "heic",
        "mp3", "m4a", "mp4", "mov", "wav", "py", "js", "ts", "yaml", "yml", "xml", "doc", "xls", "zip",
    ]

    static func webGlob(_ term: String) -> String {
        let p = term.trimmingCharacters(in: .whitespaces)
        let isExtLike = p.range(of: #"^\.?[A-Za-z0-9]{1,8}$"#, options: .regularExpression) != nil
        if isExtLike && (p.hasPrefix(".") || extWords.contains(p.lowercased())) {
            return "*." + String(p.drop(while: { $0 == "." }))
        }
        return p
    }

    /// "*.pdf" -> "pdf"; anything that is not a plain extension glob -> nil.
    static func extensionOf(_ glob: String) -> String? {
        guard glob.range(of: #"^\*\.[A-Za-z0-9]{1,8}$"#, options: .regularExpression) != nil else { return nil }
        return String(glob.dropFirst(2)).lowercased()
    }
}

/// Day strings and dates the way the web writes them: YYYY-MM-DD.
enum Day {
    static func string(_ d: Date, utc: Bool = false) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        f.timeZone = utc ? TimeZone(identifier: "UTC") : .current
        return f.string(from: d)
    }
}

/// Port of mse/ignore.py (compile_pattern and _translate): gitignore-style globs. A pattern without an inner slash
/// matches at any depth, and a pattern that names a folder also matches everything under it. Case-insensitive.
struct GlobMatcher {
    private let regexes: [NSRegularExpression]

    init(_ patterns: [String]) {
        regexes = patterns.compactMap(GlobMatcher.compile)
    }

    func matches(_ path: String) -> Bool {
        let range = NSRange(path.startIndex..., in: path)
        return regexes.contains { $0.firstMatch(in: path, options: [], range: range) != nil }
    }

    static func compile(_ pattern: String) -> NSRegularExpression? {
        var core = pattern.trimmingCharacters(in: .whitespaces)
        if core.isEmpty || core.hasPrefix("#") { return nil }
        while core.hasSuffix("/") { core.removeLast() }
        let anchored = core.hasPrefix("/") || core.dropFirst(core.hasPrefix("/") ? 1 : 0).contains("/")
        while core.hasPrefix("/") { core.removeFirst() }
        let prefix = anchored ? "^" : "^(?:.*/)?"
        return try? NSRegularExpression(pattern: prefix + translate(core) + "(?:/.*)?$", options: [.caseInsensitive])
    }

    static func translate(_ glob: String) -> String {
        let c = Array(glob)
        var out = ""
        var i = 0
        func at(_ s: String) -> Bool {
            let u = Array(s)
            return i + u.count <= c.count && Array(c[i..<(i + u.count)]) == u
        }
        while i < c.count {
            if at("**/") {
                out += "(?:.*/)?"; i += 3
            } else if at("/**") && i + 3 == c.count {
                out += "(?:/.*)?"; i += 3
            } else if at("**") {
                out += ".*"; i += 2
            } else if c[i] == "*" {
                out += "[^/]*"; i += 1
            } else if c[i] == "?" {
                out += "[^/]"; i += 1
            } else if c[i] == "[", let close = c[(i + 1)...].firstIndex(of: "]") {
                out += "[" + String(c[(i + 1)..<close]).replacingOccurrences(of: "\\", with: "\\\\") + "]"
                i = close + 1
            } else {
                out += NSRegularExpression.escapedPattern(for: String(c[i])); i += 1
            }
        }
        return out
    }
}

/// The filter sets the snapshot states show (search-indexed-filters, search-indexed-custom-date, search-mac-filters, search-indexed-chips).
enum SnapshotFilters {
    static var indexed: SearchFilters {
        var f = SearchFilters()
        f.kind = .pdf
        f.date = .month
        f.method = .keyword
        f.include = "*.pdf, Invoices/"
        f.exclude = ".md"
        f.sort = .newest
        return f
    }

    static var mac: SearchFilters {
        var f = SearchFilters()
        f.kind = .pdf
        f.date = .year
        f.include = "*.pdf"
        f.exclude = "Downloads/"
        return f
    }

    /// search-indexed-chips and search-indexed-more: file filters from three groups (type, date, include); the document
    /// filters come from the search text of the state.
    static var chips: SearchFilters {
        var f = SearchFilters()
        f.kind = .pdf
        f.date = .month
        f.include = "*.pdf"
        return f
    }
}

/// The searches the user acted on, newest first, for the search field's recent-searches menu. A search is kept once
/// it has stood for two seconds with something listed, or when a result of it is opened; never on every keystroke.
/// Stored in the app's preferences on this Mac only; "Clear Recent Searches" empties it.
enum RecentSearches {
    private static let key = "MySearch.recentSearches"
    static let didChange = Notification.Name("MySearch.recentSearchesDidChange")
    static let limit = 12

    static var all: [String] {
        (UserDefaults.standard.array(forKey: key) as? [String]) ?? []
    }

    static func record(_ query: String) {
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard q.count >= 2 else { return }
        var list = all.filter { $0.caseInsensitiveCompare(q) != .orderedSame }
        list.insert(q, at: 0)
        UserDefaults.standard.set(Array(list.prefix(limit)), forKey: key)
        NotificationCenter.default.post(name: didChange, object: nil)
    }

    static func clear() {
        UserDefaults.standard.removeObject(forKey: key)
        NotificationCenter.default.post(name: didChange, object: nil)
    }
}

// MARK: - Document filters (type, amount, currency, party, document date)
// They live in the search text as operators, as the server reads them (mse/search.py, mse/docmeta.py): type:invoice (or
// doctype:other), amount:>500, currency:EUR, party:acme, docdate:2025-03. The text is the one source: a pop-up change
// rewrites the operators of its group in the text and runs the search again, and a typed operator sets its pop-up. The
// server echoes the filters it applied (res.filters), and those are shown when the text says it in plain language.

/// What a document is (the names of mse/docmeta.py DOCTYPES, with the aliases of DOCTYPE_ALIASES).
enum DocType: String, CaseIterable {
    case invoice, receipt, bankStatement = "bank_statement", contract, letter, quote, reminder, payslip
    case taxDocument = "tax_document", other

    /// The name the search text uses.
    var key: String { rawValue }

    /// The pop-up and badge label.
    var title: String {
        switch self {
        case .invoice: return "Invoice"
        case .receipt: return "Receipt"
        case .bankStatement: return "Statement"
        case .contract: return "Contract"
        case .letter: return "Letter"
        case .quote: return "Quote"
        case .reminder: return "Reminder"
        case .payslip: return "Payslip"
        case .taxDocument: return "Tax"
        case .other: return "Other"
        }
    }

    /// The aliases the server accepts for a type, lower case (docmeta.DOCTYPE_ALIASES).
    private static let aliases: [String: DocType] = [
        "invoices": .invoice, "rechnung": .invoice, "rechnungen": .invoice, "faktura": .invoice,
        "receipts": .receipt, "quittung": .receipt, "quittungen": .receipt, "kassenbon": .receipt,
        "kassenbons": .receipt, "kassenbeleg": .receipt,
        "bank-statement": .bankStatement, "bankstatement": .bankStatement, "statement": .bankStatement,
        "statements": .bankStatement, "bank_statements": .bankStatement, "kontoauszug": .bankStatement,
        "kontoauszuege": .bankStatement, "kontoauszüge": .bankStatement, "kontoauszuge": .bankStatement,
        "contracts": .contract, "vertrag": .contract, "vertraege": .contract, "verträge": .contract,
        "vertrage": .contract, "agreement": .contract, "agreements": .contract,
        "letters": .letter, "brief": .letter, "briefe": .letter,
        "quotes": .quote, "quotation": .quote, "quotations": .quote, "angebot": .quote, "angebote": .quote,
        "offer": .quote, "offers": .quote,
        "reminders": .reminder, "mahnung": .reminder, "mahnungen": .reminder, "dunning": .reminder,
        "payslips": .payslip, "lohnabrechnung": .payslip, "lohnabrechnungen": .payslip,
        "gehaltsabrechnung": .payslip, "gehaltsabrechnungen": .payslip,
        "tax-document": .taxDocument, "tax_documents": .taxDocument, "tax": .taxDocument, "steuer": .taxDocument,
        "steuerdokument": .taxDocument, "steuerbescheid": .taxDocument,
    ]

    /// The document type a name stands for (canonical or alias, any case). `other` is returned for "other" too: the caller
    /// decides whether that counts (in type: it is the file kind other, so only doctype: means the document type).
    static func named(_ value: String) -> DocType? {
        let key = value.trimmingCharacters(in: .whitespaces).lowercased()
        return DocType(rawValue: key) ?? aliases[key]
    }
}

/// The amount presets of the Amount pop-up, and a custom minimum and maximum (either may be open).
enum AmountChoice: Equatable {
    case any, under100, between100and500, over500
    case custom(Double?, Double?)

    var title: String {
        switch self {
        case .any: return "Any amount"
        case .under100: return "Under 100"
        case .between100and500: return "100 to 500"
        case .over500: return "Over 500"
        case .custom(let lo, let hi):
            switch (lo, hi) {
            case let (l?, h?): return "\(AmountChoice.text(l)) to \(AmountChoice.text(h))"
            case let (l?, nil): return "From \(AmountChoice.text(l))"
            case let (nil, h?): return "Up to \(AmountChoice.text(h))"
            default: return "Any amount"
            }
        }
    }

    /// A number as the operators write it: 500 for 500, 12.5 for 12.5.
    static func text(_ v: Double) -> String {
        v == v.rounded() ? String(Int(v)) : String(v)
    }

    /// The bounds of a custom choice, for the amount sheet (nil, nil for the presets).
    var customBounds: (Double?, Double?) {
        if case .custom(let lo, let hi) = self { return (lo, hi) }
        return (nil, nil)
    }

    /// The operators of this choice (amount:…), in the form the server reads.
    var operators: [String] {
        switch self {
        case .any: return []
        case .under100: return ["amount:<100"]
        case .between100and500: return ["amount:100..500"]
        case .over500: return ["amount:>500"]
        case .custom(let lo, let hi):
            switch (lo, hi) {
            case let (l?, h?): return ["amount:\(AmountChoice.text(l))..\(AmountChoice.text(h))"]
            case let (l?, nil): return ["amount:>=\(AmountChoice.text(l))"]
            case let (nil, h?): return ["amount:<=\(AmountChoice.text(h))"]
            default: return []
            }
        }
    }
}

/// The document filters of a search text (or of the server's answer to it).
struct DocFilters: Equatable {
    var types: [DocType] = []
    var amount: AmountChoice = .any
    var currency: String? = nil
    var party = ""
    var docdate: DatePreset = .any

    var isActive: Bool { !types.isEmpty || amount != .any || currency != nil || !party.isEmpty || docdate != .any }

    /// The filters the (key, value) pairs of a query or of res.filters name. type: counts only for a document type (not a
    /// file kind such as pdf); doctype: counts for every document type, other included.
    init(pairs: [(key: String, value: String)] = []) {
        var amounts: [String] = []
        var docdates: [String] = []
        for (key, raw) in pairs {
            let value = DocQuery.unquote(raw)
            switch key {
            case "type", "doctype":
                if let t = DocType.named(value), key == "doctype" || t != .other, !types.contains(t) { types.append(t) }
            case "amount": amounts.append(value)
            case "currency": if currency == nil, !value.isEmpty { currency = value.uppercased() }
            case "party": if party.isEmpty { party = value }
            case "docdate": docdates.append(value)
            default: break
            }
        }
        amount = DocFilters.amountChoice(amounts)
        docdate = DocFilters.datePreset(docdates)
    }

    /// The filters the server applied, from res.filters ({key: [values]}).
    init(serverFilters: [String: Any]) {
        var pairs: [(key: String, value: String)] = []
        for key in ["type", "doctype", "amount", "currency", "party", "docdate"] {
            for v in (serverFilters[key] as? [Any]) ?? [] {
                if let s = v as? String { pairs.append((key, s)) }
            }
        }
        self.init(pairs: pairs)
    }

    /// Amount operators -> the preset when they say exactly one of them, else a custom range (a lower and an upper bound).
    static func amountChoice(_ values: [String]) -> AmountChoice {
        if values.isEmpty { return .any }
        var lo: Double?, hi: Double?
        for v in values {
            guard let b = AmountOperator.bounds(v) else { continue }
            if let l = b.0 { lo = max(lo ?? l, l) }
            if let h = b.1 { hi = min(hi ?? h, h) }
        }
        switch (lo, hi) {
        case (nil, 100?): return .under100
        case (100?, 500?): return .between100and500
        case (500?, nil): return .over500
        case (nil, nil): return .any
        default: return .custom(lo, hi)
        }
    }

    /// The date of the document: a year (this year, or any other), a month, a day, or a range; a year-long or open range
    /// is the custom range. "This year" is the year itself.
    static func datePreset(_ values: [String]) -> DatePreset {
        guard let v = values.last, let range = DocDate.range(v) else { return .any }
        if v == String(Calendar.current.component(.year, from: Date())) { return .year }
        return .custom(range.0, range.1)
    }
}

/// The operator grammar of amount: values (">500", ">=500", "<100", "<=100", "100..500", "500") as bounds.
enum AmountOperator {
    /// (lower, upper) of one value; nil when the value is not an amount.
    static func bounds(_ raw: String) -> (Double?, Double?)? {
        let v = raw.replacingOccurrences(of: " ", with: "").replacingOccurrences(of: ",", with: "")
        if let r = v.range(of: "..") {
            let lo = Double(v[..<r.lowerBound]), hi = Double(v[r.upperBound...])
            if lo == nil && hi == nil { return nil }
            return (lo, hi)
        }
        for op in [">=", "<=", ">", "<", "="] where v.hasPrefix(op) {
            guard let n = Double(v.dropFirst(op.count)) else { return nil }
            switch op {
            case ">", ">=": return (n, nil)
            case "<", "<=": return (nil, n)
            default: return (n, n)
            }
        }
        guard let n = Double(v) else { return nil }
        return (n, n)
    }
}

/// The document-date values of a docdate: operator (2025, 2025-03, 2025-03-14, 2025-01..2025-06, 2025-01-01.., ..2025-06)
/// as the first and last day they cover. Days are local; the server counts whole days.
enum DocDate {
    private static let day: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()

    /// The first and last day of one period (2025, 2025-03, 2025-03-14).
    static func period(_ v: String) -> (Date, Date)? {
        let cal = Calendar.current
        let parts = v.split(separator: "-").map(String.init)
        guard let y = Int(parts.first ?? "") else { return nil }
        if parts.count == 1 {
            guard let a = cal.date(from: DateComponents(year: y, month: 1, day: 1)),
                  let b = cal.date(from: DateComponents(year: y, month: 12, day: 31)) else { return nil }
            return (a, b)
        }
        guard let m = Int(parts[1]) else { return nil }
        if parts.count == 2 {
            guard let a = cal.date(from: DateComponents(year: y, month: m, day: 1)),
                  let next = cal.date(byAdding: .month, value: 1, to: a),
                  let b = cal.date(byAdding: .day, value: -1, to: next) else { return nil }
            return (a, b)
        }
        guard let d = day.date(from: v) else { return nil }
        return (d, d)
    }

    /// The first and last day a docdate value covers; nil when it is not a date.
    static func range(_ raw: String) -> (Date, Date)? {
        let v = raw.trimmingCharacters(in: .whitespaces)
        guard let r = v.range(of: "..") else { return period(v) }
        let a = String(v[..<r.lowerBound]), b = String(v[r.upperBound...])
        let lo = a.isEmpty ? Date.distantPast : period(a)?.0
        let hi = b.isEmpty ? Date() : period(b)?.1
        guard let lo, let hi else { return nil }
        return (lo, hi)
    }
}

/// The text operators of the document filters: reading them, and writing them back.
enum DocQuery {
    /// The operator names this file owns (type: only for a document type; see DocFilters).
    static let keys: Set<String> = ["type", "doctype", "amount", "currency", "party", "docdate"]

    /// The whitespace-separated tokens of a search text. A quoted run stays one token (party:"acme gmbh").
    static func tokens(_ text: String) -> [String] {
        var out: [String] = []
        var current = ""
        var quoted = false
        for ch in text {
            if ch == "\"" { quoted.toggle(); current.append(ch); continue }
            if ch.isWhitespace && !quoted {
                if !current.isEmpty { out.append(current); current = "" }
                continue
            }
            current.append(ch)
        }
        if !current.isEmpty { out.append(current) }
        return out
    }

    /// The (key, value) of an operator token ("party:acme" -> ("party", "acme")); nil for a word.
    static func pair(_ token: String) -> (key: String, value: String)? {
        guard let colon = token.firstIndex(of: ":") else { return nil }
        let key = token[..<colon].lowercased()
        guard keys.contains(key) || ["after", "before", "kind", "ext", "include", "exclude"].contains(key) else { return nil }
        return (key, String(token[token.index(after: colon)...]))
    }

    /// Removes the quotes around a value ("\"muster logistik\"" -> muster logistik).
    static func unquote(_ value: String) -> String {
        value.count >= 2 && value.hasPrefix("\"") && value.hasSuffix("\"") ? String(value.dropFirst().dropLast()) : value
    }

    /// The document filters a text carries (its operators only; plain-language filters are the server's to say).
    static func parse(_ text: String) -> DocFilters {
        var pairs: [(key: String, value: String)] = []
        for t in tokens(text) {
            guard let p = pair(t), isDocOperator(p.key, p.value) else { continue }
            pairs.append(p)
        }
        return DocFilters(pairs: pairs)
    }

    /// Whether an operator belongs to the document filters: type: only with a document type (type:pdf is a file kind).
    static func isDocOperator(_ key: String, _ value: String) -> Bool {
        switch key {
        case "type": return DocType.named(DocQuery.unquote(value)).map { $0 != .other } ?? false
        case "doctype", "amount", "currency", "party", "docdate": return true
        default: return false
        }
    }

    /// The groups the pop-ups change; each owns the operators of its keys.
    enum Group: CaseIterable { case type, amount, currency, party, docdate }

    /// Whether a token is one of the operators of a group (the rewrite replaces exactly these).
    static func belongs(_ token: String, to group: Group) -> Bool {
        guard let p = pair(token), isDocOperator(p.key, p.value) else { return false }
        switch group {
        case .type: return p.key == "type" || p.key == "doctype"
        case .amount: return p.key == "amount"
        case .currency: return p.key == "currency"
        case .party: return p.key == "party"
        case .docdate: return p.key == "docdate"
        }
    }

    /// The text with the operators of one group replaced by `new` (appended at the end); every other token stays as typed.
    static func replacing(_ text: String, group: Group, with new: [String]) -> String {
        var out = tokens(text).filter { !belongs($0, to: group) }
        out += new
        return out.joined(separator: " ")
    }

    /// The operators that set the document type: type:invoice, doctype:other.
    static func typeOperators(_ types: [DocType]) -> [String] {
        types.map { $0 == .other ? "doctype:other" : "type:\($0.key)" }
    }

    static func currencyOperators(_ code: String?) -> [String] {
        guard let code, !code.isEmpty else { return [] }
        return ["currency:\(code.uppercased())"]
    }

    /// party:acme, or party:"muster logistik" when the name has a space.
    static func partyOperators(_ name: String) -> [String] {
        let v = name.trimmingCharacters(in: .whitespaces)
        guard !v.isEmpty else { return [] }
        return ["party:" + (v.contains(" ") ? "\"\(v)\"" : v)]
    }

    /// The operators of a date preset: the open range from its start for the past ranges, the year itself for this
    /// year (docdate:2026), the day range for a custom range.
    static func docdateOperators(_ preset: DatePreset, now: Date = Date()) -> [String] {
        let cal = Calendar.current
        switch preset {
        case .any: return []
        case .year: return ["docdate:\(cal.component(.year, from: now))"]
        case .week, .month, .quarter:
            let days = preset == .week ? 7 : (preset == .month ? 31 : 92)
            let back = cal.date(byAdding: .day, value: -days, to: now) ?? now
            return ["docdate:\(Day.string(back)).."]
        case .custom(let a, let b): return ["docdate:\(Day.string(a))..\(Day.string(b))"]
        }
    }

    /// The text with every document operator stripped, for the content search of a names-only or a words-only request.
    static func withoutDocOperators(_ text: String) -> String {
        tokens(text).filter { pair($0).map { !isDocOperator($0.key, $0.value) } ?? true }.joined(separator: " ")
    }

    /// The text the server reads: its document type is doctype: (the URL type= parameter carries the file kind and would
    /// override a type: operator), so a typed type:invoice is sent as doctype:invoice.
    static func forServer(_ text: String) -> String {
        tokens(text).map { t -> String in
            guard let p = pair(t), p.key == "type", let dt = DocType.named(unquote(p.value)), dt != .other else { return t }
            return "doctype:\(dt.key)"
        }.joined(separator: " ")
    }
}
