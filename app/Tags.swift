// Finder colour tags: the seven colours, reading and writing them in macOS's own format (they show in Finder), the
// dots drawn in list, column, icon and search rows, the "Tags" row of the context menu, and the Tags section of
// the sidebar, which lists every file Mac-wide that carries one colour (NSMetadataQuery, kMDItemUserTags).
//
// Ported from FinderFlow v1.5.2 (https://github.com/Gtarafdar/FinderFlow, MIT, (c) 2026 Gobinda Tarafdar):
//   - FileItem.swift: the tag colour tables (colour name <-> label number, the Finder colour values and the menu
//     order) and the toggle rule (a colour is removed when every selected file has it, otherwise added to all).
//   - FolderCreationService.swift: writeTags, which stores a colour tag as "Name\nNumber" so macOS registers a real
//     colour tag, and toggleColorTag / clearTags.
// The licence text is in THIRD_PARTY_NOTICES.md at the project root.
//
// Differences from FinderFlow: tags are read back with `.tagNamesKey` (FinderFlow 1.5.2 never requested it when
// loading items), the file's own tag list is kept verbatim when one colour is toggled (custom tags and their
// numbers survive), a failed write is reported, and every change can be undone.

import Cocoa

/// One of Finder's seven colour tags. The raw value is Finder's label number.
enum TagColor: Int, CaseIterable {
    case gray = 1, green = 2, purple = 3, blue = 4, yellow = 5, red = 6, orange = 7

    /// The order of Finder's Tags menu.
    static let menuOrder: [TagColor] = [.red, .orange, .yellow, .green, .blue, .purple, .gray]

    var name: String {
        switch self {
        case .gray: return "Gray"
        case .green: return "Green"
        case .purple: return "Purple"
        case .blue: return "Blue"
        case .yellow: return "Yellow"
        case .red: return "Red"
        case .orange: return "Orange"
        }
    }

    /// Finder's tag colours.
    var color: NSColor {
        switch self {
        case .red: return NSColor(srgbRed: 1.00, green: 0.23, blue: 0.19, alpha: 1)
        case .orange: return NSColor(srgbRed: 1.00, green: 0.58, blue: 0.00, alpha: 1)
        case .yellow: return NSColor(srgbRed: 1.00, green: 0.80, blue: 0.00, alpha: 1)
        case .green: return NSColor(srgbRed: 0.20, green: 0.78, blue: 0.35, alpha: 1)
        case .blue: return NSColor(srgbRed: 0.00, green: 0.48, blue: 1.00, alpha: 1)
        case .purple: return NSColor(srgbRed: 0.69, green: 0.32, blue: 0.87, alpha: 1)
        case .gray: return NSColor(srgbRed: 0.56, green: 0.56, blue: 0.58, alpha: 1)
        }
    }

    /// The colour of a tag name ("Red", "gray", "grey"); nil for a custom tag.
    init?(tagName: String) {
        switch tagName.trimmingCharacters(in: .whitespaces).lowercased() {
        case "red": self = .red
        case "orange": self = .orange
        case "yellow": self = .yellow
        case "green": self = .green
        case "blue": self = .blue
        case "purple": self = .purple
        case "gray", "grey": self = .gray
        default: return nil
        }
    }

    /// The colour of a stored tag, "Name" or "Name\nNumber" (the number wins: it is what Finder draws).
    init?(stored: String) {
        let parts = stored.components(separatedBy: "\n")
        if parts.count > 1, let n = Int(parts[1]), let c = TagColor(rawValue: n) {
            self = c
        } else if let c = TagColor(tagName: parts[0]) {
            self = c
        } else {
            return nil
        }
    }

    /// How macOS stores this colour tag: "Red\n6".
    var stored: String { "\(name)\n\(rawValue)" }
}

enum TagStore {
    private static let attribute = "com.apple.metadata:_kMDItemUserTags"

    /// The tag name part of a stored tag ("Red\n6" is "Red").
    static func name(of stored: String) -> String {
        stored.components(separatedBy: "\n").first ?? stored
    }

    /// The colours among stored tags or plain tag names, in order and without repeats.
    static func colors(of tags: [String]) -> [TagColor] {
        var out: [TagColor] = []
        for tag in tags {
            if let c = TagColor(stored: tag), !out.contains(c) { out.append(c) }
        }
        return out
    }

    /// The file's tags exactly as macOS stores them ("Name" or "Name\nNumber"). Read from the extended attribute so
    /// a custom tag keeps its number when one colour is toggled; falls back to the plain tag names.
    static func storedTags(of url: URL) -> [String] {
        let path = url.path
        let size = getxattr(path, attribute, nil, 0, 0, 0)
        if size > 0 {
            var data = Data(count: size)
            let read = data.withUnsafeMutableBytes { getxattr(path, attribute, $0.baseAddress, size, 0, 0) }
            if read > 0, let list = try? PropertyListSerialization.propertyList(from: data, options: [], format: nil) as? [String] {
                return list
            }
        }
        return (try? url.resourceValues(forKeys: [.tagNamesKey]))?.tagNames ?? []
    }

