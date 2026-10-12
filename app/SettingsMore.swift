// The native Settings window, part 7: Favourites, AI Agents, Privacy, Mac App and About.

import Cocoa
import ServiceManagement
import SwiftUI

// MARK: - Favourites

struct SWFavouritesView: View {
    @EnvironmentObject var m: SWModel
    @State private var filter = ""

    private struct Group { let query: String; var items: [SWJSON]; var latest: String }

    private var groups: [Group] {
        let needle = filter.lowercased().trimmingCharacters(in: .whitespaces)
        var order: [String] = [], by: [String: Group] = [:]
        for x in m.favourites {
            let q = x.swS("query") ?? ""
            let hay = "\(q) \(x.swS("path") ?? "") \(x.swS("collection") ?? "")".lowercased()
            if !needle.isEmpty && !hay.contains(needle) { continue }
            let key = q.lowercased().trimmingCharacters(in: .whitespaces)
            let when = x.swS("last_used") ?? x.swS("created") ?? ""
            if by[key] == nil { order.append(key); by[key] = Group(query: q, items: [], latest: "") }
            by[key]!.items.append(x)
            if when > by[key]!.latest { by[key]!.latest = when }
        }
        return order.compactMap { by[$0] }.sorted { $0.latest > $1.latest }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                HStack {
                    HStack { Image(systemName: "line.3.horizontal.decrease").foregroundStyle(.secondary); TextField("Filter favourites", text: $filter).textFieldStyle(.plain) }
                        .padding(.horizontal, 10).padding(.vertical, 6)
                        .background(RoundedRectangle(cornerRadius: 8).fill(Color(nsColor: .quaternaryLabelColor).opacity(0.35))).frame(maxWidth: 320)
                    Spacer()
                    if let line = m.finetune?.swS("line") { Text(line).font(.caption).foregroundStyle(.secondary).lineLimit(2).multilineTextAlignment(.trailing) }
                }
                Text("Results you starred as best for a search come first the next time you, or an agent, search for it, and lift similar searches. Results marked not relevant stay out of that search.")
                    .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                if m.favAPI == false {
                    SWNote(tone: .warn, text: "Favourites need a newer version of the search service than the one running now.",
                           actions: [SWAction(title: "Copy Restart Command") { swCopy("mse service restart"); m.say("Copied: mse service restart.") }])
                } else if m.favourites.isEmpty {
                    SWCard {
                        VStack(spacing: 8) {
                            Image(systemName: "star").font(.system(size: 30)).foregroundStyle(.secondary)
                            Text("No favourites yet").font(.headline)
                            Text("When a search finds the file you wanted, click its ☆ (or press ⌘D) to make it the best result for that search.")
                                .multilineTextAlignment(.center).foregroundStyle(.secondary).frame(maxWidth: 420)
                        }.padding(28).frame(maxWidth: .infinity)
                    }
                } else if groups.isEmpty {
                    Text("Nothing matches the filter.").foregroundStyle(.secondary)
                }
                ForEach(groups, id: \.query) { g in
                    SWCard {
                        VStack(alignment: .leading, spacing: 6) {
                            HStack { Text("“\(g.query)”").font(.headline).lineLimit(1); Spacer()
                                Button("Search Again") { openSearch(g.query) }.controlSize(.small) }
                            ForEach(Array(g.items.sorted { ($0.swS("kind") ?? "") < ($1.swS("kind") ?? "") }.enumerated()), id: \.offset) { _, x in row(x) }
                        }.padding(14)
                    }
                }
            }.padding(24).id("fav.list")
        }
    }

    private func row(_ x: SWJSON) -> some View {
        let best = x.swS("kind") == "best"
        let name = (x.swS("path") ?? "").split(separator: "/").last.map(String.init) ?? ""
        let uses = x.swI("uses") ?? 0
        return HStack(spacing: 10) {
            Image(systemName: best ? "star.fill" : "eye.slash.fill").foregroundStyle(best ? Color.yellow : Color.secondary)
            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 6) { Text(name).lineLimit(1); if !best { SWPill(text: "hidden") } }
                Text("\(x.swS("collection") ?? "") · \(x.swS("path") ?? "")").font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
            }
            Spacer()
            Text([uses > 0 ? "used \(uses)×" : nil, x.swS("last_used").map { "last \($0.prefix(10))" }].compactMap { $0 }.joined(separator: " · ")).font(.caption).foregroundStyle(.secondary)
            Button { Task { await m.removeFavourite(x); m.say(best ? "Removed from favourites" : "This file can show up in that search again") } } label: { Image(systemName: "xmark.circle") }
                .buttonStyle(.plain).foregroundStyle(.secondary).help("Remove")
        }
    }

    private func openSearch(_ q: String) {
        var c = URLComponents(); c.scheme = "mysearch"; c.host = "search"; c.queryItems = [URLQueryItem(name: "q", value: q)]
        if let u = c.url { (NSApp.delegate as? AppDelegate)?.openURL(u) }
    }
}

