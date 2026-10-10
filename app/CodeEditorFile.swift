// File access for the editors: detection of text files, reading with the file's own encoding and line endings,
// and writing back the SAME encoding and line endings (FinderFlow re-encoded everything to UTF-8; this does not).
//
// Adapted in part from FinderFlow v1.5.2 (FinderFlow/CodeEditorView.swift, `TextFileDetector`): the lists of
// known text extensions and file names.
// FinderFlow is MIT licensed, Copyright (c) 2026 Gobinda Tarafdar. The full notice is in THIRD_PARTY_NOTICES.md.

import Cocoa

// MARK: - Text detection

enum TextFileDetector {
    /// Extensions that always open in the editor (no disk sniff). Adapted from FinderFlow, plus a few more.
    static let knownTextExtensions: Set<String> = [
        "txt", "text", "log", "md", "markdown", "mdown", "mkd", "rtf",
        "json", "json5", "jsonc", "yaml", "yml", "toml", "ini", "conf", "cfg", "properties", "env",
        "xml", "plist", "svg", "html", "htm", "xhtml", "css", "scss", "sass", "less",
        "js", "jsx", "mjs", "cjs", "ts", "tsx", "swift", "m", "mm", "h", "hpp", "c", "cc", "cpp", "cxx", "cs",
        "java", "kt", "kts", "go", "rs", "rb", "php", "py", "pyw", "pl", "pm", "lua", "r", "dart", "scala",
        "sh", "bash", "zsh", "fish", "ps1", "bat", "cmd", "sql", "graphql", "gql",
        "vue", "svelte", "astro", "tex", "bib", "csv", "tsv", "diff", "patch", "gitignore",
        "gitattributes", "editorconfig", "dockerfile", "makefile", "mk", "cmake", "gradle",
        "asm", "s", "vim", "el", "clj", "ex", "exs", "erl", "hs", "ml", "fs", "groovy", "nim", "zig", "eml",
    ]

    /// File names (no extension) that are usually text.
    static let knownTextFilenames: Set<String> = [
        "makefile", "dockerfile", "readme", "license", "changelog", "authors",
        ".gitignore", ".gitattributes", ".editorconfig", ".env", ".zshrc", ".bashrc", ".bash_profile",
        ".profile", ".vimrc", "podfile", "gemfile", "rakefile", ".npmrc", ".prettierrc", ".eslintrc",
    ]

    static func isMarkdown(_ url: URL) -> Bool {
        ["md", "markdown", "mdown", "mkd"].contains(url.pathExtension.lowercased())
    }

    static func isEditableText(_ url: URL) -> Bool {
        let ext = url.pathExtension.lowercased()
        if !ext.isEmpty && knownTextExtensions.contains(ext) { return true }
        if knownTextFilenames.contains(url.lastPathComponent.lowercased()) { return true }
        return sniffIsText(url)
    }

    private static func sniffIsText(_ url: URL) -> Bool {
        guard let handle = try? FileHandle(forReadingFrom: url) else { return false }
        defer { try? handle.close() }
        let data = handle.readData(ofLength: 8192)
        if data.isEmpty { return true }
        if data.starts(with: [0xFF, 0xFE]) || data.starts(with: [0xFE, 0xFF]) { return true }
        return !data.contains(0)
    }
}

// MARK: - Format (encoding + line endings)

enum EditorLineEnding {
    case lf, crlf, cr
    var string: String {
        switch self {
        case .lf: return "\n"
        case .crlf: return "\r\n"
        case .cr: return "\r"
        }
    }
    var label: String {
        switch self {
        case .lf: return "LF"
        case .crlf: return "CRLF"
        case .cr: return "CR"
        }
    }
}

struct EditorFileFormat {
    enum Kind { case utf8, utf16LE, utf16BE, latin1 }
    var kind: Kind = .utf8
    var hasBOM = false
    var lineEnding: EditorLineEnding = .lf

