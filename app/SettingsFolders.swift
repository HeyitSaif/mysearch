// The native Settings window, part 3: Folders (cards with a status line, inline actions and every rule) and Groups.

import Cocoa
import SwiftUI

// MARK: - Folders

/// The heading of the Folders pane: what deep indexing means, and the action that adds a folder to it. The action is the
/// same add flow as Add Folder (SWFolderPicker, then the add sheet, POST /api/collections).
struct SWDeepIndexHeader: View {
    @EnvironmentObject var m: SWModel

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 12) {
                Text("Deep indexed").font(.headline)
                Spacer(minLength: 12)
                Button("Deep-index a folder…") { SWFolderPicker.choose(from: m) }
                    .buttonStyle(.borderedProminent).controlSize(.small)
            }
            Text("Deep indexing reads all text, scans, audio and video in a folder, with English↔German matching.")
                .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
    }
}

/// The English-German bridge, read from the settings: on when translation is enabled and the corpus bridge is on, the
/// same two switches the Search pane shows as "Translate between English and German".
enum SWLanguage {
    static func bridgeOn(_ settings: SWJSON) -> Bool {
        let t = settings.swO("translation")
        return (t.swB("enabled") ?? false) && (t.swB("corpus_bridge") ?? false)
    }
}

struct SWFoldersView: View {
    @EnvironmentObject var m: SWModel
    @State private var dropping = false

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    SWDeepIndexHeader()
                    SWActivityPanel()
                    if m.collections.isEmpty {
                        SWEmptyFolders()
                    } else {
                        ForEach(m.folders) { f in SWFolderCard(f: f) }
                    }
                    Text("Tip: drag a folder from Finder onto this window to add it.").font(.caption).foregroundStyle(.secondary)
                }
                .padding(24)
            }
            .task(id: m.highlight) {
                guard let h = m.highlight else { return }
                try? await Task.sleep(nanoseconds: 250_000_000)
                withAnimation { proxy.scrollTo(h, anchor: .center) }
            }
            .task { await IndexProgressModel.shared.poll() }     // live indexing progress, while this list is on screen
        }
        .overlay { if dropping { RoundedRectangle(cornerRadius: 12).strokeBorder(Color.accentColor, style: StrokeStyle(lineWidth: 3, dash: [8])).padding(10).allowsHitTesting(false) } }
        .dropDestination(for: URL.self) { urls, _ in
            guard let u = urls.first(where: { (try? $0.resourceValues(forKeys: [.isDirectoryKey]))?.isDirectory == true }) else {
                m.say("Drop a folder, not a file."); return false
            }
            m.sheet = .addFolder(u); return true
        } isTargeted: { dropping = $0 }
    }
}

struct SWEmptyFolders: View {
    @EnvironmentObject var m: SWModel
    var body: some View {
        SWCard {
            VStack(spacing: 10) {
                Image(systemName: "folder.badge.plus").font(.system(size: 34)).foregroundStyle(.secondary)
                Text("No folders yet").font(.headline)
                Text("Add one to start indexing. Text becomes searchable within minutes; OCR, audio and video run in the night run.")
                    .multilineTextAlignment(.center).foregroundStyle(.secondary).frame(maxWidth: 420)
                Button("Add Folder…") { SWFolderPicker.choose(from: m) }.buttonStyle(.borderedProminent)
            }
            .padding(30).frame(maxWidth: .infinity)
        }
        .id("folders.add")
    }
}

/// What the service is doing right now (the web UI's Activity panel).
struct SWActivityPanel: View {
    @EnvironmentObject var m: SWModel
    var body: some View {
        let cur = m.currentJob
        let queued = m.jobs.swR("queued")
        if cur != nil || !queued.isEmpty {
            SWCard {
                VStack(alignment: .leading, spacing: 8) {
                    if let cur { SWJobLine(job: cur, showStop: true) }
                    ForEach(Array(queued.enumerated()), id: \.offset) { _, j in
                        Text("Next: \(SWJobText.kind(j.swS("kind"))) \(j.swS("collection") ?? "")").font(.callout).foregroundStyle(.secondary)
                    }
                }.padding(14)
            }
        }
    }
}

