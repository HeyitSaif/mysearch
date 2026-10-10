// File operations for the folder views, with undo: copy, cut, paste, duplicate, rename, new folder, make alias, move
// to Trash, delete immediately, compress, extract, colour tags, and drag-and-drop move or copy between folders.
// Every operation is registered with the window's UndoManager (one per window), so Undo and Redo (⌘Z, ⇧⌘Z) work
// for all of them.
//
// Safety rules (binding):
//   - Move to Trash is the default delete. "Delete Immediately…" is only in the File menu (⌥⌘⌫), and its alert has
//     Cancel as the default button.
//   - Undo of a copy, duplicate, alias, new folder, compress or extract moves the created items to the Trash. Nothing
//     here calls removeItem except the confirmed Delete Immediately.
//   - Paste, move, duplicate, compress and extract never overwrite: a taken name gets " 2" (" 3", ...) like Finder.
//     A rename to a name that is taken is refused.
//   - Every error is shown in an alert. A partly failed operation reports what failed, and the undo record holds
//     only what really happened.
//
// Ported from FinderFlow v1.5.2 (https://github.com/Gtarafdar/FinderFlow, MIT, (c) 2026 Gobinda Tarafdar),
// FinderFlow/FolderCreationService.swift: the unique-name rule (uniqueURL: "name 2.ext"), the confirmed permanent
// delete with Cancel as the second choice (here Cancel is the default), the "name alias" naming and the pasteboard
// handling for cut and copy. The licence text is in THIRD_PARTY_NOTICES.md at the project root. Everything else
// (undo records, background work, the archive handling, drag and drop) is written for MySearch.

import Cocoa
import UniformTypeIdentifiers

// MARK: - plain types

/// Where an operation reports to: the window (alerts appear as its sheet) and the status line.
struct FileOpContext {
    weak var window: NSWindow?
    var status: (String) -> Void
}

struct FileOpFailure: Sendable {
    let name: String
    let message: String
}

/// One undoable step. A move: `home` is where the item was, `alt` where it went. A trash: `home` is the original
/// place, `alt` the item's place in the Trash. A create: `home` is the new item, `alt` its place in the Trash after
/// an undo.
final class UndoRecord: NSObject {
    enum Kind { case move, trash, create }
    struct Entry {
        var home: URL
        var alt: URL?
    }
    struct TagChange {
        let url: URL
        let old: [String]
        let new: [String]
    }
    enum Payload {
        case files(Kind, [Entry])
        case tags([TagChange])
    }

    let name: String
    var payload: Payload
    /// True while the change is in effect (the next toggle undoes it); false once it has been undone.
    var applied = true
    var manager: UndoManager?

    init(name: String, payload: Payload) {
        self.name = name
        self.payload = payload
    }
}

// MARK: - work that runs off the main thread

enum FileWork {
    struct Pair: Sendable {
        let from: URL
        let to: URL
    }
    struct Outcome: Sendable {
        var pairs: [Pair] = []
        var failures: [FileOpFailure] = []
    }
    struct ProcessResult: Sendable {
        let status: Int32
        let message: String
    }

    private static let compoundExtensions = [".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst"]

    /// "name" and "ext" of a file name, treating ".tar.gz" as one extension.
    static func split(_ name: String) -> (stem: String, ext: String) {
        let lower = name.lowercased()
        for compound in compoundExtensions where lower.hasSuffix(compound) && name.count > compound.count {
            return (String(name.dropLast(compound.count)), String(name.suffix(compound.count - 1)))
        }
        let ns = name as NSString
        return (ns.deletingPathExtension, ns.pathExtension)
    }

    static func exists(_ url: URL) -> Bool {
        (try? FileManager.default.attributesOfItem(atPath: url.path)) != nil       // lstat: a dangling link counts
    }

    /// Finder's rule: a name that is taken gets " 2", " 3", ... before the extension. (From FinderFlow's uniqueURL.)
    static func uniqueURL(for base: URL) -> URL {
        var url = base
        var n = 2
        let (stem, ext) = split(base.lastPathComponent)
        let dir = base.deletingLastPathComponent()
        while exists(url) {
            let name = ext.isEmpty ? "\(stem) \(n)" : "\(stem) \(n).\(ext)"
            url = dir.appendingPathComponent(name)
            n += 1
        }
        return url
    }

    /// True when `dir` is `item` or lies inside it (a folder cannot go into itself).
    static func isInside(_ dir: URL, of item: URL) -> Bool {
        let d = dir.resolvingSymlinksInPath().path
        let i = item.resolvingSymlinksInPath().path
        return d == i || d.hasPrefix(i.hasSuffix("/") ? i : i + "/")
    }

    /// Moves without ever replacing: FileManager refuses a name that is taken. A change of letter case alone
    /// (a.txt to A.txt) goes through a temporary name, because the volume sees both names as one.
    static func move(_ from: URL, _ to: URL) throws {
        let fm = FileManager.default
        if from.path != to.path && from.path.lowercased() == to.path.lowercased() {
            let temp = from.deletingLastPathComponent().appendingPathComponent(".mysearch-rename-" + UUID().uuidString)
            try fm.moveItem(at: from, to: temp)
            do {
                try fm.moveItem(at: temp, to: to)
            } catch {
                try? fm.moveItem(at: temp, to: from)          // put it back; the error that matters is rethrown
                throw error
            }
            return
        }
        try fm.moveItem(at: from, to: to)
    }

