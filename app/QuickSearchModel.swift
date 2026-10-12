// Quick Search's state: the query, the scope, the names and contents it has, the rows the list shows and the selection.
// Requests are paced here: the first keystroke of a burst fires at once, later ones after a 40 ms pause; the content
// search waits 150 ms and the re-ranked answer 400 ms after typing stops. Every change of query or scope starts a new
// generation: a request that finishes for an older one is dropped, and its task is cancelled. The model never does file
// I/O on the main actor (recents are checked on a detached task) and never touches the window: it talks to it through
// QuickSearchHost and onChange.

import Cocoa

/// What the model asks of the window that shows it.
@MainActor
protocol QuickSearchHost: AnyObject {
    func quickSearchHide()
    func quickSearchOpenInApp(query: String, scope: String)
    func quickSearchToggleQuickLook(_ url: URL?)
    func quickSearchCopy(_ text: String)
    func quickSearchSetField(_ text: String)
    func quickSearchSelectionMoved()
}

struct QSRecentFile: Equatable, Sendable {
    let path: String
    let name: String
}

/// A line of the list that is not a result.
enum QSStatus: Equatable {
    case hint(String)               // help text, never selectable
    case searching                  // a section is loading
    case noResults(String)          // "No results for …"
    case offline                    // the service is not reachable: selectable, Enter retries
    case unauthorized               // the token was refused: selectable, Enter retries
    case failed(String)             // any other error: selectable, Enter retries
}

/// One row of the list. Section headers are rows too, so the list is one flat, virtual table.
enum QSRow: Equatable {
    case header(title: String, count: String, shortcut: Int?)
    case file(QSFile, top: Bool)
    case content(QSContent)
    case disk(QSDiskHit)
    case recentQuery(String)
    case recentFile(QSRecentFile)
    case status(QSStatus)

    /// The row's identity, kept across updates so the selection does not jump. nil for a row that cannot be selected.
    var id: String? {
        switch self {
        case .file(let f, _): return "f:" + f.path
        case .content(let c): return "c:" + c.path
        case .disk(let d): return "d:" + d.path
        case .recentQuery(let q): return "q:" + q
        case .recentFile(let r): return "r:" + r.path
        case .status(.offline), .status(.unauthorized), .status(.failed(_)): return "s:retry"
        default: return nil
        }
    }

    var isHeader: Bool {
        if case .header = self { return true }
        return false
    }
}

/// Seconds on the monotonic clock.
func qsNow() -> Double { Double(DispatchTime.now().uptimeNanoseconds) / 1e9 }

/// Sleeps; false when the task was cancelled meanwhile.
func qsSleep(_ nanoseconds: UInt64) async -> Bool {
    do {
        try await Task.sleep(nanoseconds: nanoseconds)
        return true
    } catch {
        return false
    }
}

/// The keystroke path's timing: keystroke, then names answered, then the first rows painted. Logged to stderr when
/// MSE_QS_TIMING is set (any value), and kept for the snapshot bench (SnapshotQuickSearch.swift). An environment switch
/// rather than a DEBUG flag, because the app build the scripts make has no -DDEBUG.
@MainActor
final class QSTiming {
    static let logging = ProcessInfo.processInfo.environment["MSE_QS_TIMING"] != nil
    private var t0 = 0.0
    private var t1 = 0.0
    private var pending = false
    private var answered = false
    /// One sample per typed query: service (keystroke to names answered), paint (answered to painted), total, in ms.
    private(set) var samples: [(service: Double, paint: Double, total: Double)] = []

    func keystroke() {
        t0 = qsNow()
        pending = true
        answered = false
    }

    /// The names answer for this keystroke arrived (already decoded off the main thread).
    func responded() {
        if pending {
            t1 = qsNow()
            answered = true
        }
    }