enum SWJobText {
    static func kind(_ k: String?) -> String {
        ["scan": "Checking for changes", "sync": "Syncing", "embed": "Embedding", "vocab": "Vocabulary", "file": "Re-indexing a file"][k ?? ""] ?? (k ?? "Working on")
    }
    static func stage(_ s: String?) -> String {
        ["extract": "Reading files", "ocr & media": "OCR, page images, audio & video", "ocr & pages": "OCR, page images & photos",
         "audio & video": "Transcribing audio & video", "embedding text": "Embedding text",
         "embedding media": "Embedding images, pages & media", "vocabulary": "Building translation vocabulary"][s ?? ""] ?? (s ?? "starting")
    }
}

struct SWJobLine: View {
    @EnvironmentObject var m: SWModel
    let job: SWJSON
    var showStop = false
    var body: some View {
        let done = job.swD("done") ?? 0, total = job.swD("total") ?? 0
        VStack(alignment: .leading, spacing: 5) {
            HStack {
                ProgressView().controlSize(.small)
                Text("\(SWJobText.kind(job.swS("kind"))) \(job.swS("collection") ?? "") — \(SWJobText.stage(job.swS("stage")))").font(.callout.weight(.medium))
                Spacer()
                if showStop { Button("Stop") { m.cancelJob() }.controlSize(.small) }
            }
            if total > 0 {
                ProgressView(value: min(done, total), total: total)
                Text("\(SW.num(Int(done))) of \(SW.num(Int(total)))\(job.swS("message").map { $0.isEmpty ? "" : " · \($0)" } ?? "")")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}

struct SWFolderCard: View {
    @EnvironmentObject var m: SWModel
    let f: SWFolder
    @ObservedObject private var indexProgress = IndexProgressModel.shared
    private var open: Bool { m.openFolders.contains(f.name) }
    private var err: String { "f.\(f.name)" }

    var body: some View {
        SWCard {
            VStack(spacing: 0) {
                header
                if open { Divider(); SWFolderDetail(f: f).padding(16) }
            }
        }
        .id("folder:" + f.name)
    }

    private var state: (SWTone, String) {
        if !f.exists { return (.bad, "Folder not found") }
        if !f.enabled { return (.neutral, "Paused") }
        guard let s = f.stats else { return (.warn, "Not indexed yet") }
        if m.currentJob?.swS("collection") == f.name { return (.busy, "Indexing…") }
        if s.embedPending > 0 { return (.busy, "\(SW.num(s.embedPending)) to embed") }
        if s.errors > 0 { return (.warn, SW.count(s.errors, "unreadable file")) }
        if s.heavyPending > 0 { return (.neutral, "\(SW.num(s.heavyPending)) for the night run") }
        return (.ok, "Up to date")
    }

    /// The status line, with the language note of a deep-indexed folder: "English + German" while the English-German
    /// bridge is on (read from the settings only; this pane never writes it).
    private var statusLine: String {
        ([baseStatusLine] + (SWLanguage.bridgeOn(m.settings) ? ["English + German"] : [])).joined(separator: " · ")
    }

    private var baseStatusLine: String {
        guard let s = f.stats else { return f.exists ? "Not indexed yet." : "\(SW.tilde(f.path)) is not there." }
        var parts = [SW.count(s.files, "file"), SW.bytes(s.bytes)]
        if s.embedPending > 0 { parts.append("\(SW.num(s.embedPending)) waiting to embed") }
        if s.heavyPending > 0 { parts.append("\(SW.num(s.heavyPending)) waiting for OCR/media") }
        if s.errors > 0 { parts.append("\(s.errors) unreadable") }
        return parts.joined(separator: " · ")
    }

    private var header: some View {
        HStack(spacing: 12) {
            Button { withAnimation(.easeOut(duration: 0.18)) { m.toggleOpen(f.name) } } label: {
                HStack(spacing: 12) {
                    Image(systemName: "chevron.right").rotationEffect(.degrees(open ? 90 : 0)).foregroundStyle(.secondary).frame(width: 12)
                    Image(systemName: f.exists ? "folder.fill" : "folder.badge.questionmark").font(.system(size: 22)).foregroundStyle(f.exists ? Color.blue : Color.red)
                    VStack(alignment: .leading, spacing: 2) {
                        HStack(spacing: 6) {
                            Text(f.name).font(.headline)
                            ForEach(f.aliases, id: \.self) { SWChip(text: $0, mono: true) }
                            if f.askCloud == false { SWPill(text: "local answers", tone: .neutral) }
                        }
                        Text(SW.tilde(f.path)).font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                        Text(statusLine).font(.caption).foregroundStyle(state.0 == .bad || state.0 == .warn ? state.0.color : .secondary).lineLimit(1)
                        IndexProgressRow(entry: indexProgress.folders[f.name])
                    }
                    Spacer(minLength: 8)
                }.contentShape(Rectangle())
            }.buttonStyle(.plain)
            SWPill(text: state.1, tone: state.0)
            chip(f.inAll ? "In All" : "Not in All", on: f.inAll, help: f.inAll ? "Searched by All. Click to leave it out (its own tab, groups and shortcuts still search it)." : "Left out of All. Click to include it.") {
                m.patchFolder(f.name, ["in_global": !f.inAll], err: err)
            }
            chip(f.enabled ? "On" : "Paused", on: f.enabled, help: f.enabled ? "Indexed and searchable. Click to pause." : "Paused: not indexed or searched. Click to turn it back on.") {
                m.patchFolder(f.name, ["enabled": !f.enabled], err: err)
            }
            Button("Sync Now") { m.syncFolder(f.name) }.controlSize(.small).disabled(!f.exists || !f.enabled)
        }
        .padding(.horizontal, 14).padding(.vertical, 12)
    }

    private func chip(_ text: String, on: Bool, help: String, _ action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(text).font(.system(size: 11, weight: .medium)).padding(.horizontal, 9).padding(.vertical, 3)
                .foregroundStyle(on ? Color.accentColor : Color.secondary)
                .background(Capsule().fill(on ? Color.accentColor.opacity(0.14) : Color(nsColor: .quaternaryLabelColor).opacity(0.4)))
        }.buttonStyle(.plain).help(help)
    }
}

struct SWFolderDetail: View {
    @EnvironmentObject var m: SWModel
    let f: SWFolder
    @State private var excludeTest = ""
    @State private var nameOnlyTest = ""
    @State private var showProblems = false
    @State private var testTasks: [String: Task<Void, Never>] = [:]
    private var err: String { "f.\(f.name)" }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            SWInlineError(key: err)
            warnings
            if let s = f.stats { SWStatTiles(s: s) }
            problems
            Divider()
            rules
            Divider()
            agents
            Divider()
            HStack {
                Button(role: .destructive) { SWRemoveFolder.confirm(f, model: m) } label: { Label("Remove Folder…", systemImage: "trash") }
                    .id("folder.remove")
                Text("Only the search index moves aside. The folder and your files are not touched.").font(.caption).foregroundStyle(.secondary)
                Spacer()
            }
        }
        .task {
            await m.loadHealth(f.name)
            await m.loadFolderAgents(f.name)
            await runTest("exclude", f.exclude)
            await runTest("metadata_only", f.nameOnly)
        }
    }

