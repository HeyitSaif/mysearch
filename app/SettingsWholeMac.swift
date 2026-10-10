// The native Settings window, part 6: Whole-Mac search. The file-name index of the whole Mac: switch, roots, excluded
// folders (add and remove), the built-in rules (read-only), Full Disk Access with how to grant it, rescan and the
// crawl's progress. The two lists are replaced as a whole on every change (mac_index.roots / mac_index.exclude);
// the service validates them and its sentence appears next to the list. Nothing on disk is touched.

import Cocoa
import SwiftUI

struct SWWholeMacView: View {
    @EnvironmentObject var m: SWModel

    private var mac: SWJSON { m.settings.swO("mac_index") }
    private var s: SWJSON { m.wholeStatus ?? [:] }

    var body: some View {
        SWFormPage {
            switchSection
            if mac.swB("enabled") != false {
                crawlSection
                accessSection
                rootsSection
                excludeSection
                alwaysSection
            }
        }
    }

    // MARK: switch

    private var switchSection: some View {
        Section {
            SWRow(id: "mac.enabled", title: "Search file names across this Mac",
                  detail: "Finds a file by its name anywhere under the folders below. Contents are never read.",
                  cost: "A name index of the whole Mac is a few hundred MB on disk at most and is kept up to date by file events.",
                  errorKey: "mac_index.enabled") {
                Toggle("", isOn: Binding(get: { mac.swB("enabled") ?? true }, set: { m.patch("mac_index", ["enabled": $0], err: "mac_index.enabled") })).labelsHidden().toggleStyle(.switch)
            }
            if s.swB("helper_present") == false || s.swB("helper_outdated") == true {
                SWNote(tone: .warn, text: s.swB("helper_outdated") == true ? "The file-event helper is out of date, so the crawl is slower and changes are noticed late." : "The file-event helper is missing, so the whole-Mac search cannot crawl.",
                       actions: [SWAction(title: "Copy Repair Command") { swCopy("mse doctor --fix"); m.say("Copied: mse doctor --fix. Run it in Terminal.") }])
            }
        }
    }

    // MARK: crawl

