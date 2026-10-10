// Folder groups in the Indexed sidebar: GroupStore (every group change goes through its POST /api/groups calls),
// the group editor sheet (SwiftUI in an NSHostingController), and two headless entry points: the --selftest-groups
// run against a DEV service, and the group-editor snapshot. Nothing here shows a window except the sheet itself.

import Cocoa
import SwiftUI

/// Notifications for the group UI. GroupStore posts `changed` after every successful change; the sidebar and any
/// list of groups reload on it. `new` is posted by File > New Group… and opens an empty editor.
enum GroupNotes {
    static let changed = Notification.Name("MSEGroupsChanged")
    static let new = Notification.Name("MSENewGroup")
}

/// A group as the service lists it (label, aliases and the tab flag included).
struct GroupDetail: Hashable {
    let name: String           // the immutable id (a slug)
    let label: String          // the name shown
    let members: [String]      // folder (collection) names
    let aliases: [String]
    let hidden: Bool           // true = no tab of its own

    init(name: String, label: String, members: [String], aliases: [String], hidden: Bool) {
        self.name = name; self.label = label; self.members = members; self.aliases = aliases; self.hidden = hidden
    }

    /// One group object from the service ({name, label, collections, aliases, hidden, files}); nil without a name.
    init?(json g: [String: Any]) {
        guard let name = g["name"] as? String else { return nil }
        self.init(name: name,
                  label: (g["label"] as? String) ?? name,
                  members: (g["collections"] as? [Any])?.compactMap { $0 as? String } ?? [],
                  aliases: (g["aliases"] as? [Any])?.compactMap { $0 as? String } ?? [],
                  hidden: (g["hidden"] as? Bool) ?? false)
    }
}

extension MSEClient {
    /// The folders and the groups from one GET /api/collections, with each group's label, aliases and tab flag.
    func libraryWithGroups() async throws -> (IndexedLibrary, [GroupDetail]) {
        guard let obj = try await get("/api/collections") as? [String: Any] else {
            return (IndexedLibrary(collections: [], groups: []), [])
        }
        let rows = (obj["collections"] as? [[String: Any]]) ?? []
        let collections: [IndexedCollection] = rows.compactMap { r in
            guard let name = r["name"] as? String, let path = r["path"] as? String else { return nil }
            let stats = r["stats"] as? [String: Any]
            return IndexedCollection(name: name, path: path,
                                     enabled: (r["enabled"] as? Bool) ?? true,
                                     exists: (r["exists"] as? Bool) ?? true,
                                     files: (stats?["files"] as? NSNumber)?.intValue ?? 0)
        }
        let details = ((obj["groups"] as? [[String: Any]]) ?? []).compactMap { GroupDetail(json: $0) }
        let groups = details.map { IndexedGroup(name: $0.name, members: $0.members) }
        return (IndexedLibrary(collections: collections, groups: groups), details)
    }
}

/// The only place that changes groups. Each call is one POST to the service, which validates it; a refusal comes
/// back as MSEServerMessage with the service's own text. A success posts GroupNotes.changed.
@MainActor
enum GroupStore {
    /// Creates a group (the service derives its id from the label) or, with `existing`, rewrites it in full.
    static func save(existing: GroupDetail?, label: String, members: [String], aliases: [String],
                     showAsTab: Bool) async throws -> GroupDetail {
        var body: [String: Any] = ["label": label, "collections": members, "aliases": aliases, "hidden": !showAsTab]
        if let existing { body["name"] = existing.name }
        return try await send(body)
    }

    /// A partial update: only the fields given change; the service keeps the rest. Members [] is refused.
    static func update(_ g: GroupDetail, label: String? = nil, members: [String]? = nil,
                       aliases: [String]? = nil, hidden: Bool? = nil) async throws -> GroupDetail {
        var body: [String: Any] = ["name": g.name]
        if let label { body["label"] = label }
        if let members { body["collections"] = members }
        if let aliases { body["aliases"] = aliases }
        if let hidden { body["hidden"] = hidden }
        return try await send(body)
    }

    static func delete(_ name: String) async throws {
        _ = try await MSEClient.shared.postJSON("/api/groups/\(name)/remove", body: [:])
        NotificationCenter.default.post(name: GroupNotes.changed, object: nil)
    }

    /// Short names for a folder ("w: invoice"); [] removes them.
    static func setFolderAliases(_ folder: String, _ aliases: [String]) async throws {
        _ = try await MSEClient.shared.postJSON("/api/collections/\(folder)", body: ["aliases": aliases])
        NotificationCenter.default.post(name: GroupNotes.changed, object: nil)
    }

