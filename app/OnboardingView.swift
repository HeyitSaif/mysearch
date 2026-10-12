// The setup assistant (first run), part 2: the window's content. Same look as Settings (SWCard, SWNote, SWPill, SWChip): a
// sidebar of steps like Finder's, a header with the step's icon, the step's page, and a footer with Back and Continue.
// Welcome > Folders > Models > Full Disk Access > Done.

import Cocoa
import SwiftUI

struct OnboardingView: View {
    @ObservedObject var m: OnboardingModel

    var body: some View {
        HStack(spacing: 0) {
            OBSidebar(m: m).frame(width: 206)
            Divider()
            VStack(spacing: 0) {
                OBHeader(step: m.step)
                Divider()
                ScrollView {
                    VStack(alignment: .leading, spacing: 14) { page }
                        .padding(.horizontal, 28).padding(.vertical, 20)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                Divider()
                OBFooter(m: m)
            }
        }
        .frame(width: 780, height: 560)
        .background(Color(nsColor: .windowBackgroundColor))
    }

    @ViewBuilder private var page: some View {
        switch m.step {
        case .welcome: OBWelcomePage(m: m)
        case .folders: OBFoldersPage(m: m)
        case .models: OBModelsPage(m: m)
        case .access: OBAccessPage(m: m)
        case .done: OBDonePage(m: m)
        }
    }
}

// MARK: - frame

private struct OBSidebar: View {
    @ObservedObject var m: OnboardingModel

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 10) {
                Image(nsImage: NSApp.applicationIconImage).resizable().frame(width: 34, height: 34)
                Text("MySearch").font(.system(size: 17, weight: .semibold))
            }
            .padding(.horizontal, 16).padding(.top, 22).padding(.bottom, 16)
            VStack(alignment: .leading, spacing: 2) {
                ForEach(OBStep.allCases) { s in row(s) }
            }
            .padding(.horizontal, 8)
            Spacer()
            Text("Everything MySearch does stays on this Mac.").font(.caption).foregroundStyle(.secondary)
                .padding(16).fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(Color(nsColor: .controlBackgroundColor))
    }

    private func row(_ s: OBStep) -> some View {
        let on = m.step == s
        return Button { m.step = s } label: {
            HStack(spacing: 10) {
                Image(systemName: s.symbol)
                    .font(.system(size: 12, weight: .semibold)).foregroundStyle(.white)
                    .frame(width: 24, height: 24)
                    .background(RoundedRectangle(cornerRadius: 6).fill(s.tint.gradient))
                Text(s.title).foregroundStyle(on ? Color.white : Color.primary)
                Spacer(minLength: 4)
                if isDone(s) && !on { Image(systemName: "checkmark.circle.fill").foregroundStyle(.green) }
            }
            .padding(.horizontal, 8).padding(.vertical, 5)
            .background(RoundedRectangle(cornerRadius: 8).fill(on ? Color.accentColor : .clear))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private func isDone(_ s: OBStep) -> Bool {
        switch s {
        case .welcome: if case .running = m.service { return true } else { return false }
        case .folders: return !m.folders.isEmpty
        case .models: return m.minimalReady
        case .access: return m.fullDiskAccess == "granted"
        case .done: return false
        }
    }
}

private struct OBHeader: View {
    let step: OBStep

    private var blurb: String {
        switch step {
        case .welcome: return "A private search engine for your own folders."
        case .folders: return "Choose what MySearch should index. Your files are only read, never changed."
        case .models: return "The search model runs on this Mac. It is downloaded once."
        case .access: return "macOS keeps some folders private until you allow it."
        case .done: return "MySearch is ready."
        }
    }

    var body: some View {
        HStack(spacing: 14) {
            Image(systemName: step.symbol)
                .font(.system(size: 17, weight: .semibold)).foregroundStyle(.white)
                .frame(width: 38, height: 38)
                .background(RoundedRectangle(cornerRadius: 9).fill(step.tint.gradient))
            VStack(alignment: .leading, spacing: 2) {
                Text(step == .welcome ? "Welcome to MySearch" : step.title).font(.system(size: 20, weight: .semibold))
                Text(blurb).font(.callout).foregroundStyle(.secondary).lineLimit(2)
            }
            Spacer(minLength: 8)
        }
        .padding(.horizontal, 28).padding(.vertical, 14)
    }
}

private struct OBFooter: View {
    @ObservedObject var m: OnboardingModel

    var body: some View {
        HStack(spacing: 10) {
            if m.step != .done { Button("Skip Setup") { m.onSkip?() }.buttonStyle(.link).font(.callout) }
            if let n = m.notice {
                Text(n).font(.caption).foregroundStyle(.secondary).lineLimit(2).frame(maxWidth: 260, alignment: .leading)
            }
            Spacer()
            if m.step != .welcome && m.step != .done { Button("Back") { m.back() } }
            Button(m.step == .done ? "Open MySearch" : "Continue") { m.next() }
                .buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction).disabled(!m.canContinue)
        }
        .padding(.horizontal, 28).padding(.vertical, 12)
    }
}