// MARK: - AI agents

struct SWAgentsView: View {
    @EnvironmentObject var m: SWModel

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    Text("AI agents search through MCP with the tools search, get, multi_get, similar, collections and sync. They search the folder they work in unless they ask for all, and what they receive is filtered by your Privacy settings.")
                        .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    if m.agentsAvailable == false {
                        SWNote(tone: .neutral, text: "Connecting agents with one click needs a newer search service. The setup commands below work already.")
                    } else {
                        globalCard
                        foldersCard
                    }
                    snippets
                }.padding(24)
            }
            .task(id: m.highlight) {
                guard let h = m.highlight else { return }
                try? await Task.sleep(nanoseconds: 250_000_000)
                withAnimation { proxy.scrollTo(h, anchor: .center) }
            }
        }
    }

    private var globalCard: some View {
        let rows = m.globalAgents ?? []
        let connected = rows.filter { $0.swS("state") == "connected" }.count
        return SWCard {
            VStack(alignment: .leading, spacing: 8) {
                Text("Your AI apps, everywhere").font(.headline)
                Text("Adds MySearch to the apps' own settings, for every project: Claude Desktop, Cursor and the Grok Bot app, Grok, Codex and Antigravity. An agent then searches the folder it is working in. Only the MySearch entry is written; an app whose folder does not exist is skipped.")
                    .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                if rows.isEmpty { Text(m.globalAgents == nil ? "Reading the apps' settings…" : "None of these apps is installed.").font(.callout).foregroundStyle(.secondary) }
                else { SWAgentPills(rows: rows) }
                HStack {
                    Button(connected > 0 ? "Connect the Rest" : "Connect") { Task { await m.setGlobalAgents(install: true) } }
                        .buttonStyle(.borderedProminent).disabled(rows.isEmpty || connected == rows.count)
                    if connected > 0 { Button("Disconnect") { Task { await m.setGlobalAgents(install: false) } } }
                }.controlSize(.small)
                ForEach(SWAgents.notes(rows), id: \.self) { Text($0).font(.caption).foregroundStyle(.secondary) }
            }.padding(14)
        }.id("agents.global")
    }

    private var foldersCard: some View {
        SWCard {
            VStack(alignment: .leading, spacing: 10) {
                Text("Per folder").font(.headline)
                Text("A folder's own settings files make agents that work inside it search that folder.").font(.callout).foregroundStyle(.secondary)
                if m.folders.isEmpty { Text("No folders yet.").foregroundStyle(.secondary) }
                ForEach(m.folders) { f in
                    let rows = m.folderAgents[f.name] ?? []
                    let connected = rows.filter { $0.swS("state") == "connected" }.count
                    VStack(alignment: .leading, spacing: 5) {
                        HStack {
                            Image(systemName: "folder.fill").foregroundStyle(.blue); Text(f.name).font(.callout.weight(.medium)); Spacer()
                            Button(connected > 0 ? "Connect the Rest" : "Connect") { Task { await m.setFolderAgents(f.name, install: true) } }
                                .disabled(rows.isEmpty || connected == rows.count)
                            if connected > 0 { Button("Disconnect") { Task { await m.setFolderAgents(f.name, install: false) } } }
                        }.controlSize(.small)
                        if !rows.isEmpty { SWAgentPills(rows: rows) }
                    }
                    Divider()
                }
            }.padding(14)
        }.id("agents.folders")
    }

    private var snippets: some View {
        let bin = m.mseBin.isEmpty ? "mse" : m.mseBin
        return SWCard {
            VStack(alignment: .leading, spacing: 10) {
                Text("Setup by hand").font(.headline)
                Text("For apps this window does not connect, or when you prefer to edit the file yourself.").font(.callout).foregroundStyle(.secondary)
                snippet("Claude Code", "claude mcp add --scope user mysearch -- \(bin) mcp")
                snippet("Claude Desktop, Cursor (JSON, inside mcpServers)", "\"mysearch\": { \"command\": \"\(bin)\", \"args\": [\"mcp\"] }")
                snippet("Codex (~/.codex/config.toml)", "[mcp_servers.mysearch]\ncommand = \"\(bin)\"\nargs = [\"mcp\"]")
                snippet("Shell-only agents", "mse search --json \"…\"\nmse get '#docid'")
                Text(m.classicOn
                     ? "The HTTP route uses your access token. It is kept out of this window on purpose; Classic Settings can show it."
                     : "The HTTP route uses your access token. It is kept out of this window on purpose.")
                    .font(.caption).foregroundStyle(.secondary)
            }.padding(14)
        }.id("agents.snippets")
    }

    private func snippet(_ title: String, _ text: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.subheadline.weight(.semibold))
            HStack(alignment: .top) {
                Text(text).font(.system(size: 11, design: .monospaced)).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                Button("Copy") { swCopy(text); m.say("Copied") }.controlSize(.small)
            }
            .padding(8).background(RoundedRectangle(cornerRadius: 6).fill(Color(nsColor: .quaternaryLabelColor).opacity(0.25)))
        }
    }
}