    // every warning carries its fix
    @ViewBuilder private var warnings: some View {
        if !f.exists {
            SWNote(tone: .bad, text: "This folder is not there. If it is on a drive, reconnect the drive; otherwise remove it here.",
                   actions: [SWAction(title: "Remove Folder…") { SWRemoveFolder.confirm(f, model: m) }])
        } else if let s = f.stats {
            if s.errors > 0 {
                SWNote(tone: .warn, text: "\(SW.count(s.errors, "file")) could not be read.",
                       actions: [SWAction(title: "Retry Failed Files") { Task { await m.retryFailed(f.name) } },
                                 SWAction(title: "Show Problems") { showProblems = true }])
            }
            if s.embedPending > 0 && m.currentJob?.swS("collection") != f.name {
                SWNote(tone: .neutral, text: "\(SW.num(s.embedPending)) pieces are waiting to be embedded; they become searchable by meaning at the night run.",
                       actions: [SWAction(title: "Sync Now") { m.syncFolder(f.name) }])
            }
            if s.heavyPending > 0 {
                SWNote(tone: .neutral, text: "\(SW.num(s.heavyPending)) files wait for OCR or media work (night run: \(m.jobs.swO("nightly").swB("enabled") == true ? m.jobs.swO("nightly").swS("at") ?? "" : "off")).",
                       actions: [SWAction(title: "Run It Now") { m.syncFolder(f.name) }])
            }
        } else {
            SWNote(tone: .neutral, text: "Not indexed yet.", actions: [SWAction(title: "Sync Now") { m.syncFolder(f.name) }])
        }
    }