    private static func send(_ body: [String: Any]) async throws -> GroupDetail {
        let obj = try await MSEClient.shared.postJSON("/api/groups", body: body)
        guard let group = GroupDetail(json: obj) else { throw MSEServerMessage(message: "unexpected answer from the service") }
        NotificationCenter.default.post(name: GroupNotes.changed, object: nil)
        return group
    }
}

/// A slug as the service makes one from a label: lowercase letters and digits, single dashes.
func groupSlug(_ text: String) -> String {
    var out = ""
    for ch in text.lowercased() {
        if ch.isASCII && (ch.isLetter || ch.isNumber) { out.append(ch) }
        else if !out.isEmpty && out.last != "-" { out.append("-") }
    }
    while out.last == "-" { out.removeLast() }
    return out
}

// MARK: - Editor

/// The state of one editor sheet. Nothing is sent to the service until Save; the checks before that are local.
@MainActor
final class GroupEditorModel: ObservableObject {
    let existing: GroupDetail?
    @Published var label: String
    @Published var showAsTab: Bool
    @Published var picked: Set<String>
    @Published var aliases: [String]
    @Published var aliasDraft = ""
    @Published var folders: [IndexedCollection] = []
    @Published var loading = true
    @Published var saving = false
    /// A message from the service (after Save) or from the editor (an alias that cannot be right). Shown in red.
    @Published var problem: String?

    init(existing: GroupDetail?, preselect: [String] = []) {
        self.existing = existing
        label = existing?.label ?? ""
        showAsTab = !(existing?.hidden ?? false)
        picked = Set(existing?.members ?? preselect)
        aliases = existing?.aliases ?? []
    }

    var trimmedLabel: String { label.trimmingCharacters(in: .whitespacesAndNewlines) }
    /// The id the service will use: the existing one, or the slug of the label.
    var idPreview: String { existing?.name ?? groupSlug(trimmedLabel) }

    /// What can be known without the service. Empty = ready to save.
    var localHint: String? {
        if trimmedLabel.isEmpty { return "Give the group a name." }
        if picked.isEmpty { return "Tick at least one folder. A group needs one." }
        return nil
    }

    var canSave: Bool { localHint == nil && !saving && !loading }

    func load() async {
        loading = true
        defer { loading = false }
        do {
            let (lib, _) = try await MSEClient.shared.libraryWithGroups()
            folders = lib.collections.sorted { $0.name < $1.name }
        } catch {
            problem = "Could not read the folders: \(error.localizedDescription)"
        }
    }

    func isMember(_ name: String) -> Bool { picked.contains(name) }

    func toggle(_ name: String, on: Bool) {
        if on { picked.insert(name) } else { picked.remove(name) }
    }

    /// An alias is checked here as it is typed (no request). The service still checks reserved words and clashes.
    /// Returns false (and keeps the draft, with a message) when the draft is not a valid alias.
    @discardableResult
    func commitAliasDraft() -> Bool {
        let alias = aliasDraft.trimmingCharacters(in: .whitespaces).lowercased()
        guard !alias.isEmpty else { return true }
        guard GroupEditorModel.isAliasShape(alias) else {
            problem = "An alias is lowercase letters, digits and single dashes, e.g. \"\(groupSlug(alias))\"."
            return false
        }
        aliasDraft = ""
        if !aliases.contains(alias) { aliases.append(alias) }
        problem = nil
        return true
    }

    func removeAlias(_ alias: String) { aliases.removeAll { $0 == alias } }

