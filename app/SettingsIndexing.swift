// The native Settings window: Indexing (what is read and what is skipped) and the Embedding placeholders. Every option
// appears only when the service sends its key in settings.indexing or settings.embedding (SWModel.indexingKeys, checked
// key by key), and nothing is posted for a key that is missing. The per-folder overrides are in SettingsFolders.swift.

import Cocoa
import SwiftUI

struct SWIndexingView: View {
    @EnvironmentObject var m: SWModel

    var body: some View {
        let g = m.indexingKeys
        SWFormPage {
            if !m.indexingShown {
                Section {
                    SWNote(tone: .neutral, text: "These options come with a newer search service. Nothing here needs changing yet.")
                }
            }
            if g["respect_gitignore"] != nil || g["index_code"] != nil {
                Section {
                    if g["respect_gitignore"] != nil {
                        SWRow(id: "indexing.gitignore", title: "Skip files listed in .gitignore",
                              detail: "Files that a git repository ignores, such as build output and caches, are left out of the index. A folder can have its own setting.",
                              errorKey: "indexing.respect_gitignore") {
                            Toggle("", isOn: m.bind("indexing", "respect_gitignore", true)).labelsHidden().toggleStyle(.switch)
                        }
                    }
                    if g["index_code"] != nil {
                        SWRow(id: "indexing.code", title: "Index code files (source code, lockfiles)",
                              detail: "Off: code files are found by name only. On: their text is read and searched too.",
                              errorKey: "indexing.index_code") {
                            Toggle("", isOn: m.bind("indexing", "index_code", false)).labelsHidden().toggleStyle(.switch)
                        }
                    }
                } header: { Text("Reading files") }
            }
            if g["extra_excludes"] != nil || !m.settings.swO("indexing_info").swA("prune_dirs_default").isEmpty {
                Section {
                    SWExcludesEditor()
                } header: { Text("Never index these folders") }
            }
            if m.embeddingAvailable {
                // Placeholders: the keys and their values come with a later service. Values are provisional (see the report).
                Section {
                    if m.settings.swO("embedding")["mode"] != nil {
                        SWRow(id: "embedding.mode", title: "Embedding mode",
                              detail: "How pieces of text are turned into meaning-based search entries.", errorKey: "embedding.mode") {
                            Picker("", selection: m.bind("embedding", "mode", "fast", err: "embedding.mode")) {
                                Text("Fast").tag("fast")
                                Text("Full quality").tag("full")
                            }
                            .labelsHidden().frame(width: 180)
                        }
                    }
                    if m.settings.swO("embedding")["priority"] != nil {
                        SWRow(id: "embedding.priority", title: "Priority",
                              detail: "How much the embedding work competes with your other apps.", errorKey: "embedding.priority") {
                            Picker("", selection: m.bind("embedding", "priority", "balanced", err: "embedding.priority")) {
                                Text("Low impact").tag("low")
                                Text("Balanced").tag("balanced")
                            }
                            .labelsHidden().frame(width: 180)
                        }
                    }
                } header: { Text("Embedding") }
            }
        }
    }
}

/// "Never index these folders": the built-in list (prune_dirs, read only) and the user's own entries (extra_excludes).
/// The whole extra list is written at once, trimmed and without duplicates. The built-in list cannot be edited here.
struct SWExcludesEditor: View {
    @EnvironmentObject var m: SWModel
    @State private var draft = ""
    private let err = "indexing.extra_excludes"

    /// The folders the walk skips by default: read only, from the service's indexing_info (indexing.prune_dirs is an override).
    private var builtIn: [String] { m.settings.swO("indexing_info").swA("prune_dirs_default") }
    private var extras: [String] { m.indexingKeys.swA("extra_excludes") }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if !builtIn.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Built in").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                    SWFlowLayout { ForEach(builtIn, id: \.self) { SWChip(text: $0, mono: true) } }
                }
            }
            VStack(alignment: .leading, spacing: 6) {
                Text("Yours").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                if extras.isEmpty { Text("None yet.").font(.callout).foregroundStyle(.secondary) }
                SWFlowLayout {
                    ForEach(extras, id: \.self) { item in
                        HStack(spacing: 4) {
                            Text(item).font(.system(size: 11, design: .monospaced))
                            Button { remove(item) } label: { Image(systemName: "xmark.circle.fill") }
                                .buttonStyle(.plain).foregroundStyle(.secondary).help("Stop skipping \(item)")
                        }
                        .padding(.horizontal, 7).padding(.vertical, 2)
                        .background(Capsule().fill(Color(nsColor: .quaternaryLabelColor).opacity(0.5)))
                    }
                }
            }
            HStack(spacing: 8) {
                TextField("", text: $draft, prompt: Text("Folder name or pattern, e.g. exports or *.bak"))
                    .textFieldStyle(.roundedBorder).frame(maxWidth: 300)
                    .onSubmit(add)
                Button("Add", action: add).disabled(draft.trimmingCharacters(in: .whitespaces).isEmpty)
            }
            SWInlineError(key: err)
        }
        .id("indexing.excludes")
    }

    private func add() {
        let value = draft.trimmingCharacters(in: .whitespaces)
        guard !value.isEmpty else { return }
        guard !value.contains("\n") else { m.errors[err] = "Add one name or pattern at a time."; return }
        guard !builtIn.contains(value) else { m.errors[err] = "“\(value)” is already in the built-in list."; return }
        draft = ""
        guard !extras.contains(value) else { return }
        m.errors[err] = nil
        m.patch("indexing", ["extra_excludes": extras + [value]], err: err)
    }

    private func remove(_ item: String) {
        m.patch("indexing", ["extra_excludes": extras.filter { $0 != item }], err: err)
    }
}
