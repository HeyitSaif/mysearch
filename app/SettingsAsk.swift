// The native Settings window, part 5: Ask. On/off, where answers are written (Cloud or Local, with the local fallback),
// masking, the grouped model menu, the model on this Mac, and the cloud providers with their keys. Keys go to the
// service once (POST /api/ask/providers), which puts them in the macOS Keychain; this app never shows one back and
// clears the field the moment it is sent.

import Cocoa
import SwiftUI

@MainActor struct SWAskCfg {
    var route = "cloud", provider = "", model = "", fallback = false
    var online: Bool?
    var whereNext = "", nextModel = "", nextProblem: String?
    var enabled = false, available = false, loaded = false, localModel = "", ramMB: Double?
    var redact = true

    init(_ m: SWModel) {
        let a = m.askStatus
        route = a.swS("route") ?? m.providers?.swS("route") ?? "cloud"
        provider = a.swS("provider") ?? m.providers?.swS("provider") ?? ""
        model = a.swS("model") ?? m.providers?.swS("model") ?? ""
        fallback = a.swB("local_fallback") ?? m.providers?.swB("local_fallback") ?? false
        if route == "auto" { route = "cloud"; fallback = true }          // the old name of cloud + fallback
        online = a["online"] as? Bool
        whereNext = a.swS("where_next") ?? ""; nextModel = a.swS("next_model") ?? ""; nextProblem = a.swS("next_problem")
        enabled = a.swB("enabled") ?? false; available = a.swB("available") ?? false; loaded = a.swB("loaded") ?? false
        localModel = a.swS("local_model") ?? ""; ramMB = a.swD("ram_mb")
        redact = a.swB("cloud_redact") ?? m.settings.swO("ask").swB("cloud_redact") ?? true
    }
}

struct SWAskView: View {
    @EnvironmentObject var m: SWModel

    var body: some View {
        let c = SWAskCfg(m)
        SWFormPage {
            answersSection(c)
            if m.providersAPI == true {
                routeSection(c)
                modelSection(c)
            }
            localSection(c)
            if m.providersAPI == true {
                providersSection(c)
                keptLocalSection
            }
            if m.providersAPI == false {
                Section { Text("This version of the search service has no cloud models; Ask uses the model on this Mac.").foregroundStyle(.secondary) }
            }
        }
    }

    // MARK: answers on/off

    private func answersSection(_ c: SWAskCfg) -> some View {
        Section {
            SWRow(id: "ask.enabled", title: "Answer questions",
                  detail: !c.available ? "The answer model is not installed, so Ask cannot be turned on. Search works as before."
                    : !c.enabled ? "Off. No model is loaded and nothing is sent anywhere."
                    : c.loaded ? "On. The model on this Mac is loaded now\(c.ramMB.map { ", using \(Int($0)) MB" } ?? ""); it unloads after 5 idle minutes."
                    : c.route == "local" ? "On. The model on this Mac loads when you ask (a few seconds the first time)."
                    : "On. The cloud model chosen below writes the answers; the model on this Mac stays unloaded unless needed.",
                  cost: c.enabled && c.route == "local" ? "The local model uses about 2.5 GB of memory while loaded." : nil, errorKey: "ask.enabled") {
                Toggle("", isOn: Binding(get: { c.enabled }, set: { m.patch("ask", ["enabled": $0], err: "ask.enabled") })).labelsHidden().toggleStyle(.switch).disabled(!c.available)
            }
            if !c.available {
                SWNote(tone: .warn, text: "No answer model was found on this Mac. Cloud answers can still be used once a provider is connected below.",
                       actions: [SWAction(title: "Set Up a Cloud Provider") { scrollToProviders() }])
            }
        } header: { Text("Answers") }
    }

    private func scrollToProviders() { m.highlight = "ask.providers" }

    // MARK: route

