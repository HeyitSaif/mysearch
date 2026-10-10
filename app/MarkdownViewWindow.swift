// The Markdown window: a reader (rendered page in a WKWebView with JavaScript DISABLED) and an editor (the native
// CodeEditorPane with Markdown colours), as Read / Edit / Split. One standalone window per file. Links to other
// Markdown files navigate inside the same window (with a Back button); http, https and mailto open in the default app;
// every other link is ignored. ⌘S saves in the file's own encoding and line endings; closing with unsaved edits asks.
//
// The window structure (reader, edit toggle, in-place .md navigation, JavaScript off, link filtering) follows
// FinderFlow v1.5.2 (FinderFlow/MarkdownReaderView.swift). FinderFlow is MIT licensed, Copyright (c) 2026 Gobinda
// Tarafdar. The full notice is in THIRD_PARTY_NOTICES.md.

import Cocoa
import WebKit

@MainActor
final class MarkdownWindowController: NSWindowController, NSWindowDelegate, WKNavigationDelegate {
    enum Mode: Int { case read = 0, edit, split }

    private(set) var url: URL
    private var history: [URL] = []
    private let pane = CodeEditorPane(language: .markdown, wrap: true, lineNumbers: false)
    private let webView: WKWebView
    private let split = NSSplitView()
    private let modeControl = NSSegmentedControl(labels: ["Read", "Edit", "Split"], trackingMode: .selectOne, target: nil, action: nil)
    private let backButton = NSButton()
    private let editedLabel = NSTextField(labelWithString: "")
    private var format = EditorFileFormat()
    private var diskDate: Date?
    private var modified = false { didSet { refreshChrome() } }
    private var mode: Mode = .read
    private var renderWork: DispatchWorkItem?
    private let tempHTML = FileManager.default.temporaryDirectory.appendingPathComponent("mysearch-md-\(UUID().uuidString).html")
    private var pendingScroll: Double = 0
    private var pendingAnchor: String?
    private var closeConfirmed = false
    var onClosed: ((MarkdownWindowController) -> Void)?

