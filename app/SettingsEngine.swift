// The native Settings window, part 4: Search (how results are found) and Schedule & Engine (night run, live limits,
// workers and memory, the job queue, search-quality learning).

import Cocoa
import SwiftUI

// MARK: - Search

struct SWSearchSettingsView: View {
    @EnvironmentObject var m: SWModel

    var body: some View {
        SWFormPage {
            Section {
                SWRow(id: "search.limit", title: "Results per search", detail: "What agents and the mse command get unless they ask for more. The app lists up to 40.", errorKey: "search.limit") {
                    SWNumberField(value: m.bindNumber("search", "limit", 20).wrappedValue, range: 1...100) { m.bindNumber("search", "limit", 20).wrappedValue = $0 }
                }
                SWRow(id: "search.bridge", title: "Translate between English and German",
                      detail: "Each folder's own vocabulary bridges the languages, so “invoice” also finds “Rechnung”.",
                      cost: "Costs a little disk for the vocabulary; no extra time per search.", errorKey: "translation.enabled") {
                    Toggle("", isOn: Binding(get: {
                        let t = m.settings.swO("translation"); return (t.swB("enabled") ?? false) && (t.swB("corpus_bridge") ?? false)
                    }, set: { on in m.patch("translation", ["enabled": on, "corpus_bridge": on], err: "translation.enabled") })).labelsHidden().toggleStyle(.switch)
                }
                SWRow(id: "search.rerank", title: "Re-check close calls",
                      detail: "When the top results are near-ties, a small local re-ranker reads them again. The app shows results at once and re-orders a moment later.",
                      cost: "About 0.3 s per close call, and the re-ranker uses memory while it is loaded.", errorKey: "search.rerank_enabled") {
                    Toggle("", isOn: m.bind("search", "rerank_enabled", false)).labelsHidden().toggleStyle(.switch)
                }
                SWRow(id: "search.hub", title: "Overview files rank lower",
                      detail: "SUMMARY, README, INDEX, TODO, CHANGELOG, MANIFEST and OVERVIEW files describe other documents. Lowering them puts the documents themselves first. ×1.00 leaves them alone.",
                      errorKey: "search.hub_penalty") {
                    HStack {
                        Slider(value: m.bind("search", "hub_penalty", 0.6, delay: 0.5), in: 0.3...1.0, step: 0.05).frame(width: 170)
                        Text(String(format: "×%.2f", m.settings.swO("search").swD("hub_penalty") ?? 0.6)).monospacedDigit().frame(width: 52, alignment: .trailing)
                    }
                }
            } header: { Text("Finding and ordering") }
            Section {
                Text("These apply to the next search, in the app, in the mse command and for agents.").font(.callout).foregroundStyle(.secondary)
            }
        }
    }
}

// MARK: - Schedule & engine

struct SWScheduleView: View {
    @EnvironmentObject var m: SWModel

    private var nightDate: Binding<Date> {
        Binding(get: {
            let t = m.settings.swO("schedule").swS("nightly_at") ?? "02:30"
            let p = t.split(separator: ":").compactMap { Int($0) }
            return Calendar.current.date(bySettingHour: p.first ?? 2, minute: p.count > 1 ? p[1] : 30, second: 0, of: Date()) ?? Date()
        }, set: { d in
            let f = DateFormatter(); f.locale = Locale(identifier: "en_US_POSIX"); f.dateFormat = "HH:mm"
            m.patch("schedule", ["nightly_at": f.string(from: d)], err: "schedule.nightly_at", delay: 0.8)
        })
    }

