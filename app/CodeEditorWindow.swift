// The code editor window: one standalone window (never a sheet on the main browser) with a tab per open file, a
// status bar (cursor, language, encoding, line endings) and ⌘S. Files keep their own encoding and line endings on
// save (see CodeEditorFile.swift). Several files open as tabs in the same window, like FinderFlow's editor.

import Cocoa

// MARK: - One open file

@MainActor
final class CodeDoc {
    let url: URL
    let pane: CodeEditorPane
    var format: EditorFileFormat
    var diskDate: Date?
    var modified = false { didSet { if oldValue != modified { onChange?() } } }
    var onChange: (() -> Void)?

    var name: String { url.lastPathComponent }

    init(url: URL) throws {
        let file = try EditorFileIO.read(url)
        self.url = url
        self.format = file.format
        self.diskDate = file.modified
        pane = CodeEditorPane(language: SyntaxLanguage.forURL(url), wrap: false, lineNumbers: true)
        pane.setText(file.text)
        pane.onTextChange = { [weak self] in self?.modified = true }
    }

    func save() -> Bool {
        guard EditorSaver.save(text: pane.text, to: url, format: &format, diskDate: &diskDate) else { return false }
        modified = false
        return true
    }

    /// Re-reads the file when it changed on disk and there are no unsaved edits here.
    func reloadIfChangedOnDisk() {
        guard !modified, let known = diskDate, let now = EditorFileIO.modificationDate(url),
              abs(now.timeIntervalSince(known)) > 0.5, let file = try? EditorFileIO.read(url) else { return }
        pane.setText(file.text)
        format = file.format
        diskDate = file.modified
    }
}

// MARK: - Tab bar

final class EditorTabBar: NSView {
    struct Item { var title: String; var modified: Bool }

    var items: [Item] = [] { didSet { needsDisplay = true } }
    var selected = 0 { didSet { needsDisplay = true } }
    var onSelect: ((Int) -> Void)?
    var onClose: ((Int) -> Void)?

    override var isFlipped: Bool { true }

    private func tabWidth() -> CGFloat {
        guard !items.isEmpty else { return 0 }
        return min(220, max(110, bounds.width / CGFloat(items.count)))
    }

    private func closeRect(_ index: Int) -> NSRect {
        let w = tabWidth()
        return NSRect(x: CGFloat(index) * w + w - 24, y: (bounds.height - 16) / 2, width: 16, height: 16)
    }

    override func draw(_ dirtyRect: NSRect) {
        NSColor.underPageBackgroundColor.setFill()
        bounds.fill()
        let w = tabWidth()
        let para = NSMutableParagraphStyle()
        para.lineBreakMode = .byTruncatingMiddle
        for (i, item) in items.enumerated() {
            let r = NSRect(x: CGFloat(i) * w, y: 0, width: w, height: bounds.height)
            if i == selected {
                NSColor.textBackgroundColor.setFill()
                r.fill()
            }
            NSColor.separatorColor.setFill()
            NSRect(x: r.maxX - 1, y: 4, width: 1, height: bounds.height - 8).fill()
            let attrs: [NSAttributedString.Key: Any] = [
                .font: NSFont.systemFont(ofSize: 12, weight: i == selected ? .medium : .regular),
                .foregroundColor: i == selected ? NSColor.labelColor : NSColor.secondaryLabelColor,
                .paragraphStyle: para,
            ]
            let textRect = NSRect(x: r.minX + 12, y: (bounds.height - 16) / 2, width: w - 40, height: 16)
            NSAttributedString(string: item.title, attributes: attrs).draw(in: textRect)
            let glyph = item.modified ? "●" : "×"
            let gAttrs: [NSAttributedString.Key: Any] = [
                .font: NSFont.systemFont(ofSize: item.modified ? 9 : 15, weight: .regular),
                .foregroundColor: NSColor.secondaryLabelColor,
            ]
            let g = NSAttributedString(string: glyph, attributes: gAttrs)
            let gs = g.size()
            let cr = closeRect(i)
            g.draw(at: NSPoint(x: cr.midX - gs.width / 2, y: cr.midY - gs.height / 2))
        }
        NSColor.separatorColor.setFill()
        NSRect(x: 0, y: bounds.height - 1, width: bounds.width, height: 1).fill()
    }