    var encodingLabel: String {
        switch kind {
        case .utf8: return hasBOM ? "UTF-8 BOM" : "UTF-8"
        case .utf16LE: return hasBOM ? "UTF-16 LE BOM" : "UTF-16 LE"
        case .utf16BE: return hasBOM ? "UTF-16 BE BOM" : "UTF-16 BE"
        case .latin1: return "Latin-1"
        }
    }
}

struct EditorLoadedFile {
    /// Line endings normalised to "\n" for editing; `format.lineEnding` is put back on save.
    var text: String
    var format: EditorFileFormat
    var modified: Date?
}

enum EditorFileError: LocalizedError {
    case tooLarge(Int)
    case notText
    case unreadable(String)
    var errorDescription: String? {
        switch self {
        case .tooLarge(let n): return "The file is too large to edit here (\(ByteCountFormatter.string(fromByteCount: Int64(n), countStyle: .file)))."
        case .notText: return "This file does not look like text."
        case .unreadable(let why): return "The file could not be read: \(why)"
        }
    }
}

enum EditorFileIO {
    static let maxBytes = 20 * 1024 * 1024

    static func modificationDate(_ url: URL) -> Date? {
        (try? FileManager.default.attributesOfItem(atPath: url.path))?[.modificationDate] as? Date
    }

    static func read(_ url: URL) throws -> EditorLoadedFile {
        let attrs: [FileAttributeKey: Any]
        do { attrs = try FileManager.default.attributesOfItem(atPath: url.path) } catch {
            throw EditorFileError.unreadable(error.localizedDescription)
        }
        if let size = attrs[.size] as? Int, size > maxBytes { throw EditorFileError.tooLarge(size) }
        let data: Data
        do { data = try Data(contentsOf: url) } catch { throw EditorFileError.unreadable(error.localizedDescription) }

        var format = EditorFileFormat()
        var body: String
        if data.starts(with: [0xEF, 0xBB, 0xBF]) {
            guard let s = String(data: data.dropFirst(3), encoding: .utf8) else { throw EditorFileError.notText }
            format.kind = .utf8; format.hasBOM = true; body = s
        } else if data.starts(with: [0xFF, 0xFE]) {
            guard let s = String(data: data.dropFirst(2), encoding: .utf16LittleEndian) else { throw EditorFileError.notText }
            format.kind = .utf16LE; format.hasBOM = true; body = s
        } else if data.starts(with: [0xFE, 0xFF]) {
            guard let s = String(data: data.dropFirst(2), encoding: .utf16BigEndian) else { throw EditorFileError.notText }
            format.kind = .utf16BE; format.hasBOM = true; body = s
        } else if data.contains(0) {
            throw EditorFileError.notText
        } else if let s = String(data: data, encoding: .utf8) {
            format.kind = .utf8; body = s
        } else if let s = String(data: data, encoding: .isoLatin1) {
            format.kind = .latin1; body = s
        } else {
            throw EditorFileError.notText
        }

        // Line endings: count on the bytes (Swift treats "\r\n" as one Character).
        var crlf = 0, lf = 0, cr = 0
        var prevCR = false
        for b in body.utf8 {
            if b == 0x0A { if prevCR { crlf += 1 } else { lf += 1 } }
            else if prevCR { cr += 1 }
            prevCR = (b == 0x0D)
        }
        if prevCR { cr += 1 }
        if crlf > lf && crlf >= cr { format.lineEnding = .crlf }
        else if cr > lf && cr > crlf { format.lineEnding = .cr }
        else { format.lineEnding = .lf }
        if crlf > 0 || cr > 0 {
            body = body.replacingOccurrences(of: "\r\n", with: "\n").replacingOccurrences(of: "\r", with: "\n")
        }
        return EditorLoadedFile(text: body, format: format, modified: attrs[.modificationDate] as? Date)
    }

