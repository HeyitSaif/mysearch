// Get Info (⌘I): a small read-only panel in the Finder's style - icon and name, then kind, size, where, created,
// modified and tags. A folder's size is added up in the background and shown when it is ready. Nothing here changes a file.

import Cocoa

@MainActor
final class InfoPanelController: NSObject {
    static let shared = InfoPanelController()

    private(set) lazy var panel: NSPanel = makePanel()
    private let icon = NSImageView()
    private let name = NSTextField(wrappingLabelWithString: "")
    private let subtitle = NSTextField(labelWithString: "")
    private var values: [String: NSTextField] = [:]
    private var sizeTask: Task<Void, Never>?
    /// True while a folder's size is still being added up (the snapshot waits on this).
    private(set) var isCalculating = false

    /// Fills the panel for `items` (the first one is described; several show a count) and, unless `orderFront` is
    /// false (snapshots), brings it forward.
    func show(_ items: [FileItem], orderFront: Bool = true) {
        guard let item = items.first else { return }
        _ = panel
        sizeTask?.cancel()
        icon.image = item.icon
        name.stringValue = item.name
        subtitle.stringValue = items.count > 1 ? "\(items.count) items selected (showing the first)" : item.kind
        panel.title = "\(item.name) Info"
        let created = (try? item.url.resourceValues(forKeys: [.creationDateKey]))?.creationDate
        set("Kind", item.kind)
        set("Where", (item.url.deletingLastPathComponent().path as NSString).abbreviatingWithTildeInPath)
        set("Created", FileFormat.longDate(created))
        set("Modified", FileFormat.longDate(item.mtime))
        set("Tags", item.tagNames.isEmpty ? "--" : item.tagNames.joined(separator: ", "))
        if item.isDir {
            set("Size", "Calculating…")
            isCalculating = true
            let url = item.url
            sizeTask = Task { [weak self] in
                let total = await Task.detached(priority: .utility) { InfoPanelController.folderSize(url) }.value
                guard !Task.isCancelled, let self else { return }
                self.isCalculating = false
                let text = ByteCountFormatter.string(fromByteCount: total.bytes, countStyle: .file)
                let detail = total.items == 1 ? "1 item" : "\(total.items) items"
                self.set("Size", "\(text) on disk (\(detail))")
            }
        } else {
            isCalculating = false
            set("Size", item.size.map { ByteCountFormatter.string(fromByteCount: $0, countStyle: .file) + " (\(Self.grouped($0)) bytes)" } ?? "--")
        }
        if orderFront { panel.makeKeyAndOrderFront(nil) }
    }

    private static func grouped(_ n: Int64) -> String {
        let f = NumberFormatter(); f.numberStyle = .decimal
        return f.string(from: NSNumber(value: n)) ?? "\(n)"
    }

    /// Adds up the files below `url`, as Get Info does. Packages are counted inside; unreadable folders count as 0.
    nonisolated static func folderSize(_ url: URL) -> (bytes: Int64, items: Int) {
        let keys: [URLResourceKey] = [.totalFileAllocatedSizeKey, .isRegularFileKey]
        guard let e = FileManager.default.enumerator(at: url, includingPropertiesForKeys: keys, options: [], errorHandler: { _, _ in true }) else {
            return (0, 0)
        }
        var bytes: Int64 = 0
        var items = 0
        while let u = e.nextObject() as? URL {
            if Task.isCancelled { break }
            items += 1
            if let v = try? u.resourceValues(forKeys: Set(keys)), v.isRegularFile == true {
                bytes += Int64(v.totalFileAllocatedSize ?? 0)
            }
        }
        return (bytes, items)
    }

    private func set(_ key: String, _ value: String) { values[key]?.stringValue = value }

    private func makePanel() -> NSPanel {
        let p = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 330, height: 330),
                        styleMask: [.titled, .closable, .utilityWindow], backing: .buffered, defer: false)
        p.isReleasedWhenClosed = false
        p.hidesOnDeactivate = false
        p.isFloatingPanel = true

        icon.imageScaling = .scaleProportionallyUpOrDown
        icon.translatesAutoresizingMaskIntoConstraints = false
        name.font = .systemFont(ofSize: 14, weight: .semibold)
        name.maximumNumberOfLines = 2
        name.isSelectable = true
        subtitle.font = .systemFont(ofSize: 12)
        subtitle.textColor = .secondaryLabelColor
        let titles = NSStackView(views: [name, subtitle])
        titles.orientation = .vertical
        titles.alignment = .leading
        titles.spacing = 2
        let head = NSStackView(views: [icon, titles])
        head.orientation = .horizontal
        head.alignment = .centerY
        head.spacing = 12

        let rows = NSStackView()
        rows.orientation = .vertical
        rows.alignment = .leading
        rows.spacing = 8
        for key in ["Kind", "Size", "Where", "Created", "Modified", "Tags"] {
            let label = NSTextField(labelWithString: key + ":")
            label.font = .systemFont(ofSize: 12)
            label.textColor = .secondaryLabelColor
            label.alignment = .right
            label.translatesAutoresizingMaskIntoConstraints = false
            label.widthAnchor.constraint(equalToConstant: 64).isActive = true
            let value = NSTextField(wrappingLabelWithString: "")
            value.font = .systemFont(ofSize: 12)
            value.isSelectable = true
            value.maximumNumberOfLines = 3
            value.lineBreakMode = .byTruncatingMiddle
            values[key] = value
            let row = NSStackView(views: [label, value])
            row.orientation = .horizontal
            row.alignment = .firstBaseline
            row.spacing = 8
            rows.addArrangedSubview(row)
        }
        let divider = NSBox(); divider.boxType = .separator
        let stack = NSStackView(views: [head, divider, rows])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 14
        stack.translatesAutoresizingMaskIntoConstraints = false
        let content = NSView()
        content.addSubview(stack)
        p.contentView = content
        NSLayoutConstraint.activate([
            icon.widthAnchor.constraint(equalToConstant: 48),
            icon.heightAnchor.constraint(equalToConstant: 48),
            stack.topAnchor.constraint(equalTo: content.topAnchor, constant: 18),
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 18),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -18),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: content.bottomAnchor, constant: -18),
            divider.widthAnchor.constraint(equalTo: stack.widthAnchor),
            rows.widthAnchor.constraint(equalTo: stack.widthAnchor),
        ])
        return p
    }
}