    private func index(at event: NSEvent) -> Int? {
        let p = convert(event.locationInWindow, from: nil)
        let w = tabWidth()
        guard w > 0 else { return nil }
        let i = Int(p.x / w)
        return i >= 0 && i < items.count ? i : nil
    }

    override func mouseDown(with event: NSEvent) {
        guard let i = index(at: event) else { return }
        let p = convert(event.locationInWindow, from: nil)
        if closeRect(i).insetBy(dx: -4, dy: -4).contains(p) { onClose?(i) } else { onSelect?(i) }
    }

    override func otherMouseDown(with event: NSEvent) {
        if let i = index(at: event) { onClose?(i) }
    }

    override var mouseDownCanMoveWindow: Bool { false }
}

// MARK: - Window controller

@MainActor
final class CodeEditorWindowController: NSWindowController, NSWindowDelegate {
    private(set) var docs: [CodeDoc] = []
    private var active: CodeDoc?
    private let tabBar = EditorTabBar()
    private let container = NSView()
    private let status = NSTextField(labelWithString: "")
    private var closeConfirmed = false
    var onClosed: (() -> Void)?

    init(startup: Bool) {
        let win = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1040, height: 720),
                           styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        win.isReleasedWhenClosed = false
        win.minSize = NSSize(width: 560, height: 360)
        win.tabbingMode = .disallowed
        win.title = "Editor"
        super.init(window: win)
        win.delegate = self
        buildUI(win)
        win.center()
        win.setFrameAutosaveName("MSECodeEditorWindow")
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) is not used") }

    private func buildUI(_ win: NSWindow) {
        let root = NSView()
        win.contentView = root
        for v in [tabBar, container, status] as [NSView] {
            v.translatesAutoresizingMaskIntoConstraints = false
            root.addSubview(v)
        }
        status.font = NSFont.systemFont(ofSize: 11)
        status.textColor = .secondaryLabelColor
        status.lineBreakMode = .byTruncatingTail
        NSLayoutConstraint.activate([
            tabBar.topAnchor.constraint(equalTo: root.topAnchor),
            tabBar.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            tabBar.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            tabBar.heightAnchor.constraint(equalToConstant: 30),
            container.topAnchor.constraint(equalTo: tabBar.bottomAnchor),
            container.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            container.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            container.bottomAnchor.constraint(equalTo: status.topAnchor, constant: -3),
            status.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 10),
            status.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -10),
            status.bottomAnchor.constraint(equalTo: root.bottomAnchor, constant: -4),
            status.heightAnchor.constraint(equalToConstant: 15),
        ])
        tabBar.onSelect = { [weak self] i in self?.select(index: i) }
        tabBar.onClose = { [weak self] i in self?.closeTab(at: i) }
    }

    // MARK: opening

    /// Opens `url` as a tab (or selects the tab it already has). Returns false and tells the user when it cannot.
    @discardableResult
    func open(_ url: URL) -> Bool {
        let std = url.standardizedFileURL
        if let existing = docs.first(where: { $0.url.standardizedFileURL.path == std.path }) {
            select(doc: existing)
            show()
            return true
        }
        do {
            let doc = try CodeDoc(url: std)
            doc.onChange = { [weak self] in self?.refreshChrome() }
            doc.pane.onSelectionChange = { [weak self] in self?.refreshStatus() }
            docs.append(doc)
            select(doc: doc)
            show()
            return true
        } catch {
            let a = NSAlert()
            a.messageText = "\"\(url.lastPathComponent)\" cannot be opened in the editor."
            a.informativeText = error.localizedDescription
            a.runModal()
            return false
        }
    }

    func show() {
        showWindow(nil)
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    // MARK: tabs

    private func select(index i: Int) {
        guard docs.indices.contains(i) else { return }
        select(doc: docs[i])
    }

    private func select(doc: CodeDoc) {
        active?.pane.scrollView.removeFromSuperview()
        active = doc
        let sv = doc.pane.scrollView
        sv.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(sv)
        NSLayoutConstraint.activate([
            sv.topAnchor.constraint(equalTo: container.topAnchor),
            sv.bottomAnchor.constraint(equalTo: container.bottomAnchor),
            sv.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            sv.trailingAnchor.constraint(equalTo: container.trailingAnchor),
        ])
        window?.makeFirstResponder(doc.pane.textView)
        refreshChrome()
        doc.reloadIfChangedOnDisk()
    }

    private func closeTab(at i: Int) {
        guard docs.indices.contains(i) else { return }
        let doc = docs[i]
        if doc.modified {
            let a = NSAlert()
            a.messageText = "Save changes to \"\(doc.name)\"?"
            a.informativeText = "Your changes will be lost if you do not save them."
            a.addButton(withTitle: "Save")
            a.addButton(withTitle: "Don't Save")
            a.addButton(withTitle: "Cancel")
            a.beginSheetModal(for: window!) { [weak self] resp in
                guard let self else { return }
                switch resp {
                case .alertFirstButtonReturn: if doc.save() { self.removeDoc(doc) }
                case .alertSecondButtonReturn: self.removeDoc(doc)
                default: break
                }
            }
        } else {
            removeDoc(doc)
        }
    }

    private func removeDoc(_ doc: CodeDoc) {
        guard let i = docs.firstIndex(where: { $0 === doc }) else { return }
        let wasActive = active === doc
        doc.pane.scrollView.removeFromSuperview()
        docs.remove(at: i)
        if docs.isEmpty {
            active = nil
            closeConfirmed = true
            window?.close()
            return
        }
        if wasActive {
            active = nil
            select(index: min(i, docs.count - 1))
        } else {
            refreshChrome()
        }
    }

    // MARK: chrome

    private func refreshChrome() {
        tabBar.items = docs.map { .init(title: $0.name, modified: $0.modified) }
        if let a = active, let i = docs.firstIndex(where: { $0 === a }) { tabBar.selected = i }
        window?.title = active?.name ?? "Editor"
        window?.representedURL = active?.url
        window?.isDocumentEdited = docs.contains { $0.modified }
        refreshStatus()
    }

    private func refreshStatus() {
        guard let d = active else { status.stringValue = ""; return }
        let lines = d.pane.ruler?.lineCount ?? 0
        status.stringValue = "\(d.pane.cursorDescription)   ·   \(lines) lines   ·   \(d.pane.language.displayName)   ·   \(d.format.encodingLabel)   ·   \(d.format.lineEnding.label)   ·   \(d.url.deletingLastPathComponent().path)"
    }

    // MARK: saving

    @objc func saveDocument(_ sender: Any?) {
        guard let d = active else { return }
        _ = d.save()
    }

    @objc func saveAllDocuments(_ sender: Any?) { _ = saveAllDirty() }

    var dirtyNames: [String] { docs.filter { $0.modified }.map { $0.name } }

    func saveAllDirty() -> Bool {
        for d in docs where d.modified { if !d.save() { return false } }
        return true
    }

    // MARK: window

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        if closeConfirmed || dirtyNames.isEmpty { return true }
        let a = NSAlert()
        let names = dirtyNames
        a.messageText = names.count == 1 ? "Save changes to \"\(names[0])\"?" : "\(names.count) files have unsaved changes."
        a.informativeText = names.count == 1 ? "Your changes will be lost if you do not save them." : names.joined(separator: ", ")
        a.addButton(withTitle: names.count == 1 ? "Save" : "Save All")
        a.addButton(withTitle: "Don't Save")
        a.addButton(withTitle: "Cancel")
        a.beginSheetModal(for: sender) { [weak self] resp in
            guard let self else { return }
            switch resp {
            case .alertFirstButtonReturn:
                if self.saveAllDirty() { self.closeConfirmed = true; sender.close() }
            case .alertSecondButtonReturn:
                self.closeConfirmed = true; sender.close()
            default: break
            }
        }
        return false
    }

    func windowDidBecomeKey(_ notification: Notification) {
        active?.reloadIfChangedOnDisk()
    }

    func windowWillClose(_ notification: Notification) {
        for d in docs { d.pane.scrollView.removeFromSuperview() }
        docs = []
        active = nil
        onClosed?()
    }
}

extension CodeEditorWindowController: NSMenuItemValidation {
    func validateMenuItem(_ item: NSMenuItem) -> Bool {
        if item.action == #selector(saveDocument(_:)) { return active != nil }
        return true
    }
}