    /// Copies or moves each source into `dir` under a free name.
    static func transfer(_ sources: [URL], into dir: URL, copy: Bool) -> Outcome {
        var out = Outcome()
        let fm = FileManager.default
        for src in sources {
            let alreadyThere = src.deletingLastPathComponent().resolvingSymlinksInPath().path == dir.resolvingSymlinksInPath().path
            if !copy && alreadyThere { continue }
            if isInside(dir, of: src) {
                out.failures.append(FileOpFailure(name: src.lastPathComponent, message: "A folder cannot be put inside itself."))
                continue
            }
            let dest = uniqueURL(for: dir.appendingPathComponent(src.lastPathComponent))
            do {
                if copy { try fm.copyItem(at: src, to: dest) } else { try move(src, dest) }
                out.pairs.append(Pair(from: src, to: dest))
            } catch {
                out.failures.append(FileOpFailure(name: src.lastPathComponent, message: error.localizedDescription))
            }
        }
        return out
    }

    /// Moves each item to the Trash. `to` is the item's place in the Trash.
    static func trash(_ urls: [URL]) -> Outcome {
        var out = Outcome()
        for url in urls {
            do {
                var result: NSURL?
                try FileManager.default.trashItem(at: url, resultingItemURL: &result)
                out.pairs.append(Pair(from: url, to: (result as URL?) ?? url))
            } catch {
                out.failures.append(FileOpFailure(name: url.lastPathComponent, message: error.localizedDescription))
            }
        }
        return out
    }

    /// Runs a command-line tool and waits for it. `output` receives the tool's standard output (for gzip and friends).
    static func run(_ executable: String, _ arguments: [String], output: FileHandle? = nil) -> ProcessResult {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = arguments
        let errors = Pipe()
        process.standardError = errors
        if let output { process.standardOutput = output } else { process.standardOutput = FileHandle.nullDevice }
        do {
            try process.run()
        } catch {
            return ProcessResult(status: -1, message: error.localizedDescription)
        }
        let data = errors.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        var text = String(data: data.prefix(2000), encoding: .utf8) ?? ""
        text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        return ProcessResult(status: process.terminationStatus, message: text.isEmpty ? "exit status \(process.terminationStatus)" : text)
    }
}

// MARK: - the service

@MainActor
final class FileOpsService: NSObject {
    static let shared = FileOpsService()
    /// Posted after anything changed on disk. userInfo: "select" ([URL] to select afterwards), "rename" (Bool: start
    /// an inline rename of the one selected item).
    static let changed = Notification.Name("MSEFileOpsChanged")

    private var lastContext: FileOpContext?
    /// The pasteboard change count of the last Cut. A Paste that finds the same count moves; otherwise it copies.
    private var cutChangeCount: Int?

    // MARK: pasteboard (cut, copy, paste)

    func copy(_ urls: [URL]) {
        guard !urls.isEmpty else { return }
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        pasteboard.writeObjects(urls.map { $0 as NSURL })
        cutChangeCount = nil
    }

    /// Cut only marks the items: nothing moves until Paste, and a Copy elsewhere cancels it.
    func cut(_ urls: [URL], ctx: FileOpContext) {
        guard !urls.isEmpty else { return }
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        pasteboard.writeObjects(urls.map { $0 as NSURL })
        cutChangeCount = pasteboard.changeCount
        ctx.status(urls.count == 1 ? "Cut 1 item. Paste moves it." : "Cut \(urls.count) items. Paste moves them.")
    }