    /// Writes the tag list in macOS's own format; the system stores it, so Finder and Spotlight see it.
    static func write(_ stored: [String], to url: URL) throws {
        try (url as NSURL).setResourceValue(stored as NSArray, forKey: .tagNamesKey)
    }

    /// The tags after toggling one colour on a file: the colour is taken off (every tag of that colour name) or put on.
    static func toggled(_ stored: [String], color: TagColor, adding: Bool) -> [String] {
        var out = stored.filter { TagColor(tagName: name(of: $0)) != color }
        if adding { out.append(color.stored) }
        return out
    }

    // MARK: dots

    private static var imageCache: [String: NSImage] = [:]

    /// The coloured dots of a row, overlapping like Finder's (the first colour on top). Nil for no colours.
    static func dotsImage(for colors: [TagColor], diameter: CGFloat = 10) -> NSImage? {
        guard !colors.isEmpty else { return nil }
        let key = colors.map { String($0.rawValue) }.joined(separator: ",") + "@\(diameter)"
        if let cached = imageCache[key] { return cached }
        let step = diameter * 0.6
        let width = diameter + step * CGFloat(colors.count - 1)
        let image = NSImage(size: NSSize(width: width, height: diameter), flipped: false) { _ in
            for (index, c) in colors.enumerated().reversed() {
                let rect = NSRect(x: step * CGFloat(index), y: 0.5, width: diameter - 1, height: diameter - 1)
                let dot = NSBezierPath(ovalIn: rect)
                c.color.setFill()
                dot.fill()
                NSColor.windowBackgroundColor.withAlphaComponent(0.9).setStroke()
                dot.lineWidth = 1
                dot.stroke()
            }
            return true
        }
        image.isTemplate = false
        imageCache[key] = image
        return image
    }

    /// One round swatch, with a tick when `checked` (the Tags row of the context menu, the sidebar).
    static func swatch(_ color: TagColor, diameter: CGFloat = 14, checked: Bool = false) -> NSImage {
        let key = "swatch\(color.rawValue)@\(diameter)\(checked)"
        if let cached = imageCache[key] { return cached }
        let image = NSImage(size: NSSize(width: diameter, height: diameter), flipped: false) { rect in
            let dot = NSBezierPath(ovalIn: rect.insetBy(dx: 0.5, dy: 0.5))
            color.color.setFill()
            dot.fill()
            color.color.shadow(withLevel: 0.3)?.setStroke()
            dot.lineWidth = 1
            dot.stroke()
            if checked {
                let tick = NSBezierPath()
                tick.move(to: NSPoint(x: rect.width * 0.28, y: rect.height * 0.52))
                tick.line(to: NSPoint(x: rect.width * 0.44, y: rect.height * 0.34))
                tick.line(to: NSPoint(x: rect.width * 0.74, y: rect.height * 0.68))
                tick.lineWidth = max(1.5, diameter / 9)
                tick.lineCapStyle = .round
                tick.lineJoinStyle = .round
                NSColor.white.setStroke()
                tick.stroke()
            }
            return true
        }
        image.isTemplate = false
        imageCache[key] = image
        return image
    }
}

// MARK: - the Tags row of the context menu

/// The seven dots in one menu row. Clicking a dot toggles that colour for the selection and closes the menu.
@MainActor
final class TagMenuRowView: NSView {
    private var buttons: [TagColor: NSButton] = [:]
    private let onToggle: (TagColor) -> Void

    /// `state[color]` is true when every selected item has the colour.
    init(state: [TagColor: Bool], onToggle: @escaping (TagColor) -> Void) {
        self.onToggle = onToggle
        let side: CGFloat = 20
        let gap: CGFloat = 5
        let count = CGFloat(TagColor.menuOrder.count)
        let width = 14 + count * side + (count - 1) * gap + 14
        super.init(frame: NSRect(x: 0, y: 0, width: width, height: 26))
        for (index, color) in TagColor.menuOrder.enumerated() {
            let button = NSButton(frame: NSRect(x: 14 + CGFloat(index) * (side + gap), y: 3, width: side, height: side))
            button.isBordered = false
            button.imagePosition = .imageOnly
            button.image = TagStore.swatch(color, diameter: 16, checked: state[color] ?? false)
            button.toolTip = color.name
            button.target = self
            button.action = #selector(clicked(_:))
            button.tag = color.rawValue
            addSubview(button)
            buttons[color] = button
        }
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    @objc private func clicked(_ sender: NSButton) {
        guard let color = TagColor(rawValue: sender.tag) else { return }
        enclosingMenuItem?.menu?.cancelTracking()
        onToggle(color)
    }
}

// MARK: - the sidebar's Tags section: every file with one colour, Mac-wide

/// Shows the files that carry one colour tag anywhere on the Mac, as a list in the main window's content area.
/// The query is NSMetadataQuery on kMDItemUserTags; it keeps updating while the list is on screen.
@MainActor
final class TagResultsController: NSObject {
    static let shared = TagResultsController()

