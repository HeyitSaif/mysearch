// Quick Search: a floating, non-activating panel that opens over any app, full-screen ones included. It is native
// (AppKit): the search field, the sectioned list and the footer are QuickSearchView, driven by QuickSearchModel. The view
// is built at launch and only its rows change on each query, so the first show is instant. The panel hides when it loses
// focus, except to its own Quick Look preview, and it drives Quick Look itself (QuickPanel). The web page (/?mini=1) is no
// longer used by the panel.

import Cocoa
import Quartz
import Carbon.HIToolbox

/// The panel's width. The list shows names, folders, sizes and dates in it.
let QS_WIDTH: CGFloat = 720

/// The panel: borderless, floating, never activates the app. It hands each key down to the controller before the field
/// editor sees it (sendEvent), and it is Quick Look's responder while the preview is open.
final class QuickPanel: NSPanel, QLPreviewPanelDataSource, QLPreviewPanelDelegate {
    var previewURL: URL?
    /// Each key down, before the field editor: true when the key was handled.
    var onKeyDown: ((NSEvent) -> Bool)?
    /// Each key down while Quick Look is the key window: the preview takes the keyboard, as Finder's does.
    var onPreviewKey: ((NSEvent) -> Bool)?

    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }

    override func sendEvent(_ event: NSEvent) {
        if event.type == .keyDown, let onKeyDown, onKeyDown(event) { return }
        super.sendEvent(event)
    }

    override func acceptsPreviewPanelControl(_ panel: QLPreviewPanel!) -> Bool { true }
    override func beginPreviewPanelControl(_ panel: QLPreviewPanel!) { panel.dataSource = self; panel.delegate = self }
    override func endPreviewPanelControl(_ panel: QLPreviewPanel!) { panel.dataSource = nil; panel.delegate = nil }
    func numberOfPreviewItems(in panel: QLPreviewPanel!) -> Int { previewURL == nil ? 0 : 1 }
    func previewPanel(_ panel: QLPreviewPanel!, previewItemAt index: Int) -> QLPreviewItem! { previewURL as NSURL? }
    func previewPanel(_ panel: QLPreviewPanel!, handle event: NSEvent!) -> Bool {
        event.type == .keyDown ? (onPreviewKey?(event) ?? false) : false
    }
}

@MainActor
final class QuickSearchController: NSObject, NSWindowDelegate, QuickSearchHost {
    private(set) var panel: QuickPanel!
    let model = QuickSearchModel()
    private(set) var view: QuickSearchView!
    /// The panel's "open in the app" action: the query and scope to open in the main window.
    var onOpenInApp: (([String: Any]) -> Void)?

    /// The screen area the panel is placed in, and the panel's top edge (kept while its height follows the rows).
    private var area = NSRect(x: 0, y: 0, width: 1440, height: 900)
    private var topY: CGFloat = 0
    /// Snapshot mode places the panel off-screen and keeps it sized, without ever ordering it in.
    private var offscreen = false
    private var quickLookObserver: NSObjectProtocol?

    override init() {
        super.init()
        view = QuickSearchView(model: model)
        buildPanel()
        model.host = self
        model.onChange = { [weak self] in self?.refresh() }
    }

    deinit {
        if let quickLookObserver { NotificationCenter.default.removeObserver(quickLookObserver) }
    }

    /// Called once at launch: the recent searches are read and the view is laid out, so the first show is instant.
    func start() {
        model.loadRecents()
        view.layoutSubtreeIfNeeded()
    }

    var isVisible: Bool { panel.isVisible }

    func toggle() {
        if panel.isVisible && panel.isKeyWindow { hide() } else { show(query: nil) }
    }

    /// Shows the panel in the top third of the screen under the pointer. With a query (a mysearch://mini link) the query is
    /// searched; without one the field is empty and the recent searches and files are listed.
    func show(query: String?) {
        model.setQuery(query ?? "", typed: false, force: query != nil)
        model.loadRecents()
        placePanel()
        view.setFieldText(model.query)
        panel.makeKeyAndOrderFront(nil)
        panel.makeFirstResponder(view.field)
        refresh()
    }