    @ViewBuilder private var problems: some View {
        let h = m.healthCache[f.name]
        if let h, (h.swI("error_count") ?? 0) > 0 || !h.swA("failed_media").isEmpty || (h.swI("name_only_count") ?? 0) > 0 {
            DisclosureGroup(isExpanded: $showProblems) {
                VStack(alignment: .leading, spacing: 8) {
                    problemList("Unreadable (\(h.swI("error_count") ?? 0))", h.swR("errors").map { "\($0.swS("path") ?? "") — \($0.swS("error") ?? "")" })
                    problemList("Media that could not be embedded", h.swA("failed_media"))
                    problemList("Name-only by your rules (\(h.swI("name_only_count") ?? 0))", h.swA("name_only"))
                }.padding(.top, 6)
            } label: {
                Text("Problems and details — \(h.swI("error_count") ?? 0) unreadable · \(h.swI("name_only_count") ?? 0) name-only").font(.callout)
            }
            .id("folder.problems")
        }
    }

    private func problemList(_ title: String, _ items: [String]) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title).font(.callout.weight(.medium))
            if items.isEmpty { Text("none").font(.caption).foregroundStyle(.secondary) }
            ForEach(Array(items.prefix(50).enumerated()), id: \.offset) { _, s in
                Text(s).font(.system(size: 11, design: .monospaced)).foregroundStyle(.secondary).lineLimit(2).textSelection(.enabled)
            }
            if items.count > 50 { Text("… and \(items.count - 50) more").font(.caption).foregroundStyle(.secondary) }
        }
    }

    private var rules: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("How this folder is read").font(.headline)
            SWRow(id: "folder.description", title: "Description", detail: "Shown to agents with every result from this folder.") {
                SWTextRow(initial: f.desc, placeholder: "What is in here") { m.patchFolder(f.name, ["description": $0.trimmingCharacters(in: .whitespaces)], err: err) }
                    .frame(width: 280)
            }
            if f.hasAliases {
                SWRow(id: "folder.aliases", title: "Short names", detail: "Type “w: invoice” in the search box to search only here. Separate several with commas.") {
                    SWTextRow(initial: f.aliases.joined(separator: ", "), placeholder: "e.g. w") {
                        m.patchFolder(f.name, ["aliases": $0.split(whereSeparator: { ", ".contains($0) }).map { $0.lowercased() }], err: err)
                    }.frame(width: 200)
                }
            }
            if let ask = f.askCloud {
                SWRow(id: "folder.cloud", title: "Allow cloud answers", detail: ask ? "Ask may send the top passages from this folder to your cloud model (identifiers masked if you chose that in Ask)." : "Ask answers questions about this folder with the model on this Mac only. Nothing from it leaves the Mac.") {
                    Toggle("", isOn: Binding(get: { ask }, set: { m.patchFolder(f.name, ["ask_cloud": $0], err: err) })).labelsHidden().toggleStyle(.switch)
                }
            }
            if m.indexingKeys["respect_gitignore"] != nil || m.indexingKeys["index_code"] != nil {
                Text("Indexing").font(.subheadline.weight(.semibold)).foregroundStyle(.secondary).padding(.top, 4)
                if m.indexingKeys["respect_gitignore"] != nil {
                    SWRow(id: "folder.gitignore", title: "Skip files listed in .gitignore",
                          detail: "Follows the Indexing setting unless this folder has its own.", errorKey: err) {
                        overridePicker(key: "respect_gitignore", value: f.raw["respect_gitignore"] as? Bool,
                                       inherited: (m.indexingKeys["respect_gitignore"] as? Bool) ?? true)
                    }
                }
                if m.indexingKeys["index_code"] != nil {
                    SWRow(id: "folder.code", title: "Index code files (source code, lockfiles)",
                          detail: "Follows the Indexing setting unless this folder has its own.", errorKey: err) {
                        overridePicker(key: "index_code", value: f.raw["index_code"] as? Bool,
                                       inherited: (m.indexingKeys["index_code"] as? Bool) ?? false)
                    }
                }
            }
            Text("Read from files").font(.subheadline.weight(.semibold)).foregroundStyle(.secondary).padding(.top, 4)
            SWRow(id: "folder.media", title: "Images", detail: "OCR for text in pictures, and search by picture.", cost: "Runs in the night run; the picture model uses memory while it works.") {
                Toggle("", isOn: Binding(get: { f.images }, set: { m.patchFolder(f.name, ["media": ["images": $0]], err: err) })).labelsHidden().toggleStyle(.switch)
            }
            SWRow(id: "folder.audio", title: "Audio", detail: "Speech is transcribed so you can search what was said.", cost: "Transcription is slow: about the length of the recording, in the night run.") {
                Toggle("", isOn: Binding(get: { f.audio }, set: { m.patchFolder(f.name, ["media": ["audio": $0]], err: err) })).labelsHidden().toggleStyle(.switch)
            }
            SWRow(id: "folder.video", title: "Video", detail: "Spoken words and key frames become searchable.", cost: "The slowest option; runs in the night run.") {
                Toggle("", isOn: Binding(get: { f.video }, set: { m.patchFolder(f.name, ["media": ["video": $0]], err: err) })).labelsHidden().toggleStyle(.switch)
            }
            SWRow(id: "folder.pdf", title: "PDF page images", detail: "Scanned pages need OCR to be searchable.", cost: "“All pages” reads every page as a picture: much slower and larger on disk.") {
                Picker("", selection: Binding(get: { f.pdfPages }, set: { m.patchFolder(f.name, ["media": ["pdf_pages": $0]], err: err) })) {
                    Text("Scanned pages + first page").tag("smart"); Text("All pages").tag("all"); Text("Off").tag("off")
                }.labelsHidden().frame(width: 220)
            }
            SWRow(id: "folder.size", title: "Largest file to read", detail: "Bigger files are listed by name only. The service does not report the current value; nothing is sent until you change it here.") {
                if let cur = m.maxFileMB[f.name] {
                    SWNumberField(value: cur, range: 1...20000, step: 50, unit: "MB") { m.maxFileMB[f.name] = $0; m.patchFolder(f.name, ["max_file_mb": $0], err: err) }
                } else {
                    HStack { Text("Default (200 MB), not reported").foregroundStyle(.secondary); Button("Change…") { m.maxFileMB[f.name] = 200 }.controlSize(.small) }
                }
            }
            ruleArea(id: "folder.exclude", title: "Skip these paths", detail: "One pattern per line, e.g. backups/ or *.zip.", initial: f.exclude, key: "exclude", note: excludeTest)
            ruleArea(id: "folder.nameonly", title: "File names only", detail: "Contents are never read, indexed or shown, e.g. Payroll/.", initial: f.nameOnly, key: "metadata_only", note: nameOnlyTest)
            VStack(alignment: .leading, spacing: 4) {
                Text("Context for agents").id("folder.contexts")
                Text("path/prefix/: what this part contains — one per line.").font(.caption).foregroundStyle(.secondary)
                SWTextArea(initial: f.contexts.map { "\($0.0): \($0.1)" }.joined(separator: "\n")) { text in
                    var ctx: [String: String] = [:]
                    for l in text.split(separator: "\n") {
                        guard let i = l.firstIndex(of: ":") else { continue }
                        let k = l[l.startIndex..<i].trimmingCharacters(in: .whitespaces), v = l[l.index(after: i)...].trimmingCharacters(in: .whitespaces)
                        if !k.isEmpty { ctx[k] = v }
                    }
                    m.patchFolder(f.name, ["contexts": ctx], err: err)
                }
            }
            HStack(spacing: 8) {
                Image(systemName: "arrow.triangle.2.circlepath").foregroundStyle(.secondary)
                Text("Rule changes apply at the next sync.").font(.caption).foregroundStyle(.secondary)
                Button("Sync Now") { m.syncFolder(f.name) }.controlSize(.small).disabled(!f.exists || !f.enabled)
            }
        }
    }

    private func ruleArea(id: String, title: String, detail: String, initial: [String], key: String, note: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).id(id)
            Text(detail).font(.caption).foregroundStyle(.secondary)
            SWTextArea(initial: initial.joined(separator: "\n"), onType: { t in
                testTasks[key]?.cancel()
                testTasks[key] = Task { try? await Task.sleep(nanoseconds: 500_000_000); if !Task.isCancelled { await runTest(key, lines(t)) } }
            }) { m.patchFolder(f.name, [key: lines($0)], err: err) }
            if !note.isEmpty { Text(note).font(.caption).foregroundStyle(key == "exclude" && note.contains("would be skipped") ? Color.orange : Color.secondary) }
        }
    }

    /// "Use default (On)" / "On" / "Off" for one per-folder Indexing override. A missing or null value is "use default";
    /// choosing it sends null, which clears the folder's override on the service.
    private func overridePicker(key: String, value: Bool?, inherited: Bool) -> some View {
        let current: String
        if let value { current = value ? "on" : "off" } else { current = "default" }
        return Picker("", selection: Binding(get: { current }, set: { choice in
            let body: Any = choice == "default" ? NSNull() : (choice == "on")
            m.patchFolder(f.name, [key: body], err: err)
        })) {
            Text("Use default (\(inherited ? "On" : "Off"))").tag("default")
            Text("On").tag("on")
            Text("Off").tag("off")
        }
        .labelsHidden().frame(width: 220)
    }

    private func lines(_ t: String) -> [String] { t.split(separator: "\n").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty } }

    /// How many indexed files a rule list matches (the cost of the rule, before it is applied).
    private func runTest(_ key: String, _ patterns: [String]) async {
        let set: (String) -> Void = { text in if key == "exclude" { excludeTest = text } else { nameOnlyTest = text } }
        guard !patterns.isEmpty, f.stats != nil else { set(""); return }
        guard let r = await m.testRule(f.name, patterns) else { return }
        let n = r.swI("matches") ?? 0, of = r.swI("of") ?? 0
        let what = key == "exclude" ? "would be skipped" : "would become name-only"
        let sample = r.swA("sample").prefix(3).joined(separator: ", ")
        set(n == 0 ? "No indexed files match." : "\(SW.num(n)) of \(SW.num(of)) indexed files \(what): \(sample)\(n > 3 ? " …" : "")")
    }

    private var agents: some View {
        let rows = m.folderAgents[f.name] ?? []
        let connected = rows.filter { $0.swS("state") == "connected" }.count
        return VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("AI agents in this folder").font(.headline).id("folder.agents")
                Spacer()
                if m.agentsAvailable == true {
                    Button(connected > 0 ? "Connect the Rest" : "Connect") { Task { await m.setFolderAgents(f.name, install: true) } }
                        .disabled(connected == rows.count && !rows.isEmpty).controlSize(.small)
                    if connected > 0 { Button("Disconnect") { Task { await m.setFolderAgents(f.name, install: false) } }.controlSize(.small) }
                }
            }
            if m.agentsAvailable == false {
                Text("Connecting agents needs a newer search service.").font(.caption).foregroundStyle(.secondary)
            } else if rows.isEmpty {
                Text("Reading the agents' settings files…").font(.caption).foregroundStyle(.secondary)
            } else {
                SWAgentPills(rows: rows)
                ForEach(SWAgents.notes(rows), id: \.self) { Text($0).font(.caption).foregroundStyle(.secondary) }
            }
        }
    }
}

