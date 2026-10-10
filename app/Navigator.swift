// Back and forward history of folders, like Finder's. Every change posts `Navigator.didChange`
// with the new folder in userInfo["url"], so the toolbar, path bar, title and views can follow it.

import Cocoa

extension Notification.Name {
    static let navigatorDidChange = Notification.Name("MySearchNavigatorDidChange")
}

final class Navigator {
    private(set) var current: URL
    private var history: [URL]
    private var index = 0

    init(start: URL) {
        current = start.standardizedFileURL
        history = [current]
    }

    var canGoBack: Bool { index > 0 }
    var canGoForward: Bool { index < history.count - 1 }
    var canGoEnclosing: Bool { current.path != "/" }

    /// Opens `url` as the new current folder. Forward history is dropped, as in Finder. Going to the folder
    /// already open does nothing.
    func go(to url: URL) {
        let target = url.standardizedFileURL
        guard target != current else { return }
        history = Array(history[...index]) + [target]
        index = history.count - 1
        current = target
        publish()
    }

    func back() {
        guard canGoBack else { return }
        index -= 1
        current = history[index]
        publish()
    }

    func forward() {
        guard canGoForward else { return }
        index += 1
        current = history[index]
        publish()
    }

    func enclosingFolder() {
        go(to: current.deletingLastPathComponent())
    }

    private func publish() {
        NotificationCenter.default.post(name: .navigatorDidChange, object: self, userInfo: ["url": current])
    }
}
