// Quick Search: a floating, non-activating panel (/?mini=1) that opens over any app, full-screen ones included.
// Its web view stays loaded while hidden, so it appears at once. It hides when it loses focus, except to its own
// Quick Look preview, and it drives Quick Look itself (QuickPanel). Moved from the single-file app unchanged in
// behaviour.

import Cocoa
import Quartz
import Carbon.HIToolbox

let MINI_WIDTH: CGFloat = 760
let MINI_BAR: CGFloat = 58        // the panel's height with an empty search (app.css: .m-bar)

/// The Quick Search panel: borderless, floating, never activates the app, and drives Quick Look.
final class QuickPanel: NSPanel, QLPreviewPanelDataSource, QLPreviewPanelDelegate {
    var previewURL: URL?
    var onPreviewKey: ((NSEvent) -> Bool)?

    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }

    override func acceptsPreviewPanelControl(_ panel: QLPreviewPanel!) -> Bool { true }
    override func beginPreviewPanelControl(_ panel: QLPreviewPanel!) { panel.dataSource = self; panel.delegate = self }
    override func endPreviewPanelControl(_ panel: QLPreviewPanel!) { panel.dataSource = nil; panel.delegate = nil }
    func numberOfPreviewItems(in panel: QLPreviewPanel!) -> Int { previewURL == nil ? 0 : 1 }
    func previewPanel(_ panel: QLPreviewPanel!, previewItemAt index: Int) -> QLPreviewItem! { previewURL as NSURL? }
    func previewPanel(_ panel: QLPreviewPanel!, handle event: NSEvent!) -> Bool {
        event.type == .keyDown ? (onPreviewKey?(event) ?? false) : false
    }
}

/// A rounded-rect mask for NSVisualEffectView (stretchable, so it fits any panel size).
func roundedMask(_ r: CGFloat) -> NSImage {
    let edge = 2 * r + 1
    let img = NSImage(size: NSSize(width: edge, height: edge), flipped: false) { rect in
        NSColor.black.setFill()
        NSBezierPath(roundedRect: rect, xRadius: r, yRadius: r).fill()
        return true
    }
    img.capInsets = NSEdgeInsets(top: r, left: r, bottom: r, right: r)
    img.resizingMode = .stretch
    return img
}

@MainActor
final class QuickSearchController: NSObject, NSWindowDelegate {
    private(set) var panel: QuickPanel!
    let pane = WebPane(messages: ["mini"],
                       startScript: "document.documentElement.classList.add('mini','in-panel')",
                       transparent: true)
    /// The panel's "open in the app" action: the query, scope, collection or path to open in the main window.
    var onOpenInApp: (([String: Any]) -> Void)?

    private var miniTop: CGFloat = 0           // the panel's top edge, kept while its height follows the results
    private var miniHeight: CGFloat = MINI_BAR
    private var pendingQuery: String?          // a mysearch://mini link arrived before the panel was shown
    private var quickLookObserver: NSObjectProtocol?

    private static let waitingHTML = """
    <html><head><style>:root{color-scheme:light dark}body{margin:0;font:13px -apple-system;color:#7A8594;
    height:58px;display:flex;align-items:center;padding:0 20px;background:transparent}</style></head>
    <body>Waiting for the search service… If this stays, run <code>&nbsp;mse service install&nbsp;</code>.</body></html>
    """

    override init() {
        super.init()
        pane.waitingHTML = QuickSearchController.waitingHTML
        pane.retryAfter = 3
        pane.onMessage = { [weak self] name, body in
            if name == "mini" { self?.message(body) }
        }
        pane.onLoaded = { [weak self] in
            guard let self, self.panel.isVisible else { return }
            self.pane.run("window.miniShow && miniShow()")
        }
        buildPanel()
    }

    /// Loads the panel's page. Called once at launch so the first show is instant.
    func start() { pane.load(MSEClient.shared.miniURL()) }