struct SWStatTiles: View {
    let s: SWStats
    var body: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 118), spacing: 8)], alignment: .leading, spacing: 8) {
            tile("Files", SW.num(s.files)); tile("Size", SW.bytes(s.bytes)); tile("Pieces", SW.num(s.items))
            tile("Waiting to embed", SW.num(s.embedPending), s.embedPending > 0 ? .busy : .neutral)
            tile("Waiting for OCR/media", SW.num(s.heavyPending), s.heavyPending > 0 ? .busy : .neutral)
            tile("Name-only", SW.num(s.nameOnly)); tile("Unreadable", SW.num(s.errors), s.errors > 0 ? .warn : .neutral)
            tile("Scanned", SW.ago(s.lastScan)); tile("Night work", SW.ago(s.lastHeavy))
        }
    }
    private func tile(_ t: String, _ v: String, _ tone: SWTone = .neutral) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(t).font(.caption).foregroundStyle(.secondary).lineLimit(1)
            Text(v).font(.system(size: 15, weight: .semibold)).foregroundStyle(tone == .neutral ? Color.primary : tone.color)
        }
        .padding(9).frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color(nsColor: .quaternaryLabelColor).opacity(0.22)))
    }
}

struct SWAgentPills: View {
    let rows: [SWJSON]
    var body: some View {
        SWFlowLayout {
            ForEach(SWAgents.summary(rows)) { s in
                let l = SWAgents.label(s.state)
                HStack(spacing: 5) {
                    Circle().fill(l.1.color).frame(width: 7, height: 7)
                    Text(s.agent).font(.system(size: 12))
                    if s.state != "connected" { Text(l.0).font(.system(size: 11)).foregroundStyle(.secondary) }
                }
                .padding(.horizontal, 9).padding(.vertical, 4)
                .background(Capsule().fill(l.1.color.opacity(0.12)))
                .help(s.detail)
            }
        }
    }
}