    init(url: URL, offset: Int) throws {
        let file = try EditorFileIO.read(url)
        self.url = url
        let cfg = WKWebViewConfiguration()
        cfg.defaultWebpagePreferences.allowsContentJavaScript = false          // no scripts, from the page or from the document
        cfg.preferences.javaScriptCanOpenWindowsAutomatically = false
        cfg.websiteDataStore = .nonPersistent()
        webView = WKWebView(frame: .zero, configuration: cfg)
        let win = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 920, height: 740),
                           styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        win.isReleasedWhenClosed = false
        win.minSize = NSSize(width: 520, height: 360)
        win.tabbingMode = .disallowed
        super.init(window: win)
        win.delegate = self
        format = file.format
        diskDate = file.modified
        webView.navigationDelegate = self
        webView.underPageBackgroundColor = .textBackgroundColor
        webView.allowsMagnification = true
        buildUI(win)
        win.center()
        var f = win.frame
        f.origin.x += CGFloat(26 * offset)
        f.origin.y -= CGFloat(26 * offset)
        win.setFrame(f, display: false)
        pane.setText(file.text)
        pane.onTextChange = { [weak self] in
            guard let self else { return }
            self.modified = true
            self.scheduleRender()
        }
        refreshChrome()
        setMode(.read)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) is not used") }

    // MARK: UI

    private func buildUI(_ win: NSWindow) {
        let root = NSView()
        win.contentView = root

        let bar = NSView()
        bar.translatesAutoresizingMaskIntoConstraints = false
        split.translatesAutoresizingMaskIntoConstraints = false
        root.addSubview(bar)
        root.addSubview(split)

        backButton.bezelStyle = .texturedRounded
        backButton.image = NSImage(systemSymbolName: "chevron.left", accessibilityDescription: "Back")
        backButton.toolTip = "Back to the previous document"
        backButton.target = self
        backButton.action = #selector(backAction)
        backButton.isEnabled = false
        modeControl.target = self
        modeControl.action = #selector(modeChanged)
        modeControl.segmentStyle = .texturedRounded
        modeControl.selectedSegment = 0
        editedLabel.font = NSFont.systemFont(ofSize: 11)
        editedLabel.textColor = .secondaryLabelColor
        for v in [backButton, modeControl, editedLabel] as [NSView] {
            v.translatesAutoresizingMaskIntoConstraints = false
            bar.addSubview(v)
        }
        let line = NSBox()
        line.boxType = .separator
        line.translatesAutoresizingMaskIntoConstraints = false
        bar.addSubview(line)

        split.isVertical = true
        split.dividerStyle = .thin
        split.addArrangedSubview(pane.scrollView)
        split.addArrangedSubview(webView)

        NSLayoutConstraint.activate([
            bar.topAnchor.constraint(equalTo: root.topAnchor),
            bar.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            bar.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            bar.heightAnchor.constraint(equalToConstant: 38),
            split.topAnchor.constraint(equalTo: bar.bottomAnchor),
            split.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            split.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            split.bottomAnchor.constraint(equalTo: root.bottomAnchor),
            backButton.leadingAnchor.constraint(equalTo: bar.leadingAnchor, constant: 10),
            backButton.centerYAnchor.constraint(equalTo: bar.centerYAnchor),
            modeControl.centerXAnchor.constraint(equalTo: bar.centerXAnchor),
            modeControl.centerYAnchor.constraint(equalTo: bar.centerYAnchor),
            editedLabel.trailingAnchor.constraint(equalTo: bar.trailingAnchor, constant: -12),
            editedLabel.centerYAnchor.constraint(equalTo: bar.centerYAnchor),
            line.leadingAnchor.constraint(equalTo: bar.leadingAnchor),
            line.trailingAnchor.constraint(equalTo: bar.trailingAnchor),
            line.bottomAnchor.constraint(equalTo: bar.bottomAnchor),
        ])
    }

    @objc private func modeChanged() { setMode(Mode(rawValue: modeControl.selectedSegment) ?? .read) }

    func setMode(_ m: Mode) {
        mode = m
        modeControl.selectedSegment = m.rawValue
        pane.scrollView.isHidden = (m == .read)
        webView.isHidden = (m == .edit)
        split.adjustSubviews()
        if m == .split {
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                self.split.setPosition(self.split.bounds.width / 2, ofDividerAt: 0)
            }
        }
        if m != .edit { render(keepScroll: webView.url != nil) }
        if m != .read { window?.makeFirstResponder(pane.textView) }
    }

    private func refreshChrome() {
        window?.title = url.lastPathComponent
        window?.representedURL = url
        window?.isDocumentEdited = modified
        editedLabel.stringValue = modified ? "Edited" : ""
        backButton.isEnabled = !history.isEmpty
    }

    // MARK: rendering

    private func scheduleRender() {
        guard mode != .edit else { return }
        renderWork?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.render(keepScroll: true) }
        renderWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3, execute: work)
    }

    private func render(keepScroll: Bool) {
        renderWork?.cancel()
        let html = MarkdownRenderer.page(markdown: pane.text, title: url.lastPathComponent,
                                         baseDirectory: url.deletingLastPathComponent())
        do { try html.write(to: tempHTML, atomically: true, encoding: .utf8) } catch { return }
        if keepScroll {
            webView.evaluateJavaScript("window.scrollY") { [weak self] value, _ in
                self?.pendingScroll = (value as? Double) ?? 0
                self?.loadRendered()
            }
        } else {
            pendingScroll = 0
            loadRendered()
        }
    }

    private func loadRendered() {
        // Read access is the whole disk so relative images (../img/x.png) work; the page cannot run scripts, and the
        // Content Security Policy in the page limits what it may load.
        webView.loadFileURL(tempHTML, allowingReadAccessTo: URL(fileURLWithPath: "/"))
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        if let anchor = pendingAnchor {
            pendingAnchor = nil
            scrollToAnchor(anchor)
        } else if pendingScroll > 0 {
            webView.evaluateJavaScript("window.scrollTo(0, \(pendingScroll))", completionHandler: nil)
        }
    }

    private func scrollToAnchor(_ raw: String) {
        let id = raw.removingPercentEncoding ?? raw
        guard let data = try? JSONSerialization.data(withJSONObject: [id]), let json = String(data: data, encoding: .utf8) else { return }
        webView.evaluateJavaScript("(function(a){var e=document.getElementById(a[0]); if(e){e.scrollIntoView();}})(\(json))", completionHandler: nil)
    }

    // MARK: links

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 preferences: WKWebpagePreferences, decisionHandler: @escaping (WKNavigationActionPolicy, WKWebpagePreferences) -> Void) {
        preferences.allowsContentJavaScript = false
        guard let target = navigationAction.request.url else { decisionHandler(.cancel, preferences); return }
        if navigationAction.navigationType != .linkActivated {
            // Only the page this window rendered itself may load.
            let ours = target.isFileURL && target.lastPathComponent == tempHTML.lastPathComponent
            decisionHandler(ours ? .allow : .cancel, preferences)
            return
        }
        decisionHandler(.cancel, preferences)
        handleLink(target)
    }

    private func handleLink(_ target: URL) {
        guard let scheme = target.scheme?.lowercased() else { return }
        switch scheme {
        case "http", "https", "mailto":
            NSWorkspace.shared.open(target)
        case "file":
            let path = URL(fileURLWithPath: target.path).standardizedFileURL
            let here = url.deletingLastPathComponent().standardizedFileURL
            if path.path == here.path {                       // "#heading": the page's own anchors resolve against its folder
                if let f = target.fragment { scrollToAnchor(f) }
                return
            }
            var isDir: ObjCBool = false
            guard FileManager.default.fileExists(atPath: path.path, isDirectory: &isDir) else {
                let a = NSAlert()
                a.messageText = "\"\(path.lastPathComponent)\" does not exist."
                a.informativeText = path.deletingLastPathComponent().path
                a.runModal()
                return
            }
            if isDir.boolValue {
                NSWorkspace.shared.activateFileViewerSelecting([path])
            } else if TextFileDetector.isMarkdown(path) {
                navigate(to: path, anchor: target.fragment)
            } else if TextFileDetector.isEditableText(path) {
                EditorRouter.shared.open(path)
            } else {
                NSWorkspace.shared.activateFileViewerSelecting([path])   // never launch a linked file; show it
            }
        default:
            break
        }
    }

    // MARK: navigating between documents

    private func confirmDiscard(then proceed: @escaping () -> Void) {
        guard modified, let win = window else { proceed(); return }
        let a = NSAlert()
        a.messageText = "Save changes to \"\(url.lastPathComponent)\"?"
        a.informativeText = "Your changes will be lost if you do not save them."
        a.addButton(withTitle: "Save")
        a.addButton(withTitle: "Don't Save")
        a.addButton(withTitle: "Cancel")
        a.beginSheetModal(for: win) { [weak self] resp in
            guard let self else { return }
            switch resp {
            case .alertFirstButtonReturn: if self.save() { proceed() }
            case .alertSecondButtonReturn: proceed()
            default: break
            }
        }
    }

    private func navigate(to newURL: URL, anchor: String?) {
        confirmDiscard { [weak self] in
            guard let self else { return }
            let previous = self.url
            if self.loadDocument(newURL, anchor: anchor) { self.history.append(previous); self.refreshChrome() }
        }
    }

    @objc private func backAction() {
        guard let prev = history.last else { return }
        confirmDiscard { [weak self] in
            guard let self else { return }
            if self.loadDocument(prev, anchor: nil) { self.history.removeLast(); self.refreshChrome() }
        }
    }

    /// Replaces the document shown in this window. Returns false (and tells the user) when it cannot be read.
    private func loadDocument(_ newURL: URL, anchor: String?) -> Bool {
        do {
            let file = try EditorFileIO.read(newURL)
            url = newURL
            format = file.format
            diskDate = file.modified
            pane.setText(file.text)
            modified = false
            pendingAnchor = anchor
            if mode != .edit { render(keepScroll: false) }
            refreshChrome()
            EditorRouter.shared.markdownWindowChangedURL(self)
            return true
        } catch {
            let a = NSAlert()
            a.messageText = "\"\(newURL.lastPathComponent)\" cannot be opened."
            a.informativeText = error.localizedDescription
            a.runModal()
            return false
        }
    }

    // MARK: saving

    @discardableResult
    private func save() -> Bool {
        guard EditorSaver.save(text: pane.text, to: url, format: &format, diskDate: &diskDate) else { return false }
        modified = false
        return true
    }

    @objc func saveDocument(_ sender: Any?) { save() }

    var dirtyNames: [String] { modified ? [url.lastPathComponent] : [] }
    func saveAllDirty() -> Bool { modified ? save() : true }

    // MARK: window

    func show() {
        showWindow(nil)
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        if closeConfirmed || !modified { return true }
        let a = NSAlert()
        a.messageText = "Save changes to \"\(url.lastPathComponent)\"?"
        a.informativeText = "Your changes will be lost if you do not save them."
        a.addButton(withTitle: "Save")
        a.addButton(withTitle: "Don't Save")
        a.addButton(withTitle: "Cancel")
        a.beginSheetModal(for: sender) { [weak self] resp in
            guard let self else { return }
            switch resp {
            case .alertFirstButtonReturn: if self.save() { self.closeConfirmed = true; sender.close() }
            case .alertSecondButtonReturn: self.closeConfirmed = true; sender.close()
            default: break
            }
        }
        return false
    }

    func windowDidBecomeKey(_ notification: Notification) {
        guard !modified, let known = diskDate, let now = EditorFileIO.modificationDate(url),
              abs(now.timeIntervalSince(known)) > 0.5, let file = try? EditorFileIO.read(url) else { return }
        format = file.format
        diskDate = file.modified
        pane.setText(file.text)
        if mode != .edit { render(keepScroll: true) }
    }

    func windowWillClose(_ notification: Notification) {
        renderWork?.cancel()
        webView.navigationDelegate = nil
        webView.stopLoading()
        try? FileManager.default.removeItem(at: tempHTML)       // our own temporary page, never the user's file
        onClosed?(self)
    }
}

extension MarkdownWindowController: NSMenuItemValidation {
    func validateMenuItem(_ item: NSMenuItem) -> Bool {
        if item.action == #selector(saveDocument(_:)) { return true }
        return true
    }
}
