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

/// The filter sets the snapshot states show (search-indexed-filters, search-indexed-custom-date, search-mac-filters).
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
