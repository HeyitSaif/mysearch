// The content area of the main window. ContentContainerViewController hosts ONE child view controller at a time
// (show(_:)), and draws the status line and the path bar under it. The list, column, icon and search views
// replace the placeholder by calling show(_:) with their own NSViewController; nothing else in the window changes.

import Cocoa

/// The container: the child view on top, then the status line ("N items") and the path bar (NSPathControl).
@MainActor
final class ContentContainerViewController: NSViewController {
    private let host = NSView()
    private let statusLine = NSTextField(labelWithString: "")
    private let pathBar = NSPathControl()
    private(set) var child: NSViewController?
    /// The host's bottom edge: above the footer (status line and path bar) while either is showing, else the view's bottom.
    /// A container with no footer (a pane inside a split, which has its own) gives the whole height to its child.
    private var footerBottom: NSLayoutConstraint?
    private var noFooterBottom: NSLayoutConstraint?
    /// Off (the default): the footer keeps its place even when empty, as before. On, the footer collapses when both the
    /// path bar and the status line are hidden. Only the folder display turns it on (its panes have their own footers).
    var collapsesEmptyFooter = false { didSet { updateFooter() } }
    /// Off hides the status line but keeps its text. A split window shows the status of its focused pane only.
    var showsStatus = true { didSet { refreshStatusVisibility(); updateFooter() } }

    /// A click on a component of the path bar, with that component's folder.
    var onPathClick: ((URL) -> Void)?

    override func loadView() {
        view = NSView()
        host.translatesAutoresizingMaskIntoConstraints = false
        statusLine.font = .systemFont(ofSize: 11)
        statusLine.textColor = MSEStyle.secondary        // the footer text: 4.5:1 or better in light and dark (MSEStyle, SearchView.swift)
        statusLine.alignment = .center
        statusLine.translatesAutoresizingMaskIntoConstraints = false
        pathBar.pathStyle = .standard
        pathBar.isEditable = false
        pathBar.target = self
        pathBar.action = #selector(pathClicked(_:))
        pathBar.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(host)
        view.addSubview(statusLine)
        view.addSubview(pathBar)
        let above = host.bottomAnchor.constraint(equalTo: statusLine.topAnchor, constant: -2)
        let bottom = host.bottomAnchor.constraint(equalTo: view.bottomAnchor)
        footerBottom = above
        noFooterBottom = bottom
        NSLayoutConstraint.activate([
            host.topAnchor.constraint(equalTo: view.topAnchor),
            host.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            host.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            statusLine.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 12),
            statusLine.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -12),
            statusLine.bottomAnchor.constraint(equalTo: pathBar.topAnchor, constant: -2),
            pathBar.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 12),
            pathBar.trailingAnchor.constraint(lessThanOrEqualTo: view.trailingAnchor, constant: -12),
            pathBar.bottomAnchor.constraint(equalTo: view.bottomAnchor, constant: -8),
        ])
        updateFooter()
    }

    /// Picks the host's bottom edge: above the footer while the status line or the path bar shows, else the view's bottom.
    private func updateFooter() {
        let footerShown = !collapsesEmptyFooter || !(pathBar.isHidden && statusLine.isHidden)
        footerBottom?.isActive = footerShown
        noFooterBottom?.isActive = !footerShown
    }

    /// The status line shows when it has text, or when the path bar is up (and `showsStatus` allows it).
    private func refreshStatusVisibility() {
        statusLine.isHidden = !showsStatus || (statusLine.stringValue.isEmpty && pathBar.isHidden)
    }

    /// Puts `vc` in the content area in place of the current child.
    func show(_ vc: NSViewController) {
        if child === vc { return }
        child?.view.removeFromSuperview()
        child?.removeFromParent()
        addChild(vc)
        child = vc
        let v = vc.view
        v.translatesAutoresizingMaskIntoConstraints = false
        host.addSubview(v)
        NSLayoutConstraint.activate([
            v.topAnchor.constraint(equalTo: host.topAnchor),
            v.leadingAnchor.constraint(equalTo: host.leadingAnchor),
            v.trailingAnchor.constraint(equalTo: host.trailingAnchor),
            v.bottomAnchor.constraint(equalTo: host.bottomAnchor),
        ])
    }

    func setStatus(_ text: String) {
        statusLine.stringValue = text
        refreshStatusVisibility()                                // a status text is always shown when there is one
        updateFooter()
    }

    /// The path bar shows `folder`'s path; nil hides the bar (for pages that are not folders).
    func setPath(_ folder: URL?) {
        pathBar.isHidden = folder == nil
        refreshStatusVisibility()
        pathBar.url = folder
        updateFooter()
    }

    @objc private func pathClicked(_ sender: NSPathControl) {
        if let url = sender.clickedPathItem?.url { onPathClick?(url) }
    }
}

/// The placeholder that shows until the list, column, icon or search views arrive: a heading and a line of detail.
@MainActor
final class PlaceholderViewController: NSViewController {
    private let heading = NSTextField(labelWithString: "")
    private let detail = NSTextField(wrappingLabelWithString: "")

    override func loadView() {
        view = NSView()
        heading.font = .systemFont(ofSize: 22, weight: .semibold)
        heading.alignment = .center
        detail.font = .systemFont(ofSize: 13)
        detail.textColor = .secondaryLabelColor
        detail.alignment = .center
        let stack = NSStackView(views: [heading, detail])
        stack.orientation = .vertical
        stack.alignment = .centerX
        stack.spacing = 8
        stack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: view.centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: view.centerYAnchor),
            stack.widthAnchor.constraint(lessThanOrEqualTo: view.widthAnchor, constant: -48),
        ])
    }

    func update(heading h: String, detail d: String) {
        heading.stringValue = h
        detail.stringValue = d
    }
}

/// Hosts an existing view (the web pane's WKWebView) as a child controller.
@MainActor
final class ViewHostViewController: NSViewController {
    private let hosted: NSView

    init(_ hosted: NSView) {
        self.hosted = hosted
        super.init(nibName: nil, bundle: nil)
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override func loadView() { view = hosted }
}