// MARK: - pages

private struct OBPoint: View {
    let symbol: String, title: String, text: String
    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: symbol).font(.system(size: 15)).foregroundStyle(Color.accentColor).frame(width: 24)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.system(size: 13, weight: .semibold))
                Text(text).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

private struct OBWelcomePage: View {
    @ObservedObject var m: OnboardingModel

    var body: some View {
        Text("MySearch finds files by words and by meaning, in English and German: documents, PDFs including scans, Office files, e-mail, pictures and recordings. Setup takes a few minutes, plus the model download.")
            .fixedSize(horizontal: false, vertical: true)
        SWCard {
            VStack(alignment: .leading, spacing: 12) {
                OBPoint(symbol: "lock.fill", title: "It stays on your Mac",
                        text: "Indexing, search and the models all run here. Files are read in place and never changed, and searches are not logged.")
                OBPoint(symbol: "arrow.down.circle", title: "Three things can go online",
                        text: "The model download you start in this assistant, a once-a-day check for a newer version (switch it off in Settings), and cloud answers only if you turn Ask on and choose a cloud model.")
                OBPoint(symbol: "slider.horizontal.3", title: "You stay in control",
                        text: "Add or remove folders, pause indexing and see everything MySearch does in Settings.")
            }.padding(14)
        }
        SWCard {
            HStack(spacing: 12) {
                Image(systemName: "gearshape.2.fill").font(.system(size: 18)).foregroundStyle(.secondary)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Background service").font(.system(size: 13, weight: .semibold))
                    Text(serviceText).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 8)
                servicePill
                if showStart {
                    Button("Start MySearch") { m.startService() }.buttonStyle(.borderedProminent).controlSize(.small)
                }
            }.padding(14)
        }
        if case .failed(let why) = m.service {
            SWNote(tone: .bad, text: why, actions: [SWAction(title: "Try Again") { Task { await m.refreshService() } }])
        }
    }

    private var showStart: Bool {
        switch m.service {
        case .notSetUp: return true
        case .failed: return OnboardingModel.bundledPython != nil && OnboardingModel.unstableLocation == nil
        default: return false
        }
    }

    private var serviceText: String {
        switch m.service {
        case .running(let v): return "Running\(v.isEmpty ? "" : " (version \(v))"). It starts when you log in and does the indexing quietly in the background."
        case .starting: return "Starting. macOS may show a “Background item added” notice: that is this."
        default: return "MySearch indexes and searches through a small service that runs in the background and starts when you log in. Start it once now."
        }
    }

    @ViewBuilder private var servicePill: some View {
        switch m.service {
        case .running: SWPill(text: "Running", tone: .ok)
        case .starting: SWPill(text: "Starting…", tone: .busy)
        case .notSetUp: SWPill(text: "Not set up", tone: .warn)
        case .failed: SWPill(text: "Not running", tone: .bad)
        case .unknown: SWPill(text: "Checking…", tone: .neutral)
        }
    }
}

private struct OBFoldersPage: View {
    @ObservedObject var m: OnboardingModel

    var body: some View {
        Text("Each folder gets its own index. Add the ones you want searchable now; you can add and remove folders later in Settings.")
            .fixedSize(horizontal: false, vertical: true)
        SWCard {
            VStack(alignment: .leading, spacing: 0) {
                if m.folders.isEmpty {
                    VStack(spacing: 8) {
                        Image(systemName: "folder.badge.plus").font(.system(size: 30)).foregroundStyle(.secondary)
                        Text("No folders yet").font(.headline)
                        Text("Choose a folder, or pick one of the usual places below.").foregroundStyle(.secondary)
                    }.frame(maxWidth: .infinity).padding(24)
                } else {
                    ForEach(Array(m.folders.enumerated()), id: \.element.id) { i, f in
                        HStack(spacing: 10) {
                            Image(systemName: "folder.fill").foregroundStyle(.blue)
                            VStack(alignment: .leading, spacing: 1) {
                                Text(f.name).font(.callout.weight(.medium))
                                Text(SW.tilde(f.path)).font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                            }
                            Spacer()
                            Text(f.files > 0 ? SW.count(f.files, "file") : "starting…").font(.caption).foregroundStyle(.secondary)
                        }
                        .padding(.horizontal, 14).padding(.vertical, 8)
                        if i < m.folders.count - 1 { Divider() }
                    }
                }
            }
        }
        HStack(spacing: 8) {
            Button { m.chooseFolder() } label: { Label("Add Folder…", systemImage: "plus") }.buttonStyle(.borderedProminent)
                .disabled(m.addingFolder)
            ForEach(m.suggestions, id: \.path) { u in
                Button(u.lastPathComponent) { m.addFolder(u) }
            }
            Spacer()
        }
        if m.folders.isEmpty {
            Text("You can continue without a folder and add one later.").font(.caption).foregroundStyle(.secondary)
        } else {
            Text("Text becomes searchable within minutes. OCR, audio and video are done at night, or with Sync Now.")
                .font(.caption).foregroundStyle(.secondary)
        }
    }
}