    var hasPasteableFiles: Bool {
        NSPasteboard.general.canReadObject(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true])
    }

    func paste(into dir: URL, ctx: FileOpContext) {
        let pasteboard = NSPasteboard.general
        let urls = (pasteboard.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL]) ?? []
        guard !urls.isEmpty else {
            ctx.status("There is nothing to paste.")
            return
        }
        let isCut = cutChangeCount == pasteboard.changeCount
        transfer(urls, into: dir, copy: !isCut, ctx: ctx)
        if isCut { cutChangeCount = nil }
    }

    // MARK: copy and move (paste, drag and drop)

    func transfer(_ urls: [URL], into dir: URL, copy: Bool, ctx: FileOpContext) {
        lastContext = ctx
        let existing = urls.filter { FileWork.exists($0) }
        let missing = urls.filter { !FileWork.exists($0) }.map { FileOpFailure(name: $0.lastPathComponent, message: "The item is no longer there.") }
        guard !existing.isEmpty else {
            report(copy ? "Nothing was copied" : "Nothing was moved", missing, ctx)
            return
        }
        ctx.status((copy ? "Copying " : "Moving ") + (existing.count == 1 ? "1 item…" : "\(existing.count) items…"))
        Task { @MainActor in
            let outcome = await Task.detached(priority: .userInitiated) {
                FileWork.transfer(existing, into: dir, copy: copy)
            }.value
            if !outcome.pairs.isEmpty {
                let record: UndoRecord
                if copy {
                    record = UndoRecord(name: "Copy", payload: .files(.create, outcome.pairs.map { UndoRecord.Entry(home: $0.to, alt: nil) }))
                } else {
                    record = UndoRecord(name: "Move", payload: .files(.move, outcome.pairs.map { UndoRecord.Entry(home: $0.from, alt: $0.to) }))
                }
                register(record, ctx)
                ctx.status((copy ? "Copied " : "Moved ") + (outcome.pairs.count == 1 ? "1 item." : "\(outcome.pairs.count) items."))
            } else {
                ctx.status("")
            }
            announce(select: outcome.pairs.map(\.to))
            report(copy ? "Not everything was copied" : "Not everything was moved", missing + outcome.failures, ctx)
        }
    }

    func duplicate(_ urls: [URL], ctx: FileOpContext) {
        lastContext = ctx
        guard !urls.isEmpty else { return }
        ctx.status("Duplicating…")
        Task { @MainActor in
            var created: [URL] = []
            var failures: [FileOpFailure] = []
            for url in urls {
                let outcome = await Task.detached(priority: .userInitiated) {
                    FileWork.transfer([url], into: url.deletingLastPathComponent(), copy: true)
                }.value
                created += outcome.pairs.map(\.to)
                failures += outcome.failures
            }
            if !created.isEmpty {
                register(UndoRecord(name: "Duplicate", payload: .files(.create, created.map { UndoRecord.Entry(home: $0, alt: nil) })), ctx)
            }
            ctx.status(created.isEmpty ? "" : (created.count == 1 ? "Duplicated 1 item." : "Duplicated \(created.count) items."))
            announce(select: created)
            report("Not everything was duplicated", failures, ctx)
        }
    }

    // MARK: new folder, alias, rename

    /// Creates "untitled folder" in `dir` and asks the view to start renaming it.
    func newFolder(in dir: URL, ctx: FileOpContext) {
        lastContext = ctx
        let url = FileWork.uniqueURL(for: dir.appendingPathComponent("untitled folder"))
        do {
            try FileManager.default.createDirectory(at: url, withIntermediateDirectories: false)
        } catch {
            report("The folder was not created", [FileOpFailure(name: url.lastPathComponent, message: error.localizedDescription)], ctx)
            return
        }
        register(UndoRecord(name: "New Folder", payload: .files(.create, [UndoRecord.Entry(home: url, alt: nil)])), ctx)
        announce(select: [url], rename: true)
    }

    /// A Finder alias ("name alias") next to each item.
    func makeAlias(_ urls: [URL], ctx: FileOpContext) {
        lastContext = ctx
        var created: [URL] = []
        var failures: [FileOpFailure] = []
        for url in urls {
            let dest = FileWork.uniqueURL(for: url.deletingLastPathComponent().appendingPathComponent(url.lastPathComponent + " alias"))
            do {
                let data = try url.bookmarkData(options: .suitableForBookmarkFile, includingResourceValuesForKeys: nil, relativeTo: nil)
                try URL.writeBookmarkData(data, to: dest)
                created.append(dest)
            } catch {
                failures.append(FileOpFailure(name: url.lastPathComponent, message: error.localizedDescription))
            }
        }
        if !created.isEmpty {
            register(UndoRecord(name: "Make Alias", payload: .files(.create, created.map { UndoRecord.Entry(home: $0, alt: nil) })), ctx)
            announce(select: created)
        }
        report("Not every alias was made", failures, ctx)
    }

    /// Renames `url` to `newName`. A name that is taken is refused (nothing is replaced).
    func rename(_ url: URL, to newName: String, ctx: FileOpContext) {
        lastContext = ctx
        let name = newName.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty, name != url.lastPathComponent else {
            announce(select: [url])
            return
        }
        func refuse(_ why: String) {
            report("The item was not renamed", [FileOpFailure(name: url.lastPathComponent, message: why)], ctx)
            announce(select: [url])
        }
        if name.contains("/") { return refuse("A name cannot contain a slash.") }
        if name == "." || name == ".." { return refuse("That name cannot be used.") }
        let dest = url.deletingLastPathComponent().appendingPathComponent(name)
        let sameNameInOtherCase = dest.path.lowercased() == url.path.lowercased()
        if !sameNameInOtherCase && FileWork.exists(dest) { return refuse("The name “\(name)” is already taken. Choose a different name.") }
        do {
            try FileWork.move(url, dest)
        } catch {
            return refuse(error.localizedDescription)
        }
        register(UndoRecord(name: "Rename", payload: .files(.move, [UndoRecord.Entry(home: url, alt: dest)])), ctx)
        announce(select: [dest])
    }

    // MARK: Trash and permanent delete

    func trash(_ urls: [URL], ctx: FileOpContext) {
        lastContext = ctx
        guard !urls.isEmpty else { return }
        ctx.status("Moving to the Trash…")
        Task { @MainActor in
            let outcome = await Task.detached(priority: .userInitiated) { FileWork.trash(urls) }.value
            if !outcome.pairs.isEmpty {
                register(UndoRecord(name: "Move to Trash", payload: .files(.trash, outcome.pairs.map { UndoRecord.Entry(home: $0.from, alt: $0.to) })), ctx)
                ctx.status(outcome.pairs.count == 1 ? "Moved 1 item to the Trash." : "Moved \(outcome.pairs.count) items to the Trash.")
            } else {
                ctx.status("")
            }
            announce(select: [])
            report("Not everything was moved to the Trash", outcome.failures, ctx)
        }
    }

    /// Deletes for good, after an alert whose default button (Return) is Cancel. Cannot be undone. Only the File
    /// menu offers it (⌥⌘⌫); there is no toolbar button and no context-menu item.
    func deleteImmediately(_ urls: [URL], ctx: FileOpContext) {
        lastContext = ctx
        guard !urls.isEmpty else { return }
        let label = urls.count == 1 ? "“\(urls[0].lastPathComponent)”" : "\(urls.count) items"
        let alert = NSAlert()
        alert.alertStyle = .critical
        alert.messageText = "Delete \(label) immediately?"
        alert.informativeText = "This cannot be undone. \(urls.count == 1 ? "The item" : "The items") will not go to the Trash and cannot be recovered."
        alert.addButton(withTitle: "Cancel")                                    // the first button is the default (Return)
        let delete = alert.addButton(withTitle: "Delete Immediately")
        delete.hasDestructiveAction = true
        delete.keyEquivalent = ""
        present(alert, in: ctx.window) { [weak self] response in
            guard response == .alertSecondButtonReturn, let self else { return }
            self.performDeleteImmediately(urls, ctx)
        }
    }

    private func performDeleteImmediately(_ urls: [URL], _ ctx: FileOpContext) {
        ctx.status("Deleting…")
        Task { @MainActor in
            let failures = await Task.detached(priority: .userInitiated) { () -> [FileOpFailure] in
                var failed: [FileOpFailure] = []
                for url in urls {
                    do { try FileManager.default.removeItem(at: url) }
                    catch { failed.append(FileOpFailure(name: url.lastPathComponent, message: error.localizedDescription)) }
                }
                return failed
            }.value
            ctx.status(failures.count == urls.count ? "" : "Deleted \(urls.count - failures.count) of \(urls.count).")
            announce(select: [])
            report("Not everything was deleted", failures, ctx)
        }
    }

    // MARK: archives

    func compress(_ urls: [URL], ctx: FileOpContext) {
        lastContext = ctx
        guard !urls.isEmpty else { return }
        if urls.count > 1 { compressTogether(urls, ctx: ctx); return }
        Task { @MainActor in
            var created: [URL] = []
            var failures: [FileOpFailure] = []
            for (index, src) in urls.enumerated() {
                ctx.status(urls.count == 1 ? "Compressing “\(src.lastPathComponent)”…" : "Compressing \(index + 1) of \(urls.count)…")
                let dest = FileWork.uniqueURL(for: src.deletingLastPathComponent().appendingPathComponent(src.lastPathComponent + ".zip"))
                let result = await Task.detached(priority: .userInitiated) {
                    FileWork.run("/usr/bin/ditto", ["-c", "-k", "--keepParent", src.path, dest.path])
                }.value
                if FileWork.exists(dest) { created.append(dest) }                // even a partial zip is ours: undo can trash it
                if result.status != 0 { failures.append(FileOpFailure(name: src.lastPathComponent, message: result.message)) }
            }
            if !created.isEmpty {
                register(UndoRecord(name: "Compress", payload: .files(.create, created.map { UndoRecord.Entry(home: $0, alt: nil) })), ctx)
            }
            let done = created.count - failures.count
            ctx.status(done > 0 ? (done == 1 ? "Compressed 1 item." : "Compressed \(done) items.") : "")
            announce(select: created)
            report("Not everything was compressed", failures, ctx)
        }
    }

    /// Several items: ONE archive, Archive.zip (Archive 2.zip when taken), in the folder of the first item, with every
    /// item at the root of the zip. The items are cloned (cp -c: a copy on APFS, a plain copy elsewhere) into a staging
    /// folder of ours under NSTemporaryDirectory, zipped from there, and the staging folder is removed. Nothing the user
    /// selected is moved or deleted. If any item cannot be staged, no archive is made, so none is missing an item.
    private func compressTogether(_ urls: [URL], ctx: FileOpContext) {
        ctx.status("Compressing \(urls.count) items…")
        let dest = FileWork.uniqueURL(for: urls[0].deletingLastPathComponent().appendingPathComponent("Archive.zip"))
        Task { @MainActor in
            let staging = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
                .appendingPathComponent("MySearch-compress-\(UUID().uuidString)", isDirectory: true)
            let failures = await Task.detached(priority: .userInitiated) { () -> [FileOpFailure] in
                defer {                       // removed off the main actor; only our own staging folder, inside the temporary directory
                    if staging.lastPathComponent.hasPrefix("MySearch-compress-"), staging.path.hasPrefix(NSTemporaryDirectory()) {
                        try? FileManager.default.removeItem(at: staging)
                    }
                }
                do { try FileManager.default.createDirectory(at: staging, withIntermediateDirectories: true) }
                catch { return [FileOpFailure(name: "Archive.zip", message: error.localizedDescription)] }
                var failed: [FileOpFailure] = []
                for src in urls {
                    let copy = FileWork.uniqueURL(for: staging.appendingPathComponent(src.lastPathComponent))   // same names clash
                    // -c clones on APFS and falls back to a plain copy on other filesystems (cp(1), copyfile(2))
                    let result = FileWork.run("/bin/cp", ["-R", "-c", src.path, copy.path])
                    if result.status != 0 { failed.append(FileOpFailure(name: src.lastPathComponent, message: result.message)) }
                }
                guard failed.isEmpty else { return failed }
                // no --keepParent: the staging folder's contents (the selected items) go to the root of the zip
                let zip = FileWork.run("/usr/bin/ditto", ["-c", "-k", "--sequesterRsrc", staging.path, dest.path])
                if zip.status != 0 { failed.append(FileOpFailure(name: "Archive.zip", message: zip.message)) }
                return failed
            }.value
            let made = FileWork.exists(dest)                                  // even a partial archive is ours: undo can trash it
            if made {
                register(UndoRecord(name: "Compress", payload: .files(.create, [UndoRecord.Entry(home: dest, alt: nil)])), ctx)
            }
            ctx.status(made && failures.isEmpty ? "Compressed \(urls.count) items." : "")
            announce(select: made ? [dest] : [])
            report("Not everything was compressed", failures, ctx)
        }
    }

    enum ArchiveKind { case zip, tar, gzip, bzip2, xz }

    /// The archive type of a file name, or nil when Extract does not handle it.
    static func archiveKind(of url: URL) -> ArchiveKind? {
        let name = url.lastPathComponent.lowercased()
        for suffix in [".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2", ".tbz", ".txz", ".tar"] where name.hasSuffix(suffix) { return .tar }
        if name.hasSuffix(".zip") { return .zip }
        if name.hasSuffix(".gz") { return .gzip }
        if name.hasSuffix(".bz2") { return .bzip2 }
        if name.hasSuffix(".xz") { return .xz }
        return nil
    }

    /// The folder name an archive extracts into: the archive's name without its extension.
    private static func extractionName(_ url: URL) -> String {
        let name = url.lastPathComponent
        let lower = name.lowercased()
        for suffix in [".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2", ".tbz", ".txz"] where lower.hasSuffix(suffix) {
            return String(name.dropLast(suffix.count))
        }
        let stem = (name as NSString).deletingPathExtension
        return stem.isEmpty ? name : stem
    }

    /// Extracts each archive into a NEW folder named after it (" 2" if that exists). Nothing is overwritten.
    func extract(_ urls: [URL], ctx: FileOpContext) {
        lastContext = ctx
        let archives = urls.filter { Self.archiveKind(of: $0) != nil }
        guard !archives.isEmpty else {
            report("Nothing to extract", [FileOpFailure(name: urls.first?.lastPathComponent ?? "", message: "Extract handles .zip, .tar, .gz, .tgz, .bz2 and .xz files.")], ctx)
            return
        }
        Task { @MainActor in
            var created: [URL] = []
            var failures: [FileOpFailure] = []
            for (index, src) in archives.enumerated() {
                ctx.status(archives.count == 1 ? "Extracting “\(src.lastPathComponent)”…" : "Extracting \(index + 1) of \(archives.count)…")
                let folder = FileWork.uniqueURL(for: src.deletingLastPathComponent().appendingPathComponent(Self.extractionName(src)))
                do {
                    try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: false)
                } catch {
                    failures.append(FileOpFailure(name: src.lastPathComponent, message: error.localizedDescription))
                    continue
                }
                created.append(folder)
                let kind = Self.archiveKind(of: src) ?? .zip
                let stem = Self.extractionName(src)
                let result = await Task.detached(priority: .userInitiated) {
                    Self.extractArchive(src, kind: kind, into: folder, singleFileName: stem)
                }.value
                if result.status != 0 {
                    failures.append(FileOpFailure(name: src.lastPathComponent, message: result.message + " The folder “\(folder.lastPathComponent)” was kept; undo moves it to the Trash."))
                }
            }
            if !created.isEmpty {
                register(UndoRecord(name: "Extract", payload: .files(.create, created.map { UndoRecord.Entry(home: $0, alt: nil) })), ctx)
            }
            let done = created.count - failures.count
            ctx.status(done > 0 ? (done == 1 ? "Extracted 1 archive." : "Extracted \(done) archives.") : "")
            announce(select: created)
            report("Not everything was extracted", failures, ctx)
        }
    }

    /// Runs the extraction into the (new, empty) `folder`. ditto for .zip, tar for the tar family; a plain .gz, .bz2
    /// or .xz that is not a tar is decompressed to one file. Never `unzip -o`.
    private nonisolated static func extractArchive(_ src: URL, kind: ArchiveKind, into folder: URL, singleFileName: String) -> FileWork.ProcessResult {
        switch kind {
        case .zip:
            return FileWork.run("/usr/bin/ditto", ["-x", "-k", src.path, folder.path])
        case .tar:
            return FileWork.run("/usr/bin/tar", ["-x", "-f", src.path, "-C", folder.path])
        case .gzip, .bzip2, .xz:
            let tried = FileWork.run("/usr/bin/tar", ["-x", "-f", src.path, "-C", folder.path])
            if tried.status == 0 { return tried }
            let leftover = (try? FileManager.default.contentsOfDirectory(atPath: folder.path)) ?? []
            if !leftover.isEmpty { return tried }                                  // tar did extract something and then failed
            let tool: String
            switch kind {
            case .gzip: tool = "/usr/bin/gzip"
            case .bzip2: tool = "/usr/bin/bzip2"
            default: tool = "/usr/bin/xz"
            }
            guard FileManager.default.isExecutableFile(atPath: tool) else {
                return FileWork.ProcessResult(status: 1, message: "This Mac has no \((tool as NSString).lastPathComponent) tool to unpack the file.")
            }
            let out = folder.appendingPathComponent(singleFileName)
            guard FileManager.default.createFile(atPath: out.path, contents: nil), let handle = try? FileHandle(forWritingTo: out) else {
                return FileWork.ProcessResult(status: 1, message: "The output file could not be created.")
            }
            defer { try? handle.close() }
            return FileWork.run(tool, ["-d", "-c", src.path], output: handle)
        }
    }

    // MARK: tags

    /// Toggles one colour for the items, as Finder does: removed from all when every item has it, otherwise added
    /// to all. The file's other tags stay as they are.
    func toggleTag(_ color: TagColor, on urls: [URL], ctx: FileOpContext) {
        lastContext = ctx
        guard !urls.isEmpty else { return }
        let before = urls.map { ($0, TagStore.storedTags(of: $0)) }
        let allHave = before.allSatisfy { TagStore.colors(of: $0.1).contains(color) }
        applyTags(before.map { ($0.0, $0.1, TagStore.toggled($0.1, color: color, adding: !allHave)) },
                  name: allHave ? "Remove Tag" : "Add Tag", ctx: ctx)
    }

    func clearTags(on urls: [URL], ctx: FileOpContext) {
        lastContext = ctx
        let before = urls.map { ($0, TagStore.storedTags(of: $0)) }.filter { !$0.1.isEmpty }
        applyTags(before.map { ($0.0, $0.1, []) }, name: "Clear Tags", ctx: ctx)
    }

    private func applyTags(_ changes: [(URL, [String], [String])], name: String, ctx: FileOpContext) {
        var done: [UndoRecord.TagChange] = []
        var failures: [FileOpFailure] = []
        for (url, old, new) in changes where old != new {
            do {
                try TagStore.write(new, to: url)
                done.append(UndoRecord.TagChange(url: url, old: old, new: new))
            } catch {
                failures.append(FileOpFailure(name: url.lastPathComponent, message: error.localizedDescription))
            }
        }
        if !done.isEmpty {
            register(UndoRecord(name: name, payload: .tags(done)), ctx)
            announce(select: done.map(\.url))
        }
        report("Not every tag was changed", failures, ctx)
    }

    // MARK: undo and redo

    private func register(_ record: UndoRecord, _ ctx: FileOpContext) {
        guard let manager = ctx.window?.undoManager ?? NSApp.keyWindow?.undoManager ?? NSApp.mainWindow?.undoManager else { return }
        record.manager = manager
        manager.registerUndo(withTarget: self, selector: #selector(toggleRecord(_:)), object: record)
        manager.setActionName(record.name)
    }

    /// Called by the UndoManager for an undo and again for the redo it registers. Whatever cannot be done is
    /// reported and left out of the record that goes back on the other stack.
    @objc func toggleRecord(_ record: UndoRecord) {
        let ctx = lastContext ?? FileOpContext(window: NSApp.keyWindow, status: { _ in })
        let undoing = record.applied
        var failures: [FileOpFailure] = []
        var select: [URL] = []
        var anyDone = false
        switch record.payload {
        case .files(let kind, let entries):
            var kept: [UndoRecord.Entry] = []
            for var entry in entries {
                do {
                    try step(&entry, kind: kind, toOriginal: undoing)
                    kept.append(entry)
                    switch (kind, undoing) {
                    case (.move, true), (.trash, true): select.append(entry.home)
                    case (.move, false): if let alt = entry.alt { select.append(alt) }
                    case (.create, false): select.append(entry.home)
                    default: break
                    }
                } catch {
                    failures.append(FileOpFailure(name: entry.home.lastPathComponent, message: error.localizedDescription))
                }
            }
            record.payload = .files(kind, kept)
            anyDone = !kept.isEmpty
        case .tags(let changes):
            var kept: [UndoRecord.TagChange] = []
            for change in changes {
                do {
                    try TagStore.write(undoing ? change.old : change.new, to: change.url)
                    kept.append(change)
                    select.append(change.url)
                } catch {
                    failures.append(FileOpFailure(name: change.url.lastPathComponent, message: error.localizedDescription))
                }
            }
            record.payload = .tags(kept)
            anyDone = !kept.isEmpty
        }
        if anyDone {
            record.applied = !undoing
            if let manager = record.manager {
                manager.registerUndo(withTarget: self, selector: #selector(toggleRecord(_:)), object: record)
                manager.setActionName(record.name)
            }
        }
        announce(select: select)
        report(undoing ? "Not everything was undone" : "Not everything was redone", failures, ctx)
    }

    private func step(_ entry: inout UndoRecord.Entry, kind: UndoRecord.Kind, toOriginal: Bool) throws {
        func needAlt() throws -> URL {
            guard let alt = entry.alt else { throw CocoaError(.fileNoSuchFile) }
            return alt
        }
        switch (kind, toOriginal) {
        case (.move, true), (.trash, true):
            try FileWork.move(try needAlt(), entry.home)
        case (.move, false):
            try FileWork.move(entry.home, try needAlt())
        case (.trash, false), (.create, true):
            var result: NSURL?
            try FileManager.default.trashItem(at: entry.home, resultingItemURL: &result)       // never removeItem
            entry.alt = result as URL?
        case (.create, false):
            try FileWork.move(try needAlt(), entry.home)
        }
    }

    // MARK: reporting

    private func announce(select: [URL], rename: Bool = false) {
        NotificationCenter.default.post(name: Self.changed, object: nil, userInfo: ["select": select, "rename": rename])
    }

    func report(_ title: String, _ failures: [FileOpFailure], _ ctx: FileOpContext) {
        guard !failures.isEmpty else { return }
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = title
        var lines = failures.prefix(6).map { "\($0.name): \($0.message)" }
        if failures.count > 6 { lines.append("…and \(failures.count - 6) more.") }
        alert.informativeText = lines.joined(separator: "\n")
        alert.addButton(withTitle: "OK")
        present(alert, in: ctx.window, completion: nil)
    }

    /// A text prompt for a new name (the column view, which has no inline rename). Returns nil on Cancel.
    func promptName(title: String, message: String, current: String, button: String, in window: NSWindow?,
                    completion: @escaping (String) -> Void) {
        let field = NSTextField(string: current)
        field.frame = NSRect(x: 0, y: 0, width: 280, height: 24)
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.accessoryView = field
        alert.addButton(withTitle: button)
        alert.addButton(withTitle: "Cancel")
        alert.window.initialFirstResponder = field
        present(alert, in: window) { response in
            if response == .alertFirstButtonReturn { completion(field.stringValue) }
        }
    }

    private func present(_ alert: NSAlert, in window: NSWindow?, completion: ((NSApplication.ModalResponse) -> Void)?) {
        if let window, window.isVisible {
            alert.beginSheetModal(for: window) { response in completion?(response) }
        } else {
            completion?(alert.runModal())
        }
    }
}

// MARK: - opening: text and code files go to the editor

enum FileOpener {
    /// Observed by the editor. Posted instead of NSWorkspace.open for markdown, plain-text and code files.
    static let editorNotification = Notification.Name("MSEOpenInEditor")

    private static let notText: Set<String> = [
        "rtf", "rtfd", "pdf", "doc", "docx", "docm", "dot", "dotx", "odt", "pages", "key", "numbers", "xls", "xlsx",
        "ppt", "pptx", "webarchive", "epub", "plist", "ps", "eps", "svgz",
    ]
    private static let textExtensions: Set<String> = [
        "md", "markdown", "mdown", "mkd", "txt", "text", "log", "json", "jsonl", "yaml", "yml", "toml", "ini", "cfg",
        "conf", "env", "csv", "tsv", "xml", "html", "htm", "css", "scss", "sass", "less", "js", "mjs", "cjs", "jsx",
        "ts", "tsx", "vue", "svelte", "py", "rb", "php", "java", "kt", "kts", "swift", "m", "mm", "h", "hpp", "c",
        "cc", "cpp", "cxx", "cs", "go", "rs", "sh", "bash", "zsh", "fish", "ps1", "bat", "sql", "graphql", "gql", "lua",
        "pl", "r", "scala", "dart", "ex", "exs", "erl", "hs", "clj", "tex", "bib", "rst", "adoc", "org", "gradle",
        "properties", "gitignore", "dockerignore", "editorconfig", "tf", "proto", "diff", "patch",
    ]
    private static let textNames: Set<String> = [
        "makefile", "dockerfile", "readme", "license", "changelog", "gemfile", "rakefile", "procfile", ".gitignore",
        ".env", ".zshrc", ".bashrc", ".zprofile", ".gitattributes", ".editorconfig",
    ]

    /// Markdown, plain-text and code files. Rich text, PDF and Office files are not.
    static func isTextFile(_ url: URL) -> Bool {
        let ext = url.pathExtension.lowercased()
        if notText.contains(ext) { return false }
        if textExtensions.contains(ext) { return true }
        if ext.isEmpty { return textNames.contains(url.lastPathComponent.lowercased()) }
        if let type = UTType(filenameExtension: ext), type.conforms(to: .text), !type.conforms(to: .rtf) { return true }
        return false
    }

    /// Opens files: text and code go to the editor, anything else to its default app.
    static func open(_ urls: [URL]) {
        for url in urls {
            if isTextFile(url) {
                NotificationCenter.default.post(name: editorNotification, object: url)
            } else {
                NSWorkspace.shared.open(url)
            }
        }
    }
}

// MARK: - status line text

enum FolderStatus {
    private static let sizeFormatter: ByteCountFormatter = {
        let f = ByteCountFormatter()
        f.countStyle = .file
        return f
    }()

    /// "12 items, 142 GB available" or "3 of 12 selected, 4 MB, 142 GB available" (FinderFlow's status bar).
    /// `total` is nil when the selection is not in the last column ("3 selected").
    static func text(total: Int?, selected: [FileItem], folder: URL?) -> String {
        var parts: [String] = []
        if selected.isEmpty {
            let n = total ?? 0
            parts.append(n == 1 ? "1 item" : "\(n) items")
        } else {
            var sel = total.map { "\(selected.count) of \($0) selected" } ?? "\(selected.count) selected"
            if selected.contains(where: { !$0.isDir }) {
                let bytes = selected.reduce(Int64(0)) { $0 + ($1.isDir ? 0 : ($1.size ?? 0)) }
                sel += ", " + sizeFormatter.string(fromByteCount: bytes)
            }
            parts.append(sel)
        }
        let probe = folder ?? FileManager.default.homeDirectoryForCurrentUser
        if let free = (try? probe.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey]))?.volumeAvailableCapacityForImportantUsage {
            parts.append(sizeFormatter.string(fromByteCount: free) + " available")
        }
        return parts.joined(separator: ", ")
    }
}