// MARK: - Privacy

struct SWPrivacyView: View {
    @EnvironmentObject var m: SWModel

    var body: some View {
        SWFormPage {
            Section {
                SWRow(id: "privacy.redact", title: "Mask personal identifiers for agents",
                      detail: "Applies to MCP and to mse output piped into another program. This app and your own terminal always show everything.",
                      errorKey: "agents.redact") {
                    Toggle("", isOn: Binding(get: { m.settings.swO("agents").swB("redact") ?? true }, set: { m.patch("agents", ["redact": $0], err: "agents.redact") })).labelsHidden().toggleStyle(.switch)
                }
                if m.settings.swO("agents").swB("redact") == false {
                    SWNote(tone: .warn, text: "Masking is off: agents receive IBANs, passport and tax numbers as they are.",
                           actions: [SWAction(title: "Turn Masking On") { m.patch("agents", ["redact": true], err: "agents.redact") }])
                }
                Text("Masked: IBANs (the country code and last four characters stay readable), passport, ID-card, residence-permit and other national ID numbers, tax IDs, social-security numbers, passport machine-readable lines and dates of birth. Company identifiers such as the commercial register number, company tax number and VAT ID stay readable.")
                    .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                HStack { Text("Masking for cloud answers is set under Ask.").font(.caption).foregroundStyle(.secondary); Button("Go to Ask") { m.setSection(.ask) }.controlSize(.small) }
            } header: { Text("Agents") }
            Section {
                Text("A folder rule can mark paths as file names only: their contents are never read, indexed or shown, and the preview offers only Open and Show in Finder. With masking on, agents see such names with long numbers replaced by [id].")
                    .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                ForEach(m.folders) { f in
                    HStack(alignment: .firstTextBaseline) {
                        Text(f.name).font(.callout.weight(.medium)).frame(width: 130, alignment: .leading)
                        if f.nameOnly.isEmpty { Text("no name-only rules").foregroundStyle(.secondary) }
                        else { SWFlowLayout { ForEach(f.nameOnly, id: \.self) { SWChip(text: $0, mono: true) } } }
                        Spacer(minLength: 4)
                        Button("Edit") { m.openFolders.insert(f.name); m.setSection(.folders); m.highlight = "folder:" + f.name }.controlSize(.small)
                    }
                }
            } header: { Text("File names only") }
            .id("privacy.nameonly")
            Section {
                Text("Credentials and secrets are never read or listed, in any folder, whatever its rules say. Hidden folders are skipped too. This list is fixed.")
                    .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                SWFlowLayout { ForEach(m.settings.swA("secret_patterns"), id: \.self) { SWChip(text: $0, mono: true) } }
            } header: { Text("Always skipped") }
            .id("privacy.secrets")
        }
    }
}

// MARK: - Mac app

struct SWMacAppView: View {
    @EnvironmentObject var m: SWModel
    @State private var login = SMAppService.mainApp.status
    @State private var problem: String?
    @State private var quick = HotKey.load()

