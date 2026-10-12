// Quick Search's requests to the local service and the reading of their answers. A request runs off the main thread
// (MSEClient.getQuick: cached config, its own timeout, one retry after a 401), and what comes back is plain Sendable
// values, so the main actor only ever applies finished results. Endpoints:
//   GET  /api/files/search  the names: scope=mac (the whole Mac) or indexed (the indexed folders), paged by offset
//   GET  /api/search        the contents of the indexed folders; precise=1 is the re-ranked answer
//   POST /api/files/opened  frecency: a result the user opened (a failure is ignored)

import Cocoa

/// Where the names are searched: the whole Mac, or only the indexed folders.
enum QuickScope: String, Sendable { case mac, indexed }

/// A file or folder name from the whole-Mac search. `match` holds the matched ranges in UTF-16 units of `name`.
struct QSFile: Sendable, Equatable {
    let path: String
    let name: String
    let parent: String
    let isDir: Bool
    let kind: String
    let size: Int64?
    let mtime: Date?
    let match: [NSRange]
    let collection: String?
}

/// What a classified document is (the `meta` of a /api/search result).
struct QSDocMeta: Sendable, Equatable {
    let docType: String
    let docDate: String?
    let amount: Double?
    let currency: String?
    let counterparty: String?
}

/// One indexed content match: its first matching passage, with the matched words as ranges in `snippet` (UTF-16).
struct QSContent: Sendable, Equatable {
    let path: String                  // absolute
    let name: String
    let relPath: String               // inside its indexed folder
    let collection: String?
    let snippet: String               // the <mark> tags are gone; `marks` says where they were
    let marks: [NSRange]
    let meta: QSDocMeta?
    let date: Date?                   // a date given as epoch seconds
    let dateText: String?             // a date given as text ("2025-03")
    let relevance: Int?
}

/// One whole-disk hit as Quick Search shows it. `snippet` is the plain text with its matched words as `marks` (UTF-16 ranges),
/// `labels` are what an image was labelled (zebra · animal · grass), and `why` is the channel that found it.
struct QSDiskHit: Sendable, Equatable {
    let path: String
    let name: String
    let mtime: Date?
    let snippet: String
    let marks: [NSRange]
    let labels: [String]
    let why: String

    init(_ hit: DiskHit) {
        path = hit.path
        name = hit.name
        mtime = hit.mtime
        let (plain, ranges) = QuickSearchService.unmark(hit.snippet)
        snippet = plain
        marks = ranges
        labels = hit.labels
        why = hit.why
    }

    /// The hint at the right of the row: the two channels worth naming ("label", "text").
    var badge: String? { why == "label" || why == "text" ? why : nil }
}

/// Why a search failed, as the panel shows it. `cancelled` is a newer query superseding this one and is never shown.
enum QuickFailure: Error, Equatable, Sendable {
    case offline                      // the service is not running or not set up
    case unauthorized                 // 401 again after the config was re-read
    case cancelled
    case server(String)
}

enum QuickSearchService {
    struct FilesPage: Sendable {
        let items: [QSFile]
        let total: Int
        let hasMore: Bool
        let nextOffset: Int?
        let ms: Int
    }

    struct ContentPage: Sendable {
        let items: [QSContent]
        let total: Int
    }

    /// GET /api/files/search. `offset` and `limit` are sent on every call, so the answer is a page (has_more, next_offset).
    static func files(_ q: String, scope: QuickScope, offset: Int, limit: Int) async throws -> FilesPage {
        let obj = try await MSEClient.shared.getQuick("/api/files/search", query: [
            URLQueryItem(name: "q", value: q),
            URLQueryItem(name: "scope", value: scope.rawValue),
            URLQueryItem(name: "limit", value: String(limit)),
            URLQueryItem(name: "offset", value: String(offset)),
        ], timeout: 8)
        let d = (obj as? [String: Any]) ?? [:]
        let items = ((d["items"] as? [[String: Any]]) ?? []).compactMap(parseFile)
        return FilesPage(items: items,
                         total: (d["total"] as? NSNumber)?.intValue ?? items.count,
                         hasMore: (d["has_more"] as? NSNumber)?.boolValue ?? false,
                         nextOffset: (d["next_offset"] as? NSNumber)?.intValue,
                         ms: (d["ms"] as? NSNumber)?.intValue ?? 0)
    }

    /// GET /api/search over every indexed folder. The query goes through DocQuery like the main window's, so `type:`
    /// means the same thing in both places. `precise` asks for the re-ranked order.
    static func content(_ q: String, precise: Bool) async throws -> ContentPage {
        var items = [URLQueryItem(name: "q", value: DocQuery.forServer(q)),
                     URLQueryItem(name: "c", value: "all"),
                     URLQueryItem(name: "limit", value: "25")]
        if precise { items.append(URLQueryItem(name: "precise", value: "1")) }
        let obj = try await MSEClient.shared.getQuick("/api/search", query: items, timeout: 20)
        let d = (obj as? [String: Any]) ?? [:]
        let rows = ((d["results"] as? [[String: Any]]) ?? []).compactMap(parseContent)
        return ContentPage(items: rows, total: (d["total"] as? NSNumber)?.intValue ?? rows.count)
    }

    /// GET /api/disk/search, the first page: the On this Mac section (Whole Mac scope, when the settings turn it on).
    static func disk(_ q: String) async throws -> [QSDiskHit] {
        try await MSEClient.shared.diskSearch(q, limit: 8, offset: 0).items.map(QSDiskHit.init)
    }