    static func isAliasShape(_ s: String) -> Bool {
        s.range(of: #"^[a-z0-9]+(-[a-z0-9]+)*$"#, options: .regularExpression) != nil
    }

    /// Saves through GroupStore. Returns the saved group, or nil with `problem` set to the service's message.
    func save() async -> GroupDetail? {
        guard commitAliasDraft(), canSave else { return nil }
        saving = true
        defer { saving = false }
        let members = folders.map(\.name).filter { picked.contains($0) }
            + picked.subtracting(folders.map(\.name)).sorted()   // a folder missing from the list stays a member
        do {
            let group = try await GroupStore.save(existing: existing, label: trimmedLabel, members: members,
                                                  aliases: aliases, showAsTab: showAsTab)
            problem = nil
            return group
        } catch {
            problem = GroupEditorModel.message(for: error)
            return nil
        }
    }

    /// The service's own sentence, without the Python exception name in front of it.
    static func message(for error: Error) -> String {
        guard let m = (error as? MSEServerMessage)?.message else { return error.localizedDescription }
        return m.hasPrefix("ValueError: ") ? String(m.dropFirst("ValueError: ".count)) : m
    }
}

/// Wraps alias chips across lines, without a fixed width.
private struct WrapLayout: Layout {
    var spacing: CGFloat = 6

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let maxWidth = proposal.width ?? 400
        var x: CGFloat = 0, y: CGFloat = 0, rowHeight: CGFloat = 0
        for view in subviews {
            let s = view.sizeThatFits(.unspecified)
            if x + s.width > maxWidth && x > 0 { x = 0; y += rowHeight + spacing; rowHeight = 0 }
            x += s.width + spacing
            rowHeight = max(rowHeight, s.height)
        }
        return CGSize(width: maxWidth, height: y + rowHeight)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY, rowHeight: CGFloat = 0
        for view in subviews {
            let s = view.sizeThatFits(.unspecified)
            if x + s.width > bounds.maxX && x > bounds.minX { x = bounds.minX; y += rowHeight + spacing; rowHeight = 0 }
            view.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(s))
            x += s.width + spacing
            rowHeight = max(rowHeight, s.height)
        }
    }
}

struct GroupEditorView: View {
    @ObservedObject var model: GroupEditorModel
    var onCancel: () -> Void
    var onSaved: (GroupDetail) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(model.existing == nil ? "New Group" : "Edit Group")
                .font(.headline)

            VStack(alignment: .leading, spacing: 4) {
                Text("Name").font(.subheadline).foregroundStyle(.secondary)
                TextField("e.g. Client Work", text: $model.label)
                    .textFieldStyle(.roundedBorder)
                Text(model.idPreview.isEmpty ? "ID: set when you type a name" : "ID: \(model.idPreview)")
                    .font(.caption).foregroundStyle(.secondary)
            }

            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text("Folders").font(.subheadline).foregroundStyle(.secondary)
                    Spacer()
                    if model.loading { ProgressView().controlSize(.small) }
                }
                ScrollView {
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(model.folders, id: \.name) { folder in
                            Toggle(isOn: Binding(get: { model.isMember(folder.name) },
                                                 set: { model.toggle(folder.name, on: $0) })) {
                                HStack {
                                    Text(folder.name)
                                    Spacer()
                                    Text(folder.files == 1 ? "1 file" : "\(folder.files) files")
                                        .font(.caption).foregroundStyle(.secondary)
                                }
                            }
                            .toggleStyle(.checkbox)
                        }
                        if !model.loading && model.folders.isEmpty {
                            Text("No folders are indexed yet.").foregroundStyle(.secondary)
                        }
                    }
                    .padding(8)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(height: 150)
                .background(RoundedRectangle(cornerRadius: 6).strokeBorder(.quaternary))
            }

            VStack(alignment: .leading, spacing: 4) {
                Text("Aliases").font(.subheadline).foregroundStyle(.secondary)
                WrapLayout {
                    ForEach(model.aliases, id: \.self) { alias in
                        HStack(spacing: 4) {
                            Text(alias).font(.callout)
                            Button { model.removeAlias(alias) } label: {
                                Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary)
                            }
                            .buttonStyle(.plain)
                            .accessibilityLabel("Remove alias \(alias)")
                        }
                        .padding(.horizontal, 8).padding(.vertical, 3)
                        .background(Capsule().fill(.quaternary))
                    }
                }
                TextField("Type a short name, then press Return", text: $model.aliasDraft)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit { model.commitAliasDraft() }
                Text("Used in search, e.g. \"w: invoice\".").font(.caption).foregroundStyle(.secondary)
            }

            Toggle("Show as a tab", isOn: $model.showAsTab)

            if let problem = model.problem {
                Text(problem).font(.callout).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true)
            } else if let hint = model.localHint, !model.loading {
                Text(hint).font(.callout).foregroundStyle(.secondary)
            }

            HStack {
                Spacer()
                Button("Cancel") { onCancel() }
                    .keyboardShortcut(.cancelAction)
                Button(model.existing == nil ? "Create Group" : "Save") {
                    Task {
                        if let group = await model.save() { onSaved(group) }
                    }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(!model.canSave)
            }
        }
        .padding(20)
        .frame(width: 480)
    }
}