    /// Saves a shortcut the Quick Search row recorded (nil: the default) through AppDelegate, which registers it at once.
    /// Returns the reason it is not used, or nil when it is saved.
    private func changeQuickKey(_ key: HotKey?) -> String? {
        guard let app = NSApp.delegate as? AppDelegate else { return "MySearch is not ready. Try again." }
        if let refusal = app.applyQuickKey(key) { return refusal }
        quick = key ?? .quickDefault
        m.flashSaved()
        return nil
    }

    var body: some View {
        SWFormPage {
            Section {
                SWRow(id: "app.login", title: "Open at login", detail: loginNote) {
                    Toggle("", isOn: Binding(get: { login == .enabled || login == .requiresApproval }, set: setLogin)).labelsHidden().toggleStyle(.switch)
                }
                if login == .requiresApproval {
                    SWNote(tone: .warn, text: "macOS wants your approval in System Settings › General › Login Items before MySearch can open at login.",
                           actions: [SWAction(title: "Open Login Items") { SMAppService.openSystemSettingsLoginItems() }])
                }
                if let problem { SWNote(tone: .bad, text: problem) }
            } header: { Text("Startup") }
            Section {
                SWRow(id: "app.quick", title: "Quick Search shortcut", detail: "Opens the small search panel over any app, even full-screen ones. Change…, then press the new keys. The default, ⌥Space, avoids ⌘Space, ⌃Space and ⌃⌥Space.") {
                    ShortcutRecorder(current: quick) { key in changeQuickKey(key) }
                }
                SWRow(id: "app.main", title: "Main window shortcut", detail: "Shows or hides the main window.") {
                    Text(HotKey.main.label).font(.system(size: 14, design: .rounded).weight(.semibold)).padding(.horizontal, 10).padding(.vertical, 3)
                        .background(RoundedRectangle(cornerRadius: 6).fill(Color(nsColor: .quaternaryLabelColor).opacity(0.4)))
                }
            } header: { Text("Shortcuts") }
            SWLaunchersSection()
            SWUpdatesSection()          // SettingsUpdates.swift
            SWSetupSection()
        }
        .onAppear { login = SMAppService.mainApp.status }
    }

    private var loginNote: String {
        switch login {
        case .enabled: return "MySearch starts when you log in, so Quick Search and the menu-bar item are always there."
        case .requiresApproval: return "Waiting for your approval in System Settings."
        case .notFound: return "macOS cannot find the installed app, so this cannot be switched on from a test build."
        default: return "Off. MySearch starts only when you open it."
        }
    }

    private func setLogin(_ on: Bool) {
        do {
            if on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
            problem = nil; m.flashSaved()
        } catch { problem = "macOS refused: \(error.localizedDescription)" }
        login = SMAppService.mainApp.status
    }
}

// MARK: - About

struct SWAboutView: View {
    @EnvironmentObject var m: SWModel