    /// The bytes to write, or nil when the text cannot be represented in the file's encoding (Latin-1 only).
    static func encode(_ text: String, format: EditorFileFormat) -> Data? {
        // Pasted text may carry its own endings; normalise first so "\r\n" is never doubled.
        var s = text
        if s.utf8.contains(13) { s = s.replacingOccurrences(of: "\r\n", with: "\n").replacingOccurrences(of: "\r", with: "\n") }
        if format.lineEnding != .lf { s = s.replacingOccurrences(of: "\n", with: format.lineEnding.string) }
        var out = Data()
        switch format.kind {
        case .utf8:
            if format.hasBOM { out.append(contentsOf: [0xEF, 0xBB, 0xBF]) }
            out.append(Data(s.utf8))
        case .utf16LE:
            if format.hasBOM { out.append(contentsOf: [0xFF, 0xFE]) }
            guard let d = s.data(using: .utf16LittleEndian) else { return nil }
            out.append(d)
        case .utf16BE:
            if format.hasBOM { out.append(contentsOf: [0xFE, 0xFF]) }
            guard let d = s.data(using: .utf16BigEndian) else { return nil }
            out.append(d)
        case .latin1:
            guard let d = s.data(using: .isoLatin1, allowLossyConversion: false) else { return nil }
            out.append(d)
        }
        return out
    }
}

// MARK: - Saving (with the questions a careful editor asks)

@MainActor
enum EditorSaver {
    /// Writes `text` to `url` in the file's own format. Asks before overwriting a file that changed on disk since it
    /// was read, and before switching a Latin-1 file to UTF-8 when the text no longer fits. Returns true when saved.
    static func save(text: String, to url: URL, format: inout EditorFileFormat, diskDate: inout Date?) -> Bool {
        let target = url.resolvingSymlinksInPath()
        let fm = FileManager.default
        if fm.fileExists(atPath: target.path), let known = diskDate, let now = EditorFileIO.modificationDate(target),
           abs(now.timeIntervalSince(known)) > 0.5 {
            let a = NSAlert()
            a.messageText = "\"\(url.lastPathComponent)\" changed on disk."
            a.informativeText = "Another program modified this file after you opened it. Saving now replaces those changes."
            a.addButton(withTitle: "Overwrite")
            a.addButton(withTitle: "Cancel")
            if a.runModal() != .alertFirstButtonReturn { return false }
        }
        var data = EditorFileIO.encode(text, format: format)
        if data == nil {
            let a = NSAlert()
            a.messageText = "Some characters do not exist in Latin-1."
            a.informativeText = "\"\(url.lastPathComponent)\" was opened as Latin-1. Save it as UTF-8 instead?"
            a.addButton(withTitle: "Save as UTF-8")
            a.addButton(withTitle: "Cancel")
            if a.runModal() != .alertFirstButtonReturn { return false }
            format.kind = .utf8
            format.hasBOM = false
            data = EditorFileIO.encode(text, format: format)
        }
        guard let bytes = data else { return false }
        do {
            if fm.fileExists(atPath: target.path) {
                // Write beside the file, then swap: a crash never leaves half a file, and replaceItemAt keeps the
                // original's metadata (permissions, Finder tags and other extended attributes).
                let dir = try fm.url(for: .itemReplacementDirectory, in: .userDomainMask, appropriateFor: target, create: true)
                let tmp = dir.appendingPathComponent(target.lastPathComponent)
                try bytes.write(to: tmp)
                _ = try fm.replaceItemAt(target, withItemAt: tmp)
                try? fm.removeItem(at: dir)
            } else {
                try bytes.write(to: target, options: .atomic)
            }
        } catch {
            let a = NSAlert(error: error)
            a.messageText = "\"\(url.lastPathComponent)\" could not be saved."
            a.runModal()
            return false
        }
        diskDate = EditorFileIO.modificationDate(target)
        return true
    }
}