    /// The list was reloaded and displayed. A paint before the answer (the "searching" rows) is not a sample.
    func painted() {
        guard pending, answered else { return }
        pending = false
        answered = false
        let t2 = qsNow()
        let sample = (service: (t1 - t0) * 1000, paint: (t2 - t1) * 1000, total: (t2 - t0) * 1000)
        samples.append(sample)
        if Self.logging {
            fputs(String(format: "QS keystroke→names %.1f ms · names→painted %.2f ms · total %.1f ms\n",
                         sample.service, sample.paint, sample.total), stderr)
        }
    }
}

@MainActor
final class QuickSearchModel {
    weak var host: QuickSearchHost?
    /// The rows, the selection or the footer changed: the window redraws.
    var onChange: (() -> Void)?

    private(set) var query = ""
    private(set) var scope: QuickScope = .mac
    private(set) var rows: [QSRow] = []
    private(set) var selectedID: String?
    /// The user has moved into the list since the last edit. Space then opens Quick Look (see QuickSearchKeys).
    private(set) var listFocused = false
    /// A keyboard move asked for the selection to be scrolled into view. The window calls `consumeScroll()`.
    private(set) var scrollRequested = false
    /// A short footer message ("Path copied"), shown until the next change.
    private(set) var flash: String?

    let timing = QSTiming()

    private var files: [QSFile] = []
    private var filesTotal: Int?
    private var filesNext: Int?
    private var filesHasMore = false
    private var filesLoading = false
    private var filesFailure: QuickFailure?
    private var filesMs: Int?

    private var content: [QSContent] = []
    private var contentTotal: Int?
    private var contentLoading = false
    private var contentFailure: QuickFailure?
    private var rerankPending = false
    /// The On this Mac section: the whole-disk hits of the query, and why they are not there (not installed, or an error).
    private var disk: [QSDiskHit] = []
    private var diskLoading = false
    private var diskTask: Task<Void, Never>?
    private var diskUnavailable = false
    private var diskFailure: QuickFailure?
    private var diskObserver: NSObjectProtocol?

    private var recentQueries: [String] = []
    private var recentFiles: [QSRecentFile] = []

    private var userMoved = false
    private var generation = 0
    private var nameTask: Task<Void, Never>?
    private var contentTask: Task<Void, Never>?
    private var rerankTask: Task<Void, Never>?
    private var moreTask: Task<Void, Never>?
    private var lastNameFire: Double = -1

    // MARK: state the window and the snapshot read

    /// Nothing is loading and no re-rank is pending. The snapshot waits for this before it renders.
    var isSettled: Bool { !filesLoading && !contentLoading && !rerankPending && !diskLoading && moreTask == nil }

    /// Something is loading (the window shows a spinner).
    var isBusy: Bool { filesLoading || contentLoading || rerankPending || diskLoading }

