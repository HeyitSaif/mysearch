// Routes "open this file in the editor" to the Markdown reader or the code editor, adds the editor items to the File
// menu, and guards unsaved edits when the app quits.
//
// Another part of the app posts `Notification.Name("MSEOpenInEditor")` with the file URL as the object when the user
// opens a Markdown or text/code file in the browser. `EditorRouter.install()` is called once at launch (after the menus
// are built) and observes it. Editors open in their own windows; the main browser is never blocked.

import Cocoa

@MainActor
final class EditorRouter: NSObject {
    static let shared = EditorRouter()

    private var codeWindow: CodeEditorWindowController?
    private var markdownWindows: [MarkdownWindowController] = []
    private var installed = false

    /// Observes MSEOpenInEditor and adds the File menu items. Safe to call more than once.
    static func install() {
        let r = shared
        guard !r.installed else { return }
        r.installed = true
        NotificationCenter.default.addObserver(r, selector: #selector(openRequested(_:)),
                                               name: Notification.Name("MSEOpenInEditor"), object: nil)
        r.addFileMenuItems()
    }

    // MARK: opening

    @objc private func openRequested(_ note: Notification) {
        if let url = note.object as? URL { open(url) }
        else if let path = note.object as? String { open(URL(fileURLWithPath: path)) }
    }

    /// `.md` and `.markdown` go to the Markdown reader, other text files to the code editor.
    func open(_ url: URL) {
        guard url.isFileURL else { return }
        var isDir: ObjCBool = false
        guard FileManager.default.fileExists(atPath: url.path, isDirectory: &isDir), !isDir.boolValue else { return }
        if TextFileDetector.isMarkdown(url) { openMarkdown(url) } else { openCode(url) }
    }

    private func openMarkdown(_ url: URL) {
        let std = url.standardizedFileURL
        if let existing = markdownWindows.first(where: { $0.url.standardizedFileURL.path == std.path }) {
            existing.show()
            return
        }
        do {
            let c = try MarkdownWindowController(url: std, offset: markdownWindows.count % 8)
            c.onClosed = { [weak self] closed in self?.markdownWindows.removeAll { $0 === closed } }
            markdownWindows.append(c)
            c.show()
        } catch {
            let a = NSAlert()
            a.messageText = "\"\(url.lastPathComponent)\" cannot be opened."
            a.informativeText = error.localizedDescription
            a.runModal()
        }
    }

    private func openCode(_ url: URL) {
        let c: CodeEditorWindowController
        if let existing = codeWindow { c = existing } else {
            c = CodeEditorWindowController(startup: true)
            c.onClosed = { [weak self] in self?.codeWindow = nil }
            codeWindow = c
        }
        if !c.open(url), c.docs.isEmpty {
            c.close()
            codeWindow = nil
        }
    }

    /// A Markdown window followed a link to another file; nothing to re-key (windows are looked up by their current URL).
    func markdownWindowChangedURL(_ c: MarkdownWindowController) {}

    // MARK: File menu

    private func addFileMenuItems() {
        guard let file = NSApp.mainMenu?.items.first(where: { $0.submenu?.title == "File" })?.submenu else { return }
        file.addItem(.separator())
        let open = NSMenuItem(title: "Open in Editor", action: #selector(openSelectionAction), keyEquivalent: "o")
        open.keyEquivalentModifierMask = [.command, .option]
        open.target = self
        file.addItem(open)
        let new = NSMenuItem(title: "New Text File…", action: #selector(newTextFileAction), keyEquivalent: "")
        new.target = self
        file.addItem(new)
        // ⌘S goes to whichever editor window is key (the first responder chain finds `saveDocument:`).
        let save = NSMenuItem(title: "Save", action: NSSelectorFromString("saveDocument:"), keyEquivalent: "s")
        file.addItem(save)
    }

    /// What is selected in the main browser window (the key one, else the last one). Never opens a window.
    private func browserSelection() -> [URL] {
        guard let app = NSApp.delegate as? AppDelegate else { return [] }
        let c = app.mainControllers.first(where: { $0.window.isKeyWindow }) ?? app.mainControllers.last
        return c?.launcherTargets ?? []
    }

    /// File > Open in Editor (⌘⌥O): the item selected in the main window (or the files chosen in the search results).
    @objc private func openSelectionAction() {
        let targets = browserSelection()
        let files = targets.filter { !$0.hasDirectoryPath }
        if files.isEmpty {
            let alert = NSAlert()
            alert.messageText = "Select a file first."
            alert.informativeText = "Choose a Markdown, text or code file in the browser, then use File > Open in Editor."
            alert.runModal()
            return
        }
        for url in files.prefix(10) {
            if TextFileDetector.isEditableText(url) { open(url) }
            else { NSSound.beep() }
        }
    }

    /// File > New Text File…: asks for a folder and a name; the file is created only when the user confirms.
    @objc private func newTextFileAction() {
        let panel = NSSavePanel()
        panel.title = "New Text File"
        panel.message = "Choose a folder and a name. The file is created when you click Create."
        panel.prompt = "Create"
        panel.nameFieldStringValue = "untitled.txt"
        panel.canCreateDirectories = true
        let start = browserSelection().first?.deletingLastPathComponent()
        panel.directoryURL = start ?? FileManager.default.homeDirectoryForCurrentUser
        guard panel.runModal() == .OK, let url = panel.url else { return }
        // NSSavePanel has already asked about replacing an existing file; never truncate one here.
        if !FileManager.default.fileExists(atPath: url.path) {
            guard FileManager.default.createFile(atPath: url.path, contents: Data()) else {
                let a = NSAlert()
                a.messageText = "\"\(url.lastPathComponent)\" could not be created."
                a.informativeText = "Check that you may write to that folder."
                a.runModal()
                return
            }
        }
        open(url)
    }

    // MARK: quit

    var dirtyNames: [String] {
        (codeWindow?.dirtyNames ?? []) + markdownWindows.flatMap { $0.dirtyNames }
    }

    /// Asks what to do with unsaved editor files before the app quits.
    func confirmQuit() -> NSApplication.TerminateReply {
        let names = dirtyNames
        if names.isEmpty { return .terminateNow }
        let a = NSAlert()
        a.messageText = names.count == 1 ? "Save changes to \"\(names[0])\" before quitting?" : "\(names.count) editor files have unsaved changes."
        a.informativeText = names.count == 1 ? "Your changes will be lost if you do not save them." : names.joined(separator: ", ")
        a.addButton(withTitle: names.count == 1 ? "Save" : "Save All")
        a.addButton(withTitle: "Don't Save")
        a.addButton(withTitle: "Cancel")
        switch a.runModal() {
        case .alertFirstButtonReturn:
            let ok = (codeWindow?.saveAllDirty() ?? true) && markdownWindows.allSatisfy { $0.saveAllDirty() }
            return ok ? .terminateNow : .terminateCancel
        case .alertSecondButtonReturn:
            return .terminateNow
        default:
            return .terminateCancel
        }
    }
}

// Unsaved editor files must not be lost on Quit. This is the app delegate's quit hook, kept with the editors it protects.
extension AppDelegate {
    @objc func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        EditorRouter.shared.confirmQuit()
    }
}
