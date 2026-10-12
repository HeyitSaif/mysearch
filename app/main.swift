// Entry point. Normal runs are a regular app with a menu bar, hot keys and the web services; `--snapshot`
// runs a prohibited, off-screen render (see Snapshot.swift). The rest of the app lives in its own files.

import Cocoa

// --daemon: the background service's supervisor (Daemon.swift). Decided before anything else exists: no NSApplication,
// no hot keys, no status item, no URL handlers, no UI. It never returns.
if CommandLine.arguments.contains("--daemon") { runDaemonSupervisor() }

// Top-level code runs on the main thread, which is the main actor's executor.
MainActor.assumeIsolated {
    // --selftest-groups: the group operations against a dev service (MSE_API_BASE); prints and exits, no window (GroupEditor.swift).
    if CommandLine.arguments.contains("--selftest-groups") { GroupsSelfTest.run() }
    // --selftest-onboarding: the setup assistant's model against a dev service (MSE_API_BASE); no window, no download (OnboardingController.swift).
    if CommandLine.arguments.contains("--selftest-onboarding") { OnboardingSelfTest.run() }
    let snapshotOptions = SnapshotOptions.parse(CommandLine.arguments)
    let application = NSApplication.shared
    application.setActivationPolicy(snapshotOptions == nil ? .regular : .prohibited)
    let appDelegate = AppDelegate(snapshot: snapshotOptions)
    application.delegate = appDelegate
    application.run()
}
