// File icons for Quick Search's rows. A row shows the generic icon for its type at once; the file's own icon is read on a
// background queue and cached, then handed back on the main actor. Nothing here reads a file on the main thread.

import Cocoa
import UniformTypeIdentifiers

@MainActor
enum QuickIcons {
    private static let cache = NSCache<NSString, NSImage>()
    private static var generic: [String: NSImage] = [:]

    /// The image for `path` now: its own icon when it is cached, else the generic icon for its type. When the own icon
    /// is not cached yet, it is loaded in the background and `done` gets it (for the row that still shows this path).
    static func image(for path: String, isDir: Bool, done: @escaping @MainActor (String, NSImage) -> Void) -> NSImage {
        if let cached = cache.object(forKey: path as NSString) { return cached }
        DispatchQueue.global(qos: .userInitiated).async {
            let icon = NSWorkspace.shared.icon(forFile: path)
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    cache.setObject(icon, forKey: path as NSString)
                    done(path, icon)
                }
            }
        }
        return genericIcon(for: path, isDir: isDir)
    }

    /// The generic icon for a row's type (a folder, a PDF, a plain document), cached per extension.
    static func genericIcon(for path: String, isDir: Bool) -> NSImage {
        let ext = isDir ? "/" : (path as NSString).pathExtension.lowercased()
        if let image = generic[ext] { return image }
        let type: UTType = isDir ? .folder : (UTType(filenameExtension: ext) ?? .data)
        let image = NSWorkspace.shared.icon(for: type)
        generic[ext] = image
        return image
    }
}
