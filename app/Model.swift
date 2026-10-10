// The data the folder views show. A FileItem is one file or folder; a ContentProvider supplies items.
// List and column views take a ContentProvider and never touch FileManager themselves, so the Indexed
// view can later supply the same FileItem shape from MySearch's catalogue.

import Cocoa

/// One file or folder, read once from its resource values. Packages (.app and the like) are files, as in Finder.
struct FileItem: Hashable {
    let url: URL
    let name: String
    let isDir: Bool
    let size: Int64?           // bytes; nil for folders
    let mtime: Date?
    let kind: String           // the localized kind, e.g. "PDF document"
    let tagNames: [String]     // Finder tag names ("Red", "Work"), from .tagNamesKey

    static let keys: [URLResourceKey] = [.localizedNameKey, .isDirectoryKey, .isPackageKey, .fileSizeKey,
                                         .contentModificationDateKey, .localizedTypeDescriptionKey, .tagNamesKey]

    init(url: URL) {
        self.url = url
        let v = try? url.resourceValues(forKeys: Set(FileItem.keys))
        isDir = (v?.isDirectory ?? false) && !(v?.isPackage ?? false)
        name = v?.localizedName ?? url.lastPathComponent
        size = isDir ? nil : v?.fileSize.map { Int64($0) }
        mtime = v?.contentModificationDate
        kind = v?.localizedTypeDescription ?? (isDir ? "Folder" : "Document")
        tagNames = v?.tagNames ?? []
    }

    /// The colours among the item's tags (the dots a row shows).
    var tagColors: [TagColor] { TagStore.colors(of: tagNames) }

    /// Computed, not stored: views ask for the icon only for the rows they draw.
    var icon: NSImage { NSWorkspace.shared.icon(forFile: url.path) }
}

/// The column a list or column view sorts by.
enum SortKey: String, CaseIterable {
    case name, kind, size, modified
}

/// Sorts like Finder: folders first (unless told otherwise), then by the key, names compared as Finder does.
func sortedItems(_ items: [FileItem], by key: SortKey, ascending: Bool = true, foldersFirst: Bool = true) -> [FileItem] {
    items.sorted { a, b in
        if foldersFirst && a.isDir != b.isDir { return a.isDir }
        let order: ComparisonResult
        switch key {
        case .name: order = a.name.localizedStandardCompare(b.name)
        case .kind: order = a.kind.localizedStandardCompare(b.kind)
        case .size: order = (a.size ?? -1) < (b.size ?? -1) ? .orderedAscending : (a.size ?? -1) > (b.size ?? -1) ? .orderedDescending : .orderedSame
        case .modified: order = (a.mtime ?? .distantPast) < (b.mtime ?? .distantPast) ? .orderedAscending : (a.mtime ?? .distantPast) > (b.mtime ?? .distantPast) ? .orderedDescending : .orderedSame
        }
        if order == .orderedSame { return a.name.localizedStandardCompare(b.name) == .orderedAscending }
        return ascending ? order == .orderedAscending : order == .orderedDescending
    }
}

/// Where a view gets its items. The Mac mode uses FileSystemProvider; the Indexed view will add its own.
protocol ContentProvider: AnyObject {
    /// The items in `folder`, unsorted. Hidden files are left out unless `includeHidden`. With `recursive`,
    /// every item at any depth below it (packages are single items).
    func items(in folder: URL, recursive: Bool, includeHidden: Bool) async throws -> [FileItem]
}

extension ContentProvider {
    /// The items in `folder` without hidden files, as before `includeHidden` existed.
    func items(in folder: URL, recursive: Bool) async throws -> [FileItem] {
        try await items(in: folder, recursive: recursive, includeHidden: false)
    }
}

/// Reads the disk with FileManager. Runs off the main thread and returns to the caller's actor.
final class FileSystemProvider: ContentProvider {
    func items(in folder: URL, recursive: Bool, includeHidden: Bool) async throws -> [FileItem] {
        try await Task.detached(priority: .userInitiated) {
            let fm = FileManager.default
            let hidden: FileManager.DirectoryEnumerationOptions = includeHidden ? [] : [.skipsHiddenFiles]
            if recursive {
                guard let e = fm.enumerator(at: folder, includingPropertiesForKeys: FileItem.keys,
                                            options: hidden.union([.skipsPackageDescendants])) else { return [] }
                var out: [FileItem] = []
                while let url = e.nextObject() as? URL { out.append(FileItem(url: url)) }
                return out
            }
            let urls = try fm.contentsOfDirectory(at: folder, includingPropertiesForKeys: FileItem.keys,
                                                  options: hidden)
            return urls.map { FileItem(url: $0) }
        }.value
    }
}