// MARK: - drag and drop between folders inside the app

@MainActor
enum DropPlan {
    /// The files of a drag that started inside this app (dragging from Finder is not accepted).
    static func urls(_ info: NSDraggingInfo) -> [URL] {
        guard info.draggingSource != nil else { return [] }
        let objects = info.draggingPasteboard.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true])
        return (objects as? [URL] ?? []).map { $0.standardizedFileURL }
    }

    private static func sameVolume(_ a: URL, _ b: URL) -> Bool {
        let va = (try? a.resourceValues(forKeys: [.volumeIdentifierKey]))?.volumeIdentifier as? NSObject
        let vb = (try? b.resourceValues(forKeys: [.volumeIdentifierKey]))?.volumeIdentifier as? NSObject
        guard let va, let vb else { return false }
        return va.isEqual(vb)
    }

    /// What dropping `urls` into `dir` does: .copy with ⌥ (or across volumes), otherwise .move; [] when it makes no
    /// sense (into itself, or a move into the folder they are already in).
    static func operation(for urls: [URL], into dir: URL) -> NSDragOperation {
        guard !urls.isEmpty else { return [] }
        for url in urls where FileWork.isInside(dir, of: url) { return [] }
        let copy = NSEvent.modifierFlags.contains(.option) || !urls.allSatisfy { sameVolume($0, dir) }
        if !copy {
            let target = dir.resolvingSymlinksInPath().path
            if urls.allSatisfy({ $0.deletingLastPathComponent().resolvingSymlinksInPath().path == target }) { return [] }
        }
        return copy ? .copy : .move
    }
}

