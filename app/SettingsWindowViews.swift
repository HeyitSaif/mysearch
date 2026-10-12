// The native Settings window, part 2: the frame (sidebar with search-in-settings, section header, saved chip, notices)
// and the small pieces every screen shares: rows, pills, banners that carry their own fix button, a wrapping layout.

import Cocoa
import SwiftUI

// MARK: - shared pieces

struct SWAction: Identifiable {
    let title: String
    let run: () -> Void
    var id: String { title }
}

extension SWTone {
    var color: Color {
        switch self {
        case .ok: return .green
        case .warn: return .orange
        case .bad: return .red
        case .busy: return .blue
        case .neutral: return .secondary
        }
    }
}

struct SWPill: View {
    let text: String
    var tone: SWTone = .neutral
    var body: some View {
        Text(text)
            .font(.system(size: 11, weight: .medium))
            .padding(.horizontal, 8).padding(.vertical, 2)
            .foregroundStyle(tone.color)
            .background(Capsule().fill(tone.color.opacity(0.14)))
            .lineLimit(1)
    }
}

/// A warning or hint that always carries the button that fixes it.
struct SWNote: View {
    var tone: SWTone = .warn
    let text: String
    var actions: [SWAction] = []
    var icon: String?
    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: icon ?? (tone == .bad ? "exclamationmark.octagon.fill" : tone == .ok ? "checkmark.circle.fill" : tone == .neutral ? "info.circle.fill" : "exclamationmark.triangle.fill"))
                .foregroundStyle(tone.color)
            VStack(alignment: .leading, spacing: 6) {
                Text(text).font(.callout).fixedSize(horizontal: false, vertical: true)
                if !actions.isEmpty {
                    HStack { ForEach(actions) { a in Button(a.title, action: a.run).controlSize(.small) } }
                }
            }
            Spacer(minLength: 0)
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 8).fill(tone.color.opacity(0.10)))
    }
}

struct SWInlineError: View {
    @EnvironmentObject var m: SWModel
    let key: String
    var body: some View {
        if let e = m.errors[key], !e.isEmpty {
            HStack(alignment: .top, spacing: 6) {
                Image(systemName: "exclamationmark.circle.fill").foregroundStyle(.red)
                Text(e).font(.caption).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
                Button { m.errors[key] = nil } label: { Image(systemName: "xmark") }.buttonStyle(.plain).foregroundStyle(.secondary)
            }
        }
    }
}

/// One setting: a label with a plain-language explanation and, when it matters, what it costs; the control on the right.
struct SWRow<Control: View>: View {
    @EnvironmentObject var m: SWModel
    let id: String
    let title: String
    var detail: String?
    var cost: String?
    var errorKey: String?
    @ViewBuilder var control: () -> Control

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline, spacing: 16) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(title)
                    if let detail { Text(detail).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true) }
                    if let cost {
                        Label(cost, systemImage: "gauge.with.dots.needle.33percent")
                            .font(.caption).foregroundStyle(.secondary).labelStyle(.titleAndIcon)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer(minLength: 12)
                control()
            }
            if let errorKey { SWInlineError(key: errorKey) }
        }
        .padding(.vertical, 2)
        .background(RoundedRectangle(cornerRadius: 6).fill(m.highlight == id ? Color.accentColor.opacity(0.22) : .clear)
            .padding(.horizontal, -6).animation(.easeOut(duration: 0.4), value: m.highlight))
        .id(id)
    }
}

/// An integer field with a stepper; the typed value is sent when the field is left, a click on the stepper after a pause.
struct SWNumberField: View {
    let value: Int
    let range: ClosedRange<Int>
    var step = 1
    var unit: String?
    let onChange: (Int) -> Void
    @State private var text = ""
    @FocusState private var focused: Bool

    var body: some View {
        HStack(spacing: 6) {
            TextField("", text: $text)
                .textFieldStyle(.roundedBorder).multilineTextAlignment(.trailing)
                .frame(width: 64).focused($focused)
                .onSubmit(commit)
            Stepper("", value: Binding(get: { value }, set: { onChange(min(max($0, range.lowerBound), range.upperBound)) }),
                    in: range, step: step).labelsHidden()
            if let unit { Text(unit).foregroundStyle(.secondary).font(.callout) }
        }
        .onAppear { text = String(value) }
        .onChange(of: value) { _, v in if !focused { text = String(v) } }
        .onChange(of: focused) { _, f in if !f { commit() } }
    }

    private func commit() {
        if let n = Int(text.trimmingCharacters(in: .whitespaces)) {
            let c = min(max(n, range.lowerBound), range.upperBound)
            text = String(c)
            if c != value { onChange(c) }
        } else { text = String(value) }
    }
}

