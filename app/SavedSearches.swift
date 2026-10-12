// Saved searches: a query, its folders and its file filters, kept under a name (GET and POST /api/saved, PATCH and DELETE
// /api/saved/<id>). Removing one removes only the definition: no file and no index is touched, and the sheet says so.
// The sidebar lists them in a "Saved" section (Sidebar.swift). A click posts SavedSearchStore.picked for the sidebar's
// window, and that window's search view runs the search in Indexed mode with the saved folders (SearchView.swift).
// A saved search keeps what the service keeps: its text (document filters included) and its folders. The search method
// (Words + meaning and the rest) is not part of it, so a saved search runs with the method the window has.

import Cocoa

/// One saved search, as /api/saved lists it.
struct SavedSearch: Equatable {
    let id: String
    let name: String
    let query: String
    /// The folders and groups it searches, comma separated (nil = every indexed folder).
    let collection: String?

    init?(_ d: [String: Any]) {
        guard let id = d["id"] as? String, let name = d["name"] as? String, let query = d["query"] as? String else { return nil }
        self.id = id
        self.name = name
        self.query = query
        self.collection = (d["collection"] as? String).flatMap { $0.isEmpty ? nil : $0 }
    }

    /// The folder and group names of `collection`, in order.
    var collectionNames: [String] {
        (collection ?? "").split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
    }
}

/// The service's saved searches. Every call goes to the service; a change posts `didChange` so the sidebar lists it again.
@MainActor
enum SavedSearchStore {
    static let didChange = Notification.Name("MySearch.savedSearchesDidChange")
    /// A saved search was clicked in a sidebar. `object` is the sidebar's window; userInfo["saved"] is the SavedSearch.
    static let picked = Notification.Name("MySearch.savedSearchPicked")

    static func load() async throws -> [SavedSearch] {
        let obj = (try await MSEClient.shared.get("/api/saved") as? [String: Any]) ?? [:]
        return ((obj["saved"] as? [[String: Any]]) ?? []).compactMap(SavedSearch.init)
    }

    @discardableResult
    static func add(name: String, query: String, collection: String?) async throws -> SavedSearch? {
        var body: [String: Any] = ["name": name, "query": query]
        if let collection, !collection.isEmpty { body["collection"] = collection }
        let obj = try await MSEClient.shared.sendJSON("POST", "/api/saved", body: body)
        changed()
        return SavedSearch(obj)
    }

    static func rename(_ s: SavedSearch, to name: String) async throws {
        _ = try await MSEClient.shared.sendJSON("PATCH", "/api/saved/\(s.id)", body: ["name": name])
        changed()
    }

    static func editQuery(_ s: SavedSearch, to query: String) async throws {
        _ = try await MSEClient.shared.sendJSON("PATCH", "/api/saved/\(s.id)", body: ["query": query])
        changed()
    }

    /// Removes the definition only (DELETE /api/saved/<id>); the files and the index stay as they are.
    static func remove(_ s: SavedSearch) async throws {
        _ = try await MSEClient.shared.sendJSON("DELETE", "/api/saved/\(s.id)")
        changed()
    }

    static func changed() {
        NotificationCenter.default.post(name: didChange, object: nil)
    }

    /// A sheet that asks for a name (or a query): the text field is selected, Return confirms, Cancel gives nil.
    static func askName(title: String, value: String, button: String, in window: NSWindow,
                        completion: @escaping (String?) -> Void) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = "The search, its filters and its folders are kept under this name. Your files are not changed."
        let field = NSTextField(string: value)
        field.placeholderString = "Name"
        field.frame = NSRect(x: 0, y: 0, width: 340, height: 24)
        alert.accessoryView = field
        alert.addButton(withTitle: button)
        alert.addButton(withTitle: "Cancel")
        alert.beginSheetModal(for: window) { response in
            guard response == .alertFirstButtonReturn else { completion(nil); return }
            let text = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            completion(text.isEmpty ? nil : text)
        }
        alert.window.initialFirstResponder = field
    }

    /// The confirmation before a delete. It says that only the saved search goes away.
    static func confirmDelete(_ s: SavedSearch, in window: NSWindow, completion: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = "Delete the saved search “\(s.name)”?"
        alert.informativeText = "This removes the saved search only. Your files are untouched."
        alert.alertStyle = .warning
        alert.addButton(withTitle: "Delete")
        alert.addButton(withTitle: "Cancel")
        alert.beginSheetModal(for: window) { response in
            completion(response == .alertFirstButtonReturn)
        }
    }

    /// A failure of a save, rename, edit or delete, in a sheet on the window.
    static func report(_ error: Error, in window: NSWindow) {
        let alert = NSAlert()
        alert.messageText = "The saved search could not be changed."
        alert.informativeText = error.localizedDescription
        alert.addButton(withTitle: "OK")
        alert.beginSheetModal(for: window) { _ in }
    }
}

/// The query of a saved search and its file filters, both ways. The file filters (kind, date, include, exclude) are
/// operators in the saved text, as the server reads them: type:pdf, after:, before:, include:, exclude:.
enum SavedQuery {
    /// The text without the file filters, and the filters it names (they become the window's filters when it is run).
    static func split(_ text: String) -> (text: String, filters: SearchFilters) {
        var f = SearchFilters()
        var rest: [String] = []
        var after: Date?, before: Date?
        for t in DocQuery.tokens(text) {
            guard let p = DocQuery.pair(t) else { rest.append(t); continue }
            let value = DocQuery.unquote(p.value)
            switch p.key {
            case "type":
                if !DocQuery.isDocOperator("type", p.value), let kind = KindFilter.fromFacet(value.lowercased()) {
                    f.kind = kind
                } else {
                    rest.append(t)
                }
            case "after": after = DocDate.period(value)?.0 ?? after
            case "before": before = DocDate.period(value)?.0 ?? before
            case "include": f.include = value
            case "exclude": f.exclude = value
            default: rest.append(t)
            }
        }
        if after != nil || before != nil {
            let cal = Calendar.current
            let lo = after ?? Date(timeIntervalSince1970: 0)
            // before: is exclusive in the saved text, the day before it is the last day of the range
            let hi = before.flatMap { cal.date(byAdding: .day, value: -1, to: $0) } ?? Date()
            f.date = .custom(lo, hi)
        }
        return (rest.joined(separator: " "), f)
    }

    /// The text to save: the query as typed (document filters included), with the window's file filters written in as
    /// operators, unless the text already names one of them.
    static func compose(_ text: String, filters: SearchFilters) -> String {
        var parts = DocQuery.tokens(text)
        let hasKind = parts.contains { t in
            guard let p = DocQuery.pair(t) else { return false }
            return p.key == "type" && !DocQuery.isDocOperator("type", p.value)
        }
        if filters.kind != .any, !hasKind, let kind = filters.kind.apiValue { parts.append("type:\(kind)") }
        let bounds = filters.dateBounds()
        if let a = bounds.after, !parts.contains(where: { $0.hasPrefix("after:") }) { parts.append("after:\(a)") }
        if let b = bounds.before, !parts.contains(where: { $0.hasPrefix("before:") }) { parts.append("before:\(b)") }
        let inc = filters.include.trimmingCharacters(in: .whitespaces)
        if !inc.isEmpty, !parts.contains(where: { $0.hasPrefix("include:") }) { parts.append("include:\"\(inc)\"") }
        let exc = filters.exclude.trimmingCharacters(in: .whitespaces)
        if !exc.isEmpty, !parts.contains(where: { $0.hasPrefix("exclude:") }) { parts.append("exclude:\"\(exc)\"") }
        return parts.joined(separator: " ")
    }
}