    init() {
        // The whole-disk settings were read or saved: the section follows them (WholeDiskStore, SettingsWholeDisk.swift).
        diskObserver = NotificationCenter.default.addObserver(forName: .wholeDiskChanged, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.wholeDiskChanged() }
        }
    }

    var selectedRow: QSRow? { rows.first { $0.id != nil && $0.id == selectedID } }

    /// The footer's line: the flash, else the counts and the time of the last names answer.
    var footerLine: String {
        if let flash { return flash }
        var parts: [String] = []
        if let t = filesTotal, !filesLoading { parts.append("\(t) name\(t == 1 ? "" : "s")") }
        if let c = contentTotal, !contentLoading { parts.append("\(c) in folders") }
        if scope == .mac, !disk.isEmpty { parts.append("\(disk.count) on this Mac") }
        if let ms = filesMs, !parts.isEmpty { parts.append("\(ms) ms") }
        return parts.joined(separator: " · ")
    }

    /// The file Quick Look would show for the selection: a file or a content match, never a folder.
    var selectedPreviewURL: URL? {
        switch selectedRow {
        case .file(let f, _)?: return f.isDir ? nil : URL(fileURLWithPath: f.path)
        case .content(let c)?: return URL(fileURLWithPath: c.path)
        case .disk(let d)?: return URL(fileURLWithPath: d.path)
        case .recentFile(let r)?: return URL(fileURLWithPath: r.path)
        default: return nil
        }
    }

    func consumeScroll() { scrollRequested = false }

    // MARK: query and scope

    /// The field's text changed: `typed` when the user typed it (the list goes back to typing), false when the window or
    /// a recent query set it. `force` searches again even when the text is the same (a retry, a scope change).
    func setQuery(_ text: String, typed: Bool = false, force: Bool = false) {
        if text == query && !force {
            if typed { listFocused = false }
            return
        }
        query = text
        if typed { listFocused = false }
        userMoved = false
        flash = nil
        selectedID = nil
        generation += 1
        cancelWork()
        let gen = generation
        let trimmed = text.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else {
            files = []; filesTotal = nil; filesHasMore = false; filesFailure = nil; filesMs = nil
            content = []; contentTotal = nil; contentFailure = nil
            disk = []; diskFailure = nil
            rebuild()
            return
        }
        if typed { timing.keystroke() }

        let now = qsNow()
        let wait: UInt64 = (now - lastNameFire) > 0.25 ? 0 : 40_000_000   // a burst's first keystroke fires at once
        lastNameFire = now
        filesLoading = true
        nameTask = Task { [weak self] in
            if wait > 0 {
                guard await qsSleep(wait) else { return }
            }
            await self?.loadNames(gen: gen)
        }
        if trimmed.count >= 2 {
            contentLoading = true
            contentTask = Task { [weak self] in
                guard await qsSleep(150_000_000) else { return }
                await self?.loadContent(gen: gen, precise: false)
            }
        } else {
            content = []; contentTotal = nil; contentFailure = nil; contentLoading = false
        }
        startDisk(gen: gen, query: trimmed)
        rebuild()
    }

    func setScope(_ s: QuickScope) {
        guard s != scope else { return }
        scope = s
        if !query.trimmingCharacters(in: .whitespaces).isEmpty {
            setQuery(query, force: true)
        } else {
            onChange?()
        }
    }

    /// Retry (Enter on the error line, ⌘R): the service's config is read again before the search is sent, so a service that
    /// has started, or moved its port, since the last try is found.
    func retry() {
        QuickConfig.markStale()
        setQuery(query, force: true)
    }

    private func cancelWork() {
        nameTask?.cancel()
        contentTask?.cancel()
        rerankTask?.cancel()
        moreTask?.cancel()
        nameTask = nil
        contentTask = nil
        rerankTask = nil
        moreTask = nil
        filesLoading = false
        contentLoading = false
        rerankPending = false
        diskTask?.cancel()
        diskTask = nil
        diskLoading = false
    }

    // MARK: whole-disk section

    /// Asks the whole-disk service for the On this Mac section for the query of generation `gen`. Only in Whole Mac scope,
    /// and only while the settings turn it on. The answer is applied only while no newer query has started.
    private func startDisk(gen: Int, query: String) {
        diskTask?.cancel()
        diskTask = nil
        diskLoading = false
        diskFailure = nil
        guard scope == .mac, !query.isEmpty else { disk = []; return }
        WholeDiskStore.shared.ensureFresh()
        guard WholeDiskStore.shared.enabled, !diskUnavailable else { disk = []; return }
        diskLoading = true
        diskTask = Task { [weak self] in
            do {
                let hits = try await QuickSearchService.disk(query)
                guard let self, gen == self.generation, !Task.isCancelled else { return }
                self.disk = hits
                self.diskLoading = false
                self.diskTask = nil
                self.rebuild()
            } catch {
                guard let self, gen == self.generation, !Task.isCancelled else { return }
                self.diskLoading = false
                self.diskTask = nil
                if let api = error as? APIError, case .http(404) = api {
                    // no whole-disk search in this service: the section is not shown, and it is not an error
                    self.disk = []
                    self.diskUnavailable = true
                } else {
                let failure = QuickSearchService.failure(error)
                    if failure == .cancelled { return }
                    self.disk = []
                    self.diskFailure = failure
                }
                self.rebuild()
            }
        }
    }

    /// The settings were read or saved: the section follows them, and a query on the screen is asked again.
    private func wholeDiskChanged() {
        diskUnavailable = false
        let q = query.trimmingCharacters(in: .whitespaces)
        guard !q.isEmpty else { return }
        startDisk(gen: generation, query: q)
        rebuild()
    }

    /// The one line the section shows when the whole-disk search failed. Nothing for a cancelled request.
    private func diskFailureText(_ failure: QuickFailure) -> String {
        switch failure {
        case .offline: return "On this Mac: the search service is not running."
        case .unauthorized: return "On this Mac: the search service refused the token."
        case .server(let message): return "On this Mac: \(message)"
        case .cancelled: return ""
        }
    }

    // MARK: requests (the service call is off the main actor; these methods apply its result)

    private func loadNames(gen: Int) async {
        guard gen == generation else { return }
        filesLoading = true
        filesFailure = nil
        let q = query
        let sc = scope
        do {
            let page = try await QuickSearchService.files(q, scope: sc, offset: 0, limit: 50)
            guard gen == generation, !Task.isCancelled else { return }
            files = page.items
            filesTotal = page.total
            filesHasMore = page.hasMore
            filesNext = page.nextOffset
            filesMs = page.ms
            filesLoading = false
            timing.responded()
            rebuild()
        } catch {
            guard gen == generation, !Task.isCancelled else { return }
            let failure = QuickSearchService.failure(error)
            if failure == .cancelled { return }
            files = []; filesTotal = nil; filesHasMore = false; filesNext = nil
            filesFailure = failure
            filesLoading = false
            rebuild()
        }
    }

    private func loadContent(gen: Int, precise: Bool) async {
        guard gen == generation else { return }
        if precise {
            rerankPending = true
        } else {
            contentLoading = true
            contentFailure = nil
        }
        let q = query
        do {
            let page = try await QuickSearchService.content(q, precise: precise)
            guard gen == generation, !Task.isCancelled else { return }
            if precise {
                rerankPending = false
                rerankTask = nil
                // the re-ranked order replaces the list only when it differs: a same order changes nothing on screen
                if page.items.map(\.path) != content.map(\.path) {
                    content = page.items
                    contentTotal = page.total
                    rebuild()
                }
                return
            }
            content = page.items
            contentTotal = page.total
            contentLoading = false
            rebuild()
            if !page.items.isEmpty {
                rerankPending = true
                rerankTask = Task { [weak self] in
                    guard await qsSleep(400_000_000) else { return }
                    await self?.loadContent(gen: gen, precise: true)
                }
            }
        } catch {
            guard gen == generation, !Task.isCancelled else { return }
            if precise {
                rerankPending = false
                rerankTask = nil
                return
            }
            let failure = QuickSearchService.failure(error)
            if failure == .cancelled { return }
            content = []; contentTotal = nil
            contentFailure = failure
            contentLoading = false
            rebuild()
        }
    }

    /// The list was scrolled near its end: the next page of names (150 at a time, from next_offset).
    func loadMoreIfNeeded(lastVisibleRow: Int) {
        guard filesHasMore, !filesLoading, moreTask == nil, let next = filesNext,
              lastVisibleRow >= rows.count - 6 else { return }
        let gen = generation
        let q = query
        let sc = scope
        filesLoading = true
        moreTask = Task { [weak self] in
            do {
                let page = try await QuickSearchService.files(q, scope: sc, offset: next, limit: 150)
                self?.appendFiles(page, gen: gen)
            } catch {
                self?.moreFailed(gen: gen)
            }
        }
    }

    private func appendFiles(_ page: QuickSearchService.FilesPage, gen: Int) {
        moreTask = nil
        guard gen == generation else { return }
        let seen = Set(files.map(\.path))
        files += page.items.filter { !seen.contains($0.path) }
        filesTotal = page.total
        filesHasMore = page.hasMore
        filesNext = page.nextOffset
        filesLoading = false
        rebuild()
    }

    private func moreFailed(gen: Int) {
        moreTask = nil
        guard gen == generation else { return }
        filesLoading = false
        onChange?()
    }

    // MARK: rows

    private func rebuild() {
        rows = buildRows()
        if let selectedID, rows.contains(where: { $0.id == selectedID }) {
            // the selection stays on the same row
        } else {
            selectedID = rows.first { $0.id != nil }?.id
        }
        onChange?()
    }

    private func buildRows() -> [QSRow] {
        let q = query.trimmingCharacters(in: .whitespaces)
        if q.isEmpty {
            var out: [QSRow] = []
            if !recentQueries.isEmpty {
                out.append(.header(title: "Recent searches", count: "\(recentQueries.count)", shortcut: nil))
                out += recentQueries.map { .recentQuery($0) }
            }
            if !recentFiles.isEmpty {
                out.append(.header(title: "Recently opened", count: "\(recentFiles.count)", shortcut: nil))
                out += recentFiles.map { .recentFile($0) }
            }
            if out.isEmpty { out.append(.status(.hint("Type to search your files and folders"))) }
            return out
        }
        if let failure = filesFailure, failure != .cancelled {
            return [.status(status(for: failure))]
        }

        var out: [QSRow] = []
        var shortcut = 0
        func nextShortcut() -> Int? {
            shortcut += 1
            return shortcut <= 4 ? shortcut : nil
        }
        let top = topHit(q)
        if let top {
            out.append(.header(title: "Top hit", count: "1", shortcut: nextShortcut()))
            out.append(.file(top, top: true))
        }
        // the names without the top hit (it is shown above); the section is left out when nothing else is left
        let rest = top == nil ? files : files.filter { $0.path != top?.path }
        if !rest.isEmpty || (files.isEmpty && filesLoading) {
            let count = files.isEmpty && filesLoading ? "…" : "\(max(rest.count, (filesTotal ?? files.count) - (top == nil ? 0 : 1)))"
            out.append(.header(title: "Files", count: count, shortcut: nextShortcut()))
            if files.isEmpty { out.append(.status(.searching)) }
            out += rest.map { .file($0, top: false) }
        }
        // On this Mac: the whole-disk hits, without the names already listed above (Whole Mac scope only)
        let listed = Set((top.map { [$0.path] } ?? []) + files.map(\.path))
        let diskRows = disk.filter { !listed.contains($0.path) }
        if scope == .mac, !diskRows.isEmpty || diskLoading || diskFailure != nil {
            let count = diskLoading && diskRows.isEmpty ? "…" : "\(diskRows.count)"
            out.append(.header(title: "On this Mac", count: count, shortcut: nextShortcut()))
            if diskRows.isEmpty, let failure = diskFailure {
                out.append(.status(.hint(diskFailureText(failure))))
            } else if diskRows.isEmpty {
                out.append(.status(.searching))
            }
            out += diskRows.map { .disk($0) }
        }
        if !content.isEmpty || contentLoading || contentFailure != nil {
            let count = contentLoading && content.isEmpty ? "…" : "\(contentTotal ?? content.count)"
            out.append(.header(title: "In your folders", count: count, shortcut: nextShortcut()))
            if content.isEmpty {
                if let failure = contentFailure, failure != .cancelled {
                    out.append(.status(status(for: failure)))
                } else if contentLoading {
                    out.append(.status(.searching))
                }
            }
            out += content.map { .content($0) }
        }
        // the empty state only when nothing is listed at all: no names, no folder matches and no whole-disk hits
        if files.isEmpty && content.isEmpty && !filesLoading && !contentLoading && contentFailure == nil
            && disk.isEmpty && !diskLoading && diskFailure == nil {
            return [.status(.noResults(q)), .status(.hint("Search contents in MySearch ⌘O"))]
        }
        return out
    }

    private func status(for failure: QuickFailure) -> QSStatus {
        switch failure {
        case .offline: return .offline
        case .unauthorized: return .unauthorized
        case .cancelled: return .searching
        case .server(let message): return .failed(message)
        }
    }

    /// The exact-name match of the query (with or without its extension), if the names have one: shown first.
    private func topHit(_ q: String) -> QSFile? {
        let needle = q.lowercased()
        guard !needle.isEmpty else { return nil }
        return files.first { f in
            let name = f.name.lowercased()
            return name == needle || (name as NSString).deletingPathExtension == needle
        }
    }

    // MARK: selection and keys

    /// A row was clicked (or a key chose one): select it, and remember that the user has moved.
    func select(id: String) {
        guard rows.contains(where: { $0.id == id }) else { return }
        listFocused = true
        userMoved = true
        selectedID = id
        onChange?()
    }

    /// ↑ and ↓: the next selectable row. The list is clamped at its ends.
    func moveSelection(_ delta: Int) {
        let ids = rows.compactMap(\.id)
        guard !ids.isEmpty else { return }
        listFocused = true
        userMoved = true
        let current = selectedID.flatMap { ids.firstIndex(of: $0) } ?? (delta > 0 ? -1 : ids.count)
        selectedID = ids[min(max(current + delta, 0), ids.count - 1)]
        scrollRequested = true
        onChange?()
        host?.quickSearchSelectionMoved()
    }

    /// ⌃1, ⌃2, ⌃3: the first result of the section with that number.
    func jumpToSection(_ n: Int) {
        guard let header = rows.firstIndex(where: { row in
            if case .header(_, _, let s) = row { return s == n }
            return false
        }) else { return }
        selectFirstAfter(header)
    }

    /// ⇥ and ⇧⇥: the first result of the next (previous) section, wrapping round.
    func cycleSection(backward: Bool) {
        let headers = rows.indices.filter { rows[$0].isHeader }
        guard !headers.isEmpty else { return }
        let here = rows.firstIndex { $0.id != nil && $0.id == selectedID } ?? 0
        let current = headers.lastIndex { $0 <= here } ?? -1
        let target: Int
        if backward {
            target = current <= 0 ? headers[headers.count - 1] : headers[current - 1]
        } else {
            target = current < 0 || current + 1 >= headers.count ? headers[0] : headers[current + 1]
        }
        selectFirstAfter(target)
    }

    private func selectFirstAfter(_ index: Int) {
        guard let id = rows[(index + 1)...].first(where: { $0.id != nil })?.id else { return }
        listFocused = true
        userMoved = true
        selectedID = id
        scrollRequested = true
        onChange?()
        host?.quickSearchSelectionMoved()
    }

    /// Enter (reveal: false) or ⌘Enter (reveal: true) on the selection.
    func activateSelected(reveal: Bool) {
        guard let row = selectedRow else { return }
        switch row {
        case .file(let f, _):
            open(path: f.path, reveal: reveal)
        case .content(let c):
            open(path: c.path, reveal: reveal)
        case .disk(let d):
            open(path: d.path, reveal: reveal)
        case .recentFile(let r):
            open(path: r.path, reveal: reveal)
        case .recentQuery(let q):
            host?.quickSearchSetField(q)
            setQuery(q)
        case .status(.offline), .status(.unauthorized), .status(.failed(_)):
            retry()
        case .header, .status:
            break
        }
    }

    func openInApp() {
        rememberQuery(query)
        host?.quickSearchOpenInApp(query: query, scope: "all")
    }

    func copySelectedPath() {
        guard let url = selectedPathURL else { return }
        host?.quickSearchCopy(url.path)
        showFlash("Path copied")
    }

    func toggleQuickLook() {
        host?.quickSearchToggleQuickLook(selectedPreviewURL)
    }

    private var selectedPathURL: URL? {
        switch selectedRow {
        case .file(let f, _)?: return URL(fileURLWithPath: f.path)
        case .content(let c)?: return URL(fileURLWithPath: c.path)
        case .disk(let d)?: return URL(fileURLWithPath: d.path)
        case .recentFile(let r)?: return URL(fileURLWithPath: r.path)
        default: return nil
        }
    }

    private func open(path: String, reveal: Bool) {
        let url = URL(fileURLWithPath: path)
        if reveal {
            NSWorkspace.shared.activateFileViewerSelecting([url])
        } else {
            NSWorkspace.shared.open(url)
        }
        rememberQuery(query)
        rememberFile(path)
        let q = query
        Task { await QuickSearchService.opened(path: path, query: q.isEmpty ? nil : q) }
        host?.quickSearchHide()
    }

    private func showFlash(_ text: String) {
        flash = text
        onChange?()
        Task { [weak self] in
            guard await qsSleep(2_500_000_000) else { return }
            guard let self, self.flash == text else { return }
            self.flash = nil
            self.onChange?()
        }
    }

    // MARK: recents (UserDefaults; the snapshot uses fictional ones and never reads or writes the defaults)

    /// Loads the recent queries and files for the empty query. Which files still exist is checked off the main thread.
    func loadRecents() {
        if SnapshotFixture.isActive {
            recentQueries = ["contoso print", "invoice 2025", "tax 2024"]
            // under the fixture's home (NSHomeDirectory is the fixture's home in snapshot runs), so it shows as ~/…
            let model = NSHomeDirectory() + "/Documents/Studio/Projects/Alpha/app/Model.swift"
            recentFiles = [QSRecentFile(path: model, name: (model as NSString).lastPathComponent)]
            rebuildIfEmpty()
            return
        }
        let defaults = UserDefaults.standard
        recentQueries = defaults.stringArray(forKey: "quickSearch.recentQueries") ?? []
        recentFiles = (defaults.stringArray(forKey: "quickSearch.recentFiles") ?? []).map {
            QSRecentFile(path: $0, name: ($0 as NSString).lastPathComponent)
        }
        rebuildIfEmpty()
        let paths = recentFiles.map(\.path)
        Task { [weak self] in
            let alive = await Task.detached(priority: .utility) {
                paths.filter { FileManager.default.fileExists(atPath: $0) }
            }.value
            guard let self, self.query.trimmingCharacters(in: .whitespaces).isEmpty else { return }
            self.recentFiles = self.recentFiles.filter { alive.contains($0.path) }
            self.rebuildIfEmpty()
        }
    }

    private func rebuildIfEmpty() {
        if query.trimmingCharacters(in: .whitespaces).isEmpty { rebuild() }
    }

    private func rememberQuery(_ q: String) {
        let t = q.trimmingCharacters(in: .whitespaces)
        guard !t.isEmpty, !SnapshotFixture.isActive else { return }
        var list = UserDefaults.standard.stringArray(forKey: "quickSearch.recentQueries") ?? []
        list.removeAll { $0 == t }
        list.insert(t, at: 0)
        list = Array(list.prefix(8))
        UserDefaults.standard.set(list, forKey: "quickSearch.recentQueries")
        recentQueries = list
    }

    private func rememberFile(_ path: String) {
        guard !SnapshotFixture.isActive else { return }
        var list = UserDefaults.standard.stringArray(forKey: "quickSearch.recentFiles") ?? []
        list.removeAll { $0 == path }
        list.insert(path, at: 0)
        list = Array(list.prefix(8))
        UserDefaults.standard.set(list, forKey: "quickSearch.recentFiles")
        recentFiles = list.map { QSRecentFile(path: $0, name: ($0 as NSString).lastPathComponent) }
    }

    // MARK: called by the window

    /// The window reloaded the list and it is on screen: the timing of the keystroke ends here.
    func didPaint() {
        timing.painted()
    }
}