    private func routeSection(_ c: SWAskCfg) -> some View {
        Section {
            SWRow(id: "ask.route", title: "Where answers are written",
                  detail: c.route == "local" ? "Always the model on this Mac. Nothing is sent anywhere."
                    : "The cloud model chosen below. The model on this Mac is not loaded unless you choose Local, a folder keeps its answers here, or you allow the fallback.",
                  errorKey: "ask.route") {
                HStack(spacing: 10) {
                    if let o = c.online { SWPill(text: o ? "Online" : "Offline", tone: o ? .ok : .warn) }
                    Picker("", selection: Binding(get: { c.route }, set: { m.patch("ask", ["route": $0], err: "ask.route") })) {
                        Text("Cloud").tag("cloud"); Text("Local").tag("local")
                    }.pickerStyle(.segmented).labelsHidden().frame(width: 170)
                }
            }
            if c.online == false && c.route == "cloud" {
                SWNote(tone: .warn, text: "This Mac is offline, so the cloud model cannot answer right now.",
                       actions: c.fallback ? [] : [SWAction(title: "Allow the Local Fallback") { m.patch("ask", ["local_fallback": true], err: "ask.local_fallback") },
                                                   SWAction(title: "Switch to Local") { m.patch("ask", ["route": "local"], err: "ask.route") }])
            }
            SWRow(id: "ask.fallback", title: "Use the local model when no cloud model is reachable",
                  detail: "Off by default: then the local model is never loaded unless you choose Local, and a cloud failure gives you a message instead.",
                  cost: "On: it answers when the cloud cannot, using about 2.5 GB of memory while loaded.", errorKey: "ask.local_fallback") {
                Toggle("", isOn: Binding(get: { c.fallback }, set: { m.patch("ask", ["local_fallback": $0], err: "ask.local_fallback") })).labelsHidden().toggleStyle(.switch).disabled(c.route == "local")
            }
            SWRow(id: "ask.redact", title: "Mask personal identifiers before sending to cloud models",
                  detail: "A cloud answer sends your question, the top passages from your documents and the last turns of the chat. Masking replaces IBANs, passport, ID and tax numbers and dates of birth before they leave. The identifiers are listed under Privacy.",
                  errorKey: "ask.cloud_redact") {
                Toggle("", isOn: Binding(get: { c.redact }, set: { m.patch("ask", ["cloud_redact": $0], err: "ask.cloud_redact") })).labelsHidden().toggleStyle(.switch)
            }
            if !c.redact && c.route == "cloud" {
                SWNote(tone: .warn, text: "Masking is off: identifiers in the passages go to the cloud model as they are.",
                       actions: [SWAction(title: "Turn Masking On") { m.patch("ask", ["cloud_redact": true], err: "ask.cloud_redact") }])
            }
            if c.route == "local" || c.whereNext == "local" { EmptyView() }
            Text(nextAnswer(c)).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        } header: { Text("Where answers are written") }
    }

    private func nextAnswer(_ c: SWAskCfg) -> String {
        if c.route == "local" { return "Next answer: \(c.localModel.isEmpty ? "the model on this Mac" : c.localModel), on this Mac." }
        if c.whereNext == "cloud" {
            if let p = c.nextProblem { return "Next answer: the cloud is not ready (\(problem(p)))." }
            return "Next answer: \(c.nextModel.isEmpty ? c.model : c.nextModel) in the cloud. It sees your question and the top passages\(c.redact ? ", identifiers masked" : "")."
        }
        return "Next answer: on this Mac (\(c.online == false ? "offline" : "no cloud model is ready"))."
    }

    private func problem(_ k: String) -> String {
        ["no_key": "the chosen provider has no key", "offline": "the provider is not reachable", "unknown_provider": "the chosen provider is not set up",
         "no_model": "no model is chosen"][k] ?? k.replacingOccurrences(of: "_", with: " ")
    }

    // MARK: model menu

    private func modelSection(_ c: SWAskCfg) -> some View {
        Section {
            SWRow(id: "ask.model", title: "Cloud model", detail: "Pick the model that writes the answers. The menu lists the providers you have connected, grouped by provider; the model on this Mac is last.", errorKey: "ask.model") {
                SWModelMenu(c: c)
            }
            if c.route == "local" {
                SWNote(tone: .neutral, text: "Answers are set to Local, so this choice is used only when you switch to Cloud.",
                       actions: [SWAction(title: "Use Cloud") { m.patch("ask", ["route": "cloud"], err: "ask.route") }])
            }
            if m.trainsOnInputs(provider: c.provider, model: c.model) {
                SWNote(tone: .warn, text: "This model's provider may use what you send for training. Ask sends your question and the top passages from your documents. Folders with cloud answers off are never sent.",
                       actions: [SWAction(title: "Use a Different Model") { m.highlight = "ask.model" }])
            }
        } header: { Text("Model") }
    }

