// Whole-disk search, the native side. The shared settings (WholeDiskStore) are what Quick Search and the main window's
// Mac mode read; the Settings pane (SWWholeDiskView) shows the live status and saves the "wholedisk" object whole. Both read
// and write the contract in API.swift. A service without the "wholedisk" key shows nothing: the pane is hidden and the
// searches add no section.

import Cocoa
import SwiftUI

extension Notification.Name {
    /// The whole-disk settings were read or saved. Quick Search and the main window search again when they are on.
    static let wholeDiskChanged = Notification.Name("MSEWholeDiskChanged")
    /// A folder was deep-indexed from the app. The sidebar reloads its Deep indexed list.
    static let deepIndexChanged = Notification.Name("MSEDeepIndexChanged")
}

/// The whole-disk settings the app holds. nil until the first read, and nil when the service sends no "wholedisk" key or
/// the read fails: then every whole-disk surface stays hidden until a later read succeeds.
@MainActor
final class WholeDiskStore {
    static let shared = WholeDiskStore()
    /// How long a read is trusted before the next query reads again.
    static let trustFor: TimeInterval = 30

    private(set) var settings: WholeDiskSettings?
    private var loadedAt: Date?
    private var task: Task<Void, Never>?

    var enabled: Bool { settings?.enabled == true }

    /// Reads again when nothing is held or the held read is older than `trustFor`. Cheap to call on every query.
    func ensureFresh() {
        if task != nil { return }
        if let at = loadedAt, Date().timeIntervalSince(at) < Self.trustFor { return }
        refresh()
    }

    /// Reads the settings now (GET /api/settings, the "wholedisk" key).
    func refresh() {
        task?.cancel()
        task = Task { [weak self] in
            let value = try? await MSEClient.shared.wholeDiskSettings()
            guard let self, !Task.isCancelled else { return }
            self.task = nil
            self.apply(value)
        }
    }

    /// Holds a value: a read, a save, or the fixed value of a snapshot. The searches are told when it changed.
    func apply(_ value: WholeDiskSettings?) {
        loadedAt = Date()
        guard value != settings else { return }
        settings = value
        NotificationCenter.default.post(name: .wholeDiskChanged, object: nil)
    }
}

extension SWModel {
    /// One status read (GET /api/disk/status). The pane's own task calls it every 2 s while the pane is on screen.
    func refreshDisk() async {
        do {
            diskStatus = try await MSEClient.shared.diskStatus()
            diskError = nil
        } catch APIError.http(404) {
            diskError = "This search service has no whole-disk status yet. Update it to see progress here."
        } catch {
            diskError = SW.clean(error)
        }
    }
}

/// The progress shown for a whole-disk index: the share of the files seen that are no longer waiting. Zero before any file
/// is seen. The ETA is the service's own estimate (eta_s), written in words.
enum WholeDiskFormat {
    static func progress(_ s: DiskStatus) -> Double {
        guard s.filesSeen > 0 else { return 0 }
        return min(max(1 - Double(s.pending) / Double(s.filesSeen), 0), 1)
    }

    static func eta(_ seconds: Double?) -> String? {
        guard let seconds, seconds >= 0 else { return nil }
        if seconds < 60 { return "under a minute left" }
        let minutes = Int(seconds / 60)
        if minutes < 60 { return "about \(minutes) min left" }
        return "about \(minutes / 60) h \(minutes % 60) min left"
    }

    static func state(_ s: DiskStatus) -> (text: String, tone: SWTone) {
        if s.pausedReason != nil { return ("Paused", .neutral) }
        switch s.state {
        case "scanning": return ("Scanning folders", .busy)
        case "describing": return ("Reading files", .busy)
        case "paused": return ("Paused", .neutral)
        default: return (s.enabled ? "Up to date" : "Off", s.enabled ? .ok : .neutral)
        }
    }

    static func pausedText(_ reason: String?) -> String? {
        switch reason {
        case "user_active": return "Paused while you use the Mac. It carries on when you are idle."
        case "on_battery": return "Paused on battery. It carries on when the Mac is plugged in."
        case "disabled": return "Off. Turn the switch on to index."
        default: return nil
        }
    }
}

/// Settings > Whole-disk search: the switch, the live status, the folders to look in, extra excludes, priority, what to read
/// (images and documents) and a rescan. Every change saves the whole "wholedisk" object at once.
struct SWWholeDiskView: View {
    @EnvironmentObject var m: SWModel

    private var w: WholeDiskSettings { WholeDiskSettings(m.settings.swO("wholedisk")) }

    /// Changes one field of the object and saves the whole object (POST /api/settings {"wholedisk": {...}}).
    private func save(_ change: (inout WholeDiskSettings) -> Void, err: String) {
        var next = w
        change(&next)
        m.patch("wholedisk", next.json, err: err)
    }

    var body: some View {
        SWFormPage {
            switchSection
            statusSection
            Group {
                rootsSection
                excludeSection
                prioritySection
                kindsSection
                rescanSection
            }
            .disabled(!w.enabled)
        }
        .task {
            // every 2 s, only while the window is on screen (SWModel.visible); the snapshot keeps the value it was given
            while !Task.isCancelled {
                if m.visible { await m.refreshDisk() }
                try? await Task.sleep(nanoseconds: 2_000_000_000)
            }
        }
    }

    // MARK: switch and note

