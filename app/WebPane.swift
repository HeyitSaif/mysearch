// The WebKit side: one WebPane wraps a WKWebView for the local service's pages. It carries the page
// messages (the `app`, `mini` and `layout` handlers), the native alert, confirm and prompt panels, external
// links to the default browser, a "waiting for the service" page that retries, and queued script calls, so a
// call made before the page has loaded still reaches it.

import Cocoa
import WebKit

/// The page background (app.css --bg), so the web view never shows a different colour while loading or resizing.
let PAGE_BG = NSColor(name: "MySearchBackground") { appearance in
    appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
        ? NSColor(srgbRed: 0x10 / 255.0, green: 0x14 / 255.0, blue: 0x1A / 255.0, alpha: 1)
        : NSColor(srgbRed: 0xE7 / 255.0, green: 0xEB / 255.0, blue: 0xEF / 255.0, alpha: 1)
}

/// Shown while the service does not answer. The pane retries after a few seconds.
let waitingPageHTML = """
<html><head><style>
:root { color-scheme: light dark; }
body { font: 15px -apple-system; color: #7A8594; display: grid; place-items: center; height: 90vh; margin: 0;
       background: transparent; -webkit-user-select: none; cursor: default; }
h1 { font: 400 22px 'Iowan Old Style', Georgia, serif; color: #445061; margin: 0 0 8px; }
@media (prefers-color-scheme: dark) { h1 { color: #AAB4C0; } }
code { font: 13px ui-monospace, Menlo, monospace; }
</style></head><body><div style="text-align:center"><h1>Waiting for the search service…</h1>
<p>If this stays, run <code>mse service install</code> in a terminal.</p></div></body></html>
"""

/// JSON for evaluateJavaScript: values go in as data, never as code.
func js(_ value: Any) -> String {
    guard let d = try? JSONSerialization.data(withJSONObject: [value], options: [.fragmentsAllowed]),
          let s = String(data: d, encoding: .utf8) else { return "null" }
    return String(s.dropFirst().dropLast())
}

@MainActor
final class WebPane: NSObject, WKNavigationDelegate, WKUIDelegate {
    let web: WKWebView

    /// Page messages: the handler name ("app", "mini", "layout") and its body.
    var onMessage: ((String, [String: Any]) -> Void)?
    /// Called when a page on the service has finished loading.
    var onLoaded: (() -> Void)?
    /// Shown while the service is not answering; the pane retries `retryURL` after `retryAfter` seconds.
    var waitingHTML: String?
    var retryURL: URL?
    var retryAfter: TimeInterval = 2

    private var loaded = false
    private var queued: [String] = []
    private var retryTimer: Timer?
    /// A 401 from the service (the page's token was refused) reloads the page once with the current token. A second 401
    /// shows a native notice with Retry and sets `authFailed`, so the owner can ask the pane to load again.
    private var authRetried = false
    private(set) var authFailed = false
    /// Set when our own cancel of a 401 response is about to fail the navigation. That failure (WebKit error 102, or
    /// NSURLErrorCancelled) is expected and must not show the waiting page or arm its retry.
    private var awaitingAuthCancel = false
    private var notice: NSView?

    /// - Parameters:
    ///   - messages: the names of the page's message handlers
    ///   - startScript: run before the first paint of every page (for example to add a CSS class)
    ///   - transparent: no background, for the Quick Search panel
    /// Page messages go through `forwarder`, which holds this pane weakly (WebKit keeps its handlers).
    private let forwarder = WeakHandler()

    init(messages: [String], startScript: String? = nil, transparent: Bool = false) {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        config.preferences.setValue(true, forKey: "developerExtrasEnabled")
        for name in messages { config.userContentController.add(forwarder, name: name) }
        if let startScript {
            config.userContentController.addUserScript(
                WKUserScript(source: startScript, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        }
        web = WKWebView(frame: .zero, configuration: config)
        super.init()
        forwarder.pane = self
        web.navigationDelegate = self
        web.uiDelegate = self
        if transparent {
            web.setValue(false, forKey: "drawsBackground")
            web.underPageBackgroundColor = .clear
        } else {
            web.underPageBackgroundColor = PAGE_BG
        }
    }

    /// Loads a page. A call from outside starts over: the 401 retry is available again.
    func load(_ url: URL) {
        loaded = false
        retryURL = url
        authRetried = false
        authFailed = false
        hideNotice()
        web.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 10))
    }

    /// Loads the page again with the token the service has now (the config may have been written or rotated since).
    /// Internal retries use this, so the "retried once" state is kept.
    private func reloadPage() {
        guard let url = retryURL else { return }
        loaded = false
        web.load(URLRequest(url: MSEClient.shared.refreshingToken(url), cachePolicy: .reloadIgnoringLocalCacheData,
                            timeoutInterval: 10))
    }