/// Presents the editor as a sheet on `window`. `done` runs with the saved group; Cancel only closes the sheet.
@MainActor
enum GroupEditorSheet {
    static func present(on window: NSWindow?, existing: GroupDetail? = nil, preselect: [String] = [],
                        done: ((GroupDetail) -> Void)? = nil) {
        guard let window else { return }
        let model = GroupEditorModel(existing: existing, preselect: preselect)
        let panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 480, height: 400),
                            styleMask: [.titled], backing: .buffered, defer: false)
        panel.title = existing == nil ? "New Group" : "Edit Group"
        let view = GroupEditorView(
            model: model,
            onCancel: { window.endSheet(panel) },
            onSaved: { group in window.endSheet(panel); done?(group) })
        panel.contentViewController = NSHostingController(rootView: view)
        window.beginSheet(panel) { _ in }
        Task { await model.load() }
    }
}

// MARK: - Snapshot (off-screen; never ordered front)

/// `--snapshot <png> --state group-editor [--dark]`: the editor, with the group "team-docs" (or the first group)
/// loaded from the service the app points at, rendered into a PNG in an off-screen window.
@MainActor
func runGroupEditorSnapshot(_ o: SnapshotOptions) {
    NSApp.appearance = NSAppearance(named: o.dark ? .darkAqua : .aqua)
    let frame = NSRect(x: -30000, y: -30000, width: 520, height: 640)
    let window = NSWindow(contentRect: frame, styleMask: [.titled], backing: .buffered, defer: false)
    window.appearance = NSApp.appearance
    Task { @MainActor in
        let details = (try? await MSEClient.shared.libraryWithGroups().1) ?? []
        let existing = details.first { $0.name == "team-docs" } ?? details.first
        let model = GroupEditorModel(existing: existing)
        let view = GroupEditorView(model: model, onCancel: {}, onSaved: { _ in })
        window.contentViewController = NSHostingController(rootView: view)
        window.setFrame(frame, display: false)
        await model.load()
        try? await Task.sleep(nanoseconds: 400_000_000)
        guard let frameView = window.contentView?.superview else { snapshotFail("the editor has no frame view") }
        window.layoutIfNeeded()
        frameView.layoutSubtreeIfNeeded()
        let bounds = frameView.bounds
        guard let rep = frameView.bitmapImageRepForCachingDisplay(in: bounds) else { snapshotFail("could not create a bitmap") }
        window.effectiveAppearance.performAsCurrentDrawingAppearance { frameView.cacheDisplay(in: bounds, to: rep) }
        guard let data = rep.representation(using: .png, properties: [:]) else { snapshotFail("PNG encoding failed") }
        let url = URL(fileURLWithPath: o.output)
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        do { try data.write(to: url) } catch { snapshotFail("could not write \(o.output)") }
        print(url.path)
        print("members ticked: \(model.picked.sorted().joined(separator: ", "))")
        exit(0)
    }
}

// MARK: - Self-test (headless: --selftest-groups)

/// Runs the same GroupStore calls the menus make, against the DEV service named by MSE_API_BASE. It refuses to run
/// without MSE_API_BASE and MSE_API_TOKEN, or when the base is the live port 8737. It opens no window and no alert.
@MainActor
enum GroupsSelfTest {
    static func run() -> Never {
        let env = ProcessInfo.processInfo.environment
        guard let base = env["MSE_API_BASE"], let url = URL(string: base), url.port != 8737,
              let token = env["MSE_API_TOKEN"], !token.isEmpty else {
            print("SELFTEST refused: set MSE_API_BASE (a dev instance, not port 8737) and MSE_API_TOKEN")
            exit(3)
        }
        print("SELFTEST base \(base)")
        Task { @MainActor in
            let code = await steps()
            exit(code)
        }
        dispatchMain()
    }

