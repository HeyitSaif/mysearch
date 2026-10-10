// Live indexing progress per folder. The service answers GET /api/index/progress (see mse/progress.py). The Settings
// folder list polls it every 1.5 seconds while that list is on screen (SwiftUI stops the task when the view goes away);
// the menu-bar item polls it on its own, faster while a folder is being indexed and slower while idle.
// Read-only: nothing here asks the service to start, stop or change anything.

import Foundation
import SwiftUI

/// One folder's progress as the service reports it.
struct IndexFolderProgress: Equatable {
    let name: String
    let job: String?                 // the kind of the running job; nil when the folder is idle
    let stage: String                // idle | scanning | extracting | ocr | transcribing | embedding | vocab | optimizing
    let done: Int
    let total: Int
    let percent: Double?
    let etaSeconds: Int?
    let errorCount: Int
    let recentErrors: [String]
    let pendingEmbed: Int
    let pendingHeavy: Int

    var isActive: Bool { job != nil }
    /// Shown next to the folder: while it is being indexed, or when it has errors to look at.
    var showsRow: Bool { isActive || errorCount > 0 }
}

enum IndexProgressAPI {
    /// The current progress by folder. Nil when the service does not answer, or is older than this app (404).
    static func fetch() async -> [String: IndexFolderProgress]? {
        guard let answer = try? await MSEClient.shared.get("/api/index/progress"),
              let obj = answer as? [String: Any] else { return nil }
        let rows = (obj["folders"] as? [String: Any]) ?? [:]
        var out: [String: IndexFolderProgress] = [:]
        for (name, value) in rows {
            guard let r = value as? [String: Any] else { continue }
            let errors = r.swO("errors")
            let pending = r.swO("pending")
            out[name] = IndexFolderProgress(
                name: name,
                job: r.swS("job"),
                stage: r.swS("stage") ?? "idle",
                done: r.swI("done") ?? 0,
                total: r.swI("total") ?? 0,
                percent: r.swD("percent"),
                etaSeconds: r.swI("eta_s"),
                errorCount: errors.swI("count") ?? 0,
                recentErrors: errors.swA("recent"),
                pendingEmbed: pending.swI("embed") ?? 0,
                pendingHeavy: pending.swI("heavy") ?? 0)
        }
        return out
    }

    /// The menu-bar text while any folder is being indexed: "Indexing <folder> 42%". Nil when none is.
    static func menuBarTitle() async -> String? {
        guard let folders = await fetch() else { return nil }
        guard let f = folders.values.filter({ $0.isActive }).sorted(by: { $0.name < $1.name }).first else { return nil }
        return IndexProgressText.menuBar(f)
    }
}

enum IndexProgressText {
    static func stage(_ s: String) -> String {
        ["scanning": "Checking files", "extracting": "Reading files", "ocr": "OCR and pictures",
         "transcribing": "Transcribing", "embedding": "Embedding", "vocab": "Building vocabulary",
         "optimizing": "Optimizing", "idle": "Idle"][s] ?? s
    }

    static func eta(_ seconds: Int) -> String {
        if seconds < 90 { return "\(max(1, seconds)) s" }
        if seconds < 3600 { return "\(Int((Double(seconds) / 60).rounded())) min" }
        return "\(seconds / 3600) h \(Int((Double(seconds % 3600) / 60).rounded())) min"
    }

    static func menuBar(_ f: IndexFolderProgress) -> String {
        "Indexing \(f.name)" + (f.percent.map { " \(Int($0.rounded()))%" } ?? "")
    }
}

/// The progress model. One shared instance: the Settings folder list polls it while it is visible, and the folder
/// cards read it.
@MainActor
final class IndexProgressModel: ObservableObject {
    static let shared = IndexProgressModel()

    @Published private(set) var folders: [String: IndexFolderProgress] = [:]

    /// Polls every 1.5 seconds until the calling task is cancelled (`.task` cancels it when the view disappears).
    func poll() async {
        while !Task.isCancelled {
            let next = await IndexProgressAPI.fetch() ?? [:]
            if next != folders { folders = next }
            try? await Task.sleep(nanoseconds: 1_500_000_000)
        }
    }
}

/// One folder's live line in the Folders settings: the stage, the count, the ETA, a bar while it runs, and an error
/// badge whose tooltip lists the most recent messages. Renders nothing when the folder is idle and has no errors.
struct IndexProgressRow: View {
    let entry: IndexFolderProgress?

    var body: some View {
        if let e = entry, e.showsRow {
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(IndexProgressText.stage(e.stage))
                        .font(.caption.weight(.medium))
                        .foregroundStyle(e.isActive ? Color.blue : Color.secondary)
                    if e.total > 0 {
                        Text("\(SW.num(e.done)) of \(SW.num(e.total))")
                            .font(.caption).foregroundStyle(.secondary).monospacedDigit()
                    }
                    if e.isActive, let eta = e.etaSeconds {
                        Text("about \(IndexProgressText.eta(eta))").font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer(minLength: 0)
                    if e.errorCount > 0 {
                        SWPill(text: SW.count(e.errorCount, "error"), tone: .warn)
                            .help(e.recentErrors.joined(separator: "\n"))
                    }
                }
                if e.isActive, e.total > 0 {
                    ProgressView(value: Double(min(e.done, e.total)), total: Double(e.total))
                        .progressViewStyle(.linear)
                }
            }
        }
    }
}