    private var crawlSection: some View {
        Section {
            let state = s.swS("state") ?? "waiting"
            LabeledContent("Status") {
                HStack {
                    SWPill(text: stateText(state), tone: state == "ready" ? .ok : state == "crawling" ? .busy : .neutral)
                    Text("\(SW.num(s.swI("entries") ?? 0)) names").foregroundStyle(.secondary)
                }
            }
            if let p = s["progress"] as? SWJSON {
                VStack(alignment: .leading, spacing: 4) {
                    ProgressView(value: min(max(p.swD("fraction") ?? 0, 0), 1))
                    Text("\(p.swS("phase") ?? "working") · \(SW.num(p.swI("entries") ?? 0)) names so far\(p.swI("elapsed_s").map { " · \($0) s" } ?? "")")
                        .font(.caption).foregroundStyle(.secondary)
                    if let c = p.swS("current") { Text(SW.tilde(c)).font(.system(size: 11, design: .monospaced)).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle) }
                }
            }
            LabeledContent("Last full crawl") { Text(s["last_crawl"] == nil || s["last_crawl"] is NSNull ? "never" : SW.ago(s["last_crawl"])).foregroundStyle(.secondary) }
            LabeledContent("Live updates") { Text(s.swB("watching") == true ? "On · \(s.swI("events") ?? 0) changes seen" : "Off").foregroundStyle(.secondary) }
            SWRow(id: "mac.rescan", title: "Rescan now", detail: "Reads every folder again in the background at low priority. You can keep searching meanwhile.") {
                Button("Rescan Now") { Task { await m.rescanWhole() } }.disabled(s.swB("crawling") == true)
            }
            SWRow(id: "mac.every", title: "Rescan automatically every", detail: "0 turns the automatic rescan off; file events still keep names current.", errorKey: "mac_index.rescan_every_h") {
                SWNumberField(value: Int(mac.swD("rescan_every_h") ?? 24), range: 0...720, step: 6, unit: "hours") {
                    m.patch("mac_index", ["rescan_every_h": $0], err: "mac_index.rescan_every_h", delay: 0.6)
                }
            }
            if let e = s.swS("error"), !e.isEmpty {
                SWNote(tone: .bad, text: "The last crawl reported: \(e)", actions: [SWAction(title: "Rescan Now") { Task { await m.rescanWhole() } }])
            }
            if (s.swI("denied") ?? 0) > 0 {
                SWNote(tone: .warn, text: "macOS refused access to \(s.swI("denied") ?? 0) places during the last crawl. Full Disk Access below lets it read their names.",
                       actions: [SWAction(title: "Open Full Disk Access") { SWWholeMacLinks.openFDA() }])
            }
        } header: { Text("Crawl") }
    }

    private func stateText(_ s: String) -> String {
        ["ready": "Ready", "crawling": "Crawling…", "waiting": "Starting soon", "not crawled": "Not crawled yet", "disabled": "Off"][s] ?? s
    }

    // MARK: Full Disk Access

    private var accessSection: some View {
        let fda = s["full_disk_access"] as? Bool
        let skipped = s.swA("skipped_protected")
        return Section {
            SWRow(id: "mac.fda", title: "Full Disk Access",
                  detail: fda == true ? "Granted. The crawl can read names in Desktop, Documents, Downloads and other protected places."
                    : fda == false ? "Not granted. Without it the crawl keeps out of the folders macOS asks consent for, so names there are missing."
                    : "Not known yet. The service checks again every minute.") {
                SWPill(text: fda == true ? "On" : fda == false ? "Off" : "Unknown", tone: fda == true ? .ok : fda == false ? .warn : .neutral)
            }
            if fda != true {
                VStack(alignment: .leading, spacing: 8) {
                    Text("How to grant it").font(.callout.weight(.semibold))
                    Text("1. Open System Settings › Privacy & Security › Full Disk Access.\n2. Press + and add the program below (or drag it in).\n3. Switch it on, then press Rescan Now.")
                        .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    let real = s.swS("python_real_path") ?? s.swS("python_path") ?? ""
                    if !real.isEmpty { Text(real).font(.system(size: 11, design: .monospaced)).textSelection(.enabled).lineLimit(2) }
                    HStack {
                        Button("Open Full Disk Access") { SWWholeMacLinks.openFDA() }
                        if !real.isEmpty {
                            Button("Reveal Program in Finder") { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: real)]) }
                            Button("Copy Path") { swCopy(real); m.say("Path copied.") }
                        }
                    }.controlSize(.small)
                }
            }
            SWRow(id: "mac.protected", title: "Protected folders", detail: "Auto skips them until Full Disk Access is on. Always skips them even then; Never reads them regardless.", errorKey: "mac_index.skip_protected") {
                Picker("", selection: Binding(get: { mac.swS("skip_protected") ?? "auto" }, set: { m.patch("mac_index", ["skip_protected": $0], err: "mac_index.skip_protected") })) {
                    Text("Auto").tag("auto"); Text("Always skip").tag("always"); Text("Never skip").tag("never")
                }.pickerStyle(.segmented).labelsHidden().frame(width: 240)
            }
            if !skipped.isEmpty && fda != true {
                Text("Skipped for now: \(skipped.map { SW.tilde($0) }.joined(separator: ", "))").font(.caption).foregroundStyle(.secondary)
            }
        } header: { Text("Permission") }
    }

    // MARK: roots

    private var rootsSection: some View {
        let roots = mac.swA("roots")
        return Section {
            ForEach(roots, id: \.self) { r in
                HStack {
                    Image(systemName: "folder").foregroundStyle(.blue)
                    Text(SW.tilde(r)).font(.system(size: 12, design: .monospaced)).lineLimit(1).truncationMode(.middle)
                    Spacer()
                    Button { remove(root: r) } label: { Image(systemName: "minus.circle") }.buttonStyle(.plain).foregroundStyle(.secondary)
                        .help(roots.count == 1 ? "A root is needed. Add another first, or switch the whole-Mac search off." : "Stop looking in this folder")
                        .disabled(roots.count == 1)
                }
            }
            HStack {
                Button { SWFolderPicker.chooseMany(from: m, message: "Choose the folders to look in.", prompt: "Add") { urls in
                    m.patch("mac_index", ["roots": roots + urls.map(\.path)], err: "mac_index.roots") } } label: { Label("Add Folder…", systemImage: "plus") }
                Text("Names are searched only under these folders.").font(.caption).foregroundStyle(.secondary)
            }
            SWInlineError(key: "mac_index.roots")
        } header: { Text("Folders to look in") }
        .id("mac.roots")
    }

    private func remove(root: String) {
        m.patch("mac_index", ["roots": m.settings.swO("mac_index").swA("roots").filter { $0 != root }], err: "mac_index.roots")
    }

    // MARK: exclusions

    private var excludeSection: some View {
        let entries = s.swR("exclude_entries")
        let raw = mac.swA("exclude")
        return Section {
            if entries.isEmpty && raw.isEmpty { Text("Nothing extra is excluded.").foregroundStyle(.secondary) }
            ForEach(Array((entries.isEmpty ? raw.map { ["entry": $0] as SWJSON } : entries).enumerated()), id: \.offset) { _, e in
                let entry = e.swS("entry") ?? "", invalid = e.swS("invalid")
                VStack(alignment: .leading, spacing: 3) {
                    HStack {
                        Image(systemName: invalid != nil ? "exclamationmark.triangle.fill" : (e.swS("kind") == "pattern" ? "textformat" : "folder.badge.minus"))
                            .foregroundStyle(invalid != nil ? .orange : .secondary)
                        Text(entry).font(.system(size: 12, design: .monospaced)).lineLimit(1).truncationMode(.middle)
                        Spacer()
                        Button(invalid != nil ? "Remove Entry" : "Include Again") { m.patch("mac_index", ["exclude": mac.swA("exclude").filter { $0 != entry }], err: "mac_index.exclude") }
                            .controlSize(.small)
                    }
                    if let invalid { Text("The service would refuse this entry today (\(invalid)). Until it is removed, no other change to this list can be saved.").font(.caption).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true) }
                    else if let note = e.swS("note") { Text(note).font(.caption).foregroundStyle(.secondary) }
                }
            }
            HStack {
                Button { SWFolderPicker.chooseMany(from: m, message: "Choose the folders to leave out of the whole-Mac search.", prompt: "Exclude") { urls in
                    m.patch("mac_index", ["exclude": mac.swA("exclude") + urls.map(\.path)], err: "mac_index.exclude") } } label: { Label("Exclude Folder…", systemImage: "plus") }
                Text("An excluded folder and everything under it never shows up in the whole-Mac search.").font(.caption).foregroundStyle(.secondary)
            }
            SWInlineError(key: "mac_index.exclude")
        } header: { Text("Excluded folders") }
        .id("mac.exclude")
    }

    // MARK: built-in rules

    private var alwaysSection: some View {
        Section {
            Text("These are never indexed or listed, in any folder, whatever the settings say. An exclusion can only add to them.").font(.callout).foregroundStyle(.secondary)
            ForEach(Array(s.swR("hard_excluded").enumerated()), id: \.offset) { _, g in
                DisclosureGroup {
                    SWFlowLayout { ForEach(g.swA("items"), id: \.self) { SWChip(text: $0, mono: true) } }.padding(.vertical, 4)
                } label: {
                    HStack { Image(systemName: "lock.fill").foregroundStyle(.secondary); Text(g.swS("label") ?? ""); Text("\(g.swA("items").count)").foregroundStyle(.secondary) }
                }
            }
        } header: { Text("Always excluded") }
        .id("mac.always")
    }
}

enum SWWholeMacLinks {
    /// Opens System Settings at Privacy & Security › Full Disk Access (only when the user presses the button).
    @MainActor static func openFDA() {
        if let u = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles") { NSWorkspace.shared.open(u) }
    }
}
