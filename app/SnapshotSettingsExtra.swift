// Off-screen renders for the Settings panes and the alert added with the Classic switch (ClassicUI.swift), and a text probe
// of the Classic entry points. Run the app with --snapshot (Snapshot.swift hands the run here first):
//   MSE_SNAPSHOT_SETTINGS=<name> MySearch --snapshot <out.png> --size 1280x760 --state home --fixture <dir>
// <name>: indexing (every Indexing key sent), indexing-bare (none sent: the pane shows only its note),
//         advanced (the Advanced pane), folder (the Folders detail with its Indexing overrides),
//         classic-alert (the alert shown while Classic is off), probe (facts as text on stdout, no picture),
//         mac-app (the Mac App pane, the Quick Search row idle with ⌥Space), mac-app-recording (the same row recording,
//         with the refusal shown for ⌘Space). Both force the shortcut in memory: no preference is read or written.
// MSE_SNAPSHOT_CLASSIC=on|off sets the Classic switch for this process only (ClassicUI.snapshotOverride, never the defaults).
// The screens are filled with fictional values in memory: nothing is polled and no service is asked.

import Cocoa
import SwiftUI

/// The windows and alerts kept alive until their render is written.
@MainActor private var settingsSnapshotHold: [AnyObject] = []

/// Returns true when MSE_SNAPSHOT_SETTINGS selects one of these renders (and this function has taken the run).
@MainActor
func runSettingsExtraSnapshot(_ o: SnapshotOptions) -> Bool {
    // the whole-disk states are chosen by --state (the render script passes no environment); the others by MSE_SNAPSHOT_SETTINGS
    let name: String
    switch o.state {
    case .settingsWholeDisk: name = "wholedisk"
    case .settingsFoldersDeep: name = "folders-deep"
    default:
        guard let env = ProcessInfo.processInfo.environment["MSE_SNAPSHOT_SETTINGS"], !env.isEmpty else { return false }
        name = env
    }
    switch ProcessInfo.processInfo.environment["MSE_SNAPSHOT_CLASSIC"] {
    case "on": ClassicUI.snapshotOverride = true
    case "off": ClassicUI.snapshotOverride = false
    default: break
    }
    NSApp.appearance = NSAppearance(named: o.dark ? .darkAqua : .aqua)
    DispatchQueue.main.asyncAfter(deadline: .now() + 30) { snapshotFail("timed out after 30 s", code: 2) }
    switch name {
    case "probe": runClassicProbe()
    case "classic-alert": runAlertRender(o)
    case "indexing", "indexing-bare", "advanced", "folder": runSettingsRender(o, name)
    case "wholedisk", "folders-deep": runSettingsRender(o, name)
    case "mac-app", "mac-app-recording":
        HotKey.snapshotKey = .quickDefault
        ShortcutRecorderModel.snapshotPreview = name == "mac-app-recording"
            ? ShortcutRecorderPreview(recording: true, warning: "⌘Space belongs to macOS or another app you use.") : nil
        runSettingsRender(o, name)
    default: snapshotFail("unknown MSE_SNAPSHOT_SETTINGS value \(name)", code: 2)
    }
    return true
}

// MARK: - the Settings window, filled with fictional values

@MainActor
private func runSettingsRender(_ o: SnapshotOptions, _ name: String) {
    let model = SWModel()
    model.statusState = true                       // the "not answering" banner stays away
    model.status = ["version": "snapshot", "model_present": true]
    model.settings = snapshotSettings(indexing: name != "indexing-bare")
    model.collections = snapshotCollections()
    switch name {
    case "advanced": model.section = .advanced
    case "wholedisk":
        model.section = .wholeDisk
        model.diskStatus = snapshotDiskStatus
    case "folders-deep": model.section = .folders
    case "mac-app", "mac-app-recording": model.section = .macApp
    case "folder":
        model.section = .folders
        model.openFolders = ["studio-notes"]
    default: model.section = .indexing             // indexing, indexing-bare
    }
    let window = NSWindow(contentRect: NSRect(x: -30000, y: -30000, width: o.width, height: o.height),
                          styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
    window.isReleasedWhenClosed = false
    window.appearance = NSApp.appearance
    let host = NSHostingController(rootView: SWRootView(m: model))
    host.sizingOptions = []
    window.contentViewController = host
    window.setContentSize(NSSize(width: o.width, height: o.height))
    settingsSnapshotHold += [window, model]
    DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
        MainActor.assumeIsolated { () -> Void in writeSnapshotFrame(window, to: o.output) }
    }
}

/// Settings as the service would send it. The indexing and embedding groups are sent only when `indexing` is true.
private func snapshotSettings(indexing: Bool) -> SWJSON {
    var s: SWJSON = [
        "search": ["limit": 20, "rerank_enabled": false, "hub_penalty": 0.6] as SWJSON,
        "schedule": ["nightly_enabled": true, "nightly_at": "02:30", "only_on_ac_power": true,
                     "scan_every_min": 60, "live_embed_limit": 200] as SWJSON,
        "worker": ["idle_unload_min": 10] as SWJSON,
        "translation": ["enabled": true, "corpus_bridge": true] as SWJSON,
        "wholedisk": WholeDiskSettings.snapshotFixture.json,
    ]
    if indexing {
        s["indexing"] = [
            "respect_gitignore": true,
            "index_code": false,
            "extra_excludes": ["old-exports", "*.bak"],
        ] as SWJSON
        s["indexing_info"] = ["prune_dirs_default": ["node_modules", ".git", "build", "dist", "__pycache__", ".venv", "DerivedData"],
                              "prune_dirs_effective": ["node_modules", ".git"]] as SWJSON
        s["embedding"] = ["mode": "fast", "priority": "balanced"] as SWJSON
    }
    return s
}

