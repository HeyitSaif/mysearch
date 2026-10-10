// Global shortcuts with Carbon hot keys (no accessibility permission needed).
//   ⌃⌥Space  show or hide the main window          (HotKey.main, id 1)
//   ⌥Space   Quick Search (default; Settings › Mac app changes it, saved in UserDefaults, id 2)

import Cocoa
import Carbon.HIToolbox

/// A global shortcut as Carbon wants it, plus how to show it.
struct HotKey: Equatable {
    var code: UInt32
    var mods: UInt32

    static let main = HotKey(code: UInt32(kVK_Space), mods: UInt32(controlKey | optionKey))
    /// ⌥Space: one hand, launcher-like, and free here. ⌘Space is Raycast (raycastGlobalHotkey "Command-49"),
    /// ⌃Space / ⌃⌥Space switch input sources (⌃⌥Space is also the main window), ⌥⌘Space is Finder search.
    static let quickDefault = HotKey(code: UInt32(kVK_Space), mods: UInt32(optionKey))
    /// shortcuts that belong to macOS or to other apps
    static let reserved: [HotKey] = [
        HotKey(code: UInt32(kVK_Space), mods: UInt32(cmdKey)),
        HotKey(code: UInt32(kVK_Space), mods: UInt32(controlKey)),
        HotKey(code: UInt32(kVK_Space), mods: UInt32(cmdKey | optionKey)),
        HotKey(code: UInt32(kVK_Space), mods: UInt32(cmdKey | controlKey)),
        HotKey(code: UInt32(kVK_Tab), mods: UInt32(cmdKey)),
        HotKey(code: UInt32(kVK_ANSI_Q), mods: UInt32(cmdKey)),
        HotKey(code: UInt32(kVK_ANSI_W), mods: UInt32(cmdKey)),
    ]

    /// KeyboardEvent.code → virtual key code (ANSI layout positions)
    static let codes: [String: Int] = [
        "KeyA": kVK_ANSI_A, "KeyB": kVK_ANSI_B, "KeyC": kVK_ANSI_C, "KeyD": kVK_ANSI_D, "KeyE": kVK_ANSI_E, "KeyF": kVK_ANSI_F,
        "KeyG": kVK_ANSI_G, "KeyH": kVK_ANSI_H, "KeyI": kVK_ANSI_I, "KeyJ": kVK_ANSI_J, "KeyK": kVK_ANSI_K, "KeyL": kVK_ANSI_L,
        "KeyM": kVK_ANSI_M, "KeyN": kVK_ANSI_N, "KeyO": kVK_ANSI_O, "KeyP": kVK_ANSI_P, "KeyQ": kVK_ANSI_Q, "KeyR": kVK_ANSI_R,
        "KeyS": kVK_ANSI_S, "KeyT": kVK_ANSI_T, "KeyU": kVK_ANSI_U, "KeyV": kVK_ANSI_V, "KeyW": kVK_ANSI_W, "KeyX": kVK_ANSI_X,
        "KeyY": kVK_ANSI_Y, "KeyZ": kVK_ANSI_Z,
        "Digit0": kVK_ANSI_0, "Digit1": kVK_ANSI_1, "Digit2": kVK_ANSI_2, "Digit3": kVK_ANSI_3, "Digit4": kVK_ANSI_4,
        "Digit5": kVK_ANSI_5, "Digit6": kVK_ANSI_6, "Digit7": kVK_ANSI_7, "Digit8": kVK_ANSI_8, "Digit9": kVK_ANSI_9,
        "Space": kVK_Space, "Enter": kVK_Return, "Tab": kVK_Tab, "Backquote": kVK_ANSI_Grave, "Minus": kVK_ANSI_Minus,
        "Equal": kVK_ANSI_Equal, "BracketLeft": kVK_ANSI_LeftBracket, "BracketRight": kVK_ANSI_RightBracket,
        "Backslash": kVK_ANSI_Backslash, "Semicolon": kVK_ANSI_Semicolon, "Quote": kVK_ANSI_Quote, "Comma": kVK_ANSI_Comma,
        "Period": kVK_ANSI_Period, "Slash": kVK_ANSI_Slash,
        "F1": kVK_F1, "F2": kVK_F2, "F3": kVK_F3, "F4": kVK_F4, "F5": kVK_F5, "F6": kVK_F6, "F7": kVK_F7, "F8": kVK_F8,
        "F9": kVK_F9, "F10": kVK_F10, "F11": kVK_F11, "F12": kVK_F12,
    ]
    static let names: [Int: String] = {
        var n: [Int: String] = [kVK_Space: "Space", kVK_Return: "↩", kVK_Tab: "⇥", kVK_ANSI_Grave: "`", kVK_ANSI_Minus: "-",
                                kVK_ANSI_Equal: "=", kVK_ANSI_LeftBracket: "[", kVK_ANSI_RightBracket: "]", kVK_ANSI_Backslash: "\\",
                                kVK_ANSI_Semicolon: ";", kVK_ANSI_Quote: "'", kVK_ANSI_Comma: ",", kVK_ANSI_Period: ".", kVK_ANSI_Slash: "/"]
        for (k, v) in codes where k.hasPrefix("Key") || k.hasPrefix("Digit") || k.hasPrefix("F") && k.count <= 3 {
            n[v] = k.hasPrefix("Key") ? String(k.dropFirst(3)) : k.hasPrefix("Digit") ? String(k.dropFirst(5)) : k
        }
        return n
    }()
    var isFunctionKey: Bool { [kVK_F1, kVK_F2, kVK_F3, kVK_F4, kVK_F5, kVK_F6, kVK_F7, kVK_F8, kVK_F9, kVK_F10, kVK_F11, kVK_F12].contains(Int(code)) }