    private static func steps() async -> Int32 {
        final class Counter: @unchecked Sendable { var changes = 0 }   // only touched on the main queue
        let counter = Counter()
        _ = NotificationCenter.default.addObserver(forName: GroupNotes.changed, object: nil, queue: nil) { _ in
            counter.changes += 1
        }
        var failures = 0
        func check(_ label: String, _ ok: Bool, _ detail: String) {
            print("SELFTEST \(ok ? "PASS" : "FAIL") \(label): \(detail)")
            if !ok { failures += 1 }
        }
        /// A step the service must refuse: passes when the refusal contains `expect`.
        func refused(_ label: String, expect: String, _ op: () async throws -> Any) async {
            do {
                _ = try await op()
                check(label, false, "accepted, but a refusal was expected")
            } catch {
                let text = GroupEditorModel.message(for: error)
                check(label, text.contains(expect), "refused as expected: \"\(text)\"")
            }
        }
        func groupNamed(_ name: String) async -> GroupDetail? {
            ((try? await MSEClient.shared.libraryWithGroups().1) ?? []).first { $0.name == name }
        }
        do {
            if await groupNamed("self-test-group") != nil {                          // a leftover from an earlier run
                try await GroupStore.delete("self-test-group")
            }
            let g0 = try await GroupStore.save(existing: nil, label: "Self Test Group",
                                               members: ["alpha", "beta"], aliases: [], showAsTab: true)
            check("create", g0.name == "self-test-group" && g0.members == ["alpha", "beta"],
                  "id \(g0.name), members \(g0.members.joined(separator: ","))")

            let g1 = try await GroupStore.update(g0, label: "Self Test Renamed")
            check("rename (label only)", g1.label == "Self Test Renamed" && g1.name == g0.name && g1.members == g0.members,
                  "label \"\(g1.label)\", id unchanged \(g1.name), members kept")

            let g2 = try await GroupStore.update(g1, members: ["alpha", "beta", "gamma"])
            check("add member", g2.members == ["alpha", "beta", "gamma"], "members \(g2.members.joined(separator: ","))")

            let g3 = try await GroupStore.update(g2, members: ["alpha", "gamma"])
            check("remove member", g3.members == ["alpha", "gamma"], "members \(g3.members.joined(separator: ","))")

            await refused("remove the last member", expect: "at least one collection") {
                try await GroupStore.update(g3, members: [])
            }

            let g4 = try await GroupStore.update(g3, aliases: ["st-group"])
            check("set alias", g4.aliases == ["st-group"], "aliases \(g4.aliases.joined(separator: ","))")

            await refused("invalid alias", expect: "lowercase letters") {
                try await GroupStore.update(g4, aliases: ["Bad Alias"])
            }

            let g5 = try await GroupStore.update(g4, hidden: true)
            check("hide as tab", g5.hidden, "hidden \(g5.hidden)")
            _ = try await GroupStore.update(g5, hidden: false)

            try await GroupStore.setFolderAliases("gamma", ["st-gamma"])
            try await GroupStore.setFolderAliases("gamma", [])
            check("folder alias set and cleared", true, "POST /api/collections/gamma {aliases}")

            await refused("name clash with a folder", expect: "already the name of a collection") {
                try await GroupStore.save(existing: nil, label: "alpha", members: ["beta"], aliases: [], showAsTab: true)
            }
            await refused("unknown folder", expect: "unknown collection") {
                try await GroupStore.save(existing: nil, label: "Ghost", members: ["nope"], aliases: [], showAsTab: true)
            }

            // the editor's own path: local checks first (no request), then Save through GroupStore
            let blank = GroupEditorModel(existing: nil, preselect: [])
            blank.label = "Editor Test"
            await blank.load()
            check("editor: no folder ticked blocks Save", !blank.canSave && blank.localHint != nil,
                  "hint: \"\(blank.localHint ?? "none")\"")
            let editor = GroupEditorModel(existing: nil, preselect: ["alpha"])
            editor.label = "Editor Test"
            await editor.load()
            let saved = await editor.save()
            check("editor: Save creates the group", saved?.name == "editor-test" && saved?.members == ["alpha"],
                  "id \(saved?.name ?? "none"), members \(saved?.members.joined(separator: ",") ?? "none"), problem \"\(editor.problem ?? "")\"")
            editor.aliasDraft = "Not Valid"
            let blocked = await editor.save()
            check("editor: a bad alias blocks Save", blocked == nil && editor.problem != nil && editor.aliases.isEmpty,
                  "problem: \"\(editor.problem ?? "none")\"")
            if saved != nil { try await GroupStore.delete("editor-test") }

            try await GroupStore.delete("self-test-group")
            check("delete", await groupNamed("self-test-group") == nil, "group removed from the service")
        } catch {
            check("unexpected error", false, GroupEditorModel.message(for: error))
        }
        check("change notifications", counter.changes >= 8, "\(counter.changes) GroupNotes.changed posts")
        print("SELFTEST RESULT: \(failures == 0 ? "all passed" : "\(failures) failed")")
        return failures == 0 ? 0 : 1
    }
}