    // MARK: the model on this Mac

    private func localSection(_ c: SWAskCfg) -> some View {
        let models = m.localModels?.swR("models") ?? []
        return Section {
            SWRow(id: "ask.local", title: "Model on this Mac",
                  detail: "Answers offline, on Local, and for folders that do not allow cloud answers. The next local answer loads the model you pick.",
                  cost: localCost(models, c), errorKey: "ask.local_model") {
                if models.isEmpty {
                    Text(c.localModel.isEmpty ? "None installed" : c.localModel).foregroundStyle(.secondary)
                } else {
                    Picker("", selection: Binding(get: { m.localModels?.swS("current") ?? c.localModel }, set: { name in
                        m.patch("ask", ["local_model": name], err: "ask.local_model")
                        Task { try? await Task.sleep(nanoseconds: 1_200_000_000); await m.loadLocalModels() }
                    })) {
                        ForEach(models.indices, id: \.self) { i in
                            let n = models[i].swS("name") ?? ""
                            Text("\(n) · \(SW.bytes((models[i].swD("size_mb") ?? 0) * 1_048_576))").tag(n)
                        }
                    }.labelsHidden().frame(width: 280)
                }
            }
        } header: { m.providersAPI == true ? Text("On this Mac") : Text("Answer model") }
    }

    private func localCost(_ models: [SWJSON], _ c: SWAskCfg) -> String {
        let cur = models.first { $0.swB("current") == true } ?? models.first
        guard let mb = cur?.swD("size_mb") else { return "The local model uses about 2.5 GB of memory while it is loaded." }
        return "Uses about \(SW.localRAM(sizeMB: mb)) of memory while loaded; it unloads after 5 idle minutes."
    }

    // MARK: providers

    private func providersSection(_ c: SWAskCfg) -> some View {
        Section {
            Text("Paste an API key into a card and press Save Key, or give a provider the file that holds its key. Keys are kept in the macOS Keychain and never shown again.")
                .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            if let e = m.errors["providers"] {
                SWNote(tone: .warn, text: e, actions: [SWAction(title: "Try Again") { Task { await m.loadProviders() } }])
            }
            ForEach(Array((m.providers?.swR("providers") ?? []).enumerated()), id: \.offset) { _, p in SWProviderCard(p: p, c: c) }
            HStack {
                Button { m.sheet = .addProvider } label: { Label("Add Provider…", systemImage: "plus") }
                Text("Any OpenAI-compatible service: DeepSeek, OpenRouter, or a server on this Mac such as LM Studio or Ollama.").font(.caption).foregroundStyle(.secondary)
            }
        } header: { Text("Cloud providers") }
        .id("ask.providers")
    }

    private var keptLocalSection: some View {
        let kept = m.folders.filter { $0.askCloud == false }
        return Section {
            if kept.isEmpty {
                Text("Every folder may be used for cloud answers.").foregroundStyle(.secondary)
            } else {
                ForEach(kept) { f in
                    HStack { Image(systemName: "lock.fill").foregroundStyle(.secondary); Text(f.name); Spacer()
                        Button("Allow Cloud Answers") { m.patchFolder(f.name, ["ask_cloud": true], err: "f.\(f.name)") }.controlSize(.small) }
                }
            }
        } header: { Text("Folders kept on this Mac") } footer: {
            Text("A folder with “Allow cloud answers” off is always answered by the local model. Change it per folder under Folders.").font(.caption)
        }
    }
}

// MARK: - the model menu

struct SWModelMenu: View {
    @EnvironmentObject var m: SWModel
    let c: SWAskCfg

    private var providers: [SWJSON] {
        (m.providers?.swR("providers") ?? []).filter { ($0.swB("has_key") ?? false) || $0.swS("status") == "ok" }
    }