    var label: String {
        var s = ""
        if mods & UInt32(controlKey) != 0 { s += "⌃" }
        if mods & UInt32(optionKey) != 0 { s += "⌥" }
        if mods & UInt32(shiftKey) != 0 { s += "⇧" }
        if mods & UInt32(cmdKey) != 0 { s += "⌘" }
        return s + (HotKey.names[Int(code)] ?? "key \(code)")
    }
    /// for NSMenuItem: the key equivalent and its modifiers (display only; the hot key does the work)
    var menuKey: (String, NSEvent.ModifierFlags)? {
        guard let name = HotKey.names[Int(code)] else { return nil }
        let key = name == "Space" ? " " : name == "↩" ? "\r" : name == "⇥" ? "\t" : name.count == 1 ? name.lowercased() : ""
        if key.isEmpty { return nil }
        var f: NSEvent.ModifierFlags = []
        if mods & UInt32(controlKey) != 0 { f.insert(.control) }
        if mods & UInt32(optionKey) != 0 { f.insert(.option) }
        if mods & UInt32(shiftKey) != 0 { f.insert(.shift) }
        if mods & UInt32(cmdKey) != 0 { f.insert(.command) }
        return (key, f)
    }

    static func load() -> HotKey {
        let d = UserDefaults.standard
        guard d.object(forKey: "quickKeyCode") != nil else { return .quickDefault }
        return HotKey(code: UInt32(d.integer(forKey: "quickKeyCode")), mods: UInt32(d.integer(forKey: "quickKeyMods")))
    }
    func save() {
        UserDefaults.standard.set(Int(code), forKey: "quickKeyCode")
        UserDefaults.standard.set(Int(mods), forKey: "quickKeyMods")
    }
}

/// Owns the two Carbon registrations and routes their presses to the app. Snapshot mode never creates one.
final class HotKeyCenter {
    static let mainID: UInt32 = 1
    static let quickID: UInt32 = 2

    var onMain: (() -> Void)?
    var onQuick: (() -> Void)?
    private var refs: [UInt32: EventHotKeyRef] = [:]

    /// Installs the Carbon event handler once. Call it before registering keys.
    func install() {
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, event, userData in
            var id = EventHotKeyID()
            GetEventParameter(event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID), nil,
                              MemoryLayout<EventHotKeyID>.size, nil, &id)
            let me = Unmanaged<HotKeyCenter>.fromOpaque(userData!).takeUnretainedValue()
            DispatchQueue.main.async {
                if id.id == HotKeyCenter.quickID { me.onQuick?() } else { me.onMain?() }
            }
            return noErr
        }, 1, &spec, Unmanaged.passUnretained(self).toOpaque(), nil)
    }

    /// Registers the main shortcut and the given Quick Search key. Returns false when the Quick Search key is taken.
    @discardableResult
    func registerAll(quick: HotKey) -> Bool {
        register(HotKeyCenter.mainID, HotKey.main)
        return register(HotKeyCenter.quickID, quick)
    }

    @discardableResult
    func register(_ id: UInt32, _ key: HotKey) -> Bool {
        if let r = refs[id] { UnregisterEventHotKey(r); refs[id] = nil }
        var ref: EventHotKeyRef?
        let status = RegisterEventHotKey(key.code, key.mods, EventHotKeyID(signature: OSType(0x4D534548), id: id),  // 'MSEH'
                                         GetApplicationEventTarget(), 0, &ref)
        guard status == noErr, let ref else { return false }
        refs[id] = ref
        return true
    }

    func unregisterAll() {
        for (_, r) in refs { UnregisterEventHotKey(r) }
        refs.removeAll()
    }
}
