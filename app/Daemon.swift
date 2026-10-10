// `MySearch --daemon`: the background service's supervisor. launchd starts THIS executable (inside MySearch.app), and it
// spawns the Python service as its child. macOS attributes a child's file access to the program launchd started, so a single
// Full Disk Access grant on MySearch.app covers the service and the helper's crawl, instead of Homebrew's Python.
//
// Plain Foundation only: never NSApplication, hot keys, status item, URL handlers or any window. main.swift calls
// runDaemonSupervisor() before anything else is created. It exits with the child's status, so launchd's KeepAlive restarts it.

import Foundation

private func daemonLog(_ message: String) {
    let stamp = ISO8601DateFormatter().string(from: Date())
    FileHandle.standardError.write(Data("\(stamp) [mysearch --daemon] \(message)\n".utf8))
}

/// The folder with the Python project: config.json key `service.project` whenever it is set (the service writes it, so an
/// install that has run once never reaches the fallback). Otherwise fallbackProjectPath().
func daemonProjectPath() -> String {
    let env = ProcessInfo.processInfo.environment
    let data = env["MSE_HOME"].map { URL(fileURLWithPath: $0) }
        ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/MySearch")
    if let raw = try? Data(contentsOf: data.appendingPathComponent("config.json")),
       let json = try? JSONSerialization.jsonObject(with: raw) as? [String: Any],
       let service = json["service"] as? [String: Any],
       let project = service["project"] as? String, !project.isEmpty {
        return (project as NSString).expandingTildeInPath
    }
    return fallbackProjectPath()
}

/// The project when config.json does not name one. Tried in order, and the first folder with the package (mse/) and the
/// virtual environment's Python wins: the clone this app was built in (the app is <clone>/build/MySearch.app, so the
/// clone is two folders up), then the folder docs/INSTALL.md tells people to clone to.
/// With neither present, the INSTALL.md path is returned, so the error the supervisor logs names that path.
func fallbackProjectPath() -> String {
    let fm = FileManager.default
    let home = fm.homeDirectoryForCurrentUser
    let built = Bundle.main.bundleURL.deletingLastPathComponent().deletingLastPathComponent().path
    let candidates = [built,
                      home.appendingPathComponent("Developer/mysearch").path]
    func isProject(_ path: String) -> Bool {
        fm.fileExists(atPath: path + "/mse") && fm.isExecutableFile(atPath: path + "/.venv/bin/python")
    }
    return candidates.first(where: isProject) ?? candidates[1]
}

func runDaemonSupervisor() -> Never {
    let fm = FileManager.default
    let project = daemonProjectPath()
    let python = project + "/.venv/bin/python"
    guard fm.fileExists(atPath: project + "/mse"), fm.isExecutableFile(atPath: python) else {
        daemonLog("cannot start: no MySearch project or virtual environment at \(project) (expected \(python)). "
                  + "Set service.project in ~/Library/Application Support/MySearch/config.json, then run `uv sync --frozen` there.")
        exit(78)
    }

    var env = ProcessInfo.processInfo.environment
    // the same environment the LaunchAgent plist sets; filled in only when launchd did not already pass it
    if env["PATH"] == nil { env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" }
    if env["HF_HUB_OFFLINE"] == nil { env["HF_HUB_OFFLINE"] = "1" }
    if env["PYTHONUNBUFFERED"] == nil { env["PYTHONUNBUFFERED"] = "1" }
    // the helper bundled in this app (Contents/MacOS/msehelper): the crawl/OCR/watch children then belong to the app
    if let exe = Bundle.main.executableURL {
        let helper = exe.deletingLastPathComponent().appendingPathComponent("msehelper").path
        if fm.isExecutableFile(atPath: helper) { env["MSE_HELPER"] = helper }
    }
    // The service stops itself when this supervisor disappears without stopping it (mse/server.py, _watch_supervisor):
    // a SIGKILL of the supervisor cannot run any cleanup here, so the child must watch its own parent.
    env["MSE_SUPERVISED"] = "1"

    // `--daemon` is ours; everything else after it is handed to `mse serve` (e.g. a port)
    var serveArgs = ["-m", "mse", "serve"]
    if let i = CommandLine.arguments.firstIndex(of: "--daemon") { serveArgs += CommandLine.arguments[(i + 1)...] }

    let child = Process()
    child.executableURL = URL(fileURLWithPath: python)
    child.arguments = serveArgs
    child.currentDirectoryURL = URL(fileURLWithPath: project)
    child.environment = env
    child.standardInput = FileHandle.nullDevice      // stdout/stderr stay launchd's log file
    child.terminationHandler = { p in
        let status: Int32 = p.terminationReason == .exit ? p.terminationStatus : 128 + p.terminationStatus
        daemonLog("service ended (\(p.terminationReason == .exit ? "exit" : "signal") \(p.terminationStatus)); exiting \(status)")
        exit(status)
    }
    do { try child.run() } catch {
        daemonLog("cannot start \(python): \(error.localizedDescription)")
        exit(71)
    }
    daemonLog("started \(python) \(serveArgs.joined(separator: " ")) (pid \(child.processIdentifier)), helper \(env["MSE_HELPER"] ?? "data-dir fallback")")

    // Forward SIGTERM/SIGINT so the service's shutdown handler runs (JOBS.freeze() persists the queue); the child's
    // exit then ends us. If it hangs, kill it after 30 s.
    var sources: [DispatchSourceSignal] = []
    for sig in [SIGTERM, SIGINT] {
        signal(sig, SIG_IGN)
        let src = DispatchSource.makeSignalSource(signal: sig, queue: .global())
        src.setEventHandler {
            daemonLog("signal \(sig) received; forwarding to the service")
            kill(child.processIdentifier, sig)
            DispatchQueue.global().asyncAfter(deadline: .now() + 30) {
                if child.isRunning { daemonLog("service did not stop in 30 s; killing it"); kill(child.processIdentifier, SIGKILL) }
            }
        }
        src.resume()
        sources.append(src)
    }
    withExtendedLifetime(sources) { dispatchMain() }
}
