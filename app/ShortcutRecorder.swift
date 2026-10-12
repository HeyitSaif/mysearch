// The Quick Search shortcut recorder (Settings › Mac app). It is a control, not a window: "Change…" starts it, the next key
// combination is saved at once (AppDelegate.applyQuickKey), Esc cancels and Delete restores ⌥Space. A combination that
// cannot be used (HotKey.refusal) stays on screen with its reason until another key is pressed or Esc is pressed.
// While it waits, the hot keys are off (AppDelegate.setHotKeysPaused), so the keys reach it and not Quick Search, and a
// local key monitor takes every key press, so a menu shortcut such as ⌘Q cannot fire meanwhile. It stops recording when
// focus leaves (another window or app) and when its pane goes away, and gives the hot keys back in every case but a save.

import Cocoa
import SwiftUI

/// Snapshot renders only (SnapshotSettingsExtra.swift): the recorder's state, drawn without an event monitor.
struct ShortcutRecorderPreview {
    var recording: Bool
    var warning: String?
}

@MainActor
final class ShortcutRecorderModel: ObservableObject {
    /// Snapshot renders only: set before the view is built, read once when the model is made.
    nonisolated(unsafe) static var snapshotPreview: ShortcutRecorderPreview?

    @Published private(set) var recording = false
    @Published private(set) var warning: String?
    private var monitor: Any?
    private var focusWatchers: [NSObjectProtocol] = []

    init() {
        if let p = Self.snapshotPreview {
            recording = p.recording
            warning = p.warning
        }
    }

    /// Starts recording. `apply` gets the key a press made (nil: the default) and returns the reason it was refused, or nil
    /// when the shortcut is now registered and saved.
    func start(apply: @escaping (HotKey?) -> String?) {
        guard !recording else { return }
        recording = true
        warning = nil
        setHotKeysPaused(true)
        monitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let self else { return event }
            MainActor.assumeIsolated { self.press(event, apply: apply) }
            return nil                                   // recorded or refused: the menus and the controls never see it
        }
        let center = NotificationCenter.default
        focusWatchers = [NSWindow.didResignKeyNotification, NSApplication.didResignActiveNotification].map { name in
            center.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in
                Task { @MainActor in self?.cancel() }
            }
        }
    }

    /// Leaves recording with no change and gives the hot keys back. Does nothing when not recording.
    func cancel() {
        guard recording else { return }
        stop(resumeHotKeys: true)
    }

    private func press(_ event: NSEvent, apply: (HotKey?) -> String?) {
        switch HotKey.capture(keyCode: Int(event.keyCode), flags: event.modifierFlags) {
        case .cancel:
            cancel()
        case .reset:
            attempt(apply(nil))
        case .key(let key):
            attempt(apply(key))
        case .refused(let why):
            warning = why
        }
    }

    /// One attempt's outcome. A refusal keeps the recorder waiting with the reason shown (the hot keys stay off); a saved
    /// shortcut ends recording, because the app has already registered it.
    private func attempt(_ refusal: String?) {
        if let refusal {
            warning = refusal
            setHotKeysPaused(true)                       // the app may have registered the old keys again after a failed save
        } else {
            stop(resumeHotKeys: false)
        }
    }

    private func stop(resumeHotKeys: Bool) {
        if let monitor { NSEvent.removeMonitor(monitor) }
        monitor = nil
        focusWatchers.forEach { NotificationCenter.default.removeObserver($0) }
        focusWatchers = []
        recording = false
        warning = nil
        if resumeHotKeys { setHotKeysPaused(false) }
    }

    private func setHotKeysPaused(_ paused: Bool) {
        (NSApp.delegate as? AppDelegate)?.setHotKeysPaused(paused)
    }
}

/// The control in the Quick Search row: the shortcut in its key symbols, or, while it records, "Type shortcut…" with the
/// reason for a refusal under it. `current` is the shortcut shown; `apply` saves a recorded one (nil: the default) and
/// returns the refusal, or nil when it is in use.
struct ShortcutRecorder: View {
    let current: HotKey
    let apply: (HotKey?) -> String?
    @StateObject private var model = ShortcutRecorderModel()

    var body: some View {
        VStack(alignment: .trailing, spacing: 6) {
            HStack(spacing: 8) {
                if model.recording {
                    pill("Type shortcut…", active: true)
                    Button("Cancel") { model.cancel() }.controlSize(.small)
                } else {
                    pill(current.label, active: false)
                    Button("Change…") { model.start(apply: apply) }.controlSize(.small)
                }
            }
            if model.recording {
                Text("Esc cancels · Delete restores \(HotKey.quickDefault.label)")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if let warning = model.warning {
                Text(warning)
                    .font(.caption).foregroundStyle(.red)
                    .multilineTextAlignment(.trailing)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: 300, alignment: .trailing)
            }
        }
        .onDisappear { model.cancel() }
    }

    private func pill(_ text: String, active: Bool) -> some View {
        Text(text)
            .font(.system(size: 14, design: .rounded).weight(.semibold))
            .padding(.horizontal, 10).padding(.vertical, 3)
            .background(RoundedRectangle(cornerRadius: 6).fill(Color(nsColor: .quaternaryLabelColor).opacity(0.4)))
            .overlay(RoundedRectangle(cornerRadius: 6).stroke(active ? Color.accentColor : .clear, lineWidth: 1.5))
    }
}
