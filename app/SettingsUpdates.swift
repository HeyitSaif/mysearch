// Settings > Mac App: "Setup Assistant" (reopens the first-run assistant) and "Updates" (the once-a-day release check,
// Updater.swift). Added to SWMacAppView as two sections.

import Cocoa
import SwiftUI

struct SWSetupSection: View {
    @EnvironmentObject var m: SWModel

    var body: some View {
        Section {
            SWRow(id: "app.setup", title: "Setup Assistant",
                  detail: "The first-run guide: background service, folders, models and Full Disk Access. Safe to open again; it changes nothing until you press a button in it.") {
                Button("Open Setup Assistant…") { OnboardingController.shared.show() }
            }
        } header: { Text("Setup") }
    }
}

struct SWUpdatesSection: View {
    @EnvironmentObject var m: SWModel
    @ObservedObject private var checker = UpdateChecker.shared

    var body: some View {
        Section {
            SWRow(id: "app.updates", title: "Check for updates",
                  detail: "Once a day MySearch asks GitHub (api.github.com) which release is the latest and compares it with this version, \(checker.currentVersion). "
                        + "The request carries only the app's name and version: no ID and none of your data. MySearch never downloads or installs an update by itself.") {
                Toggle("", isOn: $checker.enabled).labelsHidden().toggleStyle(.switch)
            }
            if let v = checker.available {
                SWNote(tone: .ok, text: "Update available — \(v)",
                       actions: [SWAction(title: "View Release") { checker.openReleasePage() }], icon: "arrow.down.circle.fill")
            }
            HStack {
                Text(status).font(.callout).foregroundStyle(.secondary)
                Spacer()
                Button(checker.checking ? "Checking…" : "Check Now") { Task { await checker.checkNow() } }
                    .controlSize(.small).disabled(checker.checking || UpdateChecker.disabledByEnvironment)
            }
        } header: { Text("Updates") }
    }

    private var status: String {
        if UpdateChecker.disabledByEnvironment { return "Update checks are switched off in this environment." }
        if let e = checker.lastError { return e }
        if checker.noRelease { return "No release is published yet. Checked \(SW.ago(checker.lastChecked?.timeIntervalSince1970))." }
        if checker.available != nil { return "Checked \(SW.ago(checker.lastChecked?.timeIntervalSince1970))." }
        if let t = checker.lastChecked { return "MySearch \(checker.currentVersion) is the latest release. Checked \(SW.ago(t.timeIntervalSince1970))." }
        return checker.enabled ? "Not checked yet." : "Off. Press Check Now to look once."
    }
}