    private var switchSection: some View {
        Section {
            SWRow(id: "disk.enabled", title: "Search the whole disk",
                  detail: "Finds files by name anywhere in the folders below, and by what is in them: labels in photos and text in documents.",
                  errorKey: "wholedisk.enabled") {
                Toggle("", isOn: Binding(get: { w.enabled }, set: { v in save({ $0.enabled = v }, err: "wholedisk.enabled") }))
                    .labelsHidden().toggleStyle(.switch)
            }
            Text("Deep-indexed folders get full search (all text, scans, audio, German↔English). Whole-disk search covers everything else lightly.")
                .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
    }

    // MARK: live status

    private var statusSection: some View {
        Section {
            let s = m.diskStatus ?? DiskStatus()
            let shown = WholeDiskFormat.state(s)
            LabeledContent("Status") { SWPill(text: shown.text, tone: shown.tone) }
            LabeledContent("Files seen") { Text(SW.num(s.filesSeen)).foregroundStyle(.secondary) }
            LabeledContent("Files described") { Text(SW.num(s.filesDescribed)).foregroundStyle(.secondary) }
            LabeledContent("Images labelled") { Text(SW.num(s.imagesLabelled)).foregroundStyle(.secondary) }
            LabeledContent("PDFs described") { Text(SW.num(s.pdfsDescribed)).foregroundStyle(.secondary) }
            LabeledContent("Waiting") { Text(SW.num(s.pending)).foregroundStyle(.secondary) }
            VStack(alignment: .leading, spacing: 4) {
                ProgressView(value: WholeDiskFormat.progress(s))
                Text([
                    "\(Int((WholeDiskFormat.progress(s) * 100).rounded())) %",
                    WholeDiskFormat.eta(s.etaS),
                ].compactMap { $0 }.joined(separator: " · "))
                    .font(.caption).foregroundStyle(.secondary)
            }
            if let note = WholeDiskFormat.pausedText(s.pausedReason) {
                SWNote(tone: .neutral, text: note)
            }
            if let e = s.lastError, !e.isEmpty {
                SWNote(tone: .bad, text: "The last pass reported: \(e)")
            }
            if let e = m.diskError {
                SWNote(tone: .warn, text: e)
            }
        }
    }

    // MARK: what to look in

    private var rootsSection: some View {
        Section {
            SWRow(id: "disk.roots", title: "Folders to look in",
                  detail: "Your home folder by default. Each folder is searched lightly; add the ones you want found by content.",
                  errorKey: "wholedisk.roots") {
                Button("Add Folder…") { chooseRoot() }.controlSize(.small)
            }
            ForEach(w.roots, id: \.self) { root in
                HStack(spacing: 8) {
                    Image(systemName: "folder").foregroundStyle(.secondary)
                    Text(SW.tilde(root)).lineLimit(1).truncationMode(.middle)
                    Spacer(minLength: 8)
                    Button("Remove") { save({ $0.roots.removeAll { $0 == root } }, err: "wholedisk.roots") }
                        .controlSize(.small)
                        .disabled(w.roots.count <= 1)
                }
            }
        }
    }

    private func chooseRoot() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.prompt = "Add"
        panel.message = "Choose a folder for whole-disk search."
        guard panel.runModal() == .OK, let url = panel.url else { return }
        let path = url.path
        save({ if !$0.roots.contains(path) { $0.roots.append(path) } }, err: "wholedisk.roots")
    }

    private var excludeSection: some View {
        Section {
            VStack(alignment: .leading, spacing: 6) {
                Text("Extra excludes").font(.body)
                Text("One folder or name per line, on top of the built-in rules.").font(.caption).foregroundStyle(.secondary)
                SWTextArea(initial: w.extraExcludes.joined(separator: "\n"), minHeight: 56) { text in
                    let lines = text.split(separator: "\n").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
                    save({ $0.extraExcludes = lines }, err: "wholedisk.extra_excludes")
                }
            }
            if let e = m.errors["wholedisk.extra_excludes"] { SWNote(tone: .bad, text: e) }
        }
    }

    // MARK: how hard, and what to read

    private var prioritySection: some View {
        Section {
            SWRow(id: "disk.priority", title: "Priority", detail: "How hard the work runs while you use the Mac.",
                  errorKey: "wholedisk.priority") {
                Picker("", selection: Binding(get: { w.priority }, set: { p in save({ $0.priority = p }, err: "wholedisk.priority") })) {
                    Text("Low impact (only when idle)").tag("low")
                    Text("Balanced").tag("balanced")
                }
                .labelsHidden().pickerStyle(.menu).fixedSize()
            }
        }
    }

    private var kindsSection: some View {
        Section {
            SWRow(id: "disk.images", title: "Images",
                  detail: "Labels such as zebra, grass or savanna, and text found in pictures.", errorKey: "wholedisk.images") {
                Toggle("", isOn: Binding(get: { w.images }, set: { v in save({ $0.images = v }, err: "wholedisk.images") }))
                    .labelsHidden().toggleStyle(.switch)
            }
            SWRow(id: "disk.documents", title: "Documents",
                  detail: "The text of PDFs and Office files, read as far as the size limit allows.", errorKey: "wholedisk.documents") {
                Toggle("", isOn: Binding(get: { w.documents }, set: { v in save({ $0.documents = v }, err: "wholedisk.documents") }))
                    .labelsHidden().toggleStyle(.switch)
            }
        }
    }

    private var rescanSection: some View {
        Section {
            SWRow(id: "disk.rescan", title: "Rescan", detail: "Looks at every folder above again. Searching carries on meanwhile.",
                  errorKey: "wholedisk.rescan") {
                Button("Rescan") { Task { await rescan() } }
                    .disabled(m.diskStatus?.state == "scanning")
            }
        }
    }

    private func rescan() async {
        do {
            try await MSEClient.shared.diskRescan()
            m.setError("wholedisk.rescan", nil)
            m.say("Rescan started. Searching carries on meanwhile.")
        } catch {
            m.setError("wholedisk.rescan", error)
        }
    }
}