    var body: some View {
        SWFormPage {
            Section {
                SWRow(id: "sched.night", title: "Night run", detail: "OCR, PDF page images, audio, video and new embeddings happen at this time.", errorKey: m.errors["schedule.nightly_enabled"] != nil ? "schedule.nightly_enabled" : "schedule.nightly_at") {
                    HStack(spacing: 10) {
                        DatePicker("", selection: nightDate, displayedComponents: .hourAndMinute).labelsHidden().disabled(m.settings.swO("schedule").swB("nightly_enabled") == false)
                        Toggle("", isOn: m.bind("schedule", "nightly_enabled", true)).labelsHidden().toggleStyle(.switch)
                    }
                }
                if m.settings.swO("schedule").swB("nightly_enabled") == false {
                    SWNote(tone: .warn, text: "The night run is off, so OCR, audio, video and large embedding batches wait until you press Sync Now.",
                           actions: [SWAction(title: "Turn On Night Run") { m.patch("schedule", ["nightly_enabled": true], err: "schedule.nightly_enabled") },
                                     SWAction(title: "Sync All Now") { m.syncAll() }])
                }
                SWRow(id: "sched.ac", title: "Only when plugged in", detail: "The night run waits for power so it never drains the battery.",
                      cost: "The heavy work can run the fans for a while; that is why it waits for the charger.", errorKey: "schedule.only_on_ac_power") {
                    Toggle("", isOn: m.bind("schedule", "only_on_ac_power", true)).labelsHidden().toggleStyle(.switch)
                }
            } header: { Text("Night run") }
            Section {
                SWRow(id: "sched.scan", title: "Check folders for changes every", detail: "A safety net; file events already index changes within seconds.", errorKey: "schedule.scan_every_min") {
                    SWNumberField(value: m.settings.swO("schedule").swI("scan_every_min") ?? 60, range: 2...240, step: 5, unit: "minutes") {
                        m.patch("schedule", ["scan_every_min": $0], err: "schedule.scan_every_min", delay: 0.6)
                    }
                }
                SWRow(id: "sched.live", title: "Embed right away when fewer than", detail: "Larger batches wait for the night run, so a big copy does not slow your Mac down in the day.", errorKey: "schedule.live_embed_limit") {
                    SWNumberField(value: m.settings.swO("schedule").swI("live_embed_limit") ?? 200, range: 0...5000, step: 25, unit: "pieces changed") {
                        m.patch("schedule", ["live_embed_limit": $0], err: "schedule.live_embed_limit", delay: 0.6)
                    }
                }
                SWRow(id: "sched.idle", title: "Unload the models after", detail: "Frees their memory. The next search reloads the text model in about a second.",
                      cost: m.workerMemoryNote, errorKey: "worker.idle_unload_min") {
                    SWNumberField(value: m.settings.swO("worker").swI("idle_unload_min") ?? 10, range: 1...240, step: 5, unit: "idle minutes") {
                        m.patch("worker", ["idle_unload_min": $0], err: "worker.idle_unload_min", delay: 0.6)
                    }
                }
            } header: { Text("Live indexing and memory") }
            SWJobsSection()
            SWEngineSection()
            SWLearningSection()
        }
    }
}

extension SWModel {
    var workers: SWJSON { status.swO("workers") }
    var workerMemoryNote: String? {
        let total = workers.values.compactMap { ($0 as? SWJSON)?.swD("rss_mb") }.reduce(0, +)
        return total > 0 ? "Models in memory now: \(Int(total)) MB." : "No model is loaded right now."
    }
}

struct SWJobsSection: View {
    @EnvironmentObject var m: SWModel
    var body: some View {
        Section {
            if let cur = m.currentJob { SWJobLine(job: cur, showStop: true) }
            ForEach(Array(m.jobs.swR("queued").enumerated()), id: \.offset) { _, j in
                Text("Next: \(SWJobText.kind(j.swS("kind"))) \(j.swS("collection") ?? "")").foregroundStyle(.secondary)
            }
            if m.currentJob == nil && m.jobs.swR("queued").isEmpty {
                if let last = m.jobs.swR("recent").first {
                    Text("Last: \(SWJobText.kind(last.swS("kind"))) \(last.swS("collection") ?? "") — \(last.swS("status") ?? "")\((last.swS("error")).map { ": \($0)" } ?? "") · \(SW.ago(last.swD("finished_at")))")
                        .font(.callout).foregroundStyle(.secondary)
                } else { Text("Nothing is running.").foregroundStyle(.secondary) }
            }
            let night = m.jobs.swO("nightly")
            LabeledContent("Night run") {
                Text(night.swB("enabled") == true ? "at \(night.swS("at") ?? "?")\(night.swS("last_run").map { " · last \($0)" } ?? "")" : "off").foregroundStyle(.secondary)
            }
            if let n = m.jobs.swI("next_scan_in_s") { LabeledContent("Next change check") { Text("in \(max(1, n / 60)) min").foregroundStyle(.secondary) } }
            HStack { Button("Sync All Now") { m.syncAll() }; Spacer() }
        } header: { Text("Jobs") }
        .id("sched.jobs")
    }
}