/// A single-line text field that saves when Return is pressed or the field is left.
struct SWTextRow: View {
    let initial: String
    var placeholder = ""
    let onCommit: (String) -> Void
    @State private var text = ""
    @FocusState private var focused: Bool

    var body: some View {
        TextField(placeholder, text: $text)
            .textFieldStyle(.roundedBorder).focused($focused)
            .onSubmit(commit)
            .onAppear { text = initial }
            .onChange(of: initial) { _, v in if !focused { text = v } }
            .onChange(of: focused) { _, f in if !f { commit() } }
    }

    private func commit() { if text != initial { onCommit(text) } }
}

/// A multi-line list (one entry per line). It saves when the field is left or after a pause in typing.
struct SWTextArea: View {
    let initial: String
    var minHeight: CGFloat = 70
    var onType: ((String) -> Void)?
    let onCommit: (String) -> Void
    @State private var text = ""
    @State private var pause: Task<Void, Never>?
    @FocusState private var focused: Bool

    var body: some View {
        TextEditor(text: $text)
            .font(.system(size: 12, design: .monospaced))
            .scrollContentBackground(.hidden)
            .padding(4)
            .frame(minHeight: minHeight)
            .background(RoundedRectangle(cornerRadius: 6).fill(Color(nsColor: .textBackgroundColor)))
            .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(Color(nsColor: .separatorColor)))
            .focused($focused)
            .onAppear { text = initial }
            .onChange(of: initial) { _, v in if !focused { text = v } }
            .onChange(of: text) { _, v in
                onType?(v)
                pause?.cancel()
                pause = Task { try? await Task.sleep(nanoseconds: 1_400_000_000); if !Task.isCancelled { commit() } }
            }
            .onChange(of: focused) { _, f in if !f { commit() } }
    }

    private func commit() { pause?.cancel(); if text != initial { onCommit(text) } }
}

struct SWFlowLayout: Layout {
    var spacing: CGFloat = 6
    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let maxW = proposal.width ?? 400
        var x: CGFloat = 0, y: CGFloat = 0, rowH: CGFloat = 0
        for v in subviews {
            let s = v.sizeThatFits(.unspecified)
            if x + s.width > maxW && x > 0 { x = 0; y += rowH + spacing; rowH = 0 }
            x += s.width + spacing; rowH = max(rowH, s.height)
        }
        return CGSize(width: maxW, height: y + rowH)
    }
    func placeSubviews(in b: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = b.minX, y = b.minY, rowH: CGFloat = 0
        for v in subviews {
            let s = v.sizeThatFits(.unspecified)
            if x + s.width > b.maxX && x > b.minX { x = b.minX; y += rowH + spacing; rowH = 0 }
            v.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(s))
            x += s.width + spacing; rowH = max(rowH, s.height)
        }
    }
}

struct SWChip: View {
    let text: String
    var mono = false
    var body: some View {
        Text(text).font(mono ? .system(size: 11, design: .monospaced) : .system(size: 11))
            .padding(.horizontal, 7).padding(.vertical, 2)
            .background(Capsule().fill(Color(nsColor: .quaternaryLabelColor).opacity(0.5)))
    }
}

/// A rounded panel (cards on the Folders, Groups, Favourites and Agents screens).
struct SWCard<Content: View>: View {
    @ViewBuilder var content: () -> Content
    var body: some View {
        content()
            .background(RoundedRectangle(cornerRadius: 12).fill(Color(nsColor: .controlBackgroundColor)))
            .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color(nsColor: .separatorColor).opacity(0.7)))
    }
}

/// A screen made of a grouped Form.
struct SWFormPage<Content: View>: View {
    @EnvironmentObject var m: SWModel
    @ViewBuilder var content: () -> Content
    var body: some View {
        ScrollViewReader { proxy in
            Form { content() }
                .formStyle(.grouped)
                .task(id: m.highlight) {
                    guard let h = m.highlight else { return }
                    try? await Task.sleep(nanoseconds: 200_000_000)
                    withAnimation { proxy.scrollTo(h, anchor: .center) }
                }
        }
    }
}

func swCopy(_ s: String) {
    NSPasteboard.general.clearContents()
    NSPasteboard.general.setString(s, forType: .string)
}

// MARK: - the window's content

