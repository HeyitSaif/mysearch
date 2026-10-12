// Off-screen renders of Quick Search, for the checks (--snapshot, Snapshot.swift):
//   --state quick-search        the query "invoice" against the fixture's stub service (names and folder matches)
//   --state quick-search-empty  the empty query: the recent searches and files (fixture seeds, no service call)
//   --state quick-search-dark   the same as quick-search, in dark appearance
//   --state quick-search-disk   the query "zebra" in Whole Mac scope: the On this Mac section from the --disk stub
// The panel is built at an off-screen frame and is never ordered in. Its content is rendered with cacheDisplay onto a
// fixed fictional backdrop (the panel's material and shadow have nothing behind them off-screen). Environment switches,
// for checks only:
//   MSE_QS_QUERY=<text>   the query for quick-search (default "invoice"); no-result and similar renders use it
//   MSE_QS_BENCH=<n>      n typed queries through the keystroke path; prints the timings and writes no picture

import Cocoa

extension SnapshotOptions.State {
    /// quick-search, quick-search-empty and quick-search-dark: the native Quick Search panel (QuickSearchPanel.swift).
    var isQuickSearch: Bool {
        switch self {
        case .quickSearch, .quickSearchEmpty, .quickSearchDark, .quickSearchDisk: return true
        default: return false
        }
    }
}

@MainActor
func runQuickSearchSnapshot(_ o: SnapshotOptions) {
    let dark = o.dark || o.state == .quickSearchDark
    let appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
    NSApp.appearance = appearance
    // the whole-disk settings the --disk stub answers with, held in memory before the panel exists (as search-mac-disk does)
    if o.state == .quickSearchDisk { WholeDiskStore.shared.apply(WholeDiskSettings.snapshotFixture) }
    let controller = QuickSearchController()
    controller.placeOffscreen(appearance: appearance)
    controller.start()

    let env = ProcessInfo.processInfo.environment
    if let n = env["MSE_QS_BENCH"].flatMap(Int.init), n > 0 {
        runQuickSearchBench(controller, count: n)
        return
    }
    let fallbackQuery = o.state == .quickSearchDisk ? "zebra" : "invoice"
    let query = o.state == .quickSearchEmpty ? "" : (env["MSE_QS_QUERY"] ?? fallbackQuery)
    Task { @MainActor in
        controller.model.setQuery(query, typed: false, force: true)
        await waitForSettled(controller.model)
        let keys = (env["MSE_QS_KEYS"] ?? "").split(separator: " ").map(String.init)
        if !keys.isEmpty { await sendScriptedKeys(keys, to: controller) }
        controller.view.update()
        try? await Task.sleep(nanoseconds: 200_000_000)
        writeQuickSearchPicture(controller, output: o.output, dark: dark)
    }
}

/// Waits until the model has no request in flight (the names, the folder matches and their re-ranking), up to 15 s.
@MainActor
private func waitForSettled(_ model: QuickSearchModel) async {
    let start = qsNow()
    while !model.isSettled && qsNow() - start < 15 {
        try? await Task.sleep(nanoseconds: 20_000_000)
    }
    if !model.isSettled { fputs("MySearch snapshot: quick search did not settle within 15 s\n", stderr) }
}

@MainActor
private func writeQuickSearchPicture(_ controller: QuickSearchController, output: String, dark: Bool) {
    guard let content = controller.panel.contentView else { snapshotFail("the quick search panel has no content view") }
    content.layoutSubtreeIfNeeded()
    guard let rep = content.bitmapImageRepForCachingDisplay(in: content.bounds) else {
        snapshotFail("could not create a bitmap for the quick search panel")
    }
    content.cacheDisplay(in: content.bounds, to: rep)

    let pad: CGFloat = 28
    let size = NSSize(width: content.bounds.width + 2 * pad, height: content.bounds.height + 2 * pad)
    let scale = CGFloat(rep.pixelsWide) / max(1, content.bounds.width)
    guard let out = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int((size.width * scale).rounded()),
                                     pixelsHigh: Int((size.height * scale).rounded()), bitsPerSample: 8, samplesPerPixel: 4,
                                     hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)
    else {
        snapshotFail("could not create the picture of the quick search panel")
    }
    out.size = size                     // points first: the context then draws in points at the bitmap's pixel density
    guard let context = NSGraphicsContext(bitmapImageRep: out) else {
        snapshotFail("could not create a graphics context for the quick search picture")
    }
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = context
    let top = dark ? NSColor(srgbRed: 0.13, green: 0.15, blue: 0.20, alpha: 1) : NSColor(srgbRed: 0.90, green: 0.93, blue: 0.97, alpha: 1)
    let bottom = dark ? NSColor(srgbRed: 0.04, green: 0.05, blue: 0.08, alpha: 1) : NSColor(srgbRed: 0.62, green: 0.71, blue: 0.84, alpha: 1)
    NSGradient(starting: top, ending: bottom)?.draw(in: NSRect(origin: .zero, size: size), angle: -90)
    rep.draw(in: NSRect(x: pad, y: pad, width: content.bounds.width, height: content.bounds.height))
    NSGraphicsContext.restoreGraphicsState()

    guard let data = snapshotPNGData(out) else { snapshotFail("PNG encoding failed") }
    let url = URL(fileURLWithPath: output)
    try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
    do {
        try data.write(to: url)
    } catch {
        snapshotFail("could not write \(output)")
    }
    print(url.path)
    exit(0)
}