private struct OBModelsPage: View {
    @ObservedObject var m: OnboardingModel

    var body: some View {
        Text("MySearch needs a language model to understand meaning. Pick a set; it downloads from Hugging Face, once, and stays on this Mac.")
            .fixedSize(horizontal: false, vertical: true)
        HStack(alignment: .top, spacing: 12) {
            tierCard("minimal", title: "Minimal",
                     text: "Text search, plus pictures and PDF pages. Enough for most people.")
            tierCard("full", title: "Full",
                     text: "Adds speech to text for audio and video, and a small local model for Ask.")
        }
        progressCard
        if let t = m.tiers[m.tier], !t.manual.isEmpty, m.tier == "full" {
            Text("The optional re-ranker cannot be downloaded yet. Search works without it.").font(.caption).foregroundStyle(.secondary)
        }
        if let free = m.freeBytes {
            Text("Free space on this Mac: \(SW.bytes(free)).").font(.caption).foregroundStyle(.secondary)
        }
    }

    private func sizeText(_ t: OBTier?) -> String {
        guard let t else { return "" }
        if t.ready { return "Installed" }
        return "\(SW.bytes(t.needBytes)) to download"
    }

    private func tierCard(_ id: String, title: String, text: String) -> some View {
        let on = m.tier == id
        let t = m.tiers[id]
        return Button { if !m.job.running { m.tier = id } } label: {
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Image(systemName: on ? "largecircle.fill.circle" : "circle").foregroundStyle(on ? Color.accentColor : Color.secondary)
                    Text(title).font(.system(size: 14, weight: .semibold))
                    Spacer()
                    if t?.ready == true { SWPill(text: "Installed", tone: .ok) }
                }
                Text(text).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
                Text(sizeText(t)).font(.caption.weight(.medium))
            }
            .padding(14).frame(maxWidth: .infinity, minHeight: 112, alignment: .topLeading)
            .background(RoundedRectangle(cornerRadius: 12).fill(Color(nsColor: .controlBackgroundColor)))
            .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(on ? Color.accentColor : Color(nsColor: .separatorColor).opacity(0.7), lineWidth: on ? 2 : 1))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private var chosenReady: Bool { m.tiers[m.tier]?.ready ?? false }

    @ViewBuilder private var progressCard: some View {
        let j = m.job
        SWCard {
            VStack(alignment: .leading, spacing: 10) {
                switch j.state {
                case "running":
                    ProgressView(value: min(j.doneBytes, max(j.totalBytes, 1)), total: max(j.totalBytes, 1))
                    HStack {
                        Text(progressLine(j)).font(.callout).monospacedDigit()
                        Spacer()
                        Text("\(Int(j.percent)) %").font(.callout.weight(.medium)).monospacedDigit()
                    }
                    ForEach(Array(j.models.enumerated()), id: \.offset) { _, row in
                        HStack(spacing: 8) {
                            Image(systemName: row.state == "installed" ? "checkmark.circle.fill" : row.state == "downloading" ? "arrow.down.circle" : "circle")
                                .foregroundStyle(row.state == "installed" ? Color.green : Color.secondary)
                            Text(row.folder).font(.system(size: 12, design: .monospaced))
                            Spacer()
                            Text(row.state == "downloading" ? "downloading" : row.state).font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    HStack { Button("Stop") { m.cancelDownload() }.controlSize(.small); Spacer() }
                case "error":
                    SWNote(tone: .bad, text: j.error ?? "The download stopped.",
                           actions: [SWAction(title: "Try Again") { m.startDownload() }])
                    Text("Finished files are kept; trying again resumes.").font(.caption).foregroundStyle(.secondary)
                case "cancelled":
                    Text("Stopped. Download again to resume where it left off.").font(.callout).foregroundStyle(.secondary)
                    HStack { Button("Resume Download") { m.startDownload() }.buttonStyle(.borderedProminent).controlSize(.small); Spacer() }
                default:
                    if chosenReady {
                        Label("This set is installed.", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
                    } else {
                        Text("Nothing is downloaded until you press the button.").font(.callout).foregroundStyle(.secondary)
                        HStack {
                            Button { m.startDownload() } label: {
                                Label("Download \(m.tier == "full" ? "Full" : "Minimal") Set", systemImage: "arrow.down.circle.fill")
                            }.buttonStyle(.borderedProminent).disabled(!isRunning)
                            Spacer()
                        }
                    }
                }
            }.padding(14)
        }
    }

    private var isRunning: Bool { if case .running = m.service { return true } else { return false } }

    private func progressLine(_ j: OBJob) -> String {
        var s = "\(SW.bytes(min(j.doneBytes, j.totalBytes))) of \(SW.bytes(j.totalBytes))"
        if let r = j.rateBps, r > 0 { s += " · \(SW.bytes(r))/s" }
        if let e = j.etaSeconds { s += " · " + (e < 90 ? "about \(max(1, Int(e))) s left" : "about \(Int((e / 60).rounded())) min left") }
        return s
    }
}

private struct OBAccessPage: View {
    @ObservedObject var m: OnboardingModel

    var body: some View {
        Text("macOS keeps Desktop, Documents, Downloads, Mail and a few other places private. Without Full Disk Access, MySearch cannot read them when it indexes in the background, and those files would not be found.")
            .fixedSize(horizontal: false, vertical: true)
        SWCard {
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    Text("Full Disk Access for MySearch").font(.system(size: 13, weight: .semibold))
                    Spacer()
                    switch m.fullDiskAccess {
                    case "granted": SWPill(text: "Allowed", tone: .ok)
                    case "not_granted": SWPill(text: "Not allowed yet", tone: .warn)
                    default: SWPill(text: "Unknown", tone: .neutral)
                    }
                }
                step("1", "Open Privacy & Security, then Full Disk Access.")
                step("2", "Switch MySearch on. If it is not listed, click + and choose MySearch in Applications, or drag it in from the Finder window the second button opens.")
                step("3", "macOS may ask you to quit and reopen MySearch. Do that, then come back here.")
                HStack {
                    Button("Open Privacy & Security") { m.openFullDiskAccess() }.buttonStyle(.borderedProminent)
                    Button("Show MySearch in Finder") { m.revealApp() }
                    Spacer()
                    Button("Check Again") { Task { await m.refreshStatus() } }.controlSize(.small)
                }
            }.padding(14)
        }
        SWNote(tone: .neutral, text: "This is optional. If you only index folders macOS does not protect, such as a projects folder, you can skip it. Settings > Whole-Mac Search shows the state later.")
    }

    private func step(_ n: String, _ text: String) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Text(n).font(.system(size: 11, weight: .bold)).foregroundStyle(.white)
                .frame(width: 18, height: 18).background(Circle().fill(Color.secondary))
            Text(text).font(.callout).fixedSize(horizontal: false, vertical: true)
        }
    }
}