// MARK: - File menu: commands for whichever folder view has the keyboard

/// The File-menu commands. They act on the folder view that is the first responder; with the keyboard anywhere else
/// (a text field, the sidebar, the search results) the items are disabled and their keys fall through.
@MainActor
final class FileMenuRouter: NSObject, NSMenuItemValidation {
    static let shared = FileMenuRouter()

    enum Command: Int {
        case newFolder = 1, rename, duplicate, alias, compress, extract, trash, deleteNow, showInFinder, copyPath
    }

    @objc func runCommand(_ sender: NSMenuItem) {
        guard let command = Command(rawValue: sender.tag), let actions = FileActions.active else { return }
        actions.run(command)
    }

    func validateMenuItem(_ item: NSMenuItem) -> Bool {
        guard let command = Command(rawValue: item.tag), let actions = FileActions.active else { return false }
        return actions.canRun(command)
    }

    /// Adds the file operations to the File menu, after what is there.
    func addItems(to menu: NSMenu) {
        menu.addItem(.separator())
        menu.addItem(entry("New Folder", .newFolder, "n", [.command, .shift]))
        menu.addItem(entry("Rename", .rename, "", []))
        menu.addItem(entry("Duplicate", .duplicate, "d", [.command]))
        menu.addItem(entry("Make Alias", .alias, "a", [.command, .control]))
        menu.addItem(entry("Compress", .compress, "", []))
        menu.addItem(entry("Extract", .extract, "", []))
        menu.addItem(.separator())
        let backspace = String(UnicodeScalar(UInt8(8)))
        menu.addItem(entry("Move to Trash", .trash, backspace, [.command]))
        menu.addItem(entry("Delete Immediately…", .deleteNow, backspace, [.command, .option]))
        menu.addItem(.separator())
        menu.addItem(entry("Show in Finder", .showInFinder, "", []))
        menu.addItem(entry("Copy Path", .copyPath, "c", [.command, .option]))
    }

    private func entry(_ title: String, _ command: Command, _ key: String, _ mask: NSEvent.ModifierFlags) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: #selector(runCommand(_:)), keyEquivalent: key)
        item.keyEquivalentModifierMask = mask
        item.tag = command.rawValue
        item.target = self
        return item
    }
}