    func hide() {
        closeQuickLook()
        panel.orderOut(nil)
    }

    /// Snapshot only (SnapshotQuickSearch.swift): places the panel off-screen, sized for its rows, and never orders it in.
    func placeOffscreen(appearance: NSAppearance?) {
        offscreen = true
        area = NSRect(x: 0, y: 0, width: 1440, height: 900)
        topY = 720
        panel.appearance = appearance
        let h = view.desiredHeight(maxHeight: maxHeight())
        panel.setFrame(NSRect(x: -30000, y: topY - h - 30000, width: QS_WIDTH, height: h), display: false)
        view.layoutSubtreeIfNeeded()
    }

    // MARK: layout

    private func placePanel() {
        let mouse = NSEvent.mouseLocation
        let screen = NSScreen.screens.first { NSMouseInRect(mouse, $0.frame, false) } ?? NSScreen.main ?? NSScreen.screens[0]
        area = screen.visibleFrame
        topY = area.maxY - (area.height * 0.2).rounded()
        let width = min(QS_WIDTH, area.width - 40)
        let h = view.desiredHeight(maxHeight: maxHeight())
        panel.setFrame(NSRect(x: (area.midX - width / 2).rounded(), y: topY - h, width: width, height: h), display: true)
    }

    private func maxHeight() -> CGFloat { min(600, area.height * 0.7) }

    /// The model changed: the view is updated, and the panel follows the height of the rows.
    private func refresh() {
        view.update()
        if panel.isVisible || offscreen { resize() }
    }

    private func resize() {
        let h = view.desiredHeight(maxHeight: maxHeight())
        var f = panel.frame
        guard abs(f.height - h) > 0.5 else { return }
        f.size.height = h
        f.origin.y = topY - h
        panel.setFrame(f, display: true)
        panel.invalidateShadow()
    }

    private func buildPanel() {
        panel = QuickPanel(contentRect: NSRect(x: -30000, y: -30000, width: QS_WIDTH, height: QuickSearchView.chromeHeight),
                           styleMask: [.borderless, .nonactivatingPanel, .fullSizeContentView], backing: .buffered, defer: false)
        panel.isFloatingPanel = true
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .transient, .ignoresCycle]
        panel.hidesOnDeactivate = false
        panel.becomesKeyOnlyIfNeeded = false
        panel.isMovableByWindowBackground = false
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.isReleasedWhenClosed = false
        panel.animationBehavior = .utilityWindow
        panel.delegate = self
        panel.onKeyDown = { [weak self] e in self?.handleKey(e) ?? false }
        panel.onPreviewKey = { [weak self] e in self?.handlePreviewKey(e) ?? false }