private struct OBDonePage: View {
    @ObservedObject var m: OnboardingModel

    var body: some View {
        Text("MySearch is set up. It indexes in the background; the menu-bar icon shows progress while it works.")
            .fixedSize(horizontal: false, vertical: true)
        SWCard {
            VStack(alignment: .leading, spacing: 10) {
                line(ok: serviceRunning, "Background service", serviceRunning ? "running" : "not running")
                Divider()
                line(ok: !m.folders.isEmpty, "Folders", m.folders.isEmpty ? "none yet: add one in Settings" : SW.count(m.folders.count, "folder"))
                Divider()
                line(ok: m.minimalReady, "Search model", m.minimalReady ? "installed" : "not downloaded yet: search by words still works")
                Divider()
                line(ok: m.fullDiskAccess == "granted", "Full Disk Access", m.fullDiskAccess == "granted" ? "allowed" : "not allowed: protected folders will not be read")
            }.padding(14)
        }
        Text("Search with ⌥Space from any app, or open MySearch from the menu bar. Reopen this assistant any time from Settings > Mac App.")
            .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
    }

    private var serviceRunning: Bool { if case .running = m.service { return true } else { return false } }

    private func line(ok: Bool, _ title: String, _ value: String) -> some View {
        HStack(spacing: 10) {
            Image(systemName: ok ? "checkmark.circle.fill" : "exclamationmark.circle").foregroundStyle(ok ? Color.green : Color.orange)
            Text(title)
            Spacer()
            Text(value).foregroundStyle(.secondary)
        }
    }
}