    /// POST /api/files/opened: the file ranks higher for this query next time. Nothing is shown when it fails.
    static func opened(path: String, query: String?) async {
        var body: [String: Any] = ["path": path]
        if let query, !query.isEmpty { body["query"] = query }
        _ = try? await MSEClient.shared.postJSON("/api/files/opened", body: body)
    }

    /// The panel's reading of a thrown error.
    static func failure(_ error: Error) -> QuickFailure {
        if error is CancellationError { return .cancelled }
        if let u = error as? URLError { return u.code == .cancelled ? .cancelled : .offline }
        if let a = error as? APIError {
            switch a {
            case .noConfig: return .offline
            case .http(let code): return code == 401 ? .unauthorized : .server("The search service answered with HTTP \(code).")
            }
        }
        if let m = error as? MSEServerMessage { return .server(m.message) }
        return .server(error.localizedDescription)
    }

    // MARK: parsing (pure; runs off the main thread)

    static func parseFile(_ d: [String: Any]) -> QSFile? {
        guard let path = d["path"] as? String else { return nil }
        let name = (d["name"] as? String) ?? (path as NSString).lastPathComponent
        let parent = (d["parent"] as? String) ?? (path as NSString).deletingLastPathComponent
        return QSFile(path: path, name: name, parent: parent,
                      isDir: (d["is_dir"] as? NSNumber)?.boolValue ?? false,
                      kind: (d["kind"] as? String) ?? "",
                      size: (d["size"] as? NSNumber)?.int64Value,
                      mtime: (d["mtime"] as? NSNumber).map { Date(timeIntervalSince1970: $0.doubleValue) },
                      match: utf16Ranges(name, (d["match"] as? [[Any]]) ?? []),
                      collection: d["collection"] as? String)
    }

    static func parseContent(_ d: [String: Any]) -> QSContent? {
        let absolute = (d["abs_path"] as? String) ?? (d["path"] as? String).flatMap { $0.hasPrefix("/") ? $0 : nil }
        guard let path = absolute else { return nil }
        let name = (d["name"] as? String) ?? (path as NSString).lastPathComponent
        let hit = ((d["hits"] as? [[String: Any]]) ?? []).first
        let (snippet, marks) = unmark((hit?["snippet"] as? String) ?? "")
        var meta: QSDocMeta?
        if let m = d["meta"] as? [String: Any], let type = m["doctype"] as? String, !type.isEmpty {
            meta = QSDocMeta(docType: type,
                             docDate: m["doc_date"] as? String,
                             amount: (m["amount"] as? NSNumber)?.doubleValue,
                             currency: nonEmpty(m["currency"] as? String),
                             counterparty: nonEmpty(m["counterparty"] as? String))
        }
        let dateValue = d["date"]
        return QSContent(path: path, name: name,
                         relPath: (d["path"] as? String) ?? path,
                         collection: d["collection"] as? String,
                         snippet: snippet, marks: marks, meta: meta,
                         date: (dateValue as? NSNumber).map { Date(timeIntervalSince1970: $0.doubleValue) },
                         dateText: dateValue as? String,
                         relevance: (d["relevance"] as? NSNumber)?.intValue)
    }

    /// Splits "<mark>word</mark>" out of a snippet: the plain text, and the UTF-16 ranges of the marked words in it.
    static func unmark(_ s: String) -> (String, [NSRange]) {
        var out = ""
        var marks: [NSRange] = []
        var length = 0
        var rest = Substring(s)
        while let open = rest.range(of: "<mark>") {
            let plain = decodeEntities(String(rest[..<open.lowerBound]))
            out += plain
            length += plain.utf16.count
            rest = rest[open.upperBound...]
            guard let close = rest.range(of: "</mark>") else { break }
            let inner = decodeEntities(String(rest[..<close.lowerBound]))
            marks.append(NSRange(location: length, length: inner.utf16.count))
            out += inner
            length += inner.utf16.count
            rest = rest[close.upperBound...]
        }
        out += decodeEntities(String(rest))
        return (out, marks)
    }

    /// The service counts name positions in code points (Python str); NSRange wants UTF-16 units. Converted here.
    static func utf16Ranges(_ name: String, _ pairs: [[Any]]) -> [NSRange] {
        let scalars = name.unicodeScalars
        return pairs.compactMap { p in
            guard p.count == 2, let s = (p[0] as? NSNumber)?.intValue, let e = (p[1] as? NSNumber)?.intValue,
                  s >= 0, e >= s, e <= scalars.count else { return nil }
            let a = scalars.index(scalars.startIndex, offsetBy: s)
            let b = scalars.index(scalars.startIndex, offsetBy: e)
            return NSRange(a..<b, in: name)
        }
    }

    static func decodeEntities(_ s: String) -> String {
        guard s.contains("&") else { return s }
        return s.replacingOccurrences(of: "&lt;", with: "<")
            .replacingOccurrences(of: "&gt;", with: ">")
            .replacingOccurrences(of: "&quot;", with: "\"")
            .replacingOccurrences(of: "&#39;", with: "'")
            .replacingOccurrences(of: "&amp;", with: "&")
    }

    private static func nonEmpty(_ s: String?) -> String? {
        guard let s, !s.isEmpty else { return nil }
        return s
    }
}
