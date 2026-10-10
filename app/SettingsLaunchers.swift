// Settings › Mac App › "Open in…": the tools MySearch found for the Open in… menus (toolbar, File menu, context menus,
// and the Finder extension's Terminal item), with a Refresh button. Used inside SWMacAppView's form.

import Cocoa
import SwiftUI

struct SWLaunchersSection: View {
    @ObservedObject private var store = LauncherStore.shared

    var body: some View {
        Section {
            ForEach(Launchers.Tool.allCases, id: \.self) { tool in
                SWRow(id: "app.launchers.\(tool.rawValue)", title: tool.title, detail: detail(tool)) {
                    SWPill(text: Launchers.isAvailable(tool) ? "Found" : "Not found",
                           tone: Launchers.isAvailable(tool) ? .ok : .neutral)
                }
            }
            SWRow(id: "app.launchers", title: "Look again",
                  detail: checkedText) {
                HStack(spacing: 8) {
                    if store.refreshing { ProgressView().controlSize(.small) }
                    Button("Refresh") { store.refresh() }.controlSize(.small).disabled(store.refreshing)
                }
            }
        } header: { Text("Open in…") }
    }

    private func detail(_ tool: Launchers.Tool) -> String {
        let e = store.entries[tool]
        switch tool {
        case .claudeCode:
            return e?.cli.map { "Runs in a new Terminal window: \(SW.tilde($0))" } ?? "Needs the claude command (not found in the usual places or your shell's PATH)."
        case .codex:
            if let app = e?.app { return SW.tilde(app) }
            return e?.cli.map { "Runs in a new Terminal window: \(SW.tilde($0))" } ?? "Needs the Codex app or the codex command."
        default:
            return e?.app.map { SW.tilde($0) } ?? "Not installed."
        }
    }

    private var checkedText: String {
        guard let at = store.checkedAt else { return "Checked when MySearch starts, and again after 6 hours." }
        let f = RelativeDateTimeFormatter()
        f.unitsStyle = .full
        return "Last checked \(f.localizedString(for: at, relativeTo: Date())). Install a tool, then press Refresh."
    }
}