/// MSE_QS_KEYS=<tokens>: key presses sent to the panel as real key-down events (QuickPanel.sendEvent, the path a key
/// takes on screen), one after another, each followed by the printed selection. Tokens: up, down, tab, shift-tab, ctrl-1,
/// ctrl-2, ctrl-3, ctrl-4, cmd-1, cmd-2. Quick Look (space, cmd-y), opening (return, cmd-o) and Esc are left out of the checks:
/// they would put a window on the screen, open a file or close the panel.
@MainActor
private func sendScriptedKeys(_ tokens: [String], to controller: QuickSearchController) async {
    for token in tokens {
        guard let key = scriptedKey(token) else {
            fputs("MySearch snapshot: unknown key token \(token)\n", stderr)
            continue
        }
        guard let event = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: key.mods, timestamp: 0,
                                           windowNumber: controller.panel.windowNumber, context: nil,
                                           characters: key.chars, charactersIgnoringModifiers: key.chars,
                                           isARepeat: false, keyCode: key.code) else { continue }
        controller.panel.sendEvent(event)
        await waitForSettled(controller.model)
        print("key \(token): selected \(controller.model.selectedID ?? "none") · listFocused \(controller.model.listFocused)")
    }
}

private func scriptedKey(_ token: String) -> (code: UInt16, chars: String, mods: NSEvent.ModifierFlags)? {
    var mods: NSEvent.ModifierFlags = []
    let parts = token.split(separator: "-").map(String.init)
    for part in parts.dropLast() {
        switch part {
        case "ctrl": mods.insert(.control)
        case "cmd": mods.insert(.command)
        case "shift": mods.insert(.shift)
        default: return nil
        }
    }
    switch parts.last ?? "" {
    case "up": return (126, "", mods)
    case "down": return (125, "", mods)
    case "tab": return (48, "\t", mods)
    case "1": return (18, "1", mods)
    case "2": return (19, "2", mods)
    case "3": return (20, "3", mods)
    case "4": return (21, "4", mods)
    default: return nil
    }
}

/// n typed queries, each through the same path as a keystroke, one after another (a pause of 60 ms between them, like
/// typing). It prints the percentiles of the keystroke timing: keystroke to names answered (the service round trip
/// included), names answered to the rows painted (the part the app does), and the whole.
@MainActor
private func runQuickSearchBench(_ controller: QuickSearchController, count: Int) {
    let queries = ["i", "in", "inv", "invo", "invoice", "acme", "acme inv", "rechnung", "receipt", "statement",
                   "contract", "contoso", "globex", "2025", "pdf", "tax", "tailspin", "fabrikam", "budget", "notes"]
    // MSE_QS_BENCH_PAUSE_MS: the pause between keystrokes. 60 ms (the default) keeps a burst going, so every query after the
    // first takes the 40 ms debounce; above 250 ms each keystroke starts a new burst and fires at once.
    let pauseMs = UInt64(ProcessInfo.processInfo.environment["MSE_QS_BENCH_PAUSE_MS"] ?? "") ?? 60
    Task { @MainActor in
        for q in queries.prefix(count) {
            let before = controller.model.timing.samples.count
            controller.model.setQuery(q, typed: true)
            let start = qsNow()
            while controller.model.timing.samples.count == before && qsNow() - start < 10 {
                try? await Task.sleep(nanoseconds: 2_000_000)
            }
            try? await Task.sleep(nanoseconds: pauseMs * 1_000_000)
        }
        let samples = controller.model.timing.samples
        func percentile(_ values: [Double], _ p: Double) -> Double {
            let sorted = values.sorted()
            guard !sorted.isEmpty else { return 0 }
            return sorted[min(sorted.count - 1, Int((Double(sorted.count - 1) * p).rounded()))]
        }
        let service = samples.map(\.service), paint = samples.map(\.paint), total = samples.map(\.total)
        print(String(format: "bench: %d typed queries, %llu ms between them", samples.count, pauseMs))
        print(String(format: "keystroke to names answered   p50 %.1f ms  p95 %.1f ms", percentile(service, 0.5), percentile(service, 0.95)))
        print(String(format: "names answered to rows painted p50 %.2f ms  p95 %.2f ms  max %.2f ms",
                     percentile(paint, 0.5), percentile(paint, 0.95), paint.max() ?? 0))
        print(String(format: "keystroke to rows painted      p50 %.1f ms  p95 %.1f ms", percentile(total, 0.5), percentile(total, 0.95)))
        exit(0)
    }
}

/// The whole-disk settings of the snapshot states: the values the --disk stub answers with (stub_service.py DISK_SETTINGS).
extension WholeDiskSettings {
    static var snapshotFixture: WholeDiskSettings {
        var s = WholeDiskSettings()
        s.enabled = true
        s.roots = ["/Users/alex"]
        s.extraExcludes = ["node_modules"]
        s.priority = "balanced"
        return s
    }
}