    /// Runs a script in the page. Before the page has loaded the call waits and runs once it has.
    func run(_ script: String) {
        if loaded { web.evaluateJavaScript(script) } else { queued.append(script) }
    }

    // MARK: navigation

    func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
        loaded = false
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard webView.url?.host == "127.0.0.1" else { return }     // not the "waiting for the service" note
        loaded = true
        authRetried = false
        authFailed = false
        awaitingAuthCancel = false
        hideNotice()
        let pending = queued
        queued = []
        for script in pending { webView.evaluateJavaScript(script) }
        onLoaded?()
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        let code = (error as NSError).code
        if awaitingAuthCancel && (code == 102 || code == NSURLErrorCancelled) {     // WebKit 102: policy change; ours
            awaitingAuthCancel = false
            return
        }
        guard let html = waitingHTML else { return }
        webView.loadHTMLString(html, baseURL: nil)
        retryTimer?.invalidate()
        retryTimer = Timer.scheduledTimer(withTimeInterval: retryAfter, repeats: false) { [weak self] _ in
            MainActor.assumeIsolated { self?.reloadPage() }     // with the token the service has now
        }
    }

    /// Links that leave the app open in the default browser.
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let url = action.request.url, action.navigationType == .linkActivated, url.host != "127.0.0.1" {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    /// A 401 from the service means the page's token was refused: the config was not written yet, or the token was
    /// rotated. The 401 page is not shown. The page is reloaded once with the current token; if that is refused too, a
    /// native notice offers Retry. Only the main frame counts: a 401 for a sub-resource is the page's own business.
    func webView(_ webView: WKWebView, decidePolicyFor response: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        guard response.isForMainFrame, (response.response as? HTTPURLResponse)?.statusCode == 401 else {
            decisionHandler(.allow)
            return
        }
        decisionHandler(.cancel)
        awaitingAuthCancel = true
        retryTimer?.invalidate()
        loaded = false
        if !authRetried {
            authRetried = true
            DispatchQueue.main.async { [weak self] in
                MainActor.assumeIsolated { self?.reloadPage() }     // after the cancel has finished
            }
        } else {
            authFailed = true
            showNotice()
        }
    }

    /// The native notice for a page the service still refuses. Retry loads it again with the current token.
    private func showNotice() {
        guard notice == nil else { return }
        let label = NSTextField(labelWithString: "MySearch could not sign in to the search service.")
        label.font = .systemFont(ofSize: 15)
        label.alignment = .center
        let button = NSButton(title: "Retry", target: self, action: #selector(retryClicked))
        button.bezelStyle = .rounded
        let stack = NSStackView(views: [label, button])
        stack.orientation = .vertical
        stack.alignment = .centerX
        stack.spacing = 12
        stack.translatesAutoresizingMaskIntoConstraints = false
        web.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: web.centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: web.centerYAnchor),
        ])
        notice = stack
    }

    private func hideNotice() {
        notice?.removeFromSuperview()
        notice = nil
    }

    @objc private func retryClicked() {
        hideNotice()
        authRetried = false
        authFailed = false
        reloadPage()
    }

    // MARK: confirm() / prompt() / alert() need native panels in WKWebView.

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = NSAlert(); a.messageText = message; a.runModal(); completionHandler()
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let a = NSAlert()
        let parts = message.components(separatedBy: "\n\n")
        a.messageText = parts.first ?? message
        a.informativeText = parts.dropFirst().joined(separator: "\n\n")
        // the confirming button is named after the question's first word ("Remove …?" → Remove)
        let verb = (parts.first ?? "").split(separator: " ").first.map(String.init) ?? ""
        a.addButton(withTitle: verb.count > 2 && verb.first!.isUppercase ? verb : "OK")
        a.addButton(withTitle: "Cancel")
        completionHandler(a.runModal() == .alertFirstButtonReturn)
    }

    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String, defaultText: String?,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (String?) -> Void) {
        let a = NSAlert()
        a.messageText = prompt
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 280, height: 24))
        field.stringValue = defaultText ?? ""
        a.accessoryView = field
        a.addButton(withTitle: "OK")
        a.addButton(withTitle: "Cancel")
        a.window.initialFirstResponder = field
        completionHandler(a.runModal() == .alertFirstButtonReturn ? field.stringValue : nil)
    }

    /// Page messages arrive here, forwarded by WeakHandler.
    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let d = message.body as? [String: Any] else { return }
        onMessage?(message.name, d)
    }
}

/// Forwards page messages to its pane through a weak reference (WebKit holds handlers strongly).
private final class WeakHandler: NSObject, WKScriptMessageHandler {
    weak var pane: WebPane?

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        MainActor.assumeIsolated { pane?.userContentController(controller, didReceive: message) }
    }
}