struct SWRootView: View {
    @ObservedObject var m: SWModel
    var body: some View {
        HStack(spacing: 0) {
            SWSidebar().frame(width: 236)
            Divider()
            SWDetail()
        }
        .environmentObject(m)
        .frame(minWidth: 880, minHeight: 560)
        .sheet(item: $m.sheet) { s in
            switch s {
            case .addFolder(let url): SWAddFolderSheet(url: url).environmentObject(m)
            case .addProvider: SWAddProviderSheet().environmentObject(m)
            }
        }
    }
}

struct SWSidebar: View {
    @EnvironmentObject var m: SWModel
    @FocusState private var searching: Bool

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 6) {
                Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                TextField("Search settings", text: $m.query).textFieldStyle(.plain).focused($searching)
                if !m.query.isEmpty {
                    Button { m.query = "" } label: { Image(systemName: "xmark.circle.fill") }.buttonStyle(.plain).foregroundStyle(.secondary)
                }
            }
            .padding(.horizontal, 10).padding(.vertical, 7)
            .background(RoundedRectangle(cornerRadius: 8).fill(Color(nsColor: .quaternaryLabelColor).opacity(0.35)))
            .padding(.horizontal, 12).padding(.top, 14).padding(.bottom, 8)

            ScrollView {
                VStack(alignment: .leading, spacing: 2) {
                    if m.query.trimmingCharacters(in: .whitespaces).isEmpty {
                        ForEach(m.sectionsShown) { s in SWSidebarRow(section: s) }
                    } else {
                        SWSearchResults()
                    }
                }
                .padding(.horizontal, 8).padding(.bottom, 8)
            }

            if m.classicOn {                  // off: no Classic Settings entry here (ClassicUI.swift)
                Divider()
                VStack(alignment: .leading, spacing: 4) {
                    Button { m.openClassic?(nil) } label: {
                        Label("Classic Settings…", systemImage: "rectangle.on.rectangle")
                    }
                    .buttonStyle(.link).font(.callout)
                    Text("The old web settings, until this window is complete.").font(.caption2).foregroundStyle(.secondary)
                }
                .padding(12).frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .background(.regularMaterial)
    }
}

