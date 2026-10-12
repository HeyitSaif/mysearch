// Headless checks of the pure Swift logic added for the DMG install: the update check's version order, schedule and request,
// the daemon's choice of Python, the snapshot PNG writer, and the Quick Search shortcut rules and key reading. Built and run
// by tests/test_setup_swift_logic.py:
//   swiftc app/Updater.swift app/Daemon.swift app/SnapshotPNG.swift app/Hotkeys.swift tests/swift/main.swift -o <exe>
// It prints one PASS or FAIL line per check, touches only folders it makes under the temporary directory, makes no
// network request and opens no window. It never reads or writes the shortcut preference (HotKey.load/save).

import AppKit
import Carbon.HIToolbox
import Foundation

var failures = 0
func check(_ label: String, _ ok: Bool, _ detail: String = "") {
    print("\(ok ? "PASS" : "FAIL") \(label)\(!ok && !detail.isEmpty ? " [\(detail)]" : "")")
    if !ok { failures += 1 }
}

// MARK: version order

let newer: [(String, String)] = [("0.1.3", "0.1.2"), ("v0.2.0", "0.1.9"), ("0.10.0", "0.9.9"), ("1.0.0", "0.99.99"), ("0.1.2.1", "0.1.2"),
                                 ("1.0.0", "1.0.0-rc.1"), ("1.0.0-rc.2", "1.0.0-rc.1"), ("1.0.0-rc.10", "1.0.0-rc.9"), ("1.0.0-beta", "1.0.0-alpha"),
                                 ("1.0", "0.9.9"), ("2", "1.9.9")]
for (a, b) in newer { check("\(a) is newer than \(b)", UpdateVersion.isNewer(a, than: b)) }
let notNewer: [(String, String)] = [("0.1.2", "0.1.2"), ("v0.1.2", "0.1.2"), ("0.1.1", "0.1.2"), ("0.1.2", "0.1.2.0"), ("1.0.0-rc.1", "1.0.0"),
                                    ("0.9.9", "0.10.0"), ("1.0.0+build7", "1.0.0"), ("1.0.0-alpha", "1.0.0-beta")]
for (a, b) in notNewer { check("\(a) is not newer than \(b)", !UpdateVersion.isNewer(a, than: b)) }
for bad in ["", "latest", "v", "1.2.x", "../../etc", "1.2.3; rm", "99999999.0.0", "1.2.3.4.5", "v1..2", "0.2.0-"] {
    check("tag \"\(bad)\" is not a version", UpdateVersion.parse(bad) == nil && !UpdateVersion.isValidTag(bad))
    check("tag \"\(bad)\" is never offered", !UpdateVersion.isNewer(bad, than: "0.1.2"))
}