    var body: some View {
        Menu {
            if providers.isEmpty { Text("No provider is connected yet") }
            ForEach(Array(providers.enumerated()), id: \.offset) { _, p in
                let pid = p.swS("id") ?? "", ids = p.swA("models")
                if ids.isEmpty {
                    Text("\(p.swS("label") ?? pid): no models listed yet")
                } else {
                    Menu(p.swS("label") ?? pid) { modelItems(pid, ids) }
                }
            }
            Divider()
            Button { m.patch("ask", ["route": "local"], err: "ask.route") } label: {
                Text("On this Mac: \(c.localModel.isEmpty ? "local model" : c.localModel)")
            }
            Button("Use a model id…") { customId() }
        } label: {
            Text(c.model.isEmpty ? "Choose a model" : c.model).lineLimit(1)
        }
        .menuStyle(.borderlessButton).frame(maxWidth: 300, alignment: .trailing)
    }

    @ViewBuilder private func modelItems(_ pid: String, _ ids: [String]) -> some View {
        if ids.count > 14 {
            ForEach(families(ids), id: \.0) { fam, list in
                Menu(fam) { ForEach(list, id: \.self) { id in item(pid, id) } }
            }
        } else {
            ForEach(ids, id: \.self) { id in item(pid, id) }
        }
    }

    private func item(_ pid: String, _ id: String) -> some View {
        Button { choose(pid, id) } label: {
            if pid == c.provider && id == c.model { Label(id, systemImage: "checkmark") } else { Text(id) }
        }
    }

    /// Groups a long list by its prefix (before the first "/", else the first two dash parts).
    private func families(_ ids: [String]) -> [(String, [String])] {
        var order: [String] = [], by: [String: [String]] = [:]
        for id in ids {
            let fam: String
            if let i = id.firstIndex(of: "/") { fam = String(id[..<i]) }
            else { let p = id.split(separator: "-"); fam = p.count >= 3 ? p[0] + "-" + p[1] : (p.first.map(String.init) ?? id) }
            if by[fam] == nil { order.append(fam) }
            by[fam, default: []].append(id)
        }
        return order.map { ($0, by[$0] ?? []) }
    }

    private func choose(_ pid: String, _ id: String) {
        var values: SWJSON = ["provider": pid, "model": id]
        let allowed = m.providers?.swA("allow_training_models") ?? []
        if m.trainsOnInputs(provider: pid, model: id) && !allowed.contains(id) {
            let a = NSAlert()
            a.alertStyle = .warning
            a.messageText = "Use \(id)?"
            a.informativeText = m.providers?.swS("training_warning") ?? "This model's provider may use the inputs and outputs for training. Ask would send your question, the top passages from your documents and the last turns of the chat."
            a.addButton(withTitle: "Use This Model"); a.addButton(withTitle: "Cancel")
            let go: (NSApplication.ModalResponse) -> Void = { r in
                guard r == .alertFirstButtonReturn else { return }
                values["allow_training_models"] = allowed + [id]
                Task { @MainActor in m.patch("ask", values, err: "ask.model") }
            }
            if let w = m.window { a.beginSheetModal(for: w, completionHandler: go) } else { go(a.runModal()) }
            return
        }
        m.patch("ask", values, err: "ask.model")
    }

    private func customId() {
        let pid = c.provider.isEmpty ? (providers.first?.swS("id") ?? "anthropic") : c.provider
        let a = NSAlert()
        a.messageText = "Use a model id"
        a.informativeText = "Type the model id exactly as \(pid) names it, for example claude-opus-5-5."
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 300, height: 24))
        field.placeholderString = "model id"
        a.accessoryView = field
        a.addButton(withTitle: "Use"); a.addButton(withTitle: "Cancel")
        let go: (NSApplication.ModalResponse) -> Void = { r in
            let id = field.stringValue.trimmingCharacters(in: .whitespaces)
            guard r == .alertFirstButtonReturn, !id.isEmpty else { return }
            Task { @MainActor in choose(pid, id) }
        }
        if let w = m.window { a.beginSheetModal(for: w, completionHandler: go) } else { go(a.runModal()) }
    }
}

// MARK: - provider cards

struct SWProviderCard: View {
    @EnvironmentObject var m: SWModel
    let p: SWJSON
    let c: SWAskCfg
    @State private var key = ""
    @State private var replacing = false
    @State private var confirmForget = false
    @State private var confirmRemove = false