struct SWSidebarRow: View {
    @EnvironmentObject var m: SWModel
    let section: SWSection
    var body: some View {
        let on = m.section == section
        Button { m.setSection(section) } label: {
            HStack(spacing: 10) {
                Image(systemName: section.symbol)
                    .font(.system(size: 12, weight: .semibold)).foregroundStyle(.white)
                    .frame(width: 24, height: 24)
                    .background(RoundedRectangle(cornerRadius: 6).fill(section.tint.gradient))
                Text(section.title).foregroundStyle(on ? Color.white : Color.primary)
                Spacer(minLength: 4)
                if let n = badge { Text(n).font(.caption).foregroundStyle(on ? Color.white.opacity(0.85) : Color.secondary) }
            }
            .padding(.horizontal, 8).padding(.vertical, 5)
            .background(RoundedRectangle(cornerRadius: 8).fill(on ? Color.accentColor : .clear))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private var badge: String? {
        switch section {
        case .folders: return m.collections.isEmpty ? nil : "\(m.collections.count)"
        case .groups: return m.groupRows.isEmpty ? nil : "\(m.groupRows.count)"
        case .favourites: return m.favourites.isEmpty ? nil : "\(m.favourites.count)"
        default: return nil
        }
    }
}

struct SWSearchResults: View {
    @EnvironmentObject var m: SWModel
    var body: some View {
        let hits = SWIndex.matches(m.query).filter { m.sectionsShown.contains($0.section) }
        if hits.isEmpty {
            Text("Nothing in Settings matches “\(m.query)”.").font(.callout).foregroundStyle(.secondary).padding(10)
        } else {
            ForEach(SWSection.allCases.filter { s in hits.contains { $0.section == s } }) { s in
                Text(s.title.uppercased()).font(.system(size: 10, weight: .semibold)).foregroundStyle(.secondary)
                    .padding(.horizontal, 8).padding(.top, 8)
                ForEach(hits.filter { $0.section == s }) { h in
                    Button { m.reveal(h) } label: {
                        HStack { Image(systemName: s.symbol).foregroundStyle(s.tint).frame(width: 18); Text(h.title).lineLimit(2).multilineTextAlignment(.leading); Spacer(minLength: 0) }
                            .padding(.horizontal, 8).padding(.vertical, 4).contentShape(Rectangle())
                    }.buttonStyle(.plain)
                }
            }
        }
    }
}

struct SWDetail: View {
    @EnvironmentObject var m: SWModel
    var body: some View {
        VStack(spacing: 0) {
            SWHeader()
            Divider()
            if m.statusState == false {
                SWNote(tone: .bad, text: "The search service is not answering, so these settings cannot be read or changed right now.",
                       actions: [SWAction(title: "Try Again") { Task { await m.refreshStatus(); await m.refreshSettings(force: true); await m.refreshCollections(force: true) } },
                                 SWAction(title: "Copy the start command") { swCopy("mse service install"); m.say("Copied: mse service install. Run it in Terminal.") }])
                    .padding(16)
            }
            content.frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .overlay(alignment: .bottom) {
            if let n = m.notice {
                Text(n).font(.callout).padding(.horizontal, 14).padding(.vertical, 9)
                    .background(Capsule().fill(.regularMaterial)).overlay(Capsule().strokeBorder(Color(nsColor: .separatorColor)))
                    .shadow(radius: 4, y: 1).padding(18).transition(.move(edge: .bottom).combined(with: .opacity))
            }
        }
        .animation(.easeOut(duration: 0.2), value: m.notice)
    }

    @ViewBuilder private var content: some View {
        switch m.section {
        case .folders: SWFoldersView()
        case .groups: SWGroupsView()
        case .search: SWSearchSettingsView()
        case .indexing: SWIndexingView()          // SettingsIndexing.swift
        case .schedule: SWScheduleView()
        case .ask: SWAskView()
        case .wholeMac: SWWholeMacView()
        case .wholeDisk: SWWholeDiskView()      // SettingsWholeDisk.swift
        case .favourites: SWFavouritesView()
        case .agents: SWAgentsView()
        case .privacy: SWPrivacyView()
        case .macApp: SWMacAppView()
        case .advanced: SWAdvancedView()          // SettingsAdvanced.swift
        case .about: SWAboutView()
        }
    }
}

struct SWHeader: View {
    @EnvironmentObject var m: SWModel
    var body: some View {
        HStack(alignment: .center, spacing: 14) {
            Image(systemName: m.section.symbol)
                .font(.system(size: 17, weight: .semibold)).foregroundStyle(.white)
                .frame(width: 38, height: 38)
                .background(RoundedRectangle(cornerRadius: 9).fill(m.section.tint.gradient))
            VStack(alignment: .leading, spacing: 2) {
                Text(m.section.title).font(.system(size: 20, weight: .semibold))
                Text(m.section.blurb).font(.callout).foregroundStyle(.secondary).lineLimit(2)
            }
            Spacer(minLength: 8)
            if m.savedAt != nil {
                Label("Saved", systemImage: "checkmark.circle.fill").font(.callout).foregroundStyle(.green)
                    .transition(.opacity)
            }
            headerActions
        }
        .padding(.horizontal, 24).padding(.vertical, 14)
        .animation(.easeOut(duration: 0.2), value: m.savedAt)
    }

    @ViewBuilder private var headerActions: some View {
        switch m.section {
        case .folders:
            HStack {
                Button("Sync All Now") { m.syncAll() }
                Button { SWFolderPicker.choose(from: m) } label: { Label("Add Folder…", systemImage: "plus") }.buttonStyle(.borderedProminent)
            }
        case .groups:
            Button { GroupEditorSheet.present(on: m.window) { _ in Task { await m.refreshCollections(force: true) } } } label: { Label("New Group…", systemImage: "plus") }
                .buttonStyle(.borderedProminent)
        case .schedule:
            Button("Sync All Now") { m.syncAll() }
        case .wholeMac:
            Button("Rescan Now") { Task { await m.rescanWhole() } }
        default: EmptyView()
        }
    }
}

enum SWFolderPicker {
    /// The native folder picker (the service never opens one for us); the chosen folder goes to the "Add a folder" sheet.
    @MainActor static func choose(from m: SWModel) {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.canCreateDirectories = false
        panel.prompt = "Choose"
        panel.message = "Choose a folder for MySearch to index. Your files are only read, never changed."
        let done: (NSApplication.ModalResponse) -> Void = { r in
            if r == .OK, let url = panel.url { m.sheet = .addFolder(url) }
        }
        if let w = m.window { panel.beginSheetModal(for: w, completionHandler: done) } else { done(panel.runModal()) }
    }

    /// Several folders at once (Whole-Mac roots and exclusions).
    @MainActor static func chooseMany(from m: SWModel, message: String, prompt: String, _ done: @escaping ([URL]) -> Void) {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = true
        panel.prompt = prompt
        panel.message = message
        let handler: (NSApplication.ModalResponse) -> Void = { r in if r == .OK { done(panel.urls) } }
        if let w = m.window { panel.beginSheetModal(for: w, completionHandler: handler) } else { handler(panel.runModal()) }
    }
}