func releaseJSON(_ fields: String) -> Data { Data("{\(fields)}".utf8) }
check("a release answer gives its tag", UpdateVersion.tag(fromReleaseJSON: releaseJSON(#""tag_name":"v0.2.0","html_url":"https://example.invalid/x""#)) == "v0.2.0")
check("a draft is ignored", UpdateVersion.tag(fromReleaseJSON: releaseJSON(#""tag_name":"v0.2.0","draft":true"#)) == nil)
check("a pre-release is ignored", UpdateVersion.tag(fromReleaseJSON: releaseJSON(#""tag_name":"v0.2.0","prerelease":true"#)) == nil)
check("a tag that is not a version is ignored", UpdateVersion.tag(fromReleaseJSON: releaseJSON(#""tag_name":"nightly""#)) == nil)
check("a non-JSON answer is ignored", UpdateVersion.tag(fromReleaseJSON: Data("<html>".utf8)) == nil)
check("no tag_name is ignored", UpdateVersion.tag(fromReleaseJSON: releaseJSON(#""name":"x""#)) == nil)

// MARK: schedule

let now = Date()
let hour: TimeInterval = 3600
check("due with no history", UpdateSchedule.isDue(now: now, lastCompleted: nil, lastAttempt: nil, enabled: true))
check("not due when switched off", !UpdateSchedule.isDue(now: now, lastCompleted: nil, lastAttempt: nil, enabled: false))
check("not due 23 h after a success", !UpdateSchedule.isDue(now: now, lastCompleted: now - 23 * hour, lastAttempt: now - 23 * hour, enabled: true))
check("due 25 h after a success", UpdateSchedule.isDue(now: now, lastCompleted: now - 25 * hour, lastAttempt: now - 25 * hour, enabled: true))
check("not due 10 min after a failed attempt", !UpdateSchedule.isDue(now: now, lastCompleted: nil, lastAttempt: now - 600, enabled: true))
check("due 2 h after a failed attempt", UpdateSchedule.isDue(now: now, lastCompleted: nil, lastAttempt: now - 2 * hour, enabled: true))

// MARK: the request

MainActor.assumeIsolated {
    let suite = "mse-setup-logic-\(ProcessInfo.processInfo.processIdentifier)"
    let defaults = UserDefaults(suiteName: suite)!
    let checker = UpdateChecker(defaults: defaults, version: "0.1.2")
    let r = checker.request()
    check("the request is a GET of the releases/latest URL", r.httpMethod == "GET"
          && r.url?.absoluteString == "https://api.github.com/repos/HeyitSaif/mysearch/releases/latest")
    check("the request carries exactly the user agent and a fixed language", r.allHTTPHeaderFields == ["User-Agent": "MySearch/0.1.2", "Accept-Language": "en"])
    check("the request has no body, no query and no cookies", r.httpBody == nil && r.url?.query == nil && r.httpShouldHandleCookies == false)
    check("the check is on by default", checker.enabled)
    check("nothing is available before the first check", checker.available == nil)
    defaults.set("v0.2.0", forKey: "UpdateLatestTag")
    let again = UpdateChecker(defaults: defaults, version: "0.1.2")
    check("a known newer release is shown at once, without a request", again.available == "0.2.0")
    check("its page is built from the tag", again.releaseURL.absoluteString == "https://github.com/HeyitSaif/mysearch/releases/tag/v0.2.0")
    let current = UpdateChecker(defaults: defaults, version: "0.2.0")
    check("the release already installed is not offered", current.available == nil)
    defaults.removePersistentDomain(forName: suite)
}

// MARK: the daemon's Python

let fm = FileManager.default
let root = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("mse-daemon-logic-\(UUID().uuidString)", isDirectory: true)

func makeProject(_ name: String) -> String {
    let p = root.appendingPathComponent(name, isDirectory: true)
    try! fm.createDirectory(at: p.appendingPathComponent("mse"), withIntermediateDirectories: true)
    try! fm.createDirectory(at: p.appendingPathComponent(".venv/bin"), withIntermediateDirectories: true)
    let py = p.appendingPathComponent(".venv/bin/python")
    fm.createFile(atPath: py.path, contents: Data("#!/bin/sh\n".utf8), attributes: [.posixPermissions: 0o755])
    return p.path
}

func makeResources(_ name: String, python: Bool = true, package: Bool = true) -> URL {
    let r = root.appendingPathComponent(name, isDirectory: true)
    try! fm.createDirectory(at: r.appendingPathComponent("runtime/bin"), withIntermediateDirectories: true)
    try! fm.createDirectory(at: r.appendingPathComponent("mse"), withIntermediateDirectories: true)
    if python { fm.createFile(atPath: r.appendingPathComponent("runtime/bin/python3").path, contents: Data("#!/bin/sh\n".utf8), attributes: [.posixPermissions: 0o755]) }
    if package { fm.createFile(atPath: r.appendingPathComponent("mse/__init__.py").path, contents: Data(), attributes: nil) }
    return r
}

let project = makeProject("clone")
let fallback = makeProject("fallback")
let bundle = makeResources("res")
let halfBundle = makeResources("res-no-python", python: false)
let noProject = root.appendingPathComponent("nothing").path

let a = resolveDaemonRuntime(configuredProject: project, resources: bundle, fallbackProject: fallback)
check("a configured project with a venv wins over the bundled runtime", a?.kind == .project && a?.python == project + "/.venv/bin/python" && a?.workdir == project && a?.extraEnvironment.isEmpty == true)
let b = resolveDaemonRuntime(configuredProject: nil, resources: bundle, fallbackProject: fallback)
check("without a project the bundled runtime is used", b?.kind == .bundled && b?.python == bundle.path + "/runtime/bin/python3" && b?.workdir == bundle.path)
check("the bundled runtime sets PYTHONHOME, PYTHONPATH, no user site and no byte code",
      b?.extraEnvironment == ["PYTHONHOME": bundle.path + "/runtime", "PYTHONPATH": bundle.path, "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"])
let c = resolveDaemonRuntime(configuredProject: noProject, resources: bundle, fallbackProject: fallback)
check("a configured project that is gone (a moved clone) falls through to the bundled runtime", c?.kind == .bundled)
let d = resolveDaemonRuntime(configuredProject: nil, resources: halfBundle, fallbackProject: fallback)
check("a bundle without its Python is not used; the fallback project is", d?.kind == .fallbackProject && d?.python == fallback + "/.venv/bin/python")
let e = resolveDaemonRuntime(configuredProject: nil, resources: nil, fallbackProject: noProject)
check("with nothing usable there is no runtime", e == nil)
let f = resolveDaemonRuntime(configuredProject: nil, resources: makeResources("res-no-pkg", package: false), fallbackProject: noProject)
check("a bundle without the mse package is not used", f == nil)

// MARK: the snapshot PNG writer

func chunk(_ type: String, _ data: [UInt8] = []) -> [UInt8] {
    let n = data.count
    return [UInt8(n >> 24 & 255), UInt8(n >> 16 & 255), UInt8(n >> 8 & 255), UInt8(n & 255)] + Array(type.utf8) + data + [0, 0, 0, 0]
}
let sig: [UInt8] = [0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]
let crafted = Data(sig + chunk("IHDR", [UInt8](repeating: 0, count: 13)) + chunk("iCCP", [UInt8](repeating: 7, count: 40)) + chunk("eXIf", [1, 2, 3])
                   + chunk("tEXt", Array("Author\0x".utf8)) + chunk("sRGB", [0]) + chunk("IDAT", [1, 2, 3, 4]) + chunk("tIME", [0, 0, 0, 0, 0, 0, 0]) + chunk("IEND"))
let stripped = strippingPNGChunks(crafted, dropping: snapshotPNGDroppedChunks)
check("iCCP, eXIf, text and tIME chunks are dropped, the rest kept in order", pngChunkTypes(stripped) == ["IHDR", "sRGB", "IDAT", "IEND"])
check("bytes that are not a PNG come back unchanged", strippingPNGChunks(Data("hello".utf8), dropping: ["iCCP"]) == Data("hello".utf8))

if let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 16, pixelsHigh: 16, bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                              colorSpaceName: .calibratedRGB, bytesPerRow: 0, bitsPerPixel: 0),
   let p3 = rep.retagging(with: .displayP3),
   let data = snapshotPNGData(p3) {
    let types = pngChunkTypes(data)
    check("a Display P3 bitmap is written without a profile or metadata chunk", !types.contains(where: { snapshotPNGDroppedChunks.contains($0) }) && types.first == "IHDR" && types.last == "IEND")
    check("the written PNG decodes", NSBitmapImageRep(data: data)?.pixelsWide == 16)
} else {
    check("a bitmap could be made for the PNG check", false)
}

// MARK: the Quick Search shortcut: its label, its rules (HotKey.refusal) and the reading of a key press (HotKey.capture)

func hk(_ code: Int, _ mods: Int = 0) -> HotKey { HotKey(code: UInt32(code), mods: UInt32(mods)) }
func refusalOf(_ c: ShortcutCapture) -> String? { if case .refused(let why) = c { return why }; return nil }
let cmd = Int(cmdKey), opt = Int(optionKey), ctl = Int(controlKey), sft = Int(shiftKey)
let typing = "Add ⌘, ⌃ or ⌥, so the shortcut does not take over normal typing."
let menuOnly = "Add ⌃ or ⌥. ⌘ and ⇧ alone are menu shortcuts in most apps."

check("the default Quick Search key is ⌥Space", HotKey.quickDefault.label == "⌥Space" && HotKey.quickDefault == hk(kVK_Space, opt))
check("the main window key is ⌃⌥Space", HotKey.main.label == "⌃⌥Space")
check("modifiers are shown in the order ⌃⌥⇧⌘", hk(kVK_ANSI_A, cmd | sft | opt | ctl).label == "⌃⌥⇧⌘A")
check("digits and function keys are named", hk(kVK_ANSI_1, cmd).label == "⌘1" && hk(kVK_F5).label == "F5")
check("Fn, Caps Lock and the keypad are not modifiers", HotKey.carbonMods([.function, .capsLock, .numericPad]) == 0
      && HotKey.carbonMods([.command, .function]) == UInt32(cmdKey))
check("the Cocoa modifiers map to the Carbon ones", HotKey.carbonMods([.control, .option, .shift, .command]) == UInt32(controlKey | optionKey | shiftKey | cmdKey))
check("Esc cancels", HotKey.capture(keyCode: kVK_Escape, flags: []) == .cancel)
check("Delete and forward Delete restore the default", HotKey.capture(keyCode: kVK_Delete, flags: []) == .reset
      && HotKey.capture(keyCode: kVK_ForwardDelete, flags: []) == .reset)
check("⌥Space is read as the default key", HotKey.capture(keyCode: kVK_Space, flags: [.option]) == .key(HotKey.quickDefault))
check("⌥⇧⌘A is read", HotKey.capture(keyCode: kVK_ANSI_A, flags: [.option, .shift, .command]) == .key(hk(kVK_ANSI_A, opt | sft | cmd)))
check("a function key needs no modifier", HotKey.capture(keyCode: kVK_F9, flags: []) == .key(hk(kVK_F9)))
check("a bare F5 is refused: the split pane copies with it", refusalOf(HotKey.capture(keyCode: kVK_F5, flags: [])) == "F5 already does something in MySearch.")
check("⇧F5 is read", HotKey.capture(keyCode: kVK_F5, flags: [.shift]) == .key(hk(kVK_F5, sft)))
check("⌃A is read (⌃ is enough for a letter)", HotKey.capture(keyCode: kVK_ANSI_A, flags: [.control]) == .key(hk(kVK_ANSI_A, ctl)))
check("⌥⌘A is read", HotKey.capture(keyCode: kVK_ANSI_A, flags: [.option, .command]) == .key(hk(kVK_ANSI_A, opt | cmd)))
check("⌥F3 is read (only the bare key is reserved)", HotKey.capture(keyCode: kVK_F3, flags: [.option]) == .key(hk(kVK_F3, opt)))
check("a bare letter is refused, so typing keeps working", refusalOf(HotKey.capture(keyCode: kVK_ANSI_A, flags: [])) == typing)
check("Caps Lock alone is not a modifier", refusalOf(HotKey.capture(keyCode: kVK_ANSI_A, flags: [.capsLock])) == typing)
check("⇧ alone is refused (it would type capitals)", refusalOf(HotKey.capture(keyCode: kVK_ANSI_A, flags: [.shift])) == typing)
check("a bare Return is refused", refusalOf(HotKey.capture(keyCode: kVK_Return, flags: [])) == typing)
check("⌘ with a letter is refused (it would take the app's menu key)", refusalOf(HotKey.capture(keyCode: kVK_ANSI_C, flags: [.command])) == menuOnly)
check("⌘⇧3 is refused: the rule needs ⌃ or ⌥ with a letter or digit", refusalOf(HotKey.capture(keyCode: kVK_ANSI_3, flags: [.command, .shift])) == menuOnly)
check("⌘Space is Spotlight's and refused", refusalOf(HotKey.capture(keyCode: kVK_Space, flags: [.command])) == "⌘Space belongs to macOS or another app you use.")
check("⌃⌥Space is the main window's and refused", refusalOf(HotKey.capture(keyCode: kVK_Space, flags: [.control, .option])) == "⌃⌥Space already shows the main window.")
check("⌘Q is refused", refusalOf(HotKey.capture(keyCode: kVK_ANSI_Q, flags: [.command])) == "⌘Q belongs to macOS or another app you use.")
check("⌘W is refused", refusalOf(HotKey.capture(keyCode: kVK_ANSI_W, flags: [.command])) == "⌘W belongs to macOS or another app you use.")
check("⌘Tab is refused", refusalOf(HotKey.capture(keyCode: kVK_Tab, flags: [.command])) == "⌘⇥ belongs to macOS or another app you use.")
check("⌘` (cycle an app's windows) is refused", refusalOf(HotKey.capture(keyCode: kVK_ANSI_Grave, flags: [.command])) == "⌘` belongs to macOS or another app you use.")
check("⌃⌘Q (lock the screen) is refused", refusalOf(HotKey.capture(keyCode: kVK_ANSI_Q, flags: [.control, .command])) == "⌃⌘Q belongs to macOS or another app you use.")
check("⌃⌘F (full screen) is refused", refusalOf(HotKey.capture(keyCode: kVK_ANSI_F, flags: [.control, .command])) == "⌃⌘F belongs to macOS or another app you use.")
check("⌥⌘D (the Dock) is refused", refusalOf(HotKey.capture(keyCode: kVK_ANSI_D, flags: [.option, .command])) == "⌥⌘D belongs to macOS or another app you use.")
check("a bare F3 (Mission Control) is refused", refusalOf(HotKey.capture(keyCode: kVK_F3, flags: [])) == "F3 belongs to macOS or another app you use.")
check("every reserved shortcut is refused by the rules", HotKey.reserved.allSatisfy { HotKey.refusal(for: $0) != nil })
check("⌥A and ⌃⌘K are accepted by the rules", HotKey.refusal(for: hk(kVK_ANSI_A, opt)) == nil && HotKey.refusal(for: hk(kVK_ANSI_K, cmd | ctl)) == nil)
check("an arrow key (no name) cannot be used", refusalOf(HotKey.capture(keyCode: kVK_LeftArrow, flags: [.function])) == "That key cannot be used for a shortcut.")
check("Esc and Delete are never shortcuts", HotKey.refusal(for: hk(kVK_Escape, opt)) != nil && HotKey.capture(keyCode: kVK_Delete, flags: [.option]) == .reset)
for k in HotKey.ownKeys {       // one check for each shortcut MySearch uses in its own menus and windows
    check("\(k.label) is refused: MySearch uses it", HotKey.refusal(for: k) == "\(k.label) already does something in MySearch.")
}
check("⌥⌘F is refused as the scope picker's key", refusalOf(HotKey.capture(keyCode: kVK_ANSI_F, flags: [.option, .command])) == "⌥⌘F already does something in MySearch.")
check("⌃1 is refused, and ⌃4 is not one of MySearch's", refusalOf(HotKey.capture(keyCode: kVK_ANSI_1, flags: [.control])) == "⌃1 already does something in MySearch."
      && HotKey.refusal(for: hk(kVK_ANSI_4, ctl)) == nil)

// MARK: replies from GitHub: any reply completes the day, only silence is retried (and then only after an hour)

func finish() -> Never {
    print(failures == 0 ? "ALL PASSED" : "\(failures) FAILED")
    exit(failures == 0 ? 0 : 1)
}

check("a 200 with a tag is a release", UpdateVersion.outcome(status: 200, data: releaseJSON(#""tag_name":"v0.2.0""#)) == .release("v0.2.0"))
check("a 404 is no release yet, not an error", UpdateVersion.outcome(status: 404, data: Data()) == .noRelease)
for status in [403, 429, 500, 502, 503] {
    if case .unusable = UpdateVersion.outcome(status: status, data: Data()) { check("status \(status) is unusable", true) } else { check("status \(status) is unusable", false) }
}
if case .unusable = UpdateVersion.outcome(status: 200, data: releaseJSON(#""tag_name":"nightly""#)) { check("a 200 whose tag is not a version is unusable", true) } else { check("a 200 whose tag is not a version is unusable", false) }

final class Counter: @unchecked Sendable { var n = 0; var seen: [String?] = [] }

@MainActor
func scenario(_ label: String, version: String = "0.1.2", reply: @escaping @Sendable () throws -> (Int, String),
              _ body: (UpdateChecker, UserDefaults, Counter) async -> Void) async {
    let suite = "mse-setup-logic-\(label)-\(ProcessInfo.processInfo.processIdentifier)"
    let defaults = UserDefaults(suiteName: suite)!
    defaults.removePersistentDomain(forName: suite)
    let counter = Counter()
    let checker = UpdateChecker(defaults: defaults, version: version, fetch: { request in
        counter.n += 1
        let (status, text) = try reply()
        return (Data(text.utf8), HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!)
    })
    checker.onChange = { counter.seen.append($0) }
    await body(checker, defaults, counter)
    defaults.removePersistentDomain(forName: suite)
}

Task { @MainActor in
    // the whole point: a reply that is not a release must not bring the request back every hour
    for (label, status, text) in [("404", 404, "{}"), ("403", 403, "{}"), ("500", 500, ""), ("garbage", 200, "<html>"), ("bad tag", 200, #"{"tag_name":"nightly"}"#)] {
        await scenario(label, reply: { (status, text) }) { checker, defaults, counter in
            await checker.checkIfDue()
            check("\(label): the first check sends one request", counter.n == 1)
            check("\(label): it completes the day's check", defaults.object(forKey: "UpdateLastCompleted") != nil)
            for _ in 0..<30 { await checker.checkIfDue() }                    // 30 hours of hourly timer ticks within the same day
            check("\(label): later ticks the same day send nothing", counter.n == 1, "requests: \(counter.n)")
            check("\(label): the next check is a day away", !UpdateSchedule.isDue(now: Date().addingTimeInterval(23 * 3600), lastCompleted: defaults.object(forKey: "UpdateLastCompleted") as? Date, lastAttempt: defaults.object(forKey: "UpdateLastAttempt") as? Date, enabled: true)
                  && UpdateSchedule.isDue(now: Date().addingTimeInterval(25 * 3600), lastCompleted: defaults.object(forKey: "UpdateLastCompleted") as? Date, lastAttempt: defaults.object(forKey: "UpdateLastAttempt") as? Date, enabled: true))
            check("\(label): nothing is offered", checker.available == nil)
            if status == 404 { check("404 is shown as no release yet", checker.noRelease && checker.lastError == nil) }
            else { check("\(label) is shown as a problem", checker.lastError != nil && !checker.noRelease) }
        }
    }
    await scenario("offline", reply: { throw URLError(.notConnectedToInternet) }) { checker, defaults, counter in
        await checker.checkIfDue()
        check("offline: one request was sent", counter.n == 1)
        check("offline: it does not count as a completed check", defaults.object(forKey: "UpdateLastCompleted") == nil && checker.lastChecked == nil)
        await checker.checkIfDue()
        check("offline: a second tick right away sends nothing", counter.n == 1)
        let attempt = defaults.object(forKey: "UpdateLastAttempt") as? Date
        check("offline: it is due again after an hour, not before", !UpdateSchedule.isDue(now: Date().addingTimeInterval(1800), lastCompleted: nil, lastAttempt: attempt, enabled: true)
              && UpdateSchedule.isDue(now: Date().addingTimeInterval(3700), lastCompleted: nil, lastAttempt: attempt, enabled: true))
        check("offline: the user sees a sentence", checker.lastError != nil)
    }
    await scenario("release", reply: { (200, #"{"tag_name":"v0.2.0","prerelease":false}"#) }) { checker, defaults, counter in
        await checker.checkIfDue()
        check("a newer release is offered", checker.available == "0.2.0" && counter.seen == ["0.2.0"], "\(String(describing: checker.available))")
        await checker.checkIfDue()
        check("and not asked for again the same day", counter.n == 1)
        check("the release page is the tag's", checker.releaseURL.absoluteString == "https://github.com/HeyitSaif/mysearch/releases/tag/v0.2.0")
    }
    await scenario("current", version: "0.2.0", reply: { (200, #"{"tag_name":"v0.2.0"}"#) }) { checker, _, counter in
        await checker.checkNow()
        check("the release already installed is not offered", checker.available == nil && counter.seen.isEmpty)
    }
    await scenario("manual", reply: { (404, "{}") }) { checker, _, counter in
        await checker.checkNow()
        await checker.checkNow()
        check("Check Now asks every time it is pressed", counter.n == 2)
    }
    await scenario("off", reply: { (200, #"{"tag_name":"v9.9.9"}"#) }) { _, defaults, counter in
        defaults.set(false, forKey: "UpdateCheckEnabled")
        let off = UpdateChecker(defaults: defaults, version: "0.1.2", fetch: { _ in counter.n += 1; throw URLError(.cancelled) })
        await off.checkIfDue()
        check("switched off: no request is sent", counter.n == 0 && !off.enabled)
    }
    await scenario("gone", reply: { (404, "{}") }) { checker, defaults, _ in
        defaults.set("v0.2.0", forKey: "UpdateLatestTag")
        await checker.checkNow()
        check("a 404 forgets an earlier release", defaults.string(forKey: "UpdateLatestTag") == nil)
    }
    finish()
}
dispatchMain()
