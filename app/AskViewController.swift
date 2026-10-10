// Ask, native: the view controller that MainWindowController shows in the content area in Ask mode. It hosts the
// SwiftUI AskRootView (AskView.swift) over an AskModel (AskModel.swift). ⌘N (a new chat) and Esc (stop, or close the
// open menu) are handled here, because the menu bar is not this file's to change. The snapshot hooks at the bottom
// bring the view into a known state for the headless checks (see Snapshot.swift).

import Cocoa
import SwiftUI

/// The hosting view: ⌘N is a new chat while Ask is showing (a view only sees key equivalents while it is in the window).
final class AskHostingView: NSHostingView<AskRootView> {
    var onNewChat: (() -> Void)?

    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        if event.modifierFlags.intersection(.deviceIndependentFlagsMask) == .command, event.charactersIgnoringModifiers == "n" {
            onNewChat?()
            return true
        }
        return super.performKeyEquivalent(with: event)
    }
}

@MainActor
final class AskViewController: NSViewController {
    let model: AskModel

    init(snapshot: Bool) {
        model = AskModel(snapshot: snapshot)
        super.init(nibName: nil, bundle: nil)
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override func loadView() {
        let v = AskHostingView(rootView: AskRootView(m: model))
        v.onNewChat = { [weak self] in self?.model.newChat() }
        view = v
    }

    /// True while Ask is still loading something (snapshot mode waits on this).
    var isBusy: Bool { model.isBusy }

    /// Ask mode was entered: load the status, providers, local models and folders, and keep the status fresh.
    func activate() {
        _ = view
        model.refresh()
        model.startPolling()
    }

    override func viewDidDisappear() {
        super.viewDidDisappear()
        model.stopPolling()
    }

    func reload() { model.refresh() }

    /// A question that arrives from Quick Search (⌥↵): a new chat that asks it.
    func ask(question: String) {
        model.newChat()
        if model.ready, !question.isEmpty { model.send(question) } else { model.draft = question }
    }

    /// Esc anywhere in the Ask view: close the open menu, else stop the answer.
    override func cancelOperation(_ sender: Any?) {
        if model.overlay != .none { model.overlay = .none }
        else if model.mentionOpen { model.closeMention() }
        else if model.streaming { model.stop() }
        else { super.cancelOperation(sender) }
    }

    // MARK: snapshot hooks

    /// Brings the view into the state `name` (ask-empty, ask-streaming, ask-done, ask-picker, ask-off, ask-model,
    /// ask-source) and holds `isBusy` until it is reached. Fake chats are added in memory only.
    func snapshotState(_ name: String) {
        _ = view
        model.snapshotWaiting = true
        AskSnapshotSeed.seed(model, withCurrent: false)
        Task { @MainActor in
            // the status and folders first, so the footer and the picker have what they show
            async let a: Void = model.refreshStatus()
            async let b: Void = model.refreshProviders()
            async let c: Void = model.refreshLocal()
            async let d: Void = model.refreshLibrary()
            _ = await (a, b, c, d)
            let question = "What are the payment terms in the Acme contract?"
            switch name {
            case "ask-streaming":
                model.send(question)
                for _ in 0..<200 where model.tokenCount < 14 { try? await Task.sleep(nanoseconds: 50_000_000) }
            case "ask-done", "ask-source":
                model.send(question)
                try? await Task.sleep(nanoseconds: 300_000_000)
                for _ in 0..<400 where model.streaming { try? await Task.sleep(nanoseconds: 50_000_000) }
                if name == "ask-source", let t = model.currentChat?.turns.last, let s = t.sources.first {
                    model.openSource = (t.id, s)
                }
            case "ask-picker":
                model.openPicker()
                model.pickerGo("work")
                for _ in 0..<100 where model.pickerRows.isEmpty { try? await Task.sleep(nanoseconds: 50_000_000) }
                model.pickerGo("work", "Clients")
                for _ in 0..<100 where model.pickerRows.isEmpty || model.pickerPath != "Clients" { try? await Task.sleep(nanoseconds: 50_000_000) }
            case "ask-model":
                model.overlay = .model
            default:
                break                                   // ask-empty, ask-off: the status is all that differs
            }
            try? await Task.sleep(nanoseconds: 300_000_000)
            model.snapshotWaiting = false
        }
    }
}

/// Fake chats for the recent list of a snapshot. In memory only: a test run never writes ask-chats.json.
enum AskSnapshotSeed {
    @MainActor
    static func seed(_ m: AskModel, withCurrent: Bool) {
        let now = Date().timeIntervalSince1970
        func chat(_ id: String, _ title: String, _ turns: Int, ago: Double, local: Bool = false) -> AskChat {
            var c = AskChat(id: id, title: title, created: now - ago, updated: now - ago, turns: [], route: local ? "local" : nil)
            for i in 0..<turns { var t = AskTurn(q: "\(title) (\(i + 1))"); t.a = "Seeded."; c.turns.append(t) }
            return c
        }
        m.chats = [
            chat("seed1", "When does the Acme offer expire?", 3, ago: 1_800),
            chat("seed2", "Summarise the meeting notes from 1 October", 1, ago: 20_000, local: true),
            chat("seed3", "Which invoices are still open for Acme?", 2, ago: 90_000),
            chat("seed4", "What did we agree about the release date?", 4, ago: 260_000),
        ]
        m.currentID = nil
    }
}