/// One fictional folder. Its override for .gitignore is "Off" and its override for code files is "use default".
private func snapshotCollections() -> [SWJSON] {
    // a fixed placeholder: the picture must not depend on the scratch folder the fixture lives in
    let root = "/Users/alex"
    let folder: SWJSON = [
        "name": "studio-notes",
        "path": root + "/Documents/Studio/Notes",
        "description": "Working notes and drafts",
        "in_global": true, "enabled": true, "exists": true,
        "aliases": ["n"], "exclude": ["*.tmp"], "metadata_only": ["Payroll/"],
        "contexts": [String: Any](),
        "media": ["images": false, "audio": false, "video": false, "pdf_pages": "smart"] as SWJSON,
        "stats": ["files": 128, "bytes": 2_400_000, "items": 512, "embed_pending": 0, "heavy_pending": 0,
                  "metadata_only": 3, "errors": 0] as SWJSON,
        "respect_gitignore": false,
        "index_code": NSNull(),
    ]
    return [folder]
}

// MARK: - the Classic alert

@MainActor
private func runAlertRender(_ o: SnapshotOptions) {
    let alert = ClassicUI.makeOffAlert()          // the same alert the route shows; it is laid out, never run
    let window = alert.window
    window.appearance = NSApp.appearance
    alert.layout()
    settingsSnapshotHold += [alert]
    DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
        MainActor.assumeIsolated { () -> Void in writeSnapshotFrame(window, to: o.output) }
    }
}

// MARK: - the probe (text, not a picture)

@MainActor
private func runClassicProbe() {
    let app = NSApp.delegate as? AppDelegate
    app?.classicStateChanged()                    // applies the switch (the override, when set) to the menus and open windows
    var lines = ["classic.enabled=\(ClassicUI.isEnabled)"]
    for (i, top) in (NSApp.mainMenu?.items ?? []).enumerated() {
        for item in top.submenu?.items ?? [] where item.title == "Open Classic MySearch" {
            lines.append("menu[\(i)] \(top.submenu?.title ?? "App"): classic hidden=\(item.isHidden) key=\"\(item.keyEquivalent)\"")
        }
    }
    lines.append("palette.classic=\(CommandPalette.builtIn().contains { $0.id == "classic" })")
    let home = SnapshotFixture.home ?? FileManager.default.homeDirectoryForCurrentUser
    let main = MainWindowController(frame: NSRect(x: -30000, y: -30000, width: 1280, height: 760), snapshot: true, startFolder: home)
    settingsSnapshotHold += [main]
    if let bar = main.window.toolbar {
        let inDefault = main.toolbarDefaultItemIdentifiers(bar).contains(ClassicUI.toolbarItemID)
        let item = main.toolbar(bar, itemForItemIdentifier: ClassicUI.toolbarItemID, willBeInsertedIntoToolbar: true)
        lines.append("main.toolbar: classic in default list=\(inDefault), item built=\(item != nil)")
    } else {
        lines.append("main.toolbar: none at this point")
    }
    if let app {
        let settings = SettingsWindowController(appDelegate: app)
        settingsSnapshotHold += [settings]
        let inDefault = settings.toolbarDefaultItemIdentifiers(NSToolbar(identifier: "probe")).contains(ClassicUI.settingsToolbarItemID)
        lines.append("settings.toolbar: classic settings in default list=\(inDefault)")
    }
    print(lines.joined(separator: "\n"))
    exit(0)
}

// MARK: - writing the picture

/// Renders a window's whole frame (title bar and content) off-screen and writes it as the same PNG as every snapshot.
@MainActor
private func writeSnapshotFrame(_ window: NSWindow, to path: String) -> Never {
    guard let frameView = window.contentView?.superview else { snapshotFail("the window has no frame view") }
    window.layoutIfNeeded()
    window.contentView?.layoutSubtreeIfNeeded()
    frameView.layoutSubtreeIfNeeded()
    guard let rep = frameView.bitmapImageRepForCachingDisplay(in: frameView.bounds) else { snapshotFail("could not create a bitmap") }
    window.effectiveAppearance.performAsCurrentDrawingAppearance {
        frameView.cacheDisplay(in: frameView.bounds, to: rep)
    }
    guard let data = snapshotPNGData(rep) else { snapshotFail("PNG encoding failed") }
    let url = URL(fileURLWithPath: path)
    try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
    do { try data.write(to: url) } catch { snapshotFail("could not write \(path)") }
    print(url.path)
    exit(0)
}

/// The whole-disk status the snapshot shows: the values the --disk stub answers with (stub_service.py DISK_STATUS).
@MainActor
private var snapshotDiskStatus: DiskStatus {
    DiskStatus(["enabled": true, "roots": ["/Users/alex"], "files_seen": 48213, "files_described": 31902,
                "images_labelled": 9127, "pdfs_described": 4411, "docs_described": 18102, "videos_described": 262,
                "failed": 0, "pending": 16311, "state": "describing", "paused_reason": NSNull(), "rate_per_s": 14.2,
                "eta_s": 1140, "last_error": NSNull(), "priority": "balanced", "last_sync": 1755165600.0,
                "last_reconcile": 1755162000.0, "name_index_ready": true])
}