enum SWRemoveFolder {
    @MainActor static func confirm(_ f: SWFolder, model m: SWModel) {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "Remove \(f.name) from MySearch?"
        alert.informativeText = "Its search index is moved aside. The folder and your files are not touched."
        if m.agentsAvailable == true {
            alert.showsSuppressionButton = true
            alert.suppressionButton?.title = "Also disconnect AI agents"
            alert.suppressionButton?.state = .on
        }
        alert.addButton(withTitle: "Remove")
        alert.addButton(withTitle: "Cancel")
        alert.buttons.first?.hasDestructiveAction = true
        let finish: (NSApplication.ModalResponse) -> Void = { r in
            guard r == .alertFirstButtonReturn else { return }
            let disconnect = alert.suppressionButton?.state == .on
            Task { @MainActor in await m.removeFolder(f.name, disconnect: disconnect) }
        }
        if let w = m.window { alert.beginSheetModal(for: w, completionHandler: finish) } else { finish(alert.runModal()) }
    }
}

struct SWAddFolderSheet: View {
    @EnvironmentObject var m: SWModel
    let url: URL
    @State private var name = ""
    @State private var connect = true
    @State private var problem: String?
    @State private var saving = false

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Add a Folder").font(.headline)
            Label { Text(SW.tilde(url.path)).font(.system(size: 12, design: .monospaced)).lineLimit(2).truncationMode(.middle) } icon: { Image(systemName: "folder.fill").foregroundStyle(.blue) }
            VStack(alignment: .leading, spacing: 4) {
                Text("Name").font(.subheadline).foregroundStyle(.secondary)
                TextField("Shown as a tab", text: $name).textFieldStyle(.roundedBorder)
            }
            if m.agentsAvailable == true {
                Toggle("Connect AI agents in this folder", isOn: $connect)
                Text("Writes a MySearch entry into this folder's settings files for Claude Code, Cursor, Codex, Grok and OpenCode, so agents working there search it. Nothing else in those files changes, and Disconnect takes the entry out again.")
                    .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            if m.collections.contains(where: { $0.swS("path") == url.path }) {
                SWNote(tone: .warn, text: "This folder is already indexed.")
            }
            if let problem { Text(problem).font(.callout).foregroundStyle(.red) }
            HStack {
                Spacer()
                Button("Cancel") { m.sheet = nil }.keyboardShortcut(.cancelAction)
                Button("Add Folder") {
                    saving = true
                    Task {
                        let e = await m.addFolder(url: url, name: name.trimmingCharacters(in: .whitespaces), connect: connect)
                        saving = false
                        if let e { problem = e } else { m.sheet = nil }
                    }
                }.keyboardShortcut(.defaultAction).disabled(saving)
            }
        }
        .padding(20).frame(width: 460)
        .onAppear { name = url.lastPathComponent }
    }
}