struct SWEngineSection: View {
    @EnvironmentObject var m: SWModel
    var body: some View {
        let st = m.status
        let idle = st.swI("idle_unload_min") ?? 10
        Section {
            workerRow("Search model (text)", m.workers.swO("text"), asleep: "Sleeping · wakes in about 1 s when you search", idle: idle)
            workerRow("Image, page, audio & video model", m.workers.swO("full"), asleep: "Sleeping · wakes for new photos, scans and media, Sync Now and the night run", idle: idle)
            if st.swB("asr_present") == true { workerRow("Speech to text (Whisper)", m.workers.swO("asr"), asleep: "Sleeping · wakes for new recordings", idle: idle) }
            else { LabeledContent("Speech to text (Whisper)") { Text("Not installed").foregroundStyle(.secondary) } }
            if st.swB("rerank_enabled") == true { workerRow("Re-ranker", m.workers.swO("rerank"), asleep: "On for close calls · wakes when the top results are near-ties", idle: idle) }
            else { LabeledContent("Re-ranker") { Text("Off").foregroundStyle(.secondary) } }
            LabeledContent("Service") { Text("\(Int(st.swD("daemon_rss_mb") ?? 0)) MB · running \((st.swI("uptime_s") ?? 0) / 60) min").foregroundStyle(.secondary) }
            let w = m.jobs.swO("watcher")
            LabeledContent("Live updates") {
                Text(w.swB("running") == true ? "On · \(w.swI("events") ?? 0) file changes seen" : "Off — checking on a timer instead").foregroundStyle(.secondary)
            }
            if st.swB("model_present") == false {
                SWNote(tone: .bad, text: "The search model is missing from disk, so new text cannot be embedded. Searching by words still works.",
                       actions: [SWAction(title: "Copy Repair Command") { swCopy("mse doctor"); m.say("Copied: mse doctor. Run it in Terminal.") }])
            }
        } header: { Text("Engine and memory") }
        .id("sched.engine")
    }

    private func workerRow(_ title: String, _ w: SWJSON, asleep: String, idle: Int) -> some View {
        LabeledContent(title) {
            Text(w.swB("running") == true ? "In use · \(Int(w.swD("rss_mb") ?? 0)) MB · frees itself after \(idle) min idle" : asleep)
                .foregroundStyle(.secondary).multilineTextAlignment(.trailing)
        }
    }
}

struct SWLearningSection: View {
    @EnvironmentObject var m: SWModel
    var body: some View {
        Section {
            if let ft = m.finetune ?? m.quality?.swO("finetune"), ft.swS("error") == nil {
                if let line = ft.swS("line") { Text(line).font(.callout) }
                if ft.swB("ready") == false, let gate = ft.swS("gate") { Text(gate).font(.caption).foregroundStyle(.secondary) }
                if ft.swB("enabled") == false {
                    SWNote(tone: .neutral, text: "Learning from favourites is off in the configuration.")
                }
                if let n = ft.swI("favourites"), n == 0 {
                    SWNote(tone: .neutral, text: "Star the result you wanted in a search (the ☆ in the results) and the re-ranker learns from it.",
                           actions: [SWAction(title: "See Favourites") { m.setSection(.favourites) }])
                }
            } else {
                Text("Learning status is not available from this service.").font(.callout).foregroundStyle(.secondary)
            }
            let entries = (m.quality?.swR("entries") ?? []).suffix(5).reversed()
            if !entries.isEmpty {
                Text("Recent tuning runs").font(.subheadline.weight(.semibold)).padding(.top, 4)
                ForEach(Array(entries.enumerated()), id: \.offset) { _, e in
                    HStack {
                        Text(String((e.swS("when") ?? "").prefix(16)).replacingOccurrences(of: "T", with: " ")).font(.system(size: 11, design: .monospaced))
                        Text(e.swS("kind") ?? "").font(.callout)
                        if let a = e.swS("action") { SWPill(text: a, tone: a == "applied" ? .ok : .neutral) }
                        Spacer()
                        if let q = e.swD("weighted_mrr") { Text(String(format: "quality %.3f", q)).font(.caption).foregroundStyle(.secondary) }
                    }
                    if let r = e.swS("reason"), !r.isEmpty { Text(r).font(.caption).foregroundStyle(.secondary).lineLimit(2) }
                }
            }
        } header: { Text("Search quality and learning") }
        .id("sched.learning")
    }
}