    private func buildPanel() {
        let web = pane.web
        web.wantsLayer = true
        web.layer?.cornerRadius = 14
        web.layer?.masksToBounds = true
        web.autoresizingMask = [.width, .height]
        web.frame = NSRect(x: 0, y: 0, width: MINI_WIDTH, height: MINI_BAR)

        panel = QuickPanel(contentRect: NSRect(x: 0, y: 0, width: MINI_WIDTH, height: MINI_BAR),
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
        panel.onPreviewKey = { [weak self] e in
            guard let self else { return false }
            let cmd = e.modifierFlags.contains(.command)
            switch Int(e.keyCode) {
            case kVK_DownArrow: self.pane.web.evaluateJavaScript("moveMini(1)")
            case kVK_UpArrow: self.pane.web.evaluateJavaScript("moveMini(-1)")
            case kVK_Return: self.closeQuickLook(); self.pane.web.evaluateJavaScript("miniOpen(miniCurrent(), \(cmd))")
            case kVK_Escape: self.closeQuickLook()
            case kVK_ANSI_Y where cmd: self.closeQuickLook()
            default: return false
            }
            return true
        }
        let effect = NSVisualEffectView(frame: panel.contentLayoutRect)
        effect.material = .popover
        effect.blendingMode = .behindWindow
        effect.state = .active
        effect.maskImage = roundedMask(14)
        effect.autoresizingMask = [.width, .height]
        web.frame = effect.bounds
        effect.addSubview(web)
        panel.contentView = effect

        quickLookObserver = NotificationCenter.default.addObserver(forName: NSWindow.willCloseNotification, object: nil, queue: .main) { [weak self] n in
            MainActor.assumeIsolated {
                guard let self, n.object is QLPreviewPanel else { return }
                self.pane.run("window.miniQuickLookClosed && miniQuickLookClosed()")
                if self.panel.isVisible { DispatchQueue.main.async { self.panel.makeKey() } }   // back to the search field
            }
        }
    }

    var isVisible: Bool { panel.isVisible }

    func toggle() {
        if panel.isVisible && panel.isKeyWindow { hide() } else { show(query: nil) }
    }

    /// Shows the panel in the top third of the screen under the pointer. A query from a mysearch://mini link
    /// (or `query`) is opened in it.
    func show(query: String?) {
        if let query { pendingQuery = query }
        let mouse = NSEvent.mouseLocation
        let screen = NSScreen.screens.first { NSMouseInRect(mouse, $0.frame, false) } ?? NSScreen.main ?? NSScreen.screens[0]
        let vf = screen.visibleFrame
        let width = min(MINI_WIDTH, vf.width - 40)
        miniTop = vf.maxY - (vf.height * 0.2).rounded()             // in the top third, like Spotlight
        let h = min(miniHeight, maxHeight(vf))
        panel.setFrame(NSRect(x: (vf.midX - width / 2).rounded(), y: miniTop - h, width: width, height: h), display: true)
        panel.makeKeyAndOrderFront(nil)
        panel.makeFirstResponder(pane.web)
        pane.run("window.miniShow && miniShow()")
        if let q = pendingQuery {
            pendingQuery = nil
            pane.run("window.miniOpenQuery && miniOpenQuery(\(js(q)))")
        }
    }

    func hide() {
        closeQuickLook()
        panel.orderOut(nil)
    }

    private func maxHeight(_ vf: NSRect) -> CGFloat { min(640, vf.height * 0.72) }

    private func resize(_ height: CGFloat) {
        miniHeight = max(MINI_BAR, height)
        guard panel.isVisible, let vf = panel.screen?.visibleFrame else { return }
        let h = min(miniHeight, maxHeight(vf))
        var f = panel.frame
        if abs(f.height - h) < 0.5 { return }
        f.size.height = h
        f.origin.y = miniTop - h
        panel.setFrame(f, display: true)
        panel.invalidateShadow()
    }

    /// Quick Look over the panel. Like Finder's, it takes the keyboard but hands ↑↓, ↵, Esc and ⌘Y back to the
    /// panel (QuickPanel.previewPanel(_:handle:)), so the preview follows the selection.
    private func quickLook(_ path: String, update: Bool) {
        let url = URL(fileURLWithPath: path)
        var isDir: ObjCBool = false
        guard FileManager.default.fileExists(atPath: url.path, isDirectory: &isDir), !isDir.boolValue,
              let ql = QLPreviewPanel.shared() else { return }
        if update && !ql.isVisible { return }
        panel.previewURL = url
        ql.updateController()
        ql.reloadData()
        if !ql.isVisible { ql.makeKeyAndOrderFront(nil) }
    }

    func closeQuickLook() {
        if QLPreviewPanel.sharedPreviewPanelExists() && QLPreviewPanel.shared().isVisible { QLPreviewPanel.shared().orderOut(nil) }
        pane.run("window.miniQuickLookClosed && miniQuickLookClosed()")
    }

    /// Messages from the panel's page.
    private func message(_ d: [String: Any]) {
        switch d["type"] as? String {
        case "size":
            if let h = d["height"] as? NSNumber { resize(CGFloat(h.doubleValue)) }
        case "hide":
            hide()
        case "copy":
            if let text = d["text"] as? String, !text.isEmpty {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(text, forType: .string)
            }
        case "quicklook":
            if let path = d["path"] as? String, path.hasPrefix("/") { quickLook(path, update: (d["update"] as? Bool) ?? false) }
        case "quicklookClose":
            closeQuickLook()
        case "openInApp":
            var o: [String: Any] = [:]
            for k in ["q", "scope", "collection", "path"] { if let v = d[k] as? String { o[k] = v } }
            o["ask"] = (d["ask"] as? Bool) ?? false
            hide()
            onOpenInApp?(o)
        case "ready":
            if panel.isVisible { pane.run("window.miniShow && miniShow()") }
        default: break
        }
    }

    // Quick Search hides when it loses focus, except to its own Quick Look preview.
    func windowDidResignKey(_ n: Notification) {
        guard n.object as? NSWindow === panel else { return }
        DispatchQueue.main.async { [weak self] in
            guard let self, self.panel.isVisible else { return }
            if NSApp.keyWindow is QLPreviewPanel || (QLPreviewPanel.sharedPreviewPanelExists() && QLPreviewPanel.shared().isVisible) { return }
            if NSApp.keyWindow === self.panel { return }
            self.hide()
        }
    }
}