// MARK: - Groups

struct SWGroupsView: View {
    @EnvironmentObject var m: SWModel
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                Text("A group gets its own tab like a folder. Switch Tab off to keep it in the Folders menu only. Groups and folders can have short names: type “w: invoice” or “invoice in:w” to search there from any tab.")
                    .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                if m.groupRows.isEmpty {
                    SWCard {
                        VStack(spacing: 10) {
                            Image(systemName: "square.stack.3d.up").font(.system(size: 32)).foregroundStyle(.secondary)
                            Text("No groups yet").font(.headline)
                            Text("Combine folders you often search together, for example all client work.").foregroundStyle(.secondary)
                            Button("New Group…") { GroupEditorSheet.present(on: m.window) { _ in Task { await m.refreshCollections(force: true) } } }.buttonStyle(.borderedProminent)
                        }.padding(28).frame(maxWidth: .infinity)
                    }.id("groups.new")
                }
                ForEach(m.groupRows, id: \.detail.name) { row in SWGroupCard(detail: row.detail, files: row.files) }
            }.padding(24)
        }
    }
}

struct SWGroupCard: View {
    @EnvironmentObject var m: SWModel
    let detail: GroupDetail
    let files: Int
    var body: some View {
        SWCard {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: "square.stack.3d.up.fill").font(.system(size: 22)).foregroundStyle(.indigo)
                VStack(alignment: .leading, spacing: 6) {
                    HStack(spacing: 6) {
                        Text(detail.label).font(.headline)
                        ForEach(detail.aliases, id: \.self) { SWChip(text: $0, mono: true) }
                    }
                    SWFlowLayout { ForEach(detail.members, id: \.self) { SWChip(text: $0, mono: true) } }
                    Text("\(SW.count(files, "file")) in \(SW.count(detail.members.count, "folder"))").font(.caption).foregroundStyle(.secondary)
                    SWInlineError(key: "g.\(detail.name)")
                }
                Spacer(minLength: 8)
                VStack(alignment: .trailing, spacing: 8) {
                    Toggle("Tab", isOn: Binding(get: { !detail.hidden }, set: { on in
                        Task {
                            do { _ = try await GroupStore.update(detail, hidden: !on); m.setError("g.\(detail.name)", nil); m.flashSaved() }
                            catch { m.setError("g.\(detail.name)", error) }
                        }
                    })).toggleStyle(.switch).help("Show this group as a tab next to All. Off: it stays in the Folders menu and still works by name or short name.")
                    HStack {
                        Button("Edit…") { GroupEditorSheet.present(on: m.window, existing: detail) { _ in Task { await m.refreshCollections(force: true) } } }
                        Button("Delete…", role: .destructive) { confirmDelete() }
                    }.controlSize(.small)
                }
            }.padding(14)
        }
    }

    private func confirmDelete() {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "Delete the group \(detail.label)?"
        alert.informativeText = "Only the group goes. Its folders, their indexes and your files are not touched."
        alert.addButton(withTitle: "Delete Group"); alert.addButton(withTitle: "Cancel")
        alert.buttons.first?.hasDestructiveAction = true
        let name = detail.name
        let finish: (NSApplication.ModalResponse) -> Void = { r in
            guard r == .alertFirstButtonReturn else { return }
            Task { @MainActor in
                do { try await GroupStore.delete(name); m.say("Deleted the group \(detail.label)") } catch { m.say(SW.clean(error)) }
            }
        }
        if let w = m.window { alert.beginSheetModal(for: w, completionHandler: finish) } else { finish(alert.runModal()) }
    }
}