    var body: some View {
        let st = m.status, cfg = m.settings, model = cfg.swO("model")
        let files = m.folders.reduce(0) { $0 + ($1.stats?.files ?? 0) }
        let appVersion = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "—"
        SWFormPage {
            Section {
                HStack(spacing: 14) {
                    Image(nsImage: NSApp.applicationIconImage).resizable().frame(width: 56, height: 56)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("MySearch").font(.title2.weight(.semibold))
                        Text("A private search engine for your own folders, running entirely on this Mac. Words, meaning, English ↔ German, pictures and recordings.")
                            .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            Section {
                fact("App", appVersion)
                fact("Service", st.swS("version") ?? "not answering")
                if st.swS("version") != nil {
                    fact("Running", "\((st.swI("uptime_s") ?? 0) / 60) min · \(Int(st.swD("daemon_rss_mb") ?? 0)) MB · port \(st.swI("port") ?? 0) · pid \(st.swI("pid") ?? 0)")
                }
                fact("Indexed", "\(SW.count(m.folders.count, "folder")) · \(SW.count(files, "file"))")
            } header: { Text("Versions") }.id("about.versions")
            Section {
                modelFact("Search model", model.swS("path"), missing: st.swB("model_present") == false)
                modelFact("Re-ranker", model.swS("reranker"), missing: false)
                modelFact("Speech to text", model.swO("asr").swS("path"), missing: st.swB("asr_present") == false)
                let local = m.localModels?.swR("models").first { $0.swB("current") == true }
                fact("Answer model on this Mac", local.map { "\($0.swS("name") ?? "") · \(SW.bytes(($0.swD("size_mb") ?? 0) * 1_048_576))" } ?? "none found")
                if st.swB("model_present") == false {
                    SWNote(tone: .bad, text: "The search model is missing from disk.", actions: [SWAction(title: "Copy Repair Command") { swCopy("mse doctor"); m.say("Copied: mse doctor.") }])
                }
            } header: { Text("Models on disk") }.id("about.models")
            Section {
                pathFact("Data", st.swS("data_dir") ?? "~/Library/Application Support/MySearch")
                pathFact("Logs", "~/Library/Logs/MySearch")
                pathFact("mse command", m.mseBin.isEmpty ? "unknown" : m.mseBin)
            } header: { Text("Where things are") }.id("about.paths")
            Section {
                LabeledContent("Notices and licence texts") {
                    Button("Licences…") { openLicences() }
                }
                Text(Self.licencesFolderExists
                     ? "THIRD_PARTY_NOTICES.md names the code MySearch is built from and bundles, including the GPL code (FFmpeg and others) in the self-contained build, and says where its source is. The Licenses folder holds the GPL and LGPL texts."
                     : "THIRD_PARTY_NOTICES.md names the code MySearch is built from and the licence of each part.")
                    .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            } header: { Text("Licences") }.id("about.licences")
        }
    }

    /// A build with the bundled runtime has Contents/Resources/Licenses (scripts/bundle_runtime.sh); a plain build does not.
    private static let licencesFolderExists: Bool = {
        guard let res = Bundle.main.resourceURL else { return false }
        var isDir: ObjCBool = false
        return FileManager.default.fileExists(atPath: res.appendingPathComponent("Licenses").path, isDirectory: &isDir) && isDir.boolValue
    }()

    /// Opens THIRD_PARTY_NOTICES.md with the system's default app for it and, when the build has one, shows the Licenses folder
    /// in Finder. Not the app's own Markdown window: its Save would write into the signed app bundle.
    private func openLicences() {
        guard let res = Bundle.main.resourceURL else { m.say("This build has no Resources folder."); return }
        let notices = res.appendingPathComponent("THIRD_PARTY_NOTICES.md")
        let folder = res.appendingPathComponent("Licenses", isDirectory: true)
        let haveNotices = FileManager.default.fileExists(atPath: notices.path)
        if haveNotices { NSWorkspace.shared.open(notices) }
        if Self.licencesFolderExists { NSWorkspace.shared.activateFileViewerSelecting([folder]) }
        else if !haveNotices { m.say("The licence files are not in this build.") }
    }

    private func fact(_ k: String, _ v: String) -> some View {
        LabeledContent(k) { Text(v).foregroundStyle(.secondary).textSelection(.enabled).multilineTextAlignment(.trailing) }
    }

    private func modelFact(_ k: String, _ path: String?, missing: Bool) -> some View {
        let p = path.map(SW.expand)
        let size = p.flatMap { m.modelSizes[$0] }
        let sizeText = size == nil ? (p == nil ? "" : " · measuring…") : (size! < 0 ? " · not on disk" : " · \(SW.bytes(size!))")
        return LabeledContent(k) {
            Text(path.map { ($0 as NSString).lastPathComponent + sizeText } ?? "not set").foregroundStyle(missing ? Color.red : Color.secondary)
                .textSelection(.enabled).multilineTextAlignment(.trailing)
        }
    }

    private func pathFact(_ k: String, _ v: String) -> some View {
        LabeledContent(k) {
            HStack {
                Text(v).font(.system(size: 11, design: .monospaced)).foregroundStyle(.secondary).textSelection(.enabled).lineLimit(2).truncationMode(.middle)
                if v.hasPrefix("/") || v.hasPrefix("~") {
                    Button { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: SW.expand(v))]) } label: { Image(systemName: "magnifyingglass.circle") }
                        .buttonStyle(.plain).help("Reveal in Finder")
                }
                Button { swCopy(v); m.say("Copied") } label: { Image(systemName: "doc.on.doc") }.buttonStyle(.plain).help("Copy")
            }
        }
    }
}