    private var query: NSMetadataQuery?
    private var list: ListViewController?
    private weak var content: ContentContainerViewController?
    private weak var window: NSWindow?
    private weak var owner: MainWindowController?      // the window whose content area holds the list; it opens folders
    private(set) var color: TagColor?
    private var generation = 0
    private var observers: [NSObjectProtocol] = []
    private static let limit = 5000

    /// Selecting a colour in the sidebar. `window` is the main window; its split view holds the content area.
    func show(_ color: TagColor, in window: NSWindow?) {
        guard let window, let split = window.contentViewController as? NSSplitViewController,
              let content = split.splitViewItems.compactMap({ $0.viewController as? ContentContainerViewController }).first
        else { return }
        let newOwner = (NSApp.delegate as? AppDelegate)?.mainControllers.first { $0.window === window }
        if let previous = owner, previous !== newOwner, isShowing(in: previous.window) {
            previous.setMode(previous.mode)    // the list moves to this window: the old one shows its own view again
        }
        self.window = window
        self.content = content
        self.owner = newOwner
        self.color = color
        let list = self.list ?? makeList()
        self.list = list
        newOwner?.tagListWillShow()            // a search in this window gives way to the list
        content.show(list)
        content.setPath(nil)
        content.setStatus("Looking for files tagged \(color.name)…")
        window.title = "Tag: \(color.name)"
        list.show(items: [])
        newOwner?.updateChrome()               // the mode control stops showing the old mode
        startQuery(for: color)
    }

    /// True while `window`'s content area shows this list (the sidebar's Tags section).
    func isShowing(in window: NSWindow?) -> Bool {
        guard let window, self.window === window, let list, let content else { return false }
        return content.child === list
    }

    /// The rows selected in the tag list (⌘I and ⌘Y act on these while it shows).
    var selectedItems: [FileItem] { list?.selectedItems ?? [] }

    private func makeList() -> ListViewController {
        let vc = ListViewController(provider: FileSystemProvider(), snapshot: false)
        vc.onNavigate = { [weak self] url in
            if let owner = self?.owner { owner.openFolder(url) } else { (NSApp.delegate as? AppDelegate)?.mainWindow.openFolder(url) }
        }
        vc.onEnclosingFolder = {}
        vc.onStatus = { [weak self] text in
            guard let self, let color = self.color else { return }
            self.content?.setStatus("\(color.name) tag: \(text)")
        }
        return vc
    }

    private func stopQuery() {
        query?.stop()
        query = nil
        for o in observers { NotificationCenter.default.removeObserver(o) }
        observers = []
    }

    private func startQuery(for color: TagColor) {
        stopQuery()
        generation += 1
        let q = NSMetadataQuery()
        q.predicate = NSPredicate(format: "%K == %@", "kMDItemUserTags", color.name)
        q.searchScopes = [NSMetadataQueryLocalComputerScope]
        let center = NotificationCenter.default
        for name in [NSNotification.Name.NSMetadataQueryDidFinishGathering, .NSMetadataQueryDidUpdate] {
            observers.append(center.addObserver(forName: name, object: q, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated { self?.reload() }
            })
        }
        query = q
        q.start()
    }

    /// Reads the query's results into the list. Stops the query once another view has taken the content area.
    private func reload() {
        guard let q = query, let list, let content, content.child === list else {
            stopQuery()
            return
        }
        q.disableUpdates()
        var paths: [String] = []
        let total = q.resultCount
        for index in 0..<min(total, Self.limit * 2) {
            if let item = q.result(at: index) as? NSMetadataItem,
               let path = item.value(forAttribute: NSMetadataItemPathKey) as? String,
               !path.contains("/.Trash/") {
                paths.append(path)
                if paths.count >= Self.limit { break }
            }
        }
        q.enableUpdates()
        let token = generation
        Task { @MainActor in
            let items = await Task.detached(priority: .userInitiated) {
                paths.map { FileItem(url: URL(fileURLWithPath: $0)) }
            }.value
            guard token == generation, content.child === list else { return }
            list.show(items: items)
        }
    }
}