        // macOS 26 and later: the system's Liquid Glass. Earlier systems: the popover material, rounded by its layer.
        if #available(macOS 26.0, *) {
            let glass = NSGlassEffectView()
            glass.cornerRadius = 16
            glass.contentView = view
            panel.contentView = glass
        } else {
            let effect = NSVisualEffectView(frame: NSRect(x: 0, y: 0, width: QS_WIDTH, height: QuickSearchView.chromeHeight))
            effect.material = .popover
            effect.blendingMode = .behindWindow
            effect.state = .active
            effect.wantsLayer = true
            effect.layer?.cornerRadius = 16
            effect.layer?.masksToBounds = true
            view.frame = effect.bounds
            view.autoresizingMask = [.width, .height]
            effect.addSubview(view)
            panel.contentView = effect
        }

        quickLookObserver = NotificationCenter.default.addObserver(forName: NSWindow.willCloseNotification, object: nil, queue: .main) { [weak self] n in
            MainActor.assumeIsolated {
                guard let self, n.object is QLPreviewPanel else { return }
                if self.panel.isVisible { DispatchQueue.main.async { self.panel.makeKey() } }   // back to the search field
            }
        }
    }

    // MARK: keys

    private var quickLookVisible: Bool { QLPreviewPanel.sharedPreviewPanelExists() && QLPreviewPanel.shared().isVisible }

    private func keyContext() -> QSKeyContext {
        QSKeyContext(fieldEmpty: model.query.trimmingCharacters(in: .whitespaces).isEmpty,
                     listFocused: model.listFocused,
                     fieldHasSelection: view.fieldHasSelectedText,
                     quickLookOpen: quickLookVisible)
    }

    private func action(for e: NSEvent) -> QSKeyAction? {
        QSKeys.action(keyCode: e.keyCode, chars: e.charactersIgnoringModifiers ?? "",
                      mods: e.modifierFlags.intersection(.deviceIndependentFlagsMask), ctx: keyContext())
    }

    private func handleKey(_ e: NSEvent) -> Bool {
        guard !view.fieldHasMarkedText else { return false }
        if let action = action(for: e) {
            run(action)
            return true
        }
        // ⌘V ⌘X ⌘A ⌘Z and ⌘C with a selection: to the field, through the responder chain
        let mods = e.modifierFlags.intersection(.deviceIndependentFlagsMask)
        if let selector = QSKeys.editSelector(chars: e.charactersIgnoringModifiers ?? "", mods: mods,
                                              fieldHasSelection: view.fieldHasSelectedText) {
            return NSApp.sendAction(selector, to: nil, from: panel)
        }
        return false
    }

    /// While Quick Look is the key window: ↑ ↓ move the selection (the preview follows it), ↵ opens, Esc and ␣ close it.
    private func handlePreviewKey(_ e: NSEvent) -> Bool {
        guard let action = action(for: e) else { return false }
        switch action {
        case .activate, .reveal:
            closeQuickLook()
            run(action)
        case .quickLook, .escape:
            closeQuickLook()
        default:
            run(action)
        }
        return true
    }

    private func run(_ action: QSKeyAction) {
        switch action {
        case .moveUp: model.moveSelection(-1)
        case .moveDown: model.moveSelection(1)
        case .activate: model.activateSelected(reveal: false)
        case .reveal: model.activateSelected(reveal: true)
        case .escape:
            if quickLookVisible { closeQuickLook() } else { hide() }
        case .nextSection: model.cycleSection(backward: false)
        case .previousSection: model.cycleSection(backward: true)
        case .section(let n): model.jumpToSection(n)
        case .quickLook: model.toggleQuickLook()
        case .openInApp: model.openInApp()
        case .copyPath: model.copySelectedPath()
        case .retry: model.retry()
        case .scope(let s): model.setScope(s)
        }
    }

    // MARK: Quick Look

    private func quickLook(_ url: URL, update: Bool = false) {
        guard let ql = QLPreviewPanel.shared() else { return }
        if update && !ql.isVisible { return }
        panel.previewURL = url
        ql.updateController()
        ql.reloadData()
        if !ql.isVisible { ql.makeKeyAndOrderFront(nil) }
    }

    func closeQuickLook() {
        if quickLookVisible { QLPreviewPanel.shared().orderOut(nil) }
    }

    // Quick Search hides when it loses focus, except to its own Quick Look preview.
    func windowDidResignKey(_ n: Notification) {
        guard n.object as? NSWindow === panel else { return }
        DispatchQueue.main.async { [weak self] in
            guard let self, self.panel.isVisible else { return }
            if NSApp.keyWindow is QLPreviewPanel || self.quickLookVisible { return }
            if NSApp.keyWindow === self.panel { return }
            self.hide()
        }
    }

    // MARK: QuickSearchHost

    func quickSearchHide() { hide() }

    func quickSearchOpenInApp(query: String, scope: String) {
        hide()
        onOpenInApp?(["q": query, "scope": scope, "ask": false])
    }

    func quickSearchToggleQuickLook(_ url: URL?) {
        if quickLookVisible {
            closeQuickLook()
        } else if let url {
            quickLook(url)
        }
    }

    func quickSearchCopy(_ text: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }

    func quickSearchSetField(_ text: String) { view.setFieldText(text) }

    func quickSearchSelectionMoved() {
        guard quickLookVisible, let url = model.selectedPreviewURL else { return }
        quickLook(url, update: true)
    }
}