    private var pid: String { p.swS("id") ?? "" }
    private var busy: Bool { m.busy.contains { $0.hasPrefix(pid) } }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Text(p.swS("label") ?? pid).font(.headline)
                SWPill(text: stateText.0, tone: stateText.1)
                if pid == c.provider { SWPill(text: "chosen", tone: .busy) }
                Spacer()
                if busy { ProgressView().controlSize(.small) }
            }
            Text("\(kindName) · \(host)").font(.caption).foregroundStyle(.secondary)
            if let e = p.swS("error") { Text(e).font(.caption).foregroundStyle(.red) }
            if let h = p.swS("hint") { Text(h).font(.caption).foregroundStyle(.secondary) }
            keyRow
            HStack(spacing: 8) {
                Button("Test") { Task { await m.testProvider(pid, model: pid == c.provider ? c.model : nil) } }
                    .disabled(busy || (p.swB("needs_key") == true && p.swB("has_key") != true))
                Button("Refresh List") { Task { await m.refreshModels(pid) } }.disabled(busy || (p.swB("needs_key") == true && p.swB("has_key") != true))
                Text(modelsText).font(.caption).foregroundStyle(.secondary)
                Spacer()
                if p.swB("builtin") != true { Button("Remove…", role: .destructive) { confirmRemove = true } }
            }.controlSize(.small)
            if let n = m.providerNote[pid] { Text(n.text).font(.caption).foregroundStyle(n.ok ? Color.green : Color.red).fixedSize(horizontal: false, vertical: true) }
        }
        .padding(.vertical, 6)
        .confirmationDialog("Forget the key for \(p.swS("label") ?? pid)?", isPresented: $confirmForget) {
            Button("Forget Key", role: .destructive) { Task { await m.forgetKey(pid) } }
            Button("Cancel", role: .cancel) {}
        } message: { Text("The key is deleted from the macOS Keychain. You would need to paste it again to use this provider.") }
        .confirmationDialog("Remove \(p.swS("label") ?? pid)?", isPresented: $confirmRemove) {
            Button("Remove and Forget Its Key", role: .destructive) { Task { await m.removeProvider(pid, forgetKey: true) } }
            Button("Remove, Keep the Key") { Task { await m.removeProvider(pid, forgetKey: false) } }
            Button("Cancel", role: .cancel) {}
        }
    }

    private var stateText: (String, SWTone) {
        switch p.swS("status") ?? "" {
        case "ok": return ("Ready", .ok)
        case "no_key": return ("Needs a key", .warn)
        case "offline": return ("Offline", .neutral)
        case "error": return ("Error", .bad)
        default: return (p.swS("status") ?? "?", .neutral)
        }
    }

    private var kindName: String {
        ["openai-chat": "OpenAI-compatible", "openai-responses": "OpenAI Responses", "anthropic": "Anthropic API"][p.swS("kind") ?? ""] ?? (p.swS("kind") ?? "")
    }

    private var host: String { URL(string: p.swS("base_url") ?? "")?.host(percentEncoded: false) ?? (p.swS("base_url") ?? "") }

    private var modelsText: String {
        let n = p.swA("models").count
        return n == 0 ? "No models listed yet" : "\(n) models"
    }

    @ViewBuilder private var keyRow: some View {
        let has = p.swB("has_key") == true, needs = p.swB("needs_key") == true
        let keyFile = p.swS("key_file") ?? ""
        if !keyFile.isEmpty && p.swS("key_source") == "file" && !replacing {
            HStack {
                Label("Key read from \(keyFile)", systemImage: "checkmark.seal.fill").font(.callout).foregroundStyle(.green)
                Spacer()
                Button("Use a Different Key") { replacing = true }.controlSize(.small)
            }
        } else if !keyFile.isEmpty && !has {
            HStack {
                Button("Connect from the Key File") { Task { await m.connectProvider(pid) } }.disabled(busy)
                Text("Reads \(keyFile) and stores the key in the Keychain.").font(.caption).foregroundStyle(.secondary)
            }
        } else if !needs {
            Text("No key needed for this server.").font(.callout).foregroundStyle(.secondary)
        } else if has && !replacing {
            HStack {
                Label(p.swS("key_source") == "env" ? "Key from the environment" : "Key in the macOS Keychain", systemImage: "key.fill").font(.callout).foregroundStyle(.green)
                Spacer()
                Button("Replace Key") { replacing = true }.controlSize(.small)
                Button("Forget Key…", role: .destructive) { confirmForget = true }.controlSize(.small)
            }
        } else {
            HStack {
                SecureField("Paste the API key", text: $key).textFieldStyle(.roundedBorder).onSubmit(send)
                Button("Save Key", action: send).disabled(key.trimmingCharacters(in: .whitespaces).isEmpty || busy)
                if replacing { Button("Cancel") { replacing = false; key = "" } }
            }
        }
    }

    private func send() {
        let k = key.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !k.isEmpty else { return }
        key = ""                                  // never kept: cleared before the request is even sent
        replacing = false
        Task { await m.saveKey(pid, k) }
    }
}

struct SWAddProviderSheet: View {
    @EnvironmentObject var m: SWModel
    @State private var label = ""
    @State private var kind = "openai-chat"
    @State private var url = ""
    @State private var key = ""
    @State private var models = ""
    @State private var keyFile = ""
    @State private var training = ""
    @State private var problem: String?
    @State private var saving = false

    private let presets: [(String, String, String)] = [
        ("DeepSeek", "https://api.deepseek.com/v1", "deepseek-chat, deepseek-reasoner"),
        ("OpenRouter", "https://openrouter.ai/api/v1", ""),
        ("LM Studio", "http://localhost:1234/v1", ""),
        ("Ollama", "http://localhost:11434/v1", ""),
    ]

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Add a Provider").font(.headline)
            HStack {
                Text("Start from").foregroundStyle(.secondary)
                ForEach(presets, id: \.0) { p in Button(p.0) { label = p.0; url = p.1; models = p.2; kind = "openai-chat" }.controlSize(.small) }
            }
            field("Name", $label, "e.g. DeepSeek")
            Picker("Kind", selection: $kind) {
                Text("OpenAI-compatible (chat completions)").tag("openai-chat")
                Text("OpenAI Responses API").tag("openai-responses")
                Text("Anthropic Messages API").tag("anthropic")
            }
            field("Address", $url, "https://api.example.com/v1")
            VStack(alignment: .leading, spacing: 4) {
                Text("API key (optional)").font(.subheadline).foregroundStyle(.secondary)
                SecureField("Stored in the Keychain, never shown again", text: $key).textFieldStyle(.roundedBorder)
            }
            field("Models (optional)", $models, "comma separated; Refresh List fetches them later")
            field("Key file (optional)", $keyFile, "a file that holds the key, e.g. ~/.config/myrouter/key")
            field("Training warning (optional)", $training, "model ids (patterns with * and ?) whose provider may use inputs for training")
            if let problem { Text(problem).font(.callout).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true) }
            HStack {
                Spacer()
                Button("Cancel") { m.sheet = nil }.keyboardShortcut(.cancelAction)
                Button("Add Provider") { add() }.keyboardShortcut(.defaultAction).disabled(saving || label.trimmingCharacters(in: .whitespaces).isEmpty || url.isEmpty)
            }
        }
        .padding(20).frame(width: 500)
    }

    private func field(_ t: String, _ b: Binding<String>, _ ph: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(t).font(.subheadline).foregroundStyle(.secondary)
            TextField(ph, text: b).textFieldStyle(.roundedBorder)
        }
    }

    private func add() {
        saving = true
        var body: SWJSON = ["id": groupSlug(label), "label": label.trimmingCharacters(in: .whitespaces), "kind": kind, "base_url": url.trimmingCharacters(in: .whitespaces)]
        let list = models.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        if !list.isEmpty { body["models"] = list }
        let kf = keyFile.trimmingCharacters(in: .whitespaces)
        if !kf.isEmpty { body["key_file"] = kf }
        let tm = training.split(whereSeparator: { ", ".contains($0) }).map(String.init)
        if !tm.isEmpty { body["training_models"] = tm }
        let k = key.trimmingCharacters(in: .whitespacesAndNewlines)
        if !k.isEmpty { body["key"] = k }
        key = ""
        Task {
            let e = await m.addProvider(body)
            saving = false
            if let e { problem = e } else { m.sheet = nil }
        }
    }
}
